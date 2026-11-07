"""Regression checks for reconstructed Windows capture fixes; no Win32 GUI required."""
import sys
import os
import types
import tempfile
import unittest
import struct
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_yakala as yakala


class RebuildRegressionTest(unittest.TestCase):
    def test_domain_boundary(self):
        self.assertNotEqual(yakala.canonical('https://notyoutube.com/watch?v=abcdEF1234'),
                            yakala.canonical('https://youtube.com/watch?v=abcdEF1234'))

    def test_youtube_alias(self):
        self.assertEqual(yakala.canonical('https://youtu.be/abcdEF1234?t=30'),
                         yakala.canonical('https://www.youtube.com/watch?v=abcdEF1234&feature=youtu.be'))

    def test_youtube_short(self):
        self.assertEqual(yakala.canonical('https://www.youtube.com/shorts/abcdEF1234'),
                         yakala.canonical('https://www.youtube.com/watch?v=abcdEF1234'))

    def test_port(self):
        self.assertNotEqual(yakala.canonical('https://example.org:444/path'),
                            yakala.canonical('https://example.org:445/path'))

    def test_routing_fragment(self):
        self.assertNotEqual(yakala.canonical('https://example.org/x#/one'),
                            yakala.canonical('https://example.org/x#/two'))

    def test_chromium_hidden_scheme_is_not_invented(self):
        # Only a renderer's explicit URL can complete a hidden scheme; this string alone cannot.
        self.assertIsNone(yakala.tab_url('github.com/avenoxai'))
        self.assertIsNone(yakala.tab_url('chrome://settings'))
        self.assertEqual(yakala.tab_url('https://github.com/avenoxai'), 'https://github.com/avenoxai')

    def test_unsupported_browser_has_no_inferred_url(self):
        sources = yakala.windows_sources({'uygulama': 'opera'}, '', False)
        self.assertFalse(any(s['kind'] == 'url' for s in sources))

    def test_png_dimension_limit(self):
        head = struct.pack('>IIBBBBB', 100000, 100000, 8, 2, 0, 0, 0)
        data = yakala.PNG_MAGIC + yakala._png_chunk(b'IHDR', head) + yakala._png_chunk(b'IDAT', b'x') + yakala._png_chunk(b'IEND', b'')
        self.assertIsNone(yakala.png_info(data))

    def test_write_card_cleans_failed_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'card.md'
            system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
            with patch.object(yakala, 'os', system), patch.object(system, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    yakala.write_card(path, {'tur': 'yakala'}, 'PRIVATE')
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
