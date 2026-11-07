"""Bounded, standard-library text extraction for captured DOCX files."""

import io
from pathlib import PurePosixPath
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MAX_SOURCE = 32 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024
MAX_TEXT = 1024 * 1024
MAX_CENTRAL_DIRECTORY = 256 * 1024
MAX_ENTRIES = 2048


def _preflight_archive(raw):
    """Bound and validate ZIP metadata before ZipFile materializes its directory."""
    start = max(0, len(raw) - (22 + 0xFFFF))
    end = raw.rfind(b'PK\x05\x06', start)
    if end < 0 or len(raw) - end < 22:
        raise ValueError('invalid_docx')
    _, disk, directory_disk, disk_entries, entries, directory_size, directory_offset, comment_size = \
        struct.unpack_from('<4s4H2LH', raw, end)
    if end + 22 + comment_size != len(raw):
        raise ValueError('invalid_docx')
    # ZIP64 permits unbounded entry counts and 64-bit metadata. DOCX input is
    # already capped at 32 MiB, so reject ZIP64 before zipfile parses its extras.
    if (disk or directory_disk or disk_entries != entries or entries == 0xFFFF or
            directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF or
            (end >= 20 and raw[end - 20:end - 16] == b'PK\x06\x07')):
        raise ValueError('archive_metadata_limit')
    if entries > MAX_ENTRIES or directory_size > MAX_CENTRAL_DIRECTORY:
        raise ValueError('archive_metadata_limit')
    if directory_offset + directory_size != end:
        raise ValueError('invalid_docx')



def extract_docx(raw):
    """Return text and a state; never treat images or omitted stories as complete."""
    if len(raw) > MAX_SOURCE:
        return {'state': 'failed', 'text': '', 'issues': ['source_too_large']}
    issues = set()
    try:
        _preflight_archive(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > MAX_ENTRIES:
                raise ValueError('archive_metadata_limit')
            if any(entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                   for entry in entries):
                raise ValueError('unsupported_compression')
            if sum(entry.file_size for entry in entries) > MAX_ARCHIVE:
                raise ValueError('archive_resource_limit')
            if len(set(names)) != len(names):
                raise ValueError('duplicate_archive_member')
            for entry in entries:
                name = PurePosixPath(entry.filename)
                if (name.is_absolute() or '..' in name.parts or '\\' in entry.filename
                        or ':' in entry.filename):
                    raise ValueError('unsafe_archive_member')
                if entry.flag_bits & 1:
                    raise ValueError('encrypted_archive')
                if entry.file_size > max(4096, entry.compress_size * 200):
                    raise ValueError('archive_compression_limit')
            document = archive.getinfo('word/document.xml')
            if document.file_size > MAX_XML:
                raise ValueError('xml_resource_limit')
            with archive.open(document) as stream:
                xml = stream.read(MAX_XML + 1)
            if len(xml) > MAX_XML:
                raise ValueError('xml_resource_limit')
            decoded = xml.decode('utf-16') if xml[:2] in (b'\xff\xfe', b'\xfe\xff') else xml.decode('utf-8-sig')
            # Parse the checked Unicode string, never the original undecoded bytes.
            # XML declarations (including DTD and entity variants) are not needed
            # for Word body text and can amplify or alter parser behavior.
            if '\0' in decoded or '<!' in decoded:
                raise ValueError('xml_declarations_unsupported')
            root = ET.fromstring(decoded)
            if root.tag != W + 'document':
                raise ValueError('unsupported_wordprocessing_namespace')
            body = root.find(W + 'body')
            if body is None:
                raise ValueError('document_body_missing')
            unsupported = {'drawing', 'pict', 'object', 'altChunk', 'txbxContent',
                           'footnoteReference', 'endnoteReference', 'commentReference',
                           'fldChar', 'instrText', 'ins', 'del', 'sdt', 'sym'}
            for node in root.iter():
                if node.tag.rsplit('}', 1)[-1] in unsupported:
                    issues.add('unsupported_content')
            if any(name.startswith(('word/media/', 'word/header', 'word/footer')) or
                   name in ('word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml')
                   for name in names):
                issues.add('additional_content')

            def paragraph(node):
                pieces = []
                def walk(item, depth=0):
                    if depth > 64:
                        raise ValueError('xml_depth_limit')
                    if item.tag in (W + 'del', W + 'txbxContent', W + 'drawing', W + 'pict'):
                        return
                    if item.tag == W + 't':
                        pieces.append(item.text or '')
                    elif item.tag == W + 'tab':
                        pieces.append('\t')
                    elif item.tag in (W + 'br', W + 'cr'):
                        pieces.append('\n')
                    else:
                        for child in item:
                            walk(child, depth + 1)
                walk(node)
                return ''.join(pieces)

            blocks = []
            size = 0
            for block in body:
                if block.tag == W + 'sectPr':
                    continue
                if block.tag == W + 'p':
                    lines = [paragraph(block)]
                elif block.tag == W + 'tbl':
                    lines = []
                    for row in block.findall(W + 'tr'):
                        for cell in row.findall(W + 'tc'):
                            if cell.find('.//' + W + 'tbl') is not None:
                                issues.add('nested_table')
                            lines.append('\n'.join(paragraph(p) for p in cell.findall(W + 'p')))
                else:
                    issues.add('unsupported_body_block')
                    continue
                for line in lines:
                    if line:
                        size += len(line) + (2 if blocks else 0)
                        if size > MAX_TEXT:
                            raise ValueError('output_text_limit')
                        blocks.append(line)
            value = '\n\n'.join(blocks)
            if not value:
                issues.add('no_extractable_text')
            return {'state': 'partial' if issues else 'ready', 'text': value,
                    'issues': sorted(issues)}
    except ValueError as exc:
        return {'state': 'failed', 'text': '', 'issues': [str(exc)]}
    except (KeyError, UnicodeError, ET.ParseError, zipfile.BadZipFile,
            NotImplementedError, RecursionError, RuntimeError, zlib.error,
            EOFError, struct.error, OverflowError):
        return {'state': 'failed', 'text': '', 'issues': ['invalid_docx']}
