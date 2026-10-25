"""Review regressions: bounded outbound calls, source labels and modified Escape.

No agent CLI, external server or native desktop is used. The concurrency check starts
two real Python MCP dispatchers sharing one job directory and its OS-owned tool lock.
"""
import contextlib
import ctypes as C
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
import uuid
from unittest.mock import Mock, patch

import v3_yakala_ajan_test as fixtures

y = fixtures.yakala


class NetworkReviewTest(unittest.TestCase):
    setUp = fixtures.AgentBridgeFilesTest.setUp
    call = fixtures.AgentBridgeFilesTest.call
    note = fixtures.AgentBridgeFilesTest.note
    transfer = fixtures.AgentBridgeFilesTest.transfer
    authorize_links = fixtures.AgentBridgeFilesTest.authorize_links
    protocol = fixtures.AgentBridgeFilesTest.protocol
    handshake = fixtures.AgentBridgeFilesTest.handshake

    def fetched(self, url=None):
        return {'url': url or self.job['url'], 'links': [], 'text': 'public text', 'source_trust': 'untrusted'}

    def budget(self):
        return json.loads((self.job_folder / 'network-budget.json').read_text(encoding='utf-8'))

    def test_fetch_and_download_share_one_persistent_budget(self):
        with patch.object(y, '_agent_fetch_source', return_value=self.fetched()) as fetch, \
                patch.object(y, '_agent_download', return_value={'staged': True}) as download:
            for index in range(y._AGENT_NETWORK_CALLS):
                if index % 2:
                    self.call('download', url=self.job['url'], filename='file.txt')
                else:
                    self.call('fetch_source', url=self.job['url'])
            for tool, extra in (('fetch_source', {}), ('download', {'filename': 'file.txt'})):
                with self.subTest(tool=tool), self.assertRaisesRegex(ValueError, 'cagri hakki doldu'):
                    self.call(tool, url=self.job['url'], **extra)
        self.assertEqual(fetch.call_count + download.call_count, y._AGENT_NETWORK_CALLS)
        self.assertEqual(self.budget()['used'], y._AGENT_NETWORK_CALLS)

    def test_failed_requests_spend_budget_and_reads_and_completion_still_work(self):
        self.note('knowledge/ordinary.md', 'still readable')
        with patch.object(y, '_agent_fetch_source', side_effect=TimeoutError('offline')) as fetch:
            for _ in range(y._AGENT_NETWORK_CALLS):
                with self.assertRaises(TimeoutError):
                    self.call('fetch_source', url=self.job['url'])
            with self.assertRaisesRegex(ValueError, 'cagri hakki doldu'):
                self.call('fetch_source', url=self.job['url'])
        self.assertEqual(fetch.call_count, y._AGENT_NETWORK_CALLS)
        self.assertEqual(self.call('read_note', path='knowledge/ordinary.md')['text'], 'still readable')
        self.assertEqual(self.call('complete', status='approval_required', summary='Ag limiti doldu.')['status'], 'submitted')

    def test_open_network_uses_same_call_budget(self):
        self.job['config'] = {'ag': 'acik'}
        url = 'https://public.example.org/research'
        with patch.object(y, '_agent_fetch_source', return_value=self.fetched(url)) as fetch:
            for _ in range(y._AGENT_NETWORK_CALLS):
                self.call('fetch_source', url=url)
            with self.assertRaisesRegex(ValueError, 'cagri hakki doldu'):
                self.call('fetch_source', url=url)
        self.assertEqual(fetch.call_count, y._AGENT_NETWORK_CALLS)

    def test_unauthorized_url_never_spends_or_touches_network(self):
        with patch.object(y, '_agent_fetch_source') as fetch:
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                self.call('fetch_source', url='https://attacker.example/?q=private')
        fetch.assert_not_called()
        self.assertFalse((self.job_folder / 'network-budget.json').exists())

    def test_invalid_counter_fails_before_network(self):
        path = self.job_folder / 'network-budget.json'
        valid = {'schema': 1, 'job_id': self.job['id'], 'used': 0}
        invalid = [None, [], {}, {**valid, 'job_id': 'another-job'}, {**valid, 'used': True},
                   {**valid, 'used': -1}, {**valid, 'used': 1.5}, {**valid, 'used': 9}]
        with patch.object(y, '_agent_fetch_source') as fetch:
            for value in invalid:
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.call('fetch_source', url=self.job['url'])
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.call('fetch_source', url=self.job['url'])
        fetch.assert_not_called()

    def test_failed_counter_commit_never_starts_network(self):
        with patch.object(y, '_agent_json', side_effect=OSError('disk full')), \
                patch.object(y, '_agent_fetch_source') as fetch:
            with self.assertRaises(OSError):
                self.call('fetch_source', url=self.job['url'])
        fetch.assert_not_called()

    def test_new_job_has_independent_budget(self):
        with patch.object(y, '_agent_fetch_source', return_value=self.fetched()):
            self.call('fetch_source', url=self.job['url'])
        self.assertEqual(self.budget()['used'], 1)
        next_folder = self.job_folder.parent / 'second-job'
        next_folder.mkdir()
        self.job['id'] = str(uuid.uuid4())
        with patch.object(y, '_agent_job_dir', return_value=next_folder), \
                patch.object(y, '_agent_fetch_source', return_value=self.fetched()):
            self.call('fetch_source', url=self.job['url'])
        self.assertEqual(json.loads((next_folder / 'network-budget.json').read_text())['used'], 1)

    def test_two_processes_cannot_overspend_and_restart_does_not_reset(self):
        # Slow the commit to make a missing cross-process lock a reliable regression.
        job_file = self.job_folder / 'fixture-job.json'
        job_file.write_text(json.dumps(self.job), encoding='utf-8')
        script = '''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as y
folder, vault, state = map(Path, sys.argv[2:5])
job = json.loads((folder / 'fixture-job.json').read_text(encoding='utf-8'))
y._agent_load_job = lambda *_: dict(job)
y._agent_job_dir = lambda *_: folder
y._agent_fetch_source = lambda url: dict(url=url, links=[], text='public')
original = y._agent_json
def delayed(path, data):
    if path.name == 'network-budget.json':
        time.sleep(0.02)
    return original(path, data)
y._agent_json = delayed
ok = 0
for _ in range(y._AGENT_NETWORK_CALLS):
    try:
        y._agent_tool_call(vault, state, job['id'], 'fetch_source', {'url': job['url']})
        ok += 1
    except ValueError as exc:
        if 'cagri hakki doldu' not in str(exc):
            raise
print(ok)
'''
        args = [sys.executable, '-c', script, str(fixtures.SCRIPTS), str(self.job_folder), str(self.vault), str(self.state)]
        children = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for child in children:
            self.addCleanup(lambda process=child: process.kill() if process.poll() is None else None)
        counts = []
        for child in children:
            output, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, error.decode('utf-8', 'replace'))
            counts.append(int(output))
        self.assertEqual(sum(counts), y._AGENT_NETWORK_CALLS)
        later = subprocess.run(args, capture_output=True, timeout=20)
        self.assertEqual(later.returncode, 0, later.stderr.decode('utf-8', 'replace'))
        self.assertEqual(int(later.stdout), 0)

    def test_windows_contender_waits_without_writing_the_locked_byte(self):
        # Model Windows mandatory byte locks on every host: the peer owns byte 0
        # of an empty file, so an initialization write must fail before locking.
        path = self.job_folder / 'empty-tools.lock'
        path.touch()
        held, events = {'peer': True, 'self': False}, []

        def fdopen(*args, **kwargs):
            stream = os.fdopen(*args, **kwargs)
            guarded = Mock(wraps=stream)

            def write(data):
                if held['peer']:
                    raise PermissionError(errno.EACCES, 'byte 0 is locked by the peer')
                return stream.write(data)

            guarded.write.side_effect = write
            return guarded

        def locking(fd, mode, size):
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
            self.assertEqual(size, 1)
            if mode == 0:
                self.assertTrue(held['self'])
                held['self'] = False
                events.append('unlocked')
            elif held['peer']:
                events.append('contended')
                raise PermissionError(errno.EACCES, 'byte 0 is locked by the peer')
            else:
                held['self'] = True
                events.append('acquired')

        def release_peer(_):
            self.assertEqual(events, ['contended'])
            held['peer'] = False
            events.append('peer-released')

        windows_os = types.SimpleNamespace(**{**vars(os), 'name': 'nt', 'fdopen': fdopen})
        windows_sys = types.SimpleNamespace(**{**vars(sys), 'platform': 'win32'})
        crt = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.object(y, 'os', windows_os), patch.object(y, 'sys', windows_sys), \
                patch.dict(sys.modules, {'msvcrt': crt}), patch.object(time, 'sleep', side_effect=release_peer):
            with y._AgentFileLock(path):
                self.assertTrue(held['self'])
                events.append('critical-section')
        self.assertEqual(events, ['contended', 'peer-released', 'acquired', 'critical-section', 'unlocked'])

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows byte-range locking')
    def test_windows_child_waits_for_a_locked_empty_file(self):
        import msvcrt

        path, waited = self.job_folder / 'empty-tools.lock', self.job_folder / 'waited'
        script = '''
import msvcrt, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as y
original = msvcrt.locking
def observed(fd, mode, size):
    try:
        return original(fd, mode, size)
    except OSError as exc:
        if mode == msvcrt.LK_NBLCK and exc.errno in (13, 36):
            Path(sys.argv[3]).touch()
        raise
msvcrt.locking = observed
with y._AgentFileLock(Path(sys.argv[2])):
    print('acquired')
'''
        with path.open('w+b', buffering=0) as holder:
            # The CRT supports locking beyond EOF; keep the file empty and held
            # until the real child has observed contention, without a race timer.
            msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                child = subprocess.Popen([sys.executable, '-c', script, str(fixtures.SCRIPTS), str(path), str(waited)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                def cleanup():
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=20)

                self.addCleanup(cleanup)
                deadline = time.monotonic() + 20
                while not waited.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                observed_contention = waited.exists()
            finally:
                msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        output, error = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 0, error.decode('utf-8', 'replace'))
        self.assertTrue(observed_contention, 'the child must wait while the parent owns byte 0')
        self.assertEqual(output.strip(), b'acquired')

    def test_completion_and_other_calls_are_serialized(self):
        entered, release, writing = threading.Event(), threading.Event(), threading.Event()
        errors = []
        real_complete = y._agent_complete

        def complete(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test gate timed out')
            return real_complete(*args, **kwargs)

        def run(name, arguments):
            try:
                self.call(name, **arguments)
            except Exception as exc:
                errors.append(exc)

        with patch.object(y, '_agent_complete', side_effect=complete), \
                patch.object(y, '_agent_write_note', side_effect=lambda *_a, **_k: writing.set()):
            first = threading.Thread(target=run, args=('complete', {'status': 'completed', 'summary': 'Done.'}))
            second = threading.Thread(target=run, args=('write_note', {'title': 'Late', 'content': 'Must not exist'}))
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            try:
                self.assertFalse(writing.wait(0.1), 'write entered while completion owned the lock')
            finally:
                release.set()
                first.join(5)
                second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(writing.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn('already completed', str(errors[0]))

    def test_source_trust_distinguishes_capture_from_existing_memory(self):
        card = Path(self.job['card_path'])
        raw = (card.parent / y.RAW / (self.job['card_id'] + '.md')).as_posix()
        self.note(raw, 'captured prompt injection')
        self.note('knowledge/ordinary.md', 'existing memory')
        for path in (card.as_posix(), raw):
            with self.subTest(path=path):
                self.assertEqual(self.call('read_note', path=path)['source_trust'], 'untrusted')
        self.assertEqual(self.call('read_note', path='knowledge/ordinary.md')['source_trust'], 'vault-data')

    def test_inflight_fetch_losing_worker_authority_does_not_expand_urls(self):
        def fetch(_url):
            self.job['state'] = 'approval_required'
            return dict(self.fetched(), links=['https://example.com/new-source'])

        with patch.object(y, '_agent_fetch_source', side_effect=fetch), \
                patch.object(y, '_agent_record_source_urls') as record:
            with self.assertRaises(ValueError):
                self.call('fetch_source', url=self.job['url'])
        record.assert_not_called()
        self.assertEqual(self.budget()['used'], 1, 'an attempted request still consumes quota')

    def test_inflight_read_losing_worker_authority_does_not_return_note_data(self):
        self.note('knowledge/ordinary.md', 'private context that must not escape after revocation')
        real_read = y._agent_read_note

        def read(*args, **kwargs):
            value = real_read(*args, **kwargs)
            self.job['runtime_deadline_ns'] = 1
            return value

        with patch.object(y, '_agent_read_note', side_effect=read):
            with self.assertRaises(ValueError):
                self.call('read_note', path='knowledge/ordinary.md')

    def test_complete_acknowledges_snapshot_even_if_worker_has_already_closed_job(self):
        real_complete = y._agent_complete

        def complete(*args, **kwargs):
            value = real_complete(*args, **kwargs)
            self.job['state'] = 'completed'
            return value

        with patch.object(y, '_agent_complete', side_effect=complete):
            value = self.call('complete', status='completed', summary='Done.')
        self.assertEqual(value['status'], 'submitted')
        self.assertTrue((self.job_folder / 'result.json').is_file())

    def test_tool_list_describes_the_job_network_mode(self):
        for mode in ('kisitli', 'acik'):
            self.job['config'] = {'ag': mode}
            code, rows = self.protocol(self.handshake() + [{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])
            self.assertEqual(code, 0)
            specs = {item['name']: item['description'] for item in rows[-1]['result']['tools']}
            for name in ('fetch_source', 'download'):
                self.assertIn('at most 8 calls per job', specs[name])
                self.assertEqual('no model-created URLs' in specs[name], mode == 'kisitli')


class EscapeReviewTest(unittest.TestCase):
    @contextlib.contextmanager
    def guard(self, held=(), foreground=42):
        user, kernel, root, dismiss = Mock(), Mock(), Mock(), Mock()
        user.GetForegroundWindow.return_value = foreground
        user.GetAsyncKeyState.side_effect = lambda key: -32768 if key in held else 0
        user.SetWindowsHookExW.return_value = 100
        user.CallNextHookEx.return_value = 73
        kernel.GetModuleHandleW.return_value = 1
        with patch.object(C, 'WinDLL', side_effect=lambda name, **_kw: user if name == 'user32' else kernel, create=True), \
                patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True):
            stop = y._win_escape_guard(root, 99, 42, dismiss)
            callback = user.SetWindowsHookExW.call_args.args[1]

            def key(vk, message=0x100, flags=0, code=0):
                data = (C.c_uint32 * 6)(vk, 0, flags, 0, 0, 0)
                return callback(code, message, C.addressof(data))

            try:
                yield key, root, dismiss, user
            finally:
                stop()

    def test_modified_escape_passes_without_dismissing(self):
        combos = [(0x11, 0x10), (0x11,), (0x12,), (0xA2,), (0xA3,), (0xA0,), (0xA1,), (0x5B,), (0x5C,)]
        for keys in combos:
            with self.subTest(keys=keys), self.guard() as (key, root, dismiss, user):
                for vk in keys:
                    self.assertEqual(key(vk, 0x104 if vk == 0x12 else 0x100), 73)
                self.assertEqual(key(0x1B), 73)
                root.after.call_args.args[1]()
                dismiss.assert_not_called()
                self.assertEqual(user.GetAsyncKeyState.call_count, 8, 'no asynchronous polling in the callback')

    def test_releasing_one_of_two_control_keys_keeps_escape_modified(self):
        with self.guard() as (key, root, dismiss, _user):
            key(0xA2)
            key(0xA3)
            key(0xA2, 0x101)
            self.assertEqual(key(0x1B), 73)
            key(0xA3, 0x101)
            self.assertEqual(key(0x1B), 1)
            root.after.call_args.args[1]()
            dismiss.assert_called_once()

    def test_modifier_held_before_hook_is_respected_until_release(self):
        for vk in (0xA0, 0xA2, 0xA3, 0xA4, 0x5B):
            with self.subTest(vk=vk), self.guard(held=(vk,)) as (key, root, dismiss, _user):
                self.assertEqual(key(0x1B), 73)
                key(vk, 0x105 if vk == 0xA4 else 0x101)
                self.assertEqual(key(0x1B), 1)
                root.after.call_args.args[1]()
                dismiss.assert_called_once()

    def test_alt_context_and_key_up_are_passed(self):
        with self.guard() as (key, root, dismiss, _user):
            self.assertEqual(key(0x1B, 0x104, flags=0x20), 73)
            self.assertEqual(key(0x1B, 0x101, flags=0x80), 73)
            self.assertEqual(key(0x1B, code=-1), 73)
            root.after.call_args.args[1]()
            dismiss.assert_not_called()

    def test_plain_escape_in_another_window_passes_and_dismisses(self):
        with self.guard(foreground=777) as (key, root, dismiss, _user):
            self.assertEqual(key(0x1B), 73)
            root.after.call_args.args[1]()
            dismiss.assert_called_once()


class PostTitleReviewTest(unittest.TestCase):
    def test_time_inside_handle_or_display_name_is_not_the_split(self):
        cases = [('abc2h', 'Author @abc2h · 2h Actual post'),
                 ('normal', '2h channel @normal · 2h Actual post'),
                 ('ABC2h', 'Author @abc2H · 2h Actual post')]
        for handle, article in cases:
            with self.subTest(article=article):
                self.assertEqual(y._post_title('https://x.com/' + handle + '/status/1', article, '2h'),
                                 '@' + handle + ': Actual post')

    def test_missing_time_after_handle_keeps_full_article(self):
        article = '2h display name @abc2h Without a time link'
        self.assertEqual(y._post_title('https://x.com/abc2h/status/1', article, '2h'), article)


if __name__ == '__main__':
    unittest.main()
