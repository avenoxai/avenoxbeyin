"""Unreadable inbox metadata is an incomplete scan, not a pending capture."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot

sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_hygiene as hygiene
import beyin_v3_triage as triage


class InboxFileErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-inbox-file-errors-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.inbox = self.vault / 'inbox'
        self.inbox.mkdir(parents=True)
        self.state.mkdir()
        self.good = self.inbox / 'readable.md'
        self.good.write_text('# Readable capture\n', encoding='utf-8')
        self.blocked = self.inbox / 'blocked.md'
        self.blocked.write_text('---\ntur: yakala\ndurum: islendi\ncreated: 2020-01-01\n---\n# Processed\n',
                                encoding='utf-8')

    def assert_incomplete(self):
        report = hygiene.inbox_report(self.vault, state=self.state)
        row = report['folders'][0]
        self.assertEqual(row['notes'], 1, row)
        self.assertEqual(row.get('error'), 'PermissionError', row)
        self.assertTrue(report['attention'])
        summary = triage.scan_inbox(self.vault, self.good.stat().st_mtime, self.state)
        self.assertEqual(summary.get('status'), 'partial', summary)
        self.assertEqual(summary['count'], 1)
        human = triage.format_human({'inbox': summary})
        self.assertIn('Taranabilen bekleyen kaynak: 1 (eksik tarama)', human)
        self.assertNotIn('PRIVATE-CANARY', json.dumps(report) + json.dumps(summary) + human)

    def test_open_failure_does_not_guess_pending_state_from_mtime(self):
        original = Path.open
        before = self.blocked.read_bytes()
        def open_file(path, *args, **kwargs):
            if path.resolve() == self.blocked.resolve():
                raise PermissionError('PRIVATE-CANARY')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            self.assert_incomplete()
        self.assertEqual(self.blocked.read_bytes(), before)

    def test_stream_failure_does_not_guess_pending_state_from_mtime(self):
        original = Path.open
        class FailedStream:
            def read(self, *args):
                raise PermissionError('PRIVATE-CANARY')
        @contextlib.contextmanager
        def open_file(path, *args, **kwargs):
            if path.resolve() == self.blocked.resolve():
                yield FailedStream()
            else:
                with original(path, *args, **kwargs) as stream:
                    yield stream
        with patch.object(Path, 'open', open_file):
            self.assert_incomplete()

    def test_readable_bad_date_still_uses_mtime_and_processed_card_stays_excluded(self):
        self.good.write_text('---\ncreated: not-a-date\n---\n# Readable capture\n', encoding='utf-8')
        report = hygiene.inbox_report(self.vault, state=self.state, now=self.good.stat().st_mtime)
        self.assertEqual(report['folders'][0]['notes'], 1)
        self.assertEqual(report['folders'][0]['oldest_days'], 0)
        self.assertNotIn('error', report['folders'][0])
        self.assertFalse(report['attention'])

    def test_index_filename_is_excluded_before_opening(self):
        index = self.inbox / 'Inbox.md'
        index.write_text('# Inbox index\n', encoding='utf-8')
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path.resolve() == index.resolve():
                self.fail('Known index filename must not be read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            report = hygiene.inbox_report(self.vault, state=self.state)
        self.assertEqual(report['folders'][0]['notes'], 1)
        self.assertNotIn('error', report['folders'][0])


class InstalledInboxFileErrorsTest(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions required')
    def test_permission_loss_is_partial_in_cli_and_recovers_without_writes(self):
        with tempfile.TemporaryDirectory(prefix='v3-inbox-file-installed-') as td:
            root = Path(td).resolve()
            vault, state = root / 'vault', root / 'state'
            vault.mkdir()
            env = isolated_env(root / 'home')
            done = install(vault, state, env)
            self.assertEqual(done.returncode, 0, done.stderr)
            def cli(*args):
                done = run_python(vault / 'beyin.py', args, vault, env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stderr, b'')
                return done.stdout.decode('utf-8')
            cli('preferences', '--inbox-report', 'on')
            cli('doctor')
            inbox = vault / 'inbox'
            inbox.mkdir(exist_ok=True)
            (inbox / 'readable.md').write_text('# Waiting\n', encoding='utf-8')
            blocked = inbox / 'blocked.md'
            for front, restored_count in (('tur: yakala\ndurum: islendi\n', 1), ('', 2)):
                with self.subTest(processed=bool(front)):
                    blocked.write_text('---\n' + front + 'created: 2020-01-01\n---\n# PRIVATE-CANARY\n',
                                       encoding='utf-8')
                    before_vault, before_state = snapshot(vault), snapshot(state)
                    before_times = {p: p.stat().st_mtime_ns for p in vault.rglob('*.md')}
                    blocked.chmod(0)
                    try:
                        try:
                            blocked.read_bytes()
                        except PermissionError:
                            pass
                        else:
                            self.skipTest('current user bypasses file permissions')
                        for flags in ((), ('--json',)):
                            report = json.loads(cli('triage', *flags))['inbox']
                            self.assertEqual(report.get('status'), 'partial', report)
                            self.assertEqual(report['count'], 1)
                            self.assertEqual(report['folders'][0]['error'], 'PermissionError')
                            self.assertNotIn('PRIVATE-CANARY', json.dumps(report))
                        self.assertIn('Taranabilen bekleyen kaynak: 1 (eksik tarama)', cli('triage', '--human'))
                        doctor = json.loads(cli('doctor'))['inbox']
                        self.assertEqual(doctor['folders'][0]['notes'], 1)
                        self.assertEqual(doctor['folders'][0]['error'], 'PermissionError')
                        self.assertTrue(doctor['attention'])
                        self.assertIn('inbox (okunamadi: PermissionError)', cli('doctor', '--human'))
                    finally:
                        blocked.chmod(0o600)
                    self.assertEqual(snapshot(vault), before_vault)
                    self.assertEqual(snapshot(state), before_state)
                    self.assertEqual({p: p.stat().st_mtime_ns for p in before_times}, before_times)
                    recovered = json.loads(cli('triage'))['inbox']
                    self.assertEqual(recovered['count'], restored_count)
                    self.assertNotIn('status', recovered)


if __name__ == '__main__':
    unittest.main()
