"""An incomplete explicit audit must not report that memory is balanced (#587)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.append(str(ROOT / 'scripts'))
import beyin_v3_audit as audit


class AuditReadErrorsTest(unittest.TestCase):
    BODY = 'database deployment protocol testing configuration pipeline reliability\n'
    ERROR = {'error': 'OSError', 'message': 'audit source scan failed'}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='audit-read-errors-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.a = self.write('knowledge/a.md', self.BODY)
        self.b = self.write('knowledge/b.md', self.BODY)
        self.env = inherited_env(HOME=str(self.root / 'home'), USERPROFILE=str(self.root / 'home'),
                                 XDG_CONFIG_HOME=str(self.root / 'config'),
                                 XDG_DATA_HOME=str(self.root / 'data'),
                                 XDG_STATE_HOME=str(self.root / 'xdg-state'),
                                 XDG_CACHE_HOME=str(self.root / 'cache'),
                                 BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1')

    def write(self, relative, body):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return path

    def block(self, path):
        if os.name == 'nt':
            self.skipTest('POSIX read permissions required; injected failures cover every platform')
        mode = path.stat().st_mode
        is_dir = path.is_dir()
        path.chmod(0)
        self.addCleanup(path.chmod, mode)
        try:
            list(path.iterdir()) if is_dir else path.read_bytes()
        except PermissionError:
            return mode
        self.skipTest('current user bypasses read permissions')

    def assert_scan_error(self, target=None):
        with self.assertRaises(OSError) as caught:
            audit.audit_memory(self.vault, target_file=target)
        self.assertIs(type(caught.exception), OSError)
        self.assertEqual(str(caught.exception), self.ERROR['message'])

    def check_installed_failure(self, path):
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8'))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.a, self.b)}
        mode = self.block(path)
        for flag in ([], ['--json'], ['--human']):
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'audit', *flag],
                                        cwd=self.vault, env=self.env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=60)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, '')
                if flag == ['--human']:
                    self.assertIn('Islem tamamlanamadi: audit source scan failed', result.stderr)
                    self.assertNotIn('hafiza durumu dengeli', result.stderr)
                else:
                    self.assertEqual(json.loads(result.stderr), self.ERROR)
                self.assertNotIn(str(path), result.stderr)
                self.assertNotIn('PermissionError', result.stderr)
        path.chmod(mode)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.a, self.b)})
        self.assertEqual(len(audit.audit_memory(self.vault)['findings']), 1)

    def test_installed_unreadable_note_is_not_a_healthy_report(self):
        self.check_installed_failure(self.b)

    def test_installed_unreadable_directory_is_not_an_empty_report(self):
        self.check_installed_failure(self.b.parent)

    def test_late_read_error_discards_findings_and_hides_private_exception(self):
        blocked = self.write('knowledge/c.md', self.BODY)
        original = Path.read_text

        class PrivateCanaryError(OSError):
            pass

        def fail(path, *args, **kwargs):
            if path == blocked:
                raise PrivateCanaryError('PRIVATE-PATH-AND-CONTENT-CANARY')
            return original(path, *args, **kwargs)

        walk = [(str(self.a.parent), [], ['a.md', 'b.md', 'c.md'])]
        with patch.object(Path, 'read_text', fail), patch.object(audit.os, 'walk', return_value=walk):
            self.assertEqual(len(audit.scan_audit_candidates(self.vault)), 2)
            self.assert_scan_error()

    def test_directory_enumeration_error_is_not_a_complete_scan(self):
        original = os.scandir

        def fail(path):
            if Path(path) == self.a.parent:
                raise PermissionError('PRIVATE-DIRECTORY-CANARY')
            return original(path)

        with patch.object(audit.os, 'scandir', fail):
            self.assertEqual(audit.scan_audit_candidates(self.vault), [])
            self.assert_scan_error()

    def test_explicit_archive_target_read_error_is_sanitized(self):
        target = self.write('archive/target.md', self.BODY)
        original = Path.read_text

        def fail(path, *args, **kwargs):
            if path == target:
                raise OSError('PRIVATE-TARGET-CANARY')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', fail):
            self.assertEqual(len(audit.scan_audit_candidates(self.vault)), 2)
            self.assert_scan_error('archive/target.md')

    def test_excluded_directories_remain_unread_and_invalid_frontmatter_is_skipped(self):
        for name in ('.hidden', 'archive', 'receipts', 'daily', 'node_modules'):
            self.write(name + '/canary.md', 'PRIVATE-CANARY\n')
        self.write('knowledge/bad.md', '---\n{broken json}\n---\n' + self.BODY)
        original = os.scandir

        def guarded(path):
            if Path(path).name in ('.hidden', 'archive', 'receipts', 'daily', 'node_modules'):
                raise AssertionError('excluded directory must not be visited')
            return original(path)

        with patch.object(audit.os, 'scandir', guarded):
            report = audit.audit_memory(self.vault)
        self.assertEqual(report['notes_scanned'], 2)
        self.assertEqual(len(report['findings']), 1)
        self.assertNotIn('PRIVATE-CANARY', json.dumps(report))

    def test_default_helpers_keep_best_effort_read_behavior(self):
        with patch.object(Path, 'read_text', side_effect=OSError('PRIVATE-CANARY')):
            self.assertIsNone(audit._auditable(self.a, 'knowledge/a.md'))
            self.assertEqual(audit.scan_audit_candidates(self.vault), [])
            self.assert_scan_error()

    def test_healthy_report_shape_and_missing_target_behavior_are_unchanged(self):
        report = audit.audit_memory(self.vault)
        self.assertEqual(set(report), {'schema', 'vault', 'target', 'notes_scanned', 'pairs_compared',
                                       'threshold', 'min_shared', 'findings'})
        self.assertEqual(report['notes_scanned'], 2)
        with self.assertRaises(FileNotFoundError):
            audit.audit_memory(self.vault, target_file='knowledge/missing.md')


if __name__ == '__main__':
    unittest.main()
