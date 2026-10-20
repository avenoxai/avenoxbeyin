"""Windows side of the capture tool (yakala): console output and the hotkey listener (#268).

Three layers. The output tests set the console's encoding with PYTHONIOENCODING and run on
every OS. The flow tests drive the listener against an in-memory model of the few Win32
calls it makes, so its rules (one listener per logon session, stop by name, status only after
the key is registered) hold everywhere. The Windows-only tests call the real API: RegisterHotKey,
the named mutex and event, and shortcuts written through the shell's link object. They skip, with a line
on stderr, when the session cannot register a hotkey at all (no interactive desktop).
"""
import base64
import ctypes
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from v3_package_helpers import inherited_env, install, snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
MODULE = SCRIPTS / 'beyin_v3_yakala.py'
INBOX = '📥 000-Inbox/Yakala'
KEY = 'ctrl+alt+shift+f9'  # unlikely to be taken on a developer machine or a CI runner
UTF8 = ('utf-8', 'utf8', 'UTF8', 'cp65001')  # one encoding under the names a stream may report
WINDOWS = os.name == 'nt'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


def wait_until(check, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return bool(check())


class ConsoleOutputTest(unittest.TestCase):
    """A piped Windows console is cp1254 or cp1252; neither can encode the inbox emoji. A UTF-8
    stream (macOS, Linux, PYTHONUTF8) keeps the characters as 3.9.0 printed them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-out-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'Örnek Beyin', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), APPDATA=str(root / 'appdata'))
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.vault / INBOX).parent.mkdir(exist_ok=True)  # the starter inbox: an emoji in every path printed

    def entry(self, encoding, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                env=dict(self.env, PYTHONIOENCODING=encoding), cwd=self.vault, timeout=120)
        self.assertEqual(result.returncode, 0, (encoding, args, result.stderr))
        self.raw = result.stdout
        if encoding in UTF8:
            # Literal characters: nobody reads a card title as a row of \u escapes.
            self.assertIsNone(re.search(rb'(?<!\\)\\u[0-9a-fA-F]{4}', result.stdout), (encoding, args))
        else:
            # ASCII bytes mean the same under every decoder a caller may use for the pipe.
            self.assertTrue(result.stdout.isascii(), (encoding, args, result.stdout[:200]))
        return json.loads(result.stdout.decode('utf-8'))

    def carries(self, encoding, text):
        """`text` as the last output must hold it: itself on UTF-8, its JSON escapes on a code page."""
        self.assertIn(text.encode('utf-8') if encoding in UTF8 else json.dumps(text)[1:-1].encode('ascii'), self.raw)

    def test_json_is_readable_on_utf8_and_ascii_on_legacy_code_pages(self):
        note, encodings = 'Şifre ve Müşteri Arşiv notu, Iğdır', ('utf-8', 'cp1254', 'cp1252')
        for number, encoding in enumerate(encodings, 1):
            with self.subTest(encoding=encoding):
                text = note + ' ' + str(number)
                installed = self.entry(encoding, 'kur', '--kisayol-yok')
                self.carries(encoding, INBOX)
                self.assertEqual(installed['klasor'], INBOX)
                self.assertTrue((self.vault / installed['web_clipper_sablonu']).is_file())
                added = self.entry(encoding, 'ekle', '--metin', text)
                self.assertTrue(added['path'].startswith(INBOX + '/'), added['path'])
                self.assertTrue((self.vault / added['path']).is_file())
                listed = self.entry(encoding, 'liste')
                self.carries(encoding, text)
                # A command that failed after writing its card left a second card on the retry.
                self.assertEqual(listed['bekleyen'], 1)
                self.assertIn(text, [card['baslik'] for card in listed['kartlar']])
                processed = self.entry(encoding, 'isle', '--ses-yok')  # a typed note: nothing is fetched
                self.assertEqual([(card['id'], card['durum']) for card in processed['kartlar']], [(added['id'], 'cikarildi')])
                self.assertTrue(processed['kartlar'][0]['ham'].startswith(INBOX + '/.ham/'), processed['kartlar'][0])
                self.assertEqual(self.entry(encoding, 'durum')['klasor'], INBOX)
                self.assertEqual(self.entry(encoding, 'sablon')['path'], INBOX)
        # Whatever the stream, the same values: the read-only commands, once per encoding.
        for command in (['liste'], ['liste', '--durum', 'cikarildi'], ['durum'], ['sablon']):
            first, *others = [self.entry(encoding, *command) for encoding in encodings]
            self.assertEqual(others, [first, first], command)
        self.assertEqual(len(self.entry('cp1254', 'liste', '--durum', 'cikarildi')['kartlar']), 3)

    def test_the_stream_decides_not_the_platform(self):
        self.entry('utf-8', 'kur', '--kisayol-yok')
        for encoding in UTF8 + ('cp857', 'ascii', 'latin-1'):
            with self.subTest(encoding=encoding):
                self.assertEqual(self.entry(encoding, 'durum')['klasor'], INBOX)
                self.carries(encoding, INBOX)

    def test_durum_writes_nothing(self):
        self.entry('utf-8', 'kur', '--kisayol-yok')
        before = (snapshot(self.vault), snapshot(self.state))
        self.entry('cp1254', 'durum')
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)

    @unittest.skipIf(WINDOWS, 'needs a POSIX pseudo terminal')
    def test_human_text_survives_a_legacy_terminal(self):
        """The module run on its own (the listener and Send To do that) prints text for a person."""
        import pty
        import select
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_yakala.py'), '--vault',
                                    str(self.vault), 'ekle', '--metin', 'Müşteri notu'], stdin=subprocess.DEVNULL,
                                   stdout=slave, stderr=subprocess.PIPE, env=dict(self.env, PYTHONIOENCODING='cp1254'),
                                   cwd=self.vault)
        os.close(slave)
        shown = b''
        try:
            while select.select([master], [], [], 60)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # Linux reports the closed terminal as EIO
                    break
                if not chunk:
                    break
                shown += chunk
        finally:
            os.close(master)
            error = process.stderr.read()
            process.stderr.close()
            process.wait(60)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn(b'Beyne atildi: ? 000-Inbox/Yakala/', shown)
        self.assertEqual(len(list((self.vault / INBOX).glob('*.md'))), 1)


class JsonTextTest(unittest.TestCase):
    def test_encoding_names_and_fallbacks(self):
        value = {'klasor': INBOX, 'baslik': 'Şifre ve Iğdır'}
        readable = json.dumps(value, ensure_ascii=False, indent=2)
        escaped = json.dumps(value, ensure_ascii=True, indent=2)
        self.assertNotEqual(readable, escaped)
        for name in UTF8 + ('UTF-8', 'utf_8'):
            self.assertEqual(yakala._json_text(value, Mock(encoding=name, closed=False)), readable, name)
        for name in ('cp1254', 'cp1252', 'cp857', 'ascii', 'latin-1', 'utf-16', 'utf-8-sig', 'no-such-codec', '', None):
            self.assertEqual(yakala._json_text(value, Mock(encoding=name, closed=False)), escaped, name)
        self.assertEqual(yakala._json_text(value, object()), escaped)  # a replaced stdout without the attributes
        self.assertEqual(yakala._json_text(value, io.StringIO()), escaped)
        stream = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        self.assertEqual(yakala._json_text(value, stream), readable)
        stream.detach()  # still reports 'utf-8', but nothing can be written to it
        self.assertEqual(yakala._json_text(value, stream), escaped)
        self.assertEqual(yakala._json_text(value, Mock(encoding='utf-8', closed=True)), escaped)
        # A lone surrogate (a file name the system could not decode) cannot be written as UTF-8.
        self.assertEqual(yakala._json_text({'ad': 'x\udcff'}, Mock(encoding='utf-8', closed=False)),
                         '{\n  "ad": "x\\udcff"\n}')


@unittest.skipUnless(WINDOWS, 'the waiting window is a real Windows process')
class WarmWindowTest(unittest.TestCase):
    """`pencere --hazir` as the listener starts it: alive to Windows while it waits, gone when the pipe closes."""

    def top_level_windows(self, pid):
        user32 = ctypes.windll.user32
        found = []
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def each(hwnd, _param):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            if owner.value == pid:
                found.append(hwnd)
            return True
        user32.EnumWindows(callback(each), None)
        return found

    def test_waiting_window_answers_messages_and_exits_when_the_pipe_closes(self):
        vault = Path(tempfile.mkdtemp(prefix='v3-yakala-hazir-'))
        self.addCleanup(shutil.rmtree, vault, True)
        child = subprocess.Popen([sys.executable, str(MODULE), 'pencere', '--vault', str(vault), '--hazir'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=inherited_env())

        def stop_child():
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait(15)
        self.addCleanup(stop_child)
        self.assertTrue(wait_until(lambda: self.top_level_windows(child.pid), 30), 'Tk never created a window')
        time.sleep(0.5)  # well past start-up: from here on it only waits for the key
        user32 = ctypes.windll.user32
        for hwnd in self.top_level_windows(child.pid):
            answer = ctypes.c_size_t()
            # WM_NULL with SMTO_ABORTIFHUNG: a window that stopped reading messages times out here.
            self.assertTrue(user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), 0, 0, 0, 0x0002, 2000, ctypes.byref(answer)))
        child.stdin.close()
        self.assertEqual(child.wait(15), 0)


class LegacyListenerWindowTest(unittest.TestCase):
    """After an update the running listener is still the old state copy until `yakala kur`, but it
    starts the vault's new module with `--hazir` and no `--instance`, then writes one context line."""

    def test_protocol_only_with_an_instance(self):
        with patch.object(yakala.os, 'name', 'nt'), \
                patch.object(yakala, '_windows_warm_popup', return_value={'status': 'protokol'}) as new, \
                patch.object(yakala, '_context_line_popup', return_value={'status': 'satir'}) as old:
            self.assertEqual(yakala.warm_popup('v', 's', 'a' * 32), {'status': 'protokol'})
            self.assertEqual(yakala.warm_popup('v', 's'), {'status': 'satir'})
        new.assert_called_once_with('v', 's', 'a' * 32)
        old.assert_called_once_with('v', 's')

    def test_a_legacy_context_line_is_not_a_protocol_message(self):
        # What a 3.10 listener writes: without the line path, the new window aborts on it and no popup opens.
        legacy = {'uygulama': 'chrome', 'pencere': 'Sayfa - Google Chrome', 'url': 'https://example.com/'}
        self.assertFalse(yakala._protocol_valid(legacy, 'a' * 32, ('ACTIVATE', 'FOCUS', 'STOP')))


class BrowserUrlTest(unittest.TestCase):
    """The active tab's link comes from the browser's own address bar; anything else is not a link."""

    def test_address_bar_values(self):
        self.assertIsNone(yakala.tab_url('youtube.com/watch?v=abc\r\n'))
        self.assertIsNone(yakala.tab_url('http://127.0.0.1:8080/x'))
        self.assertEqual(yakala.tab_url('https://github.com/avenoxai'), 'https://github.com/avenoxai')
        for value in ('yarim kalmis arama', 'brave://settings', 'localhost', 'http://10.0.0.1/', 'https://user:pass@example.com/', 'https://[broken/', 'https://example.com:bad/', '', None):
            self.assertIsNone(yakala.tab_url(value), value)

    def test_a_private_browser_tab_does_not_override_clipboard_capture(self):
        with patch.object(yakala, '_windows_read', return_value={'value': 'http://127.0.0.1:8080/panel'}):
            self.assertIsNone(yakala.windows_browser_url(42, 'chrome'))

    def test_each_browser_is_asked_for_its_own_address_bar_class(self):
        with patch.object(yakala, '_windows_read', return_value={'value': 'https://github.com/avenoxai'}) as read:
            self.assertEqual(yakala.windows_browser_url(42, 'Brave'), 'https://github.com/avenoxai')
            self.assertEqual(yakala.windows_browser_url(42, 'msedge'), 'https://github.com/avenoxai')
            self.assertIsNone(yakala.windows_browser_url(42, 'not-a-browser'))
        self.assertEqual([c.args[1]['class'] for c in read.call_args_list], ['BraveOmniboxViewViews', 'OmniboxViewViews'])

    def test_reader_failure_falls_back_to_the_clipboard(self):
        for returned in ({'error': 'timeout'}, {'value': None}, {'value': 'chrome://settings'}):
            with patch.object(yakala, '_windows_read', return_value=returned):
                self.assertIsNone(yakala.windows_browser_url(42, 'chrome'))

    def test_timeout_is_passed_to_an_independent_reader_each_time(self):
        with patch.object(yakala, '_windows_read', side_effect=[{'error': 'timeout'}, {'value': 'https://example.com/'}]) as read:
            self.assertIsNone(yakala.windows_browser_url(42, 'chrome', timeout=0.1))
            self.assertEqual(yakala.windows_browser_url(42, 'chrome', timeout=0.1), 'https://example.com/')
        self.assertEqual(read.call_count, 2)
        self.assertEqual([c.args[2] for c in read.call_args_list], [0.1, 0.1])

    @unittest.skipUnless(WINDOWS, 'IsHungAppWindow is Windows only')
    def test_a_hung_window_is_not_asked_at_all(self):
        with patch.object(ctypes.windll.user32, 'IsHungAppWindow', return_value=1), \
                patch.object(yakala, '_windows_read') as read:
            self.assertIsNone(yakala.windows_browser_url(42, 'chrome'))
        read.assert_not_called()  # the isolated reader process is never started

    @unittest.skipUnless(WINDOWS, 'UI Automation is Windows only')
    def test_real_ui_automation_with_a_dead_window_gives_nothing_and_releases_com(self):
        # The real COM path end to end: ElementFromHandle fails on a closed handle and nothing escapes.
        self.assertIsNone(yakala.windows_browser_url(0, 'chrome'))
        seen = {}

        def run():
            with self.assertRaises(OSError):
                yakala._uia_address_bar(0, 'OmniboxViewViews')
            # Balanced: COM is not left initialised on the thread, so this is a fresh S_OK, not S_FALSE.
            seen['again'] = ctypes.windll.ole32.CoInitializeEx(None, 2)
            ctypes.windll.ole32.CoUninitialize()
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(10)
        self.assertEqual(seen.get('again'), 0)


class WindowsCaptureModelTest(unittest.TestCase):
    def test_title_suffixes_and_known_edge_profile(self):
        for title, app, profile, expected in (
                ('Video - YouTube - Opera Air', 'opera', None, 'Video - YouTube'),
                ('Sayfa — Mozilla Firefox', 'firefox', None, 'Sayfa'),
                ('Sayfa - İş - Microsoft Edge', 'msedge', 'İş', 'Sayfa'),
                ('Sayfa – Microsoft Edge – İş', 'msedge', None, 'Sayfa')):
            self.assertEqual(yakala._win_page_title(title, app, profile), expected)

    def test_public_ip_and_punycode_follow_upstream_boundary(self):
        for url in ('https://8.8.8.8/path', 'https://example.xn--p1ai/'):
            self.assertEqual(yakala.tab_url(url), url)
        for url in ('http://localhost:3000', 'http://192.168.1.20', 'https://örnek.com', 'file:///C:/secret'):
            self.assertIsNone(yakala.tab_url(url))

    def test_opera_uses_the_verified_document_reader_never_generic_edit_fields(self):
        with patch.object(yakala, '_windows_read', return_value={'value': None}) as read:
            self.assertIsNone(yakala.windows_browser_url(42, 'opera'))
        self.assertEqual(read.call_args.args[0:2], ('opera', {'hwnd': 42}))

    def test_legacy_context_must_match_foreground_title(self):
        user = Mock()
        user.GetForegroundWindow.return_value = 42
        user.GetWindowTextW.side_effect = lambda _h, buffer, _n: setattr(buffer, 'value', 'Current') or 7
        with patch.object(ctypes, 'WinDLL', return_value=user, create=True):
            self.assertEqual(yakala._win_foreground({'pencere': 'Current'}), 42)
            self.assertEqual(yakala._win_foreground({'pencere': 'Previous'}), 0)
            self.assertEqual(yakala._win_foreground({'hwnd': 7, 'pencere': 'Current'}), 0)
            self.assertEqual(yakala._win_foreground({}), 0)
        with patch.object(yakala, '_win_foreground', return_value=0), \
                patch.object(yakala, 'windows_browser_url') as uia:
            context = yakala.windows_enrich({'url': 'https://example.com', 'uygulama': 'opera'})
            self.assertNotIn('url', context)
            uia.assert_not_called()

    def test_printwindow_and_both_gdi_selections_are_checked(self):
        for selection, copied, foreground, expected in (([4, 3], 1, [42, 42], True),
                ([0], 1, [42], False), ([4, 0], 1, [42], False),
                ([4, ctypes.c_void_p(-1).value], 1, [42], False),
                ([4, 3], 0, [42], False), ([4, 3], 1, [42, 7], False)):
            user, gdi = Mock(), Mock()
            user.GetForegroundWindow.side_effect = foreground
            user.IsIconic.return_value = 0
            user.GetDC.return_value = 1

            def rect(_hwnd, pointer):
                pointer._obj.left, pointer._obj.top = -100, 0
                pointer._obj.right, pointer._obj.bottom = -98, 2
                return 1
            user.GetWindowRect.side_effect = rect
            user.PrintWindow.return_value = copied
            gdi.CreateCompatibleDC.return_value, gdi.CreateCompatibleBitmap.return_value = 2, 3
            gdi.SelectObject.side_effect = selection
            gdi.GetDIBits.return_value = 2
            cleanup = []
            gdi.DeleteDC.side_effect = lambda _h: cleanup.append('dc')
            gdi.DeleteObject.side_effect = lambda _h: cleanup.append('bitmap')
            with self.subTest(selection=selection, copied=copied), \
                    patch.object(ctypes, 'WinDLL', side_effect=lambda name: user if name == 'user32' else gdi, create=True), \
                    patch.object(yakala, 'dib_to_png', return_value=(b'png', 2, 2)):
                self.assertEqual(bool(yakala._win_window_png(42)), expected)
            self.assertEqual(cleanup, ['dc', 'bitmap'])
            gdi.BitBlt.assert_not_called()
            if expected:
                user.PrintWindow.assert_called_once_with(42, 2, 2)

    def test_foreground_switch_prevents_window_capture(self):
        with patch.object(yakala, '_win_foreground', return_value=0), patch.object(yakala, '_windows_read') as capture:
            self.assertIsNone(yakala.window_image({'hwnd': 42}))
        capture.assert_not_called()  # no capture process for a window that is no longer in front

    def test_focus_does_not_attach_input_queues(self):
        user = Mock()
        user.GetForegroundWindow.return_value = 99
        with patch.object(ctypes, 'WinDLL', return_value=user, create=True):
            self.assertTrue(yakala._win_focus(99))
        user.AttachThreadInput.assert_not_called()
        user.SetForegroundWindow.assert_called_once_with(99)

    def test_escape_scope_follows_the_foreground_while_the_popup_is_open(self):
        self.assertEqual(yakala._escape_scope(42, 42, 99), 'consume')  # source page in front
        self.assertEqual(yakala._escape_scope(50, 42, 99), 'pass')  # another window was clicked
        self.assertIsNone(yakala._escape_scope(99, 42, 99))  # the popup's own Tk binding handles it
        self.assertIsNone(yakala._escape_scope(None, 42, 99))
        self.assertEqual(yakala._escape_scope(50, 0, 99), 'pass')

    def test_popup_position_uses_negative_monitor_work_area(self):
        self.assertEqual(yakala._popup_position((-1920, 0, 0, 1040), 640, 400), (-1280, 228))
        self.assertEqual(yakala._popup_position((0, -1080, 1920, -40), 640, 400), (640, -852))

    def test_monitor_uses_source_window_and_work_area(self):
        user = Mock()
        user.MonitorFromWindow.return_value = 55

        def info(_handle, pointer):
            work = pointer._obj.work
            work.left, work.top, work.right, work.bottom = -1920, 0, 0, 1040
            return 1
        user.GetMonitorInfoW.side_effect = info
        with patch.object(ctypes, 'WinDLL', return_value=user, create=True):
            self.assertEqual(yakala._win_monitor(42), (-1920, 0, 0, 1040))
        user.MonitorFromWindow.assert_called_once_with(42, 2)

    def test_clipboard_reads_never_share_a_stuck_worker_gate(self):
        with patch.object(yakala, '_windows_read', side_effect=[{'error': 'timeout'}, {'text': 'new', 'sequence': 8}]) as read:
            self.assertEqual(yakala._windows_clipboard(), {'error': 'timeout'})
            self.assertEqual(yakala._windows_clipboard()['text'], 'new')
        self.assertEqual(read.call_count, 2)

    def test_stop_timeout_and_failed_signal_are_errors(self):
        kernel = Mock()
        kernel.OpenEventW.return_value = 42
        kernel.SetEvent.return_value = 1
        with patch.object(yakala, '_win32', return_value=(Mock(), kernel)):
            with self.assertRaises(TimeoutError):
                yakala._windows_stop_listener(timeout=0)
            kernel.SetEvent.return_value = 0
            with self.assertRaises(OSError):
                yakala._windows_stop_listener(timeout=0)

    def test_job_assignment_precedes_resume_and_timeout_closes_job(self):
        kernel, process = Mock(), Mock()
        process._handle, process.pid = 12, 123
        process.wait.side_effect = [subprocess.TimeoutExpired('helper', 1), None]
        process.poll.return_value = None
        events = []
        kernel.AssignProcessToJobObject.side_effect = lambda *_: events.append('assign') or True
        kernel.CloseHandle.side_effect = lambda *_: events.append('close')
        with patch.object(yakala, '_windows_job', return_value=(kernel, 42)), \
                patch.object(yakala, '_windows_resume', side_effect=lambda _pid: events.append('resume')), \
                patch.object(yakala.subprocess, 'Popen', return_value=process) as popen:
            with self.assertRaises(subprocess.TimeoutExpired):
                yakala._windows_run(['helper'], 1, None, {})
        self.assertEqual(events, ['assign', 'resume', 'close'])
        self.assertTrue(popen.call_args.kwargs['creationflags'] & 4)
        process.kill.assert_called_once()

    def test_clipboard_snapshot_uses_a_killable_reader(self):
        with patch.object(yakala, '_windows_read', return_value={'text': 'copied'}) as read:
            self.assertEqual(yakala._windows_clipboard(), {'text': 'copied'})
        read.assert_called_once_with('clipboard', {'snapshot': True}, 1.0, None)

    def test_job_creation_failure_does_not_start_a_helper(self):
        with patch.object(yakala, '_windows_job', side_effect=OSError('job refused')), \
                patch.object(yakala.subprocess, 'Popen') as popen:
            with self.assertRaises(OSError):
                yakala._windows_run(['helper'], 1, None, {})
        popen.assert_not_called()

    @unittest.skipUnless(WINDOWS, 'ctypes callback ABI; Win32 itself is mocked')
    def test_escape_event_is_seen_once_and_hook_is_removed(self):
        user, kernel, root, dismiss = Mock(), Mock(), Mock(), Mock()
        user.GetForegroundWindow.return_value = 42
        user.SetWindowsHookExW.return_value = 100
        user.CallNextHookEx.return_value = 0
        user.GetAsyncKeyState.return_value = 0
        kernel.GetModuleHandleW.return_value = 1
        with patch.object(ctypes, 'WinDLL', side_effect=lambda name, **_kw: user if name == 'user32' else kernel):
            stop = yakala._win_escape_guard(root, 99, 42, dismiss)
            callback = user.SetWindowsHookExW.call_args.args[1]
            key = (ctypes.c_uint32 * 6)(0x1B, 0, 0, 0, 0, 0)  # KBDLLHOOKSTRUCT, including flags
            callback(0, 0x100, ctypes.addressof(key))
            root.after.call_args.args[1]()
            stop()
        dismiss.assert_called_once()
        user.UnhookWindowsHookEx.assert_called_once_with(100)


@unittest.skipUnless(WINDOWS, 'Windows Job Objects; no desktop or clipboard access')
class WindowsJobTest(unittest.TestCase):
    def test_timeout_terminates_helper_and_grandchild(self):
        # The killed processes release their working folder a moment after they report exit.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            pid_file = Path(directory) / 'child.pid'
            script = ('import subprocess, sys, time; from pathlib import Path; '
                      'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
                      'creationflags=subprocess.CREATE_NO_WINDOW); '
                      'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')
            with self.assertRaises(subprocess.TimeoutExpired):
                yakala._windows_run([sys.executable, '-c', script, str(pid_file)], 3, directory, dict(os.environ))
            self.assertTrue(pid_file.exists(), 'helper must start before the timeout')
            kernel = ctypes.WinDLL('kernel32')
            kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, int(pid_file.read_text()))
            if handle:
                try:
                    code = ctypes.c_uint32()
                    def stopped():
                        return kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259
                    self.assertTrue(wait_until(stopped, 3), 'grandchild survived the closed job')
                finally:
                    kernel.CloseHandle(handle)


class FakeSession:
    """What the listener uses of one logon session: named events and mutexes with handle
    counts, the hotkey table and one message queue per thread. Serves as user32 and kernel32."""

    def __init__(self):
        self.changed = threading.Condition()
        self.objects = {}   # name -> [kind, open handles, signalled]
        self.handles = {}   # handle -> name
        self.hotkeys = {}   # (modifiers, key) -> owning thread
        self.queues = {}    # thread -> pending messages
        self.errors = {}    # thread -> last error
        self.calls = []
        self.fail_wait = False
        self.last = 0x100

    def get_error(self):
        return self.errors.get(threading.get_ident(), 0)

    def set_error(self, value):
        self.errors[threading.get_ident()] = value

    def _handle(self, name):
        self.objects[name][1] += 1
        self.last += 4
        self.handles[self.last] = name
        return self.last

    def _create(self, kind, name, signalled=False):
        with self.changed:
            existed = name in self.objects
            if existed and self.objects[name][0] != kind:
                self.set_error(6)
                return None
            self.objects.setdefault(name, [kind, 0, signalled])
            self.set_error(183 if existed else 0)
            self.calls.append(('create-' + kind, name))
            return self._handle(name)

    def _open(self, kind, name):
        with self.changed:
            if name not in self.objects or self.objects[name][0] != kind:
                self.set_error(2)
                return None
            return self._handle(name)

    def CreateEventW(self, _security, _manual, initial, name):
        return self._create('event', name, bool(initial))

    def CreateMutexW(self, _security, _owner, name):
        return self._create('mutex', name)

    def OpenEventW(self, _access, _inherit, name):
        return self._open('event', name)

    def OpenMutexW(self, _access, _inherit, name):
        return self._open('mutex', name)

    def SetEvent(self, handle):
        with self.changed:
            self.objects[self.handles[handle]][2] = True
            self.changed.notify_all()
        return 1

    def CloseHandle(self, handle):
        with self.changed:
            name = self.handles.pop(handle)
            self.objects[name][1] -= 1
            if not self.objects[name][1]:
                del self.objects[name]
        return 1

    def RegisterHotKey(self, _window, _ident, modifiers, key):
        with self.changed:
            if (modifiers, key) in self.hotkeys:
                self.set_error(1409)
                return 0
            self.hotkeys[(modifiers, key)] = threading.get_ident()
            self.queues.setdefault(threading.get_ident(), [])
            self.calls.append(('register', (modifiers, key)))
        return 1

    def UnregisterHotKey(self, _window, _ident):
        with self.changed:
            for combination, owner in list(self.hotkeys.items()):
                if owner == threading.get_ident():
                    del self.hotkeys[combination]
        return 1

    def MsgWaitForMultipleObjects(self, count, handles, _all, _timeout, _mask):
        self.calls.append(('wait', None))
        if self.fail_wait:
            self.set_error(6)
            return 0xFFFFFFFF
        name = self.handles[handles[0]]
        deadline = time.monotonic() + 60
        with self.changed:
            queue = self.queues.setdefault(threading.get_ident(), [])
            while not self.objects[name][2] and not queue:
                if time.monotonic() > deadline:
                    return 0xFFFFFFFF
                self.changed.wait(1)
            return 0 if self.objects[name][2] else count

    def PeekMessageW(self, reference, _window, _low, _high, _remove):
        with self.changed:
            queue = self.queues.setdefault(threading.get_ident(), [])
            if not queue:
                return 0
            reference._obj.message = queue.pop(0)
        return 1

    def AllowSetForegroundWindow(self, pid):
        self.calls.append(('foreground', pid))
        return 1

    def GetCurrentThreadId(self):
        return threading.get_ident()

    def PostThreadMessageW(self, thread, message, _wparam, _lparam):
        with self.changed:
            self.queues.setdefault(thread, []).append(message)
            self.changed.notify_all()
        return 1

    def WaitForSingleObject(self, handle, _timeout):
        with self.changed:
            return 0 if self.objects[self.handles[handle]][2] else 0x102  # WAIT_TIMEOUT

    def press(self, modifiers, key):
        """The user presses a registered combination: WM_HOTKEY lands in its owner's queue."""
        with self.changed:
            self.queues[self.hotkeys[(modifiers, key)]].append(0x0312)
            self.changed.notify_all()


class ListenerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-flow-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.other, self.state = root / 'Örnek Beyin', root / 'Başka Beyin', root / 'state'
        self.vault.mkdir()
        self.other.mkdir()
        self.session = FakeSession()
        for patcher in (patch.object(yakala, '_WIN32', (self.session, self.session)),
                        patch.object(ctypes, 'get_last_error', self.session.get_error, create=True),
                        patch.object(ctypes, 'set_last_error', self.session.set_error, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.broker = Mock(error=None)
        self.broker.press.return_value = True
        broker_patch = patch.object(yakala, '_WindowsPopupBroker', return_value=self.broker)
        self.broker_factory = broker_patch.start()
        self.addCleanup(broker_patch.stop)
        self.listeners = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        yakala._windows_stop_listener(timeout=5)
        for thread, _result in self.listeners:
            thread.join(10)

    def listen(self, vault, spec='ctrl+alt+b'):
        result = {}

        def run():
            try:
                result['code'] = yakala.listen_windows(vault, MODULE, spec, self.state)
            except SystemExit as exc:
                result['code'] = exc.code
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.listeners.append((thread, result))
        return thread, result

    def test_registers_reports_opens_the_window_and_stops_by_name(self):
        thread, result = self.listen(self.vault)
        self.assertTrue(wait_until(lambda: self.broker_factory.called, 5))
        self.assertEqual(list(self.session.hotkeys), [(0x4003, ord('B'))])
        self.assertTrue(yakala._windows_listener_running(self.vault))
        self.assertFalse(yakala._windows_listener_running(self.other))
        order = [name for name, _ in self.session.calls]
        self.assertLess(order.index('register'), order.index('create-mutex'))
        context = {'schema': 2, 'hwnd': 42, 'uygulama': 'chrome', 'pencere': 'Şifre & Müşteri'}
        with patch.object(yakala, 'windows_context', return_value=context) as read:
            self.session.press(0x4003, ord('B'))
            self.assertTrue(wait_until(lambda: self.broker.press.called, 5))
        read.assert_called_once_with(read_url=False)
        self.broker.press.assert_called_once_with(context)
        command = self.broker_factory.call_args.args[0]('--hazir', '--instance', 'a' * 32)
        self.assertEqual(command[1:3], [str(MODULE.resolve()), 'pencere'])
        self.assertNotIn('--baglam', command)
        yakala._windows_stop_listener(self.other, timeout=1)
        self.assertTrue(thread.is_alive())
        self.broker.close.assert_not_called()
        yakala._windows_stop_listener(self.vault, timeout=5)
        thread.join(5)
        self.assertEqual(result, {'code': 0})
        self.broker.close.assert_called_once()
        self.assertEqual((self.session.hotkeys, self.session.objects, self.session.handles), ({}, {}, {}))

    def test_protocol_failure_stops_listener_instead_of_using_legacy_context(self):
        self.broker.error = 'IPC version mismatch'
        self.broker.press.return_value = False
        thread, result = self.listen(self.vault)
        self.assertTrue(wait_until(lambda: self.broker_factory.called, 5))
        with patch.object(yakala, 'windows_context', return_value={'schema': 2, 'hwnd': 42}):
            self.session.press(0x4003, ord('B'))
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['code'], 'IPC version mismatch')
        self.assertEqual(self.broker_factory.call_count, 1)

    def test_quick_presses_reuse_one_broker(self):
        self.listen(self.vault)
        self.assertTrue(wait_until(lambda: self.broker_factory.called, 5))
        with patch.object(yakala, 'windows_context', return_value={'schema': 2, 'hwnd': 42}):
            for _ in range(3):
                self.session.press(0x4003, ord('B'))
            self.assertTrue(wait_until(lambda: self.broker.press.call_count == 3, 5))
        self.assertEqual(self.broker_factory.call_count, 1)

    def test_context_failure_keeps_the_broker_and_shutdown_closes_it(self):
        thread, result = self.listen(self.vault)
        self.assertTrue(wait_until(lambda: self.broker_factory.called, 5))
        with patch.object(yakala, 'windows_context', side_effect=RuntimeError('provider failed')) as read:
            self.session.press(0x4003, ord('B'))
            self.assertTrue(wait_until(lambda: read.called, 5))
            yakala._windows_stop_listener(self.vault, timeout=5)
            thread.join(5)
        self.assertEqual(result, {'code': 0})
        self.broker.press.assert_not_called()
        self.broker.close.assert_called_once()

    def test_a_taken_key_never_looks_like_a_running_listener(self):
        self.session.hotkeys[(0x4003, ord('B'))] = -1  # another application owns the combination
        thread, result = self.listen(self.vault)
        thread.join(10)
        self.assertIsInstance(result['code'], str)  # SystemExit with a message: exit status 1
        self.assertNotIn('create-mutex', [name for name, _detail in self.session.calls])
        self.assertFalse(yakala._windows_listener_running(self.vault))
        self.assertEqual((self.session.objects, self.session.handles), ({}, {}))  # the session lock is free again
        log = (self.state / 'yakala/dinleyici.log').read_text(encoding='utf-8')
        self.assertIn('ctrl+alt+b (Windows hata 1409)', log)
        process = Mock()
        process.poll.return_value = 1
        started = time.monotonic()
        self.assertFalse(yakala._windows_listener_ok(self.vault, process, timeout=30))
        self.assertLess(time.monotonic() - started, 5)  # an exited listener is not waited for

    def test_one_listener_per_session_and_install_moves_the_key(self):
        first, first_result = self.listen(self.vault)
        self.assertTrue(wait_until(lambda: yakala._windows_listener_running(self.vault), 10))
        again, again_result = self.listen(self.vault, 'ctrl+alt+k')
        again.join(10)
        self.assertEqual(again_result, {'code': 0})  # this vault's listener already runs
        second, second_result = self.listen(self.other, 'ctrl+alt+k')
        second.join(10)
        self.assertIsInstance(second_result['code'], str)
        self.assertEqual(list(self.session.hotkeys), [(0x4003, ord('B'))])
        yakala._windows_stop_listener(timeout=5)  # what `kur` in the other vault does first
        first.join(10)
        self.assertEqual(first_result, {'code': 0})
        moved, _moved_result = self.listen(self.other, 'ctrl+alt+k')
        self.assertTrue(wait_until(lambda: yakala._windows_listener_running(self.other), 10))
        self.assertFalse(yakala._windows_listener_running(self.vault))
        self.assertTrue(moved.is_alive())

    def test_a_failed_wait_ends_the_listener_instead_of_spinning(self):
        self.session.fail_wait = True
        thread, result = self.listen(self.vault)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result['code'], str)
        self.assertEqual([name for name, _detail in self.session.calls].count('wait'), 1)
        self.assertEqual((self.session.hotkeys, self.session.objects), ({}, {}))

    def test_names_and_keys(self):
        self.assertEqual(yakala._windows_mutex(self.vault), yakala._windows_mutex(self.vault / '.' / '..' / self.vault.name))
        self.assertNotEqual(yakala._windows_mutex(self.vault), yakala._windows_mutex(self.other))
        self.assertTrue(yakala._windows_mutex(self.vault).startswith('Local\\AvenoxBeyinYakala_'))
        with patch.dict(os.environ, {'APPDATA': str(Path(self.tmp.name) / 'appdata')}):
            link, other = yakala._windows_listener_link(self.vault), yakala._windows_listener_link(self.other)
        self.assertEqual(link.parent.name, 'Startup')
        self.assertRegex(link.name, r'^Beyne At Dinleyici [0-9a-f]{8}\.lnk$')
        self.assertNotEqual(link, other)
        # A letter outside ASCII is no virtual key: 'ş'.upper() is U+015E, not a key code.
        for bad in ('ctrl+alt+ş', 'ctrl+alt+ö', 'ctrl+alt+ı', 'ctrl+alt+İ'):
            with self.assertRaises(ValueError):
                yakala._windows_hotkey_vk(bad)
        self.assertEqual(yakala._windows_hotkey_vk('ctrl+alt+I'), (0x4003, ord('I')))
        # PowerShell closes a single-quoted string at typographic quotes too; each is doubled.
        self.assertEqual(yakala._ps_quote("C:\\Ali'nin \u2019 & $x"), "'C:\\Ali''nin \u2019\u2019 & $x'")


class WindowsEyes:
    """`os` or `sys` as the module reads them on Windows; every other name is the real module's.

    The platform checks in install(), uninstall() and status() look at os.name and sys.platform.
    Patching those on the real modules would change pathlib for the whole test process.
    """

    def __init__(self, module, **seen):
        self._module, self._seen = module, seen

    def __getattr__(self, name):
        return self._seen[name] if name in self._seen else getattr(self._module, name)


class FakeListenerProcess:
    """`pythonw <copy> dinle --vault V --tus K` as install() starts it, run on a thread."""
    started = []

    def __new__(cls, command, **options):
        if 'dinle' not in command:  # the listener's waiting window: nothing to run here
            return Mock(pid=4321, command=command)
        return super().__new__(cls)

    def __init__(self, command, **options):
        self.command, self.options, self.code = command, options, None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.started.append(self)

    def run(self):
        vault, spec = (self.command[self.command.index(flag) + 1] for flag in ('--vault', '--tus'))
        try:
            self.code = yakala.listen_windows(vault, self.command[1], spec)
        except SystemExit as exc:
            self.code = 1 if isinstance(exc.code, str) else exc.code

    def poll(self):
        return None if self.thread.is_alive() else self.code


class ListenerReadyTest(unittest.TestCase):
    """`kur` reports a working key only when the capture window answered READY, never on the mutex alone."""

    def test_install_check_waits_for_ready_and_fails_when_the_listener_leaves(self):
        session = FakeSession()
        vault = Path(tempfile.gettempdir()) / 'v3-yakala-ready-model'
        with patch.object(yakala, '_WIN32', (session, session)):
            mutex = session.CreateMutexW(None, False, yakala._windows_mutex(vault))
            ready = session.CreateEventW(None, True, False, yakala._windows_ready_event(vault))
            alive = Mock(**{'poll.return_value': None})
            self.assertFalse(yakala._windows_listener_ok(vault, alive, timeout=0.2))  # key held, window not ready
            session.SetEvent(ready)
            self.assertTrue(yakala._windows_listener_ok(vault, alive, timeout=0.2))
            gone = Mock(**{'poll.return_value': 1})
            self.assertFalse(yakala._windows_listener_ok(vault, gone, timeout=0.2))
            session.CloseHandle(ready)
            session.CloseHandle(mutex)

    def test_broker_failure_wakes_the_listener(self):
        posted = []
        broker = yakala._WindowsPopupBroker.__new__(yakala._WindowsPopupBroker)
        broker.log, broker.on_failure = Mock(), lambda: posted.append(True)
        broker._failure('Pencere baslatilamadi')
        self.assertEqual((broker.error, posted), ('Pencere baslatilamadi', [True]))


class InstallFlowTest(unittest.TestCase):
    """install(), uninstall() and status() on their Windows branches, on every OS: the session
    is the model above and a shortcut is a small JSON file instead of a .lnk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-install-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.session = FakeSession()
        FakeListenerProcess.started = []

        def shortcut(path, target, arguments, hotkey=None):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps({'target': str(target), 'arguments': arguments, 'hotkey': hotkey}),
                                  encoding='utf-8')
        for patcher in (patch.object(yakala, '_WIN32', (self.session, self.session)),
                        patch.object(ctypes, 'get_last_error', self.session.get_error, create=True),
                        patch.object(ctypes, 'set_last_error', self.session.set_error, create=True),
                        patch.object(yakala, 'os', WindowsEyes(os, name='nt')),
                        patch.object(yakala, 'sys', WindowsEyes(sys, platform='win32')),
                        patch.object(yakala, '_windows_shortcut', shortcut),
                        patch.object(yakala.subprocess, 'Popen', FakeListenerProcess),
                        # The model's capture window is a Mock that cannot speak the window protocol: the
                        # broker is a stand-in here, and readiness has its own test (ListenerReadyTest).
                        patch.object(yakala, '_WindowsPopupBroker', return_value=Mock(error=None)),
                        patch.object(yakala, '_windows_listener_ready', return_value=True),
                        patch.dict(os.environ, {'APPDATA': str(self.root / 'appdata'),
                                                'LOCALAPPDATA': str(self.root / 'localappdata')})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_all)
        self.programs = self.root / 'appdata/Microsoft/Windows/Start Menu/Programs'
        self.shared = [self.programs / 'Beyne At.lnk', self.root / 'appdata/Microsoft/Windows/SendTo/Beyne At.lnk']

    def stop_all(self):
        yakala._windows_stop_listener(timeout=5)
        for process in FakeListenerProcess.started:
            process.thread.join(10)

    def vault(self, name):
        vault, state = self.root / name, self.root / ('state ' + name)
        vault.mkdir()
        # The listener finds its state through the vault, as an installed vault's does.
        (vault / '.beyin-runtime.json').write_text(json.dumps({'state': str(state)}), encoding='utf-8')
        return vault, state

    def running(self, vault, state):
        return yakala.status(vault, state)['dinleyici_calisiyor']

    def test_the_key_moves_and_each_vault_removes_only_its_own(self):
        first, first_state = self.vault('Örnek Beyin')
        second, second_state = self.vault('Başka Beyin')
        self.assertIsNone(self.running(first, first_state))
        stale = self.programs / 'Startup/Beyne At Dinleyici.lnk'  # the name an earlier build of this change wrote
        stale.parent.mkdir(parents=True)
        stale.write_text('eski', encoding='utf-8')

        installed = yakala.install(first, first_state)
        self.assertEqual((installed['kisayol'], installed['kisayol_calisiyor'], installed['gonder_menusu']),
                         ('Ctrl+Alt+B', True, True))
        link = yakala._windows_listener_link(first)
        self.assertEqual(sorted(path.name for path in (self.programs / 'Startup').iterdir()), [link.name])
        written = json.loads(link.read_text(encoding='utf-8'))
        runner = first_state / 'yakala' / MODULE.name
        self.assertEqual(written['arguments'], subprocess.list2cmdline([str(runner), 'dinle', '--vault', str(first),
                                                                         '--tus', 'ctrl+alt+b']))
        self.assertTrue(runner.is_file())
        # The listener owns the combination: neither shortcut carries a .Hotkey for Explorer to register.
        self.assertEqual([json.loads(path.read_text(encoding='utf-8'))['hotkey'] for path in self.shared + [link]],
                         [None, None, None])
        process = FakeListenerProcess.started[-1]
        self.assertEqual(process.command[1:], [str(runner), 'dinle', '--vault', str(first), '--tus', 'ctrl+alt+b'])
        self.assertEqual((process.options['stdin'], process.options['stdout'], process.options['stderr']),
                         (subprocess.DEVNULL,) * 3)
        self.assertNotEqual(Path(process.options['cwd']), first)  # a listener inside the vault would lock the folder
        self.assertTrue(self.running(first, first_state))
        self.assertEqual(list(self.session.hotkeys), [(0x4003, ord('B'))])

        moved = yakala.install(second, second_state, spec='ctrl+alt+k')  # `kur` in another vault: the key moves
        self.assertTrue(moved['kisayol_calisiyor'])
        process.thread.join(10)
        self.assertEqual(process.poll(), 0)
        self.assertEqual(list(self.session.hotkeys), [(0x4003, ord('K'))])
        self.assertFalse(link.exists())
        self.assertIsNone(self.running(first, first_state))
        self.assertTrue(self.running(second, second_state))

        other = yakala._windows_listener_link(second)
        removed = yakala.uninstall(first, first_state)  # the first vault's `kaldir` leaves the second's alone
        self.assertNotIn('Beyne At.lnk', removed['kaldirilan'])
        self.assertTrue(other.is_file() and all(path.is_file() for path in self.shared))
        self.assertTrue(self.running(second, second_state))

        removed = yakala.uninstall(second, second_state)
        self.assertEqual(removed['kaldirilan'].count('Beyne At.lnk'), 2)
        self.assertIn(other.name, removed['kaldirilan'])
        self.assertFalse(other.exists() or any(path.exists() for path in self.shared))
        self.assertEqual((self.session.hotkeys, self.session.objects, self.session.handles), ({}, {}, {}))
        self.assertIsNone(self.running(second, second_state))
        self.assertEqual(yakala.uninstall(second, second_state)['kaldirilan'], [])  # twice is harmless

    def test_a_taken_key_is_reported_as_stopped(self):
        vault, state = self.vault('Örnek Beyin')
        self.session.hotkeys[(0x4003, ord('B'))] = -1  # another application owns the combination
        installed = yakala.install(vault, state)
        self.assertEqual((installed['kisayol'], installed['kisayol_calisiyor']), ('Ctrl+Alt+B', False))
        self.assertIn('UYARI', yakala.human(installed, 'kur'))
        self.assertIs(self.running(vault, state), False)  # installed for this vault, and not running
        self.assertIn('durmus', yakala.human(yakala.status(vault, state), 'durum'))

    def test_a_3_9_0_install_keeps_state_without_an_owner(self):
        vault, state = self.vault('Örnek Beyin')
        for path in self.shared:  # what 3.9.0 wrote: the key lived in the Start menu entry's .Hotkey
            yakala._windows_shortcut(path, 'pythonw.exe', 'pencere', 'CTRL+ALT+B')
        state.mkdir()
        (state / 'yakala.json').write_text(json.dumps({'schema': 1, 'kisayol': 'Ctrl+Alt+B', 'tus': 'ctrl+alt+b'}),
                                           encoding='utf-8')
        status = yakala.status(vault, state)
        self.assertEqual((status['kurulu'], status['dinleyici_calisiyor']), (True, None))
        self.assertNotIn('dinleyici', yakala.human(status, 'durum'))
        self.assertEqual(yakala.uninstall(vault, state)['kaldirilan'], ['Beyne At.lnk', 'Beyne At.lnk'])
        self.assertTrue((state / 'yakala.json').exists())

    def test_stop_timeout_aborts_install_before_any_writes(self):
        vault, state = self.vault('Örnek Beyin')
        before = snapshot(vault)
        with patch.object(yakala, '_windows_stop_listener', side_effect=TimeoutError('still running')), \
                patch.object(yakala, '_windows_shortcut') as shortcut:
            with self.assertRaises(TimeoutError):
                yakala.install(vault, state)
        shortcut.assert_not_called()
        self.assertEqual(snapshot(vault), before)
        self.assertFalse(state.exists())


class ShortcutWriterTest(unittest.TestCase):
    """The shortcut is written through the shell's link object; these parts run on every system."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-lnk-')
        self.addCleanup(self.tmp.cleanup)
        self.link = Path(self.tmp.name) / 'kısayollar' / 'Beyne At.lnk'

    def script(self, run):
        encoded = run.call_args.args[0][-1]
        return base64.b64decode(encoded).decode('utf-16le')

    def shell_save(self, command, **_options):
        for temporary in self.link.parent.glob('.yakala-*.lnk'):
            temporary.write_bytes(b'shell-link')

    def test_the_link_object_that_keeps_unicode_writes_the_file(self):
        with patch.object(yakala.subprocess, 'run', side_effect=self.shell_save) as run:
            yakala._windows_shortcut(self.link, 'pythonw.exe', 'dinle --vault "Şifre\'nin ’ 📥 kasası"')
        text = self.script(run)
        self.assertIn('Shell.Application', text)
        self.assertNotIn('WScript.Shell', text)  # it stores Ş as S and an emoji as ??
        self.assertIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn("'dinle --vault \"Şifre\'\'nin ’’ 📥 kasası\"'", text)
        self.assertNotIn('.Hotkey', text)
        self.assertEqual(self.link.read_bytes(), b'shell-link')

    def test_an_old_key_is_written_as_the_number_a_link_stores(self):
        self.assertEqual(yakala._windows_link_hotkey('CTRL+ALT+B'), 0x0600 | ord('B'))
        self.assertEqual(yakala._windows_link_hotkey('Alt+Ctrl+b'), 0x0600 | ord('B'))
        self.assertEqual(yakala._windows_link_hotkey('CTRL+ALT+SHIFT+F8'), 0x0700 | 0x77)
        self.assertEqual(yakala._windows_link_hotkey('ctrl+shift+7'), 0x0300 | ord('7'))
        for bad in ('CTRL+ALT+Ş', 'CTRL+ALT+F25', 'CTRL+ALT+BB'):
            with self.assertRaises(ValueError):
                yakala._windows_link_hotkey(bad)
        with patch.object(yakala.subprocess, 'run', side_effect=self.shell_save) as run:
            yakala._windows_shortcut(self.link, 'pythonw.exe', 'pencere', hotkey='CTRL+ALT+B')
        self.assertIn('$l.Hotkey = ' + str(0x0642), self.script(run))

    def test_a_failed_write_leaves_no_empty_link_behind(self):
        failure = subprocess.CalledProcessError(1, ['powershell.exe'])
        with patch.object(yakala.subprocess, 'run', side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                yakala._windows_shortcut(self.link, 'pythonw.exe', 'pencere')
        self.assertFalse(self.link.exists())

    def test_failed_or_empty_rewrite_preserves_the_previous_link(self):
        self.link.parent.mkdir()
        self.link.write_bytes(b'old-link')
        for error in (subprocess.TimeoutExpired('powershell', 60), OSError('COM failed'), None):
            with patch.object(yakala.subprocess, 'run', side_effect=error), self.assertRaises((OSError, subprocess.TimeoutExpired)):
                yakala._windows_shortcut(self.link, 'pythonw.exe', 'pencere')
            self.assertEqual(self.link.read_bytes(), b'old-link')
            self.assertEqual(list(self.link.parent.glob('.yakala-*.lnk')), [])


def powershell(script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            capture_output=True, timeout=120)
    if result.returncode:
        raise AssertionError('PowerShell: ' + result.stderr.decode('utf-8', 'replace')[-1500:])


def read_shortcut(link, scratch):
    """Target, arguments and hotkey as the shell reads them back from the file, character for character.

    Through Shell.Application, not WScript.Shell: that one answers in the ANSI code page too.
    """
    out, link = Path(scratch) / 'shortcut.json', Path(link)
    powershell("$ErrorActionPreference = 'Stop'\n"
               '$l = (New-Object -ComObject Shell.Application).NameSpace(' + yakala._ps_quote(link.parent) + ').ParseName(' +
               yakala._ps_quote(link.name) + ').GetLink\n'
               '$o = @{ target = $l.Path; arguments = $l.Arguments; hotkey = [int]$l.Hotkey }\n'
               '[IO.File]::WriteAllText(' + yakala._ps_quote(out) + ', (ConvertTo-Json $o), (New-Object Text.UTF8Encoding $false))')
    return json.loads(out.read_text(encoding='utf-8'))


def split_arguments(line):
    """The argument vector the Windows parser makes of a shortcut's argument string."""
    from ctypes import wintypes
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    count = ctypes.c_int()
    parsed = shell32.CommandLineToArgvW('program ' + line, ctypes.byref(count))
    try:
        return [parsed[index] for index in range(1, count.value)]
    finally:
        kernel32.LocalFree(parsed)


def hotkey_user32():
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32


def try_hotkey(spec):
    """(registered, error) for a combination taken and released on this thread."""
    user32 = hotkey_user32()
    modifiers, key = yakala._windows_hotkey_vk(spec)
    if not user32.RegisterHotKey(None, 77, modifiers, key):
        return False, ctypes.get_last_error()
    user32.UnregisterHotKey(None, 77)
    return True, 0


@unittest.skipUnless(WINDOWS, 'real Win32: RegisterHotKey, named mutex and event, shell link shortcuts')
class WindowsListenerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-win-', ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # Spaces, both apostrophes, an ampersand, Turkish letters and an emoji in the vault path.
        self.vault = self.root / "Örnek Beyin & Şifre'nin \u2019 📥 kasası"
        self.other = self.root / 'Başka Beyin'
        for number, vault in enumerate((self.vault, self.other)):
            vault.mkdir()
            state = self.root / ('state-' + str(number))
            (vault / '.beyin-runtime.json').write_text(json.dumps({'state': str(state)}), encoding='utf-8')
        self.state = self.root / 'state-0'
        self.env = inherited_env(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', APPDATA=str(self.root / 'appdata'))
        appdata = patch.dict(os.environ, {'APPDATA': str(self.root / 'appdata')})
        appdata.start()
        self.addCleanup(appdata.stop)
        self.processes = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        yakala._windows_stop_listener(timeout=10)
        for process in self.processes:
            if process.poll() is None:
                process.kill()  # by the handle this test owns, never by a process id
            process.wait(30)

    def need_hotkeys(self):
        registered, error = try_hotkey(KEY)
        if not registered:
            reason = 'this session cannot register ' + KEY + ' (Windows error ' + str(error) + ')'
            sys.stderr.write('\nyakala windows test skipped: ' + reason + '\n')
            self.skipTest(reason)

    def listener(self, vault, spec=KEY):
        process = subprocess.Popen([sys.executable, str(MODULE), 'dinle', '--vault', str(vault), '--tus', spec],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=self.env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.processes.append(process)
        return process

    def test_listener_round_trip(self):
        self.need_hotkeys()
        self.assertFalse(yakala._windows_listener_running(self.vault))
        process = self.listener(self.vault)
        self.assertTrue(wait_until(lambda: yakala._windows_listener_running(self.vault) or process.poll() is not None))
        self.assertIsNone(process.poll())
        self.assertTrue(yakala._windows_listener_running(self.vault))
        self.assertFalse(yakala._windows_listener_running(self.other))
        self.assertEqual(try_hotkey(KEY), (False, 1409))  # ERROR_HOTKEY_ALREADY_REGISTERED: the listener holds it
        self.assertEqual(self.listener(self.vault).wait(60), 0)  # this vault's listener already runs
        self.assertEqual(self.listener(self.other, 'ctrl+alt+shift+f8').wait(60), 1)  # one listener per session
        self.assertIn('Baska bir vault', (self.root / 'state-1/yakala/dinleyici.log').read_text(encoding='utf-8'))
        yakala._windows_stop_listener(self.other, timeout=2)  # another vault's uninstall
        self.assertIsNone(process.poll())
        yakala._windows_stop_listener(self.vault, timeout=30)
        self.assertEqual(process.wait(60), 0)
        self.assertFalse(yakala._windows_listener_running(self.vault))
        self.assertEqual(try_hotkey(KEY), (True, 0))  # and the key is free again
        yakala._windows_stop_listener(self.vault, timeout=2)  # twice is harmless

    def test_a_taken_key_exits_once_with_a_reason(self):
        self.need_hotkeys()
        user32 = hotkey_user32()
        modifiers, key = yakala._windows_hotkey_vk(KEY)
        self.assertTrue(user32.RegisterHotKey(None, 78, modifiers, key))  # this test is the other application
        try:
            process = self.listener(self.vault)
            self.assertEqual(process.wait(60), 1)
            self.assertFalse(yakala._windows_listener_running(self.vault))
            self.assertFalse(yakala._windows_listener_ok(self.vault, process, timeout=30))
        finally:
            user32.UnregisterHotKey(None, 78)
        self.assertIn(KEY + ' (Windows hata 1409)', (self.state / 'yakala/dinleyici.log').read_text(encoding='utf-8'))
        # The failed start left no session lock behind: the next one can run.
        process = self.listener(self.vault)
        self.assertTrue(wait_until(lambda: yakala._windows_listener_running(self.vault) or process.poll() is not None))
        self.assertIsNone(process.poll())

    def test_key_press_opens_the_window(self):
        self.need_hotkeys()
        result = {}
        def run():
            try:
                result['code'] = yakala.listen_windows(self.vault, MODULE, KEY, self.state)
            except SystemExit as exc:
                result['code'] = exc.code
        broker = Mock(error=None)
        broker.press.return_value = True
        with patch.object(yakala, '_WindowsPopupBroker', return_value=broker) as factory:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(wait_until(lambda: factory.called, 10))
                self.assertTrue(hotkey_user32().PostThreadMessageW(thread.native_id, 0x0312, 1, 0))
                self.assertTrue(wait_until(lambda: broker.press.called, 10))
            finally:
                yakala._windows_stop_listener(self.vault, timeout=10)
                thread.join(10)
        self.assertEqual(result, {'code': 0})
        sent = broker.press.call_args.args[0]
        self.assertEqual(sent.get('schema'), 2)
        self.assertIn('hwnd', sent)
        self.assertIn('pid', sent)
        self.assertNotIn('url', sent)  # the hotkey thread never calls UIA
        broker.close.assert_called_once()

    def test_shortcut_content_and_clean_rewrite(self):
        link = self.root / 'kısayollar' / 'Beyne At Dinleyici deneme.lnk'
        runner = self.state / 'yakala' / MODULE.name
        command = [str(runner), 'dinle', '--vault', str(self.vault), '--tus', KEY]
        yakala._windows_shortcut(link, sys.executable, yakala._argline(command), hotkey='CTRL+ALT+B')
        written = read_shortcut(link, self.root)
        self.assertEqual(os.path.normcase(written['target']), os.path.normcase(sys.executable))
        self.assertEqual(written['arguments'], yakala._argline(command))
        self.assertEqual(split_arguments(written['arguments']), command)
        self.assertTrue(written['hotkey'])  # a 3.9.0 Start menu entry carried the key like this
        yakala._windows_shortcut(link, sys.executable, yakala._argline(command))
        self.assertEqual(read_shortcut(link, self.root)['hotkey'], 0)


@unittest.skipUnless(WINDOWS, 'real Win32: `kur`, `durum` and `kaldir` with the listener')
class WindowsInstalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-wininst-', ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault, self.state, self.appdata = self.root / 'Örnek Beyin', self.root / 'state', self.root / 'appdata'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(self.root / 'home'), APPDATA=str(self.appdata))
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        appdata = patch.dict(os.environ, {'APPDATA': str(self.appdata)})
        appdata.start()
        self.addCleanup(appdata.stop)
        self.addCleanup(yakala._windows_stop_listener, timeout=10)

    def entry(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                env=self.env, cwd=self.vault, timeout=180)
        self.assertEqual(result.returncode, 0, (args, result.stderr))
        return json.loads(result.stdout)

    def test_install_status_and_uninstall(self):
        programs = self.appdata / 'Microsoft/Windows/Start Menu/Programs'
        start, startup = programs / 'Beyne At.lnk', programs / 'Startup'
        link = yakala._windows_listener_link(self.vault)
        self.assertEqual(link.parent, startup)
        # What 3.9.0 left: the key inside the Start menu entry. Plus another vault's startup entry
        # and the unsuffixed name an earlier build of this change used.
        yakala._windows_shortcut(start, sys.executable, 'eski', hotkey='CTRL+ALT+SHIFT+F8')
        self.assertTrue(read_shortcut(start, self.root)['hotkey'])
        stale = [startup / 'Beyne At Dinleyici 00000000.lnk', startup / 'Beyne At Dinleyici.lnk']
        for path in stale:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')
        self.assertIsNone(self.entry('durum')['dinleyici_calisiyor'])  # no listener of this vault: unknown, not "stopped"

        can_register, error = try_hotkey(KEY)
        installed = self.entry('kur', '--tus', KEY)
        self.assertEqual(installed['kisayol'], 'Ctrl+Alt+Shift+F9')
        if can_register:
            self.assertTrue(installed['kisayol_calisiyor'])
        else:
            sys.stderr.write('\nyakala windows test: no hotkey in this session (Windows error ' + str(error) +
                             '); listener start not asserted\n')
        self.assertTrue(link.is_file())
        self.assertEqual([path.exists() for path in stale], [False, False])  # the key moved to this vault
        self.assertEqual(read_shortcut(start, self.root)['hotkey'], 0)  # the listener owns the key now
        written = read_shortcut(link, self.root)
        runner = self.state.resolve() / 'yakala' / MODULE.name
        self.assertEqual(split_arguments(written['arguments']),
                         [str(runner), 'dinle', '--vault', str(self.vault.resolve()), '--tus', KEY])
        self.assertIn(Path(written['target']).name.lower(), ('pythonw.exe', 'python.exe'))
        self.assertTrue(runner.is_file())

        before = (snapshot(self.vault), snapshot(self.state), snapshot(self.appdata))
        status = self.entry('durum')
        self.assertEqual((snapshot(self.vault), snapshot(self.state), snapshot(self.appdata)), before)  # read-only
        self.assertEqual(status['dinleyici_calisiyor'], installed['kisayol_calisiyor'])
        self.assertEqual(yakala._windows_listener_running(self.vault), installed['kisayol_calisiyor'])
        if can_register:
            self.assertEqual(try_hotkey(KEY), (False, 1409))

        # Another vault's uninstall leaves this vault's listener and startup entry alone.
        other = self.root / 'Başka Beyin'
        other.mkdir()
        (other / '.beyin-runtime.json').write_text(json.dumps({'state': str(self.root / 'state-other')}), encoding='utf-8')
        yakala.uninstall(other, self.root / 'state-other')
        self.assertTrue(link.is_file() and start.is_file())
        self.assertEqual(yakala._windows_listener_running(self.vault), installed['kisayol_calisiyor'])

        removed = self.entry('kaldir')
        self.assertIn(link.name, removed['kaldirilan'])
        self.assertFalse(link.exists() or runner.exists())
        self.assertFalse(yakala._windows_listener_running(self.vault))
        if can_register:
            self.assertEqual(try_hotkey(KEY), (True, 0))
        self.assertIsNone(self.entry('durum')['dinleyici_calisiyor'])
        self.assertEqual(self.entry('kaldir')['kaldirilan'], [])  # twice is harmless


if __name__ == '__main__':
    unittest.main()
