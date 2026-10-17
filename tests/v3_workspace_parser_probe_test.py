import sys
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
from beyin_v3_parser_probe import document_capabilities
from beyin_v3_parser_assets import managed_files, NAME


class ParserProbeTest(unittest.TestCase):
    def test_real_reader_capabilities_missing_and_corrupt_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = {c['name']: c['state'] for c in document_capabilities(root, include_ocr=False)}
            self.assertEqual(missing, {'pdf_text': 'missing_dependency', 'docx_text': 'ready'})
            for name, raw in managed_files().items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            self.assertEqual({c['name']: c['state'] for c in document_capabilities(root, include_ocr=False)},
                             {'pdf_text': 'ready', 'docx_text': 'ready'})
            (root / '.claude/parser-assets' / NAME).write_bytes(b'corrupt')
            self.assertEqual({c['name']: c['state'] for c in document_capabilities(root, include_ocr=False)},
                             {'pdf_text': 'failed', 'docx_text': 'ready'})
            self.assertFalse((root / '.beyin-workspaces').exists())
            self.assertNotIn('pypdf', sys.modules)


    def install_assets(self, root):
        for name, raw in managed_files().items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def test_missing_native_tools_and_languages_are_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.install_assets(root)
            with patch.dict(os.environ, {'PATH': ''}):
                caps = {c['name']: c for c in document_capabilities(root)}
            self.assertEqual(caps['pdf_text']['state'], 'ready')
            self.assertEqual(caps['ocr_tr_en']['state'], 'missing_dependency')
            self.assertIn('install_poppler_pdftoppm_and_tesseract', caps['ocr_tr_en']['detail'])
            from v3_workspace_package_test import native_ocr_ready
            if native_ocr_ready():
                empty = root / 'empty-languages'; empty.mkdir()
                with patch.dict(os.environ, {'TESSDATA_PREFIX': str(empty)}):
                    caps = {c['name']: c for c in document_capabilities(root)}
                self.assertEqual(caps['ocr_tr_en']['state'], 'missing_dependency')
                self.assertIn('install_tesseract_tur_and_eng', caps['ocr_tr_en']['detail'])

    def test_real_installed_ocr_and_corrupt_probe(self):
        from v3_workspace_package_test import native_ocr_ready
        if not native_ocr_ready():
            self.skipTest('native TR/EN OCR tools/languages unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.install_assets(root)
            caps = {c['name']: c['state'] for c in document_capabilities(root)}
            self.assertEqual(caps, {'pdf_text': 'ready', 'docx_text': 'ready', 'ocr_tr_en': 'ready'})
            (root / '.claude/parser-assets/ocr-probe-tr-en.pdf').write_bytes(b'corrupt')
            caps = {c['name']: c['state'] for c in document_capabilities(root)}
            self.assertEqual(caps['ocr_tr_en'], 'failed')
            self.assertEqual(caps['pdf_text'], 'ready')
            self.assertFalse((root / '.beyin-workspaces').exists())
