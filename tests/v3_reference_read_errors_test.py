"""Unreadable instruction files are not a successful empty link check (#587)."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot

SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3_references as references


class ReferenceReadErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-reference-read-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / 'vault'
        self.vault.mkdir()

    def test_invalid_utf8_is_not_counted_as_checked(self):
        source = self.vault / 'AGENTS.md'
        original = b'# Instructions\n[[Missing note]]\n\xff'
        source.write_bytes(original)
        with self.assertRaises(UnicodeDecodeError):
            references.check(self.vault)
        self.assertEqual(source.read_bytes(), original)
        source.write_bytes(original[:-1])
        self.assertEqual(references.check(self.vault), {
            'checked_files': 1, 'dead_count': 1, 'dead': [
                {'file': 'AGENTS.md', 'line': 2, 'target': 'Missing note'}], 'truncated': False})

    def test_io_failure_after_a_valid_file_does_not_return_partial_success(self):
        good, failed = self.vault / 'AGENTS.md', self.vault / 'CLAUDE.md'
        good.write_text('[[Missing note]]\n', encoding='utf-8')
        failed.write_text('[[Other missing note]]\n', encoding='utf-8')
        before = snapshot(self.vault)
        original_read = Path.read_text
        injected = []

        def read(path, *args, **kwargs):
            if path.resolve() == failed.resolve():
                injected.append(path)
                raise OSError('private-path-and-content-canary')
            return original_read(path, *args, **kwargs)

        with patch.object(references, 'instruction_files', return_value=[good, failed]), \
                patch.object(Path, 'read_text', read):
            with self.assertRaises(OSError):
                references.check(self.vault)
        self.assertEqual(len(injected), 1)
        self.assertEqual(snapshot(self.vault), before)


class InstalledReferenceReadErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-reference-read-installed-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.source = self.vault / 'AGENTS.md'

    def doctor(self, *args):
        result = run_python(self.vault / 'beyin.py', ['doctor', *args], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        return result.stdout.decode('utf-8')

    def assert_unavailable(self, expected_error):
        before = snapshot(self.vault) if os.access(self.source, os.R_OK) else None
        for flags in ((), ('--json',)):
            with self.subTest(flags=flags):
                result = json.loads(self.doctor(*flags))
                self.assertEqual(result['instruction_references'], {
                    'status': 'unavailable', 'error': expected_error})
                self.assertIn('state_location', result)
                self.assertIn('preferences', result)
                self.assertIn('jev', result)
                self.assertNotEqual(result['status'], 'needs_attention')
        human = self.doctor('--human')
        warning = 'Talimat ve skill baglanti denetimi tamamlanamadi (bilgi): ' + expected_error + '.'
        self.assertIn(warning, human)
        self.assertNotIn('private-content-canary', human)
        if before is not None:
            self.assertEqual(snapshot(self.vault), before)

    def test_invalid_utf8_is_reported_by_installed_cli(self):
        original = self.source.read_bytes() + b'\n[[private-content-canary]]\n\xff'
        self.source.write_bytes(original)
        self.assert_unavailable('UnicodeDecodeError')
        self.assertEqual(self.source.read_bytes(), original)

    @unittest.skipIf(os.name == 'nt', 'POSIX read permissions required')
    def test_unreadable_instruction_file_is_reported_by_installed_cli(self):
        original = self.source.read_bytes()
        mode = self.source.stat().st_mode
        self.source.chmod(0)
        self.addCleanup(self.source.chmod, mode)
        try:
            self.source.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest('current user bypasses file read permissions')
        self.assert_unavailable('PermissionError')
        self.source.chmod(mode)
        self.assertEqual(self.source.read_bytes(), original)

    def test_readable_file_still_reports_dead_links_without_writes(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n[[Missing note]]\n')
        before = snapshot(self.vault)
        result = json.loads(self.doctor('--json'))
        report = result['instruction_references']
        self.assertEqual(report['dead_count'], 1)
        self.assertEqual(report['dead'][0]['target'], 'Missing note')
        self.assertNotEqual(result['status'], 'needs_attention')
        self.assertNotIn('denetimi tamamlanamadi', self.doctor('--human'))
        self.assertEqual(snapshot(self.vault), before)


class ReferenceHumanOutputTest(unittest.TestCase):
    def test_unavailable_report_is_visible_and_not_a_partial_link_report(self):
        spec = importlib.util.spec_from_file_location('reference_read_entry', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        text = entry.human_result({'status': 'observed_metadata', 'instruction_references': {
            'status': 'unavailable', 'error': 'OSError',
            'message': 'private-path-and-content-canary'}}, 'doctor', '3.12.0')
        self.assertIn('Talimat ve skill baglanti denetimi tamamlanamadi (bilgi): OSError.', text)
        self.assertNotIn('private-path-and-content-canary', text)
        self.assertNotIn('kirik baglanti (bilgi)', text)


if __name__ == '__main__':
    unittest.main()
