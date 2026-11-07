"""Popup recovery through the real listener/broker, with no desktop or child processes."""
import ctypes
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from v3_yakala_windows_test import FakeSession, MODULE, wait_until, yakala as y


class PopupModel:
    def __init__(self, test, instance, mode, pid):
        self.test, self.instance, self.mode = test, instance, mode
        self.failed, self.packets = threading.Event(), queue.Queue()
        self.ready_seen = False
        self.eof_queued = False
        self.done_seen = False
        self.exit_seen, self.reader_release = threading.Event(), threading.Event()
        self.sent = []
        self.process = types.SimpleNamespace(pid=pid, returncode=None, stdin=self, stdout=self)
        self.process.poll = self.poll
        self.read_thread = threading.Thread(target=self.read_final, daemon=True)
        self.read_thread.start()
        if mode != 'warm_timeout':
            self.packets.put(y._protocol_message('READY', instance, hwnd=111, pid=pid))

    def poll(self):
        if self.process.returncode is not None:
            self.exit_seen.set()
        return self.process.returncode

    def read_final(self):
        if self.mode not in ('exit_delayed_done', 'exit_stuck_reader'):
            return
        # The reader cannot queue DONE until the broker has observed process exit.
        if not self.exit_seen.wait(5):
            return
        if self.mode == 'exit_stuck_reader':
            self.reader_release.wait(10)
        else:
            time.sleep(0.02)
            self.packets.put(y._protocol_message('DONE', self.instance, capture_id=self.capture_id))
        self.failed.set()  # reader EOF, after its final packet has been queued

    def receive(self):
        try:
            message = self.packets.get_nowait()
        except queue.Empty:
            if (self.mode in ('eof_delayed_done', 'eof_delayed_active_done')
                    and hasattr(self, 'capture_id') and not self.eof_queued):
                # The reader queues its final packets after receive() saw an empty queue,
                # then publishes EOF before the broker checks the failed flag.
                self.eof_queued = True
                if self.mode == 'eof_delayed_active_done':
                    self.packets.put(y._protocol_message('ACTIVE', self.instance, capture_id=self.capture_id))
                self.packets.put(y._protocol_message('DONE', self.instance, capture_id=self.capture_id))
                self.failed.set()
            return None
        if message['type'] == 'READY':
            self.ready_seen = True
        if self.mode == 'exit_ready' and message['type'] == 'READY':
            self.process.returncode = 1
        if self.mode in ('exit_active', 'exit_delayed_done', 'exit_stuck_reader') and message['type'] == 'ACTIVE':
            self.process.returncode = 1
        if message['type'] == 'DONE':
            self.done_seen = True
            if self.mode in ('eof_delayed_done', 'eof_delayed_active_done'):
                self.process.returncode = 0
        return message

    def send(self, message):
        self.sent.append(message)
        if message['type'] == 'ACTIVATE':
            self.capture_id = message['capture_id']
            if self.mode not in ('activation_timeout', 'eof_delayed_active_done'):
                self.packets.put(y._protocol_message('ACTIVE', self.instance, capture_id=message['capture_id']))
        if message['type'] == 'STOP':
            self.process.returncode = 0
        return True

    def close(self):
        if self.test.fail_cleanup:
            raise subprocess.TimeoutExpired('fake-popup', 2)
        if self.test.reap_gate and self is self.test.owners[0]:
            self.test.reaping.set()
            if not self.test.reap_gate.wait(5):
                raise subprocess.TimeoutExpired('fake-popup', 5)
        self.process.returncode = 0
        self.test.events.append(('reaped', self.process.pid))

    def finish(self, timeout=0):
        pass


class BrokerRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yakala-broker-test-')
        self.addCleanup(self.tmp.cleanup)
        self.vault, self.state = Path(self.tmp.name) / 'vault', Path(self.tmp.name) / 'state'
        self.vault.mkdir()
        self.session = FakeSession()
        self.owners, self.brokers, self.events, self.overlaps = [], [], [], []
        self.modes = []
        self.fail_cleanup, self.reap_gate = False, None
        self.reaping = threading.Event()
        self.listener, self.result = None, {}
        broker_class = self.broker_class = y._WindowsPopupBroker

        def make_broker(*args, **kwargs):
            broker = broker_class(*args, validate_window=lambda hwnd, pid: hwnd == 111, **kwargs)
            self.brokers.append(broker)
            return broker

        for patcher in (
                patch.object(y, '_WIN32', (self.session, self.session)),
                patch.object(ctypes, 'get_last_error', self.session.get_error, create=True),
                patch.object(ctypes, 'set_last_error', self.session.set_error, create=True),
                patch.object(y, '_clipboard_sequence', return_value=10),
                patch.object(y, 'windows_context', return_value={'schema': 2, 'hwnd': 42}),
                patch.object(y, '_OwnedProcess', side_effect=self.spawn),
                patch.object(y, '_JsonPipe', side_effect=lambda reader, writer: reader),
                patch.object(broker_class, 'WARM_TIMEOUT', 0.08),
                patch.object(broker_class, 'ACTIVE_TIMEOUT', 0.08),
                patch.object(broker_class, 'RETRY_DELAY', 0.02),
                patch.object(y, '_WindowsPopupBroker', side_effect=make_broker)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop)

    def spawn(self, command, **options):
        alive = [owner.process.pid for owner in self.owners if owner.process.poll() is None]
        if alive:
            self.overlaps.append(alive)
            raise AssertionError('replacement spawned before the previous owner died')
        mode = self.modes.pop(0) if self.modes else 'normal'
        instance = command[command.index('--instance') + 1]
        owner = PopupModel(self, instance, mode, 100 + len(self.owners))
        self.events.append(('spawn', owner.process.pid))
        self.owners.append(owner)
        return owner

    def start(self, *modes):
        self.modes = list(modes)

        def listen():
            try:
                self.result['code'] = y.listen_windows(self.vault, MODULE, spec='ctrl+alt+b', state=self.state)
            except SystemExit as exc:
                self.result['code'] = exc.code
        self.listener = threading.Thread(target=listen, daemon=True)
        self.listener.start()
        self.assertTrue(wait_until(lambda: bool(self.brokers), 5))
        return self.brokers[0]

    def stop(self):
        if self.reap_gate:
            self.reap_gate.set()
        for owner in self.owners:
            owner.reader_release.set()
        if self.listener:
            y._windows_stop_listener(self.vault, timeout=5)
            self.listener.join(5)
            self.assertFalse(self.listener.is_alive())
        # A cleanup-failure test deliberately retains ownership until its assertion.
        self.fail_cleanup = False
        for broker in self.brokers:
            if not broker.thread.is_alive():
                broker._drop()

    def log(self):
        try:
            return (self.state / 'yakala/dinleyici.log').read_text(encoding='utf-8')
        except OSError:
            return ''

    def ready(self):
        handle = self.session.OpenEventW(0, False, y._windows_ready_event(self.vault))
        if not handle:
            return False
        try:
            return self.session.WaitForSingleObject(handle, 0) == 0
        finally:
            self.session.CloseHandle(handle)

    def assert_recovered(self, count=2):
        self.assertTrue(wait_until(lambda: len(self.owners) == count and self.owners[-1].ready_seen
                                   and self.ready(), 5), self.log())
        self.assertTrue(self.listener.is_alive())
        self.assertIsNone(self.brokers[0].error)
        self.assertEqual(list(self.session.hotkeys), [(0x4003, ord('B'))])
        self.assertEqual(self.overlaps, [])
        self.assertLess(self.events.index(('reaped', 100)), self.events.index(('spawn', 101)))

    def press(self):
        self.session.press(0x4003, ord('B'))

    def test_one_warm_timeout_keeps_listener_and_rewarms(self):
        self.start('warm_timeout', 'normal')
        self.assert_recovered()
        self.assertIn('asama: warming', self.log())
        self.assertIn('1/3 gecici hata', self.log())
        self.press()
        self.assertTrue(wait_until(lambda: any(m['type'] == 'ACTIVATE' for m in self.owners[1].sent), 5))

    def test_activation_timeout_does_not_replay_capture_and_next_key_works(self):
        self.start('activation_timeout', 'normal')
        self.press()
        self.assert_recovered()
        self.assertEqual(self.owners[1].sent, [])
        self.assertIn('DONE olmadan', self.log())
        self.assertIn('asama: activating', self.log())
        self.press()
        self.assertTrue(wait_until(lambda: bool(self.owners[1].sent), 5))
        before, after = self.owners[0].sent[0], self.owners[1].sent[0]
        self.assertEqual((before['type'], after['type']), ('ACTIVATE', 'ACTIVATE'))
        self.assertNotEqual(before['capture_id'], after['capture_id'])
        self.assertNotEqual(before['instance'], after['instance'])
        self.assertEqual(sum(m['type'] == 'ACTIVATE' for m in self.owners[0].sent), 1)

    def test_unexpected_warm_exit_keeps_listener_and_rewarms(self):
        self.start('exit_ready', 'normal')
        self.assert_recovered()
        self.assertIn('beklenmedik bicimde kapandi', self.log())

    def test_active_exit_without_done_does_not_replay_capture(self):
        self.start('exit_active', 'normal')
        self.press()
        self.assert_recovered()
        self.assertEqual(self.owners[1].sent, [])
        self.assertIn('DONE olmadan kapandi', self.log())

    def test_delayed_done_after_exit_rewarms_without_counting_failures(self):
        self.start('exit_delayed_done', 'exit_delayed_done', 'exit_delayed_done', 'normal')
        for count in range(2, 5):
            self.press()
            self.assert_recovered(count=count)
            previous = self.owners[count - 2]
            self.assertTrue(previous.exit_seen.is_set())
            self.assertTrue(previous.done_seen)
            self.assertFalse(previous.read_thread.is_alive())
            self.assertEqual(sum(m['type'] == 'ACTIVATE' for m in previous.sent), 1)
            self.assertEqual(self.owners[-1].sent, [])
        self.assertNotIn('gecici hata', self.log())
        self.assertNotIn('sonucu bilinmiyor', self.log())

    def test_final_packets_queued_during_eof_check_preserve_capture_phase(self):
        self.start('eof_delayed_done', 'eof_delayed_active_done', 'normal')
        for count in (2, 3):
            self.press()
            self.assert_recovered(count=count)
            previous = self.owners[count - 2]
            self.assertTrue(previous.done_seen)
            self.assertTrue(previous.eof_queued)
            self.assertEqual(self.owners[-1].sent, [])
        self.assertNotIn('gecici hata', self.log())
        self.assertNotIn('sonucu bilinmiyor', self.log())

    def test_final_packets_at_activation_deadline_are_drained_before_recovery(self):
        # Expire the handshake before the first receive after ACTIVATE, without
        # relying on scheduling delays. That receive queues ACTIVE/DONE and EOF.
        with patch.object(self.broker_class, 'ACTIVE_TIMEOUT', 0):
            self.start('eof_delayed_active_done', 'normal')
            self.press()
            self.assert_recovered()
        previous = self.owners[0]
        self.assertTrue(previous.eof_queued)
        self.assertTrue(previous.done_seen)
        self.assertEqual(sum(m['type'] == 'ACTIVATE' for m in previous.sent), 1)
        self.assertEqual(self.owners[-1].sent, [])
        self.assertNotIn('gecici hata', self.log())
        self.assertNotIn('sonucu bilinmiyor', self.log())

    def test_stuck_reader_after_exit_has_bounded_wait_and_does_not_replay_capture(self):
        self.start('exit_stuck_reader', 'normal')
        self.press()
        self.assert_recovered()
        self.assertTrue(self.owners[0].read_thread.is_alive())
        self.assertFalse(self.owners[0].done_seen)
        self.assertEqual(self.owners[1].sent, [])
        self.assertIn('DONE olmadan kapandi', self.log())
        self.assertIn('1/3 gecici hata', self.log())

    def test_third_recent_failure_wakes_listener_and_releases_hotkey(self):
        broker = self.start('warm_timeout', 'exit_ready', 'warm_timeout')
        self.listener.join(5)
        self.assertFalse(self.listener.is_alive())
        self.assertIn('3/3 gecici hata', broker.error)
        self.assertEqual(self.result['code'], broker.error)
        self.assertIn('yakala kur', self.log())
        self.assertEqual(self.session.hotkeys, {})
        self.assertFalse(y._windows_listener_running(self.vault))
        self.assertEqual(len(self.owners), 3)
        self.assertTrue(all(owner.process.poll() is not None for owner in self.owners))
        self.assertEqual(self.overlaps, [])

    def test_old_failures_expire_from_the_window(self):
        # Backoff exceeds this test's short window; production uses 120 seconds.
        with patch.object(self.broker_class, 'FAILURE_WINDOW', 0.05), \
                patch.object(self.broker_class, 'RETRY_DELAY', 0.08):
            self.start('warm_timeout', 'warm_timeout', 'warm_timeout', 'normal')
            self.assert_recovered(count=4)

    def test_no_replacement_until_old_process_is_reaped(self):
        self.reap_gate = threading.Event()
        self.start('warm_timeout', 'normal')
        self.assertTrue(self.reaping.wait(5))
        self.assertFalse(wait_until(lambda: len(self.owners) > 1, 0.1))
        self.assertEqual(len(self.owners), 1)
        self.assertIsNone(self.owners[0].process.poll())
        self.assertTrue(self.listener.is_alive())
        self.reap_gate.set()
        self.assert_recovered()

    def test_cleanup_failure_is_terminal_and_keeps_ownership(self):
        self.fail_cleanup = True
        broker = self.start('warm_timeout')
        self.listener.join(5)
        self.assertFalse(self.listener.is_alive())
        self.assertEqual(broker.error, 'TimeoutExpired')
        self.assertIs(broker.owned, self.owners[0])
        self.assertEqual(len(self.owners), 1)
        self.assertEqual(self.overlaps, [])
        self.assertEqual(self.session.hotkeys, {})

    def test_stop_interrupts_backoff_without_spawning_again(self):
        with patch.object(self.broker_class, 'RETRY_DELAY', 30):
            self.start('warm_timeout')
            self.assertTrue(wait_until(lambda: 'yeniden isitilacak' in self.log(), 5))
            self.stop()
        self.assertEqual(self.result, {'code': 0})
        self.assertEqual(len(self.owners), 1)


class JsonPipeDirectTest(unittest.TestCase):
    def test_real_pipe_eof_is_terminal(self):
        import io
        import os
        fd, write = os.pipe()
        reader = os.fdopen(fd, 'rb')
        os.close(write)
        pipe = y._JsonPipe(reader, io.BytesIO())
        self.addCleanup(pipe.finish)
        self.assertTrue(pipe.failed.wait(1))
        pipe.read_thread.join(1)
        self.assertFalse(pipe.read_thread.is_alive())

    def test_only_transient_errors_are_retried_with_a_bound(self):
        import errno
        import io
        for code, expected in ((errno.EAGAIN, 4), (errno.EINTR, 4), (errno.EIO, 1)):
            with self.subTest(code=code):
                class Reader(io.BytesIO):
                    calls = 0
                    def readline(self, limit):
                        self.calls += 1
                        raise OSError(code, 'read failed')
                reader = Reader()
                pipe = y._JsonPipe(reader, io.BytesIO())
                try:
                    self.assertTrue(pipe.failed.wait(1))
                    self.assertEqual(reader.calls, expected)
                finally:
                    pipe.finish()

    def test_transient_error_recovers_before_eof(self):
        import errno
        import io
        class Reader(io.BytesIO):
            calls = 0
            def readline(self, limit):
                self.calls += 1
                if self.calls == 1:
                    raise OSError(errno.EINTR, 'interrupted')
                return super().readline(limit)
        pipe = y._JsonPipe(Reader(b'{"type":"READY"}\n'), io.BytesIO())
        self.addCleanup(pipe.finish)
        self.assertTrue(pipe.failed.wait(1))
        self.assertEqual(pipe.receive(), {'type': 'READY'})

    def test_malformed_message_is_terminal(self):
        import io
        pipe = y._JsonPipe(io.BytesIO(b'invalid\n{"type":"READY"}\n'), io.BytesIO())
        self.addCleanup(pipe.finish)
        self.assertTrue(pipe.failed.wait(1))
        self.assertIsNone(pipe.receive())


if __name__ == '__main__':
    unittest.main()
