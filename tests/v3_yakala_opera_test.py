"""Opera's document provider is trusted only while its renderer and URL remain stable.

All native objects below are in-memory models. No browser, profile, clipboard, or file is changed.
"""
import contextlib
import ctypes as C
import io
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y

PAGE = 'https://www.youtube.com/'
LINK = 'https://www.youtube.com/watch?v=video'
DOCUMENT = {'value': PAGE, 'name': 'YouTube'}


class OperaSnapshotTest(unittest.TestCase):
    @contextlib.contextmanager
    def snapshot(self, documents=None, renderers=None, foreground=42, hover=None):
        user = Mock()
        user.GetForegroundWindow.return_value = foreground
        with patch.object(C, 'WinDLL', return_value=user, create=True), \
                patch.object(y, '_opera_renderer', side_effect=renderers, return_value=(55, 100)) as renderer, \
                patch.object(y, '_msaa_document', side_effect=documents, return_value=dict(DOCUMENT)) as document, \
                patch.object(y, '_uia_link_at_cursor', return_value=hover) as hovered:
            yield user, renderer, document, hovered

    def test_stable_document_and_hover_use_renderer_but_guard_browser_window(self):
        with self.snapshot(hover={'value': LINK, 'name': 'Video'}) as (_, renderer, document, hover):
            result = y._opera_snapshot(42, [10, 20])
        self.assertEqual(result.get('value'), PAGE)
        self.assertEqual(result.get('hover_value'), LINK)
        self.assertEqual(result.get('hover_name'), 'Video')
        self.assertGreaterEqual(document.call_count, 2)
        self.assertTrue(all(call.args == (55,) for call in document.call_args_list))
        self.assertTrue(all(call.args == (42,) for call in renderer.call_args_list))
        self.assertEqual(hover.call_args.args[0], 42)
        self.assertEqual(hover.call_args.kwargs.get('root'), 55)

    def test_url_title_or_renderer_change_discards_every_link(self):
        changes = (
            {'documents': [DOCUMENT, dict(DOCUMENT, value=PAGE + 'feed')]},
            {'documents': [DOCUMENT, dict(DOCUMENT, name='Other tab')]},
            {'renderers': [(55, 100), (56, 100)]},
            {'renderers': [(55, 100), (55, 101)]},
            {'renderers': [(55, 100), None]},
        )
        for options in changes:
            with self.subTest(options=options), self.snapshot(hover={'value': LINK}, **options):
                self.assertFalse(y._opera_snapshot(42, [10, 20]))

    def test_no_document_never_returns_unverified_hover(self):
        with self.snapshot(documents=[{}], hover={'value': LINK}) as (_, _, _, hovered):
            self.assertFalse(y._opera_snapshot(42, [10, 20]))
        hovered.assert_not_called()

    def test_invalid_hover_keeps_verified_document(self):
        for value in ('youtube.com/watch?v=video', 'javascript:alert(1)', 'http://127.0.0.1/', ''):
            with self.subTest(value=value), self.snapshot(hover={'value': value, 'name': 'Unsafe'}):
                result = y._opera_snapshot(42, [10, 20])
                self.assertEqual(result.get('value'), PAGE)
                self.assertFalse(result.get('hover_value'))

    def test_unreadable_hover_keeps_stable_verified_document(self):
        with self.snapshot() as (_, _, _, hover):
            hover.side_effect = OSError('provider unavailable')
            self.assertEqual(y._opera_snapshot(42, [10, 20]), DOCUMENT)

    def test_hover_changes_are_not_attached_to_the_stable_page(self):
        with self.snapshot() as (_, _, _, hover):
            hover.side_effect = [{'value': LINK, 'name': 'Video'}, {'value': LINK + '2', 'name': 'Other'}]
            result = y._opera_snapshot(42, [10, 20])
        self.assertEqual(result.get('value'), PAGE)
        self.assertFalse(result.get('hover_value'))


class RendererModel:
    def __init__(self, children=None):
        self.children = children if children is not None else {55: {}}
        self.user = types.SimpleNamespace(
            GetForegroundWindow=Mock(return_value=42), IsWindow=Mock(return_value=1),
            IsHungAppWindow=Mock(return_value=0), IsWindowVisible=Mock(side_effect=lambda h: self.children[h].get('visible', 1)),
            IsChild=Mock(side_effect=lambda parent, h: self.children[h].get('child', 1)),
            GetAncestor=Mock(side_effect=lambda h, _: self.children[h].get('root', 42)),
            GetClassNameW=Mock(side_effect=self.class_name), GetWindowThreadProcessId=Mock(side_effect=self.pid),
            EnumChildWindows=Mock(side_effect=self.enumerate))

    def class_name(self, hwnd, buffer, _):
        buffer.value = self.children[hwnd].get('class', 'Chrome_RenderWidgetHostHWND')
        return len(buffer.value)

    def pid(self, hwnd, pointer):
        pointer._obj.value = 100 if hwnd == 42 else self.children[hwnd].get('pid', 100)
        return 1

    def enumerate(self, window, callback, param):
        for hwnd in self.children:
            callback(hwnd, param)
        return 1

    @contextlib.contextmanager
    def install(self):
        with patch.object(C, 'WinDLL', return_value=self.user, create=True), \
                patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True):
            yield self


class OperaRendererTest(unittest.TestCase):
    def test_only_one_visible_renderer_in_same_browser_process_and_root(self):
        model = RendererModel({55: {}, 56: {'visible': 0}, 57: {'class': 'ToolbarWindow32'}})
        with model.install():
            self.assertEqual(y._opera_renderer(42), (55, 100))

    def test_missing_hidden_ambiguous_and_foreign_renderers_fail_closed(self):
        for children in ({}, {55: {'visible': 0}}, {55: {}, 56: {}}, {55: {'child': 0}},
                         {55: {'root': 99}}, {55: {'pid': 101}}, {55: {}, 56: {'pid': 101}},
                         {55: {'class': 'Chrome_RenderWidgetHostHWND-extra'}}):
            with self.subTest(children=children), RendererModel(children).install():
                self.assertIsNone(y._opera_renderer(42))

    def test_invalid_hung_background_window_or_unknown_owner_does_not_enumerate(self):
        for method, value in (('IsWindow', 0), ('IsHungAppWindow', 1), ('GetForegroundWindow', 99),
                              ('GetWindowThreadProcessId', 0)):
            model = RendererModel()
            function = getattr(model.user, method)
            function.side_effect, function.return_value = None, value
            with self.subTest(method=method), model.install():
                self.assertIsNone(y._opera_renderer(42))
            model.user.EnumChildWindows.assert_not_called()

    def test_foreground_switch_during_enumeration_fails_closed(self):
        model = RendererModel()
        model.user.GetForegroundWindow.side_effect = [42, 99]
        with model.install():
            self.assertIsNone(y._opera_renderer(42))


class MSAAModel:
    def __init__(self, init=0, role=15, state=0, url=PAGE, fail=None, role_vt=3, state_vt=3, access_hr=0):
        self.role, self.state, self.url, self.fail = role, state, url, fail
        self.role_vt, self.state_vt, self.access_hr = role_vt, state_vt, access_hr
        self.buffers, self.freed, self.cleared, self.released = [], [], [], []
        self.ole = types.SimpleNamespace(CoInitializeEx=Mock(return_value=init), CoUninitialize=Mock())
        self.auto = types.SimpleNamespace(VariantClear=Mock(side_effect=self.clear), SysFreeString=Mock(side_effect=self.free))
        self.access = types.SimpleNamespace(AccessibleObjectFromWindow=Mock(side_effect=self.acquire))

    def acquire(self, window, object_id, iid, pointer):
        assert window == 55
        assert object_id.value == 0xfffffffc
        pointer._obj.value = 100
        return self.access_hr

    def clear(self, pointer):
        self.cleared.append(pointer._obj.vt)
        pointer._obj.vt, pointer._obj.v1 = 0, None
        return 0

    def free(self, pointer):
        self.freed.append(pointer.value if hasattr(pointer, 'value') else pointer)

    def call(self, obj, slot, *args, **_):
        if slot == 2:
            self.released.append(obj.value)
            return 0
        assert args[0].vt == 3 and not args[0].v1  # CHILDID_SELF only, never a guessed descendant.
        if slot in (13, 14):
            variant = args[-1]._obj
            variant.vt = self.role_vt if slot == 13 else self.state_vt
            variant.v1 = self.role if slot == 13 else self.state
        elif slot in (10, 11):
            buffer = C.create_unicode_buffer(self.url if slot == 11 else '  YouTube\n title  ')
            self.buffers.append(buffer)
            args[-1]._obj.value = C.addressof(buffer)
        else:
            raise AssertionError(slot)
        if slot == self.fail:
            raise OSError('partial property failure')
        return 0

    @contextlib.contextmanager
    def install(self):
        with patch.object(C, 'windll', types.SimpleNamespace(ole32=self.ole, oleaut32=self.auto, oleacc=self.access), create=True), \
                patch.object(y, '_com_call', side_effect=self.call):
            yield self


class OperaMSAATest(unittest.TestCase):
    def test_document_url_and_title_have_balanced_mta_variant_and_bstr_lifetimes(self):
        for init in (0, 1):
            model = MSAAModel(init=init)
            with self.subTest(init=init), model.install():
                self.assertEqual(y._msaa_document(55), {'value': PAGE, 'name': 'YouTube title'})
            model.ole.CoInitializeEx.assert_called_once_with(None, 0)
            model.ole.CoUninitialize.assert_called_once()
            self.assertEqual(model.released, [100])
            self.assertEqual(len(model.cleared), 2)
            self.assertEqual(model.freed, [C.addressof(b) for b in model.buffers])

    def test_non_document_hidden_offscreen_and_untyped_values_are_not_pages(self):
        for options in ({'role': 42}, {'state': 0x8000}, {'state': 0x10000}, {'state': 0x18000},
                        {'role_vt': 0}, {'state_vt': 0}):
            model = MSAAModel(**options)
            with self.subTest(options=options), model.install():
                self.assertFalse(y._msaa_document(55))
            self.assertEqual(model.buffers, [])
            self.assertEqual(model.released, [100])
            model.ole.CoUninitialize.assert_called_once()

    def test_only_explicit_safe_http_document_urls_are_accepted(self):
        for url in ('youtube.com/', 'opera://startpage', 'file:///C:/secret', 'javascript:alert(1)',
                    'http://127.0.0.1/', 'http://192.168.1.2/', 'https://user:pass@example.org/',
                    'https://[broken/', 'https://example.org:bad/', ''):
            model = MSAAModel(url=url)
            with self.subTest(url=url), model.install():
                self.assertFalse(y._msaa_document(55))
            self.assertEqual(model.freed, [C.addressof(b) for b in model.buffers])
            self.assertEqual(model.released, [100])

    def test_partial_property_failures_free_every_returned_allocation(self):
        for slot in (13, 14, 11, 10):
            model = MSAAModel(fail=slot)
            with self.subTest(slot=slot), self.assertRaises(OSError), model.install():
                y._msaa_document(55)
            self.assertEqual(model.freed, [C.addressof(b) for b in model.buffers])
            self.assertGreaterEqual(len(model.cleared), 1)
            self.assertEqual(model.released, [100])
            model.ole.CoUninitialize.assert_called_once()

    def test_failed_com_init_does_not_acquire_or_uninitialize(self):
        model = MSAAModel(init=-2147417850)
        with model.install():
            self.assertFalse(y._msaa_document(55))
        model.access.AccessibleObjectFromWindow.assert_not_called()
        model.ole.CoUninitialize.assert_not_called()

    def test_failed_accessible_object_acquisition_releases_partial_reference(self):
        model = MSAAModel(access_hr=-2147467259)
        with model.install():
            self.assertFalse(y._msaa_document(55))
        self.assertEqual(model.released, [100])
        model.ole.CoUninitialize.assert_called_once()


class OperaPublicPathTest(unittest.TestCase):
    @contextlib.contextmanager
    def reader(self, result):
        user = types.SimpleNamespace(IsHungAppWindow=Mock(return_value=0))
        with patch.object(C, 'windll', types.SimpleNamespace(user32=user), create=True), \
                patch.object(y, '_windows_read', return_value=result) as read:
            yield read

    def test_browser_and_hover_use_bounded_opera_reader(self):
        cancel = threading.Event()
        with self.reader({'value': PAGE, 'hover_value': LINK, 'hover_name': ' Video\n title '}) as read:
            self.assertEqual(y.windows_browser_url(42, 'opera', .25, cancel), PAGE)
            self.assertEqual(y.windows_hovered_link(42, 'opera', [10, 20], .25, cancel), (LINK, 'Video title'))
        self.assertEqual([c.args[0] for c in read.call_args_list], ['opera', 'opera'])
        self.assertEqual(read.call_args.args[1], {'hwnd': 42, 'at': [10, 20]})
        self.assertTrue(all(c.args[2:] == (.25, cancel) for c in read.call_args_list))

    def test_helper_timeout_and_invalid_output_cannot_turn_into_inferred_urls(self):
        for result in ({'error': 'timeout'}, {}, {'value': 'example.com/', 'hover_value': 'example.com/link'},
                       {'value': 'opera://startpage', 'hover_value': 'http://localhost/'}):
            with self.subTest(result=result), self.reader(result):
                self.assertIsNone(y.windows_browser_url(42, 'opera'))
                self.assertEqual(y.windows_hovered_link(42, 'opera'), (None, None))

    def test_chrome_and_edge_keep_the_existing_omnibox_and_hyperlink_paths(self):
        for app in ('chrome', 'msedge'):
            with self.subTest(app=app), self.reader({'value': PAGE, 'name': 'YouTube'}) as read:
                self.assertEqual(y.windows_browser_url(42, app), PAGE)
                self.assertEqual(y.windows_hovered_link(42, app, [10, 20]), (PAGE, 'YouTube'))
            self.assertEqual([c.args[0] for c in read.call_args_list], ['uia', 'uia_link'])
            self.assertEqual(read.call_args_list[0].args[1]['class'], 'OmniboxViewViews')

    def test_enrich_uses_one_combined_snapshot_and_preserves_hover_precedence(self):
        context = {'hwnd': 42, 'uygulama': 'opera', 'pencere': 'YouTube - Opera Air', 'imlec': [10, 20]}
        cancel = threading.Event()
        with self.reader({'value': PAGE, 'name': 'YouTube', 'hover_value': LINK, 'hover_name': 'Video'}) as read, \
                patch.object(y, '_win_foreground', return_value=42), \
                patch.object(y, '_win_title', return_value=context['pencere']):
            result = y.windows_enrich(context, cancel)
        self.assertEqual(result['url'], PAGE)
        self.assertEqual(result['url_imlec'], LINK)
        self.assertEqual(result['baslik'], 'YouTube')
        self.assertEqual(result['baslik_imlec'], 'Video')
        read.assert_called_once()
        self.assertEqual(read.call_args.args[0:2], ('opera', {'hwnd': 42, 'at': [10, 20]}))
        self.assertIs(read.call_args.args[3], cancel)
        self.assertNotIn('url', context)

    def test_enrich_rejects_late_tab_switch(self):
        context = {'hwnd': 42, 'uygulama': 'opera', 'pencere': 'YouTube - Opera Air'}
        with self.reader({'value': PAGE, 'hover_value': LINK}), \
                patch.object(y, '_win_foreground', return_value=42), \
                patch.object(y, '_win_title', return_value='Other tab - Opera Air'):
            result = y.windows_enrich(context)
        for key in ('url', 'url_imlec', 'baslik', 'baslik_imlec'):
            self.assertNotIn(key, result)

    def test_failed_snapshot_clears_stale_context_links(self):
        context = {'hwnd': 42, 'uygulama': 'opera', 'pencere': 'YouTube - Opera Air',
                   'url': PAGE, 'baslik': 'old page', 'url_imlec': LINK, 'baslik_imlec': 'old link'}
        with self.reader({'error': 'timeout'}), patch.object(y, '_win_foreground', return_value=42):
            result = y.windows_enrich(context)
        for key in ('url', 'url_imlec', 'baslik', 'baslik_imlec'):
            self.assertFalse(result.get(key), key)


class OperaHelperProtocolTest(unittest.TestCase):
    def run_reader(self, request):
        stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode('utf-8')))
        stdout = types.SimpleNamespace(buffer=io.BytesIO())
        with patch.object(y.sys, 'stdin', stdin), patch.object(y.sys, 'stdout', stdout), \
                patch.object(y.os, 'name', 'nt'), patch.object(y, '_opera_snapshot', return_value=DOCUMENT) as snapshot:
            self.assertEqual(y._windows_reader_main('opera'), 0)
        return y._parse_read_packet(stdout.buffer.getvalue()), snapshot

    def test_reader_validates_hwnd_and_point_before_native_calls(self):
        for request in ({'hwnd': 0}, {'hwnd': -1}, {'hwnd': True}, {'hwnd': '42'},
                        {'hwnd': 42, 'at': [1]}, {'hwnd': 42, 'at': [True, 2]},
                        {'hwnd': 42, 'at': [1.5, 2]}, {'hwnd': 42, 'at': '1,2'}):
            with self.subTest(request=request):
                result, snapshot = self.run_reader(request)
                self.assertEqual(result.get('error'), 'ValueError')
                snapshot.assert_not_called()

    def test_reader_passes_the_keypress_point_and_returns_document_packet(self):
        result, snapshot = self.run_reader({'hwnd': 42, 'at': [-100, 50]})
        self.assertEqual(result, DOCUMENT)
        snapshot.assert_called_once_with(42, [-100, 50])

    def test_cancel_and_deadline_reach_the_existing_bounded_process_runner(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(y, '_windows_python', return_value='pythonw'), \
                patch.object(y, '_process_packet', return_value={}) as packet:
            self.assertEqual(y._windows_opera_snapshot(42, [10, 20], .05, cancel), {})
        self.assertEqual(packet.call_args.args[0][-2:], ['_win-read', 'opera'])
        self.assertEqual(packet.call_args.args[1:], ({'hwnd': 42, 'at': [10, 20]}, .05, cancel))


class OperaUIARootTest(unittest.TestCase):
    def test_hover_search_uses_renderer_root_but_pointer_guard_uses_top_window(self):
        user = Mock()
        user.WindowFromPoint.return_value = 55
        user.GetAncestor.return_value = 42
        ole = types.SimpleNamespace(CoInitializeEx=Mock(return_value=0), CoUninitialize=Mock(),
                                    CoCreateInstance=Mock(side_effect=lambda *a: setattr(a[-1]._obj, 'value', 100) or 0))
        auto = Mock()
        auto.SysAllocString.return_value = 0
        roots, released, conditions, searches = [], [], iter((102, 104)), iter((103, 105))

        def call(obj, slot, *args, **_):
            if slot == 2:
                released.append(obj.value)
            elif obj.value == 100 and slot == 6:
                roots.append(args[0].value)
                args[-1]._obj.value = 101
            elif obj.value == 100 and slot == 23:  # hyperlink, then article-role condition
                args[-1]._obj.value = next(conditions)
            elif obj.value == 101 and slot == 6:  # links, then articles: none under the pointer
                args[-1]._obj.value = next(searches)
            elif obj.value in (103, 105) and slot == 3:
                args[-1]._obj.value = 0
            else:
                raise AssertionError((obj.value, slot))
            return 0

        with patch.object(C, 'WinDLL', return_value=user, create=True), \
                patch.object(C, 'windll', types.SimpleNamespace(ole32=ole, oleaut32=auto), create=True), \
                patch.object(C, 'oledll', types.SimpleNamespace(ole32=ole), create=True), \
                patch.object(y, '_com_call', side_effect=call):
            self.assertIsNone(y._uia_link_at_cursor(42, [-100, 50], root=55, posts=True))
        user.GetAncestor.assert_called_once_with(55, 2)
        self.assertEqual(roots, [55])
        self.assertEqual(released, [105, 104, 103, 102, 101, 100])
        ole.CoUninitialize.assert_called_once()

    def test_pointer_on_another_window_is_rejected_before_provider_access(self):
        user = Mock()
        user.WindowFromPoint.return_value = 55
        user.GetAncestor.return_value = 99
        with patch.object(C, 'WinDLL', return_value=user, create=True), patch.object(y, '_com_call') as com:
            self.assertIsNone(y._uia_link_at_cursor(42, [10, 20], root=55))
        com.assert_not_called()


if __name__ == '__main__':
    unittest.main()
