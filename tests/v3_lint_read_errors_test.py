"""A source lost after lint indexing must not become a successful partial report."""
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_lint as linter


class LintReadErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / 'vault'
        self.vault.mkdir()
        self.note = self.vault / 'note.md'
        self.note.write_text('# Note\n[[missing]]\n', encoding='utf-8')

    def assert_late_failure(self, mutate):
        build = linter.build_vault_index

        def indexed_then_changed(*args, **kwargs):
            result = build(*args, **kwargs)
            self.assertIn('note.md', result['by_rel'])
            mutate()
            return result

        with patch.object(linter, 'build_vault_index', indexed_then_changed):
            with self.assertRaises(OSError) as caught:
                linter.lint_vault(self.vault)
        self.assertEqual(str(caught.exception), 'lint source read failed')
        self.assertIs(type(caught.exception), OSError)

    def test_source_removed_after_index_is_not_success(self):
        self.assert_late_failure(self.note.unlink)

    def test_invalid_encoding_after_index_is_not_success(self):
        self.assert_late_failure(lambda: self.note.write_bytes(b'\xffPRIVATE-CANARY'))
        self.assertEqual(self.note.read_bytes(), b'\xffPRIVATE-CANARY')

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions required')
    def test_source_becomes_unreadable_after_index(self):
        mode = self.note.stat().st_mode
        self.note.chmod(0)
        try:
            self.note.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest('current user bypasses read permissions')
        finally:
            self.note.chmod(mode)
        self.addCleanup(self.note.chmod, mode)
        self.assert_late_failure(lambda: self.note.chmod(0))

    def test_healthy_source_still_reports_broken_link(self):
        result = linter.lint_vault(self.vault)
        self.assertEqual(result['status'], 'issues_found')
        self.assertEqual(result['broken_links_count'], 1)

    def test_installed_cli_reports_failure_without_private_path(self):
        state = self.vault.parent / 'state'
        env = inherited_env(BEYIN_V3_NO_SPAWN='1')
        result = install(self.vault, state, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        wrapper = self.vault / 'late_read.py'
        wrapper.write_text('''
import runpy, sys
from pathlib import Path
vault = Path(sys.argv[1])
sys.path.insert(0, str(vault / '.claude/scripts'))
import beyin_v3_lint as lint
build = lint.build_vault_index
def changed(*args, **kwargs):
    result = build(*args, **kwargs)
    (vault / 'note.md').unlink()
    return result
lint.build_vault_index = changed
sys.argv = [str(vault / 'beyin.py'), 'lint', '--json']
runpy.run_path(sys.argv[0], run_name='__main__')
''', encoding='utf-8')
        result = subprocess.run([sys.executable, str(wrapper), str(self.vault)],
                                cwd=self.vault, env=env, capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(json.loads(result.stderr),
                         {'error': 'OSError', 'message': 'lint source read failed'})
        self.assertNotIn(str(self.vault), result.stderr)


if __name__ == '__main__':
    unittest.main()
