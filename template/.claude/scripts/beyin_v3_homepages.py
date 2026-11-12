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

from beyin_v3 import BLOCKED_SCOPE, _trusted_record, read_project_scopes, record_scope
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


def _markdown_text(value):
    """Keep generated headings and labels literal inside Markdown formatting."""
    escapes = {ord(character): '&#' + str(ord(character)) + ';' for character in '\\`*_[]#|'}
    return html.escape(value, quote=False).translate(escapes)


def _source_title(source, metadata, body):
    title = metadata.get('title')
    if isinstance(title, str) and title.strip() and not any(c in title for c in '\r\n'):
        return title.strip()
    first = next((line.strip() for line in body.splitlines() if line.strip()), '')
    heading = re.fullmatch(r'# +(.+?)(?: +#+)?', first)
    return heading[1] if heading else source.rsplit('/', 1)[-1][:-3]


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


def _writable(metadata):
    return (_trusted_record(metadata) and not metadata.get('generated') and
            metadata.get('kind') not in ('task', 'receipt', 'receipt-index'))


def _scope(metadata, source, scopes):
    return record_scope(dict(metadata, source=source), scopes or {'folders': []})


def _eligible(home, source, page, path, scopes, *, check_remote=True):
    # Use the same explicit-project precedence and blocked-folder rules as retrieval.
    owner = _scope(home, page, scopes)
    source_owner = _scope(source, path, scopes)
    return (_writable(home) and _writable(source) and not is_projection(source) and owner is not BLOCKED_SCOPE and
            source_owner is not BLOCKED_SCOPE and source_owner == owner and
            home.get('visibility', 'internal') == source.get('visibility', 'internal') and
            (not check_remote or _remote(home) == _remote(source)))


def _remote(record):
    from beyin_v3_jev import remote_allowed
    return remote_allowed(record)


def is_projection(record):
    return (record.get('workspace_homepage') is True or
            '<!-- beyin-field:' in record.get('text', '') or
            CREATION.search(record.get('text', '')) is not None)


def scope_stamp(root):
    path = root / '.beyin-projects.json'
    if path.is_symlink():
        raise ValueError('project scope file must not be redirected')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def project_records(store, records, available=None):
    """Narrow derived copies at index/read time without rewriting human Markdown.

    A stale or missing dependency withholds the whole page until sync verifies it.
    Local-only dependencies still allow local retrieval, but never remote context.
    """
    if not any(is_projection(record) for record in records):
        return records
    sources = {r['source']: r for r in (available if available is not None else records)}
    memo, active = {}, set()

    def protect(record):
        if not is_projection(record):
            return record
        page = record['source']
        if page in memo:
            return memo[page]
        result = dict(record)
        try:
            if page in active or len(active) >= 64:
                raise ValueError('cyclic homepage dependency')
            active.add(page)
            raw = (store.vault_root / store._source(page)).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record['source_sha256']:
                raise ValueError('stale homepage')
            home, body = parse(raw.decode('utf-8'))
            # Discard prior index-only restrictions before deriving current ones.
            for key in ('project', 'trust', 'trusted', 'status', 'remote_allowed', 'sensitivity'):
                result.pop(key, None)
                if key in home:
                    result[key] = home[key]
            result['visibility'] = home.get('visibility', 'internal')
            result['kind'] = home.get('kind', 'note')
            matches = list(CELL.finditer(body))
            if not matches or body.count('<!-- beyin-field:') != len(matches):
                raise ValueError('missing or malformed homepage bindings')
            scopes = read_project_scopes(store.vault_root)
            dependencies = {}
            for match in matches:
                binding = json.loads(match[1])
                source = store._source(binding['source'])
                dependency = sources.get(source)
                if (dependency is None or source == page or
                        dependency['id'] != binding['record_id'] or
                        hashlib.sha256((store.vault_root / source).read_bytes()).hexdigest() != dependency['source_sha256']):
                    raise ValueError('missing, changed or stale homepage dependency')
                dependency = protect(dependency)
                if not _eligible(home, dependency, page, source, scopes, check_remote=False):
                    raise ValueError('homepage dependency policy narrowed')
                if not _remote(dependency):
                    result['remote_allowed'] = False
                dependencies[source] = dict(source=source, sha256=dependency['source_sha256'])
                dependencies.update({d['source']: d for d in dependency.get('homepage_dependencies', [])})
            result['homepage_dependencies'] = [dependencies[key] for key in sorted(dependencies)]
            result['homepage_scope_sha256'] = scope_stamp(store.vault_root)
        except (ValueError, KeyError, TypeError, OSError, UnicodeError):
            # Keep the original file and its audit history; gate its derived copy.
            result.update(trusted=False, remote_allowed=False)
        finally:
            active.discard(page)
        memo[page] = result
        return result

    return [protect(record) for record in records]


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
                not isinstance(request[key], str) or not request[key].strip() or any(c in request[key] for c in '\r\n')):
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
            if not isinstance(label, str) or not label.strip() or any(c in label for c in '\r\n'):
                raise ValueError('field label must be one nonempty line')
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
    scopes = read_project_scopes(engine.root)
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
        if (home.get('workspace_homepage') is not True or not _writable(home) or
                ('project' in request and
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
            metadata, source_body = parse(current['content'])
            metadata = dict(metadata, text=source_body)
            if (_source_id(source, metadata) != indexed[source] or
                    bindings[(source, field, editable)].get('record_id') != indexed[source] or
                    not _eligible(home, metadata, page, source, scopes)):
                raise NoteConflict('selected source identity or scope changed')
            _fact(metadata, field)
        return {'status': 'unchanged', 'source': page, 'sha256': target['sha256']}
    if target['sha256'] != expected:
        raise NoteConflict('homepage changed; note-read again and rebuild the selection')
    if target['exists']:
        home, body = parse(target['content'])
        if home.get('workspace_homepage') is True or '<!-- beyin-field:' in target['content']:
            raise NoteConflict('homepage already has selections')
        if not _writable(home):
            raise ValueError('target must be a writable human note')
        if home.get('project') is None and 'project' not in request:
            raise ValueError('existing homepage without a project requires explicit project (or null)')
        if 'project' in request and home.get('project') not in (None, request['project']):
            raise ValueError('homepage project differs from the selected project')
        if request.get('title') is not None and home.get('title') not in (None, request['title']):
            raise ValueError('homepage title differs from the selected title')
        if home.get('project') is None and request.get('project') is not None:
            home['project'] = request['project']
        if home.get('title') is None and request.get('title') is not None:
            home['title'] = request['title']
    else:
        home, body = {'title': request['title'], 'visibility': 'internal'}, ''
        if request['project'] is not None:
            home['project'] = request['project']
    home['workspace_homepage'] = True
    with engine.store._connect() as db:
        indexed = {row[0]: row[1] for row in db.execute('SELECT source,id FROM markdown_sources')}
    groups = {}
    for selection in selections:
        source = selection['source']
        if source == page or source not in indexed:
            raise ValueError('selected source must be a separate indexed human note')
        snapshot = engine.note_read(source)
        if not snapshot['exists']:
            raise ValueError('selected source is missing')
        metadata, source_body = parse(snapshot['content'])
        metadata = dict(metadata, text=source_body)
        record_id = _source_id(source, metadata)
        if record_id != indexed[source] or not _eligible(home, metadata, page, source, scopes):
            raise ValueError('selected source identity or scope changed')
        if source not in groups:
            relative = posixpath.relpath(source, posixpath.dirname(page) or '.')
            link = quote(relative, safe='/')
            groups[source] = ['### [' + _markdown_text(_source_title(source, metadata, source_body)) + '](' + link + ')', '']
        fields = selection['fields']
        for selected in fields:
            field, label, editable = selected['field'], selected['label'], selected['editable']
            value = _fact(metadata, field)
            groups[source].append('- **' + _markdown_text(label) + ':** ' +
                                  cell(source, record_id, field, value, editable=editable) +
                                  (' _(düzenlenebilir)_' if editable else ' _(salt okunur)_'))
    blocks = ['\n'.join(lines) + '\n\n**Kişisel notum:**\n' for lines in groups.values()]
    section = ('## Seçilen bilgiler\n\n'
               'Düzenlenebilir değerleri bu notta değiştirebilirsin; değişiklikler olağan '
               'eşitlemede esas kayda işlenir. Kişisel notların yalnız bu sayfada kalır.\n\n' +
               CREATION.pattern.replace('([0-9a-f]{64})', fingerprint) + '\n\n' + '\n'.join(blocks))
    if target['exists']:
        newline = '\r\n' if '\r\n' in target['content'] else '\n'
        new_header = render(home, '')
        if target['content'].startswith('\ufeff'):
            new_header = '\ufeff' + new_header
            if body.startswith('\ufeff'):
                body = body[1:]
        # Keep the human body byte for byte, then append the new section. A
        # single SHA-checked anchor also handles an empty or header-only note.
        separator = newline * (0 if body.endswith(newline * 2) else 1 if body.endswith(newline) else 2)
        return engine.note_edit({'source': page, 'expected_sha256': expected,
                                 'edits': [{'old': target['content'], 'new': new_header + body + separator + section.replace('\n', newline)}]})
    return engine.note_edit({'source': page, 'expected_sha256': None,
                             'content': render(home, '# ' + _markdown_text(request['title']) + '\n\n' + section)})


def _edit(engine, snapshot, old, new):
    return engine.note_edit(dict(source=snapshot['source'], expected_sha256=snapshot['sha256'],
                                 edits=[dict(old=old, new=new)]))


def reconcile(engine, *, on_write=None):
    """Re-read live files for every cell; report only divergent fields for choice."""
    scopes = read_project_scopes(engine.root)
    with engine.store._connect() as db:
        indexed = {row[0] for row in db.execute('SELECT source FROM markdown_sources')}
        records = (json.loads(row[0]) for row in db.execute('SELECT payload FROM records'))
        pages = [record['source'] for record in records if record.get('workspace_homepage') is True]
    result = dict(updated=0, conflicts=[], warnings=[])
    for page in pages:
        try:
            snapshot = engine.note_read(page)
            home, _ = parse(snapshot['content'])
            if home.get('workspace_homepage') is not True or not _writable(home):
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
                if (_source_id(source, metadata) != binding['record_id'] or
                        not _eligible(home, dict(metadata, text=body), page, source, scopes)):
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
                    written = _edit(engine, current, header, updated_header)
                    if on_write:
                        on_write(written)
                    value = proposed
                updated = _cell(dict(binding, base=value), value)
                if updated != original[0]:
                    latest = engine.note_read(page)
                    latest_home, _ = parse(latest['content'])
                    if latest_home != home:
                        raise NoteConflict('homepage configuration changed; rerun sync')
                    # Latest personal text is never part of this exact anchor.
                    written = _edit(engine, latest, original[0], updated)
                    if on_write:
                        on_write(written)
                    result['updated'] += 1
        except (ValueError, KeyError, TypeError, OSError, NoteConflict) as exc:
            result['warnings'].append(dict(homepage=page, reason=str(exc)))
    return result
