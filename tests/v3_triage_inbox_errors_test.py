"""Explicit triage must distinguish incomplete inbox scans from empty inboxes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.v3_package_helpers import install, isolated_env, snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_triage as triage


class InboxDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='triage-inbox-errors-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()

    def test_healthy_shape_is_unchanged(self):
        expected = {'count': 0, 'oldest_days': None, 'attention': False,
                    'folders': [], 'has_yakala': False}
        self.assertEqual(triage.scan_inbox(self.vault, 0, self.state), expected)
        (self.state / 'inbox-report.json').write_text('{"folders": []}', encoding='utf-8')
        self.assertEqual(triage.scan_inbox(self.vault, 0, self.state), expected)

    def test_settings_exception_does_not_scan_or_expose_exception_text(self):
        with patch.object(triage, 'read_inbox_settings', side_effect=PermissionError('SECRET-PATH')), \
                patch.object(triage, 'inbox_report') as scan:
            report = triage.scan_inbox(self.vault, 0, self.state)
        scan.assert_not_called()
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['reason'], 'inbox_settings_unreadable')
        self.assertEqual(report['error'], 'PermissionError')
        self.assertNotIn('SECRET-PATH', json.dumps(report))

    def test_report_error_and_exception_are_preserved_without_raw_messages(self):
        for source in ('report', 'exception'):
            with self.subTest(source=source):
                kwargs = ({'return_value': {'folders': [], 'error': 'PermissionError', 'attention': False}}
                          if source == 'report' else {'side_effect': PermissionError('SECRET-PATH')})
                with patch.object(triage, 'inbox_report', **kwargs):
                    report = triage.scan_inbox(self.vault, 0, self.state)
                self.assertEqual(report['status'], 'unavailable')
                self.assertEqual(report['reason'], 'inbox_scan_failed')
                self.assertEqual(report['error'], 'PermissionError')
                self.assertTrue(report['attention'])
                rendered = triage.format_human({'inbox': report})
                self.assertIn('Taranamadi:', rendered)
                self.assertNotIn('Bekleyen kaynak: 0', rendered)
                self.assertNotIn('SECRET-PATH', json.dumps(report) + rendered)

    def test_partial_count_survives_and_error_text_is_not_echoed(self):
        rows = [{'folder': 'inbox', 'notes': 2, 'oldest_days': 3, 'attention': False,
                 'error': 'PermissionError'}]
        with patch.object(triage, 'inbox_report', return_value={'folders': rows, 'attention': True}):
            report = triage.scan_inbox(self.vault, 0, self.state)
        self.assertEqual(report['count'], 2)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['folders'], rows)
        human = triage.format_human({'inbox': report})
        self.assertIn('Taranabilen bekleyen kaynak: 2 (eksik tarama)', human)
        self.assertIn('2 not sayilabildi; tarama eksik', human)
        with patch.object(triage, 'inbox_report', return_value={'folders': [], 'error': 'SECRET-PATH'}):
            report = triage.scan_inbox(self.vault, 0, self.state)
        self.assertNotIn('SECRET-PATH', json.dumps(report))


class InstalledInboxDiagnosticTest(unittest.TestCase):
    def setUp(self):
        InboxDiagnosticTest.setUp(self)
        self.env = isolated_env(self.root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        # The CLI creates its runtime database on first use; establish it before read-only checks.
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'triage'],
                                cwd=self.vault, env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_modes(self):
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        results = []
        for mode in ([], ['--json'], ['--human']):
            result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'triage', *mode],
                                    cwd=self.vault, env=self.env, capture_output=True, text=True,
                                    encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            results.append(result.stdout if mode == ['--human'] else json.loads(result.stdout)['inbox'])
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)
        self.assertEqual(results[0], results[1])
        return results[0], results[2]

    def test_invalid_settings_do_not_fall_back_to_discovery(self):
        inbox = self.vault / 'inbox'
        inbox.mkdir()
        (inbox / 'pending.md').write_text('# Pending\n', encoding='utf-8')
        settings = self.state / 'inbox-report.json'
        for value in ('{broken SECRET-PATH', '{"folders": "bad"}'):
            with self.subTest(value=value):
                settings.write_text(value, encoding='utf-8')
                report, human = self.run_modes()
                self.assertEqual(report['status'], 'unavailable')
                self.assertEqual(report['reason'], 'inbox_settings_invalid')
                self.assertTrue(report['attention'])
                self.assertEqual(report['folders'], [])
                self.assertIn('inbox-report.json', human)
                self.assertIn('Taranamadi:', human)
                self.assertNotIn('Bekleyen kaynak: 0', human)
                self.assertNotIn('SECRET-PATH', json.dumps(report) + human)

    def test_missing_folder_does_not_render_as_empty(self):
        readable = self.vault / 'inbox'
        readable.mkdir()
        (readable / 'pending.md').write_text('# Pending\n', encoding='utf-8')
        (self.state / 'inbox-report.json').write_text(
            '{"folders": ["inbox", "Missing Captures"]}', encoding='utf-8')
        report, human = self.run_modes()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['count'], 1)
        self.assertEqual(report['folders'][1]['error'], 'not_found')
        self.assertIn('not_found', human)
        self.assertNotIn('Missing Captures: 0 not', human)
        self.assertIn('(eksik tarama)', human)

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory permission fixture')
    def test_unreadable_folder_with_real_installed_cli(self):
        inbox = self.vault / 'inbox'
        inbox.mkdir()
        note = inbox / 'pending.md'
        note.write_text('# Pending\n', encoding='utf-8')
        inbox.chmod(0)
        try:
            try:
                list(inbox.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest('Current user can read chmod(0) directories (e.g. root)')
            report, human = self.run_modes()
            self.assertEqual(report['status'], 'partial')
            self.assertEqual(report['folders'][0]['error'], 'PermissionError')
            self.assertIn('PermissionError', human)
            self.assertNotIn('inbox: 0 not', human)
            self.assertIn('(eksik tarama)', human)
        finally:
            inbox.chmod(0o700)
        self.assertEqual(note.read_text(encoding='utf-8'), '# Pending\n')


if __name__ == '__main__':
    unittest.main()
