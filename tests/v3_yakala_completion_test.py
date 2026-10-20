"""Windows capture completion: pure policies, real subprocess pipes, and in-memory COM/GPU models."""
import contextlib
import ctypes as C
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as y


def windows_os():
    return types.SimpleNamespace(**{**vars(os), 'name': 'nt'})


def png(width=2, height=2):
    import zlib
    head = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    body = zlib.compress((b'\0' + b'\x11\x22\x33' * width) * height)
    return y.PNG_MAGIC + y._png_chunk(b'IHDR', head) + y._png_chunk(b'IDAT', body) + y._png_chunk(b'IEND', b'')


def wait_for(check, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return bool(check())


class WindowsPolicyTest(unittest.TestCase):
    def test_hovered_link_is_the_default_and_the_page_stays_selectable(self):
        context = {'url': 'https://www.youtube.com/', 'baslik': 'YouTube',
                   'url_imlec': 'https://www.youtube.com/watch?v=RgvsbigEylQ', 'baslik_imlec': 'Fragman'}
        sources = y.windows_sources(context, '', False)
        chosen = y._source_arguments(sources)
        self.assertEqual((chosen['url'], chosen['title']), ('https://www.youtube.com/watch?v=RgvsbigEylQ', 'Fragman'))
        page = next(source for source in sources if source['key'] == 's')
        y._source_toggle(sources, page)
        self.assertEqual(y._source_arguments(sources)['extra_urls'], ['https://www.youtube.com/'])
        y._source_toggle(sources, sources[0])
        self.assertEqual(y._source_arguments(sources)['url'], 'https://www.youtube.com/')

    def test_copied_page_link_marks_the_page_source_and_the_hovered_link_stays_default(self):
        context = {'url': 'https://example.org/page', 'url_imlec': 'https://example.org/hover'}
        sources = y.windows_sources(context, 'https://example.org/page', True)
        self.assertEqual(len([source for source in sources if source['kind'] == 'url']), 2)  # no duplicate row
        chosen = [source for source in sources if source['on']]
        self.assertEqual([(source['key'], source['clipboard']) for source in chosen], [('i', False)])
        page = next(source for source in sources if source['key'] == 's')
        self.assertTrue(page['clipboard'])  # choosing it consumes the clipboard, the hovered link never does

    def test_hovered_link_alone_is_a_link_and_no_window_picture_is_preselected(self):
        shot = dict(path='window.png', width=2, height=2, method='WGC')
        sources = y.windows_sources({'url_imlec': 'https://example.org/hover'}, '', False, shot=shot)
        self.assertEqual([source['key'] for source in sources if source['on']], ['i'])
        with patch.object(y, '_windows_clipboard', return_value={}), \
                patch.object(y, 'clipboard_fresh', return_value=False), \
                patch.object(y, 'clipboard_image', return_value=None), \
                patch.object(y, 'windows_enrich', return_value={'url_imlec': 'https://example.org/hover'}), \
                patch.object(y, 'window_image', return_value=shot) as image:
            y._collect_windows({}, '/unused', '/unused')
        image.assert_called_once_with({'url_imlec': 'https://example.org/hover'}, cancel=None)

    def test_a_window_switch_drops_the_hovered_link_too(self):
        context = {'hwnd': 42, 'uygulama': 'chrome', 'pencere': 'Old - Google Chrome', 'imlec': [10, 20]}
        with patch.object(y, '_win_foreground', side_effect=[42, 0]), \
                patch.object(y, 'windows_browser_url', return_value=None), \
                patch.object(y, 'windows_hovered_link', return_value=('https://example.org/hover', 'Video')) as hover:
            result = y.windows_enrich(context)
        self.assertNotIn('url_imlec', result)
        self.assertNotIn('baslik_imlec', result)
        self.assertEqual(hover.call_args.args[2], [10, 20])  # the key press position, not the pointer now

    def test_hovered_link_reader_needs_a_chromium_window_and_a_web_link(self):
        self.assertEqual(y.windows_hovered_link(42, 'notepad'), (None, None))
        for returned, expected in (({'value': 'https://example.org/a', 'name': ' Bir\nvideo '}, ('https://example.org/a', 'Bir video')),
                                   ({'value': 'javascript:void(0)'}, (None, None)), ({}, (None, None))):
            with patch.object(y, '_windows_read', return_value=returned):
                self.assertEqual(y.windows_hovered_link(42, 'chrome'), expected)
        with patch.object(y, '_windows_read', return_value={}) as read:
            y.windows_hovered_link(42, 'chrome', (5, -7))
        self.assertEqual(read.call_args.args[1], {'hwnd': 42, 'at': [5, -7]})
        with patch.object(y, '_windows_read') as read:
            y.windows_hovered_link(42, 'notepad')
        read.assert_not_called()  # the isolated reader is never started outside a Chromium browser

    def test_domain_boundary_and_nonstandard_port_are_not_aliases(self):
        page = 'https://youtube.com/watch?v=abcdefghijk'
        for other in ('https://notyoutube.com/watch?v=abcdefghijk', 'https://youtube.com.evil.org/watch?v=abcdefghijk',
                      'https://youtube.com:8443/watch?v=abcdefghijk'):
            self.assertNotEqual(y.canonical(page), y.canonical(other))
        self.assertEqual(y.canonical(page), y.canonical('https://music.youtube.com/watch?v=abcdefghijk'))

    def test_ambiguous_video_query_and_different_general_pages_stay_distinct(self):
        self.assertNotEqual(y.canonical('https://youtube.com/watch?v=abcdefghijk&v=lmnopqrstuv'), 'youtube:abcdefghijk')
        distinct = ('https://example.org/x', 'https://example.org:444/x', 'https://example.org/x#/a',
                    'https://example.org/x#/b', 'https://example.org/y', 'https://example.org/x?q=1')
        self.assertEqual(len(set(map(y.canonical, distinct))), len(distinct))
        same = ('http://example.org/x', 'https://www.example.org/x/', 'https://example.org/x#section')
        self.assertEqual(len(set(map(y.canonical, same))), 1)

    def test_ipv6_hosts_with_ports_stay_distinct(self):
        self.assertNotEqual(y.canonical('https://[2001:4860:4860::8888]:8443/x'),
                            y.canonical('https://[2001:4860:4860::8888:8443]/x'))

    def test_windows_link_with_an_attached_picture_stays_a_private_link_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, picture = Path(tmp) / 'vault', Path(tmp) / 'shot.png'
            vault.mkdir()
            picture.write_bytes(png())
            with patch.object(y, 'os', windows_os()):
                saved = y.capture(vault, url='https://www.youtube.com/watch?v=abcdefghijk', files=[picture])
            card = y.find_card(vault, saved['id'])
            self.assertEqual((card['meta']['kaynak_turu'], card['meta']['visibility']), ('youtube', 'private'))

    def test_canonical_is_the_same_on_every_system(self):
        for name in ('nt', 'posix'):
            with patch.object(y, 'os', types.SimpleNamespace(**{**vars(os), 'name': name})):
                self.assertEqual(y.canonical('https://www.example.org/x/'), 'example.org/x')
                self.assertEqual(y.canonical('https://x.com/b/status/42?s=20'), 'x:42')

    def test_window_image_is_offered_but_never_selected_unasked(self):
        shot = dict(path='window.png', width=2, height=2, method='WGC')
        image = dict(path='clip.png', width=2, height=2, on=True)
        sources = y.windows_sources({}, 'old text', False, image, shot)
        self.assertEqual([s['key'] for s in sources if s['on']], [])
        self.assertIn('w', [s['key'] for s in sources])
        y._source_toggle(sources, next(s for s in sources if s['key'] == 'w'))
        self.assertEqual(y._source_arguments(sources)['files'], ['window.png'])

    def test_browser_link_wins_over_a_fresh_clipboard_which_stays_one_key_away(self):
        sources = y.windows_sources({'url': 'https://example.org/page'}, 'https://example.org/copy', True)
        selected = [s for s in sources if s['on']]
        self.assertEqual([(s['key'], s['clipboard']) for s in selected], [('s', False)])
        y._source_toggle(sources, next(s for s in sources if s['key'] == 'p'))
        self.assertEqual([(s['key'], s['clipboard']) for s in sources if s['on']], [('s', False), ('p', True)])
        without_link = y.windows_sources({}, 'https://example.org/copy', True)
        self.assertEqual([s['key'] for s in without_link if s['on']], ['p'])  # no browser link: fresh clipboard leads

    def test_same_page_and_copied_link_have_one_selected_source(self):
        sources = y.windows_sources({'url': 'https://example.org/'}, 'https://example.org/', True)
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]['on'] and sources[0]['clipboard'])

    def test_generation_restart_cancel_and_consumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            self.assertFalse(y.clipboard_fresh(tmp, 10, state, 10, 'first'))
            self.assertTrue(y.clipboard_fresh(tmp, 11, state, 10, 'first'))
            self.assertTrue(y.clipboard_fresh(tmp, 11, state, 10, 'first'))  # opening/cancel is read-only
            self.assertFalse(state.exists())
            y._clipboard_saved(tmp, 11, state, 'first')
            self.assertFalse(y.clipboard_fresh(tmp, 11, state, 10, 'first'))
            self.assertTrue(y.clipboard_fresh(tmp, 12, state, 10, 'first'))
            self.assertFalse(y.clipboard_fresh(tmp, 5, state, 5, 'new-session'))
            self.assertFalse(y.clipboard_fresh(tmp, 0, state, 10, 'first'))

    def test_source_change_after_hotkey_never_selects_new_clipboard(self):
        with patch.object(y, '_windows_clipboard', return_value={'text': 'later', 'sequence': 12}), \
                patch.object(y, 'clipboard_fresh', return_value=True), \
                patch.object(y, 'clipboard_image', return_value=None), \
                patch.object(y, 'windows_enrich', side_effect=lambda ctx, cancel=None: ctx), \
                patch.object(y, 'window_image', return_value=None):
            got = y._collect_windows({'pano_sira': 11}, '/unused', '/unused')
        self.assertFalse(any(s['on'] for s in got['sources']))

    def test_verified_page_also_offers_an_unselected_screenshot(self):
        shot = dict(path='window.png', width=2, height=2)
        with patch.object(y, '_windows_clipboard', return_value={}), \
                patch.object(y, 'clipboard_fresh', return_value=False), \
                patch.object(y, 'clipboard_image', return_value=None), \
                patch.object(y, 'windows_enrich', return_value={'url': 'https://example.org/page'}), \
                patch.object(y, 'window_image', return_value=shot) as image:
            result = y._collect_windows({}, '/unused', '/unused')
        image.assert_called_once_with({'url': 'https://example.org/page'}, cancel=None)
        self.assertIs(result['shot'], shot)
        self.assertFalse(next(s for s in result['sources'] if s['key'] == 'w')['on'])
        self.assertEqual(y._source_arguments(result['sources'])['url'], 'https://example.org/page')

    def test_url_read_after_a_foreground_switch_is_discarded(self):
        with patch.object(y, '_win_foreground', side_effect=[42, 0]), \
                patch.object(y, 'windows_browser_url', return_value='https://example.org/wrong'):
            result = y.windows_enrich({'hwnd': 42, 'uygulama': 'chrome', 'pencere': 'Old - Google Chrome'})
        self.assertNotIn('url', result)
        self.assertNotIn('baslik', result)

    def test_failed_source_acquisition_removes_owned_image(self):
        image = {'folder': 'private'}
        with patch.object(y, '_windows_clipboard', return_value={}), \
                patch.object(y, 'clipboard_fresh', return_value=False), \
                patch.object(y, 'clipboard_image', return_value=image), \
                patch.object(y, 'windows_enrich', side_effect=OSError), patch.object(y, '_image_done') as done:
            with self.assertRaises(OSError):
                y._collect_windows({}, '/unused', '/unused')
        self.assertIn(((image,), {}), [(c.args, c.kwargs) for c in done.call_args_list])


class CommitAndFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.system = patch.object(y, 'os', windows_os())
        self.system.start()
        self.addCleanup(self.system.stop)

    def test_extra_urls_survive_the_commit_lock_and_a_replay_is_still_idempotent(self):
        saved = y.capture(self.vault, url='https://example.org/page', extra_urls=['https://example.org/quote'],
                          state=self.state, capture_id='a' * 32)
        replay = y.capture(self.vault, url='https://example.org/page', extra_urls=['https://example.org/other'],
                           state=self.state, capture_id='a' * 32)
        self.assertEqual(replay['status'], 'ayni_yakalama')
        self.assertEqual(y.find_card(self.vault, saved['id'], self.state)['meta']['ek_urller'],
                         ['https://example.org/quote'])

    def test_same_id_after_a_success_never_appends_or_copies_twice(self):
        file = self.vault / 'source.txt'
        file.write_text('private', encoding='utf-8')
        first = y.capture(self.vault, url='https://example.org/x', files=[file], why='one',
                          state=self.state, capture_id='a' * 32)
        second = y.capture(self.vault, url='https://example.org/x', files=[file], why='two',
                           state=self.state, capture_id='a' * 32)
        self.assertEqual(first['id'], second['id'])
        cards = y.cards(self.vault, self.state)
        self.assertEqual(len(cards), 1)
        self.assertNotIn('two', cards[0]['body'])
        self.assertNotIn('Yeniden yakalandi', cards[0]['body'])
        self.assertEqual(y._capture_ids(cards[0]['meta']), ['a' * 32])
        attached = [p for p in (cards[0]['path'].parent / 'dosyalar').iterdir() if not p.name.startswith('.')]
        self.assertEqual(len(attached), 1)
        y.capture(self.vault, url='https://example.org/x', why='new capture', state=self.state, capture_id='e' * 32)
        again = y.capture(self.vault, url='https://example.org/x', why='OLD REPLAY', state=self.state, capture_id='a' * 32)
        self.assertEqual(again['status'], 'ayni_yakalama')
        self.assertNotIn('OLD REPLAY', y.cards(self.vault, self.state)[0]['body'])

    def test_two_threads_with_one_id_commit_once(self):
        barrier, results, errors = threading.Barrier(3), [], []
        def work():
            try:
                barrier.wait()
                results.append(y.capture(self.vault, text='one', state=self.state, capture_id='b' * 32))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(y.cards(self.vault, self.state)), 1)

    def test_failed_card_replace_preserves_old_and_removes_staging(self):
        target = self.vault / 'card.md'
        target.write_text('original', encoding='utf-8')
        with patch.object(y.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                y.write_card(target, {'private': True}, 'SECRET')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), ['card.md'])

    def test_failed_fdopen_closes_descriptor_and_staging(self):
        real, descriptors = y.tempfile.mkstemp, []
        def created(*args, **kw):
            fd, path = real(*args, **kw)
            descriptors.append(fd)
            return fd, path
        with patch.object(y.tempfile, 'mkstemp', side_effect=created), \
                patch.object(y.os, 'fdopen', side_effect=OSError('fdopen failed')):
            with self.assertRaises(OSError):
                y.write_card(self.vault / 'a.md', {}, 'SECRET')
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_raw_replace_failure_cleans_staging(self):
        with patch.object(y.os, 'replace', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                y._write_raw(self.vault / 'raw.md', 'SECRET')
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_clipboard_state_write_failure_leaves_no_staging(self):
        with patch.object(y.os, 'replace', side_effect=PermissionError):
            y._clipboard_saved(self.vault, 12, self.state, 'session')
        self.assertEqual(list((self.state / 'yakala').iterdir()), [])

    def test_failed_commit_removes_new_attachment_but_not_original(self):
        source = self.vault / 'a.txt'
        source.write_text('private', encoding='utf-8')
        with patch.object(y, 'write_card', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                y.capture(self.vault, files=[source], state=self.state, capture_id='c' * 32)
        self.assertEqual(source.read_text(), 'private')
        self.assertEqual([p for p in y.inbox(self.vault, self.state).rglob('a.txt')], [])

    def test_failed_file_copy_rolls_back_previous_attachments(self):
        source = self.vault / 'a.txt'
        source.write_text('private', encoding='utf-8')
        with self.assertRaises(ValueError):
            y.capture(self.vault, files=[source, self.vault / 'missing'], state=self.state, capture_id='d' * 32)
        self.assertEqual(list(y.inbox(self.vault, self.state).rglob('a.txt')), [])


class ReaderProcessTest(unittest.TestCase):
    def command(self, body):
        return [sys.executable, '-c', body]

    def success(self):
        return self.command("import sys,struct,json;sys.stdin.buffer.read();h=json.dumps({'value':{'text':'ok'},'blobs':[]}).encode();sys.stdout.buffer.write(struct.pack('<I',len(h))+h)")

    def test_timeout_kills_reader_and_next_read_succeeds(self):
        owners = []
        original = y._OwnedProcess
        def start(*a, **k):
            item = original(*a, **k)
            owners.append(item)
            return item
        with patch.object(y, '_OwnedProcess', side_effect=start):
            before = time.monotonic()
            result = y._process_packet(self.command('import time;time.sleep(30)'), {}, 0.3)
            self.assertEqual(result.get('error'), 'timeout')
            self.assertLess(time.monotonic() - before, 3)
            self.assertIsNotNone(owners[0].process.poll())
            self.assertEqual(y._process_packet(self.success(), {}, 3), {'text': 'ok'})
        self.assertEqual(len(owners), 2)

    def test_cancel_kills_reader(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        try:
            result = y._process_packet(self.command('import time;time.sleep(30)'), {}, 10, event)
        finally:
            timer.join()
        self.assertEqual(result.get('error'), 'cancelled')

    def test_no_launch_for_already_cancelled_request(self):
        event = threading.Event()
        event.set()
        with patch.object(y, '_OwnedProcess') as spawn:
            y._process_packet(self.success(), {}, 3, event)
        spawn.assert_not_called()

    def test_malformed_child_reply_and_child_crash_are_not_content(self):
        self.assertEqual(y._process_packet(self.command("print('not a packet')"), {}, 3), {})
        self.assertEqual(y._process_packet(self.command('raise SystemExit(4)'), {}, 3)['error'], 'reader-exit')

    def test_packet_roundtrip_and_boundaries(self):
        value = dict(text='Türkçe', png=b'PNG', dib=b'DIB')
        self.assertEqual(y._parse_read_packet(y._read_packet(value)), value)
        for raw in (b'', b'abcd', struct.pack('<I', 1000000), y._read_packet(value)[:-1], y._read_packet(value) + b'x'):
            self.assertEqual(y._parse_read_packet(raw), {})
        with patch.object(y, 'WIN_MESSAGE_LIMIT', 5):
            with self.assertRaises(ValueError):
                y._read_packet({'text': 'long'})

    def test_large_unicode_text_uses_bounded_binary_transport(self):
        value = {'text': 'Türkçe 🧠 ' * 20000, 'sequence': 100}
        packet = y._read_packet(value)
        self.assertLess(struct.unpack_from('<I', packet)[0], 1024)
        self.assertEqual(y._parse_read_packet(packet), value)
        with patch.object(y, 'WIN_TEXT_LIMIT', 10):
            with self.assertRaises(ValueError):
                y._read_packet(value)
            self.assertEqual(y._parse_read_packet(packet), {})

    def test_private_endpoint_never_needs_a_vault(self):
        # Linux rejects native requests, but the private protocol is still the only output.
        if os.name == 'nt':
            request = {'hwnd': 0}
        else:
            request = {}
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'beyin_v3_yakala.py'), '_win-read', 'wgc'],
                              input=json.dumps(request).encode(), capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(y._parse_read_packet(proc.stdout).get('error'), 'ValueError')


class ImageBackendTest(unittest.TestCase):
    def setUp(self):
        self.context = {'schema': 2, 'hwnd': 42}
        self.valid = dict(png=png(), width=2, height=2)

    def test_wgc_success_skips_printwindow_and_labels_source(self):
        with patch.object(y, '_win_foreground', return_value=42), patch.object(y, '_windows_read', return_value=self.valid) as read:
            image = y.window_image(self.context)
        self.addCleanup(y._image_done, image)
        self.assertEqual(image['method'], 'WGC')
        self.assertEqual([c.args[0] for c in read.call_args_list], ['wgc'])

    def test_wgc_failure_falls_back_only_to_printwindow(self):
        for failure in ({}, {'error': 'timeout'}, {'png': b'broken', 'width': 2, 'height': 2}):
            with patch.object(y, '_win_foreground', return_value=42), \
                    patch.object(y, '_windows_read', side_effect=[failure, self.valid]) as read:
                image = y.window_image(self.context)
            self.assertEqual(image['method'], 'PrintWindow')
            self.assertEqual([c.args[0] for c in read.call_args_list], ['wgc', 'printwindow'])
            y._image_done(image)

    def test_both_backends_fail_without_desktop_fallback(self):
        with patch.object(y, '_win_foreground', return_value=42), patch.object(y, '_windows_read', return_value={}) as read:
            self.assertIsNone(y.window_image(self.context))
        self.assertEqual([c.args[0] for c in read.call_args_list], ['wgc', 'printwindow'])

    def test_switch_after_frame_discards_it(self):
        with patch.object(y, '_win_foreground', side_effect=[42, 42, 0]), \
                patch.object(y, '_windows_read', return_value=self.valid), patch.object(y.tempfile, 'mkdtemp') as folder:
            self.assertIsNone(y.window_image(self.context))
        folder.assert_not_called()

    def test_oversized_png_never_reaches_tk(self):
        header = struct.pack('>IIBBBBB', 100000, 100000, 8, 2, 0, 0, 0)
        data = y.PNG_MAGIC + y._png_chunk(b'IHDR', header) + y._png_chunk(b'IDAT', b'x') + y._png_chunk(b'IEND', b'')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'image.png'
            path.write_bytes(data)
            fake = types.SimpleNamespace(PhotoImage=Mock(), TclError=RuntimeError)
            with patch.dict(sys.modules, tkinter=fake):
                self.assertIsNone(y._image_thumb(None, dict(path=path, width=100000, height=100000)))
            fake.PhotoImage.assert_not_called()

    def test_duplicate_png_header_and_excess_pixels_are_rejected(self):
        first = png()
        duplicate = first[:33] + first[8:33] + first[33:]
        self.assertIsNone(y.png_info(duplicate))
        with patch.object(y, 'IMAGE_PIXEL_LIMIT', 3):
            self.assertIsNone(y.png_info(first))
            dib = struct.pack('<IiiHHIIiiII', 40, 2, 2, 1, 32, 0, 16, 0, 0, 0, 0) + b'\0' * 16
            self.assertIsNone(y.dib_to_png(dib))

    def test_thumbnail_opens_only_a_validated_picture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image.png'
            path.write_bytes(png())
            photo = Mock()
            photo.width.return_value, photo.height.return_value = 2, 2
            fake = types.SimpleNamespace(PhotoImage=Mock(return_value=photo), TclError=RuntimeError)
            with patch.dict(sys.modules, tkinter=fake), patch.object(y, 'os', windows_os()):
                self.assertIs(y._image_thumb(None, dict(path=path, width=2, height=2)), photo)
                self.assertEqual(fake.PhotoImage.call_args.kwargs['file'], path)
                fake.PhotoImage.reset_mock()
                self.assertIsNone(y._image_thumb(None, dict(path=path, width=3, height=2)))  # size must match
                fake.PhotoImage.assert_not_called()


class ProtocolClientTest(unittest.TestCase):
    def make(self, packets):
        root = Mock()
        pipe = Mock()
        pipe.failed = threading.Event()
        pipe.receive.side_effect = list(packets) + [None] * 20
        pipe.send.return_value = True
        session = y._WindowSession(root, 'a' * 32, pipe)
        return session, pipe, root

    def activate(self, **changes):
        value = dict(capture_id='b' * 32, context={'schema': 2, 'hwnd': 42}, source_hwnd=42)
        value.update(changes)
        return y._protocol_message('ACTIVATE', 'a' * 32, **value)

    def test_ready_has_instance_schema_version_native_hwnd_and_pid(self):
        session, pipe, root = self.make([])
        with patch.object(y, '_win_hwnd', return_value=123):
            session.ready()
        message = pipe.send.call_args.args[0]
        self.assertTrue(y._protocol_valid(message, 'a' * 32, ('READY',)))
        self.assertEqual((message['hwnd'], message['pid']), (123, os.getpid()))

    def test_activate_once_then_focus_keeps_original_context(self):
        focus = y._protocol_message('FOCUS', 'a' * 32, capture_id='b' * 32, source_hwnd=77)
        session, pipe, root = self.make([self.activate(), focus])
        session.focus = Mock()
        session.poll()
        self.assertEqual(session.context, {'schema': 2, 'hwnd': 42})
        self.assertEqual(session.source, 77)
        self.assertEqual(pipe.send.call_args.args[0]['type'], 'ACTIVE')
        session.focus.assert_called_once_with(77)

    def test_old_schema_version_instance_and_wrong_capture_are_rejected(self):
        for field, value in (('schema', 2), ('version', 'old'), ('instance', 'c' * 32)):
            message = self.activate()
            message[field] = value
            session, _, _ = self.make([message])
            session.poll()
            self.assertTrue(session.cancel.is_set(), field)
            self.assertIsNone(session.context)
        message = y._protocol_message('FOCUS', 'a' * 32, capture_id='c' * 32, source_hwnd=77)
        session, _, _ = self.make([self.activate(), message])
        session.poll()
        self.assertTrue(session.cancel.is_set())

    def test_replayed_activate_is_not_a_second_capture(self):
        session, _, _ = self.make([self.activate(), self.activate()])
        session.poll()
        self.assertTrue(session.cancel.is_set())
        self.assertEqual(session.capture_id, 'b' * 32)

    def test_parent_eof_and_stop_cancel_the_owned_popup(self):
        for packet in (None, y._protocol_message('STOP', 'a' * 32)):
            session, pipe, root = self.make([packet] if packet else [])
            if not packet:
                pipe.failed.set()
            session.dismiss = Mock()
            session.poll()
            self.assertTrue(session.cancel.is_set())
            session.dismiss.assert_called_once()


FAKE_POPUP = r'''
import json, os, sys, time
instance, path, mode, version, schema = sys.argv[1:]
schema = int(schema)
capture = None
def record(message):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(dict(pid=os.getpid(), **message))+'\n')
def send(kind, **fields):
    print(json.dumps(dict(type=kind, instance=instance, schema=schema, version=version, **fields)), flush=True)
record(dict(event='spawn'))
send('READY', hwnd=111, pid=os.getpid())
if mode == 'unread':
    time.sleep(30)
else:
    for line in sys.stdin.buffer:
        message=json.loads(line)
        record(message)
        if message['type']=='ACTIVATE':
            capture=message['capture_id']
            send('ACTIVE', capture_id=capture)
        elif message['type']=='STOP':
            if capture:
                send('DONE', capture_id=capture, status='vazgecildi')
            break
'''


class BrokerPipeTest(unittest.TestCase):
    def start(self, mode='normal', schema=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        script, log = root / 'popup.py', root / 'packets.jsonl'
        script.write_text(FAKE_POPUP, encoding='utf-8')
        grants, problems = [], []
        def command(*args):
            instance = args[args.index('--instance') + 1]
            return [sys.executable, str(script), instance, str(log), mode, y.WIN_CAPTURE_VERSION,
                    str(y.WIN_CAPTURE_SCHEMA if schema is None else schema)]
        with patch.object(y, '_clipboard_sequence', return_value=10):
            broker = y._WindowsPopupBroker(command, lambda pid: grants.append(pid) or True, problems.append,
                                            validate_window=lambda hwnd, pid: hwnd == 111 and pid > 0)
        self.addCleanup(broker.close)
        def records():
            try:
                return [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]
            except (OSError, ValueError):
                return []
        return broker, records, grants, problems

    def test_real_pipe_one_popup_repeated_keys_and_cooperative_shutdown(self):
        broker, records, grants, problems = self.start()
        context = dict(schema=2, hwnd=42, pencere='original')
        self.assertTrue(broker.press(context))
        self.assertTrue(wait_for(lambda: any(r.get('type') == 'ACTIVATE' for r in records())))
        self.assertTrue(wait_for(lambda: broker.pipe is not None and not broker.pipe.incoming.qsize()))
        for _ in range(10):
            self.assertTrue(broker.press(dict(schema=2, hwnd=99, pencere='other window')))
        self.assertTrue(wait_for(lambda: any(r.get('type') == 'FOCUS' for r in records())))
        sent = records()
        self.assertEqual(sum(r.get('event') == 'spawn' for r in sent), 1)
        activations = [r for r in sent if r.get('type') == 'ACTIVATE']
        self.assertEqual(len(activations), 1)
        self.assertEqual(activations[0]['context']['pencere'], 'original')
        self.assertTrue(all(r['capture_id'] == activations[0]['capture_id'] for r in sent if r.get('type') == 'FOCUS'))
        self.assertTrue(grants)
        broker.close()
        self.assertFalse(broker.thread.is_alive())
        self.assertTrue(any(r.get('type') == 'STOP' for r in records()))
        self.assertEqual(problems, [])

    def test_old_ready_schema_is_rejected_without_activation(self):
        broker, records, _, problems = self.start(schema=2)
        broker.press(dict(schema=2, hwnd=42))
        self.assertTrue(wait_for(lambda: broker.error is not None))
        broker.close()
        self.assertFalse(any(r.get('type') == 'ACTIVATE' for r in records()))
        self.assertTrue(problems)

    def test_unread_pipe_never_blocks_the_hotkey_caller(self):
        broker, records, _, problems = self.start(mode='unread')
        context = dict(schema=2, hwnd=42, pencere='x' * 24000)
        before = time.monotonic()
        for _ in range(40):
            self.assertTrue(broker.press(context))
        self.assertLess(time.monotonic() - before, 0.6)
        self.assertTrue(wait_for(lambda: bool(records())))
        before = time.monotonic()
        broker.close()
        self.assertLess(time.monotonic() - before, 4)
        self.assertFalse(broker.thread.is_alive())
        self.assertEqual(sum(r.get('event') == 'spawn' for r in records()), 1)


class AbiBasicsTest(unittest.TestCase):
    def test_windows_struct_sizes_are_not_host_c_long_sizes(self):
        t = y._win_abi_types()
        self.assertEqual(C.sizeof(t.GUID), 16)
        self.assertEqual(C.sizeof(t.SIZE), 8)
        self.assertEqual(C.sizeof(t.TEXTURE), 44)
        self.assertEqual(t.MAPPED.RowPitch.offset, C.sizeof(C.c_void_p))
        self.assertEqual(C.sizeof(t.MAPPED), 16 if C.sizeof(C.c_void_p) == 8 else 12)

    def test_guid_bytes_follow_windows_little_endian_layout(self):
        for value in y.WGC_IIDS.values():
            self.assertEqual(bytes(y._abi_guid(value)), uuid.UUID(value).bytes_le)

    def test_slots_against_sdk_inherited_method_order(self):
        expected = dict(QueryInterface=0, Release=2, Close=6, CreateForWindow=3, ItemSize=7,
                        IsSupported=6, CreateFreeThreaded=6, TryGetNextFrame=7, CreateCaptureSession=10,
                        StartCapture=6, Surface=6, ContentSize=8, GetInterface=3, CreateTexture2D=5,
                        GetDesc=10, CopyResource=47, Map=14, Unmap=15)
        self.assertEqual(y.WGC_SLOTS, expected)

    def test_raw_vtable_call_uses_signed_hresult_and_exact_slot(self):
        called = []
        callback_type = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_int32)
        good = callback_type(lambda obj, n: called.append(n) or 0)
        bad = callback_type(lambda obj, n: -2147467259)
        table = (C.c_void_p * 2)(C.cast(good, C.c_void_p), C.cast(bad, C.c_void_p))
        instance = (C.c_void_p * 1)(C.cast(table, C.c_void_p))
        with patch.object(C, 'WINFUNCTYPE', C.CFUNCTYPE, create=True):
            self.assertEqual(y._abi_call(C.cast(instance, C.c_void_p), 0, (C.c_int32,), (17,)), 0)
            with self.assertRaisesRegex(OSError, '80004005'):
                y._abi_call(C.cast(instance, C.c_void_p), 1, (C.c_int32,), (17,))
        self.assertEqual(called, [17])

    def test_row_pitch_padding_is_not_saved_as_pixels(self):
        buffer = C.create_string_buffer(b'abcdefghPAD!ijklmnopPAD!')
        self.assertEqual(y._mapped_rows(C.addressof(buffer), 12, 2, 2, 2, 2), b'abcdefghijklmnop')
        for args in ((7, 2, 2, 2, 2), (12, 3, 2, 2, 2), (12, 2, 3, 2, 2)):
            with self.assertRaises(ValueError):
                y._mapped_rows(C.addressof(buffer), *args)


if __name__ == '__main__':
    unittest.main()
