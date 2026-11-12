"""A mixed file card keeps extracted text and names a PDF with no text layer."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

from v3_package_helpers import inherited_env


ROOT = Path(__file__).resolve().parents[1]


def write_pdf(path, text=False):
    """Write a valid one page raster PDF or one with a real text layer."""
    if text:
        content = b'BT /F1 12 Tf 10 50 Td (Searchable PDF words) Tj ET'
        resources = b'<< /Font << /F1 4 0 R >> >>'
        embedded = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
    else:
        pixels = zlib.compress(b'\0\0\0' * 4)
        content = b'q 100 0 0 100 0 0 cm /Im0 Do Q'
        resources = b'<< /XObject << /Im0 4 0 R >> >>'
        embedded = (b'<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB '
                    b'/BitsPerComponent 8 /Filter /FlateDecode /Length ' + str(len(pixels)).encode() +
                    b' >>\nstream\n' + pixels + b'\nendstream')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Resources ' + resources + b' /Contents 5 0 R >>',
        embedded,
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
    ]
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)


@unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext required for a real PDF extraction check')
class PdfWarningCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='yakala-pdf-warning-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), USERPROFILE=str(root / 'home'),
                                 APPDATA=str(root / 'appdata'), LOCALAPPDATA=str(root / 'localappdata'),
                                 XDG_STATE_HOME=str(root / 'xdg'))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *map(str, args)],
                                capture_output=True, text=True, encoding='utf-8', env=self.env,
                                cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_mixed_text_and_image_pdf_reports_pdf_in_result_and_card(self):
        source = self.vault.parent / 'ders.txt'
        full_text = 'Birinci satır.\nİkinci satır: tam metin korunur.\n'
        source.write_text(full_text, encoding='utf-8')
        pdf = self.vault.parent / 'taranmış not.pdf'
        write_pdf(pdf)
        extracted = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True)
        self.assertEqual(extracted.returncode, 0, extracted.stderr)
        self.assertFalse(extracted.stdout.strip())
        added = self.cli('ekle', source, pdf)
        entry = self.cli('isle', added['id'])['kartlar'][0]
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'dosya'))
        self.assertIn(pdf.name, entry['not'])
        card_text = (self.vault / entry['kart']).read_text(encoding='utf-8')
        self.assertIn('uyari:', card_text.split('---', 2)[1])
        self.assertIn(pdf.name, card_text.split('---', 2)[1])
        raw = (self.vault / entry['ham']).read_text(encoding='utf-8')
        self.assertIn(full_text, raw)
        self.assertNotIn('## ' + pdf.name, raw)

        # A later retry can extract a repaired text layer and clears the old warning.
        captured_pdf = self.vault / '📥 000-Inbox/Yakala/dosyalar' / pdf.name
        write_pdf(captured_pdf, text=True)
        again = self.cli('isle', added['id'], '--tekrar')['kartlar'][0]
        self.assertNotIn('not', again)
        self.assertNotIn('uyari:', (self.vault / again['kart']).read_text(encoding='utf-8'))
        self.assertIn('Searchable PDF words', (self.vault / again['ham']).read_text(encoding='utf-8'))

    def test_successful_pdf_and_other_file_types_keep_previous_behavior(self):
        pdf = self.vault.parent / 'searchable.pdf'
        write_pdf(pdf, text=True)
        binary = self.vault.parent / 'archive.bin'
        binary.write_bytes(b'\0\1\2')
        added = self.cli('ekle', pdf, binary)
        entry = self.cli('isle', added['id'])['kartlar'][0]
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'dosya'))
        self.assertNotIn('not', entry)
        self.assertNotIn('uyari:', (self.vault / entry['kart']).read_text(encoding='utf-8'))
        self.assertIn('Searchable PDF words', (self.vault / entry['ham']).read_text(encoding='utf-8'))

    def test_pdf_only_warning_and_retry_do_not_keep_old_raw_reference(self):
        pdf = self.vault.parent / 'sayfa.pdf'
        write_pdf(pdf)
        added = self.cli('ekle', pdf)
        first = self.cli('isle', added['id'])['kartlar'][0]
        self.assertEqual((first['durum'], first['yontem']), ('cikarildi', 'ajan-okusun'))
        self.assertIn(pdf.name, first['not'])
        card = self.vault / first['kart']
        self.assertIn('uyari:', card.read_text(encoding='utf-8'))
        captured_pdf = self.vault / '📥 000-Inbox/Yakala/dosyalar' / pdf.name
        write_pdf(captured_pdf, text=True)
        success = self.cli('isle', added['id'], '--tekrar')['kartlar'][0]
        self.assertNotIn('not', success)
        self.assertIn('ham:', card.read_text(encoding='utf-8'))
        write_pdf(captured_pdf)
        failed_again = self.cli('isle', added['id'], '--tekrar')['kartlar'][0]
        self.assertEqual(failed_again['yontem'], 'ajan-okusun')
        self.assertIn(pdf.name, failed_again['not'])
        self.assertNotIn('ham:', card.read_text(encoding='utf-8'))
        self.assertIn('uyari:', card.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
