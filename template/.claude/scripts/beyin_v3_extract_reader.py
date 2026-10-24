"""Isolated byte-only PDF/DOCX reader selected in M03.

Run with Python -I -S -B. pypdf is imported dynamically ONLY in this child from
the installer-owned, SHA256-pinned wheel. The memory/hook process never imports it.
DOCX uses bounded ZIP/XML stdlib parsing; unsupported stories remain partial.
"""
import argparse
import base64
import hashlib
import importlib
import io
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path,PurePosixPath
import sys
import xml.etree.ElementTree as ET
import zipfile

PYPDF_NAME='pypdf-6.21.0-py3-none-any.whl'
PYPDF_SHA256='3aec619699dacb1b73f57e87d9adaa4424814517b2cc028de20adba5a37af4c4'
MAX_SOURCE=32*1024*1024
MAX_TEXT=1024*1024
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def result(state,chunks=(),issues=()):
    return {'state':state,'chunks':list(chunks),'issues':[{'code':code} for code in issues]}


def extract_docx(raw):
    chunks=[];issues=set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            items=archive.infolist();names=[i.filename for i in items]
            if len(items)>2048 or sum(i.file_size for i in items)>8*1024*1024:raise ValueError('archive_resource_limit')
            if len(set(names))!=len(names):raise ValueError('duplicate_archive_member')
            for item in items:
                name=PurePosixPath(item.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename:raise ValueError('unsafe_archive_member')
                if item.flag_bits&1:raise ValueError('encrypted_zip_unsupported')
                if item.file_size>max(4096,item.compress_size*200):raise ValueError('archive_compression_limit')
            entry=archive.getinfo('word/document.xml')
            if entry.file_size>2*1024*1024:raise ValueError('xml_resource_limit')
            with archive.open(entry) as stream:xml=stream.read(2*1024*1024+1)
            if len(xml)>2*1024*1024:raise ValueError('xml_resource_limit')
            decoded=xml.decode('utf-16') if xml[:2] in (b'\xff\xfe',b'\xfe\xff') else xml.decode('utf-8-sig')
            if '\0' in decoded or '<!DOCTYPE' in decoded.upper() or '<!ENTITY' in decoded.upper():raise ValueError('xml_declarations_unsupported')
            root=ET.fromstring(xml)
            if root.tag!=W+'document':raise ValueError('unsupported_wordprocessing_namespace')
            body=root.find(W+'body')
            if body is None:raise ValueError('document_body_missing')
            unsupported={'drawing','pict','object','ins','del','fldChar','instrText','footnoteReference','endnoteReference','commentReference','altChunk','sdt','txbxContent','sym','numPr'}
            for element in root.iter():
                local=element.tag.rsplit('}',1)[-1]
                if local in unsupported:issues.add('unsupported_'+local)
            if any(name.startswith(('word/header','word/footer')) or name in ('word/footnotes.xml','word/endnotes.xml','word/comments.xml') for name in names):
                issues.add('additional_stories_not_locatable')
            def text(element):
                pieces=[]
                def walk(node,depth=0):
                    if depth>64:raise ValueError('xml_depth_limit')
                    if node.tag in (W+'del',W+'txbxContent',W+'drawing',W+'pict'):return
                    if node.tag==W+'t':pieces.append(node.text or '')
                    elif node.tag==W+'tab':pieces.append('\t')
                    elif node.tag in (W+'br',W+'cr'):pieces.append('\n')
                    else:
                        for child in node:walk(child,depth+1)
                walk(element);return ''.join(pieces)
            block_no=0;table_no=0;total=0
            def append(value,fragment):
                nonlocal total
                if not value:return
                if total+len(value)>MAX_TEXT:raise ValueError('output_text_limit')
                total+=len(value);chunks.append({'text':value,'fragment':fragment})
            for block in body:
                if block.tag==W+'sectPr':continue
                if block.tag not in (W+'p',W+'tbl'):issues.add('unsupported_body_block');continue
                block_no+=1
                if block.tag==W+'p':append(text(block),{'kind':'docx_block','block':block_no})
                else:
                    table_no+=1
                    for row_no,row in enumerate(block.findall(W+'tr'),1):
                        for cell_no,cell in enumerate(row.findall(W+'tc'),1):
                            if cell.find('.//'+W+'tbl') is not None:issues.add('nested_table')
                            append('\n'.join(text(p) for p in cell.findall(W+'p')),
                                {'kind':'docx_block','block':block_no,'table':table_no,'row':row_no,'cell':cell_no})
        if not chunks:issues.add('no_extractable_text')
        return result('partial' if issues else 'ready',chunks,sorted(issues))
    except ValueError as exc:
        return result('partial' if chunks else 'failed',chunks,[str(exc)])
    except (KeyError,UnicodeError,ET.ParseError,zipfile.BadZipFile,NotImplementedError,RecursionError):
        return result('failed',issues=['invalid_docx'])


def ocr_page(raw,page):
    """Local native tools receive only a private temporary copy of supplied bytes.

    No source locator, network, shell or user-configured command is accepted.
    The enclosing reader process and parent enforce memory/time/output limits.
    """
    renderer=shutil.which('pdftoppm');recognizer=shutil.which('tesseract')
    if not renderer or not recognizer:return None,'missing_ocr_dependency'
    try:
        languages=subprocess.run([recognizer,'--list-langs'],capture_output=True,timeout=3)
        if languages.returncode or not {'tur','eng'}.issubset(set(languages.stdout.decode(errors='replace').splitlines())):
            return None,'missing_ocr_languages'
        with tempfile.TemporaryDirectory(prefix='beyin-ocr-') as directory:
            source=Path(directory)/'source.pdf';source.write_bytes(raw)
            image=Path(directory)/'page'
            rendered=subprocess.run([renderer,'-f',str(page),'-l',str(page),'-singlefile','-scale-to','2400','-png',str(source),str(image)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
            path=image.with_suffix('.png')
            if rendered.returncode or not path.is_file() or path.stat().st_size>32*1024*1024:return None,'ocr_render_failed'
            text=subprocess.run([recognizer,str(path),'stdout','-l','tur+eng','--psm','3'],capture_output=True,timeout=8)
            if text.returncode:return None,'ocr_recognition_failed'
            value=text.stdout.decode('utf-8')
            if len(value)>MAX_TEXT:return None,'output_text_limit'
            return value,None
    except subprocess.TimeoutExpired:return None,'ocr_timeout'
    except (OSError,UnicodeError):return None,'ocr_failed'


def extract_pdf(raw,wheel,*,ocr=False):
    if wheel is None:return result('missing_dependency',issues=['missing_dependency'])
    path=Path(wheel)
    if path.is_symlink() or path.name!=PYPDF_NAME or not path.is_file():return result('missing_dependency',issues=['missing_dependency'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=PYPDF_SHA256:return result('failed',issues=['dependency_integrity_failed'])
    sys.path.insert(0,str(path))
    try:
        logging.getLogger('pypdf').setLevel(logging.CRITICAL)
        pdf=importlib.import_module('pypdf')
        reader=pdf.PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:return result('requires_password',issues=['requires_password'])
        if len(reader.pages)>1000:return result('failed',issues=['page_limit'])
        chunks=[];empty=0;total=0;issues=set();ocr_pages=0
        for index,page in enumerate(reader.pages,1):
            value=page.extract_text() or ''
            if not value.strip() and ocr:
                ocr_pages+=1
                if ocr_pages>20:issues.add('ocr_page_limit')
                else:
                    recognized,issue=ocr_page(raw,index)
                    if issue:issues.add(issue)
                    else:value=recognized
            if value.strip():
                total+=len(value)
                if total>MAX_TEXT:return result('partial',chunks,['output_text_limit'])
                chunks.append({'text':value,'fragment':{'kind':'pdf_page','page':index}})
            else:empty+=1
        if empty:
            issues.add('page_without_text')
            missing=bool(issues & {'missing_ocr_dependency','missing_ocr_languages'})
            return result('partial' if chunks else 'missing_dependency' if missing else 'requires_ocr',chunks,sorted(issues))
        return result('ready' if chunks else 'partial',chunks,[] if chunks else ['no_extractable_text'])
    except Exception:
        return result('failed',issues=['invalid_or_unsupported_pdf'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--wheel');parser.add_argument('--ocr',action='store_true');args=parser.parse_args()
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(1024*1024*1024,1024*1024*1024))
        except (ImportError,ValueError,OSError):pass
        request=json.loads(sys.stdin.buffer.read(MAX_SOURCE*4//3+4096).decode('utf-8'))
        if set(request)!={'format','bytes_base64','source_sha256','limits'}:raise ValueError('invalid_request')
        raw=base64.b64decode(request['bytes_base64'],validate=True)
        if len(raw)>MAX_SOURCE or hashlib.sha256(raw).hexdigest()!=request['source_sha256']:raise ValueError('invalid_source_bytes')
        value=extract_docx(raw) if request['format']=='docx' else extract_pdf(raw,args.wheel,ocr=args.ocr) if request['format']=='pdf' else result('unsupported',issues=['unsupported'])
    except (ValueError,TypeError,KeyError):value=result('failed',issues=['invalid_reader_request'])
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))


if __name__=='__main__':main()
