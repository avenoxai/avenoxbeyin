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
            return dict(exists=True, resolved_path=match)
    # Qualified paths must never fall back to an unrelated basename; ambiguity is explicit.
    if '/' not in target:
        matches = index['by_basename'].get(_norm_key(PurePosixPath(target).stem), [])
        if len(matches) == 1:
            return dict(exists=True, resolved_path=matches[0])
        if len(matches) > 1:
            return dict(fail, ambiguous=True)
    return fail


def _heading_exists(body, heading):
    if heading.startswith('^'):
        return bool(re.search(r'(?m)(?:^|\s)\^' + re.escape(heading[1:]) + r'\s*$', body))
    headings = re.findall(r'(?m)^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', body)
    return _norm_key(heading) in {_norm_key(h) for h in headings}


def lint_vault(vault_root, folder=None, *, state=None):
    root = Path(vault_root).resolve()
    if folder:
        _inside(root, folder)
        # Validate scope with the same exclusion policy even when the report is empty.
        build_vault_index(root, folder, state=state)
    index = build_vault_index(root, state=state)
    targets = [f for f in index['texts'] if not folder or f.startswith(folder.rstrip('/') + '/')]
    incoming, outgoing = defaultdict(set), defaultdict(set)
    broken, total = [], 0
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
                    reason = 'embed'
            else:
                resolved = resolve_wikilink(root, target, source, index)
                if not resolved['exists']:
                    reason = 'ambiguous' if resolved.get('ambiguous') else 'wikilink'
                else:
                    destination = resolved['resolved_path']
                    incoming[destination].add(source)
                    outgoing[source].add(destination)
                    if link['heading'] is not None and not _heading_exists(index['texts'][destination][1], link['heading']):
                        reason = 'heading'
            if reason and source in targets:
                broken.append(dict(source=source, line=link['line'], target=target, raw=link['raw'], type=reason))
    orphans = sorted(f for f in targets if not incoming[f] and not outgoing[f])
    unlinked = sorted(f for f in targets if not incoming[f] and f not in orphans)
    return dict(status='issues_found' if broken else 'ok', scanned_files_count=len(targets),
                total_links_count=total, broken_links_count=len(broken), broken_links=broken,
                orphan_count=len(orphans), orphans=orphans, unlinked_count=len(unlinked), unlinked=unlinked)


def _title(metadata, body, rel):
    heading = re.search(r'(?m)^#\s+(.+)$', body)
    title = str(metadata.get('title') or (heading.group(1).strip() if heading else PurePosixPath(rel).stem))
    return re.sub(r'[\r\n|\[\]]', ' ', title)


def plan_moc(vault_root, target_folder='knowledge', moc_filename='MOC.md', *, state=None):
    """Return a deterministic note-edit request; no lock, DB or state writes."""
    root = Path(vault_root).resolve()
    folder = _inside(root, target_folder)
    if not folder.is_dir() or '/' in moc_filename or '\\' in moc_filename or not moc_filename.lower().endswith('.md'):
        raise ValueError('MOC requires an existing folder and a Markdown filename')
    source = target_folder + '/' + moc_filename
    destination = _inside(root, source)
    if sensitive_source(source) or sensitive_excluded(Path(moc_filename).stem):
        raise ValueError('excluded MOC destination')
    index = build_vault_index(root, target_folder, state=state)
    categories = defaultdict(list)
    for rel, (metadata, body) in index['texts'].items():
        if rel == source:
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
    return dict(status='preview', action='moc', file=source, indexed_notes_count=sum(map(len, categories.values())),
                categories_count=len(categories), request=request)


def generate_moc(engine, target_folder='knowledge', moc_filename='MOC.md'):
    """Write using the shared writer's confinement, hash, lock, backup and sync contract."""
    plan = plan_moc(engine.root, target_folder, moc_filename, state=engine.state)
    result = engine.note_edit(plan.pop('request'))
    return dict(plan, status=result['status'], sha256=result['sha256'])
