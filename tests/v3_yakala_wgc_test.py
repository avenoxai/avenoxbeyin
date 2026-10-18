"""Portable COM/D3D lifecycle models; actual WGC is an explicit desktop acceptance test."""
import contextlib
import ctypes as C
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y


class NativeModel:
    """Each native out-pointer is a distinct owned reference, even on partial failure."""
    def __init__(self, fail=None, init=0, supported=True, empty=False, invalid=False):
        self.fail, self.init, self.supported, self.empty, self.invalid = fail, init, supported, empty, invalid
        self.refs, self.roles, self.closed, self.events, self.strings = set(), {}, [], [], {}
        self.next = 100
        self.mapped = self.unmapped = self.uninitialized = 0
        # Two BGRA pixels, followed by driver row padding, for each row.
        self.pixels = b'\x10\x20\x30\xff\x40\x50\x60\xff' * 2
        self.buffer = C.create_string_buffer(self.pixels[:8] + b'PAD!' + self.pixels[8:] + b'PAD!')
        self.combase = types.SimpleNamespace(
            RoInitialize=Mock(side_effect=self.initialize), RoUninitialize=Mock(side_effect=self.uninitialize),
            WindowsCreateString=Mock(side_effect=self.create_string),
            WindowsDeleteString=Mock(side_effect=self.delete_string),
            RoGetActivationFactory=Mock(side_effect=self.factory))
        self.d3d = types.SimpleNamespace(D3D11CreateDevice=Mock(side_effect=self.device),
                                        CreateDirect3D11DeviceFromDXGIDevice=Mock(side_effect=self.rt_device))

    def initialize(self, mode):
        assert mode == 1
        return self.init

    def uninitialize(self):
        self.uninitialized += 1

    def alloc(self, role, output=None):
        self.next += 1
        value = self.next
        self.refs.add(value)
        self.roles[value] = role
        if output is not None:
            output._obj.value = value
        return C.c_void_p(value)

    def check(self, stage):
        self.events.append(stage)
        if self.fail == stage:
            raise OSError('injected:' + stage)

    def create_string(self, text, length, out):
        assert length == len(text)
        self.next += 1
        out._obj.value = self.next
        self.strings[self.next] = text
        self.check('string')
        return 0

    def delete_string(self, value):
        self.strings.pop(value.value)
        return 0

    def factory(self, string, iid, out):
        roles = {'IGraphicsCaptureSessionStatics': 'supported', 'IGraphicsCaptureItemInterop': 'interop',
                 'IDirect3D11CaptureFramePoolStatics2': 'factory'}
        key = str(uuid.UUID(bytes_le=bytes(iid._obj)))
        name = next(k for k, v in y.WGC_IIDS.items() if v == key)
        self.alloc(roles[name], out)
        self.check('factory:' + roles[name])
        return 0

    def device(self, adapter, driver, software, flags, levels, count, version, device, level, context):
        assert adapter is None and driver == 1 and software is None and flags == 0x20 and version == 7
        self.alloc('device', device)
        self.alloc('context', context)
        level._obj.value = 0xb000
        self.check('device')
        return 0

    def rt_device(self, dxgi, out):
        assert self.roles[dxgi.value] == 'dxgi'
        self.alloc('inspectable', out)
        self.check('rt-device')
        return 0

    def query(self, obj, iid):
        role = self.roles[obj.value]
        if iid == y.WGC_IIDS['IClosable']:
            return self.alloc('close:' + role)
        targets = {'IDXGIDevice': 'dxgi', 'IDirect3DDevice': 'rtdevice',
                   'IDirect3DDxgiInterfaceAccess': 'access'}
        name = next(k for k, v in y.WGC_IIDS.items() if v == iid)
        self.check('query:' + name)
        return self.alloc(targets[name])

    def call(self, obj, slot, types=(), args=(), result='hr'):
        pointer = obj.value if hasattr(obj, 'value') else obj
        role = self.roles[pointer]
        assert pointer in self.refs, ('use after release', role)
        if slot == 2:
            assert result == 'ulong'
            self.refs.remove(pointer)
            self.events.append('release:' + role)
            return 0
        if role.startswith('close:'):
            assert slot == 6
            self.closed.append(role[6:])
            self.check('close:' + role[6:])
            return 0
        if role == 'supported':
            assert slot == 6
            args[0]._obj.value = int(self.supported)
        elif role == 'interop':
            assert slot == 3 and args[0] == 42
            assert str(uuid.UUID(bytes_le=bytes(args[1]._obj))) == y.WGC_IIDS['IGraphicsCaptureItem']
            self.alloc('item', args[2])
        elif role == 'item':
            assert slot == 7
            args[0]._obj.width = args[0]._obj.height = 2
        elif role == 'factory':
            assert slot == 6 and args[1:3] == (87, 2)
            assert args[3].width == args[3].height == 2
            self.alloc('pool', args[4])
        elif role == 'pool' and slot == 10:
            self.alloc('session', args[1])
        elif role == 'pool' and slot == 7:
            if not self.empty:
                self.alloc('frame', args[0])
        elif role == 'session':
            assert slot == 6
        elif role == 'frame' and slot == 8:
            args[0]._obj.width = args[0]._obj.height = 2
        elif role == 'frame' and slot == 6:
            self.alloc('surface', args[0])
        elif role == 'access':
            assert slot == 3
            assert str(uuid.UUID(bytes_le=bytes(args[0]._obj))) == y.WGC_IIDS['ID3D11Texture2D']
            self.alloc('texture', args[1])
        elif role == 'texture':
            assert slot == 10 and result == 'void'
            desc = args[0]._obj
            desc.Width = desc.Height = 2
            desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1
            desc.Format = 10 if self.invalid else 87
        elif role == 'device':
            assert slot == 5
            desc = args[0]._obj
            assert desc.Usage == 3 and desc.BindFlags == 0 and desc.CPUAccessFlags == 0x20000
            self.alloc('staging', args[2])
        elif role == 'context' and slot == 47:
            assert result == 'void'
        elif role == 'context' and slot == 14:
            assert args[1:4] == (0, 1, 0)
            args[4]._obj.pData = C.addressof(self.buffer)
            args[4]._obj.RowPitch = 12
            args[4]._obj.DepthPitch = 24
            if self.fail != 'context:14':
                self.mapped += 1
        elif role == 'context' and slot == 15:
            assert result == 'void'
            self.unmapped += 1
        else:
            raise AssertionError((role, slot))
        self.check(role + ':' + str(slot))
        return 0

    @contextlib.contextmanager
    def install(self):
        def dll(name, **_):
            return {'combase': self.combase, 'd3d11': self.d3d}[name]
        with patch.object(C, 'WinDLL', side_effect=dll, create=True), \
                patch.object(y, '_abi_call', side_effect=self.call), \
                patch.object(y, '_abi_query', side_effect=self.query):
            yield self


class WGCLifecycleTest(unittest.TestCase):
    def assert_disposed(self, model, initialized=True):
        self.assertEqual(model.refs, set(), model.roles)
        self.assertEqual(model.strings, {})
        self.assertEqual(model.uninitialized, int(initialized))
        self.assertEqual(model.mapped, model.unmapped)

    def test_frame_reads_padded_rows_and_closes_all_objects(self):
        model = NativeModel()
        with model.install(), y._WGCSession(42) as session:
            made = session.capture(.1)
        self.assertEqual(y.png_info(made[0])[:2], (2, 2))
        self.assert_disposed(model)
        self.assertEqual(model.closed, ['frame', 'session', 'pool', 'rtdevice'])
        self.assertLess(model.events.index('release:staging'), model.events.index('close:frame'))
        self.assertLess(model.events.index('close:session'), model.events.index('close:pool'))

    def test_partial_native_failures_release_every_registered_output(self):
        stages = ('string', 'factory:supported', 'supported:6', 'device', 'query:IDXGIDevice',
                  'rt-device', 'query:IDirect3DDevice', 'factory:interop', 'interop:3', 'item:7',
                  'factory:factory', 'factory:6', 'pool:10', 'session:6', 'pool:7', 'frame:8',
                  'frame:6', 'query:IDirect3DDxgiInterfaceAccess', 'access:3', 'texture:10',
                  'device:5', 'context:47', 'context:14')
        for stage in stages:
            with self.subTest(stage=stage):
                model = NativeModel(fail=stage)
                with self.assertRaises(OSError), model.install(), y._WGCSession(42) as session:
                    session.capture(.1)
                self.assert_disposed(model)

    def test_close_failures_do_not_skip_remaining_releases(self):
        for stage in ('close:frame', 'close:session', 'close:pool', 'close:rtdevice'):
            with self.subTest(stage=stage):
                model = NativeModel(fail=stage)
                with model.install(), y._WGCSession(42) as session:
                    self.assertIsNotNone(session.capture(.1))
                self.assert_disposed(model)

    def test_s_false_is_balanced_but_failed_roinit_is_not_uninitialized(self):
        for code in (1, -2147417850):
            with self.subTest(code=code):
                model = NativeModel(init=code)
                with model.install():
                    if code < 0:
                        with self.assertRaises(OSError):
                            with y._WGCSession(42):
                                pass
                    else:
                        with y._WGCSession(42):
                            pass
                self.assert_disposed(model, code >= 0)

    def test_unsupported_and_empty_frames_are_bounded_clean_failures(self):
        model = NativeModel(supported=False)
        with self.assertRaises(OSError), model.install(), y._WGCSession(42):
            pass
        self.assert_disposed(model)
        model = NativeModel(empty=True)
        with model.install(), y._WGCSession(42) as session:
            self.assertIsNone(session.capture(.02))
        self.assert_disposed(model)

    def test_malformed_surface_is_not_mapped(self):
        model = NativeModel(invalid=True)
        with model.install(), y._WGCSession(42) as session:
            self.assertIsNone(session.capture(.1))
        self.assert_disposed(model)
        self.assertNotIn('context:14', model.events)

    def test_cpu_copy_failure_still_unmaps_and_disposes(self):
        model = NativeModel()
        with self.assertRaises(ValueError), model.install(), \
                patch.object(y, '_mapped_rows', side_effect=ValueError('pitch')), y._WGCSession(42) as session:
            session.capture(.1)
        self.assert_disposed(model)
        self.assertEqual(model.unmapped, 1)


class UIAModel:
    def __init__(self, init=0, focused=False, changed=False, fail=None):
        self.init, self.focused, self.changed, self.fail = init, focused, changed, fail
        self.buffers, self.released, self.cleared, self.reads = [], [], [], 0
        self.ole32 = types.SimpleNamespace(CoInitializeEx=Mock(return_value=init), CoUninitialize=Mock(),
                                          CoCreateInstance=Mock(side_effect=self.create))
        self.oleaut = types.SimpleNamespace(SysAllocString=Mock(side_effect=self.bstr),
                                           VariantClear=Mock(side_effect=self.clear))

    def bstr(self, text):
        value = C.create_unicode_buffer(text)
        self.buffers.append(value)
        return C.addressof(value)

    def create(self, *args):
        args[-1]._obj.value = 100
        return 0

    def clear(self, arg):
        self.cleared.append((arg._obj.vt, arg._obj.v1))
        arg._obj.vt, arg._obj.v1 = 0, None
        return 0

    def call(self, obj, slot, *args, **_):
        value = obj.value if hasattr(obj, 'value') else obj
        if slot == 2:
            self.released.append(value)
            return 0
        if value == 100 and slot == 6:
            args[-1]._obj.value = 101
        elif value == 100 and slot == 23:
            assert args[0] == 30012  # ClassName, not arbitrary Edit elements.
            args[-1]._obj.value = 102
        elif value == 101 and slot == 5:
            args[-1]._obj.value = 103
        elif value == 103 and slot == 10:
            variant = args[-1]._obj
            if args[0] == 30008:
                variant.vt, variant.v1 = 11, 0xffff if self.focused else 0
            elif args[0] == 30045:
                self.reads += 1
                variant.vt = 8
                variant.v1 = self.bstr('https://example.org/' + ('changed' if self.changed and self.reads > 1 else 'page'))
            else:
                raise AssertionError(args[0])
            if self.fail == args[0]:
                raise OSError('partial property failure')
        else:
            raise AssertionError((value, slot))
        return 0

    @contextlib.contextmanager
    def install(self):
        windll = types.SimpleNamespace(ole32=self.ole32, oleaut32=self.oleaut)
        with patch.object(C, 'windll', windll, create=True), \
                patch.object(C, 'oledll', types.SimpleNamespace(ole32=self.ole32), create=True), \
                patch.object(y, '_com_call', side_effect=self.call):
            yield self


class UIALifecycleTest(unittest.TestCase):
    def test_stable_unfocused_value_and_mta_lifetime(self):
        for code in (0, 1):
            model = UIAModel(init=code)
            with model.install():
                self.assertEqual(y._uia_address_bar(42, 'OmniboxViewViews'), 'https://example.org/page')
            model.ole32.CoInitializeEx.assert_called_once_with(None, 0)
            model.ole32.CoUninitialize.assert_called_once()
            self.assertEqual(model.released, [103, 102, 101, 100])
            self.assertEqual(len(model.cleared), 4)

    def test_failed_initialization_never_creates_or_uninitializes(self):
        model = UIAModel(init=-2147417850)
        with model.install():
            self.assertIsNone(y._uia_address_bar(42, 'OmniboxViewViews'))
        model.ole32.CoCreateInstance.assert_not_called()
        model.ole32.CoUninitialize.assert_not_called()

    def test_focused_draft_and_changed_value_are_not_page_addresses(self):
        for options in ({'focused': True}, {'changed': True}):
            model = UIAModel(**options)
            with model.install():
                self.assertIsNone(y._uia_address_bar(42, 'OmniboxViewViews'))
            self.assertEqual(model.released, [103, 102, 101, 100])
            model.ole32.CoUninitialize.assert_called_once()

    def test_partially_filled_properties_are_cleared_on_error(self):
        for property_id in (30045, 30008):
            model = UIAModel(fail=property_id)
            with self.assertRaises(OSError), model.install():
                y._uia_address_bar(42, 'OmniboxViewViews')
            self.assertEqual(model.released, [103, 102, 101, 100])
            self.assertGreaterEqual(len(model.cleared), 2)
            model.ole32.CoUninitialize.assert_called_once()


class EscapeHookTest(unittest.TestCase):
    @contextlib.contextmanager
    def hooks(self):
        user = types.SimpleNamespace(**{name: Mock() for name in (
            'SetWindowsHookExW', 'CallNextHookEx', 'UnhookWindowsHookEx', 'GetForegroundWindow',
            'SetWinEventHook', 'UnhookWinEvent')})
        user.GetForegroundWindow.return_value = 42
        user.SetWindowsHookExW.return_value = 100
        user.CallNextHookEx.return_value = 7
        kernel = types.SimpleNamespace(GetModuleHandleW=Mock(return_value=200))
        root, dismiss = Mock(), Mock()
        with patch.object(C, 'WinDLL', side_effect=lambda name, **_: user if name == 'user32' else kernel, create=True), \
                patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True):
            stop = y._win_escape_guard(root, 99, 42, dismiss)
            try:
                yield user, root, dismiss, stop
            finally:
                stop()

    def test_escape_is_consumed_once_and_the_hook_is_released(self):
        with self.hooks() as (user, root, dismiss, stop):
            callback = user.SetWindowsHookExW.call_args.args[1]
            key = C.c_uint32(27)
            self.assertEqual(callback(0, 0x100, C.addressof(key)), 1)
            check = root.after.call_args.args[1]
            check()
            check()
            dismiss.assert_called_once_with()
            user.UnhookWindowsHookEx.assert_called_once_with(100)

    def test_escape_after_clicking_another_window_closes_and_passes_through(self):
        with self.hooks() as (user, root, dismiss, stop):
            callback = user.SetWindowsHookExW.call_args.args[1]
            user.GetForegroundWindow.return_value = 77
            key = C.c_uint32(27)
            self.assertEqual(callback(0, 0x100, C.addressof(key)), 7)
            root.after.call_args.args[1]()
            dismiss.assert_called_once_with()

    def test_escape_in_the_focused_popup_is_left_to_tk(self):
        with self.hooks() as (user, root, dismiss, stop):
            callback = user.SetWindowsHookExW.call_args.args[1]
            user.GetForegroundWindow.return_value = 99
            key = C.c_uint32(27)
            self.assertEqual(callback(0, 0x100, C.addressof(key)), 7)
            root.after.call_args.args[1]()
            dismiss.assert_not_called()

    def test_other_keys_and_negative_hook_codes_pass_through(self):
        with self.hooks() as (user, root, dismiss, stop):
            callback = user.SetWindowsHookExW.call_args.args[1]
            key = C.c_uint32(65)
            self.assertEqual(callback(0, 0x100, C.addressof(key)), 7)
            key.value = 27
            self.assertEqual(callback(-1, 0x100, C.addressof(key)), 7)
            dismiss.assert_not_called()


@unittest.skipUnless(os.name == 'nt' and os.environ.get('BEYIN_TEST_WGC') == '1',
                     'Actual WGC needs an unlocked interactive Windows desktop; set BEYIN_TEST_WGC=1 explicitly')
class WGCDesktopTest(unittest.TestCase):
    def test_real_wgc_captures_our_foreground_test_window_without_fallback(self):
        import tkinter as tk
        root = tk.Tk()
        root.title('Beyin WGC acceptance - test pattern')
        root.geometry('420x260+100+100')
        canvas = tk.Canvas(root, bg='#2355aa', width=420, height=260)
        canvas.pack()
        canvas.create_rectangle(40, 40, 180, 160, fill='#eeee33')
        root.update()
        self.addCleanup(root.destroy)
        hwnd = y._win_hwnd(root)
        self.assertTrue(y._win_focus(hwnd), 'Test window did not receive foreground; do not count this as WGC success')
        root.update()
        result = y._windows_read('wgc', {'hwnd': hwnd}, 5)
        self.assertEqual(result.get('method'), 'WGC', result)
        self.assertEqual(y.png_info(result['png'])[:2], (result['width'], result['height']))
        self.assertGreater(result['width'], 350)
        self.assertGreater(result['height'], 200)
        # Visual/color acceptance for GPU games is still performed with the manual probe.


if __name__ == '__main__':
    unittest.main()
