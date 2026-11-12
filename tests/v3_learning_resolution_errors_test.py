"""Explicit learning resolution reports IO errors without changing durable writes (#587)."""
import importlib.util
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v3_learning_resolution_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_learning as learning
from beyin_v3_sync import SyncEngine


class LearningResolutionErrorsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: evaluator.close_store(self.engine.store))
        (self.vault / 'notes').mkdir(exist_ok=True)
        (self.vault / 'notes/synthetic.md').write_text('# Synthetic\n', encoding='utf-8')
        learning.stage_candidate(self.state, 'resolution-test', 'Private synthetic candidate',
                                 source='notes/synthetic.md')
        self.ledger = self.state / learning.LEARNING_STATE_FILE
        self.original = self.ledger.read_bytes()

    def block_resolution(self):
        # Real filesystem failure without platform-specific permissions or mocked IO.
        (self.state / (learning.LEARNING_STATE_FILE + '.tmp')).mkdir()

    def cli(self, *args):
        result = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_installed_cli_reports_failure_and_preserves_no_match_and_success(self):
        self.block_resolution()
        for prefix in ([], ['--json']):
            with self.subTest(prefix=prefix):
                result = run_python(self.vault / 'beyin.py',
                                    prefix + ['learn', '--resolve', 'distilled', '--session', 'resolution-test'],
                                    self.vault, self.env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(json.loads(result.stderr),
                                 {'error': 'OSError', 'message': 'learning candidate resolution failed'})
                self.assertEqual(self.ledger.read_bytes(), self.original)
        (self.state / (learning.LEARNING_STATE_FILE + '.tmp')).rmdir()
        no_match = self.cli('learn', '--resolve', 'distilled', '--session', 'other-session')
        self.assertEqual(no_match['resolved_count'], 0)
        success = self.cli('learn', '--resolve', 'distilled', '--session', 'resolution-test')
        self.assertEqual(success['resolved_count'], 1)
        again = self.cli('learn', '--resolve', 'distilled', '--session', 'resolution-test')
        self.assertEqual(again['resolved_count'], 0)

    def test_receipt_keeps_best_effort_and_replay_contract(self):
        self.block_resolution()
        args = ('synthetic-event', 'Saved synthetic note', ['notes/synthetic.md'], 'claude')
        result = self.engine.receipt(*args, session='resolution-test')
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('candidates_resolved', result)
        self.assertEqual(self.ledger.read_bytes(), self.original)
        source = self.vault / result['source']
        before = source.read_bytes()
        with self.engine.store._connect() as db:
            self.assertIsNotNone(db.execute('SELECT payload FROM receipts WHERE id=?', (args[0],)).fetchone())
        (self.state / (learning.LEARNING_STATE_FILE + '.tmp')).rmdir()
        replay = self.engine.receipt(*args, session='resolution-test')
        self.assertEqual(replay['status'], 'succeeded')
        self.assertEqual(replay['source'], result['source'])
        self.assertNotIn('candidates_resolved', replay)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_integer_api_stays_best_effort_and_missing_ledger_is_not_error(self):
        self.block_resolution()
        self.assertEqual(learning.resolve_candidates(self.state, 'resolution-test'), 0)
        with self.assertRaisesRegex(OSError, '^learning candidate resolution failed$'):
            learning.resolve_candidates(self.state, 'resolution-test', strict=True)
        self.ledger.unlink()
        self.assertEqual(learning.resolve_candidates(self.state, 'resolution-test', strict=True), 0)

    def test_strict_probe_sanitizes_errors_and_default_probe_is_unchanged(self):
        error = PermissionError(13, 'private synthetic error', str(self.ledger))
        with patch.object(Path, 'stat', side_effect=error):
            with self.assertRaisesRegex(OSError, '^learning candidate resolution failed$'):
                learning.resolve_candidates(self.state, strict=True)
        # Keep both pre-3.14 raising and 3.14 suppression behaviors delegated to is_file.
        with patch.object(Path, 'is_file', side_effect=error):
            with self.assertRaises(PermissionError) as caught:
                learning.resolve_candidates(self.state)
            self.assertIs(caught.exception, error)
        with patch.object(Path, 'is_file', return_value=False):
            self.assertEqual(learning.resolve_candidates(self.state), 0)
        for error_type in (FileNotFoundError, NotADirectoryError):
            with self.subTest(error_type=error_type):
                with patch.object(Path, 'stat', side_effect=error_type('synthetic')):
                    self.assertEqual(learning.resolve_candidates(self.state, strict=True), 0)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    @unittest.skipIf(os.name == 'nt', 'POSIX directory permissions required')
    def test_strict_probe_sanitizes_real_unsearchable_state(self):
        mode = stat.S_IMODE(self.state.stat().st_mode)
        try:
            self.state.chmod(0)
            try:
                self.ledger.stat()
            except PermissionError:
                pass
            else:
                self.skipTest('effective user bypasses directory permissions')
            with self.assertRaisesRegex(OSError, '^learning candidate resolution failed$'):
                learning.resolve_candidates(self.state, strict=True)
            # The default API follows this interpreter's original is_file behavior.
            try:
                expected = self.ledger.is_file()
            except OSError as original:
                with self.assertRaises(type(original)) as caught:
                    learning.resolve_candidates(self.state)
                self.assertEqual(caught.exception.args, original.args)
            else:
                self.assertFalse(expected)
                self.assertEqual(learning.resolve_candidates(self.state), 0)
        finally:
            self.state.chmod(mode)
        self.assertEqual(self.ledger.read_bytes(), self.original)

    def test_strict_probe_keeps_nonregular_ledger_behavior(self):
        self.ledger.unlink()
        self.ledger.mkdir()
        self.assertEqual(learning.resolve_candidates(self.state, strict=True), 0)
        self.assertEqual(learning.resolve_candidates(self.state), 0)

    def test_strict_probe_keeps_symlink_following_behavior(self):
        target = self.state / 'synthetic-ledger.jsonl'
        self.ledger.rename(target)
        try:
            self.ledger.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation is unavailable')
        self.assertEqual(learning.resolve_candidates(self.state, 'resolution-test', strict=True), 1)
        # Existing replace behavior replaces the link, leaving its original target intact.
        self.assertFalse(self.ledger.is_symlink())
        self.assertEqual(target.read_bytes(), self.original)
        self.ledger.unlink()
        target.unlink()
        self.ledger.symlink_to(target)
        self.assertEqual(learning.resolve_candidates(self.state, strict=True), 0)

    def test_strict_resolution_preserves_malformed_lines(self):
        malformed = b'  {broken JSON \r\n\r\n  [1,2]  \n'
        self.ledger.write_bytes(malformed + self.original)
        self.assertEqual(learning.resolve_candidates(self.state, 'resolution-test', strict=True), 1)
        self.assertTrue(self.ledger.read_bytes().startswith(malformed))

    def test_strict_resolution_sanitizes_lock_write_and_replace_failures(self):
        for target in ('_ledger_lock', 'os.fsync', 'os.replace'):
            with self.subTest(target=target):
                with patch('beyin_v3_learning.' + target,
                           side_effect=OSError('private path and synthetic contents')):
                    with self.assertRaisesRegex(OSError, '^learning candidate resolution failed$'):
                        learning.resolve_candidates(self.state, 'resolution-test', strict=True)
                self.assertEqual(self.ledger.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
