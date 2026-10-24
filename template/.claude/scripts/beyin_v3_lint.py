"""Local, read-only wikilink checks and cooperative MOC writes (adapted from #448).

No registry, builder or learning synthesis is implied by this navigation tool.
MOC writes use the existing hash-checked note writer; previews never open a store.
"""
from collections import defaultdict
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import unicodedata

from beyin_v3 import sensitive_excluded, sensitive_source, _trusted_record
from beyin_v3_index import ARCHIVE_HINT_DIRS, dir_key, exclusions, applied_keys
from beyin_v3_sync import EXCLUDED_DIRS, EXCLUDED_FILES, parse

WIKILINK_PATTERN = re.compile(r"!?\[\[([^\[\]\r\n]{1,500})\]\]")
MOC_START_MARKER = '<!-- beyin-moc:start -->'
MOC_END_MARKER = '<!-- beyin-moc:end -->'


def _norm_key(text):
    return unicodedata.normalize('NFC', text.replace('\\', '/')).strip().casefold()


def extract_wikilinks(text):
    """Return bounded wikilinks, aliases, headings and embeds with line numbers."""
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in WIKILINK_PATTERN.finditer(line):
            target, sep, display = match.group(1).partition('|')
            target, heading_sep, heading = target.partition('#')
            result.append(dict(raw=match.group(0), file_target=target.strip(),
                               heading=heading.strip() if heading_sep else None,
                               display=display.strip() if sep else None,
                               is_embed=match.group(0).startswith('!'), line=number))
    return result


def _inside(root, relative):
    """Reject traversal, device/absolute paths and every link/reparse component."""
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('use a visible vault-relative path')
    parts = relative.split('/')
    if any(not p or p in ('.', '..') or p.startswith('.') or p.endswith((' ', '.')) or
           re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p) for p in parts):
        raise ValueError('use a visible vault-relative path')
    cursor = root
    for part in parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
            raise ValueError('symlink or junction path rejected')
    if not cursor.resolve().is_relative_to(root):
        raise ValueError('path outside vault')
    return cursor


def _excluded(parts, extra):
    return any(p.startswith('.') or dir_key(p) in extra or sensitive_excluded(p)
               or dir_key(p) in ARCHIVE_HINT_DIRS for p in parts)


def build_vault_index(vault_root, target_folder=None, *, state=None):
    """Index eligible human Markdown and media; never follow symlinks.

    Markdown is read locally to inspect metadata; excluded metadata/bodies are
    never returned. Callers must provide state to enforce workspace activation.

    An optional state reads the existing index exclusion policy without creating it.
    Invalid policy fails closed rather than silently scanning excluded content.
    """
    root = Path(vault_root).resolve()
    if state is not None and (Path(state) / 'workspace-runtime.json').exists():
        from beyin_v3_workspace_context import enabled
        if enabled(root, state):
            raise ValueError('active_workspace_requires_scoped_context')
    extra = {dir_key(name) for name in EXCLUDED_DIRS}
    if state is not None:
        names, report = exclusions(Path(state), root)
        if report and report.get('error'):
            raise ValueError('invalid index exclusions')
        extra.update(applied_keys(names, EXCLUDED_DIRS))
    start = _inside(root, target_folder) if target_folder else root
    if target_folder and _excluded(PurePosixPath(target_folder).parts, extra):
        raise ValueError('excluded target folder')
    by_rel, basenames, texts, media = {}, defaultdict(list), {}, {}
    ambiguous_rel = set()
    for directory, dirs, files in os.walk(start, followlinks=False):
        kept = []
        for name in dirs:
            if _excluded([name], extra):
                continue
            try:
                _inside(root, (Path(directory) / name).relative_to(root).as_posix())
            except ValueError:
                continue
            kept.append(name)
        dirs[:] = sorted(kept)
        for name in sorted(files):
            if (name.startswith('.') or name.casefold() in EXCLUDED_FILES or name.casefold().startswith('setup-')
                    or sensitive_excluded(Path(name).stem)):
                continue
            rel = (Path(directory) / name).relative_to(root).as_posix()
            if sensitive_source(rel):
                continue
            try:
                path = _inside(root, rel)
                if path.suffix.lower() != '.md':
                    media[_norm_key(rel)] = rel
                    continue
                text = path.read_text(encoding='utf-8-sig')
                metadata, body = parse(text)
                if (metadata.get('visibility', 'internal') not in ('internal', 'public')
                        or not _trusted_record(metadata) or metadata.get('generated_by')):
                    continue
            except (OSError, ValueError, UnicodeError):
                continue
            key = _norm_key(rel)
            if key in by_rel or key in ambiguous_rel:
                ambiguous_rel.add(key)
                by_rel.pop(key, None)
            else:
                by_rel[key] = rel
            basenames[_norm_key(path.stem)].append(rel)
            texts[rel] = (metadata, body)
    return dict(by_rel=by_rel, by_basename=dict(basenames), texts=texts, media=media, ambiguous_rel=ambiguous_rel)


def scoped_vault_index(vault_root, state, workspace_id, grant_ref, *, audience='internal'):
    """Build navigation data from fresh, authorized canonical records only.

    This never walks the vault. Files absent from the indexed workspace inventory
    remain unknown, including external sources and unindexed Markdown.
    """
    from beyin_v3 import MemoryStore
    from beyin_v3_workspace_context import authorized_record_scope, enabled
    from beyin_v3_workspaces import Registry, WorkspaceError, local_root_matches, content_hash
    from beyin_v3_sources import SourceResolver
    root, state = Path(vault_root).resolve(), Path(state).resolve()
    if not enabled(root, state):
        raise WorkspaceError('workspace_context_required')
    registry = Registry(root, state)
    snapshot = registry.snapshot()
    if snapshot is None:
        raise WorkspaceError('workspace_unavailable')
    bindings, _ = registry.bindings(snapshot)
    profile = next((p for p in snapshot['profiles'] if p['id'] == workspace_id and p['lifecycle'] == 'active'), None)
    if profile is None:
        raise WorkspaceError('scope_denied')
    configured_ids = [s['id'] for s in snapshot['sources'] if s['kind'] == 'vault' and s['id'] in profile['source_ids']
                      and s['id'] in bindings and local_root_matches(bindings[s['id']]['locator'], root)]
    store = MemoryStore(state, root, read_only=True)
    with authorized_record_scope(registry, snapshot, bindings, workspace_id, grant_ref, audience=audience):
        records, stale_count = store._eligible(audience)
    resolver = SourceResolver(registry, readers={}, cache_write=False)
    vault_ids = [sid for sid in configured_ids if resolver.authority.allows(
        grant_ref, bindings[sid], workspace_id, audience=audience)]
    if not vault_ids:
        raise WorkspaceError('scope_denied')
    by_rel, basenames, aliases, texts, metadata_by_rel = {}, defaultdict(list), defaultdict(list), {}, {}
    read_sources = {}
    ambiguous_rel = set()
    for record in records:
        rel = record.get('source')
        if not isinstance(rel, str) or not rel.lower().endswith('.md'):
            continue
        for sid in vault_ids:
            try:
                path, _, _ = resolver.resolve(workspace_id, sid, rel, grant_ref, audience=audience)
                raw = path.read_bytes()
                if content_hash(raw) != record['source_sha256']:
                    break
                # A grant may be revoked while bytes are read.
                resolver.resolve(workspace_id, sid, rel, grant_ref, audience=audience)
                metadata, body = parse(raw.decode('utf-8-sig'))
                key = _norm_key(rel)
                if key in by_rel or key in ambiguous_rel:
                    ambiguous_rel.add(key); by_rel.pop(key, None)
                else:
                    by_rel[key] = rel
                basenames[_norm_key(PurePosixPath(rel).stem)].append(rel)
                for alias in metadata.get('aliases', [])[:32] if isinstance(metadata.get('aliases'), list) else []:
                    if isinstance(alias, str) and 0 < len(alias) <= 160 and '/' not in alias and '\\' not in alias:
                        aliases[_norm_key(alias[:-3] if alias.lower().endswith('.md') else alias)].append(rel)
                texts[rel], metadata_by_rel[rel] = (metadata, body), record
                read_sources[rel] = (sid, record['source_sha256'])
                break
            except (WorkspaceError, OSError, UnicodeError, ValueError):
                continue
    # Recheck each exact binding and source after collecting bytes. A different
    # still-authorized binding cannot keep a revoked source's content visible.
    for rel, (sid, digest) in read_sources.items():
        try:
            path, _, _ = resolver.resolve(workspace_id, sid, rel, grant_ref, audience=audience)
            if content_hash(path.read_bytes()) != digest:
                raise WorkspaceError('stale_source')
            resolver.resolve(workspace_id, sid, rel, grant_ref, audience=audience)
        except WorkspaceError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkspaceError('source_unavailable') from exc
    if not read_sources and not any(resolver.authority.allows(
            grant_ref, bindings[sid], workspace_id, audience=audience) for sid in vault_ids):
        raise WorkspaceError('scope_denied')
    return dict(by_rel=by_rel, by_basename=dict(basenames), by_alias=dict(aliases), texts=texts, media={},
                ambiguous_rel=ambiguous_rel, records=metadata_by_rel,
                scope=dict(kind='authorized_canonical_records', workspace_id=workspace_id,
                           source_ids=sorted(vault_ids), indexed_count=len(texts), stale_count=stale_count,
                           outside_inventory='unknown', media_inventory='not_indexed'))


def plan_link_impact(vault_index, source, destination=None):
    """Preview known references before a path change; no source or index mutation.

    Wikilink display aliases and canonical metadata aliases are distinct. A
    resolved link outside this approved inventory cannot be asserted safe.
    """
    if 'scope' not in vault_index:
        raise ValueError('authorized inventory required')
    if source not in vault_index['texts']:
        raise ValueError('source outside authorized inventory')
    if destination is not None and (not isinstance(destination, str) or destination == source):
        raise ValueError('invalid destination')
    if destination is not None and (not destination.endswith('.md') or destination.startswith('/') or
                                    '\\' in destination or ':' in destination or
                                    any(p in ('', '.', '..') or p.startswith('.') for p in destination.split('/'))):
        raise ValueError('invalid destination')
    record = vault_index['records'][source]
    incoming, unresolved, relations, source_refs = [], [], [], []
    for rel, (metadata, body) in sorted(vault_index['texts'].items()):
        for link in extract_wikilinks(body):
            target = link['file_target']
            resolved = resolve_wikilink(None, target, rel, vault_index)
            item = dict(source=rel, line=link['line'], target=target,
                        display_alias=link['display'], is_embed=link['is_embed'])
            if resolved.get('resolved_path') == source:
                item['match_kind'] = resolved.get('match_kind', 'self')
                incoming.append(item)
            elif not resolved['exists']:
                item['reason'] = 'ambiguous' if resolved.get('ambiguous') else 'unresolved'
                unresolved.append(item)
        for field in ('source_refs', 'evidence_refs'):
            refs = metadata.get(field, [])
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, str) and ref == source:
                    source_refs.append(dict(source=rel, field=field, reference_kind='path'))
                elif isinstance(ref, dict) and ref.get('relative_path') == source:
                    source_refs.append(dict(source=rel, field=field, reference_kind='source_ref',
                                            source_id=ref.get('source_id'),
                                            content_sha256=ref.get('content_sha256')))
        for relation in metadata.get('workspace_relations', []) if isinstance(metadata.get('workspace_relations'), list) else []:
            if not isinstance(relation, dict):
                continue
            if any(isinstance(relation.get(end), dict) and relation[end].get('record_id') == record['id']
                   for end in ('from', 'to')):
                relations.append(dict(source=rel, relation_id=relation.get('id'), type=relation.get('type')))
            for ref in relation.get('evidence_refs', []) if isinstance(relation.get('evidence_refs'), list) else []:
                if isinstance(ref, dict) and ref.get('relative_path') == source:
                    source_refs.append(dict(source=rel, relation_id=relation.get('id'),
                                            source_id=ref.get('source_id'), content_sha256=ref.get('content_sha256')))
    aliases = record.get('aliases', [])
    if destination is None:
        destination_status = 'not_requested'
    elif _norm_key(destination) in (set(vault_index['by_rel']) | vault_index['ambiguous_rel']):
        destination_status = 'known_conflict'
    else:
        destination_status = 'unknown'
    return dict(status='preview', writes_performed=False, source=source, destination=destination,
                record_id=record['id'], source_sha256=record['source_sha256'],
                destination_status=destination_status,
                destination_conflict=True if destination_status == 'known_conflict' else None,
                canonical_aliases=aliases if isinstance(aliases, list) else [],
                incoming_wikilinks=incoming, known_unresolved=unresolved,
                record_relations=relations, historical_source_refs=source_refs,
                scope=vault_index['scope'], outside_inventory='unknown',
                review_required=bool(unresolved or destination is not None))


def plan_attached_source_impact(vault_root, state, vault_index, workspace_id, grant_ref,
                                source_id, source, destination=None):
    """Preview one approved non-Markdown source without a media/vault walk.

    An unqualified basename is only a possible link: uniqueness cannot be
    established from this single approved attachment.
    """
    from beyin_v3_workspaces import Registry, WorkspaceError, content_hash, relative_name, local_root_matches
    from beyin_v3_sources import SourceResolver
    if 'scope' not in vault_index or vault_index['scope']['workspace_id'] != workspace_id:
        raise WorkspaceError('scope_denied')
    if not isinstance(source_id, str) or not source_id or not isinstance(source, str):
        raise WorkspaceError('source_unavailable')
    relative_name(source)
    excluded_dirs = {dir_key(name) for name in EXCLUDED_DIRS}
    if (Path(source).suffix.lower() in ('', '.md') or sensitive_source(source) or
            sensitive_excluded(PurePosixPath(source).stem) or
            _excluded(PurePosixPath(source).parts[:-1], excluded_dirs)):
        raise WorkspaceError('scope_denied')
    if destination is not None:
        relative_name(destination)
        if destination == source or Path(destination).suffix.lower() == '.md':
            raise ValueError('invalid destination')
    registry = Registry(vault_root, state)
    snapshot = registry.snapshot()
    descriptor = next((item for item in (snapshot or {}).get('sources', []) if item['id'] == source_id), None)
    if descriptor is None or descriptor['kind'] == 'vault':
        raise WorkspaceError('scope_denied')
    resolver = SourceResolver(registry, readers={}, cache_write=False)
    path, _, binding = resolver.resolve(workspace_id, source_id, source, grant_ref)
    raw = resolver._read_bytes(path)
    digest = content_hash(raw)
    same_vault_root = local_root_matches(binding['locator'], registry.vault)
    incoming, possible_basename, possible_path_links, source_refs, possible_path_refs = [], [], [], [], []
    for rel, (metadata, body) in sorted(vault_index['texts'].items()):
        for link in extract_wikilinks(body):
            target = link['file_target'].replace('\\', '/')
            if not target or target.startswith('/') or ':' in target or PurePosixPath(target).suffix.lower() != Path(source).suffix.lower():
                continue
            item = dict(source=rel, line=link['line'], target=link['file_target'],
                        display_alias=link['display'], is_embed=link['is_embed'])
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if same_vault_root and not relative.startswith('../') and _norm_key(relative) == _norm_key(source):
                item['match_kind'] = 'relative_path'
                incoming.append(item)
            elif same_vault_root and not target.startswith(('./', '../')) and _norm_key(target) == _norm_key(source):
                item['match_kind'] = 'qualified_path'
                incoming.append(item)
            elif not same_vault_root and _norm_key(target) == _norm_key(source):
                item['reason'] = 'binding_root_not_vault'
                possible_path_links.append(item)
            elif '/' not in target and _norm_key(target) == _norm_key(PurePosixPath(source).name):
                item['reason'] = 'basename_uniqueness_unknown'
                possible_basename.append(item)
        for field in ('source_refs', 'evidence_refs'):
            refs = metadata.get(field, [])
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, dict) and ref.get('source_id') == source_id and ref.get('relative_path') == source:
                    source_refs.append(dict(source=rel, field=field, reference_kind='source_ref',
                                            content_sha256=ref.get('content_sha256')))
                elif isinstance(ref, str) and ref == source:
                    item = dict(source=rel, field=field, reference_kind='path_only')
                    (source_refs if same_vault_root else possible_path_refs).append(item)
        relations = metadata.get('workspace_relations', [])
        for relation in relations if isinstance(relations, list) else []:
            if not isinstance(relation, dict):
                continue
            refs = relation.get('evidence_refs', [])
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, dict) and ref.get('source_id') == source_id and ref.get('relative_path') == source:
                    source_refs.append(dict(source=rel, relation_id=relation.get('id'),
                                            reference_kind='source_ref', content_sha256=ref.get('content_sha256')))
    # The approved Markdown inventory and this exact attachment must still be
    # readable after the scan; a revoked binding cannot return collected refs.
    fresh_index = scoped_vault_index(vault_root, state, workspace_id, grant_ref)
    if ({rel: record['source_sha256'] for rel, record in fresh_index['records'].items()} !=
            {rel: record['source_sha256'] for rel, record in vault_index['records'].items()}):
        raise WorkspaceError('stale_source')
    fresh_path, _, _ = resolver.resolve(workspace_id, source_id, source, grant_ref)
    if content_hash(resolver._read_bytes(fresh_path)) != digest:
        raise WorkspaceError('stale_source')
    resolver.resolve(workspace_id, source_id, source, grant_ref)
    return dict(status='preview', writes_performed=False, source_id=source_id, source=source,
                source_sha256=digest, destination=destination,
                destination_status='unknown' if destination is not None else 'not_requested',
                incoming_links=incoming, possible_basename_references=possible_basename,
                possible_path_links=possible_path_links,
                source_refs=source_refs, possible_path_refs=possible_path_refs,
                scope=vault_index['scope'], outside_inventory='unknown',
                review_required=bool(destination is not None or possible_basename or possible_path_links or possible_path_refs))


def resolve_wikilink(vault_root, link_target, source_file=None, vault_index=None):
    index = vault_index if vault_index is not None else build_vault_index(vault_root)
    fail = dict(exists=False, resolved_path=None)
    if not link_target:
        return dict(exists=source_file in index.get('texts', index['by_rel'].values()), resolved_path=source_file, is_internal=True)
    target = link_target.replace('\\', '/').strip()
    if target.startswith('/') or ':' in target:
        return fail
    target = target if target.lower().endswith('.md') else target + '.md'
    explicit_relative = target.startswith(('./', '../'))
    candidates = []
    if source_file:
        candidates.append(posixpath.normpath(posixpath.join(posixpath.dirname(source_file), target)))
    if not explicit_relative:
        candidates.append(posixpath.normpath(target))
    for candidate in candidates:
        if candidate.startswith('../'):
            continue
        if _norm_key(candidate) in index.get('ambiguous_rel', set()):
            return dict(fail, ambiguous=True)
        match = index['by_rel'].get(_norm_key(candidate))
        if match:
            return dict(exists=True, resolved_path=match, match_kind='path')
    # Qualified paths must never fall back to an unrelated basename; ambiguity is explicit.
    if '/' not in target:
        key = _norm_key(PurePosixPath(target).stem)
        basename_matches = set(index['by_basename'].get(key, []))
        alias_matches = set(index.get('by_alias', {}).get(key, []))
        matches = basename_matches | alias_matches
        if len(matches) == 1:
            match = next(iter(matches))
            return dict(exists=True, resolved_path=match,
                        match_kind='basename' if match in basename_matches else 'canonical_alias')
        if len(matches) > 1:
            return dict(fail, ambiguous=True)
    return fail


def _heading_exists(body, heading):
    if heading.startswith('^'):
        return bool(re.search(r'(?m)(?:^|\s)\^' + re.escape(heading[1:]) + r'\s*$', body))
    headings = re.findall(r'(?m)^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', body)
    return _norm_key(heading) in {_norm_key(h) for h in headings}


def lint_vault(vault_root, folder=None, *, state=None, vault_index=None):
    root = Path(vault_root).resolve()
    if folder:
        _inside(root, folder)
        # Validate scope with the same exclusion policy even when the report is empty.
        if vault_index is None:
            build_vault_index(root, folder, state=state)
    index = vault_index if vault_index is not None else build_vault_index(root, state=state)
    targets = [f for f in index['texts'] if not folder or f.startswith(folder.rstrip('/') + '/')]
    incoming, outgoing = defaultdict(set), defaultdict(set)
    broken, unknown, total = [], [], 0
    scoped = index.get('scope') is not None
    # Graph sees the whole eligible vault even when the report is folder-limited.
    for source, (_, body) in index['texts'].items():
        for link in extract_wikilinks(body):
            if source in targets:
                total += 1
            target = link['file_target']
            reason = None
            if link['is_embed'] and PurePosixPath(target).suffix.lower() not in ('', '.md'):
                names = [posixpath.normpath(posixpath.join(posixpath.dirname(source), target)), posixpath.normpath(target)]
                exists = any(not n.startswith('../') and _norm_key(n) in index['media'] for n in names)
                if not exists:
                    reason = 'unknown_media' if scoped else 'embed'
            else:
                resolved = resolve_wikilink(root, target, source, index)
                if not resolved['exists']:
                    reason = 'ambiguous' if resolved.get('ambiguous') else 'unknown_wikilink' if scoped else 'wikilink'
                else:
                    destination = resolved['resolved_path']
                    incoming[destination].add(source)
                    outgoing[source].add(destination)
                    if link['heading'] is not None and not _heading_exists(index['texts'][destination][1], link['heading']):
                        reason = 'heading'
            if reason and source in targets:
                issue = dict(source=source, line=link['line'], target=target, raw=link['raw'], type=reason)
                (unknown if reason.startswith('unknown_') else broken).append(issue)
    orphans = sorted(f for f in targets if not incoming[f] and not outgoing[f])
    unlinked = sorted(f for f in targets if not incoming[f] and f not in orphans)
    return dict(status='issues_found' if broken else 'indeterminate' if unknown else 'ok',
                scope=index.get('scope'), scanned_files_count=len(targets),
                total_links_count=total, broken_links_count=len(broken), broken_links=broken,
                unknown_links_count=len(unknown), unknown_links=unknown,
                orphan_count=len(orphans), orphans=orphans, unlinked_count=len(unlinked), unlinked=unlinked)


def _title(metadata, body, rel):
    heading = re.search(r'(?m)^#\s+(.+)$', body)
    title = str(metadata.get('title') or (heading.group(1).strip() if heading else PurePosixPath(rel).stem))
    return re.sub(r'[\r\n|\[\]]', ' ', title)


def plan_moc(vault_root, target_folder='knowledge', moc_filename='MOC.md', *, state=None, vault_index=None):
    """Return a deterministic note-edit request; no lock, DB or state writes."""
    root = Path(vault_root).resolve()
    folder = _inside(root, target_folder)
    if not folder.is_dir() or '/' in moc_filename or '\\' in moc_filename or not moc_filename.lower().endswith('.md'):
        raise ValueError('MOC requires an existing folder and a Markdown filename')
    source = target_folder + '/' + moc_filename
    destination = _inside(root, source)
    if sensitive_source(source) or sensitive_excluded(Path(moc_filename).stem):
        raise ValueError('excluded MOC destination')
    index = vault_index if vault_index is not None else build_vault_index(root, target_folder, state=state)
    categories = defaultdict(list)
    for rel, (metadata, body) in index['texts'].items():
        if rel == source or not rel.startswith(target_folder.rstrip('/') + '/'):
            continue
        category = PurePosixPath(rel).relative_to(target_folder).parent.as_posix()
        categories['Genel' if category == '.' else category].append((rel, _title(metadata, body, rel)))
    lines = [MOC_START_MARKER]
    for category, notes in sorted(categories.items()):
        lines.append('### ' + category)
        lines.extend('- [[' + rel + '|' + title + ']]' for rel, title in sorted(notes, key=lambda n: (n[1].casefold(), n[0])))
        lines.append('')
    lines.append(MOC_END_MARKER)
    block = '\n'.join(lines)
    if destination.exists() and source not in index['texts']:
        raise ValueError('excluded MOC destination')
    raw = destination.read_bytes() if destination.exists() else None
    existing = raw.decode('utf-8') if raw is not None else None
    request = dict(source=source, expected_sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None)
    if existing is None:
        request['content'] = '# ' + target_folder + ' — Map of Content (MOC)\n\n' + block + '\n'
    else:
        if existing.count(MOC_START_MARKER) != existing.count(MOC_END_MARKER) or existing.count(MOC_START_MARKER) > 1:
            raise ValueError('ambiguous MOC markers; preserve and repair manually')
        if MOC_START_MARKER in existing:
            start, end = existing.index(MOC_START_MARKER), existing.index(MOC_END_MARKER)
            if end < start:
                raise ValueError('reversed MOC markers')
            request['edits'] = [dict(old=existing[start:end + len(MOC_END_MARKER)], new=block.replace('\n', '\r\n') if '\r\n' in existing else block)]
        elif existing:
            request['edits'] = [dict(old=existing, new=existing + '\n\n' + block + '\n')]
        else:
            raise ValueError('empty existing MOC; add a heading before updating')
    return dict(status='preview', action='moc', file=source, scope=index.get('scope'), indexed_notes_count=sum(map(len, categories.values())),
                categories_count=len(categories), request=request)


def generate_moc(engine, target_folder='knowledge', moc_filename='MOC.md', *, vault_index=None,
                 workspace_id=None, grant_ref=None, source_id=None):
    """Write using the shared writer's confinement, hash, lock, backup and sync contract."""
    plan = plan_moc(engine.root, target_folder, moc_filename, state=engine.state, vault_index=vault_index)
    if vault_index is not None:
        from beyin_v3_workspace_context import request_scope
        from beyin_v3_workspaces import Registry, WorkspaceError, content_hash
        from beyin_v3_sources import SourceResolver
        if not all((workspace_id, grant_ref, source_id)):
            raise WorkspaceError('scope_denied')
        resolver = SourceResolver(Registry(engine.root, engine.state), readers={}, cache_write=False)
        resolver.resolve(workspace_id, source_id, plan['file'], grant_ref, access='write_memory')
        with request_scope(workspace_id, grant_ref):
            request = plan.pop('request')
            if request['expected_sha256'] is None:
                created = engine.note_create(plan['file'], request['content'],
                                             {'workspace_id': workspace_id, 'kind': 'note'})
                result = {'status': 'written' if created['status'] == 'succeeded' else created['status'],
                          'sha256': content_hash((Path(engine.root) / plan['file']).read_bytes())}
            else:
                if plan['file'] not in vault_index['texts']:
                    raise WorkspaceError('scope_denied')
                result = engine.note_edit(request)
    else:
        result = engine.note_edit(plan.pop('request'))
    return dict(plan, status=result['status'], sha256=result['sha256'])
