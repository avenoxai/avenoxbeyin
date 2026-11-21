"""A companion read failure is not evidence of an empty companion."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.v3_package_helpers import install, isolated_env, run_python, snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_triage as triage
from beyin_v3_hygiene import DEFAULT_COMPANION


class CompanionScanErrorsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='triage-companion-errors-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.companion = self.vault / DEFAULT_COMPANION
        self.companion.mkdir(parents=True)

    def seed(self):
        (self.companion / 'Kurallar.md').write_text('# Kurallar\n- Şifre\n- ılık\n', encoding='utf-8', newline='\n')
        (self.companion / 'Threads.md').write_text('## Açık\n### Müşteri\n', encoding='utf-8', newline='\n')
        (self.companion / 'Last-Session.md').write_text('## 2026-11-10 12:30 · İstanbul\n', encoding='utf-8', newline='\n')

    def test_admitted_read_and_stat_errors_keep_the_readable_summary(self):
        self.seed()
        original_lstat, original_read = Path.lstat, Path.read_text

        def stat(path):
            if path == self.companion / 'Threads.md':
                raise PermissionError('PRIVATE-ERROR-CANARY')
            return original_lstat(path)

        def read(path, *args, **kwargs):
            if path == self.companion / 'Last-Session.md':
                raise FileNotFoundError('PRIVATE-ERROR-CANARY')
            return original_read(path, *args, **kwargs)

        with patch.object(Path, 'lstat', stat), patch.object(Path, 'read_text', read):
            result = triage.triage(self.vault, self.state)
        summary = result['companion']
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['unreadable_sources'], ['Last-Session.md', 'Threads.md'])
        self.assertEqual((summary['rules_count'], summary['active_threads'], summary['last_session']), (2, 0, None))
        human = triage.format_human(result)
        self.assertIn('Eslikci taramasi eksik', human)
        self.assertIn('Kurallar: 2 kural', human)
        self.assertIn('Acik is parcaciklari: taranamadi', human)
        self.assertIn('Son oturum: taranamadi', human)
        self.assertNotIn('PRIVATE-ERROR-CANARY', json.dumps(result) + human)

    def test_absent_nonregular_and_healthy_summary_shape_is_unchanged(self):
        (self.companion / 'Kurallar.md').mkdir()
        expected = {'rules_count': 0, 'active_threads': 0, 'last_session': None, 'active_presence': 0}
        self.assertEqual(triage.scan_companion(self.vault, self.state), expected)
        (self.companion / 'Kurallar.md').rmdir()
        self.seed()
        expected.update(rules_count=2, active_threads=1, last_session='2026-11-10 12:30')
        self.assertEqual(triage.scan_companion(self.vault, self.state), expected)

    def test_invalid_utf8_is_unreadable_without_reporting_content(self):
        sources = (
            ('Kurallar.md', 'rules_count', 0, 'Kurallar', b'- PRIVATE-CONTENT-CANARY\n'),
            ('Threads.md', 'active_threads', 0, 'Acik is parcaciklari',
             b'## Acik\n### PRIVATE-CONTENT-CANARY\n'),
            ('Last-Session.md', 'last_session', None, 'Son oturum',
             b'## 2026-11-10 12:30 PRIVATE-CONTENT-CANARY\n'),
        )
        healthy = {'rules_count': 2, 'active_threads': 1, 'last_session': '2026-11-10 12:30',
                   'active_presence': 0}
        for filename, key, value, label, content in sources:
            with self.subTest(filename=filename):
                self.seed()
                (self.companion / filename).write_bytes(content + b'\xff\xfe')
                result = triage.triage(self.vault, self.state)
                expected = dict(healthy, status='partial', unreadable_sources=[filename])
                expected[key] = value
                self.assertEqual(result['companion'], expected)
                if filename == 'Threads.md':
                    self.assertEqual(result['threads']['status'], 'unavailable')
                human = triage.format_human(result)
                self.assertIn(f'{label}: taranamadi', human)
                self.assertNotIn('PRIVATE-CONTENT-CANARY', json.dumps(result) + human)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_installed_cli_reports_unreadable_rules_without_writing(self):
        self.seed()
        env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        cli = self.vault / 'beyin.py'
        seeded = run_python(cli, ['triage'], self.vault, env)
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        before = snapshot(self.vault), snapshot(self.state)
        rules = self.companion / 'Kurallar.md'
        mode = rules.stat().st_mode & 0o777
        rules.chmod(0)
        try:
            try:
                rules.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest('Current executor bypasses POSIX permissions')
            result = run_python(cli, ['triage', '--json'], self.vault, env)
            self.assertEqual((result.returncode, result.stderr), (0, b''))
            report = json.loads(result.stdout)['companion']
            self.assertEqual((report['status'], report['unreadable_sources']), ('partial', ['Kurallar.md']))
            self.assertEqual(report['active_threads'], 1)
            human = run_python(cli, ['triage', '--human'], self.vault, env)
            self.assertEqual((human.returncode, human.stderr), (0, b''))
            self.assertIn(b'Eslikci taramasi eksik', human.stdout)
            self.assertIn(b'Kurallar: taranamadi', human.stdout)
        finally:
            rules.chmod(mode)
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)


if __name__ == '__main__':
    unittest.main()
