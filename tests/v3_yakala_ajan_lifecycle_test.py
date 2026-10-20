"""Process generation, orphan revocation and independent POSIX deadline regressions.

No agent CLI, model or network request runs. POSIX integration tests create only
our own short-lived Python children and a sleeping grandchild in temporary paths.
"""
import ctypes as C
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import v3_yakala_ajan_test as fixtures

yakala = fixtures.yakala


class AgentProcessIdentityTest(unittest.TestCase):
    def test_current_native_process_identity_is_stable_and_requires_birth(self):
        identity = yakala._agent_process_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertEqual(identity['pid'], os.getpid())
        self.assertTrue(yakala._agent_same_process(identity))
        self.assertFalse(yakala._agent_same_process(dict(identity, birth=identity['birth'] + '-old')))
        self.assertFalse(yakala._agent_same_process({'pid': os.getpid()}))

    def test_invalid_pids_never_reach_an_os_api(self):
        for pid in (None, True, False, 0, -1, '123', 2 ** 31):
            with self.subTest(pid=pid), patch.object(yakala.os, 'kill') as kill:
                self.assertIsNone(yakala._agent_process_identity(pid))
                kill.assert_not_called()

    def test_linux_stat_with_parentheses_and_newlines_in_comm(self):
        raw = '123 (odd ) process\n(name)) ' + ' '.join(['R'] + ['0'] * 18 + ['998877'] + ['0'] * 3)
        boot = '00000000-0000-0000-0000-000000000001'

        def read(path, **_):
            return boot if Path(path).name == 'boot_id' else raw  # Windows paths use backslashes

        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'linux'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), patch.object(Path, 'read_text', read):
            identity = yakala._agent_process_identity(123)
        self.assertEqual(identity, {'pid': 123, 'kind': 'linux-starttime', 'birth': '998877', 'boot': boot})

    def test_linux_zombie_is_not_a_live_owner(self):
        raw = '123 (dead) Z ' + ' '.join(['0'] * 22)
        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'linux'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(Path, 'read_text', return_value=raw):
            self.assertIsNone(yakala._agent_process_identity(123))

    def test_linux_mismatched_pid_namespace_is_refused(self):
        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'linux'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(Path, 'read_text', return_value='321 (foreign) R ' + '0 ' * 25), \
                self.assertRaises(OSError):
            yakala._agent_process_identity(123)

    def test_windows_birth_and_liveness_are_read_from_one_owned_handle(self):
        kernel = types.SimpleNamespace(OpenProcess=Mock(return_value=321),
                                      WaitForSingleObject=Mock(return_value=258),
                                      GetProcessTimes=Mock(), CloseHandle=Mock())

        def times(handle, created, *_):
            self.assertEqual(handle, 321)
            created._obj.value = 987654321
            return 1

        kernel.GetProcessTimes.side_effect = times
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'win32'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(C, 'WinDLL', return_value=kernel, create=True):
            self.assertEqual(yakala._agent_process_identity(123),
                             {'pid': 123, 'kind': 'windows-filetime', 'birth': '987654321'})
        kernel.OpenProcess.assert_called_once_with(0x101000, False, 123)
        kernel.CloseHandle.assert_called_once_with(321)

    def test_windows_exited_process_even_exit_code_259_is_not_live(self):
        kernel = types.SimpleNamespace(OpenProcess=Mock(return_value=321),
                                      WaitForSingleObject=Mock(return_value=0),
                                      GetProcessTimes=Mock(), CloseHandle=Mock())
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'win32'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(C, 'WinDLL', return_value=kernel, create=True):
            self.assertIsNone(yakala._agent_process_identity(123))
        kernel.GetProcessTimes.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(321)

    def test_windows_access_denied_is_ambiguous_not_an_absent_process(self):
        kernel = types.SimpleNamespace(OpenProcess=Mock(return_value=None), WaitForSingleObject=Mock(),
                                      GetProcessTimes=Mock(), CloseHandle=Mock())
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'win32'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(C, 'WinDLL', return_value=kernel, create=True), \
                patch.object(C, 'get_last_error', return_value=5, create=True), self.assertRaises(OSError):
            yakala._agent_process_identity(123)
        kernel.CloseHandle.assert_not_called()

    def test_darwin_proc_bsdinfo_layout_and_birth(self):
        library = types.SimpleNamespace(proc_pidinfo=Mock())

        def info(pid, flavor, arg, address, length):
            self.assertEqual((pid, flavor, arg, length), (123, 3, 0, 136))
            address._obj.pid, address._obj.status = pid, 2
            address._obj.start_sec, address._obj.start_usec = 1700000000, 998877
            return length

        library.proc_pidinfo.side_effect = info
        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'darwin'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(C, 'CDLL', return_value=library):
            self.assertEqual(yakala._agent_process_identity(123),
                             {'pid': 123, 'kind': 'darwin-starttime', 'birth': '1700000000:998877'})

    def test_darwin_short_struct_read_fails_closed(self):
        library = types.SimpleNamespace(proc_pidinfo=Mock(return_value=128))
        system = types.SimpleNamespace(**{**vars(os), 'name': 'posix'})
        platform = types.SimpleNamespace(**{**vars(sys), 'platform': 'darwin'})
        with patch.object(yakala, 'os', system), patch.object(yakala, 'sys', platform), \
                patch.object(C, 'CDLL', return_value=library), self.assertRaises(OSError):
            yakala._agent_process_identity(123)


class AgentOrphanRecoveryTest(fixtures.AgentFiles):
    def test_cli_lifetime_budget_starts_after_preflight(self):
        self.configure(timeout_seconds=30)
        _, job, _ = self.queued()
        clock = [0]
        owned = types.SimpleNamespace(process=types.SimpleNamespace(
            pid=os.getpid(), poll=Mock(return_value=0), wait=Mock(return_value=0)), close=Mock())

        def preflight(*_):
            clock[0] = 125000000000  # several slow, independently bounded discovery probes
            return []

        with patch.object(yakala, '_agent_executable', return_value=['fake-cli']), \
                patch.object(yakala, '_agent_preflight', side_effect=preflight), \
                patch.object(yakala, '_agent_owned_process', return_value=owned), \
                patch('time.monotonic_ns', side_effect=lambda: clock[0]):
            yakala._agent_run_job(self.vault, self.state, job)
        saved = yakala._agent_load_job(self.vault, self.state, job['id'])
        self.assertEqual(saved['runtime_deadline_ns'], 155000000000)

    def recover(self, identity, *, identity_lookup=None):
        self.configure()
        _, old, card = self.queued(text='Interrupted job')
        old.update(state='running', agent_pid=os.getpid(), worker_pid=os.getpid(),
                   worker_identity=yakala._agent_process_identity(os.getpid()),
                   runtime_deadline_ns=time.monotonic_ns() + 60000000000)
        if identity is not None:
            old['agent_identity'] = identity
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, old['id']) / 'job.json', old)
        _, next_job, _ = self.queued(text='Next independent job')

        def run(vault, state, job):
            recovered = yakala._agent_load_job(vault, state, old['id'])
            self.assertEqual(recovered['state'], 'approval_required')
            with self.assertRaisesRegex(ValueError, 'not running'):
                yakala._agent_require_live_job(recovered)
            yakala._agent_end(vault, state, job, 'approval_required', 'Test complete.')

        with patch.object(yakala, '_agent_run_job', side_effect=run) as run_job, \
                patch.object(yakala, '_agent_process_identity', wraps=yakala._agent_process_identity,
                             side_effect=identity_lookup), \
                patch.object(yakala.os, 'kill') as kill, \
                patch.object(yakala.os, 'killpg', create=True) as killpg:
            self.assertEqual(yakala._agent_worker(self.vault, self.state), 0)
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual([call.args[2]['id'] for call in run_job.call_args_list], [next_job['id']])
        recovered = yakala._agent_load_job(self.vault, self.state, old['id'])
        self.assertEqual(recovered['state'], 'approval_required')
        # The terminal state must still revoke tools after process metadata becomes
        # readable again, even while that stored worker generation is live.
        self.assertTrue(yakala._agent_same_process(recovered['worker_identity']))
        with self.assertRaisesRegex(ValueError, 'not running'):
            yakala._agent_require_live_job(recovered)
        self.assertEqual(yakala.release_agent_card(self.vault, self.state, card['id'])['status'], 'ajan_karti_serbest')
        return recovered

    def test_reused_agent_pid_never_blocks_queue_or_receives_signal(self):
        identity = yakala._agent_process_identity(os.getpid())
        self.recover(dict(identity, birth=identity['birth'] + '-old'))

    def test_legacy_pid_only_job_is_quarantined_without_rerun_or_signal(self):
        self.recover(None)

    def test_unqueryable_reused_agent_pid_is_quarantined_without_blocking_queue(self):
        identity = {'pid': os.getpid(), 'kind': 'windows-filetime', 'birth': 'old-process-generation'}
        recovered = self.recover(identity, identity_lookup=PermissionError(5, 'Access denied'))
        self.assertIn('surec kimligi dogrulanamadi', recovered['result_summary'])
        self.assertIn('kismi etkiler bilinmiyor', recovered['result_summary'])

    def test_guardian_identity_becoming_unreadable_during_wait_is_quarantined(self):
        identity = yakala._agent_process_identity(os.getpid())
        with patch('time.sleep') as sleep:
            recovered = self.recover(identity, identity_lookup=[identity, OSError('Process metadata unavailable')])
        sleep.assert_called_once_with(0.1)
        self.assertIn('surec kimligi dogrulanamadi', recovered['result_summary'])


class AgentBridgeLeaseTest(unittest.TestCase):
    setUp = fixtures.AgentBridgeFilesTest.setUp
    call = fixtures.AgentBridgeFilesTest.call

    def test_worker_pid_reuse_revokes_every_tool(self):
        identity = self.job['worker_identity']
        self.job['worker_identity'] = dict(identity, birth=identity['birth'] + '-old')
        for name, arguments in (
                ('read_note', {'path': 'AGENTS.md'}), ('search_notes', {'query': 'x'}),
                ('fetch_source', {'url': self.job['url']}), ('download', {'url': self.job['url'], 'filename': 'a.mp3'}),
                ('write_note', {'title': 'x', 'content': 'x'}), ('complete', {'status': 'completed', 'summary': 'x'})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'araclar kapatildi'):
                self.call(name, **arguments)

    def test_expired_lease_closes_bridge_even_if_worker_still_lives(self):
        self.job['runtime_deadline_ns'] = time.monotonic_ns() - 1
        with self.assertRaisesRegex(ValueError, 'suresi gecersiz'):
            self.call('search_notes', query='x')

    def test_missing_legacy_owner_closes_before_any_capability_is_published(self):
        self.job.pop('worker_identity')
        output = io.StringIO()
        with patch.object(yakala.sys, 'stdout', output), self.assertRaises(ValueError):
            yakala._agent_mcp_main(self.vault, self.state, self.job['id'])
        self.assertEqual(output.getvalue(), '')

    def test_process_metadata_errors_close_bridge(self):
        for error in (PermissionError(5, 'Access denied'), OSError('Process metadata unavailable')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(yakala, '_agent_process_identity', side_effect=error), \
                    self.assertRaisesRegex(ValueError, 'artik dogrulanamiyor'):
                self.call('search_notes', query='x')

    def test_existing_mcp_connection_stops_listing_tools_when_lease_expires(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        incoming = io.StringIO(''.join(json.dumps(row) + '\n' for row in requests))
        outgoing = io.StringIO()
        expired = dict(self.job, runtime_deadline_ns=time.monotonic_ns() - 1)
        with patch.object(yakala.sys, 'stdin', incoming), patch.object(yakala.sys, 'stdout', outgoing), \
                patch.object(yakala, '_agent_load_job', side_effect=[self.job, self.job, expired]):
            self.assertEqual(yakala._agent_mcp_main(self.vault, self.state, self.job['id']), 1)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertIn('capabilities', responses[0]['result'])
        self.assertEqual(responses[1]['error']['code'], -32000)
        self.assertNotIn('tools', responses[1].get('result', {}))


@unittest.skipIf(os.name == 'nt', 'POSIX guardian integration; Windows uses its native Job Object.')
class AgentPosixGuardianTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='yakala-guardian-')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('Expected process boundary did not complete before timeout.')

    def child_command(self):
        ids = self.folder / 'child-pids.json'
        child = ('import os,sys,json,time,subprocess; from pathlib import Path; '
                 'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                 'Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)')
        return [sys.executable, '-c', child, str(ids)], ids

    def assert_children_stopped(self, ids):
        pids = json.loads(ids.read_text())
        self.wait_for(lambda: all(yakala._agent_process_identity(pid) is None for pid in pids))

    def test_independent_deadline_stops_cli_and_grandchild(self):
        command, ids = self.child_command()
        owned = yakala._AgentPosixProcess(command, deadline_ns=time.monotonic_ns() + 1200000000,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True)
        self.addCleanup(owned.close)
        self.wait_for(ids.is_file)
        owned.process.wait(timeout=5)
        with self.assertRaises(TimeoutError):
            owned.result_code()
        self.assert_children_stopped(ids)

    def test_expired_guardian_never_starts_the_cli(self):
        marker = self.folder / 'must-not-start'
        command = [sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)]
        owned = yakala._AgentPosixProcess(command, deadline_ns=time.monotonic_ns() - 1,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True)
        self.addCleanup(owned.close)
        owned.process.wait(timeout=5)
        with self.assertRaises(TimeoutError):
            owned.result_code()
        self.assertFalse(marker.exists())

    def test_successful_cli_exit_code_is_preserved_and_group_is_reaped(self):
        command, ids = self.child_command()
        command[2] = command[2].removesuffix('time.sleep(60)') + 'raise SystemExit(7)'
        owned = yakala._AgentPosixProcess(command,
                                          deadline_ns=time.monotonic_ns() + 5000000000,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True)
        self.addCleanup(owned.close)
        owned.process.wait(timeout=5)
        self.assertEqual(owned.result_code(), 7)
        self.assert_children_stopped(ids)

    def test_worker_sigkill_closes_guardian_cli_and_grandchild_without_pid_signal(self):
        command, ids = self.child_command()
        guardian = self.folder / 'guardian.json'
        worker_code = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import beyin_v3_yakala as y
owned=y._AgentPosixProcess(json.loads(sys.argv[2]),deadline_ns=time.monotonic_ns()+60000000000,
                         stdin=y.subprocess.DEVNULL,stdout=y.subprocess.DEVNULL,
                         stderr=y.subprocess.DEVNULL,close_fds=True)
Path(sys.argv[3]).write_text(json.dumps(y._agent_process_identity(owned.process.pid)))
time.sleep(60)
'''
        worker = subprocess.Popen([sys.executable, '-c', worker_code, str(fixtures.SCRIPTS),
                                   json.dumps(command), str(guardian)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True)

        def close_worker():
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
            worker.stderr.close()

        self.addCleanup(close_worker)
        self.wait_for(lambda: ids.is_file() and guardian.is_file())
        identity = json.loads(guardian.read_text())
        self.assertTrue(yakala._agent_same_process(identity))
        worker.kill()  # Only the Popen we just created, never a durable PID.
        worker.wait(timeout=5)
        self.wait_for(lambda: not yakala._agent_same_process(identity))
        self.assert_children_stopped(ids)


if __name__ == '__main__':
    unittest.main()
