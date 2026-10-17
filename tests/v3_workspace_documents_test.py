import io
import json
import shutil
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'template/.claude/scripts'))
from beyin_v3_sources import SourceResolver,default_readers
from beyin_v3_parser_assets import managed_files
from beyin_v3_extract_reader import PYPDF_NAME,extract_docx,W


def unicode_pdf(text):
    cmap='\n'.join('<%04X> <%04X>'%(ord(c),ord(c)) for c in sorted(set(text)))
    cmap=(' /CIDInit /ProcSet findresource begin 12 dict begin begincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /Test def /CMapType 2 def\n1 begincodespacerange <0000> <FFFF> endcodespacerange\n'+str(len(set(text)))+' beginbfchar\n'+cmap+'\nendbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end').encode()
    content=('BT /F1 12 Tf 72 720 Td <'+text.encode('utf-16-be').hex()+'> Tj ET').encode()
    stream=lambda raw:b'<< /Length '+str(len(raw)).encode()+b' >>\nstream\n'+raw+b'\nendstream'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H /DescendantFonts [7 0 R] /ToUnicode 6 0 R >>',
        stream(content),stream(cmap),b'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Synthetic /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor 8 0 R /DW 500 >>',
        b'<< /Type /FontDescriptor /FontName /Synthetic /Flags 4 /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>']
    raw=b'%PDF-1.4\n';offsets=[]
    for number,obj in enumerate(objects,1):offsets.append(len(raw));raw+=str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(raw);raw+=b'xref\n0 9\n0000000000 65535 f \n'+b''.join(('%010d 00000 n \n'%o).encode() for o in offsets)
    return raw+b'trailer\n<< /Size 9 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'


def docx_bytes(body,extra=None):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr('word/document.xml','<w:document xmlns:w="'+W[1:-1]+'"><w:body>'+body+'</w:body></w:document>')
        for name,value in (extra or {}).items():z.writestr(name,value)
    return buffer.getvalue()


class WorkspaceDocumentsTest(unittest.TestCase):
    def setUp(self):
        # Reuse isolated setup only, without importing a second TestCase into discovery.
        import v3_workspace_sources_test as source_tests
        self.h=source_tests.WorkspaceSourcesTest();self.h.setUp();self.addCleanup(self.h.tmp.cleanup)
        for name,raw in managed_files().items():
            path=self.h.vault/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        self.resolver=SourceResolver(self.h.registry)

    def test_real_pdf_tr_en_page_and_docx_table_locations(self):
        expected='Türkçe ölçüm öğrenim ödev — English quartz.'
        (self.h.material/'ders.pdf').write_bytes(unicode_pdf(expected))
        pdf=self.resolver.extract('academic','materials','ders.pdf','user')
        self.assertEqual(pdf['state'],'ready');self.assertIn(expected,pdf['chunks'][0]['text'])
        self.assertEqual(pdf['chunks'][0]['source_ref']['fragment'],{'kind':'pdf_page','page':1})
        body='<w:p><w:r><w:t>Türkçe öğrenim. English source.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Ödev</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sonuç 82</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        path=self.h.material/'ders.docx';path.write_bytes(docx_bytes(body));before=path.read_bytes()
        docx=self.resolver.extract('academic','materials','ders.docx','user')
        self.assertEqual(docx['state'],'ready');self.assertEqual(docx['chunks'][-1]['text'],'Sonuç 82')
        self.assertEqual(docx['chunks'][-1]['source_ref']['fragment'],{'kind':'docx_block','block':2,'table':1,'row':1,'cell':2})
        self.assertEqual(path.read_bytes(),before)

    @unittest.skipUnless(__import__('v3_workspace_package_test').native_ocr_ready(),'native TR/EN OCR tools/languages unavailable')
    def test_real_raster_ocr_tr_en_and_mixed_pages(self):
        raw=(Path(__file__).parent/'fixtures/v3/workspaces/runtime/ocr-scan-tr-en.pdf').read_bytes()
        path=self.h.material/'raster.pdf';path.write_bytes(raw)
        result=self.resolver.extract('academic','materials','raster.pdf','user')
        self.assertEqual(result['state'],'ready',result)
        text=result['chunks'][0]['text']
        for expected in ('Türkçe öğrenim','yüzey çatladı','English ceramic firing'):self.assertIn(expected,text)
        self.assertEqual(result['chunks'][0]['source_ref']['fragment'],{'kind':'pdf_page','page':1})
        self.assertEqual(path.read_bytes(),raw)
        wheel=self.h.vault/'.claude/parser-assets'/PYPDF_NAME
        (self.h.material/'text.pdf').write_bytes(unicode_pdf('Text quartz'))
        code='import sys;sys.path.insert(0,sys.argv[1]);from pypdf import PdfWriter;from pathlib import Path;r=Path(sys.argv[2]);w=PdfWriter();w.append(r/"text.pdf");w.append(r/"raster.pdf");w.write(r/"combined.pdf")'
        subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(wheel),str(self.h.material)],check=True,capture_output=True)
        mixed=self.resolver.extract('academic','materials','combined.pdf','user')
        self.assertEqual(mixed['state'],'ready',mixed)
        self.assertEqual([c['source_ref']['fragment']['page'] for c in mixed['chunks']],[1,2])
        self.assertIn('English ceramic',mixed['chunks'][1]['text'])

    def test_missing_native_ocr_dependency_never_reports_ready(self):
        raw=(Path(__file__).parent/'fixtures/v3/workspaces/runtime/ocr-scan-tr-en.pdf').read_bytes()
        (self.h.material/'raster.pdf').write_bytes(raw)
        with patch.dict('os.environ',{'PATH':''}):
            result=SourceResolver(self.h.registry).extract('academic','materials','raster.pdf','user')
        self.assertEqual(result['state'],'missing_dependency');self.assertFalse(result['chunks'])

    def test_partial_docx_parts_and_revisions_are_visible(self):
        cases=[('<w:p><w:r><w:t>Main</w:t></w:r></w:p>',{'word/header1.xml':'header'}),
            ('<w:p><w:del><w:r><w:delText>DELETED_CANARY</w:delText></w:r></w:del><w:ins><w:r><w:t>Yeni</w:t></w:r></w:ins></w:p>',{}),
            ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Dış</w:t></w:r></w:p><w:tbl/></w:tc></w:tr></w:tbl>',{})]
        for body,extra in cases:
            result=extract_docx(docx_bytes(body,extra))
            self.assertEqual(result['state'],'partial');self.assertNotIn('DELETED_CANARY',str(result['chunks']))

    def test_docx_path_escape_entity_bomb_empty_and_invalid_archive(self):
        for raw in [docx_bytes('<w:p/>',{'../outside.xml':'no'}),docx_bytes('<w:p><w:r><w:t>'+'A'*200000+'</w:t></w:r></w:p>'),b'invalid']:
            self.assertEqual(extract_docx(raw)['state'],'failed')
        self.assertEqual(extract_docx(docx_bytes(''))['state'],'partial')

    def test_real_pdf_password_scan_mixed_and_broken_status(self):
        original=self.h.material/'source.pdf';original.write_bytes(unicode_pdf('Ödev quartz'))
        wheel=self.h.vault/'.claude/parser-assets'/PYPDF_NAME
        code='import sys;sys.path.insert(0,sys.argv[1]);from pypdf import PdfWriter;from pathlib import Path;r=Path(sys.argv[2]);w=PdfWriter();w.add_blank_page(width=612,height=792);w.write(r/"scan.pdf");w=PdfWriter();w.append(r/"source.pdf");w.encrypt("synthetic");w.write(r/"locked.pdf");w=PdfWriter();w.append(r/"source.pdf");w.add_blank_page(width=612,height=792);w.write(r/"mixed.pdf")'
        subprocess.run([sys.executable,'-I','-S','-B','-c',code,str(wheel),str(self.h.material)],check=True,capture_output=True)
        (self.h.material/'broken.pdf').write_bytes(b'not a PDF')
        readers=default_readers(self.h.vault);readers['.pdf']['command'].remove('--ocr')
        self.resolver=SourceResolver(self.h.registry,readers=readers)
        for name,state in [('scan.pdf','requires_ocr'),('locked.pdf','requires_password'),('mixed.pdf','partial'),('broken.pdf','failed')]:
            with self.subTest(name=name):self.assertEqual(self.resolver.extract('academic','materials',name,'user')['state'],state)


if __name__=='__main__':unittest.main()
