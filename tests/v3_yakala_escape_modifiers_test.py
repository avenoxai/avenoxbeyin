"""Standalone regressions extracted from Yakala agent review (#384).

Synthetic input and Win32 call models only; no agent client is required.
"""
import contextlib
import ctypes as C
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y  # noqa: E402


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



if __name__ == '__main__':
    unittest.main()
