#!/usr/bin/env python3
"""M03 alternative: existing Python + verified dependency directory, no venv/pip.

Scratch-only experiment; not the C2 extractor, installer, or a dependency choice.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

from prototype_workspace_parsers import WORKER


def extract_wheel(wheel, destination, expected_hash):
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_hash:
        raise ValueError('candidate wheel integrity mismatch')
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (name.is_absolute() or '..' in name.parts or '\\' in item.filename
                    or ':' in item.filename or stat.S_ISLNK(mode)):
                raise ValueError('unsafe wheel member')
            path = destination.joinpath(*name.parts)
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            if path.exists():
                raise ValueError('wheel target collision')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))


def stdlib_docx(file):
    """Bounded main-document paragraph/table PoC. Unsupported content stays visible."""
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    with zipfile.ZipFile(file) as archive:
        item = archive.getinfo('word/document.xml')
        if item.file_size > 2 * 1024 * 1024:
            raise ValueError('document XML exceeds prototype limit')
        raw = archive.read(item)
        if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            raise ValueError('XML declarations are unsupported')
        root = ET.fromstring(raw)
        body = root.find(ns + 'body')
        if body is None:
            raise ValueError('missing DOCX document body')
        unsupported = {'drawing', 'pict', 'object', 'ins', 'del', 'fldChar', 'instrText',
                       'footnoteReference', 'endnoteReference', 'altChunk', 'sdt', 'txbxContent'}
        findings = sorted({element.tag.rsplit('}', 1)[-1] for element in root.iter()
                           if element.tag.rsplit('}', 1)[-1] in unsupported})
        if any(name.startswith(('word/header', 'word/footer')) for name in archive.namelist()):
            findings.append('header_footer_parts_not_read')
        chunks = []
        def text(element):
            return ''.join(node.text or '' for node in element.iter(ns + 't'))
        block_no = 0
        for block in body:
            if block.tag not in (ns + 'p', ns + 'tbl'):
                continue
            block_no += 1
            if block.tag == ns + 'p' and text(block):
                chunks.append({'location': {'block': block_no}, 'text': text(block)})
            elif block.tag == ns + 'tbl':
                for row_no, row in enumerate(block.findall(ns + 'tr'), 1):
                    for cell_no, cell in enumerate(row.findall(ns + 'tc'), 1):
                        if cell.find('.//' + ns + 'tbl') is not None:
                            findings.append('nested_table')
                        if text(cell):
                            chunks.append({'location': {'block': block_no, 'row': row_no, 'cell': cell_no},
                                           'text': text(cell)})
    return {'state': 'partial' if findings else 'ready', 'chunks': chunks, 'issues': sorted(set(findings)),
            'limit': 'main document paragraphs/simple tables only; not production DOCX coverage'}


def prototype(wheelhouse, baseline_report, work):
    wheelhouse, work = Path(wheelhouse).resolve(), Path(work).resolve()
    baseline = json.loads(Path(baseline_report).read_text(encoding='utf-8'))
    if work.exists():
        raise ValueError('fresh scratch directory required')
    work.mkdir(parents=True)
    dependencies = work / 'Doğrulanmış paketler'; dependencies.mkdir()
    started = time.monotonic()
    for item in baseline['packages']:
        extract_wheel(wheelhouse / item['wheel'], dependencies, item['sha256'])
    setup_ms = round((time.monotonic() - started) * 1000, 3)
    worker = work / 'flat-worker.py'
    worker.write_text('import sys\nsys.path.insert(0, sys.argv[3])\n' + WORKER, encoding='utf-8')
    fixtures = work / 'Sentetik belgeler'; fixtures.mkdir()
    def call(mode, deps=dependencies):
        return subprocess.run([sys.executable, '-I', '-S', str(worker), str(fixtures), mode, str(deps)],
                              capture_output=True, timeout=30,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    created = call('fixtures')
    if created.returncode:
        raise RuntimeError(created.stderr.decode(errors='replace')[-1500:])
    extracted = call('extract')
    if extracted.returncode:
        raise RuntimeError(extracted.stderr.decode(errors='replace')[-1500:])
    result = json.loads(extracted.stdout)
    assert result['documents'] == baseline['extraction']['documents'], 'content/location behavior changed'
    docx = stdlib_docx(fixtures / 'source.docx')
    library_docx = next(item for item in result['documents'] if item['name'] == 'source.docx')
    assert docx['chunks'] == library_docx['chunks']
    # Removing native lxml must surface an error, not inherit it from system site-packages.
    incomplete = work / 'missing-lxml'; incomplete.mkdir()
    for item in dependencies.iterdir():
        if not item.name.startswith('lxml'):
            if item.is_dir(): shutil.copytree(item, incomplete / item.name)
            else: shutil.copyfile(item, incomplete / item.name)
    missing = call('extract', incomplete)
    assert missing.returncode != 0 and b'lxml' in missing.stderr
    before = (fixtures / 'source.docx').read_bytes()
    richer = work / 'richer.docx'
    with zipfile.ZipFile(fixtures / 'source.docx') as original, zipfile.ZipFile(richer, 'w') as new:
        for item in original.infolist():
            data = original.read(item)
            if item.filename == 'word/document.xml':
                data = data.replace(b'</w:body>', b'<w:p><w:r><w:drawing/></w:r></w:p></w:body>')
            new.writestr(item, data)
    assert stdlib_docx(richer)['state'] == 'partial'
    assert (fixtures / 'source.docx').read_bytes() == before
    installed_bytes = sum(path.stat().st_size for path in dependencies.rglob('*') if path.is_file())
    report = {'status': 'prototype_passed', 'layout': 'existing_python_verified_dependency_directory',
        'platform': sys.platform, 'python': sys.version.split()[0],
        'venv_created': False, 'pip_invoked': False, 'global_packages_modified': False,
        'wheel_bytes': baseline['wheel_bytes'], 'dependency_bytes': installed_bytes,
        'dependency_setup_ms': setup_ms, 'subprocess_flags': ['-I', '-S'],
        'venv_baseline_bytes': baseline['helper_installed_bytes'],
        'disk_reduction_percent': round((1 - installed_bytes / baseline['helper_installed_bytes']) * 100, 2),
        'same_fixture_results_as_venv': True, 'extraction': result,
        'missing_dependency': {'exit_code': missing.returncode, 'state': 'missing_dependency'},
        'stdlib_docx': docx,
        'limits': ['Linux CPython 3.13 only; Windows/macOS and ABI matrix remain open',
                   'same parser dependencies; no OCR or AES crypto extras',
                   'isolated process is not an OS sandbox',
                   'stdlib DOCX experiment is narrower and not selected for production',
                   'C2 policy/cache/freshness and installer atomic publication/recovery remain open']}
    (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prototype(args.wheelhouse, args.baseline_report, args.work), ensure_ascii=False, indent=2))
