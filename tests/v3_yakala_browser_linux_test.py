"""Linux browser capture: synthetic X11/AT-SPI providers; never access a real desktop."""
import ctypes as C
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y  # noqa: E402

PAGE = 'https://example.com/page'
LINK = 'https://example.com/target'
FRAME_RECT = (10, 20, 800, 600)
DOC_RECT = (10, 100, 800, 520)
POINT = (70, 150)
IDENTITY = (210, '/opt/firefox/firefox', 12345, 1, 90)
FRONT = (52, IDENTITY, FRAME_RECT)


def node(role=39, children=(), parent=None, **options):
    result = {'role': role, 'children': list(children), 'parent': parent, 'pid': 210,
              'states': {25, 30}, 'name': '', 'rect': DOC_RECT, 'hit': None}
    result.update(options)
    return result


class FakeAtspi:
    desktop = 1

    def __init__(self):
        self.nodes = {
            1: node(14, [2]),
            2: node(75, [3], 1),
            3: node(23, [4], 2, states={1, 25, 30}, rect=FRAME_RECT, hit=4),
            4: node(95, [5], 3, document=True, uri=PAGE, name='Example', hit=5),
            5: node(88, [6], 4, link=LINK, name='Target', hit=6, rect=(40, 120, 120, 50)),
            6: node(29, [], 5, rect=(40, 120, 120, 50)),
        }
        self.closed, self.reads = False, []

    def children(self, value, limit=128):
        self.reads.append(('children', value))
        result = self.nodes[value]['children']
        if len(result) > limit:
            raise ValueError('limit')
        return result

    def pid(self, value):
        return self.nodes[value]['pid']

    def uncache(self, value):
        self.reads.append(('uncache', value))

    def role(self, value):
        return self.nodes[value]['role']

    def states(self, value):
        return self.nodes[value]['states']

    def name(self, value):
        self.reads.append(('name', value))
        return self.nodes[value]['name']

    def document(self, value):
        return value if self.nodes[value].get('document') else None

    def uri(self, value, attribute):
        self.reads.append(('uri', value, attribute))
        return self.nodes[value]['uri']

    def rect(self, value):
        return self.nodes[value]['rect']

    def hit(self, value, point):
        return self.nodes[value]['hit']

    def parent(self, value):
        return self.nodes[value]['parent']

    def link(self, value):
        return self.nodes[value].get('link')

    def close(self):
        self.closed = True


class FakeX11:
    def __init__(self):
        self.closed = False
        self.foreground = Mock(return_value=FRONT)
        self.pointer = Mock(return_value=POINT)

    def close(self):
        self.closed = True


class LinuxBrowserSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.api, self.x11 = FakeAtspi(), FakeX11()

    def read(self):
        with patch.object(y, '_linux_x11_session', return_value=True), \
                patch.object(y, '_LinuxAtspi', return_value=self.api), \
                patch.object(y, '_LinuxX11', return_value=self.x11):
            return y._linux_browser_snapshot()

    def test_committed_page_and_point_hit_link(self):
        self.assertEqual(self.read(), {'uygulama': 'Mozilla Firefox', 'url': PAGE, 'baslik': 'Example',
                                      'url_yontem': 'linux-atspi-x11', 'url_imlec': LINK, 'baslik_imlec': 'Target'})
        self.assertEqual([entry for entry in self.api.reads if entry[0] == 'uri'],
                         [('uri', 4, 'DocURL'), ('uri', 4, 'DocURL')])
        self.assertEqual([entry for entry in self.api.reads if entry[0] == 'uncache'],
                         [('uncache', 2), ('uncache', 2)])
        self.assertTrue(self.api.closed)
        self.assertTrue(self.x11.closed)

    def test_chromium_document_attribute_is_uri_not_docurl(self):
        front = (52, (210, '/opt/google/chrome/chrome', 12345, 1, 90), FRAME_RECT)
        self.x11.foreground.return_value = front
        self.assertEqual(self.read()['uygulama'], 'Google Chrome')
        self.assertEqual([entry[-1] for entry in self.api.reads if entry[0] == 'uri'], ['URI', 'URI'])

    def test_second_visible_pane_is_ambiguous_even_if_url_invalid(self):
        for uri in (PAGE, 'about:blank', '', 'http://127.0.0.1/'):
            with self.subTest(uri=uri):
                self.api = FakeAtspi()
                self.api.nodes[3]['children'].append(7)
                self.api.nodes[7] = node(95, [], 3, document=True, uri=uri)
                self.assertEqual(self.read(), {})

    def test_hidden_tab_is_not_read(self):
        self.api.nodes[3]['children'].append(7)
        self.api.nodes[7] = node(95, [8], 3, document=True, uri='https://private.example/hidden', states={30})
        self.assertEqual(self.read()['url'], PAGE)
        self.assertFalse(any(entry[1] == 7 for entry in self.api.reads if entry[0] in ('name', 'uri', 'children')))

    def test_top_document_never_substitutes_iframe(self):
        self.api.nodes[4]['children'].append(7)
        self.api.nodes[7] = node(95, [], 4, document=True, uri='https://iframe.example/')
        self.assertEqual(self.read()['url'], PAGE)
        self.assertNotIn(('children', 4), self.api.reads)

    def test_invalid_top_document_cannot_fall_through_to_iframe(self):
        self.api.nodes[4]['uri'] = 'about:blank'
        self.api.nodes[7] = node(95, [], 4, document=True, uri=PAGE)
        self.api.nodes[4]['children'] = [7]
        self.assertEqual(self.read(), {})

    def test_missing_top_document_interface_cannot_promote_iframe(self):
        self.api.nodes[4]['document'] = False
        self.api.nodes[7] = node(95, [], 4, document=True, uri=PAGE)
        self.api.nodes[4]['children'] = [7]
        self.assertEqual(self.read(), {})
        self.assertNotIn(('children', 4), self.api.reads)

    def test_second_visible_document_without_interface_is_ambiguous(self):
        self.api.nodes[3]['children'].append(7)
        self.api.nodes[7] = node(95, [], 3, document=False)
        self.assertEqual(self.read(), {})

    def test_second_busy_or_stale_pane_is_not_filtered_out(self):
        for states in ({25, 30, 3}, {25, 30, 27}, {6}):
            self.api = FakeAtspi()
            self.api.nodes[3]['children'].append(7)
            self.api.nodes[7] = node(95, [], 3, document=True, uri=PAGE, states=states)
            self.assertEqual(self.read(), {})

    def test_draft_entry_is_not_a_document(self):
        self.api.nodes[4].update(role=79, uri='https://draft.example/', document=False)
        self.assertEqual(self.read(), {})
        self.assertFalse(any(entry[0] == 'uri' for entry in self.api.reads))

    def test_foreground_switch_before_or_after_read_is_empty(self):
        other = (53, IDENTITY, FRAME_RECT)
        for answers in ([FRONT, other], [FRONT, FRONT, other]):
            with self.subTest(answers=answers):
                self.x11.foreground.side_effect = answers
                self.assertEqual(self.read(), {})

    def test_pid_reuse_and_window_geometry_change_are_empty(self):
        for changed in ((52, (210, '/opt/firefox/firefox', 99999, 1, 90), FRAME_RECT),
                        (52, IDENTITY, (10, 20, 700, 600))):
            self.x11.foreground.side_effect = [FRONT, FRONT, changed]
            self.assertEqual(self.read(), {})

    def test_document_navigation_same_object_same_title_is_empty(self):
        original = self.api.uri
        count = 0

        def uri(value, attribute):
            nonlocal count
            count += 1
            return original(value, attribute) if count == 1 else 'https://example.com/next'

        self.api.uri = uri
        self.assertEqual(self.read(), {})

    def test_hover_href_changes_keeps_page_only(self):
        self.api.link = Mock(side_effect=[LINK, 'https://example.com/changed'])
        result = self.read()
        self.assertEqual(result['url'], PAGE)
        self.assertNotIn('url_imlec', result)

    def test_pointer_moves_or_is_over_another_window_keeps_page_only(self):
        self.x11.pointer.side_effect = [POINT, (71, 150)]
        self.assertNotIn('url_imlec', self.read())
        self.x11.pointer.side_effect = None
        self.x11.pointer.return_value = None
        with patch.object(y, '_linux_link_at_point') as hit:
            self.assertEqual(self.read()['url'], PAGE)
            hit.assert_not_called()

    def test_hit_outside_document_ancestry_is_not_a_link(self):
        self.api.nodes[5]['parent'] = 2
        self.assertNotIn('url_imlec', self.read())

    def test_browser_overlay_cannot_expose_a_link_underneath(self):
        self.api.nodes[3]['hit'] = 7
        self.api.nodes[7] = node(39, [], 3, name='Overlay')
        result = self.read()
        self.assertEqual(result['url'], PAGE)
        self.assertNotIn('url_imlec', result)

    def test_hit_cycle_and_parent_cycle_are_not_links(self):
        self.api.nodes[6]['hit'] = 5
        self.assertNotIn('url_imlec', self.read())
        self.api.nodes[6]['hit'] = None
        self.api.nodes[5]['parent'] = 6
        self.assertNotIn('url_imlec', self.read())

    def test_partial_rectangle_or_invalid_link_is_not_a_link(self):
        for target in ('javascript:alert(1)', '//example.com/', 'example.com', 'https://example.com/a b',
                       'file:///tmp/foo', 'http://localhost/', 'https://user:pass@example.com/'):
            self.api.nodes[5]['link'] = target
            self.assertNotIn('url_imlec', self.read())
        self.api.nodes[5]['link'] = LINK
        self.api.nodes[5]['rect'] = (70, 150, 0, 50)
        self.assertNotIn('url_imlec', self.read())

    def test_non_link_hyperlink_interface_is_not_accepted(self):
        self.api.nodes[5]['role'] = 27  # Image may implement embedded Hyperlink, but is not a LINK.
        self.assertNotIn('url_imlec', self.read())

    def test_busy_defunct_stale_minimized_or_unshown_page_is_empty(self):
        for states in ({25, 30, 3}, {25, 30, 6}, {25, 30, 15}, {25, 30, 27}, {25}, {30}):
            self.api.nodes[4]['states'] = states
            self.assertEqual(self.read(), {})

    def test_missing_foreground_does_not_start_atspi(self):
        self.x11.foreground.return_value = None
        with patch.object(y, '_linux_x11_session', return_value=True), \
                patch.object(y, '_LinuxX11', return_value=self.x11), patch.object(y, '_LinuxAtspi') as atspi:
            self.assertEqual(y._linux_browser_snapshot(), {})
            atspi.assert_not_called()

    def test_missing_atspi_library_closes_x11(self):
        with patch.object(y, '_linux_x11_session', return_value=True), \
                patch.object(y, '_LinuxX11', return_value=self.x11), \
                patch.object(y, '_LinuxAtspi', side_effect=OSError('missing')):
            with self.assertRaises(OSError):
                y._linux_browser_snapshot()
        self.assertTrue(self.x11.closed)


class LinuxBrowserBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAtspi()

    def test_other_app_only_identity_is_read(self):
        self.api.nodes[1]['children'].append(10)
        self.api.nodes[10] = node(75, [11], 1, pid=999, name='Secret Editor')
        self.assertEqual(y._linux_active_frame(self.api, 210, FRAME_RECT), 3)
        self.assertNotIn(('children', 10), self.api.reads)
        self.assertNotIn(('name', 10), self.api.reads)

    def test_duplicate_pid_apps_are_ambiguous(self):
        self.api.nodes[1]['children'].append(10)
        self.api.nodes[10] = node(75, [3], 1, pid=210)
        self.assertIsNone(y._linux_active_frame(self.api, 210, FRAME_RECT))

    def test_multiple_active_windows_or_dialog_are_ambiguous(self):
        self.api.nodes[2]['children'].append(10)
        self.api.nodes[10] = node(23, [], 2, states={1, 25, 30})
        self.assertIsNone(y._linux_active_frame(self.api, 210, FRAME_RECT))
        self.api.nodes[2]['children'] = [10]
        self.api.nodes[10]['role'] = 16
        self.assertIsNone(y._linux_active_frame(self.api, 210, FRAME_RECT))

    def test_no_geometry_or_scale_guess(self):
        self.assertIsNone(y._linux_active_frame(self.api, 210, (0, 0, 1600, 1200)))

    def test_document_walk_cycle_or_depth_limit_fails_closed(self):
        self.api.nodes[3]['children'] = [3]
        self.assertIsNone(y._linux_document(self.api, 3, 'DocURL'))
        self.api = FakeAtspi()
        self.api.nodes[3]['children'] = [20]
        for value in range(20, 40):
            self.api.nodes[value] = node(39, [value + 1])
        self.api.nodes[40] = node(95, [], document=True, uri=PAGE)
        self.assertIsNone(y._linux_document(self.api, 3, 'DocURL'))

    def test_child_limit_is_not_a_partial_success(self):
        self.api.nodes[3]['children'] = [4] * 129
        with self.assertRaises(ValueError):
            y._linux_document(self.api, 3, 'DocURL')

    def test_session_requires_explicit_local_x11(self):
        for environment, expected in [({'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11'}, True),
                                      ({'DISPLAY': ':0.1', 'XDG_SESSION_TYPE': 'x11'}, True),
                                      ({'DISPLAY': ':0'}, False),
                                      ({'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'wayland'}, False),
                                      ({'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11', 'WAYLAND_DISPLAY': 'wayland-0'}, False),
                                      ({'DISPLAY': 'host:0', 'XDG_SESSION_TYPE': 'x11'}, False),
                                      ({'DISPLAY': 'localhost:10.0', 'XDG_SESSION_TYPE': 'x11'}, False),
                                      ({'XDG_SESSION_TYPE': 'x11'}, False)]:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch.object(y.sys, 'platform', 'linux'):
                self.assertEqual(y._linux_x11_session(), expected)

    def test_process_identity_reads_only_exe_and_proc_stat(self):
        record = '210 (fire fox) ' + ' '.join(['S'] + ['0'] * 18 + ['12345'] + ['0'] * 5)
        with patch.object(y.os, 'readlink', return_value='/opt/firefox/firefox') as readlink, \
                patch.object(y.os, 'stat', return_value=SimpleNamespace(st_dev=1, st_ino=90)) as stat, \
                patch('builtins.open', mock_open(read_data=record)) as opened:
            self.assertEqual(y._linux_process_identity(210), IDENTITY)
        readlink.assert_called_once_with('/proc/210/exe')
        stat.assert_called_once_with('/proc/210/exe')
        opened.assert_called_once_with('/proc/210/stat', encoding='ascii')

    def test_spoofed_name_and_invalid_pid_cannot_select_browser(self):
        with patch.object(y.os, 'readlink', return_value='/usr/bin/terminal'), patch('builtins.open') as opened:
            self.assertIsNone(y._linux_process_identity(210))
            opened.assert_not_called()
        for value in (True, 0, -1, '210', None, 2 ** 31):
            with patch.object(y.os, 'readlink') as readlink:
                self.assertIsNone(y._linux_process_identity(value))
                readlink.assert_not_called()


class LinuxReaderProtocolTest(unittest.TestCase):
    value = {'uygulama': 'Mozilla Firefox', 'url': PAGE, 'url_yontem': 'linux-atspi-x11',
             'url_imlec': LINK, 'baslik': '  Test\n title ', 'baslik_imlec': 'Target'}

    def test_public_reader_uses_timed_child_and_fixed_argv(self):
        with patch.object(y, '_linux_x11_session', return_value=True), \
                patch.object(y, '_process_packet', return_value=self.value) as packet:
            result = y.linux_browser_context(timeout=20)
        self.assertEqual(result['url'], PAGE)
        self.assertEqual(result['url_imlec'], LINK)
        self.assertEqual(result['baslik'], 'Test title')
        packet.assert_called_once_with([sys.executable, str(Path(y.__file__).resolve()), '_linux-read', 'browser'],
                                       {}, y.LINUX_BROWSER_TIMEOUT, None)

    def test_unavailable_wayland_never_starts_child(self):
        with patch.object(y, '_linux_x11_session', return_value=False), patch.object(y, '_process_packet') as packet:
            self.assertEqual(y.linux_browser_context(), {})
            self.assertEqual(y._linux_browser_snapshot(), {})
            packet.assert_not_called()

    def test_malformed_or_error_packet_cannot_leak_a_page(self):
        for result in ({}, {'error': 'timeout'}, [], dict(self.value, error='bad'),
                       dict(self.value, url='http://127.0.0.1/'), dict(self.value, uygulama='secret-editor'),
                       dict(self.value, uygulama=[]), dict(self.value, url_yontem='clipboard'),
                       dict(self.value, error='')):
            with patch.object(y, '_linux_x11_session', return_value=True), \
                    patch.object(y, '_process_packet', return_value=result):
                self.assertEqual(y.linux_browser_context(), {})

    def test_invalid_hover_is_omitted_without_its_title(self):
        with patch.object(y, '_linux_x11_session', return_value=True), \
                patch.object(y, '_process_packet', return_value=dict(self.value, url_imlec='//example.com/')):
            result = y.linux_browser_context()
        self.assertNotIn('url_imlec', result)
        self.assertNotIn('baslik_imlec', result)

    def run_endpoint(self, raw, kind='browser', platform='linux', failure=None):
        output = io.BytesIO()
        with patch.object(y.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), \
                patch.object(y.sys, 'stdout', SimpleNamespace(buffer=output)), \
                patch.object(y.sys, 'platform', platform), \
                patch.object(y, '_linux_browser_snapshot', side_effect=failure, return_value=self.value) as snapshot:
            self.assertEqual(y.main(['_linux-read', kind]), 0)
        return y._parse_read_packet(output.getvalue()), snapshot

    def test_endpoint_request_validation(self):
        for raw, kind, platform in [(b'[]', 'browser', 'linux'), (b'{"path":"/private"}', 'browser', 'linux'),
                                    (b'{', 'browser', 'linux'), (b'{}', 'bad-kind', 'linux'),
                                    (b'{}', 'browser', 'darwin'), (b' ' * (y.WIN_MESSAGE_LIMIT + 1), 'browser', 'linux')]:
            result, snapshot = self.run_endpoint(raw, kind, platform)
            self.assertIn('error', result)
            snapshot.assert_not_called()

    def test_endpoint_reports_only_exception_class(self):
        result, _ = self.run_endpoint(b'{}', failure=OSError('private.example/password'))
        self.assertEqual(result, {'error': 'OSError'})

    def test_endpoint_success_packet(self):
        result, _ = self.run_endpoint(b'{}')
        self.assertEqual(result, self.value)

    def test_context_retains_clipboard_fallback_and_copied_text(self):
        for copied in ({'url': 'https://copied.example/'}, {'metin': 'Copied text'}, {}):
            with patch.object(y, 'linux_browser_context', return_value={}), \
                    patch.object(y, '_linux_clipboard_context', return_value=copied):
                self.assertEqual(y.linux_context(), copied)
            with patch.object(y, 'linux_browser_context', return_value=dict(self.value)), \
                    patch.object(y, '_linux_clipboard_context', return_value=copied):
                result = y.linux_context()
            self.assertEqual(result['url'], PAGE)
            if 'metin' in copied:
                self.assertEqual(result['metin'], copied['metin'])

    def test_copied_files_replace_browser_context_including_hover_and_titles(self):
        copied = {'dosyalar': ['/tmp/document.txt', '/tmp/other.pdf']}
        for browser in ({}, self.value):
            with self.subTest(browser=browser), \
                    patch.object(y, 'linux_browser_context', return_value=dict(browser)), \
                    patch.object(y, '_linux_clipboard_context', return_value=copied):
                self.assertEqual(y.linux_context(), copied)


class Function:
    def __init__(self, result=0):
        self.result, self.calls = result, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


class Library:
    def __init__(self, **values):
        self.functions = {name: Function(value) for name, value in values.items()}

    def __getattr__(self, name):
        return self.functions.setdefault(name, Function())


class LinuxNativeAbiTest(unittest.TestCase):
    def atspi(self):
        lib = Library(atspi_get_desktop_count=1, atspi_get_desktop=100)
        glib, gobject = Library(), Library()
        with patch.object(C, 'CDLL', side_effect=[lib, glib, gobject]):
            api = y._LinuxAtspi()
        return api, lib, glib, gobject

    def test_atspi_pointer_returns_and_gerror_double_pointer_are_explicit(self):
        api, lib, glib, gobject = self.atspi()
        self.assertIs(lib.atspi_accessible_get_child_at_index.restype, C.c_void_p)
        self.assertEqual(lib.atspi_document_get_document_attribute_value.argtypes,
                         [C.c_void_p, C.c_char_p, C.POINTER(C.c_void_p)])
        self.assertIs(lib.atspi_component_get_extents.restype, C.c_void_p)
        self.assertEqual(lib.atspi_component_get_accessible_at_point.argtypes,
                         [C.c_void_p, C.c_int, C.c_int, C.c_int, C.POINTER(C.c_void_p)])
        self.assertEqual(lib.atspi_set_timeout.calls, [(180, 180)])
        self.assertEqual(lib.atspi_accessible_set_cache_mask.calls, [(100, 0)])
        self.assertEqual(glib.g_error_free.argtypes, [C.c_void_p])
        self.assertEqual(gobject.g_object_unref.argtypes, [C.c_void_p])
        api.close()
        api.close()
        self.assertEqual(gobject.g_object_unref.calls, [(100,)])
        self.assertEqual(lib.atspi_exit.calls, [()])

    def test_owned_strings_are_bounded_decoded_and_freed_even_on_error(self):
        api, _, glib, _ = self.atspi()
        for raw, limit, expected in [(b'abc\0', 3, 'abc'), (b'\xff\0', 3, None), (b'abcd\0', 3, None)]:
            buffer = C.create_string_buffer(raw)
            pointer = C.addressof(buffer)
            if expected is not None:
                self.assertEqual(api.string(pointer, limit), expected)
            else:
                with self.assertRaises((ValueError, UnicodeError)):
                    api.string(pointer, limit)
            self.assertEqual(glib.g_free.calls[-1], (pointer,))
        api.close()

    def test_gerror_is_freed_and_only_stage_is_raised(self):
        api, lib, glib, _ = self.atspi()

        def fail(node, error):
            C.cast(error, C.POINTER(C.c_void_p))[0] = 1001
            return 0

        lib.atspi_accessible_get_process_id.result = fail
        with self.assertRaisesRegex(ValueError, '^atspi-call$'):
            api.pid(100)
        self.assertEqual(glib.g_error_free.calls[0][0].value, 1001)
        api.close()

    def test_gerror_with_allocated_rect_or_string_frees_both_outputs(self):
        api, lib, glib, _ = self.atspi()
        lib.atspi_accessible_get_component_iface.result = 500

        def fail_with_output(*args):
            C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 1001
            return 1002

        lib.atspi_component_get_extents.result = fail_with_output
        lib.atspi_accessible_get_name.result = fail_with_output
        for read in (api.rect, api.name):
            with self.assertRaisesRegex(ValueError, '^atspi-call$'):
                read(200)
        self.assertEqual(glib.g_free.calls, [(1002,), (1002,)])
        self.assertEqual([entry[0].value for entry in glib.g_error_free.calls], [1001, 1001])
        api.close()

    def test_gerror_with_partial_accessible_output_releases_reference(self):
        api, lib, glib, gobject = self.atspi()

        def fail_with_output(*args):
            C.cast(args[-1], C.POINTER(C.c_void_p))[0] = 1001
            return 1002

        lib.atspi_accessible_get_parent.result = fail_with_output
        with self.assertRaisesRegex(ValueError, '^atspi-call$'):
            api.parent(200)
        self.assertEqual(gobject.g_object_unref.calls, [(1002,)])
        self.assertEqual(glib.g_error_free.calls[0][0].value, 1001)
        api.close()

    def test_multiple_hyperlink_anchors_are_not_guessed(self):
        api, lib, _, _ = self.atspi()
        lib.atspi_accessible_get_hyperlink.result = 501
        for count in (0, 2, 100):
            lib.atspi_hyperlink_get_n_anchors.result = count
            self.assertIsNone(api.link(500))
        self.assertEqual(lib.atspi_hyperlink_get_uri.calls, [])
        api.close()

    def test_invalid_hyperlink_is_not_read(self):
        api, lib, _, _ = self.atspi()
        lib.atspi_accessible_get_hyperlink.result = 501
        lib.atspi_hyperlink_get_n_anchors.result = 1
        lib.atspi_hyperlink_is_valid.result = 0
        self.assertIsNone(api.link(500))
        self.assertEqual(lib.atspi_hyperlink_get_uri.calls, [])
        api.close()

    def test_x11_property_native_unsigned_long_and_xfree(self):
        lib = Library(XOpenDisplay=100, XDefaultRootWindow=1, XInternAtom=200)
        with patch.object(C, 'CDLL', return_value=lib):
            api = y._LinuxX11()
        buffer = (C.c_ulong * 1)(0xFEDCBA98)

        def property_call(*args):
            C.cast(args[7], C.POINTER(C.c_ulong))[0] = 33
            C.cast(args[8], C.POINTER(C.c_int))[0] = 32
            C.cast(args[9], C.POINTER(C.c_ulong))[0] = 1
            C.cast(args[10], C.POINTER(C.c_ulong))[0] = 0
            C.cast(args[11], C.POINTER(C.c_void_p))[0] = C.addressof(buffer)
            return 0

        lib.XGetWindowProperty.result = property_call
        self.assertEqual(api.property(1, b'_NET_ACTIVE_WINDOW', 33), 0xFEDCBA98)
        self.assertEqual(lib.XFree.calls[-1][0].value, C.addressof(buffer))
        self.assertIsNone(api.property(1, b'_NET_WM_PID', 6))  # wrong property type
        self.assertEqual(len(lib.XFree.calls), 2)
        self.assertIs(lib.XDefaultRootWindow.restype, C.c_ulong)
        self.assertEqual(lib.XGetWindowProperty.argtypes[-1], C.POINTER(C.c_void_p))
        api.close()
        api.close()
        self.assertEqual(lib.XCloseDisplay.calls, [(100,)])

    def test_x11_active_property_must_match_real_input_focus_ancestry(self):
        lib = Library(XOpenDisplay=100, XDefaultRootWindow=1)
        with patch.object(C, 'CDLL', return_value=lib):
            api = y._LinuxX11()

        def input_focus(display, output, revert):
            C.cast(output, C.POINTER(C.c_ulong))[0] = 900
            return 1

        lib.XGetInputFocus.result = input_focus
        api.property = Mock(side_effect=[52, 210, 52, 210])
        api.parent = Mock(side_effect=[52, 1])
        api.rect = Mock(return_value=FRAME_RECT)
        with patch.object(y, '_linux_process_identity', return_value=IDENTITY):
            self.assertEqual(api.foreground(), FRONT)
            self.assertIsNone(api.foreground())
        api.rect.assert_called_once_with(52)
        api.close()

    def test_x11_pointer_chain_excludes_covering_windows_and_cycles(self):
        lib = Library(XOpenDisplay=100, XDefaultRootWindow=1)
        with patch.object(C, 'CDLL', return_value=lib):
            api = y._LinuxX11()
        chain = {1: 100, 100: 52, 52: 900, 900: 0}

        def pointer(display, window, root, child, x, z, local_x, local_y, mask):
            C.cast(root, C.POINTER(C.c_ulong))[0] = 1
            C.cast(child, C.POINTER(C.c_ulong))[0] = chain[window]
            C.cast(x, C.POINTER(C.c_int))[0] = POINT[0]
            C.cast(z, C.POINTER(C.c_int))[0] = POINT[1]
            return 1

        lib.XQueryPointer.result = pointer
        self.assertEqual(api.pointer(52), POINT)
        self.assertIsNone(api.pointer(99))
        chain[52] = 100
        self.assertIsNone(api.pointer(52))
        api.close()


if __name__ == '__main__':
    unittest.main()
