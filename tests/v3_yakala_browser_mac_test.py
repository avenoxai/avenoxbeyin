"""macOS browser readers: contract fixtures only, no desktop, browser or AX permission needed.

Fake AX objects exercise provenance and ambiguous trees. They do not claim browser-device
coverage; the ctypes ABI, OS permissions and real vendor trees need the documented smoke test.
"""
import copy
import ctypes as C
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y  # noqa: E402


FRONT = {'bundle': 'org.mozilla.firefox', 'uygulama': 'Firefox', 'pid': 41,
         'mac_window': 77, 'mac_bounds': [0, 0, 800, 600], 'imlec': [40, 125]}
PAGE = 'https://example.com/page?x=1#part'
LINK = 'https://example.com/link'


class FakeAX:
    """Read-only graph with distinct object identities, provider values and screen rectangles."""
    def __init__(self):
        self.trusted, self.app, self.closed, self.hit_value = True, 'app', False, 'text'
        self.graph = {
            'app': {'AXFocusedWindow': 'window'},
            'window': {'AXRole': 'AXWindow', 'children': ['toolbar', 'web'], 'bounds': [0, 0, 800, 600], 'pid': 41},
            'toolbar': {'AXRole': 'AXTextField', 'AXValue': 'https://draft.example/', 'children': []},
            'web': {'AXRole': 'AXWebArea', 'AXURL': PAGE, 'AXTitle': 'Page', 'AXParent': 'window',
                    'children': ['link', 'frame'], 'bounds': [0, 100, 800, 500], 'AXLoaded': True},
            'link': {'AXRole': 'AXLink', 'AXURL': LINK, 'AXTitle': 'Read link', 'AXParent': 'web',
                     'children': ['text'], 'bounds': [20, 120, 160, 30]},
            'text': {'AXRole': 'AXStaticText', 'AXParent': 'link'},
            'frame': {'AXRole': 'AXWebArea', 'AXURL': 'https://example.net/frame', 'AXParent': 'web',
                      'bounds': [200, 100, 300, 200]},
        }
        self.sequences = {}

    def value(self, element, name, default=None):
        sequence = self.sequences.get((element, name))
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.graph.get(element, {}).get(name, default)

    def text(self, element, name):
        return self.value(element, name)

    def flag(self, element, name):
        return self.value(element, name)

    def number(self, element, name):
        return self.value(element, name)

    def element(self, element, name):
        return self.value(element, name)

    def children(self, element):
        return self.value(element, 'children', [])

    def bounds(self, element):
        return self.value(element, 'bounds')

    def pid(self, element):
        return self.value(element, 'pid')

    def equal(self, first, second):
        return bool(first and second and first == second)

    def hit(self, at):
        return self.hit_value

    def has_sheets(self, window):
        return self.value(window, 'sheets', False)

    def close(self):
        self.closed = True


class MacAXProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.ax = FakeAX()
        self.front = copy.deepcopy(FRONT)

    def snapshot(self, fronts=None):
        with patch.object(y, '_MacAX', return_value=self.ax), \
                patch.object(y, 'mac_frontmost', side_effect=fronts, return_value=self.front):
            result = y._mac_ax_snapshot(self.front)
        self.assertTrue(self.ax.closed)
        return result

    def test_committed_document_and_exact_pointer_link(self):
        result = self.snapshot()
        self.assertEqual(result['url'], PAGE)
        self.assertEqual(result['url_imlec'], LINK)
        self.assertEqual(result['url_yontem'], 'mac-ax')
        self.assertNotIn('draft', result['url'])

    def test_browser_names_are_not_used_to_guess_ax_roles(self):
        for bundle in y.MAC_AX_BROWSERS:
            with self.subTest(bundle=bundle):
                self.front['bundle'] = bundle
                self.assertEqual(self.snapshot()['url'], PAGE)

    def test_nested_frame_is_not_a_second_top_level_document(self):
        self.assertEqual(self.snapshot()['url'], PAGE)
        self.ax.graph['link']['AXParent'] = 'frame'
        self.assertEqual(self.snapshot()['url_imlec'], LINK)

    def test_split_view_even_equal_or_missing_urls_is_ambiguous(self):
        for other_url in (PAGE, '', 'about:blank', None, 'https://elsewhere.example/'):
            with self.subTest(other_url=other_url):
                self.ax.graph['web2'] = dict(self.ax.graph['web'], AXURL=other_url)
                self.ax.graph['window']['children'] = ['toolbar', 'web', 'web2']
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_explicitly_hidden_document_is_not_selected(self):
        self.ax.graph['web2'] = dict(self.ax.graph['web'], AXHidden=True, AXURL='https://hidden.example/')
        self.ax.graph['window']['children'].append('web2')
        self.assertEqual(self.snapshot()['url'], PAGE)

    def test_unloaded_or_invalid_document_cannot_fallback(self):
        for bad_url in ('', 'about:blank', 'https://example.com/\n', 'https://u:p@example.com/'):
            self.ax.graph['web']['AXURL'] = bad_url
            self.assertEqual(self.snapshot(), {'status': 'ambiguous'})
        self.ax.graph['web']['AXURL'] = PAGE
        self.ax.graph['web']['AXLoaded'] = False
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_loaded_true_cannot_override_incomplete_or_invalid_progress(self):
        for progress in (0, 0.5, -1, 1.01, float('nan'), float('inf'), float('-inf'), True, '1'):
            with self.subTest(progress=progress):
                self.ax.graph['web']['AXLoadingProgress'] = progress
                self.assertTrue(self.ax.graph['web']['AXLoaded'])
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_optional_progress_preserves_the_loaded_guard(self):
        for progress in (None, 1, 1.0):
            with self.subTest(progress=progress):
                self.ax.graph['web']['AXLoadingProgress'] = progress
                self.ax.graph['web']['AXLoaded'] = True
                self.assertEqual(self.snapshot()['url'], PAGE)
                self.ax.graph['web']['AXLoaded'] = False
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_readiness_change_between_document_reads_rejects_page_and_hover(self):
        for progress in ([1.0, 0.5], [1.0, 0.0], [1.0, None], [None, 1.0]):
            with self.subTest(progress=progress):
                self.ax.sequences[('web', 'AXLoadingProgress')] = progress.copy()
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_document_navigation_or_replacement_between_reads(self):
        self.ax.sequences[('web', 'AXURL')] = [PAGE, 'https://example.com/new']
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})
        self.ax.sequences.clear()
        self.ax.graph['web2'] = dict(self.ax.graph['web'])
        self.ax.sequences[('window', 'children')] = [['web'], ['web2']]
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_foreground_pid_window_or_window_bounds_change(self):
        for key, value in (('pid', 42), ('mac_window', 78), ('mac_bounds', [10, 0, 800, 600]),
                           ('bundle', 'com.apple.finder')):
            changed = dict(self.front, **{key: value})
            self.assertEqual(self.snapshot([self.front, changed]), {'status': 'ambiguous'})

    def test_stale_focused_window_and_dialog_are_rejected(self):
        for change in ({'pid': 88}, {'AXMinimized': True}, {'sheets': True}, {'bounds': [10, 0, 800, 600]},
                       {'AXRole': 'AXDialog'}):
            original = self.ax.graph['window'].copy()
            self.ax.graph['window'].update(change)
            self.assertEqual(self.snapshot(), {'status': 'ambiguous'})
            self.ax.graph['window'] = original
        self.ax.sequences[('app', 'AXFocusedWindow')] = ['window', 'other-window']
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_pointer_movement_drops_hover_and_retains_page(self):
        result = self.snapshot([self.front, dict(self.front, imlec=[41, 125])])
        self.assertEqual(result['url'], PAGE)
        self.assertNotIn('url_imlec', result)

    def test_point_must_hit_link_in_selected_document(self):
        for hit in ('toolbar', 'frame', 'window', None):
            self.ax.hit_value = hit
            result = self.snapshot()
            self.assertEqual(result['url'], PAGE)
            self.assertNotIn('url_imlec', result)

    def test_link_bounds_url_and_ancestry_are_all_required(self):
        original = self.ax.graph['link'].copy()
        for change in ({'AXURL': '/relative'}, {'bounds': [500, 500, 20, 20]}, {'AXParent': 'toolbar'},
                       {'AXHidden': True}, {'AXParent': 'link'}, {'AXRole': 'AXButton'}):
            self.ax.graph['link'] = dict(original, **change)
            result = self.snapshot()
            self.assertEqual(result['url'], PAGE)
            self.assertNotIn('url_imlec', result)

    def test_hover_link_changes_between_reads(self):
        self.ax.sequences[('link', 'AXURL')] = [LINK, 'https://example.com/replaced']
        result = self.snapshot()
        self.assertEqual(result['url'], PAGE)
        self.assertNotIn('url_imlec', result)

    def test_navigation_during_second_hit_invalidates_page_and_hover(self):
        hits = [0]

        def hit(at):
            hits[0] += 1
            if hits[0] == 2:
                self.ax.graph['web']['AXURL'] = 'https://example.com/navigated'
            return 'text'

        self.ax.hit = hit
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_native_hover_failure_keeps_revalidated_page(self):
        self.ax.hit = Mock(side_effect=ValueError('ax-cannot-complete'))
        result = self.snapshot()
        self.assertEqual(result['url'], PAGE)
        self.assertNotIn('url_imlec', result)

    def test_window_state_changes_during_second_hit_are_rejected(self):
        for changes in ({'sheets': True}, {'AXMinimized': True}, {'bounds': [20, 0, 800, 600]},
                        {'pid': 42}, {'AXRole': 'AXDialog'}):
            with self.subTest(changes=changes):
                self.ax = FakeAX()
                hits = [0]

                def hit(at):
                    hits[0] += 1
                    if hits[0] == 2:
                        self.ax.graph['window'].update(changes)
                    return 'text'

                self.ax.hit = hit
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_missing_document_role_does_not_promote_embedded_frame(self):
        for missing in (None, '', 'AXUnknown'):
            with self.subTest(role=missing):
                self.ax.graph['web']['AXRole'] = missing
                self.assertEqual(self.snapshot(), {'status': 'ambiguous'})

    def test_no_permission_returns_unavailable_without_querying_tree(self):
        self.ax.trusted = False
        self.ax.element = Mock(side_effect=AssertionError('AX read without permission'))
        self.assertEqual(self.snapshot(), {'status': 'unavailable'})

    def test_cycles_and_limits_and_unknown_webarea_geometry_fail_closed(self):
        self.ax.graph['toolbar']['children'] = ['toolbar']
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})
        self.ax.graph['toolbar']['children'] = []
        with patch.object(y, 'MAC_AX_NODES', 1):
            self.assertEqual(self.snapshot(), {'status': 'ambiguous'})
        self.ax.graph['web']['bounds'] = None
        self.assertEqual(self.snapshot(), {'status': 'ambiguous'})


class MacScriptAndDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.front = dict(FRONT, bundle='com.apple.Safari')
        self.output = '0,0,800,600\n' + PAGE + '\n' + PAGE + '\nA title\n'

    def context(self, packet=None, fronts=None, script=None, timeout=y.MAC_BROWSER_TIMEOUT):
        with patch.object(y, 'mac_frontmost', side_effect=fronts, return_value=self.front), \
                patch.object(y, '_process_packet', return_value=packet or {'status': 'unavailable'}) as child, \
                patch.object(y, '_osascript', return_value=self.output if script is None else script) as runner:
            result = y.mac_context(self.front, timeout=timeout)
        return result, child, runner

    def test_native_contract_is_available_as_a_killable_child(self):
        result, child, runner = self.context({'status': 'ok', 'url': PAGE, 'url_imlec': LINK, 'baslik': 'Page'})
        self.assertEqual(result['url_imlec'], LINK)
        self.assertEqual(child.call_args.args[0][-2:], ['_mac-read', 'browser'])
        self.assertEqual(child.call_args.args[1]['front']['pid'], 41)
        self.assertLessEqual(child.call_args.args[2], y.MAC_AX_TIMEOUT)
        runner.assert_not_called()

    def test_safari_automation_fallback_does_not_need_ax_permission(self):
        result, _, runner = self.context()
        self.assertEqual(result['url'], PAGE)
        self.assertEqual(result['url_yontem'], 'mac-applescript')
        self.assertTrue(runner.call_args.kwargs['exact'])
        self.assertLessEqual(runner.call_args.kwargs['timeout'], y.MAC_BROWSER_TIMEOUT)

    def test_ax_timeout_allows_independent_bounded_browser_model_read(self):
        self.assertEqual(self.context({'error': 'timeout'})[0]['url'], PAGE)

    def test_explicit_ambiguity_blocks_script(self):
        result, _, runner = self.context({'status': 'ambiguous'})
        self.assertNotIn('url', result)
        runner.assert_not_called()

    def test_unknown_and_ax_only_browsers_do_not_run_generic_script(self):
        for bundle in ('unknown.browser', 'company.thebrowser.Browser', 'org.mozilla.firefox', 'app.zen-browser.zen'):
            self.front['bundle'] = bundle
            result, _, runner = self.context()
            self.assertNotIn('url', result)
            runner.assert_not_called()

    def test_missing_identity_and_expired_budget_do_not_start_readers(self):
        for key in ('pid', 'mac_window'):
            original = self.front.pop(key)
            result, child, runner = self.context()
            self.assertNotIn('url', result)
            child.assert_not_called()
            runner.assert_not_called()
            self.front[key] = original
        result, child, runner = self.context(timeout=0)
        child.assert_not_called()
        runner.assert_not_called()

    def test_changed_foreground_discards_both_page_and_hover(self):
        changed = dict(self.front, mac_window=999)
        result, _, _ = self.context({'status': 'ok', 'url': PAGE, 'url_imlec': LINK}, [self.front, changed])
        self.assertNotIn('url', result)
        self.assertNotIn('url_imlec', result)

    def test_no_stale_inherited_url_on_failure(self):
        self.front.update(url=PAGE, url_imlec=LINK, baslik='old')
        result, _, _ = self.context({'status': 'ambiguous'})
        self.assertNotIn('url', result)
        self.assertNotIn('url_imlec', result)
        self.assertNotIn('baslik', result)

    def test_all_reader_time_is_taken_from_one_deadline(self):
        clock = [0.0]

        def child(*args):
            self.assertEqual(args[2], y.MAC_AX_TIMEOUT)
            clock[0] = 0.9
            return {'status': 'unavailable'}

        def run(*args, **kwargs):
            self.assertAlmostEqual(kwargs['timeout'], 1.6)
            clock[0] = 2.6
            return self.output

        with patch('time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(y, 'mac_frontmost', return_value=self.front), \
                patch.object(y, '_process_packet', side_effect=child), patch.object(y, '_osascript', side_effect=run):
            result = y.mac_context(self.front)
        self.assertNotIn('url', result)

    def test_fixed_script_per_browser_family_and_no_mutating_commands(self):
        for bundle in y.CHROMIUM | y.MAC_SAFARI:
            script = y._mac_browser_script(bundle)
            self.assertIn('if not frontmost then return', script)
            self.assertTrue(script.startswith('if application id "' + bundle + '" is not running'))
            self.assertNotIn('modal of', script)  # absent from the Chromium SDEF
            self.assertIn('if (id of w2) is not wid', script)
            self.assertIn('if u2 is not u', script)
            self.assertNotIn('activate', script)
            self.assertNotIn('JavaScript', script)
            self.assertNotIn('set URL', script)
            self.assertNotIn('clipboard', script)
        self.assertIn('current tab of w', y._mac_browser_script('com.apple.Safari'))
        self.assertIn('index of t', y._mac_browser_script('com.apple.Safari'))
        self.assertIn('active tab of w', y._mac_browser_script('com.google.Chrome'))
        self.assertIn('if loading of t2', y._mac_browser_script('com.google.Chrome'))
        self.assertIsNone(y._mac_browser_script('com.apple.Safari"\nactivate'))

    def test_script_packet_is_bound_to_visible_window_bounds(self):
        self.assertEqual(y._mac_script_result(self.output, self.front)['url'], PAGE)
        for rectangle in ('1,2,801,602', '0,0,80,60', 'nan,0,800,600', '0,0,inf,600', '0,0,800', '-1,-1,-2,-2'):
            self.assertEqual(y._mac_script_result(rectangle + '\n' + PAGE + '\n' + PAGE + '\nTitle', self.front), {})
        self.assertEqual(y._mac_script_result('0,0,800,600\n' + PAGE + '\n' + PAGE, self.front)['baslik'], '')
        self.assertEqual(y._mac_script_result('0,0,800,600\nhttps://example.com/Path\nhttps://example.com/path\nTitle', self.front), {})

    def test_full_exact_urls_only_and_no_credential_or_private_urls(self):
        for good in ('https://example.com/', 'HTTP://EXAMPLE.COM/a?q=x%20y#here', PAGE):
            self.assertEqual(y._mac_url(good), good)
        for bad in ('example.com', '//example.com', '/foo', 'https://example.com/ ', 'https://example.com/\n',
                    'https://u:p@example.com/', 'file:///private/tmp/x', 'http://127.0.0.1/',
                    'http://localhost/', 'https://example.com:99999/', 'https://example.com/\\evil', None):
            self.assertIsNone(y._mac_url(bad), repr(bad))
        self.assertEqual(y._mac_script_result('0,0,800,600\n' + PAGE + ' \n' + PAGE + ' \nTitle', self.front), {})

    def test_os_system_script_fixed_argv_deadline_and_exact_output(self):
        success = subprocess.CompletedProcess([], 0, b' leading\ntrailing \n', b'')
        with patch.object(y.subprocess, 'run', return_value=success) as run:
            self.assertEqual(y._osascript('fixed script', timeout=.2, exact=True), ' leading\ntrailing \n')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/osascript', '-e', 'fixed script'])
            self.assertEqual(run.call_args.kwargs['timeout'], .2)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        with patch.object(y.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['osascript'], .2)):
            self.assertIsNone(y._osascript('fixed', timeout=.2))


class MacNativeFallbackTest(unittest.TestCase):
    def invoke(self, context, answer=b'\n', code=0, error=None):
        result = subprocess.CompletedProcess([], code, answer, b'')
        with patch.object(y.sys, 'platform', 'darwin'), \
                patch.object(y.subprocess, 'run', return_value=result, side_effect=error) as run, \
                patch.object(y, 'capture', return_value={'status': 'yakalandi'}) as save:
            done = y._popup_fallback(Path('/tmp/mac-native-fallback-unused'), context)
        return done, run, save

    def test_cancel_timeout_and_dialog_error_never_capture(self):
        context = {'url': PAGE, 'url_imlec': LINK, 'baslik': 'Page'}
        for code, error in ((1, None), (5, None),
                            (0, subprocess.TimeoutExpired(['/usr/bin/osascript'], 300)),
                            (0, PermissionError('denied')), (0, FileNotFoundError('missing'))):
            with self.subTest(code=code, error=type(error).__name__):
                done, run, save = self.invoke(context, code=code, error=error)
                self.assertEqual(done, {'status': 'vazgecildi'})
                self.assertEqual(run.call_args.kwargs['timeout'], 300)
                save.assert_not_called()

    def test_empty_confirmation_saves_the_displayed_page_not_the_hover(self):
        done, run, save = self.invoke({'url': PAGE, 'url_imlec': LINK,
                                      'baslik': 'Page', 'baslik_imlec': 'Hover'})
        self.assertEqual(done, {'status': 'yakalandi'})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ['/usr/bin/osascript', '-e', y._MAC_FALLBACK_SCRIPT, '--'])
        self.assertEqual(len(argv), 5)
        displayed = argv[4].splitlines()[1]
        self.assertEqual(displayed, PAGE)
        self.assertEqual(save.call_args.kwargs['url'], displayed)
        self.assertEqual(save.call_args.kwargs['title'], 'Page')
        self.assertEqual(save.call_args.kwargs['why'], '')
        self.assertIn('yalniz bu sayfa', argv[4])
        self.assertIn('imlec linki secilemez', argv[4])
        self.assertNotIn(LINK, argv[4])
        self.assertIn('cancel button "Vazgec"', argv[2])
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_script_and_argument_boundaries_are_fixed_for_hostile_data(self):
        quoted_url = 'https://example.com/";error-128?a=$(id)&b=`id`'
        hostile = '--help\x00"\nend run\ndo shell script "id"\n--url'
        for context in ({'url': quoted_url}, {'metin': hostile}):
            with self.subTest(context=context):
                done, run, save = self.invoke(context, answer=b'bir neden\n')
                argv = run.call_args.args[0]
                self.assertEqual(argv[:4], ['/usr/bin/osascript', '-e', y._MAC_FALLBACK_SCRIPT, '--'])
                self.assertEqual(len(argv), 5)
                self.assertNotIn('do shell script', argv[2])
                self.assertNotIn(quoted_url, argv[2])
                self.assertNotIn('\x00', argv[4])
                self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
                self.assertEqual(done, {'status': 'yakalandi'})
                self.assertEqual(save.call_args.kwargs['why'], 'bir neden')
                if 'url' in context:
                    self.assertEqual(argv[4].splitlines()[1], save.call_args.kwargs['url'])
                else:
                    self.assertIn('--help " end run do shell script "id" --url', argv[4])
                    self.assertEqual(save.call_args.kwargs['text'], hostile)

    def test_no_source_empty_confirmation_and_typed_note_remain_distinct(self):
        done, _, save = self.invoke({})
        self.assertEqual(done, {'status': 'vazgecildi'})
        save.assert_not_called()
        done, _, save = self.invoke({}, answer=b'yeni not\n')
        self.assertEqual(done, {'status': 'yakalandi'})
        self.assertEqual(save.call_args.kwargs['text'], 'yeni not')
        self.assertEqual(save.call_args.kwargs['why'], '')

    def test_unsafe_or_changed_display_url_is_never_confirmed(self):
        for url in (' https://example.com/page', 'https://example.com/page\n', 'example.com',
                    'https://example.com/\x00', 'file:///private/tmp/x', 'http://localhost/', 12):
            with self.subTest(url=url):
                done, run, save = self.invoke({'url': url})
                self.assertEqual(done, {'status': 'vazgecildi'})
                run.assert_not_called()
                save.assert_not_called()

    def test_script_argv_data_is_not_parsed_as_options(self):
        result = subprocess.CompletedProcess([], 0, b'\n', b'')
        with patch.object(y.subprocess, 'run', return_value=result) as run:
            self.assertEqual(y._osascript('fixed', args=('--help', '-e', 'end run')), '')
            self.assertEqual(run.call_args.args[0], ['/usr/bin/osascript', '-e', 'fixed', '--', '--help', '-e', 'end run'])
        for args in ('split string', [None], ['nul\x00text']):
            with self.subTest(args=args), patch.object(y.subprocess, 'run') as run:
                self.assertIsNone(y._osascript('fixed', args=args))
                run.assert_not_called()


class MacReaderPacketTest(unittest.TestCase):
    def invoke(self, request, platform='darwin', kind='browser'):
        stdout = type('Output', (), {'buffer': io.BytesIO()})()
        stdin = type('Input', (), {'buffer': io.BytesIO(request)})()
        with patch.object(y.sys, 'platform', platform), patch.object(y.sys, 'stdin', stdin), \
                patch.object(y.sys, 'stdout', stdout), patch.object(y, '_mac_ax_snapshot', return_value={'status': 'ok'}) as read:
            code = y._mac_reader_main(kind)
        self.assertEqual(code, 0)
        return y._parse_read_packet(stdout.buffer.getvalue()), read

    def test_valid_request_uses_existing_framed_protocol(self):
        result, read = self.invoke(json.dumps({'front': FRONT}).encode())
        self.assertEqual(result, {'status': 'ok'})
        read.assert_called_once_with(FRONT)

    def test_platform_kind_and_malformed_requests_never_reach_native_provider(self):
        valid = json.dumps({'front': FRONT}).encode()
        for raw, platform, kind in ((valid, 'linux', 'browser'), (valid, 'darwin', 'other'),
                                    (b'null', 'darwin', 'browser'), (b'[]', 'darwin', 'browser'),
                                    (b'{', 'darwin', 'browser'), (b'x' * (y.WIN_MESSAGE_LIMIT + 1), 'darwin', 'browser')):
            result, read = self.invoke(raw, platform, kind)
            self.assertEqual(result, {'error': 'request-invalid'})
            read.assert_not_called()

    def test_wrong_identifiers_and_coordinates_are_rejected(self):
        for changes in ({'pid': True}, {'pid': 0}, {'mac_window': False}, {'bundle': 'com.apple.finder'},
                        {'imlec': [float('nan'), 1]}, {'imlec': [1, float('inf')]}, {'imlec': [True, 2]},
                        {'mac_bounds': [0, 0, 0, 400]}, {'mac_bounds': [0, 0, 800, float('nan')]}):
            result, read = self.invoke(json.dumps({'front': dict(FRONT, **changes)}).encode())
            self.assertEqual(result, {'error': 'request-invalid'})
            read.assert_not_called()

    def test_main_routes_before_public_cli_parsing(self):
        with patch.object(y, '_mac_reader_main', return_value=0) as reader:
            self.assertEqual(y.main(['_mac-read', 'browser']), 0)
            reader.assert_called_once_with('browser')


class MacNativeBindingTest(unittest.TestCase):
    """ctypes ABI/CF ownership checks with fake libraries, independent of real AX permission."""
    def setUp(self):
        self.native, self.cf = Mock(), Mock()
        self.app, self.system = 0x100000041, 0x100000042
        self.keys, self.types = {}, {self.app: 17, self.system: 17, 200: 11, 201: 12, 202: 13,
                                   203: 14, 204: 19, 205: 19, 206: 15}
        self.strings = {200: PAGE.encode()}
        self.numbers = {206: 1.0}
        self.values = {'AXURL': 201, 'AXRole': 200, 'AXLoaded': 202, 'AXChildren': 203,
                       'AXPosition': 204, 'AXSize': 205, 'AXLoadingProgress': 206}
        self.native.AXIsProcessTrusted.return_value = True
        self.native.AXUIElementCreateApplication.return_value = self.app
        self.native.AXUIElementCreateSystemWide.return_value = self.system
        self.native.AXUIElementGetTypeID.return_value = 17
        self.native.AXValueGetTypeID.return_value = 19
        self.cf.CFStringGetTypeID.return_value = 11
        self.cf.CFURLGetTypeID.return_value = 12
        self.cf.CFBooleanGetTypeID.return_value = 13
        self.cf.CFArrayGetTypeID.return_value = 14
        self.cf.CFNumberGetTypeID.return_value = 15
        self.cf.CFGetTypeID.side_effect = lambda value: self.types[value]
        self.cf.CFURLGetString.return_value = 200
        self.cf.CFBooleanGetValue.return_value = True
        self.cf.CFStringGetLength.side_effect = lambda value: len(self.strings[value])
        self.cf.CFArrayGetCount.return_value = 2
        self.cf.CFArrayGetValueAtIndex.side_effect = lambda array, index: (self.app, self.system)[index]

        def key(allocator, data, encoding):
            value = len(self.keys) + 500
            self.keys[value] = data.decode()
            return value

        def attribute(element, name, out):
            value = self.values.get(self.keys[name])
            if value is None:
                return -25205
            C.cast(out, C.POINTER(C.c_void_p))[0] = value
            return 0

        def string(value, out, capacity, encoding):
            data = self.strings[value] + b'\x00'
            C.memmove(out, data, len(data))
            return True

        def number(value, kind, out):
            C.cast(out, C.POINTER(C.c_double))[0] = self.numbers[value]
            return True

        def count(element, name, out):
            C.cast(out, C.POINTER(C.c_long))[0] = 2
            return 0

        def children(element, name, start, size, out):
            C.cast(out, C.POINTER(C.c_void_p))[0] = 203
            return 0

        def pair(value, kind, out):
            pair = (0.5, -20.5) if kind == 1 else (800.0, 600.0)
            target = C.cast(out, C.POINTER(C.c_double))
            target[0], target[1] = pair
            return True

        self.cf.CFStringCreateWithCString.side_effect = key
        self.cf.CFStringGetCString.side_effect = string
        self.cf.CFNumberGetValue.side_effect = number
        self.native.AXUIElementCopyAttributeValue.side_effect = attribute
        self.native.AXUIElementGetAttributeValueCount.side_effect = count
        self.native.AXUIElementCopyAttributeValues.side_effect = children
        self.native.AXValueGetType.side_effect = lambda value: 1 if value == 204 else 2
        self.native.AXValueGetValue.side_effect = pair
        with patch.object(C, 'CDLL', side_effect=[self.native, self.cf]):
            self.ax = y._MacAX(41)

    def tearDown(self):
        self.ax.close()

    def test_pointer_width_and_float_point_prototypes(self):
        self.assertEqual(self.ax.app, 0x100000041)
        self.assertIs(self.native.AXUIElementCreateApplication.restype, C.c_void_p)
        self.assertIs(self.native.AXUIElementCopyElementAtPosition.argtypes[1], C.c_float)
        self.assertIs(self.native.AXUIElementGetAttributeValueCount.argtypes[2]._type_, C.c_long)
        self.assertIs(self.native.AXValueGetValue.argtypes[2], C.c_void_p)

    def test_cfurl_cfstring_and_boolean_are_typed(self):
        self.assertEqual(self.ax.text(self.app, 'AXURL'), PAGE)
        self.assertEqual(self.ax.text(self.app, 'AXRole'), PAGE)
        self.assertTrue(self.ax.flag(self.app, 'AXLoaded'))
        self.assertIsNone(self.ax.text(self.app, 'AXLoaded'))
        self.assertIsNone(self.ax.flag(self.app, 'AXURL'))
        self.assertIsNone(self.ax.element(self.app, 'AXURL'))

    def test_cfnumber_uses_double_output_and_cfindex_type_and_retains_copy(self):
        self.assertIs(self.cf.CFNumberGetTypeID.restype, C.c_ulong)
        self.assertEqual(self.cf.CFNumberGetValue.argtypes, [C.c_void_p, C.c_long, C.c_void_p])
        self.assertIs(self.cf.CFNumberGetValue.restype, C.c_bool)
        for progress in (0.0, 0.5, 1.0):
            self.numbers[206] = progress
            self.assertEqual(self.ax.number(self.app, 'AXLoadingProgress'), progress)
            self.assertEqual(self.cf.CFNumberGetValue.call_args.args[:2], (206, 13))
        self.assertIn(206, self.ax.owned)

    def test_optional_number_missing_wrong_type_and_conversion_error_differ(self):
        self.assertIsNone(self.ax.number(self.app, 'AXUnsupported'))
        for value in (200, 202):  # text and Boolean are not numeric progress
            self.values['AXLoadingProgress'] = value
            with self.assertRaises(ValueError):
                self.ax.number(self.app, 'AXLoadingProgress')
        self.cf.CFNumberGetValue.assert_not_called()
        self.values['AXLoadingProgress'] = 206

        def lossy(value, kind, out):
            C.cast(out, C.POINTER(C.c_double))[0] = 1.0
            return False  # a provider conversion error cannot become a ready=1 result

        self.cf.CFNumberGetValue.side_effect = lossy
        with self.assertRaises(ValueError):
            self.ax.number(self.app, 'AXLoadingProgress')

    def test_cfurl_borrowed_string_is_not_released_as_a_copy(self):
        self.ax.text(self.app, 'AXURL')
        self.ax.close()
        released = [call.args[0] for call in self.cf.CFRelease.call_args_list]
        self.assertIn(201, released)
        self.assertNotIn(200, released)
        self.assertEqual(released.count(self.app), 1)
        self.assertEqual(released.count(self.system), 1)

    def test_child_array_refs_stay_owned_and_are_bounded(self):
        self.assertEqual(self.ax.children(self.app), [self.app, self.system])
        self.assertIn(203, self.ax.owned)
        with patch.object(y, 'MAC_AX_NODES', 1):
            with self.assertRaises(ValueError):
                self.ax.children(self.app)
        self.native.AXUIElementCopyAttributeValues.assert_called_once()

    def test_native_position_size_and_type_guard(self):
        self.assertEqual(self.ax.bounds(self.app), [0.5, -20.5, 800.0, 600.0])
        self.types[204] = 11
        self.assertIsNone(self.ax.bounds(self.app))

    def test_unsupported_attribute_is_optional_but_provider_failure_is_not(self):
        self.assertIsNone(self.ax.get(self.app, 'AXHidden'))
        self.native.AXUIElementCopyAttributeValue.side_effect = lambda *args: -25204
        with self.assertRaises(ValueError):
            self.ax.get(self.app, 'AXHidden')
        self.native.AXUIElementGetAttributeValueCount.side_effect = lambda *args: -25204
        with self.assertRaises(ValueError):
            self.ax.children(self.app)


if __name__ == '__main__':
    unittest.main()
