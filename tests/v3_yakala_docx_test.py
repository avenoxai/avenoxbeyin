"""Captured DOCX text is usable without an optional parser package."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as yakala
from beyin_v3_docx import extract_docx

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def document(body, extras=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml',
            f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


class YakalaDocxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)

    def capture_and_extract(self, data):
        source = self.vault / 'öğrenim.docx'
        source.write_bytes(data)
        captured = yakala.capture(self.vault, files=[source])
        card = yakala.find_card(self.vault, captured['id'])
        info = {}
        value = yakala.extract(self.vault, card, {}, info=info)
        return value, info, captured['id']

    def test_text_and_table_are_saved_as_untrusted_raw_capture(self):
        raw = document('<w:p><w:r><w:t>Öğrenim 日本語</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Sonuç 82</w:t></w:r></w:p>'
            '</w:tc></w:tr></w:tbl>')
        (text, method, note), _, card_id = self.capture_and_extract(raw)
        self.assertEqual(method, 'dosya')
        self.assertIsNone(note)
        self.assertIn('Öğrenim 日本語', text)
        self.assertIn('Sonuç 82', text)
        with patch.object(yakala, 'tools', return_value={}):
            result = yakala.process(self.vault, [card_id])
        self.assertEqual(result['kartlar'][0]['durum'], 'cikarildi')
        stored = (self.vault / result['kartlar'][0]['ham']).read_text(encoding='utf-8')
        self.assertIn('visibility: private', stored)
        self.assertIn('Güvenilmeyen kaynak metni', stored)
        self.assertIn('Sonuç 82', stored)
        self.assertEqual((self.vault / 'öğrenim.docx').read_bytes(), raw)

    def test_image_only_document_keeps_agent_review_fallback(self):
        raw = document('<w:p><w:r><w:drawing/></w:r></w:p>',
                       {'word/media/image1.png': b'fake image'})
        (text, method, note), _, card_id = self.capture_and_extract(raw)
        self.assertIsNone(text)
        self.assertEqual(method, 'ajan-okusun')
        self.assertIn('no_extractable_text', note)
        with patch.object(yakala, 'tools', return_value={}):
            result = yakala.process(self.vault, [card_id])
        self.assertEqual(result['kartlar'][0]['yontem'], 'ajan-okusun')
        self.assertIn('no_extractable_text', result['kartlar'][0]['not'])

    def test_archive_limits_and_unsupported_content(self):
        bomb = document('<w:p><w:r><w:t>safe</w:t></w:r></w:p>',
                        {'word/media/bomb.bin': b'x' * (8 * 1024 * 1024 + 1)})
        self.assertEqual(extract_docx(bomb)['issues'], ['archive_resource_limit'])
        partial = document('<w:p><w:r><w:t>Visible</w:t></w:r><w:r><w:drawing/></w:r></w:p>')
        result = extract_docx(partial)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['text'], 'Visible')
        self.assertIn('unsupported_content', result['issues'])
        self.assertEqual(extract_docx(b'broken')['state'], 'failed')

    def test_source_bound_and_xml_declarations_fail_safely(self):
        from beyin_v3_docx import MAX_SOURCE
        self.assertEqual(extract_docx(b'x' * (MAX_SOURCE + 1))['issues'], ['source_too_large'])
        crafted = document('<w:p><w:r><w:t>unsafe</w:t></w:r></w:p>')
        with zipfile.ZipFile(io.BytesIO(crafted)) as archive:
            xml = archive.read('word/document.xml').decode('utf-8')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('word/document.xml',
                ('<!DOCTYPE w:document [<!ENTITY x "expanded">]>' + xml).encode('utf-16'))
        self.assertEqual(extract_docx(stream.getvalue())['issues'], ['xml_declarations_unsupported'])

    def test_archive_metadata_and_compression_are_rejected_before_zipfile_init(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for index in range(2049):
                archive.writestr(f'entry-{index}.txt', b'x')
        with patch('beyin_v3_docx.zipfile.ZipFile', side_effect=AssertionError('ZipFile initialized')):
            self.assertEqual(extract_docx(stream.getvalue())['issues'], ['archive_metadata_limit'])

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for index in range(20):
                info = zipfile.ZipInfo(f'entry-{index}.txt')
                info.comment = b'x' * 15000
                archive.writestr(info, b'x')
        with patch('beyin_v3_docx.zipfile.ZipFile', side_effect=AssertionError('ZipFile initialized')):
            self.assertEqual(extract_docx(stream.getvalue())['issues'], ['archive_metadata_limit'])

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_LZMA) as archive:
            archive.writestr('word/document.xml', '<w:document/>')
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('member opened')):
            self.assertEqual(extract_docx(stream.getvalue())['issues'], ['unsupported_compression'])

    def test_malformed_zip64_metadata_fails_before_zipfile_init(self):
        raw = bytearray(document('<w:p><w:r><w:t>safe</w:t></w:r></w:p>'))
        eocd = raw.rfind(b'PK\x05\x06')
        raw[eocd + 10:eocd + 14] = b'\xff\xff\xff\xff'
        with patch('beyin_v3_docx.zipfile.ZipFile', side_effect=AssertionError('ZipFile initialized')):
            self.assertEqual(extract_docx(bytes(raw))['issues'], ['archive_metadata_limit'])

    def test_corrupt_deflate_payload_falls_back_without_raising(self):
        raw = bytearray(document('<w:p><w:r><w:t>' + ('safe ' * 500) + '</w:t></w:r></w:p>'))
        local = raw.find(b'PK\x03\x04')
        name_size = int.from_bytes(raw[local + 26:local + 28], 'little')
        extra_size = int.from_bytes(raw[local + 28:local + 30], 'little')
        data_offset = local + 30 + name_size + extra_size
        compressed_size = int.from_bytes(raw[local + 18:local + 22], 'little')
        raw[data_offset + compressed_size // 2] ^= 0xff
        self.assertEqual(extract_docx(bytes(raw))['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
