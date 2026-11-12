"""Explicit listing must not disguise an unreadable learning ledger as empty (#587)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import clean_environ, install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_learning as learning


class LearningListErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='private-list-canary-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.record = learning.stage_candidate(self.state, 'list-test', 'Private candidate canary')
        self.ledger = self.state / learning.LEARNING_STATE_FILE
        self.original = self.ledger.read_bytes()

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions required')
    def test_installed_cli_reports_unreadable_ledger_without_private_data(self):
        vault = self.root / 'vault'
        vault.mkdir()
        env = isolated_env(self.root / 'home')
        installed = install(vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before = self.ledger.stat()
        mode = stat.S_IMODE(before.st_mode)
        try:
            self.ledger.chmod(0)
            try:
                self.ledger.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest('effective user bypasses file permissions')
            self.assertEqual(learning.list_candidates(self.state), [])
            self.assertFalse(learning.has_candidates(self.state))
            for prefix in ([], ['--json'], ['--human']):
                with self.subTest(prefix=prefix):
                    result = run_python(vault / 'beyin.py', prefix + ['learn', '--list'], vault, env)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertEqual(result.stdout, b'')
                    if prefix == ['--human']:
                        self.assertIn(b'learning candidate listing failed', result.stderr)
                    else:
                        self.assertEqual(json.loads(result.stderr),
                                         {'error': 'OSError', 'message': 'learning candidate listing failed'})
                    for canary in (str(self.root), self.record['candidate'], 'Traceback'):
                        self.assertNotIn(canary, (result.stdout + result.stderr).decode())
        finally:
            self.ledger.chmod(mode)
        after = self.ledger.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))
        self.assertEqual(self.ledger.read_bytes(), self.original)
        result = run_python(vault / 'beyin.py', ['learn', '--list'], vault, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'candidates': [self.record]})

    def test_embedded_cli_returns_sanitized_error_without_printing(self):
        self.check_embedded_cli_read_error()

    def test_embedded_cli_read_error_through_state_alias(self):
        # macOS temporary paths can use /var while the CLI resolves /private/var.
        alias = self.root / 'state-alias'
        try:
            alias.symlink_to(self.state, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('directory symlink creation is unavailable')
        self.state = alias
        self.ledger = alias / learning.LEARNING_STATE_FILE
        self.check_embedded_cli_read_error()

    def check_embedded_cli_read_error(self):
        vault = self.root / 'vault'
        vault.mkdir()
        env = isolated_env(self.root / 'home')
        installed = install(vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        spec = importlib.util.spec_from_file_location('learning_list_cli_test', ROOT / 'scripts/beyin_v3.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        stdout, stderr = io.StringIO(), io.StringIO()
        original_open = open
        ledger = self.ledger.resolve()
        blocked_reads = []

        def unreadable(path, *args, **kwargs):
            if Path(path).resolve() == ledger:
                blocked_reads.append(path)
                raise PermissionError(13, 'Private candidate canary', str(self.ledger))
            return original_open(path, *args, **kwargs)

        with clean_environ(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch('builtins.open', side_effect=unreadable):
                result, code = cli.main(['--vault', str(vault), '--state', str(self.state),
                                         'learn', '--list'], return_result=True)
        self.assertEqual(len(blocked_reads), 1)
        self.assertEqual(code, 1)
        self.assertEqual(result, {'error': 'OSError', 'message': 'learning candidate listing failed'})
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_read_error_is_sanitized_only_for_explicit_reads(self):
        error = PermissionError(13, 'Private candidate canary', str(self.ledger))
        with patch('builtins.open', side_effect=error):
            self.assertEqual(learning.list_candidates(self.state), [])
            self.assertFalse(learning.has_candidates(self.state))
            with self.assertRaisesRegex(OSError, '^learning candidate listing failed$'):
                learning.list_candidates(self.state, strict=True)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_mid_read_failure_never_returns_partial_candidates(self):
        @contextlib.contextmanager
        def interrupted_read(*args, **kwargs):
            def lines():
                yield self.original.decode('utf-8')
                raise OSError('Private candidate canary and private path')
            yield lines()

        with patch('builtins.open', side_effect=interrupted_read):
            self.assertEqual(learning.list_candidates(self.state), [])
            self.assertFalse(learning.has_candidates(self.state))
            with self.assertRaisesRegex(OSError, '^learning candidate listing failed$'):
                learning.list_candidates(self.state, strict=True)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_strict_stat_does_not_depend_on_python_is_file_suppression(self):
        error = PermissionError(13, 'private stat canary', str(self.ledger))
        with patch.object(Path, 'stat', side_effect=error):
            with self.assertRaisesRegex(OSError, '^learning candidate listing failed$'):
                learning.list_candidates(self.state, strict=True)
        # Preserve the default API on both pre-3.14 and 3.14 pathlib behavior.
        with patch.object(Path, 'is_file', side_effect=error):
            with self.assertRaises(PermissionError) as caught:
                learning.list_candidates(self.state)
            self.assertIs(caught.exception, error)
        with patch.object(Path, 'is_file', return_value=False):
            self.assertEqual(learning.list_candidates(self.state), [])
        for error_type in (FileNotFoundError, NotADirectoryError):
            with self.subTest(error_type=error_type):
                with patch.object(Path, 'stat', side_effect=error_type('synthetic')):
                    self.assertEqual(learning.list_candidates(self.state, strict=True), [])

    def test_missing_and_nonregular_ledgers_stay_empty(self):
        self.ledger.unlink()
        self.assertEqual(learning.list_candidates(self.state, strict=True), [])
        self.ledger.mkdir()
        self.assertEqual(learning.list_candidates(self.state, strict=True), [])
        self.assertEqual(learning.list_candidates(self.state), [])

    def test_filters_and_malformed_lines_keep_existing_behavior(self):
        learning.stage_candidate(self.state, 'other', 'Other session', scope='project')
        learning.stage_candidate(self.state, 'cli', 'CLI fallback', scope='project')
        learning.stage_candidate(self.state, 'list-test', 'Other scope', scope='elsewhere')
        with self.ledger.open('a', encoding='utf-8') as stream:
            stream.write('\n{broken JSON\n[1,2]\n"literal"\n')
        before = self.ledger.read_bytes()
        for filters in ({}, {'session': 'list-test', 'scope': 'project'},
                        {'status': 'distilled'}, {'since': 0}, {'since': float('inf')}):
            with self.subTest(filters=filters):
                self.assertEqual(learning.list_candidates(self.state, strict=True, **filters),
                                 learning.list_candidates(self.state, **filters))
        self.assertEqual([r['candidate'] for r in learning.list_candidates(
            self.state, session='list-test', scope='project', strict=True)],
                         ['Private candidate canary', 'CLI fallback'])
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_symlink_following_is_unchanged(self):
        target = self.state / 'synthetic-ledger.jsonl'
        self.ledger.rename(target)
        try:
            self.ledger.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation is unavailable')
        self.assertEqual(learning.list_candidates(self.state, strict=True), [self.record])
        target.unlink()
        self.assertEqual(learning.list_candidates(self.state, strict=True), [])


if __name__ == '__main__':
    unittest.main()
