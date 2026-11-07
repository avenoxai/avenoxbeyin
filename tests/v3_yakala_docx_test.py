"""Captured DOCX text is usable without an optional parser package."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
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

    def test_partial_text_keeps_its_warning_on_the_card_until_a_clean_rerun(self):
        raw = document('<w:p><w:r><w:t>Görünen metin</w:t></w:r><w:r><w:drawing/></w:r></w:p>',
                       {'word/media/image1.png': b'fake image'})
        _, info, card_id = self.capture_and_extract(raw)
        self.assertIn('unsupported_content', info['uyari'])
        with patch.object(yakala, 'tools', return_value={}):
            row = yakala.process(self.vault, [card_id])['kartlar'][0]
        self.assertEqual((row['durum'], row['yontem']), ('cikarildi', 'dosya'))
        self.assertIn('eksik olabilir', row['not'])
        card = yakala.find_card(self.vault, card_id)
        self.assertIn('öğrenim.docx: additional_content, unsupported_content', card['meta']['uyari'])
        self.assertIn('Görünen metin', (self.vault / row['ham']).read_text(encoding='utf-8'))
        copy = self.vault / card['body'].split('[[', 1)[1].split(']]', 1)[0]
        copy.write_bytes(document('<w:p><w:r><w:t>Tam metin</w:t></w:r></w:p>'))
        with patch.object(yakala, 'tools', return_value={}):
            row = yakala.process(self.vault, [card_id])['kartlar'][0]
        self.assertNotIn('not', row)
        self.assertNotIn('uyari', yakala.find_card(self.vault, card_id)['meta'])

    def test_archive_limits_and_unsupported_content(self):
        bomb = document('<w:p><w:r><w:t>safe</w:t></w:r></w:p>',
                        {'word/media/bomb.bin': b'x' * (8 * 1024 * 1024 + 1)})
        self.assertEqual(extract_docx(bomb)['issues'], ['archive_compression_limit'])
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


class DocxContentTest(unittest.TestCase):
    def test_declared_total_size_is_bounded_without_decompressing(self):
        from beyin_v3_docx import MAX_ARCHIVE
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('word/document.xml',
                f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>safe</w:t></w:r></w:p></w:body></w:document>')
            for index in range(3):
                archive.writestr(f'word/media/image{index}.bin', b'y' * 1024)
        raw = bytearray(stream.getvalue())
        offset = raw.find(b'PK\x01\x02')
        while offset >= 0:  # central directory: declared uncompressed size at +24
            name = bytes(raw[offset + 46:offset + 46 + int.from_bytes(raw[offset + 28:offset + 30], 'little')])
            if name.startswith(b'word/media/'):
                raw[offset + 24:offset + 28] = (MAX_ARCHIVE // 3 + 1).to_bytes(4, 'little')
            offset = raw.find(b'PK\x01\x02', offset + 4)
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('member opened')):
            self.assertEqual(extract_docx(bytes(raw))['issues'], ['archive_resource_limit'])

    def test_large_embedded_photo_does_not_hide_the_text(self):
        # Photos barely compress: 9 MiB of media must not cost the document its text.
        photo = bytes(range(256)) * (9 * 4096)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml',
                f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Fotoğraflı rapor</w:t></w:r></w:p>'
                '<w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>')
            archive.writestr('word/media/image1.jpeg', photo, compress_type=zipfile.ZIP_STORED)
        result = extract_docx(stream.getvalue())
        self.assertEqual(result['text'], 'Fotoğraflı rapor')
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['issues'], ['additional_content', 'unsupported_content'])

    def test_tracked_move_source_is_not_duplicated(self):
        result = extract_docx(document(
            '<w:p><w:moveFrom w:id="1"><w:r><w:t>Taşınan paragraf</w:t></w:r></w:moveFrom></w:p>'
            '<w:p><w:r><w:t>Arada</w:t></w:r></w:p>'
            '<w:p><w:moveTo w:id="2"><w:r><w:t>Taşınan paragraf</w:t></w:r></w:moveTo></w:p>'))
        self.assertEqual(result['text'], 'Arada\n\nTaşınan paragraf')
        self.assertEqual(result['state'], 'partial')
        self.assertIn('unsupported_content', result['issues'])

    def test_turkish_text_is_returned_in_nfc(self):
        decomposed = 'S\u0327ifre, Mu\u0308s\u0327teri, Ars\u0327iv, I\u0307stanbul'
        result = extract_docx(document('<w:p><w:r><w:t>' + decomposed + '</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Gu\u0308n</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['text'], 'Şifre, Müşteri, Arşiv, İstanbul\n\nGün')


class CopiedCliDocxTest(unittest.TestCase):
    """The hotkey listener runs a copy of the Yakala file alone from the state folder (#501)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-docx-copy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.home, self.cwd = self.root / 'home', self.root / 'neutral'
        for folder in (self.vault, self.state / 'yakala', self.home, self.cwd):
            folder.mkdir(parents=True)
        (self.vault / '.beyin-runtime.json').write_text(
            json.dumps({'state': str(self.state)}), encoding='utf-8')
        self.runner = self.state / 'yakala/beyin_v3_yakala.py'
        self.runner.write_bytes((SCRIPTS / 'beyin_v3_yakala.py').read_bytes())
        self.source = self.cwd / 'müşteri-raporu.docx'
        self.source.write_bytes(document('<w:p><w:r><w:t>Şifre değil, Müşteri özeti</w:t></w:r></w:p>'))
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                                 XDG_STATE_HOME=str(self.root / 'xdg'),
                                 LOCALAPPDATA=str(self.home / 'local'), APPDATA=str(self.home / 'roaming'),
                                 PATH='', BEYIN_UPDATES_OFF='1', BEYIN_V3_NO_SPAWN='1')
        self.env.pop('PYTHONPATH', None)
        self.env.pop('PYTHONHOME', None)

    def command(self, *args):
        # -I -S: neither the script folder nor PYTHONPATH can supply a sibling module.
        result = subprocess.run([sys.executable, '-I', '-S', str(self.runner), '--vault', str(self.vault),
                                 '--json', *args], cwd=self.cwd, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def capture_and_process(self):
        captured = self.command('ekle', str(self.source))
        processed = self.command('isle', captured['id'], '--ses-yok')
        return processed['kartlar'][0]

    def test_copy_without_reader_keeps_agent_fallback(self):
        row = self.capture_and_process()
        self.assertEqual((row['durum'], row['yontem']), ('cikarildi', 'ajan-okusun'), row)
        self.assertIn('müşteri-raporu.docx: docx_reader_missing', row['not'])
        self.assertNotIn('ModuleNotFoundError', row['not'])

    def test_adjacent_reader_loads_without_script_folder_on_path(self):
        (self.runner.parent / 'beyin_v3_docx.py').write_bytes((SCRIPTS / 'beyin_v3_docx.py').read_bytes())
        row = self.capture_and_process()
        self.assertEqual((row['durum'], row['yontem']), ('cikarildi', 'dosya'), row)
        stored = (self.vault / row['ham']).read_text(encoding='utf-8')
        self.assertIn('Şifre değil, Müşteri özeti', stored)
        self.assertIn('visibility: private', stored)


if __name__ == '__main__':
    unittest.main()
