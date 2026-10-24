"""Installed parser capability probes using C2's actual isolated reader service.

Only synthetic in-memory documents are read. No source resolver/permission/cache
is duplicated and no user source, registry, index or global config is written.
A format probe is not verification of every document or of the whole workspace.
"""
import base64
import hashlib
import io
from pathlib import Path
import zipfile

from beyin_v3_sources import default_readers, run_reader
from beyin_v3_workspaces import WorkspaceError


def _pdf():
    text = b'BT /F1 12 Tf 72 720 Td (Beyin parser probe quartz) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream']
    raw, offsets = b'%PDF-1.4\n', []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw)); raw += str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(raw); raw += b'xref\n0 6\n0000000000 65535 f \n'
    raw += b''.join(('%010d 00000 n \n' % offset).encode() for offset in offsets)
    return raw + b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n'


def _docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Öğrenim quartz</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Sonuç 82</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    return buffer.getvalue()


OCR_PROBE_SHA256 = 'b3eb25619abfd7404835bf172104fae9e711f48088bd56d745d4e84d8a6a3f2a'


def document_capabilities(vault, *, timeout=20, include_ocr=True):
    """Return G0 Capability objects for installed PDF/DOCX readers.

    The C/runtime adapter may map name -> state into D1 required capabilities.
    Format ready means these exact extraction/location probes passed, not
    every Word story, workflow readiness or completed daily verification.
    """
    cases = [('pdf_text', '.pdf', _pdf(), [('Beyin parser probe quartz', {'kind': 'pdf_page', 'page': 1})]),
             ('docx_text', '.docx', _docx(), [('Öğrenim quartz', {'kind': 'docx_block', 'block': 1}),
                ('Sonuç 82', {'kind': 'docx_block', 'block': 2, 'table': 1, 'row': 1, 'cell': 1})])]
    result = []
    if include_ocr:
        path = Path(vault) / '.claude/parser-assets/ocr-probe-tr-en.pdf'
        try:
            if path.is_symlink() or path.stat().st_size > 128 * 1024:
                raise ValueError('probe_integrity_failed')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != OCR_PROBE_SHA256:
                raise ValueError('probe_integrity_failed')
            cases.append(('ocr_tr_en', '.pdf', raw, [
                (text, {'kind': 'pdf_page', 'page': 1})
                for text in ('Türkçe öğrenim', 'yüzey çatladı', 'English ceramic firing')]))
        except (OSError, ValueError) as exc:
            result.append({'name': 'ocr_tr_en', 'state': 'failed' if isinstance(exc, ValueError) else 'missing_dependency',
                           'detail': 'ocr_probe_asset_unavailable_or_corrupt; repair_managed_parser_assets'})
    try:
        readers = default_readers(vault)
    except OSError:
        return result + [{'name': name, 'state': 'missing_dependency', 'detail': 'installed_reader_missing; repair_managed_reader'} for name, *_ in cases]
    for name, suffix, raw, expected in cases:
        state, detail = 'failed', 'probe_output_mismatch'
        try:
            response = run_reader(readers[suffix]['command'], {'format': suffix[1:],
                'bytes_base64': base64.b64encode(raw).decode(), 'source_sha256': hashlib.sha256(raw).hexdigest(),
                'limits': {'max_output_bytes': 16384}}, timeout=timeout)
            if isinstance(response, dict) and response.get('state') != 'ready':
                value = response.get('state')
                state = value if value in ('partial', 'requires_ocr', 'requires_password', 'unsupported', 'missing_dependency', 'failed') else 'failed'
                detail = 'installed_reader_probe_' + state
                codes = {issue.get('code') for issue in response.get('issues', []) if isinstance(issue, dict)}
                if 'missing_ocr_dependency' in codes:
                    detail = 'missing_ocr_dependency; install_poppler_pdftoppm_and_tesseract_then_retry'
                elif 'missing_ocr_languages' in codes:
                    detail = 'missing_ocr_languages; install_tesseract_tur_and_eng_data_then_retry'
            elif isinstance(response, dict):
                chunks = response.get('chunks', [])
                if isinstance(chunks, list) and all(any(isinstance(chunk, dict) and
                        text in chunk.get('text', '') and chunk.get('fragment') == fragment
                        for chunk in chunks) for text, fragment in expected):
                    state, detail = 'ready', 'synthetic_content_and_locations_verified; each_user_document_requires_C2_verification'
        except (OSError, WorkspaceError, KeyError, TypeError):
            state, detail = 'failed', 'installed_reader_probe_failed'
        result.append({'name': name, 'state': state, 'detail': detail})
    return result
