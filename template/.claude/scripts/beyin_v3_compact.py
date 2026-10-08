"""Lossless size control for the two companion handoff files (#96): rotate, never delete.

Runs only as the explicit `beyin.py companion-compact` command: no timer, no model call,
no interpretation of the text. When Last-Session.md or Threads.md is over its limit, the
Previous/Closed history section and then the oldest dated entries move verbatim into a
private monthly archive beside the companion files, until the file fits. The newest entry
of every container (the handoff, each thread) always stays. Every line either stays or
moves; nothing is rewritten, summarized or dropped.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time

from beyin_v3_companion import LIMITS, directory, read_limits

LINE = re.compile(r'[^\n]*\n|[^\n]+$')
HEADING = re.compile(r'(#{1,6})[ \t]')
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')
DATE = re.compile(r'(?<!\d)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?!\d)')
CLOCK = re.compile(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)')
ITEM = re.compile(r'(?:[-*+]|\d{1,3}[.)])[ \t]')
LEADING = re.compile(r'(?:(?:[-*+>]|\d{1,3}[.)])[ \t]+)?[*_`\[(]*\d{4}-\d{2}-\d{2}(?!\d)')
# The same section names the context extractor already treats as history.
HISTORY = {'Last-Session.md': re.compile(r'(?i)## (?:Previous|Önceki)'),
           'Threads.md': re.compile(r'(?i)## (?:Closed|Kapan|Kapalı)')}
# Headings up to this level are containers, never entries: Last-Session cards sit under
# its title; in Threads every ### heading is a thread whose newest update stays.
CONTAINER = {'Last-Session.md': 1, 'Threads.md': 3}
# A dated line directly under the Active section (or the title) may be a whole thread,
# not an update of one, so in Threads only updates inside a named thread heading move.
ACTIVE = re.compile(r'(?i)#{1,2}[ \t]+(?:Active|Aktif|Açık|Acik|Open)|#[ \t]')
ADDED_HISTORY = {'Last-Session.md': '## Önceki oturumlar', 'Threads.md': '## Kapanan konular'}
ARCHIVE_DIRECTORY = 'Arşiv'
POINTER = 'Arşivlenen metin: `{}`'
BACKUP = '{} karakter tam metin yedeği (needs_rewrite)'
POINTER_LINE = re.compile(r'Arşivlenen metin: `[^`\n]+`[ \t]*\r?\n?$')


def stamp(line, heading=False):
    """(date, time) of an entry-opening line, or None.

    The ISO date must be among the first 48 characters and, outside headings, before any
    colon: `**Status:** active, 2026-10-01` is a field, `2026-09-24 14:05: ...` an entry.
    """
    text = line.strip()
    date = DATE.search(text, 0, 48)
    if not date:
        return None
    colon = text.find(':')
    if not heading and 0 <= colon < date.start():
        return None
    clock = CLOCK.search(text, date.end(), date.end() + 12)
    return date[0], (clock[1].zfill(2) + ':' + clock[2]) if clock else ''


def _frontmatter_end(lines):
    if not lines or lines[0].lstrip('\ufeff').rstrip('\r\n') != '---':
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip('\r\n') in ('---', '...'):
            return index + 1
    return 0


def blocks(lines, name):
    """Split lines into contiguous static, entry and history blocks; together they cover every line."""
    top, history = CONTAINER[name], HISTORY[name]
    found = []
    container = None
    fence = None
    in_history = False
    boundary = True

    def begin(kind, index, key=None, level=None):
        if found:
            found[-1]['end'] = index
        found.append({'kind': kind, 'start': index, 'end': None, 'container': container, 'key': key, 'level': level})

    start = _frontmatter_end(lines)
    if start:
        begin('static', 0)
    begin('static', start)
    for index in range(start, len(lines)):
        bare = lines[index].rstrip('\r\n')
        if fence:
            if re.match(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*$', bare):
                fence = None
            continue
        opened = FENCE.match(bare)
        # CommonMark: a backtick fence's info string cannot contain a backtick, so a line such as
        # ```code``` is inline code, not an opening fence that would swallow the cards below (#228).
        if opened and opened[1][0] == '`' and '`' in bare[opened.end():]:
            opened = None
        if opened:
            fence = opened[1]
            boundary = False
            continue
        # A dated heading opens a card (#118): up to the next heading of its own or a higher
        # level, every line is that one entry, dated lines and sub-headings included, so a
        # card moves or stays whole and an older date quoted inside it never splits it.
        card = found[-1]['level'] if found[-1]['kind'] == 'entry' else None
        heading = HEADING.match(bare)
        if heading:
            level = len(heading[1])
            if in_history and level > 2:
                continue
            in_history = False
            if level == 2 and history.match(bare):
                in_history = True
                begin('history', index)
            elif level <= top:
                container = index
                begin('static', index)
            elif card and level > card:
                pass  # A deeper heading, dated or not, stays inside the card.
            elif stamp(bare, heading=True):
                begin('entry', index, stamp(bare, heading=True), level)
            elif found[-1]['kind'] in ('history', 'entry'):
                # An undated heading closes a dated paragraph and a dated heading of the same
                # or a higher level: it is the file's own structure, not part of the old entry.
                begin('static', index)
            boundary = True
            continue
        if in_history:
            continue
        key = stamp(bare) if bare.strip() and not card else None
        if key and (boundary or ITEM.match(bare) or LEADING.match(bare)):
            begin('entry', index, key)
        boundary = not bare.strip()
    found[-1]['end'] = len(lines)
    return [block for block in found if block['end'] > block['start']]


def plan(text, name, limit, pointer):
    """Decide what moves. Returns None when nothing can move (the file needs a rewrite)."""
    lines = LINE.findall(text)
    parsed = blocks(lines, name)
    newline = '\r\n' if '\r\n' in text else '\n'
    pointer_line = pointer + newline
    # Newest entry per container stays. Equal timestamps follow the container's own
    # direction: newest-first files keep the top one, append-ordered files the bottom one.
    groups = {}
    for position, block in enumerate(parsed):
        owner = block['container']
        if block['kind'] == 'entry' and (name != 'Threads.md' or (owner is not None and not ACTIVE.match(lines[owner]))):
            groups.setdefault(owner, []).append(position)
    order, protected = {}, set()
    for positions in groups.values():
        keys = [parsed[p]['key'] for p in positions]
        rising = any(a < b for a, b in zip(keys, keys[1:]))
        falling = any(a > b for a, b in zip(keys, keys[1:]))
        # Same-day entries carry no order of their own, so the file's direction decides:
        # Last-Session cards are newest-first, thread updates are appended at the bottom (#163).
        newest_first = falling and not rising if rising or falling else name == 'Last-Session.md'
        for rank, p in enumerate(positions):
            order[p] = (parsed[p]['key'], -rank if newest_first else rank, p)
        protected.add(max(positions, key=order.get))
    histories = [p for p, block in enumerate(parsed) if block['kind'] == 'history']
    kept_pointers = {i for p in histories for i in range(parsed[p]['start'] + 1, parsed[p]['end'])
                     if POINTER_LINE.match(lines[i])}
    has_pointer = any(lines[i].rstrip('\r\n').rstrip() == pointer for i in kept_pointers)
    # History sections are history by definition; their body moves first, blank lines
    # included. Earlier pointer lines and one closing blank line stay, so the section
    # keeps its heading and its separation from whatever follows it.
    history_moved = set()
    for p in histories:
        body = [i for i in range(parsed[p]['start'] + 1, parsed[p]['end']) if i not in kept_pointers]
        if any(lines[i].strip() for i in body):
            if not lines[body[-1]].strip():
                body.pop()
            history_moved.update(body)
    moved_lines, moved_blocks = set(history_moved), set()

    def projected():
        moved = sum(len(lines[i]) for i in moved_lines)
        if histories:
            extra = 0 if has_pointer else len(pointer_line) + (0 if lines[parsed[histories[0]]['start']].endswith('\n') else len(newline))
        else:
            extra = (0 if not text or text.endswith('\n') else len(newline)) + len(newline + ADDED_HISTORY[name] + newline + newline + pointer_line)
        return len(text) - moved + extra

    for p in sorted((p for p in order if p not in protected), key=order.get):
        if projected() <= limit:
            break
        moved_blocks.add(p)
        moved_lines.update(range(parsed[p]['start'], parsed[p]['end']))
    if not moved_lines:
        return None
    live, archive, label = [], [], None
    for p, block in enumerate(parsed):
        span = range(block['start'], block['end'])
        if block['kind'] == 'history':
            heading = lines[block['start']] if lines[block['start']].endswith('\n') else lines[block['start']] + newline
            live.append(heading)
            if p == histories[0] and not has_pointer:
                live.append(pointer_line)
            live.extend(lines[i] for i in span if i != block['start'] and i not in moved_lines)
            body = [lines[i] for i in span if i in moved_lines]
            if body:
                archive.append(heading)
                archive.extend(body)
                label = None
        elif p in moved_blocks:
            owner = block['container']
            if owner is not None and owner != label and len(HEADING.match(lines[owner])[1]) >= 2:
                archive.append(lines[owner] if lines[owner].endswith('\n') else lines[owner] + newline)
                label = owner
            archive.extend(lines[i] for i in span)
        else:
            live.extend(lines[i] for i in span)
    live = ''.join(live)
    if not histories:
        live = (live if not live or live.endswith('\n') else live + newline) + newline + ADDED_HISTORY[name] + newline + newline + pointer_line
    if archive and not archive[-1].endswith('\n'):
        archive[-1] += newline
    return {'live': live, 'archive': ''.join(archive), 'newline': newline,
            'moved_lines': sorted(moved_lines), 'moved_chars': sum(len(lines[i]) for i in moved_lines),
            'moved_entries': len(moved_blocks), 'moved_history': bool(history_moved)}


def _write(path, text):
    from beyin_v3_sync import atomic
    atomic(path, text)


def _inside(path, vault):
    cursor = path
    while cursor != vault and cursor != cursor.parent:
        if cursor.is_symlink():
            raise ValueError('symlink in companion archive path')
        cursor = cursor.parent
    if not path.resolve().is_relative_to(vault):
        raise ValueError('companion archive escapes vault')


@contextmanager
def _compact_lock(vault, state, timeout=10.0, step=0.1):
    vault_path = Path(vault).resolve()
    key = hashlib.sha256(str(vault_path).encode('utf-8')).hexdigest()[:16]
    handle = None
    candidates = [
        Path(tempfile.gettempdir()) / f'beyin-compact-{key}.lock',
        Path(state) / 'compact.lock',
    ]
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.touch(exist_ok=True)
            handle = candidate.open('a+b')
            break
        except OSError:
            continue
    if handle is None:
        yield
        return

    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(step)
        if not acquired:
            raise TimeoutError('vault compaction lock busy; wait for completion')
        yield
    finally:
        if acquired:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


def compact(vault, state, dry_run=False, now=None):
    vault = Path(vault).resolve()
    target = directory(vault)
    if target is None:
        return {'status': 'needs_attention', 'reason': 'Multiple companion directories; nothing moved.', 'files': {}}
    configured, valid = read_limits(state)
    if not valid:
        # The user chose limits that can no longer be read; moving text by the defaults
        # could archive far more than they wanted.
        return {'status': 'needs_attention', 'files': {}, 'limits_file': 'invalid',
                'reason': 'companion-limits.json in the runtime state is invalid; nothing moved. Fix or remove it first.'}
    if dry_run:
        return _compact_files(vault, target, configured, dry_run=True, now=now)
    try:
        with _compact_lock(vault, state):
            return _compact_files(vault, target, configured, dry_run=False, now=now)
    except TimeoutError as exc:
        return {'status': 'conflict', 'reason': str(exc), 'files': {}}


def _compact_files(vault, target, configured, dry_run=False, now=None):
    now = now or datetime.now(timezone.utc)
    month = now.strftime('%Y-%m')
    relative = target.relative_to(vault).as_posix()
    files = {}
    for name in LIMITS:
        path = target / name
        limit = configured[name]
        if path.is_symlink() or not path.is_file():
            continue
        _inside(path, vault)
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            files[name] = {'status': 'needs_attention', 'reason': 'not UTF-8; nothing moved', 'limit': limit}
            continue
        report = {'chars': len(text), 'limit': limit}
        if not limit or len(text) <= limit:
            files[name] = dict(report, status='within_limit' if limit else 'limit_off')
            continue
        archive_name = f'{Path(name).stem}-{month}.md'
        archive_relative = f'{relative}/{ARCHIVE_DIRECTORY}/{archive_name}'
        result = plan(text, name, limit, POINTER.format(archive_relative))
        report.update(archive=archive_relative)
        if result is None:
            backup = text
        else:
            report.update(chars_after=len(result['live']), moved_chars=result['moved_chars'],
                          moved_entries=result['moved_entries'], moved_history=result['moved_history'],
                          within_limit_after=len(result['live']) <= limit)
            backup = None if report['within_limit_after'] else result['live']
        # #168: a file that stays over the limit is rewritten by the agent next, which may
        # summarize. Its full remaining text goes to the archive first, verbatim, so the
        # rewrite can never be the only copy of anything.
        if dry_run:
            if backup is not None:
                report.update(backup='planned')
            if result is None:
                files[name] = dict(report, status='needs_rewrite',
                                   reason='No Previous/Closed section and no older dated entry to move; a real run backs '
                                          'up the full file to the archive first. Then rewrite the file within the limit.')
            else:
                files[name] = dict(report, status='planned')
            continue
        archive = target / ARCHIVE_DIRECTORY / archive_name
        _inside(archive, vault)
        previous = archive.read_bytes() if archive.exists() else None
        try:
            existing = previous.decode('utf-8') if previous is not None else None
        except UnicodeDecodeError:
            files[name] = dict(report, status='needs_attention', reason='existing archive is not UTF-8; nothing moved')
            continue
        newline = result['newline'] if result else '\r\n' if '\r\n' in text else '\n'
        heading = '## ' + now.strftime('%Y-%m-%d %H:%M UTC') + ', ' + name + ', '
        added = []
        if result is not None:
            added.append(newline + heading + str(result['moved_chars']) + ' karakter taşındı' + newline + newline + result['archive'])
        if backup is not None:
            marker = BACKUP.format(len(backup))
            if existing is not None and marker in existing and backup in existing:
                report.update(backup='exists')  # an unchanged file is not copied again
            else:
                added.append(newline + heading + marker + newline + newline + backup +
                             ('' if backup.endswith('\n') else newline))
                report.update(backup='written')
        if added:
            if existing is None:
                existing = newline.join([
                    '---', '{"kind": "note", "visibility": "private", "title": "' + name + ' arşivi ' + month + '"}', '---',
                    '# ' + name + ' arşivi: ' + month, '',
                    '`beyin.py companion-compact` bu dosyaya yalnız ekleme yapar. Taşınan metin kelimesi kelimesine '
                    'korunur, hiçbir şey silinmez. `visibility: private` olduğu için otomatik bağlama girmez; '
                    'gerektiğinde bu dosyayı doğrudan aç.', ''])
            elif not existing.endswith('\n'):
                existing += newline
            archive_written = existing + ''.join(added)
            _write(archive, archive_written)
            # Compare-and-swap: a concurrent edit of the live file wins and the archive returns
            # to its previous bytes, so no text is ever held only by the archive or lost.
            if path.read_bytes() != raw:
                # #193: Roll back only if the archive still holds this invocation's exact write.
                # A concurrent caller that already committed its own archive is never reverted.
                if archive.exists() and archive.read_bytes() == archive_written.encode('utf-8'):
                    if previous is None:
                        archive.unlink()
                    else:
                        _write(archive, previous.decode('utf-8'))
                report.pop('backup', None)
                files[name] = dict(report, status='conflict', reason='file changed during compaction; nothing moved, retry')
                continue
        if result is None:
            files[name] = dict(report, status='needs_rewrite',
                               reason='No Previous/Closed section and no older dated entry to move; full file backed up to '
                                      'the archive. Rewrite the file within the limit.')
            continue
        _write(path, result['live'])
        files[name] = dict(report, status='compacted')
    statuses = {entry['status'] for entry in files.values()}
    still_over = any(entry.get('within_limit_after') is False for entry in files.values())
    status = ('conflict' if 'conflict' in statuses else
              'needs_attention' if 'needs_attention' in statuses else
              'dry_run' if dry_run else
              'needs_rewrite' if 'needs_rewrite' in statuses or still_over else
              'compacted' if 'compacted' in statuses else 'within_limit')
    return {'status': status, 'directory': relative, 'files': files, 'dry_run': bool(dry_run),
            'limits_file': 'ok', 'deleted_chars': 0, 'model_calls': False}
