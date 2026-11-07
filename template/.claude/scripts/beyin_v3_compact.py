"""Lossless size control for the two companion handoff files (#96): rotate, never delete.

Runs only as the explicit `beyin.py companion-compact` command: no timer, no model call,
no interpretation of the text. When Last-Session.md or Threads.md is over its limit, the
Previous/Closed history section and then the oldest dated entries move verbatim into a
private monthly archive beside the companion files, until the file fits. The newest entry
of every container (the handoff, each thread) always stays. Every line either stays or
moves; nothing is rewritten, summarized or dropped.

Decision lines (`**Karar:**`, `**Karar (Ad):**`, `**Decision:**`) in the moved text are also
copied, verbatim and once each, to Kararlar.md beside the companion files with the heading of
the card they came from, so a decision stays in plain sight after its card is archived.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import errno
import os
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata

from beyin_v3_companion import LIMITS, directory, read_limits
from beyin_v3_notes import NoteConflict, vault_locked

LINE = re.compile(r'[^\n]*\n|[^\n]+$')
HEADING = re.compile(r'(#{1,6})[ \t]')
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')
# Month names for dates written out as `12 Ekim 2026` or `12 October 2026`. A day range such
# as `4→5 Ekim 2026` or `26–27 Eylül 2026` dates the entry by its last day.
MONTHS = {name: number for number, names in enumerate((
    ('ocak', 'january'), ('şubat', 'subat', 'february'), ('mart', 'march'), ('nisan', 'april'),
    ('mayıs', 'mayis', 'may'), ('haziran', 'june'), ('temmuz', 'july'), ('ağustos', 'agustos', 'august'),
    ('eylül', 'eylul', 'september'), ('ekim', 'october'), ('kasım', 'kasim', 'november'),
    ('aralık', 'aralik', 'december')), 1) for name in names}
_DATE = (r'(?:\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])'
         r'|(?:\d{1,2}[ \t]*[-–→][ \t]*)?(\d{1,2})[ \t]+('
         + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')[ \t]+(\d{4}))(?!\d)')
DATE = re.compile(r'(?<!\d)' + _DATE, re.IGNORECASE)
CLOCK = re.compile(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)')
ITEM = re.compile(r'(?:[-*+]|\d{1,3}[.)])[ \t]')
LEADING = re.compile(r'(?:(?:[-*+>]|\d{1,3}[.)])[ \t]+)?[*_`\[(]*' + _DATE, re.IGNORECASE)
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
BACKUP_LOOKBACK_MONTHS = 12
BACKUP_LOOKBACK_BYTES = 8 * 1024 * 1024
POINTER_LINE = re.compile(r'Arşivlenen metin: `[^`\n]+`[ \t]*\r?\n?$')
LOCK_NAME = '.beyin-compact.lock'
DECISIONS = 'Kararlar.md'
DECISION = re.compile(r'[ \t]*(?:[-*+][ \t]+)?\*\*(?:Karar(?:[ \t]+\d+)?(?:[ \t]+\([^*\n)]{1,40}\))?|Decision):[ \t]*\*\*', re.I)
DECISION_KEY = re.compile(r'<!-- beyin-decision: ([0-9a-f]{64}) -->')


class _LockError(Exception):
    pass


def _iso(match):
    """`2026-10-12` for a DATE match, written-out dates included; None for an impossible day."""
    try:
        if not match[1]:
            return datetime.strptime(match[0], '%Y-%m-%d').strftime('%Y-%m-%d')
        month = match[2].replace('İ', 'i').lower()
        return datetime(int(match[3]), MONTHS[month], int(match[1])).strftime('%Y-%m-%d')
    except (KeyError, ValueError):
        return None


def stamp(line, heading=False):
    """(ISO date, time) of an entry-opening line, or None.

    The date (ISO, or written out as in MONTHS) must be among the first 48 characters and,
    outside headings, before any colon: `**Status:** active, 2026-10-01` is a field,
    `2026-09-24 14:05: ...` and `12 Ekim 2026: ...` are entries.
    """
    text = unicodedata.normalize('NFC', line.strip())
    date = DATE.search(text, 0, 48)
    iso = _iso(date) if date else None
    if not iso:
        return None
    colon = text.find(':')
    if not heading and 0 <= colon < date.start():
        return None
    clock = CLOCK.search(text, date.end(), date.end() + 12)
    return iso, (clock[1].zfill(2) + ':' + clock[2]) if clock else ''


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
        if key and (boundary or ITEM.match(bare) or LEADING.match(unicodedata.normalize('NFC', bare))):
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
        # Same-day entries carry no order of their own, and neither does a mixed order such as one
        # backdated card (#220), so the file's direction decides: Last-Session cards are
        # newest-first, thread updates are appended at the bottom (#163).
        newest_first = falling if rising != falling else name == 'Last-Session.md'
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


def decisions(text, moved_lines):
    """(card heading, decision line) pairs among the moved lines, in file order.

    A dated card keeps its heading through deeper subsections; otherwise use the nearest
    level-2+ heading. Fenced code is skipped like
    everywhere else in the companion files.
    """
    found, heading, fence, card_level = [], None, None, None
    moved = set(moved_lines)
    for index, line in enumerate(LINE.findall(text)):
        bare = line.rstrip('\r\n')
        if fence:  # same fence rule as blocks()
            if re.match(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*$', bare):
                fence = None
            continue
        opened = FENCE.match(bare)
        if opened and not (opened[1][0] == '`' and '`' in bare[opened.end():]):
            fence = opened[1]
            continue
        level = HEADING.match(line)
        if level and len(level[1]) >= 2:
            depth = len(level[1])
            if not card_level or depth <= card_level:
                heading = line.strip()
                card_level = depth if stamp(heading, heading=True) else None
        elif index in moved and DECISION.match(line):
            found.append((heading, bare))
    return found


def _decision_source_allowed(text):
    """A copied line must never broaden the source's local visibility or trust policy."""
    from beyin_v3_sync import parse
    from beyin_v3 import _rejected_inference
    try:
        metadata, _ = parse(text)
    except (ValueError, TypeError):
        return False
    return (metadata.get('visibility', 'internal') in ('internal', 'public')
            and metadata.get('trust') != 'untrusted' and metadata.get('trusted') is not False
            and metadata.get('status') != 'untrusted' and metadata.get('kind') != 'untrusted'
            and not _rejected_inference(metadata))


def _decision_ledger_check(target, vault):
    path = target / DECISIONS
    _inside(path, vault)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('decision ledger must be a real UTF-8 file inside the vault')
    if path.exists():
        from beyin_v3_sync import parse
        metadata, _ = parse(path.read_bytes().decode('utf-8'))
        if metadata.get('visibility') != 'private' and metadata.get('remote_allowed') is not False:
            raise ValueError('existing decision ledger must have remote_allowed: false or visibility: private')


def _record_decisions(target, vault, found, archive_relative, newline, source_relative=None):
    """Append each source/card/line once, without losing identical decisions in other cards."""
    path = target / DECISIONS
    _inside(path, vault)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('decision ledger must be a real UTF-8 file inside the vault')
    existing = path.read_bytes().decode('utf-8') if path.exists() else None
    known = set(DECISION_KEY.findall(existing or ''))
    fresh = []
    source_relative = source_relative or archive_relative
    for heading, line in found:
        key = hashlib.sha256(json.dumps([source_relative, heading, line], ensure_ascii=False).encode('utf-8')).hexdigest()
        if key not in known:
            known.add(key)
            fresh.append((heading, line, key))
    if not fresh:
        return 0
    if existing is None:
        existing = newline.join([
            '---', '{"kind": "note", "visibility": "internal", "remote_allowed": false, "title": "Kararlar"}', '---', '# Kararlar', '',
            '`beyin.py companion-compact` arşive taşıdığı kartlardaki karar satırlarını buraya, kartın başlığı '
            've arşiv yoluyla kelimesi kelimesine ekler. Dosya yalnız eklenerek büyür; elle düzenleyebilirsin.', ''])
    elif not existing.endswith('\n'):
        existing += newline
    added, groups = [], []
    for heading, line, key in fresh:
        if not groups or groups[-1][0] != heading:
            groups.append((heading, []))
        groups[-1][1].append((line, key))
    for heading, group in groups:
        added.append(newline + ''.join('<!-- beyin-decision: ' + key + ' -->' + newline for _, key in group))
        added.append((heading if heading else '## (başlıksız)') + newline + 'Arşiv: `' + archive_relative + '`' + newline)
        added.extend(line + newline for line, _ in group)
    _write(path, existing + ''.join(added))
    return len(fresh)


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


def _previous_backup(target, vault, name, now, backup):
    """Find a complete, byte-equivalent snapshot without an unbounded archive scan.

    This is only for an unchanged file with nothing left to move. Missing, unsafe,
    unreadable or over-budget archives are cache misses: a new complete backup is
    safer than claiming a copy that has not been verified. No state index survives
    deletion or external editing of the actual archive.
    """
    remaining = BACKUP_LOOKBACK_BYTES
    month_number = now.year * 12 + now.month - 1
    marker = BACKUP.format(len(backup))
    for offset in range(1, BACKUP_LOOKBACK_MONTHS + 1):
        year, month = divmod(month_number - offset, 12)
        candidate = target / ARCHIVE_DIRECTORY / f'{Path(name).stem}-{year:04d}-{month + 1:02d}.md'
        try:
            _inside(candidate, vault)
            if not candidate.is_file() or candidate.stat().st_size > remaining:
                continue
            with candidate.open('rb') as handle:
                raw = handle.read(remaining + 1)
            remaining -= len(raw)
            if remaining < 0:
                break
            archived = raw.decode('utf-8')
        except (OSError, UnicodeError, ValueError):
            continue
        # Bind the full payload to its backup heading; finding the text elsewhere
        # in moved history or a bare marker is not enough to identify a snapshot.
        if any(marker + newline + newline + backup in archived for newline in ('\n', '\r\n')):
            return candidate.relative_to(vault).as_posix()
    return None


@contextmanager
def _compact_lock(target, timeout=10.0, step=0.1):
    """Serialize writers that share one canonical companion directory.

    The empty file is intentionally persistent: unlinking a held advisory-lock file can
    let a later process lock a new inode while the first process still owns the old one.
    """
    lock_path = Path(target).resolve() / LOCK_NAME
    try:
        if lock_path.is_symlink():
            raise OSError('compaction lock must not be a symlink')
        handle = lock_path.open('a+b')
    except OSError as exc:
        raise _LockError(f'cannot open compaction lock {lock_path}: {exc}') from exc

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
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise _LockError(f'cannot use compaction lock {lock_path}: {exc}') from exc
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
    # An uninitialized vault has nothing to compact and should not gain a companion
    # directory merely because the lock lives beside the files it protects.
    if not target.is_dir():
        return {'status': 'within_limit', 'directory': target.relative_to(vault).as_posix(), 'files': {},
                'dry_run': False, 'limits_file': 'ok', 'deleted_chars': 0, 'model_calls': False}
    with ExitStack() as guard:
        # Plan reads, archive append, decision ledger and live replacement are one
        # critical section shared with note-edit/create and note journal recovery.
        # Retain the inner lock for compatibility with already-running compactors.
        try:
            guard.enter_context(vault_locked(vault))
        except NoteConflict as exc:
            return {'status': 'conflict', 'reason': str(exc), 'files': {}}
        except (OSError, ValueError) as exc:
            return {'status': 'needs_attention', 'reason': 'cannot acquire note lock: ' + str(exc), 'files': {}}
        try:
            with _compact_lock(target):
                return _compact_files(vault, target, configured, dry_run=False, now=now)
        except TimeoutError as exc:
            return {'status': 'conflict', 'reason': str(exc), 'files': {}}
        except _LockError as exc:
            return {'status': 'needs_attention', 'reason': str(exc), 'files': {}}


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
        found = decisions(text, result['moved_lines']) if result is not None and _decision_source_allowed(text) else []
        if found:
            try:
                _decision_ledger_check(target, vault)
            except (ValueError, UnicodeError, OSError) as exc:
                files[name] = dict(report, status='needs_attention', reason=str(exc) + '; nothing moved')
                continue
        if dry_run:
            if found:
                report.update(decisions_planned=len(found))
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
                previous_backup = _previous_backup(target, vault, name, now, backup) if result is None else None
                if previous_backup is not None:
                    # Nothing is written below, so the live check under `if added` never runs;
                    # an older snapshot only covers the file if the scan did not race an edit.
                    if (path.read_bytes() if path.exists() else None) != raw:
                        files[name] = dict(report, status='conflict',
                                           reason='file changed during compaction; nothing moved, retry')
                        continue
                    report.update(backup='exists', archive=previous_backup)
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
            current_live = path.read_bytes() if path.exists() else None
            current_archive = archive.read_bytes() if archive.exists() else None
            if current_live != raw or current_archive != previous:
                report.pop('backup', None)
                files[name] = dict(report, status='conflict',
                                   reason='file changed during compaction; nothing moved, retry')
                continue
            _write(archive, archive_written)
            # Best-effort conflict detection for writers that do not take our lock.
            # Separate reads and writes are not an atomic compare-and-swap.
            if path.read_bytes() != raw:
                # #193: Roll back only if the archive still holds this invocation's exact write.
                # A concurrent caller that already committed its own archive is never reverted.
                if archive.exists() and archive.read_bytes() == archive_written.encode('utf-8'):
                    if previous is None:
                        try:
                            archive.unlink(missing_ok=True)
                        except OSError:
                            pass
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
        if found:
            # Store the searchable copy before removing its original live card.
            report.update(decisions_added=_record_decisions(target, vault, found, archive_relative, result['newline'],
                                                           source_relative=f'{relative}/{name}'))
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
