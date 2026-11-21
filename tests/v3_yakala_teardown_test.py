"""Warm popup lifetime and real byte-pipe protocol, without a Windows desktop."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from v3_yakala_pano_test import Canvas, Entry, Root, yakala


class WarmRoot(Root):
    def __init__(self, action, trace, active):
        super().__init__([])
        self.action, self.trace, self.active = action, trace, active
        self.loops, self.dispatching, self.quitting = 0, False, False
        self.protocols = {}

    def protocol(self, event, handler):
        self.protocols[event] = handler

    def quit(self):
        self.quitting = True

    def deiconify(self):
        if self.action == 'early-wm':
            self.protocols['WM_DELETE_WINDOW']()

    def destroy(self):
        self.trace.append(('destroy', self.dispatching))
        super().destroy()
        if self.action == 'destroy-error':
            raise RuntimeError('synthetic Tk teardown failure')

    def mainloop(self):
        self.loops += 1
        self.quitting = False
        if self.loops == 1:
            deadline = time.monotonic() + 2
            while self.alive and not self.quitting and time.monotonic() < deadline:
                handler, args = self.timers.pop(0)
                handler(*args)
                time.sleep(0.001)  # let the actual IPC reader enqueue ACTIVATE
            if not self.quitting:
                raise AssertionError('ACTIVATE was not delivered')
            return
        if not self.active.wait(2):
            raise AssertionError('ACTIVE was not written')
        self.dispatching = True
        try:
            if self.action == 'canvas':
                item, = [ix for ix, row in self.canvas.items.items() if row.get('text') == '×']
                self.canvas.tag_bind(item, '<Button-1>')(None)
            elif self.action in ('wm', 'early-wm', 'destroy-error'):
                self.protocols['WM_DELETE_WINDOW']()
            elif self.action == 'escape':
                self.bound['<Escape>'](types.SimpleNamespace(state=0))
            elif self.action == 'save':
                self.entry.type('synthetic note')
                self.bound['<Return>'](None)
                for _ in range(40):
                    if self.quitting or not self.alive:
                        break
                    handler, args = self.timers.pop(0)
                    handler(*args)
            if not self.quitting and self.alive:
                raise AssertionError('popup event did not exit its Tk loop')
        finally:
            self.dispatching = False
        self.trace.append(('mainloop-return', False))


class TraceOutput(io.BytesIO):
    def __init__(self, trace, active):
        super().__init__()
        self.trace, self.active = trace, active
        self.messages = []

    def write(self, data):
        message = json.loads(data)
        self.messages.append(message)
        self.trace.append((message['type'], False))
        written = super().write(data)
        if message['type'] == 'ACTIVE':
            self.active.set()
        return written


class WarmPopupTeardownTest(unittest.TestCase):
    def test_active_dismiss_unwinds_before_single_destroy_and_done(self):
        for action in ('canvas', 'wm', 'escape', 'save', 'prepare-cancel', 'early-wm',
                       'prepare-error', 'destroy-error'):
            with self.subTest(action=action), self.warm_window(action) as case:
                root, trace, output, capture, result = case
                saved = action == 'save'
                status = 'yakalandi' if saved else 'vazgecildi'
                if action == 'prepare-error':
                    self.assertIsInstance(result, RuntimeError)
                    self.assertEqual(str(result), 'synthetic preparation failure')
                else:
                    self.assertEqual(result, {'status': status})
                self.assertEqual(capture.call_count, int(saved))
                ran_active = action not in ('prepare-cancel', 'early-wm', 'prepare-error')
                self.assertEqual(root.loops, 2 if ran_active else 1)
                lifecycle = [event for event in trace if event[0] not in ('READY', 'ACTIVE')]
                expected = [('mainloop-return', False)] if ran_active else []
                self.assertEqual(lifecycle, expected + [('destroy', False), ('DONE', False)])
                self.assertEqual([message['type'] for message in output.messages], ['READY', 'ACTIVE', 'DONE'])
                self.assertEqual(output.messages[-1]['capture_id'], 'b' * 32)
                self.assertEqual(output.messages[-1]['status'], status)

    @contextlib.contextmanager
    def warm_window(self, action):
        trace, active, pipes = [], threading.Event(), []
        root = WarmRoot(action, trace, active)
        output = TraceOutput(trace, active)
        read_fd, write_fd = os.pipe()
        with tempfile.TemporaryDirectory() as temporary, os.fdopen(read_fd, 'rb') as incoming, \
                os.fdopen(write_fd, 'wb') as parent, contextlib.ExitStack() as patches:
            msg = yakala._protocol_message('ACTIVATE', 'a' * 32, capture_id='b' * 32,
                                           context={'schema': 2}, source_hwnd=0)
            parent.write((json.dumps(msg) + '\n').encode('ascii'))
            parent.flush()
            fake = types.ModuleType('tkinter')
            fake.Tk, fake.Canvas, fake.Entry, fake.TclError = lambda: root, Canvas, Entry, RuntimeError
            fake.font = types.ModuleType('tkinter.font')
            fake.font.Font = lambda **kw: types.SimpleNamespace(measure=lambda text: len(text) * 6)
            patches.enter_context(patch.dict(sys.modules, {'tkinter': fake, 'tkinter.font': fake.font}))
            windows = types.SimpleNamespace(**{**vars(os), 'name': 'nt',
                                               'environ': {k: v for k, v in os.environ.items()
                                                           if k != 'BEYIN_YAKALA_SNAPSHOT'}})
            streams = types.SimpleNamespace(**{**vars(sys), 'stdin': types.SimpleNamespace(buffer=incoming),
                                               'stdout': types.SimpleNamespace(buffer=output), 'platform': 'win32'})
            patches.enter_context(patch.object(yakala, 'os', windows))
            patches.enter_context(patch.object(yakala, 'sys', streams))
            # Keep real protocol and UI handlers; replace Windows calls and source acquisition only.
            for name in ('_win_dpi', '_win_managed_chrome', '_win_place', '_win_escape_guard'):
                patches.enter_context(patch.object(yakala, name))
            for name, value in dict(_win_hwnd=123, _win_monitor=None, _win_focus=True,
                                    agent_settings={'client': 'kapali'},
                                    _windows_prepare=(None if action == 'prepare-cancel' else
                                                      dict(context={}, clip='', image=None, shot=None,
                                                           sources=[], sequence=None)),
                                    _font=('Fake', 12)).items():
                patches.enter_context(patch.object(yakala, name, return_value=value))
            if action == 'prepare-error':
                yakala._windows_prepare.side_effect = RuntimeError('synthetic preparation failure')

            class Pipe(yakala._JsonPipe):
                def __init__(self, reader, writer):
                    super().__init__(reader, writer)
                    pipes.append(self)

            patches.enter_context(patch.object(yakala, '_JsonPipe', Pipe))
            capture = patches.enter_context(patch.object(yakala, 'capture', return_value={'status': 'yakalandi'}))
            try:
                try:
                    result = yakala._windows_warm_popup(Path(temporary), Path(temporary), 'a' * 32)
                except RuntimeError as exc:
                    result = exc
                yield root, trace, output, capture, result
            finally:
                # Keep stdin open through DONE, like the broker. Release the actual blocking reader
                # before the input file context closes (BufferedReader cannot close while it owns I/O).
                parent.close()
                for pipe in pipes:
                    pipe.read_thread.join(timeout=2)
                    pipe.write_thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
