"""Synthetic durable Hermes counters: no Hermes account, user data, or network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

MODULE = Path(__file__).resolve().parents[1] / 'template/.claude/scripts/beyin_v3_hermes.py'


def load_module():
    spec = importlib.util.spec_from_file_location('test_hermes_turn_state', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def key(session):
    return hashlib.sha256(session.encode('utf-8')).hexdigest()


class HermesTurnStateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='hermes-state-synthetic-')
        self.addCleanup(temporary.cleanup)
        # make_hooks resolves state; macOS tempfile paths can be under /var -> /private/var.
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.module = load_module()
        self.file = self.state / self.module.TURNS_FILE
        stub = patch.object(self.module, 'run_hook', return_value='ctx')
        self.run = stub.start()
        self.addCleanup(stub.stop)

    def seed(self, session='synthetic-session', turns=14, platform='cli', at=None):
        self.file.write_text(json.dumps({'schema': 1, 'sessions': {
            session: {'turns': turns, 'platform': platform, 'at': time.time() if at is None else at}
        }}), encoding='utf-8')

    def turn(self, hooks, session='synthetic-session', **kw):
        return hooks['pre_llm_call'](session_id=session, user_message='synthetic prompt', **kw)

    def stored(self):
        return json.loads(self.file.read_text(encoding='utf-8'))['sessions']

    def test_failed_replace_does_not_repeat_turn_15_and_recovers_pending_delta(self):
        self.seed()
        original = self.file.read_bytes()
        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module.os, 'replace', side_effect=PermissionError('synthetic private path')), \
                self.assertLogs(self.module.LOGGER, level='WARNING') as logs:
            results = [self.turn(hooks) for _ in range(17)]
        self.assertEqual([i + 15 for i, r in enumerate(results) if '[Hafıza]' in r['context']], [15, 30])
        self.assertEqual(self.file.read_bytes(), original)
        self.assertNotIn('synthetic private path', '\n'.join(logs.output))
        # Another process can commit while this process retains unwritten increments.
        self.turn(self.module.make_hooks(self.vault, self.state))
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 33)

    def test_legacy_ids_rewritten_as_hashes_without_resetting_cadence(self):
        session = 'synthetic-private-chat-id-987654'
        self.seed(session=session)
        result = self.turn(self.module.make_hooks(self.vault, self.state), session)
        self.assertIn('15. mesaj', result['context'])
        data = json.loads(self.file.read_text(encoding='utf-8'))
        self.assertEqual(data['schema'], 2)
        self.assertEqual(data['sessions'][key(session)]['turns'], 15)
        for path in self.state.iterdir():
            if path.is_file():
                self.assertNotIn(session.encode(), path.read_bytes())
                self.assertNotIn(b'synthetic prompt', path.read_bytes())

    def test_unreadable_existing_file_is_not_replaced_and_memory_advances(self):
        self.seed()
        hooks = self.module.make_hooks(self.vault, self.state)
        self.turn(hooks)  # learn committed turn 15 before read failure
        original = self.file.read_bytes()
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.file:
                raise PermissionError('synthetic private path')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertLogs(self.module.LOGGER, level='WARNING'):
            results = [self.turn(hooks) for _ in range(15)]
        self.assertEqual([i + 16 for i, r in enumerate(results) if '[Hafıza]' in r['context']], [30])
        self.assertEqual(self.file.read_bytes(), original)
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 31)

    def test_corrupt_file_counts_in_memory_without_destroying_evidence(self):
        self.file.write_bytes(b'{synthetic malformed')
        hooks = self.module.make_hooks(self.vault, self.state)
        with self.assertLogs(self.module.LOGGER, level='WARNING'):
            results = [self.turn(hooks) for _ in range(30)]
        self.assertEqual([i + 1 for i, r in enumerate(results) if '[Hafıza]' in r['context']], [15, 30])
        self.assertEqual(self.file.read_bytes(), b'{synthetic malformed')

    def test_expired_state_is_not_resumed_and_cache_cannot_resurrect_finalized_state(self):
        self.seed(at=time.time() - self.module.TURNS_KEEP_SECONDS - 1)
        one = self.module.make_hooks(self.vault, self.state)
        self.assertNotIn('[Hafıza]', self.turn(one)['context'])
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 1)
        two = self.module.make_hooks(self.vault, self.state)
        two['on_session_finalize'](session_id='synthetic-session')
        self.turn(one)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 1)

    def test_successful_finalize_does_not_override_another_process_reopening_session(self):
        self.seed()
        one = self.module.make_hooks(self.vault, self.state)
        one['on_session_finalize'](session_id='synthetic-session')
        two = self.module.make_hooks(self.vault, self.state)
        self.turn(two, is_first_turn=True, platform='telegram')
        self.turn(one)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 2)
        self.assertEqual(self.stored()[key('synthetic-session')]['platform'], 'telegram')

    def test_failed_finalize_preserves_platform_and_does_not_resume_stale_count(self):
        self.seed(platform='telegram')
        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module.os, 'replace', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            hooks['on_session_finalize'](session_id='synthetic-session', platform='gateway')
        self.run.assert_not_called()
        self.turn(hooks, is_first_turn=True, platform='cli')
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission fixture; portable refusal tests also run')
    def test_real_readable_but_unwritable_directory_keeps_cadence(self):
        self.seed()
        original = self.file.read_bytes()
        self.state.chmod(0o500)
        try:
            try:
                (self.state / 'probe').write_text('synthetic')
            except PermissionError:
                pass
            else:
                self.skipTest('executor bypasses POSIX permissions')
            hooks = self.module.make_hooks(self.vault, self.state)
            with self.assertLogs(self.module.LOGGER, level='WARNING'):
                first = self.turn(hooks)
                second = self.turn(hooks)
            self.assertIn('15. mesaj', first['context'])
            self.assertNotIn('[Hafıza]', second['context'])
            self.assertEqual(self.file.read_bytes(), original)
        finally:
            self.state.chmod(0o700)
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 17)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission fixture; portable refusal tests also run')
    def test_real_unreadable_file_remains_untouched(self):
        self.seed()
        hooks = self.module.make_hooks(self.vault, self.state)
        self.turn(hooks)
        original = self.file.read_bytes()
        self.file.chmod(0)
        try:
            try:
                self.file.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest('executor bypasses POSIX permissions')
            with self.assertLogs(self.module.LOGGER, level='WARNING'):
                results = [self.turn(hooks) for _ in range(15)]
            self.assertEqual([i + 16 for i, r in enumerate(results) if '[Hafıza]' in r['context']], [30])
        finally:
            self.file.chmod(0o600)
        self.assertEqual(self.file.read_bytes(), original)
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 31)

    def test_failed_finalize_discards_unwritten_delta_even_when_disk_stays_unreadable(self):
        self.seed(platform='telegram')
        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module.os, 'replace', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            self.turn(hooks)
            hooks['on_session_finalize'](session_id='synthetic-session', platform='gateway')
        self.assertEqual([call.args[2]['hook_event_name'] for call in self.run.call_args_list], ['UserPromptSubmit'])
        with patch.object(self.module, 'load_turns', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            self.turn(hooks, is_first_turn=True, platform='cli')
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 2)

    def test_repeated_failed_finalize_keeps_known_unattended_platform(self):
        self.seed(platform='telegram')
        hooks = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module.os, 'replace', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            for _ in range(2):
                hooks['on_session_finalize'](session_id='synthetic-session', platform='gateway')
        hooks['on_session_finalize'](session_id='synthetic-session', platform='gateway')
        self.run.assert_not_called()
        self.assertNotIn(key('synthetic-session'), self.stored())

    def test_failed_finalize_does_not_override_newer_disk_platform_on_recovery(self):
        self.seed(platform='cli')
        one = self.module.make_hooks(self.vault, self.state)
        with patch.object(self.module.os, 'replace', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            one['on_session_finalize'](session_id='synthetic-session')
            self.turn(one, is_first_turn=True, platform='cli')
        two = self.module.make_hooks(self.vault, self.state)
        self.turn(two, is_first_turn=True, platform='telegram')
        self.turn(one)
        self.assertEqual(self.stored()[key('synthetic-session')]['platform'], 'telegram')
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 17)

    def test_retention_bounds_include_dirty_memory_and_reject_nonfinite_times(self):
        records = {'synthetic-' + str(i): {'turns': 14, 'platform': 'telegram', 'at': time.time() - i}
                   for i in range(self.module.TURNS_KEEP_MAX + 10)}
        records['nan'] = {'turns': 14, 'platform': 'cli', 'at': float('nan')}
        records['inf'] = {'turns': 14, 'platform': 'cli', 'at': float('inf')}
        self.file.write_text(json.dumps({'schema': 1, 'sessions': records}), encoding='utf-8')
        hooks = self.module.make_hooks(self.vault, self.state)
        self.turn(hooks)
        self.assertEqual(len(self.stored()), self.module.TURNS_KEEP_MAX)
        self.assertNotIn(key('synthetic-209'), self.stored())
        self.assertNotIn(key('nan'), self.stored())
        self.assertNotIn(key('inf'), self.stored())
        # Windows can return the same wall time for many turns in a burst.
        with patch.object(self.module.time, 'time', return_value=time.time()), \
                patch.object(self.module, 'load_turns', side_effect=PermissionError()), \
                self.assertLogs(self.module.LOGGER, level='WARNING'):
            for i in range(self.module.TURNS_KEEP_MAX + 1):
                self.turn(hooks, 'synthetic-dirty-' + str(i), is_first_turn=True, platform='cli')
            # The oldest local delta was evicted rather than growing forever.
            self.turn(hooks, 'synthetic-dirty-0')
        self.turn(hooks, 'synthetic-dirty-0')
        self.assertEqual(self.stored()[key('synthetic-dirty-0')]['turns'], 2)

    def test_same_tick_persistence_keeps_most_recent_touches_across_restart(self):
        with patch.object(self.module.time, 'time', return_value=time.time()), \
                patch.object(self.module, 'TURNS_KEEP_MAX', 2):
            hooks = self.module.make_hooks(self.vault, self.state)
            for session in ('synthetic-first', 'synthetic-second', 'synthetic-first'):
                self.turn(hooks, session)
            # The stored order must break ties even after the process-local cache is gone.
            self.turn(self.module.make_hooks(self.vault, self.state), 'synthetic-third')
        records = self.stored()
        self.assertEqual(set(records), {key('synthetic-first'), key('synthetic-third')})
        self.assertEqual(records[key('synthetic-first')]['turns'], 2)
        self.assertEqual(records[key('synthetic-third')]['turns'], 1)

    def test_same_tick_cwd_cache_keeps_most_recent_touches(self):
        with patch.object(self.module.time, 'time', return_value=time.time()), \
                patch.object(self.module, 'TURNS_KEEP_MAX', 2):
            hooks = self.module.make_hooks(self.vault, self.state)
            for session in ('synthetic-first', 'synthetic-second', 'synthetic-first', 'synthetic-third'):
                self.turn(hooks, session, platform='desktop', cwd=str(self.vault / session))
            hooks['on_session_finalize'](session_id='synthetic-second')
            self.assertNotIn('cwd', self.run.call_args.args[2])
            for session in ('synthetic-first', 'synthetic-third'):
                hooks['on_session_finalize'](session_id=session)
                self.assertEqual(self.run.call_args.args[2]['cwd'], str(self.vault / session))

    def test_first_turn_without_platform_does_not_forget_known_unattended_platform(self):
        self.seed(platform='telegram')
        hooks = self.module.make_hooks(self.vault, self.state)
        self.assertNotIn('[Hafıza]', self.turn(hooks, is_first_turn=True)['context'])
        hooks['on_session_finalize'](session_id='synthetic-session', platform='gateway')
        self.assertEqual([call.args[2]['hook_event_name'] for call in self.run.call_args_list], ['SessionStart'])

    def test_malformed_numeric_time_and_surrogate_id_fail_open(self):
        self.seed(session='synthetic-invalid-time', at=10 ** 400)
        hooks = self.module.make_hooks(self.vault, self.state)
        self.turn(hooks, 'synthetic-\ud800')
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(next(iter(self.stored().values()))['turns'], 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX unlock injection; close releases the real lock')
    def test_unlock_error_after_commit_does_not_replay_committed_increment(self):
        import fcntl
        self.seed()
        hooks = self.module.make_hooks(self.vault, self.state)
        flock = fcntl.flock
        def failing_unlock(fd, operation):
            if operation == fcntl.LOCK_UN:
                raise OSError('synthetic unlock error')
            return flock(fd, operation)
        with patch.object(fcntl, 'flock', failing_unlock), self.assertLogs(self.module.LOGGER, level='WARNING'):
            self.turn(hooks)
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 16)

    def test_lock_timeout_is_bounded_and_deferred_turns_reconcile(self):
        self.seed()
        original = self.file.read_bytes()
        code = """import importlib.util,sys
spec=importlib.util.spec_from_file_location('hermes_lock',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with m._turns_lock(sys.argv[2]):
    print('ready',flush=True);sys.stdin.readline()
"""
        worker = subprocess.Popen([sys.executable, '-c', code, str(MODULE), str(self.state)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding='utf-8', env=inherited_env())
        try:
            self.assertEqual(worker.stdout.readline().strip(), 'ready')
            hooks = self.module.make_hooks(self.vault, self.state)
            start = time.monotonic()
            with patch.object(self.module, 'TURNS_LOCK_TIMEOUT', .05), self.assertLogs(self.module.LOGGER, level='WARNING'):
                first = self.turn(hooks)
                second = self.turn(hooks)
            self.assertLess(time.monotonic() - start, 2)
            self.assertIn('15. mesaj', first['context'])
            self.assertNotIn('[Hafıza]', second['context'])
            self.assertEqual(self.file.read_bytes(), original)
            stdout, stderr = worker.communicate('release\n', timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
        finally:
            if worker.poll() is None:
                worker.kill(); worker.communicate()
        self.turn(hooks)
        self.assertEqual(self.stored()[key('synthetic-session')]['turns'], 17)

    def test_competing_processes_keep_all_sessions_and_all_shared_turns(self):
        code = '''import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location("hermes_worker",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.run_hook=lambda *a,**kw:""
# Separate bounded-timeout test owns refusal; give slow CI enough time to serialize.
m.TURNS_LOCK_TIMEOUT=20
original=m.load_turns
def delayed(*a,**kw):
    value=original(*a,**kw);time.sleep(.015);return value
m.load_turns=delayed
hooks=m.make_hooks(Path(sys.argv[2]),Path(sys.argv[3]))
print("ready",flush=True);sys.stdin.readline()
for n in range(5):
    for session in ("synthetic-shared", "synthetic-worker-"+sys.argv[4]):
        hooks["pre_llm_call"](session_id=session,platform="telegram",is_first_turn=n==0)
'''
        workers = [subprocess.Popen([sys.executable, '-c', code, str(MODULE), str(self.vault), str(self.state), str(i)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', env=inherited_env()) for i in range(4)]
        try:
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), 'ready')
            for worker in workers:
                worker.stdin.write('go\n'); worker.stdin.flush()
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=30)
                self.assertEqual(worker.returncode, 0, stderr)
                self.assertEqual(stderr, '', 'contention should serialize, not silently fall back')
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill(); worker.communicate()
        records = self.stored()
        self.assertEqual(len(records), 5)
        self.assertEqual(records[key('synthetic-shared')]['turns'], 20)
        for i in range(4):
            self.assertEqual(records[key('synthetic-worker-' + str(i))]['turns'], 5)
        hooks = self.module.make_hooks(self.vault, self.state)
        hooks['on_session_finalize'](session_id='synthetic-shared', platform='gateway')
        self.run.assert_not_called()
        self.assertNotIn(key('synthetic-shared'), self.stored())


if __name__ == '__main__':
    unittest.main()
