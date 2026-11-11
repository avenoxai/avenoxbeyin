"""Opt-in, field-sized Workspace projections using the ordinary human-note writer.

Only facts explicitly selected on a trusted homepage are writable. Baselines live
beside each cell, so a crash after a source write converges without repeating it.
"""
import html
import hashlib
import json
import posixpath
import re
from urllib.parse import quote

from beyin_v3_notes import NoteConflict, locked, source_path, require_utf8_values
from beyin_v3_sync import parse, render

CELL = re.compile(r'<!-- beyin-field:(\{[^\r\n]*?\}) -->([^\r\n]*?)<!-- /beyin-field -->')
CREATION = re.compile(r'<!-- beyin-homepage-create:([0-9a-f]{64}) -->')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _display(value):
    if isinstance(value, (dict, list)) or '\n' in str(value) or '\r' in str(value):
        raise ValueError('homepage fields require single-line scalar facts')
    displayed = html.escape(value if isinstance(value, str) else _json(value), quote=False)
    for character in '|`[]*':
        displayed = displayed.replace(character, '&#' + str(ord(character)) + ';')
    return displayed


def cell(source, record_id, field, value, *, editable=False):
    """Render one selected fact; place its source link and personal note outside it."""
    binding = dict(source=source, record_id=record_id, field=field, base=value, editable=editable)
    return _cell(binding, value)


def _cell(binding, value):
    # Escaped hyphens prevent a fact value from terminating its HTML comment.
    return '<!-- beyin-field:' + _json(binding).replace('-', '\\u002d').replace('<', '\\u003c') + ' -->' + _display(value) + '<!-- /beyin-field -->'


def _fact(metadata, field):
    if not isinstance(field, str) or not field.startswith('facts.') or not field[6:]:
        raise ValueError('select an existing facts.<name> field')
    value = metadata.get('facts', {})[field[6:]]
    _display(value)
    return value


def _same(left, right):
    return _json(left) == _json(right)


def _eligible(home, source):
    # No cross-project/private/untrusted promotion through a homepage.
    return (home.get('project') == source.get('project') and
            home.get('visibility', 'internal') == source.get('visibility', 'internal') and
            source.get('trust') != 'untrusted' and not source.get('generated') and
            source.get('kind') not in ('task', 'receipt', 'receipt-index'))


def _source_id(source, metadata):
    return metadata.get('id', 'md-' + hashlib.sha256(source.encode('utf-8')).hexdigest()[:24])


def _validate_request(engine, request):
    """Reject malformed and unsafe requests before sync can recover prior work."""
    if not isinstance(request, dict) or set(request) - {'source', 'expected_sha256', 'title', 'project', 'selections'}:
        raise ValueError('homepage-create requires source, expected_sha256, title/project, and selections')
    require_utf8_values(request, 'homepage-create')
    page = request.get('source')
    source_path(engine, page)
    if 'expected_sha256' not in request:
        raise ValueError('expected_sha256 is required; use note-read first')
    expected = request['expected_sha256']
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
        raise ValueError('expected_sha256 must be a SHA-256 hex digest or null')
    selections = request.get('selections')
    if not isinstance(selections, list) or not selections:
        raise ValueError('select at least one existing fact')
    if expected is None and (not isinstance(request.get('title'), str) or not request['title'].strip() or
                             'project' not in request):
        raise ValueError('new homepage requires a title and explicit project (or null)')
    for key in ('title', 'project'):
        if key in request and not (key == 'project' and request[key] is None) and (
                not isinstance(request[key], str) or not request[key].strip() or '\n' in request[key]):
            raise ValueError(key + ' must be one nonempty line')
    keys = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {'source', 'fields'}:
            raise ValueError('each selection needs source and fields')
        source = selection['source']
        source_path(engine, source)
        if source == page:
            raise ValueError('selected source must differ from homepage')
        fields = selection['fields']
        if not isinstance(fields, list) or not fields:
            raise ValueError('select at least one fact from each source')
        for selected in fields:
            if not isinstance(selected, dict) or set(selected) != {'field', 'label', 'editable'}:
                raise ValueError('each field needs field, label, and editable')
            field, label, editable = selected['field'], selected['label'], selected['editable']
            if not isinstance(field, str) or not field.startswith('facts.') or not field[6:] or any(c in field for c in '\r\n'):
                raise ValueError('select an existing facts.<name> field')
            if not isinstance(label, str) or not label.strip() or any(c in label for c in '\r\n|'):
                raise ValueError('field label must be one nonempty table-safe line')
            if type(editable) is not bool:
                raise ValueError('editable must be true or false')
            key = (source, field)
            if key in keys:
                raise ValueError('duplicate selected fact')
            keys.add(key)


def create(engine, request):
    """Create or attach one explicit selection using the ordinary CAS note writer."""
    _validate_request(engine, request)
    # Index newly added ordinary notes before checking their effective identities.
    # This scan does not reconcile or edit any existing homepage.
    previous = getattr(engine, '_homepage_busy', False)
    engine._homepage_busy = True
    try:
        engine.sync()
    finally:
        engine._homepage_busy = previous
    with locked(engine):
        return _create_locked(engine, request)


def _create_locked(engine, request):
    page = request['source']
    expected = request['expected_sha256']
    selections = request['selections']
    canonical = {key: request.get(key) for key in ('source', 'title', 'project', 'selections')}
    fingerprint = hashlib.sha256(_json(canonical).encode('utf-8')).hexdigest()
    target = engine.note_read(page)
    if target['exists'] and CREATION.search(target['content']):
        matches = CREATION.findall(target['content'])
        if len(matches) != 1 or matches[0] != fingerprint:
            raise NoteConflict('homepage already has a different creation selection')
        home, _ = parse(target['content'])
        if (home.get('workspace_homepage') is not True or home.get('trust') == 'untrusted' or
                home.get('generated') or ('project' in request and
                home.get('project') != request['project'])):
            raise NoteConflict('homepage configuration changed')
        expected_cells = {(part['source'], field['field'], field['editable'])
                          for part in selections for field in part['fields']}
        actual_cells = []
        bindings = {}
        for match in CELL.finditer(target['content']):
            binding = json.loads(match[1])
            key = (binding.get('source'), binding.get('field'), binding.get('editable'))
            actual_cells.append(key)
            bindings[key] = binding
        if (target['content'].count('<!-- beyin-field:') != len(actual_cells) or
                len(actual_cells) != len(expected_cells) or set(actual_cells) != expected_cells):
            raise NoteConflict('homepage selections changed')
        with engine.store._connect() as db:
            indexed = {row[0]: row[1] for row in db.execute('SELECT source,id FROM markdown_sources')}
        for source, field, editable in expected_cells:
            source_path(engine, source)
            if source == page or source not in indexed:
                raise NoteConflict('selected source is no longer indexed')
            current = engine.note_read(source)
            if not current['exists']:
                raise NoteConflict('selected source is missing')
            metadata, _ = parse(current['content'])
            if (_source_id(source, metadata) != indexed[source] or
                    bindings[(source, field, editable)].get('record_id') != indexed[source] or
                    not _eligible(home, metadata)):
                raise NoteConflict('selected source identity or scope changed')
            _fact(metadata, field)
        return {'status': 'unchanged', 'source': page, 'sha256': target['sha256']}
    if target['sha256'] != expected:
        raise NoteConflict('homepage changed; note-read again and rebuild the selection')
    if target['exists']:
        home, body = parse(target['content'])
        if home.get('workspace_homepage') is True or '<!-- beyin-field:' in target['content']:
            raise NoteConflict('homepage already has selections')
        if home.get('trust') == 'untrusted' or home.get('generated') or home.get('kind') in ('task', 'receipt', 'receipt-index'):
            raise ValueError('target must be a writable human note')
        if home.get('project') is None and 'project' not in request:
            raise ValueError('existing homepage without a project requires explicit project (or null)')
        if 'project' in request and home.get('project') not in (None, request['project']):
            raise ValueError('homepage project differs from the selected project')
        if request.get('title') is not None and home.get('title') not in (None, request['title']):
            raise ValueError('homepage title differs from the selected title')
        if home.get('project') is None and 'project' in request:
            home['project'] = request['project']
        if home.get('title') is None and request.get('title') is not None:
            home['title'] = request['title']
    else:
        home, body = {'title': request['title'], 'project': request['project'], 'visibility': 'internal'}, ''
    home['workspace_homepage'] = True
    with engine.store._connect() as db:
        indexed = {row[0]: row[1] for row in db.execute('SELECT source,id FROM markdown_sources')}
    rows = []
    for selection in selections:
        source = selection['source']
        if source == page or source not in indexed:
            raise ValueError('selected source must be a separate indexed human note')
        snapshot = engine.note_read(source)
        if not snapshot['exists']:
            raise ValueError('selected source is missing')
        metadata, _ = parse(snapshot['content'])
        record_id = _source_id(source, metadata)
        if record_id != indexed[source] or not _eligible(home, metadata):
            raise ValueError('selected source identity or scope changed')
        fields = selection['fields']
        for selected in fields:
            field, label, editable = selected['field'], selected['label'], selected['editable']
            value = _fact(metadata, field)
            relative = posixpath.relpath(source, posixpath.dirname(page) or '.')
            link = quote(relative, safe='/')
            link_label = source.rsplit('/', 1)[-1][:-3].replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
            rows.append('- [' + link_label + '](' + link + ') · ' +
                        html.escape(label, quote=False) +
                        (' (düzenlenebilir)' if editable else ' (salt okunur)') + ': ' +
                        cell(source, record_id, field, value, editable=editable))
    section = '\n## Seçilen bilgiler\n\n' + CREATION.pattern.replace('([0-9a-f]{64})', fingerprint) + '\n' + '\n'.join(rows) + '\n'
    if target['exists']:
        old_header = target['content'][:-len(body)] if body else target['content']
        if old_header:
            new_header = render(home, '')
            if old_header.startswith('\ufeff'):
                new_header = '\ufeff' + new_header
            replacement = new_header + section
            return engine.note_edit({'source': page, 'expected_sha256': expected,
                                     'edits': [{'old': old_header, 'new': replacement}]})
        return engine.note_edit({'source': page, 'expected_sha256': expected,
                                 'edits': [{'old': target['content'], 'new': render(home, section + target['content'])}]})
    return engine.note_edit({'source': page, 'expected_sha256': None,
                             'content': render(home, section)})


def _edit(engine, snapshot, old, new):
    return engine.note_edit(dict(source=snapshot['source'], expected_sha256=snapshot['sha256'],
                                 edits=[dict(old=old, new=new)]))


def reconcile(engine):
    """Re-read live files for every cell; report only divergent fields for choice."""
    with engine.store._connect() as db:
        indexed = {row[0] for row in db.execute('SELECT source FROM markdown_sources')}
        records = (json.loads(row[0]) for row in db.execute('SELECT payload FROM records'))
        pages = [record['source'] for record in records if record.get('workspace_homepage') is True]
    result = dict(updated=0, conflicts=[], warnings=[])
    for page in pages:
        try:
            snapshot = engine.note_read(page)
            home, _ = parse(snapshot['content'])
            if home.get('workspace_homepage') is not True or home.get('trust') == 'untrusted':
                continue
            text = snapshot['content']
            if text.count('<!-- beyin-field:') != len(list(CELL.finditer(text))):
                raise ValueError('ambiguous or malformed homepage field markers')
            # Ambiguous duplicate bindings fail before any write on this page.
            matches = list(CELL.finditer(text))
            keys = [_json({k: json.loads(m[1]).get(k) for k in ('source', 'record_id', 'field')}) for m in matches]
            if len(keys) != len(set(keys)):
                raise ValueError('duplicate homepage field binding')
            for original in matches:
                binding = json.loads(original[1])
                field = binding['field']
                source = binding['source']
                if source not in indexed or source == page:
                    raise ValueError('selected source must be a separate indexed human note')
                current = engine.note_read(source)
                metadata, body = parse(current['content'])
                if _source_id(source, metadata) != binding['record_id'] or not _eligible(home, metadata):
                    raise ValueError('selected source identity or scope changed')
                value = _fact(metadata, field)
                base = binding['base']
                _display(base)
                displayed = html.unescape(original[2])
                proposed = displayed if isinstance(base, str) else json.loads(displayed)
                _display(proposed)
                if type(proposed) is not type(base):
                    raise ValueError('keep the selected fact scalar type')
                local_changed = not _same(proposed, base)
                remote_changed = not _same(value, base)
                if local_changed and binding.get('editable') is not True:
                    result['conflicts'].append(dict(homepage=page, source=source, field=field,
                        reason='read_only_field', source_value=value, homepage_value=proposed))
                    continue
                if local_changed and remote_changed and not _same(proposed, value):
                    result['conflicts'].append(dict(homepage=page, source=source, field=field,
                        reason='choose_value', base=base, source_value=value, homepage_value=proposed))
                    continue
                if local_changed and not _same(proposed, value):
                    metadata['facts'][field[6:]] = proposed
                    # Keep body and unrelated facts; use the existing CAS writer and backup.
                    header = current['content'][:-len(body)] if body else current['content']
                    updated_header = render(metadata, '')
                    if current['content'].startswith('\ufeff'):
                        updated_header = '\ufeff' + updated_header
                    _edit(engine, current, header, updated_header)
                    value = proposed
                updated = _cell(dict(binding, base=value), value)
                if updated != original[0]:
                    latest = engine.note_read(page)
                    latest_home, _ = parse(latest['content'])
                    if latest_home != home:
                        raise NoteConflict('homepage configuration changed; rerun sync')
                    # Latest personal text is never part of this exact anchor.
                    _edit(engine, latest, original[0], updated)
                    result['updated'] += 1
        except (ValueError, KeyError, TypeError, OSError, NoteConflict) as exc:
            result['warnings'].append(dict(homepage=page, reason=str(exc)))
    return result
