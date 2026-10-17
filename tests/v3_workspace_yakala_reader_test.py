"""One document service, identical text/location and source trust across callers."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import v3_workspace_documents_test as documents
import beyin_v3_yakala as yakala
from beyin_v3_sources import extract_document_bytes


class SharedDocumentReaderTest(unittest.TestCase):
    def setUp(self):
        self.h=documents.WorkspaceDocumentsTest();self.h.setUp();self.addCleanup(self.h.h.tmp.cleanup)
        self.vault=self.h.h.vault;self.material=self.h.h.material

    def test_pdf_docx_workspace_and_yakala_share_content_locations_and_hash(self):
        pdf=documents.unicode_pdf('Türkçe öğrenim English quartz')
        docx=documents.docx_bytes('<w:p><w:r><w:t>Öğrenim quartz</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Sonuç 82</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        for suffix,raw in (('pdf',pdf),('docx',docx)):
            with self.subTest(suffix=suffix):
                path=self.material/('same.'+suffix);path.write_bytes(raw)
                workspace=self.h.resolver.extract('academic','materials',path.name,'user')
                captured=yakala.capture(self.vault,files=[path]);card=yakala.find_card(self.vault,captured['id'])
                info={};text,method,note=yakala.extract(self.vault,card,{},info=info)
                self.assertEqual(method,'dosya');self.assertIsNone(note)
                for chunk in workspace['chunks']:self.assertIn(chunk['text'],text)
                self.assertEqual(info['documents'][0]['fragments'],[c['source_ref']['fragment'] for c in workspace['chunks']])
                self.assertEqual(info['documents'][0]['source_sha256'],workspace['source_sha256'])
                with patch.object(yakala,'tools',return_value={}):result=yakala.process(self.vault,[captured['id']])
                self.assertEqual(result['kartlar'][0]['cikarim_durumu'],'ready')
                processed=yakala.find_card(self.vault,captured['id'])
                self.assertIsInstance(processed['meta']['belge_cikarim'],list)
                raw_path=self.vault/result['kartlar'][0]['ham'];stored=raw_path.read_text()
                self.assertIn('visibility: private',stored);self.assertIn('trust: untrusted',stored)
                self.assertIn('Güvenilmeyen kaynak metni',stored);self.assertEqual(path.read_bytes(),raw)

    def test_missing_corrupt_and_partial_are_visible_not_empty_ready(self):
        cases=[('broken.pdf',b'not-pdf','failed'),('empty.docx',documents.docx_bytes(''),'partial')]
        for name,raw,state in cases:
            path=self.material/name;path.write_bytes(raw)
            captured=yakala.capture(self.vault,files=[path])
            with patch.object(yakala,'tools',return_value={}):result=yakala.process(self.vault,[captured['id']])
            entry=result['kartlar'][0];self.assertEqual(entry['durum'],'hata');self.assertEqual(entry['belge_cikarim'][0]['state'],state)
        result=extract_document_bytes(self.material,documents.unicode_pdf('quartz'),'pdf')
        self.assertEqual(result['state'],'missing_dependency');self.assertFalse(result['chunks'])

    def test_source_changed_during_shared_read_does_not_publish(self):
        directory=self.vault/'docs';directory.mkdir();path=directory/'same.pdf';path.write_bytes(documents.unicode_pdf('quartz'))
        card={'meta':{'kaynak_turu':'dosya'},'body':'## Dosyalar\n\n[[docs/same.pdf]]\n'}
        actual=extract_document_bytes
        def changed(*args,**kwargs):
            result=actual(*args,**kwargs);path.write_bytes(b'manual edit');return result
        with patch('beyin_v3_sources.extract_document_bytes',changed),self.assertRaisesRegex(ValueError,'stale_source'):
            yakala.extract(self.vault,card,{})
        self.assertEqual(path.read_bytes(),b'manual edit')


if __name__=='__main__':unittest.main()
