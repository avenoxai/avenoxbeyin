#!/usr/bin/env python3
"""Bounded stdlib DOCX feasibility study; not a production C2 extractor.

Main-document text and physical XML table cells have locations. Unsupported
stories/revisions/drawings are explicit partial results, never empty success.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

from prototype_workspace_parser_flat import extract_wheel

NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W = '{' + NS + '}'
MAX_ENTRIES = 2048
MAX_TOTAL = 8 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024


def extract_docx(path):
    if Path(path).stat().st_size > MAX_TOTAL:
        return {'state': 'failed', 'chunks': [], 'issues': ['compressed_resource_limit'],
                'source_sha256': None, 'source_unchanged': True}
    original = Path(path).read_bytes()
    before = hashlib.sha256(original).hexdigest()
    result = {'state': 'failed', 'chunks': [], 'issues': []}
    try:
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            names = [item.filename for item in items]
            if len(items) > MAX_ENTRIES or sum(item.file_size for item in items) > MAX_TOTAL:
                raise ValueError('archive_resource_limit')
            if len(set(names)) != len(names):
                raise ValueError('duplicate_archive_member')
            for item in items:
                name = PurePosixPath(item.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename:
                    raise ValueError('unsafe_archive_member')
                if item.flag_bits & 1:
                    raise ValueError('encrypted_zip_unsupported')
                if item.file_size > max(4096, item.compress_size * 200):
                    raise ValueError('archive_compression_limit')
            if 'word/document.xml' not in names:
                raise ValueError('main_document_missing')
            item = archive.getinfo('word/document.xml')
            if item.file_size > MAX_XML:
                raise ValueError('xml_resource_limit')
            raw = archive.read(item)
            # Reject declarations before XML parsing, including UTF-16 variants.
            decoded = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8-sig')
            if '\x00' in decoded:
                raise ValueError('unsupported_xml_encoding')
            if '<!DOCTYPE' in decoded or '<!ENTITY' in decoded:
                raise ValueError('xml_declarations_unsupported')
            root = ET.fromstring(raw)
            if root.tag != W + 'document':
                raise ValueError('unsupported_wordprocessing_namespace')
            body = root.find(W + 'body')
            if body is None:
                raise ValueError('document_body_missing')
            issues = set()
            unsupported = {'drawing', 'pict', 'object', 'ins', 'del', 'fldChar', 'instrText',
                'footnoteReference', 'endnoteReference', 'commentReference', 'altChunk',
                'sdt', 'txbxContent', 'sym', 'numPr'}
            for element in root.iter():
                name = element.tag.rsplit('}', 1)[-1]
                if name in unsupported:
                    issues.add('unsupported_or_review_required:' + name)
            story_names = [name for name in names if name.startswith(('word/header', 'word/footer'))
                           or name in ('word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml')]
            if story_names:
                issues.add('additional_stories_not_locatable_in_contract_0_1')
            def text(element):
                pieces = []
                def walk(node):
                    if node.tag in (W + 'del', W + 'txbxContent', W + 'drawing', W + 'pict'):
                        return  # Not current ordinary text; keep explicit partial finding.
                    if node.tag == W + 't': pieces.append(node.text or '')
                    elif node.tag == W + 'tab': pieces.append('\t')
                    elif node.tag in (W + 'br', W + 'cr'): pieces.append('\n')
                    else:
                        for child in node: walk(child)
                walk(element)
                return ''.join(pieces)
            chunks = []; block_no = 0; table_no = 0
            for block in body:
                if block.tag == W + 'sectPr': continue
                if block.tag not in (W + 'p', W + 'tbl'):
                    issues.add('unsupported_body_block:' + block.tag.rsplit('}', 1)[-1]); continue
                block_no += 1
                if block.tag == W + 'p':
                    value = text(block)
                    if value:
                        chunks.append({'location': {'kind': 'docx_block', 'block': block_no}, 'text': value})
                else:
                    table_no += 1
                    for row_no, row in enumerate(block.findall(W + 'tr'), 1):
                        for cell_no, cell in enumerate(row.findall(W + 'tc'), 1):
                            if cell.find('.//' + W + 'tbl') is not None: issues.add('nested_table')
                            # Paragraph boundaries remain visible; no rendered-page claim.
                            value = '\n'.join(text(p) for p in cell.findall(W + 'p'))
                            if value:
                                chunks.append({'location': {'kind': 'docx_block', 'block': block_no,
                                    'table': table_no, 'row': row_no, 'cell': cell_no}, 'text': value})
            if not chunks and not issues:
                issues.add('no_extractable_text')
            result = {'state': 'partial' if issues else 'ready', 'chunks': chunks, 'issues': sorted(issues)}
    except (ValueError, KeyError, UnicodeError, ET.ParseError, zipfile.BadZipFile, NotImplementedError) as exc:
        result['issues'] = [str(exc) if isinstance(exc, ValueError) else type(exc).__name__]
    result['source_sha256'] = before
    result['source_unchanged'] = hashlib.sha256(Path(path).read_bytes()).hexdigest() == before
    return result


def make_document(path, body, extra=None):
    # Minimal valid OPC main-document package; no third-party fixture generator.
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="' + NS + '"><w:body>' + body + '</w:body></w:document>')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', document)
        for name, raw in (extra or {}).items(): archive.writestr(name, raw)


def study(work, pypdf_wheel, expected_hash, pdf_fixture):
    work = Path(work).resolve()
    if work.exists(): raise ValueError('fresh scratch directory required')
    work.mkdir(parents=True)
    p = lambda value: '<w:p><w:r><w:t>' + value + '</w:t></w:r></w:p>'
    cell = lambda value: '<w:tc>' + p(value) + '</w:tc>'
    table = '<w:tbl><w:tr>' + cell('Ödev') + cell('Sonuç 80') + '</w:tr></w:tbl>'
    cases = {
        'tr_en_table': (p('Türkçe öğrenim ölçüm. English quartz.') + table, None, 'ready'),
        'hyperlink_tab_break': ('<w:p><w:hyperlink><w:r><w:t>Bağlantı</w:t><w:tab/><w:t>etiketi</w:t><w:br/><w:t>devam</w:t></w:r></w:hyperlink></w:p>', None, 'ready'),
        'nested_table': ('<w:tbl><w:tr><w:tc>' + p('Dış') + table + '</w:tc></w:tr></w:tbl>', None, 'partial'),
        'tracked_changes': ('<w:p><w:ins><w:r><w:t>Yeni</w:t></w:r></w:ins><w:del><w:r><w:delText>ESKİ_SİLİNMİŞ</w:delText></w:r></w:del></w:p>', None, 'partial'),
        'textbox': (p('Ana metin') + '<w:p><w:r><w:pict><w:txbxContent>' + p('Kutu') + '</w:txbxContent></w:pict></w:r></w:p>', None, 'partial'),
        'header_footer_notes': (p('Ana metin'), {'word/header1.xml': '<header>Üstbilgi</header>', 'word/footnotes.xml': '<footnotes>Dipnot</footnotes>'}, 'partial'),
        'fields': ('<w:p><w:r><w:fldChar/><w:instrText>PAGE</w:instrText><w:t>1</w:t></w:r></w:p>', None, 'partial'),
        'external_content': (p('Metin') + '<w:altChunk/>', None, 'partial'),
        'empty': ('', None, 'partial'),
        'path_escape': (p('Metin'), {'../escape.xml': 'untrusted'}, 'failed'),
        'high_compression': (p('A' * 200000), None, 'failed')}
    results = {}
    for name, (body, extra, expected) in cases.items():
        path = work / (name + '.docx'); make_document(path, body, extra)
        result = extract_docx(path)
        assert result['state'] == expected, (name, result)
        assert result['source_unchanged']
        results[name] = result
    simple = results['tr_en_table']
    assert any('öğrenim' in chunk['text'] for chunk in simple['chunks'])
    assert simple['chunks'][-1]['location'] == {'kind': 'docx_block', 'block': 2, 'table': 1, 'row': 1, 'cell': 2}
    assert results['hyperlink_tab_break']['chunks'][0]['text'] == 'Bağlantı\t etiketi\ndevam'.replace('\t ', '\t')
    assert 'ESKİ_SİLİNMİŞ' not in str(results['tracked_changes']['chunks'])
    # Real python-docx-generated baseline, not just our own XML generator.
    baseline_docx = Path(pdf_fixture).with_name('source.docx')
    external = extract_docx(baseline_docx)
    assert external['state'] == 'ready' and len(external['chunks']) == 3
    samples = []
    for _ in range(25):
        started = time.perf_counter(); extract_docx(baseline_docx); samples.append((time.perf_counter() - started) * 1000)
    wheel = Path(pypdf_wheel).resolve()
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_hash: raise ValueError('wheel_integrity_failed')
    code = 'import sys,json;sys.path.insert(0,sys.argv[1]);from pypdf import PdfReader;print(json.dumps({"text":PdfReader(sys.argv[2]).pages[0].extract_text()}))'
    started = time.perf_counter()
    extracted = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(wheel), str(Path(pdf_fixture).resolve())],
                               capture_output=True, timeout=30)
    if extracted.returncode: raise RuntimeError(extracted.stderr.decode(errors='replace')[-1500:])
    assert 'quartz' in json.loads(extracted.stdout)['text']
    first_zip_ms = round((time.perf_counter() - started) * 1000, 3)
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == expected_hash
    unpacked = work / 'pypdf-only'; unpacked.mkdir()
    setup_started = time.perf_counter(); extract_wheel(wheel, unpacked, expected_hash)
    unpack_ms = round((time.perf_counter() - setup_started) * 1000, 3)
    comparisons = {}
    for layout, dependency in [('zipimport', wheel), ('unpacked_pypdf_only', unpacked)]:
        samples_pdf = []
        for _ in range(5):
            call_started = time.perf_counter()
            checked = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(dependency), str(Path(pdf_fixture).resolve())],
                                     capture_output=True, timeout=30)
            if checked.returncode: raise RuntimeError(checked.stderr.decode(errors='replace')[-1500:])
            assert 'quartz' in json.loads(checked.stdout)['text']
            samples_pdf.append((time.perf_counter() - call_started) * 1000)
        comparisons[layout] = {'median_subprocess_ms_5_runs': round(statistics.median(samples_pdf), 3),
                               'dependency_bytes': wheel.stat().st_size if layout == 'zipimport' else
                               sum(path.stat().st_size for path in unpacked.rglob('*') if path.is_file())}
    report = {'status': 'prototype_passed', 'platform': sys.platform, 'python': sys.version.split()[0],
        'candidate': 'stdlib_docx_plus_zipimport_pypdf', 'venv': False, 'pip': False,
        'native_dependencies': [], 'pypdf_wheel_bytes': wheel.stat().st_size,
        'pypdf_wheel_sha256': expected_hash, 'prototype_source_bytes': Path(__file__).stat().st_size,
        'pdf_subprocess_ms': first_zip_ms, 'pypdf_unpack_ms': unpack_ms, 'pdf_layout_comparison': comparisons,
        'docx_median_ms_25_runs': round(statistics.median(samples), 3),
        'case_count': len(results), 'cases': results, 'python_docx_generated_document': external,
        'limits': ['prototype only; not C2 production/search/installer acceptance',
                   'main document paragraphs and simple physical XML table cells only',
                   'extra stories cannot get a precise location in G0 0.1; partial until contract decision',
                   'revisions/drawings/fields/nested tables remain explicit partial',
                   'PDF TR/font, Windows/macOS, large files, timeout/crash and full adversarial suite remain open']}
    (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--pypdf-wheel', type=Path, required=True)
    parser.add_argument('--wheel-sha256', required=True)
    parser.add_argument('--pdf-fixture', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(study(args.work, args.pypdf_wheel, args.wheel_sha256, args.pdf_fixture), ensure_ascii=False, indent=2))
