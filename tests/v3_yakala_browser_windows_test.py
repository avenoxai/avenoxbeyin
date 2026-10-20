"""Windows document readers: noninteractive MSAA/Win32 models, no browser or desktop access."""
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

PAGE = 'http://example.com/current'
LINK = 'https://example.org/link'
DOCUMENT = {'value': PAGE, 'name': 'Current page'}


class TreeModel:
    def __init__(self, nodes=None, point='text'):
        self.nodes = nodes or {
            'root': {'role': 10, 'children': ['toolbar', 'tab']},
            'toolbar': {'role': 42, 'value': 'https://uncommitted.example/'},
            'tab': {'role': 15, 'value': PAGE, 'name': 'Current page', 'children': ['link', 'iframe']},
            'link': {'role': 30, 'value': LINK, 'name': ' A\n link ', 'parent': 'tab', 'rect': (-40, 0, 100, 80)},
            'text': {'role': 42, 'parent': 'link'},
            'iframe': {'role': 15, 'value': 'https://iframe.example/', 'parent': 'tab'},
        }
        self.point = point
        self.visited, self.reads = [], []
        self.close = Mock()

    def identity(self, node):
        return node

    def root(self, window):
        return 'root'

    def integer(self, node, slot):
        value = self.nodes[node].get('role' if slot == 13 else 'state', 0)
        if value is None:
            raise OSError('missing property')
        return value

    def text(self, node, slot):
        self.reads.append((node, slot))
        return self.nodes[node].get('value' if slot == 11 else 'name', '')

    def children(self, node, remaining):
        self.visited.append(node)
        children = self.nodes[node].get('children', [])
        if len(children) > remaining:
            raise ValueError('limit')
        return children

    def at_point(self, at):
        return self.point

    def parent(self, node):
        return self.nodes[node]['parent']

    def rectangle(self, node):
        return self.nodes[node].get('rect', (0, 0, 0, 0))


class GeckoDocumentTest(unittest.TestCase):
    def test_native_chrome_shell_application_role_is_traversed_without_reading_its_url(self):
        model = TreeModel()
        model.nodes['root'].update(role=14, value='chrome://browser/content/browser.xhtml')
        self.assertEqual(y._gecko_document(model, 'root'), ('tab', DOCUMENT))
        self.assertNotIn(('root', 11), model.reads)

    def test_committed_document_ignores_address_drafts_and_never_enters_iframes(self):
        model = TreeModel()
        self.assertEqual(y._gecko_document(model, 'root'), ('tab', DOCUMENT))
        self.assertNotIn('tab', model.visited)
        self.assertNotIn('iframe', model.visited)
        self.assertNotIn(('toolbar', 11), model.reads)

    def test_two_visible_documents_are_ambiguous_even_with_the_same_url(self):
        for second_url in (PAGE, 'https://second.example/', 'about:blank'):
            model = TreeModel()
            model.nodes['other'] = {'role': 15, 'value': second_url}
            model.nodes['root']['children'].append('other')
            with self.subTest(second_url=second_url):
                self.assertIsNone(y._gecko_document(model, 'root'))

    def test_invisible_and_offscreen_tab_subtrees_are_pruned(self):
        for state in (0x8000, 0x10000, 0x18000):
            model = TreeModel()
            model.nodes['hidden'] = {'role': 10, 'state': state, 'children': ['iframe']}
            model.nodes['root']['children'].append('hidden')
            with self.subTest(state=state):
                self.assertEqual(y._gecko_document(model, 'root'), ('tab', DOCUMENT))
                self.assertNotIn('hidden', model.visited)

    def test_busy_or_internal_document_is_not_replaced_with_an_iframe(self):
        for options in ({'state': 0x800}, {'value': 'about:blank'}, {'value': 'file:///private'},
                        {'value': 'example.com/'}, {'value': 'https://user:pass@example.com/'},
                        {'value': 'http://127.0.0.1/'}, {'value': 'https://[broken/'},
                        {'value': 'https://example.org/path\x00hidden'}):
            model = TreeModel()
            model.nodes['tab'].update(options)
            with self.subTest(options=options):
                self.assertIsNone(y._gecko_document(model, 'root'))
                self.assertNotIn('tab', model.visited)

    def test_missing_state_fails_instead_of_assuming_visibility(self):
        model = TreeModel()
        model.nodes['tab']['state'] = None
        with self.assertRaises(OSError):
            y._gecko_document(model, 'root')

    def test_ui_tree_cycle_is_rejected(self):
        model = TreeModel()
        model.nodes['root']['children'].append('root')
        self.assertIsNone(y._gecko_document(model, 'root'))

    def test_ui_tree_count_cannot_be_truncated_to_first_document(self):
        model = TreeModel()
        with self.assertRaises(ValueError):
            y._gecko_document(model, 'root', limit=2)

    def test_deep_tree_fails_at_limit(self):
        nodes = {str(i): {'role': 10, 'children': [str(i + 1)]} for i in range(40)}
        nodes['40'] = {'role': 15, 'value': PAGE}
        self.assertIsNone(y._gecko_document(TreeModel(nodes), '0'))

    def test_no_web_document_yields_nothing(self):
        model = TreeModel()
        model.nodes['root']['children'] = ['toolbar']
        self.assertIsNone(y._gecko_document(model, 'root'))


class GeckoHoverTest(unittest.TestCase):
    def test_hit_text_reaches_one_link_inside_selected_document(self):
        self.assertEqual(y._gecko_hover(TreeModel(), 'tab', (-10, 20)),
                         {'value': LINK, 'name': 'A link', '_identity': 'link'})

    def test_iframe_link_may_be_hovered_but_cannot_replace_top_level_document(self):
        model = TreeModel()
        model.nodes['link']['parent'] = 'iframe'
        self.assertEqual(y._gecko_hover(model, 'tab', (-10, 20))['value'], LINK)
        self.assertEqual(y._gecko_document(model, 'root')[1]['value'], PAGE)

    def test_foreign_document_or_browser_toolbar_cannot_supply_hover_link(self):
        for point in ('toolbar', 'foreign'):
            model = TreeModel(point=point)
            model.nodes['foreign'] = {'role': 30, 'value': LINK, 'rect': (-40, 0, 100, 80), 'parent': 'foreign'}
            model.nodes['toolbar']['parent'] = 'toolbar'
            with self.subTest(point=point):
                self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_no_point_does_not_query_global_accessibility(self):
        model = TreeModel()
        with patch.object(model, 'at_point') as hit:
            self.assertFalse(y._gecko_hover(model, 'tab', None))
        hit.assert_not_called()

    def test_invisible_or_offscreen_hover_is_discarded(self):
        for state in (0x8000, 0x10000):
            model = TreeModel()
            model.nodes['link']['state'] = state
            with self.subTest(state=state):
                self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_busy_link_or_iframe_ancestor_cannot_supply_hover(self):
        for busy in ('link', 'iframe'):
            model = TreeModel()
            model.nodes['link']['parent'] = 'iframe'
            model.nodes[busy]['state'] = 0x800
            with self.subTest(busy=busy):
                self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_link_rectangle_must_contain_physical_point(self):
        for rect in ((0, 0, 10, 10), (-40, 0, 0, 80), (-40, 0, 100, 0), (-40, 0, 30, 80)):
            model = TreeModel()
            model.nodes['link']['rect'] = rect
            with self.subTest(rect=rect):
                self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_provider_url_is_never_completed_with_an_invented_scheme(self):
        for value in ('example.org/link', 'javascript:alert(1)', 'http://192.168.0.1/', 'https://a:b@example.org/'):
            model = TreeModel()
            model.nodes['link']['value'] = value
            with self.subTest(value=value):
                self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_multiple_link_ancestors_are_ambiguous_even_with_same_value(self):
        model = TreeModel()
        model.nodes['outer'] = dict(model.nodes['link'])
        model.nodes['link']['parent'] = 'outer'
        self.assertFalse(y._gecko_hover(model, 'tab', (-10, 20)))

    def test_hit_on_plain_document_keeps_only_page(self):
        self.assertFalse(y._gecko_hover(TreeModel(point='tab'), 'tab', (-10, 20)))

    def test_parent_walk_has_finite_budget(self):
        nodes = {str(i): {'role': 10, 'parent': str(i + 1)} for i in range(66)}
        self.assertFalse(y._gecko_hover(TreeModel(nodes, point='0'), 'tab', (1, 1)))


class GeckoSnapshotTest(unittest.TestCase):
    @contextlib.contextmanager
    def installed(self, model=None):
        model = model or TreeModel()
        with patch.object(y, '_WindowsMSAA', return_value=model), \
                patch.object(y, '_win_document_identity', return_value=(42, 100, 'MozillaWindowClass', 'firefox')) as identity, \
                patch.object(y, '_win_pointer_in_window', return_value=(-10, 20)) as point:
            yield model, identity, point

    def test_page_and_hover_are_collected_in_same_owned_snapshot(self):
        with self.installed() as (model, identity, point):
            result = y._gecko_snapshot(42, 'firefox', [-10, 20])
        self.assertEqual(result, dict(DOCUMENT, hover_value=LINK, hover_name='A link'))
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(point.call_count, 2)
        model.close.assert_called_once()

    def test_foreground_identity_change_discards_all_urls(self):
        with self.installed() as (model, identity, _):
            identity.side_effect = [(42, 100), (42, 101)]
            self.assertFalse(y._gecko_snapshot(42, 'firefox'))
        model.close.assert_called_once()

    def test_same_title_and_url_with_new_document_identity_is_rejected(self):
        with self.installed(), patch.object(y, '_gecko_document', side_effect=[('tab', DOCUMENT), ('other', DOCUMENT)]):
            self.assertFalse(y._gecko_snapshot(42, 'firefox'))

    def test_document_url_or_title_change_is_rejected(self):
        for changed in (dict(DOCUMENT, value=PAGE + '/next'), dict(DOCUMENT, name='New title')):
            with self.subTest(changed=changed), self.installed(), \
                    patch.object(y, '_gecko_document', side_effect=[('tab', DOCUMENT), ('tab', changed)]):
                self.assertFalse(y._gecko_snapshot(42, 'zen'))

    def test_transient_missing_document_is_rejected(self):
        with self.installed(), patch.object(y, '_gecko_document', side_effect=[('tab', DOCUMENT), None]):
            self.assertFalse(y._gecko_snapshot(42, 'zen'))

    def test_changed_hover_keeps_only_stable_document(self):
        with self.installed(), patch.object(y, '_gecko_hover', side_effect=[{'value': LINK, 'name': 'A'}, {}]):
            self.assertEqual(y._gecko_snapshot(42, 'firefox'), DOCUMENT)

    def test_replaced_link_with_identical_url_and_name_is_discarded(self):
        first = {'value': LINK, 'name': 'A link', '_identity': (1001, 0)}
        second = dict(first, _identity=(1002, 0))
        with self.installed(), patch.object(y, '_gecko_hover', side_effect=[first, second]):
            self.assertEqual(y._gecko_snapshot(42, 'firefox'), DOCUMENT)

    def test_pointer_ownership_change_discards_hover(self):
        with self.installed() as (_, _, point):
            point.side_effect = [(-10, 20), None]
            self.assertEqual(y._gecko_snapshot(42, 'firefox'), DOCUMENT)

    def test_hit_test_failure_preserves_stable_document_and_closes_provider(self):
        with self.installed() as (model, _, _), patch.object(y, '_gecko_hover', side_effect=OSError('provider')):
            self.assertEqual(y._gecko_snapshot(42, 'firefox'), DOCUMENT)
        model.close.assert_called_once()

    def test_provider_closed_even_when_document_enumeration_fails(self):
        with self.installed() as (model, _, _), patch.object(y, '_gecko_document', side_effect=OSError('provider')):
            with self.assertRaises(OSError):
                y._gecko_snapshot(42, 'firefox')
        model.close.assert_called_once()


class NativeMSAAModel:
    """The actual ctypes adapter runs against modeled SDK exports and COM slots."""
    def __init__(self, init=0):
        self.buffers, self.freed, self.released, self.variants, self.qi = {}, [], [], [], []
        self.child_values = [(9, 1002), (3, -7)]
        self.child_count, self.obtained, self.child_hr = 2, 2, 0
        self.root_hr, self.fail_slot, self.integer_vt = 0, None, 3
        self.point_child, self.point_vt = -7, 3
        self.ole = types.SimpleNamespace(CoInitializeEx=Mock(return_value=init), CoUninitialize=Mock())
        self.auto = types.SimpleNamespace(VariantClear=Mock(side_effect=self.clear), SysFreeString=Mock(side_effect=self.free),
                                         SysStringLen=Mock(side_effect=lambda p: self.buffers[p.value][1]))
        self.access = types.SimpleNamespace(AccessibleObjectFromWindow=Mock(side_effect=self.root),
                                           AccessibleChildren=Mock(side_effect=self.children),
                                           AccessibleObjectFromPoint=Mock(side_effect=self.at_point))

    def root(self, window, ident, iid, pointer):
        assert window == 42 and ident == 0xfffffffc
        pointer._obj.value = 1
        return self.root_hr

    def children(self, obj, start, count, values, obtained):
        for value, (vt, pointer) in zip(values, self.child_values):
            value.vt, value.v1 = vt, pointer & 0xffffffff
        obtained._obj.value = self.obtained
        return self.child_hr

    def at_point(self, point, obj, child):
        assert (point.x, point.y) == (-10, 20)
        obj._obj.value = 2
        child._obj.vt, child._obj.v1 = self.point_vt, self.point_child & 0xffffffff
        return 0

    def clear(self, value):
        value = value._obj
        self.variants.append((value.vt, value.v1))
        value.vt, value.v1 = 0, None
        return 0

    def free(self, pointer):
        self.freed.append(pointer.value)

    def call(self, obj, slot, *args, **kwargs):
        pointer = obj.value
        if slot == 2:
            assert kwargs['restype'] is C.c_uint32
            self.released.append(pointer)
            return 0
        if slot == 0:
            iid = args[0]._obj
            unknown = iid.a == 0
            self.qi.append((pointer, unknown))
            args[-1]._obj.value = (pointer % 1000) + (2000 if unknown else 0)
        elif slot == 8:
            assert C.sizeof(kwargs['argtypes'][0]._type_) == 4
            args[-1]._obj.value = self.child_count
        elif slot in (13, 14):
            args[-1]._obj.vt = self.integer_vt
            args[-1]._obj.v1 = 15 if slot == 13 else 0
        elif slot in (10, 11):
            text = PAGE if slot == 11 else 'Current page'
            buffer = C.create_unicode_buffer(text)
            address = C.addressof(buffer)
            self.buffers[address] = (buffer, len(text))
            args[-1]._obj.value = address
        elif slot == 7:
            args[-1]._obj.value = 1001
        elif slot == 22:
            for pointer, value in zip(args, (-40, 0, 100, 80)):
                pointer._obj.value = value
            self.assert_location_child = args[-1].v1
            assert all(C.sizeof(t._type_) == 4 for t in kwargs['argtypes'][:4])
        else:
            raise AssertionError(slot)
        if self.fail_slot == slot:
            raise OSError('partial output')
        return 0

    @contextlib.contextmanager
    def installed(self):
        libraries = {'ole32': self.ole, 'oleaut32': self.auto, 'oleacc': self.access}
        with patch.object(C, 'WinDLL', side_effect=libraries.__getitem__, create=True), \
                patch.object(y, '_com_call', side_effect=self.call):
            yield self


class NativeMSAATest(unittest.TestCase):
    def test_abi_guid_variant_and_point_match_windows_layout(self):
        T = y._win_msaa_types()
        self.assertEqual(C.sizeof(T.GUID), 16)
        self.assertEqual(C.sizeof(T.POINT), 8)
        self.assertEqual(C.sizeof(T.LONG), 4)
        self.assertEqual(T.VARIANT.v1.offset, 8)
        self.assertEqual(C.sizeof(T.VARIANT), 8 + 2 * C.sizeof(C.c_void_p))
        self.assertEqual(C.alignment(T.VARIANT), C.alignment(C.c_uint64))
        for pointer, expected in ((C.c_uint32, 16), (C.c_uint64, 24)):
            variant = y._win_msaa_types(pointer).VARIANT
            self.assertEqual(C.sizeof(variant), expected)
            self.assertEqual(C.alignment(variant), C.alignment(C.c_uint64))
            self.assertEqual(variant.v1.offset, 8)

    def test_partial_accessible_object_output_is_released_on_failure(self):
        model = NativeMSAAModel()
        model.root_hr = -1
        with model.installed():
            provider = y._WindowsMSAA()
            try:
                with self.assertRaises(OSError):
                    provider.root(42)
            finally:
                provider.close()
        self.assertEqual(model.released, [1])
        model.ole.CoUninitialize.assert_called_once()

    def test_com_init_failure_never_uninitializes_or_acquires(self):
        model = NativeMSAAModel(init=-1)
        with model.installed(), self.assertRaises(OSError):
            y._WindowsMSAA()
        model.ole.CoUninitialize.assert_not_called()
        model.access.AccessibleObjectFromWindow.assert_not_called()

    def test_s_false_init_is_balanced_and_close_is_idempotent(self):
        model = NativeMSAAModel(init=1)
        with model.installed():
            provider = y._WindowsMSAA()
            provider.close()
            provider.close()
        model.ole.CoUninitialize.assert_called_once()

    def test_children_dispatch_requires_query_interface_and_balanced_variant_clear(self):
        model = NativeMSAAModel()
        with model.installed():
            provider = y._WindowsMSAA()
            root = provider.root(42)
            children = provider.children(root, 20)
            self.assertEqual([(p.value, child) for p, child in children], [(2, 0), (1, -7)])
            self.assertEqual(provider.identity(children[0]), (2002, 0))
            provider.close()
        self.assertEqual(model.qi, [(1002, False), (2, True)])
        self.assertIn((9, 1002), model.variants)
        self.assertEqual(model.released, [2002, 2, 1])

    def test_partial_children_enumeration_releases_every_output_and_returns_no_prefix(self):
        for hr, obtained in ((1, 1), (0, 1), (-1, 2), (0, 3)):
            model = NativeMSAAModel()
            model.child_hr, model.obtained = hr, obtained
            with self.subTest(hr=hr, obtained=obtained), model.installed():
                provider = y._WindowsMSAA()
                try:
                    with self.assertRaises(OSError):
                        provider.children(provider.root(42), 20)
                finally:
                    provider.close()
            self.assertIn((9, 1002), model.variants)
            self.assertEqual(len(model.variants), 2)
            self.assertEqual(model.qi, [])

    def test_child_count_limit_rejects_before_allocation_or_enumeration(self):
        for count in (-1, 513):
            model = NativeMSAAModel()
            model.child_count = count
            with self.subTest(count=count), model.installed():
                provider = y._WindowsMSAA()
                try:
                    with self.assertRaises(ValueError):
                        provider.children(provider.root(42), 512)
                finally:
                    provider.close()
            model.access.AccessibleChildren.assert_not_called()

    def test_failed_dispatch_query_cleans_up_its_partial_reference_and_all_variants(self):
        model = NativeMSAAModel()
        model.fail_slot = 0
        with model.installed():
            provider = y._WindowsMSAA()
            try:
                with self.assertRaises(OSError):
                    provider.children(provider.root(42), 20)
            finally:
                provider.close()
        self.assertEqual(model.released, [2, 1])
        self.assertEqual(len(model.variants), 2)

    def test_simple_child_never_enumerates_parent_as_its_children(self):
        model = NativeMSAAModel()
        with model.installed():
            provider = y._WindowsMSAA()
            self.assertEqual(provider.children((C.c_void_p(1), -7), 20), [])
            provider.close()
        model.access.AccessibleChildren.assert_not_called()

    def test_integer_and_bstr_cleanup_after_partial_property_failure(self):
        for slot in (13, 14, 10, 11):
            model = NativeMSAAModel()
            model.fail_slot = slot
            with self.subTest(slot=slot), model.installed():
                provider = y._WindowsMSAA()
                try:
                    with self.assertRaises(OSError):
                        (provider.integer if slot in (13, 14) else provider.text)((C.c_void_p(1), 0), slot)
                finally:
                    provider.close()
            if slot in (13, 14):
                self.assertEqual(len(model.variants), 1)
            else:
                self.assertEqual(model.freed, list(model.buffers))

    def test_wrong_variant_tag_is_not_interpreted_as_integer(self):
        model = NativeMSAAModel()
        model.integer_vt = 11
        with model.installed():
            provider = y._WindowsMSAA()
            try:
                with self.assertRaises(OSError):
                    provider.integer((C.c_void_p(1), 0), 13)
            finally:
                provider.close()
        self.assertEqual(len(model.variants), 1)

    def test_bstr_length_is_bounded_before_copying(self):
        model = NativeMSAAModel()
        model.auto.SysStringLen.side_effect = None
        model.auto.SysStringLen.return_value = 32769
        with model.installed():
            provider = y._WindowsMSAA()
            try:
                with self.assertRaises(ValueError):
                    provider.text((C.c_void_p(1), 0), 11)
            finally:
                provider.close()
        self.assertEqual(model.freed, list(model.buffers))

    def test_point_and_parent_preserve_negative_child_ids_without_unsafe_cast(self):
        model = NativeMSAAModel()
        with model.installed():
            provider = y._WindowsMSAA()
            node = provider.at_point((-10, 20))
            self.assertEqual((node[0].value, node[1]), (2, -7))
            parent = provider.parent(node)
            self.assertEqual((parent[0].value, parent[1]), (2, 0))
            next_parent = provider.parent(parent)
            self.assertEqual((next_parent[0].value, next_parent[1]), (1, 0))
            self.assertEqual(provider.rectangle(node), (-40, 0, 100, 80))
            provider.close()
        self.assertEqual(model.assert_location_child, (-7) & 0xffffffff)
        self.assertEqual(model.released, [1, 1001, 2])


class IdentityModel:
    def __init__(self, path=r'C:\Program Files\Mozilla Firefox\firefox.exe', name='MozillaWindowClass'):
        self.path, self.name = path, name
        self.user = types.SimpleNamespace(GetForegroundWindow=Mock(return_value=42), IsWindow=Mock(return_value=1),
                                         IsHungAppWindow=Mock(return_value=0), GetClassNameW=Mock(side_effect=self.class_name),
                                         GetWindowThreadProcessId=Mock(side_effect=self.pid))
        self.kernel = types.SimpleNamespace(OpenProcess=Mock(return_value=555), QueryFullProcessImageNameW=Mock(side_effect=self.image),
                                           CloseHandle=Mock(return_value=1))

    def class_name(self, window, buffer, size):
        buffer.value = self.name
        return len(self.name)

    def pid(self, window, pointer):
        pointer._obj.value = 100
        return 10

    def image(self, process, flags, buffer, size):
        buffer.value = self.path
        return 1

    @contextlib.contextmanager
    def installed(self):
        with patch.object(C, 'WinDLL', side_effect={'user32': self.user, 'kernel32': self.kernel}.__getitem__, create=True):
            yield self


class DocumentIdentityTest(unittest.TestCase):
    def test_exact_firefox_and_zen_processes_require_mozilla_window_class(self):
        for app in ('firefox', 'zen'):
            model = IdentityModel(path='C:\\Apps\\' + app + '.exe')
            with self.subTest(app=app), model.installed():
                self.assertEqual(y._win_document_identity(42, app), (42, 100, 'MozillaWindowClass', model.path.lower()))
            model.kernel.CloseHandle.assert_called_once_with(555)

    def test_unknown_process_prefix_and_non_mozilla_window_are_rejected(self):
        for path, name in ((r'C:\Apps\notfirefox.exe', 'MozillaWindowClass'),
                           (r'C:\Apps\firefox-test.exe', 'MozillaWindowClass'),
                           (r'C:\Apps\firefox.exe', 'MozillaWindowClass2')):
            model = IdentityModel(path, name)
            with self.subTest(path=path, name=name), model.installed():
                self.assertIsNone(y._win_document_identity(42, 'firefox'))

    def test_yandex_generic_binary_requires_official_product_path(self):
        for path, expected in ((r'C:\Users\Name\AppData\Local\Yandex\YandexBrowser\Application\browser.exe', True),
                               (r'C:\Program Files\YandexBrowser\Application\browser.exe', True),
                               (r'C:\Another App\browser.exe', False),
                               (r'C:\YandexBrowser2\Application\browser.exe', False),
                               (r'C:\YandexBrowser\Application\notbrowser.exe', False)):
            model = IdentityModel(path, 'Chrome_WidgetWin_1')
            with self.subTest(path=path), model.installed():
                self.assertEqual(bool(y._win_document_identity(42, 'browser')), expected)

    def test_live_pid_lookup_foreground_and_hung_window_gates(self):
        for method, value in (('GetForegroundWindow', 99), ('IsWindow', 0), ('IsHungAppWindow', 1),
                              ('GetWindowThreadProcessId', 0)):
            model = IdentityModel()
            method_mock = getattr(model.user, method)
            method_mock.side_effect, method_mock.return_value = None, value
            with self.subTest(method=method), model.installed():
                self.assertIsNone(y._win_document_identity(42, 'firefox'))
            model.kernel.OpenProcess.assert_not_called()

    def test_foreground_switch_during_identity_read_is_rejected_and_handle_closed(self):
        model = IdentityModel()
        model.user.GetForegroundWindow.side_effect = [42, 99]
        with model.installed():
            self.assertIsNone(y._win_document_identity(42, 'firefox'))
        model.kernel.CloseHandle.assert_called_once_with(555)

    def test_process_access_denied_is_no_url(self):
        model = IdentityModel()
        model.kernel.OpenProcess.return_value = 0
        with model.installed():
            self.assertIsNone(y._win_document_identity(42, 'firefox'))
        model.kernel.QueryFullProcessImageNameW.assert_not_called()

    def test_physical_point_is_bound_to_browser_root(self):
        user = types.SimpleNamespace(GetPhysicalCursorPos=Mock(), WindowFromPhysicalPoint=Mock(return_value=55),
                                     GetAncestor=Mock(return_value=42))
        with patch.object(C, 'WinDLL', return_value=user, create=True):
            self.assertEqual(y._win_pointer_in_window(42, (-200, 150)), (-200, 150))
            user.GetAncestor.return_value = 99
            self.assertIsNone(y._win_pointer_in_window(42, (-200, 150)))
        user.GetPhysicalCursorPos.assert_not_called()


class BrowserDocumentPublicPathTest(unittest.TestCase):
    @contextlib.contextmanager
    def reader(self, result):
        user = types.SimpleNamespace(IsHungAppWindow=Mock(return_value=0))
        with patch.object(C, 'windll', types.SimpleNamespace(user32=user), create=True), \
                patch.object(y, '_windows_read', return_value=result) as read:
            yield read

    def test_firefox_zen_and_yandex_route_through_same_bounded_document_protocol(self):
        cancel = threading.Event()
        for app in ('firefox', 'zen', 'browser'):
            with self.subTest(app=app), self.reader(dict(DOCUMENT, hover_value=LINK, hover_name='A link')) as read:
                self.assertEqual(y.windows_browser_url(42, app, .2, cancel), PAGE)
                self.assertEqual(y.windows_hovered_link(42, app, [-10, 20], .2, cancel), (LINK, 'A link'))
            self.assertEqual([c.args[0] for c in read.call_args_list], ['browser_document', 'browser_document'])
            self.assertEqual(read.call_args.args[1:], ({'hwnd': 42, 'app': app, 'at': [-10, 20]}, .2, cancel))

    def test_unsupported_families_and_unmeasured_arc_are_not_assumed_chromium(self):
        for app in ('arc', 'samsung', 'firefox-beta', 'zen_test', 'notbrowser', 'browser-test'):
            with self.subTest(app=app), self.reader(DOCUMENT) as read:
                self.assertIsNone(y.windows_browser_url(42, app))
                self.assertEqual(y.windows_hovered_link(42, app), (None, None))
            read.assert_not_called()

    def test_error_or_schemeless_helper_output_cannot_become_a_url(self):
        for result in ({}, {'error': 'timeout'}, {'value': 'example.com/', 'hover_value': 'example.com/a'},
                       {'value': 'about:blank'}, {'value': 'http://localhost/', 'hover_value': 'javascript:alert(1)'}):
            with self.subTest(result=result), self.reader(result):
                self.assertIsNone(y.windows_browser_url(42, 'firefox'))
                self.assertEqual(y.windows_hovered_link(42, 'firefox'), (None, None))

    def test_enrichment_replaces_stale_urls_with_one_combined_snapshot(self):
        for app, brand in (('firefox', 'Mozilla Firefox'), ('zen', 'Zen'), ('browser', 'Yandex')):
            context = {'hwnd': 42, 'uygulama': app, 'pencere': 'Current page - ' + brand,
                       'imlec': [-10, 20], 'url': 'https://stale.example/', 'url_imlec': 'https://stale.example/a'}
            with self.subTest(app=app), self.reader(dict(DOCUMENT, hover_value=LINK, hover_name='A link')) as read, \
                    patch.object(y, '_win_foreground', return_value=42), patch.object(y, '_win_title', return_value=context['pencere']):
                result = y.windows_enrich(context)
            self.assertEqual(result['url'], PAGE)
            self.assertEqual(result['url_imlec'], LINK)
            self.assertEqual(result['baslik'], 'Current page')
            self.assertEqual(result['url_yontem'], 'windows-msaa-renderer' if app == 'browser' else 'windows-msaa-gecko')
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0], 'browser_document')

    def test_failed_snapshot_clears_stale_context_fields(self):
        context = {'hwnd': 42, 'uygulama': 'firefox', 'url': PAGE, 'url_imlec': LINK,
                   'baslik': 'Old', 'baslik_imlec': 'Old link', 'url_alinti': LINK + '/quote'}
        with self.reader({'error': 'timeout'}), patch.object(y, '_win_foreground', return_value=42):
            result = y.windows_enrich(context)
        self.assertFalse(any(result.get(k) for k in ('url', 'url_imlec', 'baslik', 'baslik_imlec', 'url_alinti')))

    def test_enrichment_late_window_or_title_switch_discards_results(self):
        context = {'hwnd': 42, 'uygulama': 'firefox', 'pencere': 'Current page - Mozilla Firefox'}
        with self.reader(dict(DOCUMENT, hover_value=LINK)), patch.object(y, '_win_foreground', return_value=42), \
                patch.object(y, '_win_title', return_value='Other tab - Mozilla Firefox'):
            result = y.windows_enrich(context)
        self.assertFalse(any(result.get(k) for k in ('url', 'url_imlec', 'baslik', 'baslik_imlec')))

    def test_yandex_uses_existing_renderer_contract_and_rechecks_identity(self):
        with patch.object(y, '_win_document_identity', side_effect=[(42, 100), (42, 100)]), \
                patch.object(y, '_opera_snapshot', return_value=DOCUMENT) as opera:
            self.assertEqual(y._document_browser_snapshot(42, 'browser', [-10, 20]), DOCUMENT)
        opera.assert_called_once_with(42, [-10, 20])
        with patch.object(y, '_win_document_identity', side_effect=[(42, 100), None]), \
                patch.object(y, '_opera_snapshot', return_value=DOCUMENT):
            self.assertFalse(y._document_browser_snapshot(42, 'browser', [-10, 20]))

    def run_reader(self, request):
        stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode('utf-8')))
        stdout = types.SimpleNamespace(buffer=io.BytesIO())
        with patch.object(y.sys, 'stdin', stdin), patch.object(y.sys, 'stdout', stdout), \
                patch.object(y.os, 'name', 'nt'), patch.object(y, '_document_browser_snapshot', return_value=DOCUMENT) as snapshot:
            self.assertEqual(y._windows_reader_main('browser_document'), 0)
        return y._parse_read_packet(stdout.buffer.getvalue()), snapshot

    def test_protocol_rejects_invalid_hwnd_app_and_coordinates_before_native_calls(self):
        for request in ({'hwnd': True, 'app': 'firefox'}, {'hwnd': 0, 'app': 'firefox'},
                        {'hwnd': 42, 'app': 'arc'}, {'hwnd': 42, 'app': ['firefox']}, {'hwnd': 42},
                        {'hwnd': 42, 'app': 'firefox', 'at': [True, 1]},
                        {'hwnd': 42, 'app': 'firefox', 'at': [0, 2 ** 31]},
                        {'hwnd': 42, 'app': 'firefox', 'at': [-2 ** 31 - 1, 0]},
                        {'hwnd': 42, 'app': 'firefox', 'at': '0,1'}):
            with self.subTest(request=request):
                result, snapshot = self.run_reader(request)
                self.assertEqual(result, {'error': 'ValueError'})
                snapshot.assert_not_called()

    def test_protocol_preserves_negative_physical_point(self):
        result, snapshot = self.run_reader({'hwnd': 42, 'app': 'zen', 'at': [-10, 20]})
        self.assertEqual(result, DOCUMENT)
        snapshot.assert_called_once_with(42, 'zen', [-10, 20])

    def test_cancel_and_deadline_reach_existing_process_packet_runner(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(y, '_windows_python', return_value='pythonw'), patch.object(y, '_process_packet', return_value={}) as packet:
            self.assertFalse(y._windows_document_snapshot(42, 'firefox', [-10, 20], .05, cancel))
        self.assertEqual(packet.call_args.args[0][-2:], ['_win-read', 'browser_document'])
        self.assertEqual(packet.call_args.args[1:], ({'hwnd': 42, 'app': 'firefox', 'at': [-10, 20]}, .05, cancel))


class ChromiumCommittedURLTest(unittest.TestCase):
    def setUp(self):
        foreground = patch.object(y, '_win_foreground', return_value=42)
        self.foreground = foreground.start()
        self.addCleanup(foreground.stop)

    def test_explicit_http_and_https_values_are_preserved_without_renderer_work(self):
        for value in ('http://example.com/page', 'https://example.com/page'):
            with self.subTest(value=value), patch.object(y, '_uia_address_bar', return_value=value) as address, \
                    patch.object(y, '_opera_snapshot') as renderer:
                self.assertEqual(y._chromium_committed_url(42, 'OmniboxViewViews'), value)
            address.assert_called_once_with(42, 'OmniboxViewViews')
            renderer.assert_not_called()

    def test_unreadable_or_focused_omnibox_never_allows_renderer_fallback(self):
        for value in (None, ''):
            with self.subTest(value=value), patch.object(y, '_uia_address_bar', return_value=value), \
                    patch.object(y, '_opera_snapshot') as renderer:
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))
            renderer.assert_not_called()

    def test_invalid_or_internal_omnibox_is_not_replaced_with_document(self):
        for value in ('search words', 'chrome://settings', 'file:///private', 'about:blank',
                      'javascript:example.com', 'https://user:pass@example.com/', 'http://127.0.0.1/',
                      'example.com:bad/path', 'example.com:0/path', 'user:pass@example.com', r'example.com\path'):
            with self.subTest(value=value), patch.object(y, '_uia_address_bar', return_value=value), \
                    patch.object(y, '_opera_snapshot') as renderer:
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))
            renderer.assert_not_called()

    def test_missing_scheme_uses_provider_protocol_without_upgrading_http(self):
        for value, url in (('example.com/current', PAGE), ('example.com', 'http://example.com/'),
                           ('example.com:8080/a', 'http://example.com:8080/a'),
                           ('example.com/current', 'https://example.com/current')):
            with self.subTest(value=value, url=url), patch.object(y, '_uia_address_bar', return_value=value) as address, \
                    patch.object(y, '_opera_snapshot', return_value={'value': url}) as renderer:
                self.assertEqual(y._chromium_committed_url(42, 'OmniboxViewViews'), url)
            self.assertEqual(address.call_count, 2)
            renderer.assert_called_once_with(42)

    def test_conflicting_document_or_unmeasured_formatting_is_rejected(self):
        for url in ('https://other.example/current', 'http://www.example.com/current',
                    'http://example.com/current?new=1', 'http://example.com/different'):
            with self.subTest(url=url), patch.object(y, '_uia_address_bar', return_value='example.com/current'), \
                    patch.object(y, '_opera_snapshot', return_value={'value': url}):
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))

    def test_no_renderer_or_ambiguous_document_yields_no_inferred_url(self):
        for result in ({}, {'error': 'timeout'}, {'value': 'example.com/current'}, {'value': 'http://127.0.0.1/'}):
            with self.subTest(result=result), patch.object(y, '_uia_address_bar', return_value='example.com/current'), \
                    patch.object(y, '_opera_snapshot', return_value=result):
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))

    def test_focus_or_address_change_after_document_read_discards_url(self):
        for second in (None, 'example.com/other'):
            with self.subTest(second=second), patch.object(y, '_uia_address_bar', side_effect=['example.com/current', second]), \
                    patch.object(y, '_opera_snapshot', return_value=DOCUMENT):
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))

    def test_non_foreground_window_does_not_read_an_address(self):
        self.foreground.return_value = 0
        with patch.object(y, '_uia_address_bar') as address, patch.object(y, '_opera_snapshot') as renderer:
            self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))
        address.assert_not_called()
        renderer.assert_not_called()

    def test_foreground_change_during_last_uia_read_discards_explicit_and_resolved_urls(self):
        for value in (PAGE, 'example.com/current'):
            self.foreground.side_effect = [42, 0]
            with self.subTest(value=value), patch.object(y, '_uia_address_bar', return_value=value), \
                    patch.object(y, '_opera_snapshot', return_value=DOCUMENT):
                self.assertIsNone(y._chromium_committed_url(42, 'OmniboxViewViews'))

    def test_uia_protocol_runs_combined_reader_inside_original_child(self):
        request = {'hwnd': 42, 'class': 'OmniboxViewViews'}
        stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode('utf-8')))
        stdout = types.SimpleNamespace(buffer=io.BytesIO())
        with patch.object(y.sys, 'stdin', stdin), patch.object(y.sys, 'stdout', stdout), \
                patch.object(y.os, 'name', 'nt'), patch.object(y, '_chromium_committed_url', return_value=PAGE) as snapshot:
            self.assertEqual(y._windows_reader_main('uia'), 0)
        self.assertEqual(y._parse_read_packet(stdout.buffer.getvalue()), {'value': PAGE})
        snapshot.assert_called_once_with(42, 'OmniboxViewViews')

    def test_chromium_page_and_hover_outputs_must_always_include_protocol(self):
        user = types.SimpleNamespace(IsHungAppWindow=Mock(return_value=0))
        with patch.object(C, 'windll', types.SimpleNamespace(user32=user), create=True), \
                patch.object(y, '_windows_read', return_value={'value': 'example.com/current', 'name': 'Page'}):
            self.assertIsNone(y.windows_browser_url(42, 'chrome'))
            self.assertEqual(y.windows_hovered_link(42, 'chrome'), (None, None))

    def test_existing_child_budget_and_cancel_still_apply_to_all_chromium_work(self):
        cancel = threading.Event()
        user = types.SimpleNamespace(IsHungAppWindow=Mock(return_value=0))
        with patch.object(C, 'windll', types.SimpleNamespace(user32=user), create=True), \
                patch.object(y, '_windows_python', return_value='pythonw'), patch.object(y, '_process_packet', return_value={}) as packet:
            self.assertIsNone(y.windows_browser_url(42, 'chrome', .17, cancel))
        packet.assert_called_once()
        self.assertEqual(packet.call_args.args[0][-2:], ['_win-read', 'uia'])
        self.assertEqual(packet.call_args.args[1:], ({'hwnd': 42, 'class': 'OmniboxViewViews'}, .17, cancel))


if __name__ == '__main__':
    unittest.main()
