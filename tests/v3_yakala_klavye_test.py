"""Typed characters Windows Tk delivers as '?' (#994), with no desktop: the key event is scripted."""
from pathlib import Path
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_yakala as y  # noqa: E402
import v3_yakala_pano_test as pano  # noqa: E402

UNICODE = 0x1000000  # X11 keysym of a Unicode character: this offset plus its code point


def typed(char, number, windows=True):
    return y._typed_char(char, number, windows=windows)


class TypedCharTest(unittest.TestCase):
    """_typed_char(): the code point Tk kept in keysym_num, only where the character itself was lost."""

    def test_azerbaijani_schwa_outside_cp1254_comes_back(self):
        # The reporter's event log: char '?', keysym_num 601 (ə) and 399 (Ə, Shift).
        for number, expected in ((601, 'ə'), (399, 'Ə'), (UNICODE + 601, 'ə'), (UNICODE + 399, 'Ə')):
            with self.subTest(number=number):
                self.assertEqual(typed('?', number), expected)

    def test_other_letters_missing_from_their_code_page(self):
        # Romanian comma-below letters (not in cp1250), Maori macron and Welsh circumflex (not in cp1252),
        # Latin-1 letters cp1254 swapped out for Turkish ones.
        for letter in 'șțȘȚāŵŷýþðÐ':
            with self.subTest(letter=letter):
                self.assertEqual(typed('?', ord(letter)), letter)
                self.assertEqual(typed('?', UNICODE + ord(letter)), letter)

    def test_a_character_tk_already_delivered_is_left_alone(self):
        # ğ is in cp1254: char 'ğ', keysym_num 287. A correct char never consults keysym_num.
        for char, number in (('ğ', 287), ('ğ', 601), ('ə', 601), ('a', 97), ('Ş', 350), ('ı', 305), ('İ', 304)):
            with self.subTest(char=char):
                self.assertIsNone(typed(char, number))

    def test_the_real_question_mark_key_stays_a_question_mark(self):
        for number in (63, UNICODE + 63):
            self.assertIsNone(typed('?', number))

    def test_ascii_and_control_code_points_are_not_characters_to_insert(self):
        numbers = list(range(0x00, 0xA0)) + [0xA0, 0xAD, 0x200B, 0x200E, 0x2028, 0x2029, 0xFEFF, 0xE000, 0xF8FF]
        for number in numbers:
            for value in (number, UNICODE + number):
                with self.subTest(value=hex(value)):
                    self.assertIsNone(typed('?', value))

    def test_tk_key_names_in_its_own_block_are_not_characters(self):
        # dead_acute, dead_circumflex, ISO_Left_Tab, BackSpace, Escape, KP_Multiply, F1, Shift_L, Delete.
        for number in (0xFE00, 0xFE51, 0xFE52, 0xFE20, 0xFF08, 0xFF1B, 0xFFAA, 0xFFBE, 0xFFE1, 0xFFFF):
            with self.subTest(number=hex(number)):
                self.assertIsNone(typed('?', number))
        # With the Unicode offset the same numbers are unambiguous characters.
        self.assertEqual(typed('?', UNICODE + 0xFF08), '（')
        self.assertEqual(typed('?', UNICODE + 0xFE52), '﹒')

    def test_invalid_and_negative_numbers_change_nothing(self):
        for number in (None, '??', '601', b'601', 601.0, True, False, -1, -601, -UNICODE - 601,
                       0x110000, UNICODE + 0x110000, 0x1008FF11, 0x1005FF00, 2 ** 64, -(2 ** 64)):
            with self.subTest(number=number):
                self.assertIsNone(typed('?', number))

    def test_lone_surrogate_halves_never_reach_the_field(self):
        # Windows sends a character beyond the BMP as two UTF-16 halves; half a pair is not text.
        for number in (0xD800, 0xD83D, 0xDBFF, 0xDC00, 0xDE00, 0xDFFF):
            for value in (number, UNICODE + number):
                with self.subTest(value=hex(value)):
                    self.assertIsNone(typed('?', value))

    def test_a_whole_code_point_beyond_the_bmp_is_one_character(self):
        for number, expected in ((0x1F600, '😀'), (UNICODE + 0x1F600, '😀'), (0x10348, '𐍈'), (UNICODE + 0x20BB7, '𠮷')):
            with self.subTest(number=hex(number)):
                self.assertEqual(typed('?', number), expected)
                self.assertEqual(typed('?', number).encode('utf-8').decode('utf-8'), expected)

    def test_macos_and_linux_deliver_the_character_themselves(self):
        for number in (601, 399, UNICODE + 601, 0x1F600, 287):
            with self.subTest(number=number):
                self.assertIsNone(typed('?', number, windows=False))

    def test_platform_follows_the_running_system_by_default(self):
        for name, expected in (('nt', 'ə'), ('posix', None)):
            system = types.SimpleNamespace(**{**vars(os), 'name': name})
            with self.subTest(name=name), patch.object(y, 'os', system):
                self.assertEqual(y._typed_char('?', 601), expected)

    def test_invariant_over_a_grid_of_key_events(self):
        """Only Windows and char '?' change anything, and then only into the very code point Tk reported."""
        numbers = list(range(0, 0x3400)) + list(range(0xD7F0, 0xE010)) + list(range(0xFDF0, 0x10010))
        numbers += [0x1F600, 0x10FFFF, 0x110000, -1, -0x1F600]
        numbers += [UNICODE + n for n in range(0, 0x400)] + [UNICODE + n for n in range(0xD7F0, 0xE010)]
        numbers += [UNICODE + n for n in range(0xFDF0, 0x10010)] + [UNICODE + 0x10FFFF, UNICODE + 0x110000]
        changed = 0
        for number in numbers:
            code = number - UNICODE if number >= UNICODE else number
            for char in ('?', 'a', 'ğ', 'ə', '', ' ', '\x08', '\r', '？', '??'):
                for windows in (True, False):
                    result = y._typed_char(char, number, windows=windows)
                    if not (windows and char == '?'):
                        self.assertIsNone(result, (char, number, windows))
                        continue
                    if result is None:
                        continue
                    changed += 1
                    self.assertEqual(result, chr(code), hex(number))
                    self.assertTrue(result.isprintable(), hex(number))
                    self.assertGreaterEqual(code, 0xA0, hex(number))
                    self.assertFalse(0xD800 <= code <= 0xDFFF, hex(number))
                    self.assertFalse(number < UNICODE and 0xFE00 <= number <= 0xFFFF, hex(number))
        self.assertGreater(changed, 10000)  # the grid really exercises the replacement path


class KeyEntry(pano.Entry):
    """The note field fed real Tk key events: Entry's class binding inserts `char` unless the widget binding broke."""

    def __init__(self, root, **options):
        super().__init__(root, **options)
        self.calls = []
        self.tk = types.SimpleNamespace(call=self.call)

    def call(self, *words):
        self.calls.append(words)
        if words[0] == '::tk::EntryInsert':
            self.insert('insert', words[2])

    def type(self, keys):
        for char, keysym, number in keys:
            event = types.SimpleNamespace(char=char, keysym=keysym, keysym_num=number, keycode=0, state=0)
            if self.handlers['<KeyPress>'](event) != 'break':
                self.insert('insert', char)  # bind Entry <Key> {tk::EntryInsert %W %A}
            self.handlers['<KeyRelease>'](None)


# Azerbaijani Latin under cp1254, as the reporter logged it; the last key is the real ?.
KEYS = [('?', '??', 601), ('?', '??', 399), (' ', 'space', 32), ('ğ', '??', 287), ('ı', '??', 305), ('?', 'question', 63)]


class PopupKeyTest(pano.ScreenshotFiles, unittest.TestCase):
    """popup() with a scripted tkinter: what the note field holds after those key presses."""

    popup = pano.PopupWiringTest.popup

    def setUp(self):
        self.make_store('v3-yakala-klavye-')
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()
        pano.Photo.opened = []

    def note(self, **platform):
        context = {'url': 'https://example.com/page'}
        with patch.object(pano, 'Entry', KeyEntry):
            result, root, _ = self.popup('', None, [('type', KEYS), ('look', None), ('key', '<Return>')],
                                         context, **platform)
        self.assertEqual(result['status'], 'yakalandi')
        return y.find_card(self.vault, result['id'])['body'], root

    def test_windows_note_keeps_the_letters_and_the_real_question_mark(self):
        body, root = self.note(windows=True)
        self.assertIn('## Neden\n\nəƏ ğı?', body)
        self.assertEqual(root.seen[0][0], 'əƏ ğı?')  # the placeholder went away on the first key
        entry = root.entry
        self.assertEqual(entry.calls, [('::tk::CancelRepeat',), ('::tk::EntryInsert', str(entry), 'ə'),
                                       ('::tk::CancelRepeat',), ('::tk::EntryInsert', str(entry), 'Ə')])

    def test_linux_note_is_what_tk_delivered(self):
        body, root = self.note(platform='linux')
        self.assertIn('## Neden\n\n?? ğı?', body)
        self.assertEqual(root.entry.calls, [])


if __name__ == '__main__':
    unittest.main()
