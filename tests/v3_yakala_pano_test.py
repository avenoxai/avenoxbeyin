"""Clipboard image for the capture popup (#274): Windows screenshots become a file card, not a path note.

The conversion and the decision are plain functions, tested on every OS with synthetic bitmaps and a
strict PNG reader. The popup wiring runs against a scripted stand-in for tkinter, so no window opens.
The real Windows clipboard is exercised only on a Windows CI runner (or with BEYIN_TEST_PANO=1),
because those tests replace whatever the clipboard holds.
"""
import contextlib
import ctypes
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
import zlib

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INBOX = '📥 000-Inbox/Yakala'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402

BYTE_OF = {0xFF: 0, 0xFF00: 1, 0xFF0000: 2, 0xFF000000: 3}
REPORTED = ('C:/Users/Özdemir/AppData/Local/Packages/MicrosoftWindows.Client.Core_cw5n1h2txyewy/'
            'TempState/ScreenClip/{4F1C2A6E-0B7D-4C55-9E21-7A3D5B8C9F10}.png')


def picture(width, height):
    """Top-down rows of distinct (red, green, blue) pixels."""
    return [[((10 + 40 * x + y) % 256, (20 + 3 * x + 50 * y) % 256, (200 - 7 * x - 11 * y) % 256) for x in range(width)]
            for y in range(height)]


def make_dib(pixels, bits=24, top_down=False, compression=0, masks=None, header=40, table=0, fourth=0,
             repeat_masks=False, slack=0):
    """Packed DIB as CF_DIB carries it; padding bytes are 0xEE so a reader that touches them shows up."""
    height, width, step = len(pixels), len(pixels[0]), bits // 8
    red, green, blue = [BYTE_OF[mask] for mask in masks] if masks else (2, 1, 0)
    stride = (width * bits + 31) // 32 * 4
    lines = []
    for row in pixels:
        line = bytearray(b'\xee' * stride)
        for x, (r, g, b) in enumerate(row):
            if step == 4:
                line[x * 4:x * 4 + 4] = bytes([fourth(x) if callable(fourth) else fourth]) * 4
            line[x * step + red], line[x * step + green], line[x * step + blue] = r, g, b
        lines.append(bytes(line))
    if not top_down:
        lines.reverse()
    head = bytearray(struct.pack('<IiiHHIIiiII', header, width, -height if top_down else height, 1, bits,
                                 compression, stride * height, 2835, 2835, table, 0) + bytes(header - 40))
    if masks and header == 40:
        head += struct.pack('<III', *masks)
    elif masks:
        head[40:52] = struct.pack('<III', *masks)
        if repeat_masks:
            head += struct.pack('<III', *masks)
    return bytes(head) + b'\x11' * (table * 4) + b''.join(lines) + bytes(slack)


def decode_png(data):
    """Strict reader for the PNGs under test: 8-bit RGB or RGBA, unfiltered rows, every CRC verified."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n', data[:8]
    position, chunks = 8, []
    while position < len(data):
        length = struct.unpack('>I', data[position:position + 4])[0]
        kind, body = data[position + 4:position + 8], data[position + 8:position + 8 + length]
        assert zlib.crc32(kind + body) == struct.unpack('>I', data[position + 8 + length:position + 12 + length])[0], kind
        chunks.append((kind, body))
        position += 12 + length
    assert position == len(data) and chunks[0][0] == b'IHDR' and chunks[-1] == (b'IEND', b''), [kind for kind, _ in chunks]
    width, height, depth, colour, compression, filtering, interlace = struct.unpack('>IIBBBBB', chunks[0][1])
    assert (depth, compression, filtering, interlace) == (8, 0, 0, 0) and colour in (2, 6), chunks[0][1]
    channels = 3 if colour == 2 else 4
    raw = zlib.decompress(b''.join(body for kind, body in chunks if kind == b'IDAT'))
    stride = width * channels + 1
    assert len(raw) == height * stride, (len(raw), height, stride)
    rows = []
    for index in range(height):
        line = raw[index * stride:(index + 1) * stride]
        assert line[0] == 0, 'filtered row'
        rows.append([tuple(line[1 + at:1 + at + channels]) for at in range(0, width * channels, channels)])
    return width, height, channels, rows


def make_png(pixels, alpha=200):
    """An RGBA PNG written by the test itself: the kind of payload another app puts on the clipboard."""
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    raw = b''.join(b'\x00' + b''.join(bytes(pixel) + bytes([alpha]) for pixel in row) for row in pixels)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', len(pixels[0]), len(pixels), 8, 6, 0, 0, 0))
            + chunk(b'tEXt', b'Software\x00test') + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class BitmapToPngTest(unittest.TestCase):
    def convert(self, pixels, **options):
        made = yakala.dib_to_png(make_dib(pixels, **options))
        self.assertIsNotNone(made, options)
        png, width, height = made
        decoded = decode_png(png)
        self.assertEqual(decoded[:3], (width, height, 3))
        self.assertEqual((width, height), (len(pixels[0]), len(pixels)))
        self.assertEqual(yakala.png_info(png), (width, height, len(png)))
        return decoded[3]

    def test_24_bit_bottom_up_with_row_padding(self):
        for width in (1, 2, 3, 4, 5):  # strides 4, 8, 12, 12, 16: one to three padding bytes, or none
            pixels = picture(width, 3)
            self.assertEqual(self.convert(pixels), pixels, width)

    def test_24_bit_top_down(self):
        pixels = picture(5, 4)
        self.assertEqual(self.convert(pixels, top_down=True), pixels)

    def test_32_bit_rgb_is_opaque_whatever_the_fourth_byte_holds(self):
        pixels = picture(4, 3)
        for fourth in (0, 255, lambda x: (0, 77, 255, 1)[x]):  # all zero after a screen grab; garbage elsewhere
            for top_down in (False, True):
                self.assertEqual(self.convert(pixels, bits=32, top_down=top_down, fourth=fourth), pixels)

    def test_32_bit_bitfields_in_any_byte_order(self):
        pixels = picture(3, 2)
        bgr, rgb, shifted = (0xFF0000, 0xFF00, 0xFF), (0xFF, 0xFF00, 0xFF0000), (0xFF000000, 0xFF0000, 0xFF00)
        for masks in (bgr, rgb, shifted):
            self.assertEqual(self.convert(pixels, bits=32, compression=3, masks=masks), pixels, masks)
            self.assertEqual(self.convert(pixels, bits=32, compression=3, masks=masks, top_down=True), pixels, masks)

    def test_v4_and_v5_headers_keep_masks_inside_and_tolerate_a_repeated_copy(self):
        pixels = picture(3, 2)
        masks = (0xFF0000, 0xFF00, 0xFF)
        for header in (108, 124):
            self.assertEqual(self.convert(pixels, bits=32, header=header), pixels)
            self.assertEqual(self.convert(pixels, bits=32, header=header, compression=3, masks=masks), pixels)
            self.assertEqual(self.convert(pixels, bits=32, header=header, compression=3, masks=masks, repeat_masks=True), pixels)

    def test_colour_table_and_allocation_slack_are_skipped(self):
        pixels = picture(3, 3)
        self.assertEqual(self.convert(pixels, table=2), pixels)
        self.assertEqual(self.convert(pixels, bits=32, table=3, slack=13), pixels)  # GlobalSize rounds up

    def test_other_layouts_are_refused_not_guessed(self):
        pixels = picture(3, 2)
        good = make_dib(pixels)
        self.assertIsNotNone(yakala.dib_to_png(good))
        refused = {
            'empty': b'', 'short header': good[:39], 'missing rows': good[:-1],
            'core header': struct.pack('<IHHHH', 12, 3, 2, 1, 24) + bytes(64),
            '8 bit': good[:14] + struct.pack('<H', 8) + good[16:],
            '16 bit': good[:14] + struct.pack('<H', 16) + good[16:],
            'rle': good[:16] + struct.pack('<I', 1) + good[20:],
            'png inside': good[:16] + struct.pack('<I', 5) + good[20:],
            'zero width': good[:4] + struct.pack('<i', 0) + good[8:],
            'zero height': good[:8] + struct.pack('<i', 0) + good[12:],
            'negative width': good[:4] + struct.pack('<i', -3) + good[8:],
            'huge width': good[:4] + struct.pack('<i', 70000) + good[8:],
            'bitfields at 24 bit': good[:16] + struct.pack('<I', 3) + good[20:],
            'ten bit masks': make_dib(pixels, bits=32, compression=3, masks=(0xFF0000, 0xFF00, 0xFF))[:40]
                             + struct.pack('<III', 0x3FF00000, 0xFFC00, 0x3FF) + bytes(64),
            'same mask twice': make_dib(pixels, bits=32, compression=3, masks=(0xFF0000, 0xFF00, 0xFF))[:40]
                               + struct.pack('<III', 0xFF, 0xFF, 0xFF00) + bytes(64),
        }
        for name, data in refused.items():
            self.assertIsNone(yakala.dib_to_png(data), name)

    def test_png_info_checks_structure_and_every_crc(self):
        pixels = picture(4, 2)
        png = make_png(pixels)
        self.assertEqual(yakala.png_info(png), (4, 2, len(png)))
        self.assertEqual(yakala.png_info(png + bytes(40)), (4, 2, len(png)))  # allocation slack after IEND
        self.assertEqual(yakala.png_info(bytearray(png)), (4, 2, len(png)))
        flipped = bytearray(png)
        flipped[len(png) // 2] ^= 1
        without_pixels = png[:33] + png[-12:]
        for name, data in {'empty': b'', 'bitmap': make_dib(pixels), 'cut': png[:-5], 'no end': png[:-12],
                           'flipped bit': bytes(flipped), 'no pixels': without_pixels, 'jpeg': b'\xff\xd8\xff\xe0' + bytes(64)}.items():
            self.assertIsNone(yakala.png_info(data), name)


class SnipPathTest(unittest.TestCase):
    def test_only_a_packaged_app_temp_picture_path_matches(self):
        back = REPORTED.replace('/', '\\')
        packages = 'd:/Users/a/AppData/Local/Packages/'
        for text in (REPORTED, back, '"' + back + '"', '  ' + REPORTED + '\n', REPORTED.upper(),
                     packages + 'MicrosoftWindows.Client.CBS_cw5n1h2txyewy/TempState/ScreenClip/x.PNG',
                     packages + 'Microsoft.ScreenSketch_8wekyb3d8bbwe/TempState/Ekran görüntüsü 2026-10-11.jpg'):
            self.assertEqual(yakala.snip_path(text), text.strip().strip('"'), text)
        for text in ('', None, 'not', 'https://ornek.com/Packages/a/TempState/ScreenClip/a.png', 'C:/Users/a/Pictures/Screenshots/a.png',
                     'C:/Users/a/Desktop/ScreenClip/a.png', 'C:/Users/a/TempState/a.png', packages + 'Uygulama_x/LocalState/a.png',
                     REPORTED + '\n' + REPORTED, 'bak: ' + REPORTED, REPORTED + ' buna bak', REPORTED[:-4] + '.json',
                     REPORTED.replace('C:/', '/mnt/c/'), REPORTED + '\nC:/Users/a/not.png'):
            self.assertIsNone(yakala.snip_path(text), text)


class ScreenshotFiles:
    """A folder laid out like the Snipping Tool's temp store, on whatever system runs the test."""

    def make_store(self, prefix):
        self.tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(self.tmp.cleanup)
        if os.name != 'nt':  # a temp path here has no drive letter; the rest of the pattern stays as shipped
            relaxed = patch.object(yakala, 'SNIP_TEMP', re.compile(
                r'(?:[a-z]:)?/(?:[^/\r\n]+/)*packages/[^/\r\n]+/tempstate/(?:[^/\r\n]+/)*[^/\r\n]+\.png', re.I), create=True)
            relaxed.start()
            self.addCleanup(relaxed.stop)

    def shot_file(self, age=None, content=None, now=None):
        """Path of a temp screenshot `age` seconds old (as Tk reports it); the file is absent when age is None."""
        path = Path(self.tmp.name) / 'Packages' / 'Ornek.Uygulama_abc' / 'TempState' / 'ScreenClip' / '{0A1B}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        if age is not None:
            now = time.time() if now is None else now
            path.write_bytes(make_png(self.pixels) if content is None else content)
            os.utime(path, (now - age, now - age))
        return path.as_posix()


class ImageOfferTest(ScreenshotFiles, unittest.TestCase):
    """clipboard_image(): what the popup may offer, decided from the text Tk read and the raw payload."""

    def setUp(self):
        self.make_store('v3-yakala-pano-')
        self.pixels = picture(6, 4)
        self.now = 1_800_000_000.0

    def shot_file(self, age=None, content=None):
        return super().shot_file(age, content, self.now)

    def offer(self, clip, payload, context=None):
        with patch.object(yakala, '_clipboard_payload', return_value=payload) as probe:
            image = yakala.clipboard_image(clip, context or {}, at=self.now)
        if image:
            self.addCleanup(yakala._image_done, image)
        return image, probe

    def test_picture_without_a_temp_file_is_offered_switched_off(self):
        # A copied web image or an old PrtScn has no age: an agent reads what is saved, so the user decides.
        image, _ = self.offer('', {'image': True, 'png': None, 'dib': make_dib(self.pixels, bits=32)})
        self.assertEqual((image['width'], image['height'], image['age'], image['stale'], image['on']), (6, 4, None, False, False))
        path = Path(image['path'])
        self.assertRegex(path.name, r'^pano-goruntusu-\d{8}-\d{6}\.png$')
        self.assertEqual(decode_png(path.read_bytes())[3], self.pixels)
        self.assertEqual(yakala.image_lines(image, True), ('Panodaki görüntü', '6×4 · Tab ile çıkar'))
        self.assertEqual(yakala.image_lines(image, False), ('Panodaki görüntü eklenmeyecek', '6×4 · Tab ile ekle'))

    def test_png_payload_is_stored_byte_for_byte_and_wins_over_the_bitmap(self):
        png = make_png(self.pixels)
        other = make_dib(picture(2, 2))
        image, _ = self.offer('', {'image': True, 'png': png + bytes(24), 'dib': other})
        self.assertEqual(Path(image['path']).read_bytes(), png)
        self.assertEqual((image['width'], image['height']), (6, 4))
        broken, _ = self.offer('', {'image': True, 'png': png[:-3], 'dib': other})
        self.assertEqual(decode_png(Path(broken['path']).read_bytes())[3], picture(2, 2))

    def test_fresh_screenshot_drops_its_own_path_and_stays_on(self):
        clip = self.shot_file(age=95)
        image, _ = self.offer(clip, {'image': True, 'png': None, 'dib': make_dib(self.pixels)})
        self.assertEqual((image['age'], image['stale'], image['on']), (95.0, False, True))
        self.assertEqual(yakala.image_lines(image, True)[1], '6×4 · 1 dk önce · Tab ile çıkar')
        quoted, _ = self.offer('"' + clip + '"', {'image': True, 'png': None, 'dib': make_dib(self.pixels)})
        self.assertTrue(quoted['on'])

    def test_old_screenshot_is_offered_switched_off(self):
        for age, label in ((yakala.IMAGE_FRESH + 1, '10 dk önce'), (3 * 3600 + 20, '3 saat önce'), (3 * 86400, '3 gün önce')):
            image, _ = self.offer(self.shot_file(age=age), {'image': True, 'png': make_png(self.pixels), 'dib': None})
            self.assertEqual((image['stale'], image['on']), (True, False), age)
            self.assertEqual(yakala.image_lines(image, False)[1], '6×4 · ' + label + ' · Tab ile ekle')
        edge, _ = self.offer(self.shot_file(age=yakala.IMAGE_FRESH), {'image': True, 'png': make_png(self.pixels), 'dib': None})
        self.assertTrue(edge['on'])
        ahead, _ = self.offer(self.shot_file(age=-30), {'image': True, 'png': make_png(self.pixels), 'dib': None})
        self.assertEqual((ahead['age'], ahead['on']), (0.0, True))  # clock moved back: not older than now

    def test_deleted_temp_file_means_an_old_screenshot(self):
        image, _ = self.offer(self.shot_file(age=None), {'image': True, 'png': make_png(self.pixels), 'dib': None})
        self.assertEqual((image['age'], image['stale'], image['on']), (None, True, False))
        self.assertEqual(yakala.image_lines(image, False)[1], '6×4 · eski, geçici dosyası silinmiş · Tab ile ekle')

    def test_unreadable_bitmap_falls_back_to_the_screenshot_file(self):
        clip = self.shot_file(age=20)
        for payload in ({'image': True, 'png': None, 'dib': None},  # over the size limit
                        {'image': True, 'png': None, 'dib': make_dib(self.pixels)[:14] + struct.pack('<H', 16) + bytes(200)}):
            image, _ = self.offer(clip, payload)
            self.assertEqual(Path(image['path']).read_bytes(), make_png(self.pixels))
            self.assertTrue(image['on'])
        self.assertNotEqual(Path(image['path']).parent, Path(clip).parent)  # a copy; the original is never ours to delete
        not_png = self.shot_file(age=20, content=b'GIF89a' + bytes(64))
        self.assertIsNone(self.offer(not_png, {'image': True, 'png': None, 'dib': None})[0])

    def test_anything_else_changes_nothing(self):
        bitmap = {'image': True, 'png': None, 'dib': make_dib(self.pixels)}
        clip = self.shot_file(age=5)
        # No picture, no reader on this OS, clipboard busy, a layout the reader refuses.
        for payload in (None, {}, {'image': False, 'png': None, 'dib': None}, {'image': True, 'png': None, 'dib': None},
                        {'image': True, 'png': b'bozuk', 'dib': b'bozuk'}):
            self.assertIsNone(self.offer('', payload)[0], payload)
        for payload in (None, {'image': False, 'png': None, 'dib': None}):
            self.assertIsNone(self.offer(clip, payload)[0], payload)  # the path alone, without a picture, stays text
        # Real text or a link is the capture, as before; the clipboard is not even opened.
        for text in ('toplantı notu', 'https://ornek.com/yazi', clip + '\nikinci satır', 'bak ' + clip):
            image, probe = self.offer(text, bitmap)
            self.assertIsNone(image, text)
            probe.assert_not_called()
        # Something better already comes from the context.
        for context in ({'url': 'https://ornek.com'}, {'dosyalar': ['a.pdf']}, {'metin': 'secili metin'}):
            image, probe = self.offer('', bitmap, context)
            self.assertIsNone(image, context)
            probe.assert_not_called()

    def test_reader_failure_is_swallowed_and_leaves_no_temp_folder(self):
        made, real = [], tempfile.mkdtemp

        def recording(**options):
            made.append(real(**options))
            return made[-1]
        with patch.object(yakala, '_clipboard_payload', side_effect=OSError('exception: access violation')):
            self.assertIsNone(yakala.clipboard_image('', {}))
        with patch.object(yakala, '_clipboard_payload', return_value={'image': True, 'png': make_png(self.pixels), 'dib': None}), \
                patch.object(yakala.tempfile, 'mkdtemp', recording), \
                patch.object(yakala.Path, 'write_bytes', side_effect=OSError('disk dolu')):
            self.assertIsNone(yakala.clipboard_image('', {}))
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())

    @unittest.skipIf(os.name == 'nt', 'Windows has the reader')
    def test_unsupported_systems_never_get_an_image(self):
        # Do not inspect a real Linux clipboard while running synthetic tests.
        with patch.object(yakala.sys, 'platform', 'darwin'):
            self.assertIsNone(yakala._clipboard_payload())
            self.assertIsNone(yakala.clipboard_image('', {}))
            self.assertIsNone(yakala.clipboard_image(REPORTED, {}))

    def test_age_words(self):
        self.assertEqual([yakala._ago(seconds) for seconds in (0, 59, 60, 3599, 3600, 172799, 172800)],
                         ['az önce', 'az önce', '1 dk önce', '59 dk önce', '1 saat önce', '47 saat önce', '2 gün önce'])


class Call:
    """A foreign function stand-in: callable, and accepts the argtypes/restype the reader declares."""

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeWin32:
    """user32 and kernel32 in one: a clipboard of {format: bytes} that checks how the reader treats it."""

    def __init__(self, formats, busy=0, slack=0, unlockable=()):
        self.formats, self.busy, self.slack, self.unlockable = dict(formats), busy, slack, set(unlockable)
        self.is_open, self.locked, self.asked, self.closed, self.buffers = False, set(), [], 0, {}
        for name in ('OpenClipboard', 'CloseClipboard', 'IsClipboardFormatAvailable', 'RegisterClipboardFormatW',
                     'GetClipboardData', 'GlobalSize', 'GlobalLock', 'GlobalUnlock', 'DragQueryFileW',
                     'GetClipboardSequenceNumber'):
            setattr(self, name, Call(getattr(self, '_' + name)))

    def _GetClipboardSequenceNumber(self):
        return 42

    def _OpenClipboard(self, _window):
        assert not self.is_open
        if self.busy:
            self.busy -= 1
            return 0
        self.is_open = True
        return 1

    def _CloseClipboard(self):
        assert self.is_open and not self.locked, 'closed while a payload was still locked'
        self.is_open, self.closed = False, self.closed + 1
        return 1

    def _RegisterClipboardFormatW(self, name):
        return {'PNG': 0xC001, 'image/png': 0xC002}[name]

    def _IsClipboardFormatAvailable(self, code):
        assert self.is_open
        return int(code in self.formats)

    def _GetClipboardData(self, code):
        assert self.is_open
        self.asked.append(code)
        return None if self.formats[code] is None else 0x7FF000000000 + code  # None: listed, but the owner gives nothing

    def _GlobalSize(self, handle):
        data = self.formats[handle - 0x7FF000000000]
        if isinstance(data, Exception):
            raise data
        return len(data) + self.slack

    def _GlobalLock(self, handle):
        import ctypes
        assert self.is_open and handle not in self.locked
        code = handle - 0x7FF000000000
        if code in self.unlockable:
            return None
        self.locked.add(handle)
        self.buffers[code] = ctypes.create_string_buffer(self.formats[code] + bytes(self.slack), len(self.formats[code]) + self.slack)
        return ctypes.addressof(self.buffers[code])

    def _GlobalUnlock(self, handle):
        self.locked.remove(handle)
        return 0

    def _DragQueryFileW(self, handle, index, buffer, size):
        data = self.formats.get(handle - 0x7FF000000000) if handle else None
        if not data or len(data) < 20:
            return 0
        pFiles = struct.unpack_from('<I', data, 0)[0]
        raw = data[pFiles:]
        paths = [p for p in raw.decode('utf-16-le', 'ignore').split('\0') if p]
        if index == 0xFFFFFFFF:
            return len(paths)
        if 0 <= index < len(paths):
            path = paths[index]
            if buffer is None or size == 0:
                return len(path)
            for i, ch in enumerate(path[:size - 1]):
                buffer[i] = ch
            buffer[min(len(path), size - 1)] = '\0'
            return min(len(path), size - 1)
        return 0


class WindowsReaderFlowTest(unittest.TestCase):
    """_win_clipboard_payload() against a recording stand-in for user32/kernel32, on every OS.

    The real calls run only on Windows (WindowsClipboardTest); this pins the order: open with a bounded
    retry, copy each payload under its own lock, always close, never lock what is over the limit.
    """

    def read(self, formats, limit=None, **options):
        import ctypes
        snapshot = options.pop('snapshot', False)
        fake = FakeWin32(formats, **options)
        with patch.object(ctypes, 'WinDLL', lambda _name: fake, create=True), patch.object(time, 'sleep') as sleep:
            payload = yakala._win_clipboard_payload(**({'limit': limit} if limit else {}), snapshot=snapshot)
        self.assertFalse(fake.is_open)
        self.assertEqual(fake.locked, set())
        return payload, fake, sleep

    def test_bitmap_is_copied_with_its_allocation_slack_and_the_clipboard_closed(self):
        dib = make_dib(picture(3, 2))
        payload, fake, _ = self.read({8: dib}, slack=5)
        self.assertEqual(payload, {'image': True, 'png': None, 'dib': dib + bytes(5)})
        self.assertEqual((fake.asked, fake.closed), ([8], 1))
        self.assertEqual(decode_png(yakala.dib_to_png(payload['dib'])[0])[3], picture(3, 2))

    def test_valid_png_is_enough_and_a_broken_one_falls_back_to_the_bitmap(self):
        png, dib = make_png(picture(3, 2)), make_dib(picture(3, 2))
        for code in (0xC001, 0xC002):  # 'PNG' and 'image/png'
            payload, fake, _ = self.read({code: png, 8: dib}, slack=3)
            self.assertEqual(payload, {'image': True, 'png': png + bytes(3), 'dib': None})
            self.assertEqual(fake.asked, [code])
        payload, fake, _ = self.read({0xC001: png[:-2], 8: dib})
        self.assertEqual((payload['png'], payload['dib']), (png[:-2], dib))
        self.assertEqual(fake.asked, [0xC001, 8])

    def test_busy_clipboard_is_retried_a_bounded_number_of_times(self):
        dib = make_dib(picture(2, 2))
        payload, fake, sleep = self.read({8: dib}, busy=3)
        self.assertEqual((payload['dib'], sleep.call_count, fake.closed), (dib, 3, 1))
        payload, fake, sleep = self.read({8: dib}, busy=99)
        self.assertIsNone(payload)
        self.assertEqual((sleep.call_count, fake.closed, fake.asked), (5, 0, []))
        self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 0.5)

    def test_oversized_missing_and_unlockable_payloads_are_skipped_not_fatal(self):
        dib = make_dib(picture(4, 4))
        empty = {'image': True, 'png': None, 'dib': None}
        payload, fake, _ = self.read({8: dib}, limit=len(dib) - 1)
        self.assertEqual((payload, fake.buffers), (empty, {}))  # never locked, never copied
        self.assertEqual(self.read({8: dib}, limit=len(dib))[0]['dib'], dib)
        self.assertEqual(self.read({8: None})[0], empty)
        self.assertEqual(self.read({8: b''})[0], empty)
        self.assertEqual(self.read({8: dib}, unlockable=[8])[0], empty)
        self.assertEqual(self.read({13: b'm\0e\0t\0i\0n\0\0\0'})[0], {'image': False, 'png': None, 'dib': None})
        self.assertEqual(self.read({})[0], {'image': False, 'png': None, 'dib': None})

    def test_error_inside_the_reader_still_closes_the_clipboard(self):
        import ctypes
        fake = FakeWin32({8: OSError('exception: access violation reading 0x0000000000000000')})
        with patch.object(ctypes, 'WinDLL', lambda _name: fake, create=True):
            with self.assertRaises(OSError):
                yakala._win_clipboard_payload()
            self.assertEqual((fake.is_open, fake.closed), (False, 1))
            with patch.object(yakala, '_clipboard_payload', yakala._win_clipboard_payload):
                self.assertIsNone(yakala.clipboard_image('', {}))  # the popup sees "no picture", not the error
        self.assertEqual(fake.closed, 2)

    def test_file_drop_format_is_extracted_when_snapshot_requested(self):
        with tempfile.NamedTemporaryFile(suffix='.txt') as tmp1, tempfile.NamedTemporaryFile(suffix='.pdf') as tmp2:
            paths = [tmp1.name, tmp2.name]
            drop = file_drop(*paths)
            payload, fake, _ = self.read({15: drop}, snapshot=True)
            self.assertEqual(payload.get('dosyalar'), paths)
            self.assertEqual(payload.get('text'), '')
            self.assertFalse(payload['image'])

    def test_file_drop_is_ignored_when_snapshot_not_requested(self):
        with tempfile.NamedTemporaryFile(suffix='.txt') as tmp:
            payload, fake, _ = self.read({15: file_drop(tmp.name)}, snapshot=False)
            self.assertNotIn('dosyalar', payload)
            self.assertFalse(payload['image'])


class Widget:
    """Stand-in for a Tk widget: every call the test does not care about is accepted and ignored."""

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return lambda *args, **options: None


class Root(Widget):
    def __init__(self, script):
        self.script, self.bound, self.timers, self.seen, self.alive = script, {}, [], [], True

    def bind(self, sequence, handler, add=None):
        self.bound[sequence] = handler

    def after(self, _delay, handler, *args):
        self.timers.append((handler, args))

    def winfo_screenwidth(self):
        return 1440

    def winfo_screenheight(self):
        return 900

    def geometry(self, value):
        self.size = value

    def destroy(self):
        self.alive = False

    def mainloop(self):
        for action, value in self.script:
            if action in ('key', 'root-key'):
                if value.startswith(('<Alt-', '<Option-')) and value not in self.bound:
                    modifier, key = value[1:-1].split('-', 1)
                    event = types.SimpleNamespace(keysym=key, keycode=ord(key.upper()), state=0x20000, char=key)
                    handlers = self.bound if action == 'root-key' else self.entry.handlers
                    handlers['<' + modifier + '-KeyPress>'](event)
                else:
                    self.bound[value](None)
            elif action == 'alt':
                self.entry.handlers['<Alt-KeyPress>'](types.SimpleNamespace(**value))
            elif action == 'event':  # a root binding with real event fields, e.g. a modified Esc
                sequence, fields = value
                self.bound[sequence](types.SimpleNamespace(**fields))
            elif action == 'type':
                self.entry.type(value)
            else:
                self.seen.append((self.entry.text, self.canvas.texts()))
        for _ in range(400):
            if not (self.alive and self.timers):
                break
            handler, args = self.timers.pop(0)
            handler(*args)


class Entry(Widget):
    def __init__(self, root, **_options):
        self.text, self.handlers = '', {}
        root.entry = self

    def bind(self, sequence, handler, add=None):
        self.handlers[sequence] = handler

    def delete(self, _first, _last=None):
        self.text = ''

    def insert(self, _index, text):
        self.text += text

    def get(self):
        return self.text

    def type(self, text):
        self.handlers['<KeyPress>'](types.SimpleNamespace(char=text[0], keysym=text[0]))
        self.insert('end', text)
        self.handlers['<KeyRelease>'](None)


class Canvas(Widget):
    def __init__(self, root, **_options):
        self.items = {}
        self.options, self.clicks = dict(_options), {}
        if 'canvas' not in vars(root):
            root.canvas = self

    def configure(self, **options):
        self.options.update(options)

    def yview_scroll(self, step, units):
        self.options['last_scroll'] = (step, units)

    def yview_moveto(self, fraction):
        self.options['last_position'] = fraction

    def tag_bind(self, item, event=None, handler=None):
        if handler:
            self.clicks[item, event] = handler
        return self.clicks.get((item, event))

    def _create(self, *_coordinates, **options):
        self.items[len(self.items) + 1] = dict(options)
        return len(self.items)
    create_text = create_polygon = create_oval = create_line = create_window = create_image = _create

    def itemconfigure(self, item, **options):
        self.items[item].update(options)

    def texts(self):
        texts = [item['text'] for item in self.items.values() if item.get('text')]
        for item in self.items.values():
            if isinstance(item.get('window'), Canvas):
                texts.extend(item['window'].texts())
        return texts


class Photo:
    opened = []

    def __init__(self, master=None, file=None, size=(1920, 1080), data=None, format=None):
        self.size = size
        if data:
            assert yakala.png_info(data)
        if file:
            assert yakala.png_info(Path(file).read_bytes()), file  # the temp PNG exists while the window is up
            Photo.opened.append(file)

    def width(self):
        return self.size[0]

    def height(self):
        return self.size[1]

    def subsample(self, factor):
        return Photo(size=(-(-self.size[0] // factor), -(-self.size[1] // factor)))


class PopupWiringTest(ScreenshotFiles, unittest.TestCase):
    """popup() end to end with a scripted tkinter: which card a key sequence produces."""

    def setUp(self):
        self.make_store('v3-yakala-pencere-')
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()
        self.pixels = picture(8, 5)
        self.bitmap = {'image': True, 'png': None, 'dib': make_dib(self.pixels, bits=32)}
        Photo.opened = []

    def popup(self, clip, payload, script, context=None, windows=False, fresh=False, shot=None, platform=None):
        roots = []
        fake = types.ModuleType('tkinter')
        fake.Tk = lambda: roots.append(Root(script)) or roots[-1]
        fake.Canvas, fake.Entry, fake.PhotoImage, fake.TclError = Canvas, Entry, Photo, RuntimeError
        fake.font = types.ModuleType('tkinter.font')
        fake.font.Font = lambda **_options: types.SimpleNamespace(measure=lambda text: len(text) * 6)
        environment = {key: value for key, value in os.environ.items() if key != 'BEYIN_YAKALA_SNAPSHOT'}
        if not windows and os.name == 'nt' and platform is None:
            # On a Windows host the non-Windows model skips the desktop path this way; never on macOS,
            # where the variable starts the development screencapture of the (fake) window.
            environment['BEYIN_YAKALA_SNAPSHOT'] = 'model'
        # Replace only the capture module's os reference; pathlib keeps the host's real os.name.
        name = 'posix' if platform in ('darwin', 'linux') else ('nt' if windows else os.name)
        system = types.SimpleNamespace(**{**vars(os), 'name': name})
        # Grouped so the with statement stays under Python's static nesting limit.
        platform_patches = contextlib.ExitStack()
        platform_patches.enter_context(patch.object(sys, 'platform', platform or sys.platform))
        platform_patches.enter_context(patch.object(yakala, '_style_mac'))
        if platform in ('darwin', 'linux') and os.name == 'nt':
            # The commit lock's OS primitive belongs to the real host; Windows has no fcntl.
            platform_patches.enter_context(
                patch.object(yakala, '_WindowsCommitLock', lambda _vault: contextlib.nullcontext()))
        with patch.dict(sys.modules, {'tkinter': fake, 'tkinter.font': fake.font}), \
                patch.dict(os.environ, environment, clear=True), \
                patch.object(yakala, 'os', system), \
                platform_patches, \
                patch.object(yakala, '_clipboard', return_value=clip), \
                patch.object(yakala, '_clipboard_payload', return_value=payload, create=True) as probe, \
                patch.object(yakala, '_windows_clipboard', return_value={**(payload or {}), 'text': clip, 'sequence': 77}), \
                patch.object(yakala, 'clipboard_fresh', return_value=fresh), \
                patch.object(yakala, 'windows_enrich', side_effect=lambda value, cancel=None: value), \
                patch.object(yakala, 'window_image', return_value=shot), \
                patch.object(yakala, '_windows_prepare', side_effect=lambda root, ctx, vault, state, session=None:
                             yakala._collect_windows(ctx, vault, state)), \
                patch.object(yakala, '_font', side_effect=lambda size, weight='normal': ('Segoe UI', size, weight)), \
                patch.object(yakala, '_win_dpi'), patch.object(yakala, '_win_managed_chrome'), \
                patch.object(yakala, '_win_place'), \
                patch.object(yakala, '_win_monitor', return_value=(-1920, 0, 0, 1080)), \
                patch.object(yakala, '_win_focus', return_value=True), patch.object(yakala, '_win_escape_guard'), \
                patch.object(yakala, '_style_mac'), patch.object(yakala.subprocess, 'run'):
            result = yakala.popup(self.vault, context, state=Path(self.tmp.name) / 'state')
        return result, roots[0], probe

    def card(self, result):
        return yakala.find_card(self.vault, result['id'])

    def saved_files(self):
        # Only what a capture copied in: the folder may also hold housekeeping dot files.
        return sorted(path for path in (self.vault / INBOX / 'dosyalar').glob('*') if not path.name.startswith('.'))

    def test_screenshot_becomes_a_file_card_without_the_temp_path(self):
        result, root, _ = self.popup(self.shot_file(age=40), self.bitmap, [('look', None), ('type', 'hata ekranı'), ('key', '<Return>')])
        self.assertEqual((result['status'], result['kaynak_turu']), ('yakalandi', 'dosya'))
        card = self.card(result)
        self.assertEqual((card['meta']['kaynak_turu'], card['meta']['visibility'], card['meta']['arac']), ('dosya', 'private', 'masaustu'))
        self.assertNotIn('ScreenClip', card['body'])
        self.assertNotIn('## Secim', card['body'])
        self.assertIn('## Neden\n\nhata ekranı', card['body'])
        files = self.saved_files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r'^pano-goruntusu-\d{8}-\d{6}\.png$')
        self.assertIn('[[' + INBOX + '/dosyalar/' + files[0].name + ']]', card['body'])
        self.assertEqual(decode_png(files[0].read_bytes())[3], self.pixels)
        # The picture was on screen before anything was saved, and the temp copy is gone afterwards.
        placeholder, texts = root.seen[0]
        self.assertEqual(placeholder, 'Neden kaydediyorsun?')
        self.assertIn('GÖRSEL', texts)
        self.assertIn('Panodaki görüntü', texts)
        self.assertIn('8×5 · az önce · Tab ile çıkar', texts)
        self.assertEqual(len(Photo.opened), 1)
        self.assertFalse(Path(Photo.opened[0]).parent.exists())
        self.assertIn('✓  Beyne atıldı', root.canvas.texts())

    def test_tab_takes_the_picture_off_and_the_typed_line_is_the_note(self):
        result, root, _ = self.popup(self.shot_file(age=40), self.bitmap, [('key', '<Tab>'), ('look', None), ('type', 'yarın ara'), ('key', '<Return>')])
        self.assertEqual((result['status'], result['kaynak_turu']), ('yakalandi', 'metin'))
        body = self.card(result)['body']
        self.assertIn('> yarın ara', body)
        self.assertNotIn('ScreenClip', body)
        self.assertNotIn('## Dosyalar', body)
        self.assertEqual(self.saved_files(), [])
        placeholder, texts = root.seen[0]
        self.assertEqual(placeholder, 'Aklındakini yaz…')
        self.assertIn('Panodaki görüntü eklenmeyecek', texts)
        self.assertIn('8×5 · az önce · Tab ile ekle', texts)
        self.assertFalse(Path(Photo.opened[0]).parent.exists())

    def test_old_screenshot_waits_for_tab(self):
        # Hours later the same screenshot is still on the clipboard: shown, but a quick note stays a note.
        for clip in (self.shot_file(age=3 * 3600), REPORTED):  # an old temp file, and one Windows already deleted
            script = [('look', None), ('type', 'süt al'), ('key', '<Return>')]
            result, root, _ = self.popup(clip, self.bitmap, script)
            self.assertEqual(result['kaynak_turu'], 'metin', clip)
            body = self.card(result)['body']
            self.assertIn('> süt al', body)
            self.assertNotIn('ScreenClip', body)
            self.assertEqual(self.saved_files(), [])
            placeholder, texts = root.seen[0]
            self.assertEqual(placeholder, 'Aklındakini yaz…')
            self.assertIn('Panodaki görüntü eklenmeyecek', texts)
        result, root, _ = self.popup(REPORTED, self.bitmap, [('key', '<Tab>'), ('look', None), ('type', 'eski hata'), ('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'dosya')
        self.assertEqual(len(self.saved_files()), 1)
        self.assertIn('8×5 · eski, geçici dosyası silinmiş · Tab ile çıkar', root.seen[0][1])

    def test_picture_of_unknown_age_waits_for_tab(self):
        # No temp file next to it (a copied web image, an old PrtScn): shown, told how to add it, not attached.
        result, root, _ = self.popup('', self.bitmap, [('look', None), ('type', 'aklıma gelen'), ('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'metin')
        self.assertIn('> aklıma gelen', self.card(result)['body'])
        self.assertEqual(self.saved_files(), [])
        placeholder, texts = root.seen[0]
        self.assertEqual(placeholder, 'Aklındakini yaz…')
        self.assertIn('GÖRSEL', texts)
        self.assertIn('Panodaki görüntü eklenmeyecek', texts)
        self.assertIn('8×5 · Tab ile ekle', texts)
        self.assertFalse(Path(Photo.opened[0]).parent.exists())
        result, root, _ = self.popup('', self.bitmap, [('key', '<Tab>'), ('look', None), ('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'dosya')
        self.assertEqual(len(self.saved_files()), 1)
        self.assertIn('_(not yok)_', self.card(result)['body'])
        placeholder, texts = root.seen[0]
        self.assertEqual(placeholder, 'Neden kaydediyorsun?')
        self.assertIn('Panodaki görüntü', texts)
        self.assertIn('8×5 · Tab ile çıkar', texts)

    def test_tab_twice_puts_it_back_and_enter_alone_saves_the_picture(self):
        script = [('key', '<Tab>'), ('key', '<Tab>'), ('look', None), ('key', '<Return>')]
        result, root, _ = self.popup(self.shot_file(age=40), self.bitmap, script)
        self.assertEqual(result['kaynak_turu'], 'dosya')
        self.assertEqual(len(self.saved_files()), 1)
        self.assertIn('_(not yok)_', self.card(result)['body'])
        self.assertEqual(root.seen[0][0], 'Neden kaydediyorsun?')

    def test_picture_switched_off_and_nothing_typed_saves_nothing(self):
        # Off from the start (unknown age), or taken off by hand (fresh screenshot): Enter has nothing to save.
        for clip, keys in (('', []), (self.shot_file(age=40), [('key', '<Tab>')])):
            Photo.opened = []
            result, root, _ = self.popup(clip, self.bitmap, keys + [('key', '<Return>'), ('look', None), ('key', '<Escape>')])
            self.assertEqual(result, {'status': 'vazgecildi'}, clip)
            self.assertIn('Önce bir şey yaz', root.seen[0][1])
            self.assertFalse((self.vault / INBOX).exists())
            self.assertFalse(Path(Photo.opened[0]).parent.exists())

    def test_escape_saves_nothing_and_removes_the_temp_copy(self):
        result, _, _ = self.popup(self.shot_file(age=40), self.bitmap, [('key', '<Escape>')])
        self.assertEqual(result, {'status': 'vazgecildi'})
        self.assertFalse((self.vault / INBOX).exists())
        self.assertFalse(Path(Photo.opened[0]).parent.exists())

    def test_text_next_to_a_picture_is_captured_as_before(self):
        # A spreadsheet copies cells as text and as a bitmap; the text is still the capture.
        result, root, probe = self.popup('kira 12.500', self.bitmap, [('look', None), ('type', 'bütçe'), ('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'metin')
        body = self.card(result)['body']
        self.assertIn('> kira 12.500', body)
        self.assertIn('## Neden\n\nbütçe', body)
        self.assertEqual(self.saved_files(), [])
        probe.assert_not_called()
        self.assertNotIn('GÖRSEL', root.seen[0][1])
        self.assertEqual(Photo.opened, [])

    def test_without_a_picture_the_popup_is_unchanged(self):
        for payload in (None, {'image': False, 'png': None, 'dib': None}):
            result, root, _ = self.popup('', payload, [('look', None), ('type', 'aklıma gelen'), ('key', '<Return>')])
            self.assertEqual(result['kaynak_turu'], 'metin')
            self.assertIn('> aklıma gelen', self.card(result)['body'])
            self.assertEqual(root.seen[0][0], 'Aklındakini yaz…')
            self.assertNotIn('GÖRSEL', root.seen[0][1])
        # The reader found no picture: the path text is retained exactly as originally.
        result, _, _ = self.popup(REPORTED, {'image': False, 'png': None, 'dib': None}, [('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'metin')
        self.assertIn('> ' + REPORTED, self.card(result)['body'])
        self.assertEqual(self.saved_files(), [])
        self.assertEqual(Photo.opened, [])

    def test_link_on_the_clipboard_is_unchanged(self):
        result, _, probe = self.popup('https://ornek.com/yazi', self.bitmap, [('key', '<Return>')])
        self.assertEqual(result['kaynak_turu'], 'makale')
        probe.assert_not_called()

    def test_linux_copied_files_are_saved_without_the_previously_captured_browser_url(self):
        page, hover = 'https://example.com/page', 'https://example.com/hover'
        previous = yakala.capture(self.vault, url=page)
        previous_card = self.card(previous)['path'].read_bytes()
        copied = Path(self.tmp.name) / 'copied document.txt'
        copied.write_text('copied file contents', encoding='utf-8')
        with patch.object(yakala, 'linux_browser_context', return_value={
                'url': page, 'url_imlec': hover, 'baslik': 'Page', 'baslik_imlec': 'Hover',
                'uygulama': 'Mozilla Firefox', 'url_yontem': 'linux-atspi-x11'}), \
                patch.object(yakala, '_linux_clipboard_context', return_value={'dosyalar': [str(copied)]}):
            context = yakala.linux_context()
        result, root, _ = self.popup(copied.as_uri(), None,
                                    [('look', None), ('key', '<Return>')], context, platform='linux')
        self.assertEqual(result['status'], 'yakalandi')
        self.assertEqual(result['kaynak_turu'], 'dosya')
        self.assertNotEqual(result['id'], previous['id'])
        card = self.card(result)
        self.assertFalse(card['meta'].get('url'))
        self.assertEqual(card['meta']['baslik'], copied.name)
        self.assertNotIn(page, card['body'])
        self.assertNotIn(hover, card['body'])
        self.assertTrue(any(copied.name in text for text in root.seen[0][1]))
        self.assertFalse(any(page in text or hover in text for text in root.seen[0][1]))
        self.assertEqual([path.read_bytes() for path in self.saved_files()], [copied.read_bytes()])
        self.assertEqual(self.card(previous)['path'].read_bytes(), previous_card)

    def test_browser_shortcuts_use_the_displayed_platform_modifier(self):
        context = {'url': 'https://example.com/page', 'url_imlec': 'https://example.com/hover',
                   'baslik': 'Page', 'baslik_imlec': 'Hover'}
        for platform, modifier in (('darwin', 'Option'), ('linux', 'Alt')):
            page_key, hover_key = '<' + modifier + '-s>', '<' + modifier + '-i>'
            # Entry adds the page without dropping the hover; the root shortcut can then
            # remove the hover. Check the saved selections, not the older radio labels.
            scenarios = (([], context['url_imlec'], 'Hover', []),
                         ([('key', page_key)], context['url_imlec'], 'Hover', [context['url']]),
                         ([('key', page_key), ('root-key', hover_key)], context['url'], 'Page', []))
            for keys, expected, title, extras in scenarios:
                with self.subTest(platform=platform, keys=keys), \
                        patch.object(yakala, 'capture', return_value={'status': 'yakalandi'}) as save:
                    result, root, _ = self.popup('', None,
                        [('look', None), *keys, ('key', '<Return>')], context, platform=platform)
                    for key in ('I', 'S'):
                        self.assertIn(modifier + '+' + key, root.seen[0][1])
                    other = 'Alt' if modifier == 'Option' else 'Option'
                    self.assertNotIn('<' + other + '-KeyPress>', root.bound)
                    self.assertNotIn('<' + other + '-KeyPress>', root.entry.handlers)
                    self.assertEqual(result['status'], 'yakalandi')
                    save.assert_called_once()
                    self.assertEqual(save.call_args.kwargs['url'], expected)
                    self.assertEqual(save.call_args.kwargs['title'], title)
                    self.assertEqual(save.call_args.kwargs['extra_urls'], extras)
                    self.assertEqual(save.call_args.kwargs['files'], [])

    def test_windows_page_link_wins_and_a_fresh_copied_link_stays_selectable(self):
        context = {'url': 'https://example.com/page', 'baslik': 'Page'}
        result, root, _ = self.popup('https://example.com/copied', None,
                                    [('look', None), ('key', '<Return>')], context, windows=True, fresh=True)
        self.assertEqual(self.card(result)['meta']['url'], context['url'])
        self.assertTrue(any('Pano bağlantısı' in text for text in root.seen[0][1]))
        result, _, _ = self.popup('https://example.com/copied', None,
                                  [('key', '<Alt-p>'), ('key', '<Return>')], context, windows=True, fresh=True)
        self.assertEqual(self.card(result)['meta']['url'], context['url'])
        self.assertEqual(self.card(result)['meta']['ek_urller'], ['https://example.com/copied'])

    def test_windows_stale_text_is_off_even_when_window_capture_fails(self):
        for clip in ('eski özel metin', 'https://example.com/old'):
            result, _, _ = self.popup(clip, None, [('key', '<Return>'), ('key', '<Escape>')], windows=True)
            self.assertEqual(result, {'status': 'vazgecildi'})
        self.assertEqual(yakala.cards(self.vault), [])

    def test_windows_stale_text_has_no_tile_or_hidden_attachment(self):
        result, root, _ = self.popup('eski özel metin', None,
                                     [('look', None), ('type', 'yeni not'), ('key', '<Return>')],
                                     {'url': 'https://example.com/page'}, windows=True)
        self.assertFalse(any('Pano metni' in text or 'eski özel metin' in text for text in root.seen[0][1]))
        self.assertNotIn('eski özel metin', self.card(result)['body'])

    def test_windows_fresh_text_is_visible_and_can_be_added_from_the_note_field(self):
        result, root, _ = self.popup('taze metin', None,
                                     [('look', None), ('type', 'notum'), ('key', '<Alt-m>'), ('key', '<Return>')],
                                     {'url': 'https://example.com/page'}, windows=True, fresh=True)
        self.assertTrue(any('Pano metni' in text for text in root.seen[0][1]))
        self.assertIn('> taze metin', self.card(result)['body'])
        self.assertIn('## Neden\n\nnotum', self.card(result)['body'])
        self.assertEqual(self.card(result)['meta']['visibility'], 'private')

    def test_alt_a_in_the_note_field_keeps_the_page_and_note(self):
        context = {'url': 'https://example.com/page', 'url_alinti': 'https://example.com/quote'}
        for key, state in (('a', 0x20000), ('A', 0x20003)):
            with self.subTest(key=key):
                result, root, _ = self.popup('', None,
                    [('type', 'notum'), ('alt', dict(keysym=key, keycode=65, char=key, state=state)),
                     ('look', None), ('key', '<Return>')], context, windows=True)
                card = self.card(result)
                self.assertEqual(card['meta']['url'], context['url'])
                self.assertEqual(card['meta']['ek_urller'], [context['url_alinti']])
                self.assertEqual(root.seen[0][0], 'notum')
                self.assertIn(context['url_alinti'], card['body'])

    def test_turkish_alt_i_and_caps_lock_work_in_the_note_field(self):
        context = {'url': 'https://example.com/page', 'url_imlec': 'https://example.com/link'}
        result, root, _ = self.popup('', None,
            [('key', '<Alt-s>'), ('alt', dict(keysym='Iabovedot', keycode=73, char='İ', state=0x20002)),
             ('look', None), ('key', '<Return>')], context, windows=True)
        self.assertEqual(root.seen[0][0], 'Neden kaydediyorsun?')
        self.assertEqual(self.card(result)['meta']['url'], context['url'])
        self.assertNotIn('ek_urller', self.card(result)['meta'])

    def test_duplicate_quote_has_no_second_tile(self):
        page = 'https://www.instagram.com/reel/Abcd/'
        result, root, _ = self.popup(page, None, [('look', None), ('key', '<Return>')],
                                     {'url': page, 'url_alinti': page}, windows=True, fresh=True)
        self.assertFalse(any('Alıntılanan gönderi' in text or 'Pano bağlantısı' in text for text in root.seen[0][1]))
        self.assertNotIn('ek_urller', self.card(result)['meta'])

    def test_link_and_window_picture_are_saved_together_only_after_alt_w(self):
        page = 'https://example.com/page'
        for enabled in (False, True):
            shot = yakala.clipboard_image('', {}, payload=self.bitmap, offered=True)
            script = [('look', None)] + ([('key', '<Alt-w>')] if enabled else []) + [('key', '<Return>')]
            result, root, _ = self.popup('', None, script, {'url': page}, windows=True, shot=shot)
            card = self.card(result)
            self.assertTrue(any('Pencere görüntüsü' in text for text in root.seen[0][1]))
            self.assertEqual(card['meta']['url'], page)
            self.assertEqual(card['meta']['kaynak_turu'], 'makale')
            self.assertEqual(len(self.saved_files()), int(enabled))
            if enabled:
                self.assertEqual(card['meta']['visibility'], 'private')
                self.assertIn('## Dosyalar', card['body'])
            self.assertFalse(Path(shot['folder']).exists())

    def test_many_sources_fit_a_small_monitor_and_keep_controls_visible(self):
        with patch.object(yakala, '_popup_bounds', return_value=(448, 288)):
            result, root, _ = self.popup('', None,
                [('look', None), ('key', '<Next>'), ('key', '<Alt-1>'), ('key', '<Escape>')],
                {'dosyalar': [f'document-{i}.txt' for i in range(40)]}, windows=True)
        self.assertEqual(result['status'], 'vazgecildi')
        width, height = map(int, root.size.split('x'))
        self.assertLessEqual(width, 448)
        self.assertLessEqual(height, 288)
        rows = next(item['window'] for item in root.canvas.items.values() if isinstance(item.get('window'), Canvas))
        self.assertGreater(rows.options['scrollregion'][3], rows.options['height'])
        self.assertEqual(rows.options['last_scroll'], (1, 'pages'))
        self.assertEqual(rows.options['last_position'], 0)
        self.assertTrue(any('Kaydet' in text for text in root.seen[0][1]))
        self.assertFalse((self.vault / INBOX).exists())

    def test_empty_windows_sources_keep_the_note_field_without_a_negative_viewport(self):
        # Clipboard age is unknown and window capture failed: only the note remains.
        result, root, _ = self.popup('old clipboard text', None,
            [('type', 'yalnız bir not'), ('key', '<Return>')], windows=True)
        self.assertEqual(result['status'], 'yakalandi')
        self.assertIn('yalnız bir not', self.card(result)['body'])
        self.assertNotIn('old clipboard text', self.card(result)['body'])
        rows = [item['window'] for item in root.canvas.items.values()
                if isinstance(item.get('window'), Canvas)]
        self.assertEqual(rows, [])

    def test_windows_sequence_makes_image_fresh_without_snipping_tool_path(self):
        result, root, _ = self.popup('', self.bitmap, [('look', None), ('key', '<Return>')],
                                     windows=True, fresh=True)
        self.assertEqual(result['kaynak_turu'], 'dosya')
        self.assertEqual(len(self.saved_files()), 1)
        self.assertTrue(any('Pano görüntüsü' in text for text in root.seen[0][1]))
        # Next to a browser link the fresh picture is offered, not chosen.
        result, _, _ = self.popup('', self.bitmap, [('key', '<Return>')],
                                  {'url': 'https://example.com/page'}, windows=True, fresh=True)
        self.assertEqual(self.card(result)['meta']['url'], 'https://example.com/page')
        self.assertEqual(len(self.saved_files()), 1)

    def test_windows_clipboard_and_window_images_are_independent(self):
        shot = yakala.clipboard_image('', {}, payload=self.bitmap, offered=True)
        self.assertIsNotNone(shot)
        result, root, _ = self.popup('taze metin', self.bitmap,
                                     [('look', None), ('key', '<Alt-g>'), ('key', '<Alt-m>'), ('key', '<Alt-w>'), ('key', '<Return>')],
                                     windows=True, shot=shot, fresh=True, context={'url': 'https://example.com/page'})
        self.assertEqual(len(self.saved_files()), 2)
        self.assertIn('> taze metin', self.card(result)['body'])
        self.assertTrue(any('Alt+M' in text for text in root.seen[0][1]))
        self.assertFalse(Path(shot['folder']).exists())

    def test_windows_escape_guard_is_armed_even_when_the_popup_got_focus(self):
        # A click on the page after the popup opened takes the key away from Tk, so the Esc hook
        # must already be there; it was armed only when focus was refused before (#383).
        guards = []

        def hwnd(_root):
            guards.append(yakala._win_escape_guard)  # the model's guard mock, live inside popup()
            return 99
        with patch.object(yakala, '_win_hwnd', side_effect=hwnd):
            result, root, _ = self.popup('', None, [('key', '<Escape>')], {'hwnd': 42}, windows=True)
        self.assertEqual(result, {'status': 'vazgecildi'})
        self.assertFalse(root.alive)
        guard = guards[-1]
        guard.assert_called_once()
        self.assertEqual(guard.call_args.args[1:3], (99, 42))  # popup frame, source window
        guard.return_value.assert_called()  # and stopped again when the popup closed

    def test_windows_modified_escape_in_the_focused_popup_is_left_to_the_system(self):
        # The hook passes Shift/Ctrl/Alt/Win+Esc on without dismissing (#459), but Tk's <Escape>
        # matches every modifier state, so the focused popup's own binding has to skip them too.
        # Tk on Windows: Shift 0x1, Control 0x4, Alt 0x20000; Caps 0x2, Num 0x8 and Scroll Lock 0x20
        # are lock states. The Windows key is not in event.state, only in the thread's key state.
        held, real = set(), getattr(ctypes, 'WinDLL', None)
        user = Mock()
        user.GetKeyState.side_effect = lambda key: -128 if key in held else 0  # SHORT, high bit = down
        cases = ((0x1, (), True), (0x4, (), True), (0x20000, (), True), (0x1 | 0x4 | 0x8, (), True),
                 (0, (0x5B,), True), (0x8, (0x5C,), True),
                 (0, (), False), (0x2, (), False), (0x8, (), False), (0x20, (), False), (0x2 | 0x8 | 0x20, (), False))
        for state, keys, modified in cases:
            held.clear()
            held.update(keys)
            with self.subTest(state=hex(state), keys=keys), \
                    patch.object(ctypes, 'WinDLL', create=True,
                                 side_effect=lambda name, **kw: user if name == 'user32' else real(name, **kw)):
                result, root, _ = self.popup('', None, [('event', ('<Escape>', {'keysym': 'Escape', 'state': state}))],
                                             windows=True)
                self.assertEqual(result, {'status': 'vazgecildi'})
                self.assertEqual(root.alive, modified, 'a modified Esc keeps the popup, a plain one cancels it')

    def test_windows_cancel_does_not_consume_sequence_and_double_enter_saves_once(self):
        state = Path(self.tmp.name) / 'state/yakala/pano-sira.txt'
        result, root, _ = self.popup('new', None, [('key', '<Escape>')], windows=True, fresh=True)
        self.assertFalse(root.alive)
        self.assertFalse(state.exists())
        result, _, _ = self.popup('new', None, [('key', '<Return>'), ('key', '<Return>')], windows=True, fresh=True)
        self.assertEqual(len(yakala.cards(self.vault)), 1)
        self.assertEqual(state.read_text(encoding='ascii'), '77')

    def test_windows_failed_save_keeps_sequence_and_allows_retry(self):
        with patch.object(yakala, 'capture', side_effect=[OSError('locked'), {'status': 'yakalandi'}]) as capture:
            result, _, _ = self.popup('new', None, [('key', '<Return>'), ('key', '<Return>')], windows=True, fresh=True)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(result['status'], 'yakalandi')
        with patch.object(yakala, 'capture', side_effect=OSError('locked')):
            before = (Path(self.tmp.name) / 'state/yakala/pano-sira.txt').read_text()
            self.popup('another', None, [('key', '<Return>'), ('key', '<Escape>')], windows=True, fresh=True)
            self.assertEqual((Path(self.tmp.name) / 'state/yakala/pano-sira.txt').read_text(), before)

    def test_windows_freshness_only_changes_after_success(self):
        state = Path(self.tmp.name) / 'state'
        self.assertFalse(yakala.clipboard_fresh(self.vault, 10, state))
        self.assertFalse(state.exists())
        yakala._clipboard_saved(self.vault, 10, state)
        self.assertFalse(yakala.clipboard_fresh(self.vault, 10, state))
        self.assertTrue(yakala.clipboard_fresh(self.vault, 11, state))
        self.assertTrue(yakala.clipboard_fresh(self.vault, 11, state))
        self.assertFalse(yakala.clipboard_fresh(self.vault, None, state))


class StandaloneCopyTest(unittest.TestCase):
    def test_single_file_copy_runs_alone(self):
        # The macOS listener runs a copy of this one file from the state folder: no sibling module may be needed.
        with tempfile.TemporaryDirectory(prefix='v3-yakala-tek-') as folder:
            copy = Path(folder) / 'beyin_v3_yakala.py'
            copy.write_bytes((SCRIPTS / 'beyin_v3_yakala.py').read_bytes())
            code = ('import beyin_v3_yakala as y, os; from unittest.mock import patch; '
                    'with_no_helper = patch.object(y, "_which", return_value=None); with_no_helper.start(); '
                    'print(y.png_info(b"") is None, y.clipboard_image("bir not", {}) is None, '
                    'os.name == "nt" or y.clipboard_image("", {}) is None)')
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, encoding='utf-8',
                                    cwd=folder, env=inherited_env(PYTHONPATH=folder, PYTHONDONTWRITEBYTECODE='1'), timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'True True True'), result.stderr)


def set_clipboard(formats):
    """Replace the real clipboard with {format id or registered name: bytes}. Skips when the session has none."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL('user32'), ctypes.WinDLL('kernel32')
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD] + [ctypes.c_int] * 4 + \
        [wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes, user32.DestroyWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.OpenClipboard.argtypes, user32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user32.EmptyClipboard.argtypes, user32.EmptyClipboard.restype = [], wintypes.BOOL
    user32.CloseClipboard.argtypes, user32.CloseClipboard.restype = [], wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes, user32.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], wintypes.UINT
    user32.SetClipboardData.argtypes, user32.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes, kernel32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes, kernel32.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    kernel32.GlobalFree.argtypes, kernel32.GlobalFree.restype = [wintypes.HGLOBAL], wintypes.HGLOBAL
    # EmptyClipboard needs an owner window, or SetClipboardData is refused.
    owner = user32.CreateWindowExW(0, 'STATIC', None, 0, 0, 0, 0, 0, None, None, None, None)
    if not owner:
        raise unittest.SkipTest('bu oturumda pencere acilamiyor; pano kullanilamaz')
    try:
        for _ in range(40):
            if user32.OpenClipboard(owner):
                break
            time.sleep(0.05)
        else:
            raise unittest.SkipTest('pano acilamadi; bu oturumda kullanilabilir pano yok')
        try:
            if not user32.EmptyClipboard():
                raise unittest.SkipTest('pano bosaltilamadi')
            for key, data in formats.items():
                code = key if isinstance(key, int) else user32.RegisterClipboardFormatW(key)
                handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
                pointer = kernel32.GlobalLock(handle) if handle else None
                if not pointer:
                    raise unittest.SkipTest('pano icin bellek ayrilamadi')
                ctypes.memmove(pointer, data, len(data))
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(code, handle):
                    kernel32.GlobalFree(handle)
                    raise unittest.SkipTest('panoya yazilamadi')
        finally:
            user32.CloseClipboard()
    finally:
        user32.DestroyWindow(owner)


def file_drop(*paths):
    """CF_HDROP payload: DROPFILES header, then wide paths, each and the list ending in NUL."""
    return struct.pack('<IiiII', 20, 0, 0, 0, 1) + ''.join(str(path) + '\0' for path in paths).encode('utf-16-le') + b'\0\0'


@unittest.skipUnless(os.name == 'nt' and (os.environ.get('CI') or os.environ.get('BEYIN_TEST_PANO')),
                     'replaces the real Windows clipboard: CI runners, or BEYIN_TEST_PANO=1')
class WindowsClipboardTest(unittest.TestCase):
    """The real reader against the real clipboard, filled through ctypes with synthetic payloads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-winpano-')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(set_clipboard, {})
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()
        self.pixels = picture(7, 5)

    def payload(self, **options):
        # A clipboard listener (history, remote desktop) may hold the clipboard for a moment after a change.
        for _ in range(20):
            found = yakala._win_clipboard_payload(**options)
            if found is not None:
                return found
            time.sleep(0.1)
        self.skipTest('pano okunamadi; baska bir surec birakmiyor')

    def image(self, clip=''):
        self.payload()
        image = yakala.clipboard_image(clip, {})
        self.assertIsNotNone(image)
        self.addCleanup(yakala._image_done, image)
        return image

    def test_bitmap_round_trip(self):
        for options in ({'bits': 24}, {'bits': 32}, {'bits': 32, 'top_down': True}, {'bits': 24, 'top_down': True}):
            set_clipboard({8: make_dib(self.pixels, **options)})
            payload = self.payload()
            self.assertTrue(payload['image'], options)
            self.assertIsNone(payload['png'])
            made = yakala.dib_to_png(payload['dib'])
            self.assertIsNotNone(made, options)
            self.assertEqual(decode_png(made[0])[3], self.pixels, options)

    def test_png_format_is_taken_as_it_is(self):
        png = make_png(self.pixels)
        for name in ('PNG', 'image/png'):
            set_clipboard({name: png, 8: make_dib(picture(2, 2))})
            payload = self.payload()
            self.assertEqual(payload['png'][:len(png)], png, name)
            self.assertIsNone(payload['dib'])  # a valid PNG makes the bitmap unnecessary
            image = self.image()
            self.assertEqual(Path(image['path']).read_bytes(), png)
            self.assertEqual((image['age'], image['stale'], image['on']), (None, False, False))  # no temp file: the user decides

    def test_broken_png_falls_back_to_the_bitmap(self):
        set_clipboard({'PNG': make_png(self.pixels)[:-9], 8: make_dib(self.pixels)})
        self.assertEqual(decode_png(Path(self.image()['path']).read_bytes())[3], self.pixels)

    def test_size_limit_and_no_picture(self):
        nothing = {'image': False, 'png': None, 'dib': None}
        set_clipboard({8: make_dib(self.pixels)})
        self.assertEqual(self.payload(limit=64), {'image': True, 'png': None, 'dib': None})
        set_clipboard({13: 'yalnız metin\0'.encode('utf-16-le')})
        self.assertEqual(self.payload(), nothing)
        self.assertIsNone(yakala.clipboard_image('', {}))
        set_clipboard({})
        self.assertEqual(self.payload(), nothing)
        self.assertIsNone(yakala.clipboard_image('', {}))

    def screenshot(self):
        path = Path(self.tmp.name) / 'Packages' / 'Ornek.Uygulama_abc' / 'TempState' / 'ScreenClip' / '{0A1B2C3D}.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(make_png(self.pixels))
        set_clipboard({8: make_dib(self.pixels, bits=32), 15: file_drop(path)})
        return path

    def test_screenshot_with_file_drop_becomes_a_file_card(self):
        path = self.screenshot()
        clip = str(path).replace('\\', '/')  # how Tk reports the file drop
        self.assertEqual(yakala.snip_path(clip), clip)
        image = self.image(clip)
        self.assertTrue(image['on'])
        self.assertLess(image['age'], yakala.IMAGE_FRESH)
        self.assertEqual(decode_png(Path(image['path']).read_bytes())[3], self.pixels)
        result = yakala.capture(self.vault, files=[image['path']], why='ekran')
        self.assertEqual(result['kaynak_turu'], 'dosya')
        os.utime(path, (time.time() - 7200, time.time() - 7200))
        stale = self.image(clip)
        self.assertEqual((stale['stale'], stale['on']), (True, False))
        path.unlink()
        gone = self.image(clip)
        self.assertEqual((gone['age'], gone['stale'], gone['on']), (None, True, False))

    def test_tk_reports_the_file_drop_as_text_and_reads_the_png(self):
        path = self.screenshot()
        self.payload()
        try:
            import tkinter
            root = tkinter.Tk()
        except Exception as exc:
            self.skipTest('tkinter penceresi acilamadi: ' + type(exc).__name__)
        try:
            root.withdraw()
            clip = yakala._clipboard(root).strip()
            # This is the text the popup used to save as the note; the resolution depends on recognising it.
            self.assertEqual(clip, str(path).replace('\\', '/'))
            self.assertEqual(yakala.snip_path(clip), clip)
            thumb = yakala._image_thumb(root, self.image(clip))
            self.assertIsNotNone(thumb)  # Tk decodes the PNG this module wrote
            self.assertEqual((thumb.width(), thumb.height()), (7, 5))
            del thumb
        finally:
            root.destroy()

    def test_file_drop_round_trip_in_snapshot(self):
        with tempfile.NamedTemporaryFile(suffix='.txt') as tmp:
            set_clipboard({15: file_drop(tmp.name)})
            payload = self.payload(snapshot=True)
            self.assertEqual(payload.get('dosyalar'), [tmp.name])
            self.assertEqual(payload.get('text'), '')
            self.assertFalse(payload['image'])


if __name__ == '__main__':
    unittest.main()
