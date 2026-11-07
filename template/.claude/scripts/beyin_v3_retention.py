"""Bounded local state growth (#585): event snapshots, stale temp files and free pages.

Every real record change appends a full snapshot to the `events` table, so a vault
that is edited daily, or reindexed by an upgrade, grows that table without bound.
Its readers need little of it: `history` reads one record's snapshots, the
receipt-view quarantine stores only MAX(sequence) as a cutoff, the updater checks
the table's columns, and sync, receipts, doctor and usage reports never read old
rows. Retention therefore keeps

- the newest event of every record that still exists (exactly what a fresh sync
  of the same vault writes), so `history` of a current record is never empty,
- at most EVENTS_PER_RECORD newest events of each record, and only while they are
  younger than EVENT_RETENTION_DAYS; a deleted record keeps its trail, ending in
  the `delete` event, for that window,
- the newest row of the table, so MAX(sequence) never moves back.

The table has no time column, and adding one would change the schema that an
older release reads after a rollback. Age comes from sequence marks instead: at
most once a day the current MAX(sequence) is saved with the time in `metadata`.
An event at or below a mark saved EVENT_RETENTION_DAYS ago is at least that old.
Events written before the first mark count as new for one full window.

Pruning runs in bounded batches on the existing SQLite writer lock, from the
background queue worker after a successful drain (never from a worker the prompt
or session-start hook is waiting for), at most once a day once the backlog is
gone. It never reads the vault. VACUUM rewrites the whole file and needs an
exclusive lock, so it runs only from the explicit `beyin.py state-compact`
command, which also switches the file to incremental auto-vacuum; after that, and
for every index created by this release, the worker returns a bounded number of
free pages to the file system.

Stale temp files are removed only in the state root and the two hook folders, only
when their names match a temp name this runtime itself writes there, and only when
they are older than TEMP_MAX_AGE_SECONDS.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

EVENT_RETENTION_DAYS = 30
EVENTS_PER_RECORD = 10
PRUNE_BATCH = 500
COMPACT_BATCH = 5000
CHECK_INTERVAL_SECONDS = 86400
MARK_INTERVAL_SECONDS = 86400
TEMP_MAX_AGE_SECONDS = 86400
INCREMENTAL_VACUUM_PAGES = 1024
COMPACT_HINT_BYTES = 64 * 1024 * 1024
MARKS_KEY = 'event_retention_marks'
CHECK_KEY = 'state_retention_last_check'
AUTO_VACUUM = {0: 'none', 1: 'full', 2: 'incremental'}

# Temp names this runtime writes into the state root: beyin_v3_hook.atomic's
# "<name>.<uuid4 hex>.tmp", and tempfile.mkstemp/NamedTemporaryFile names
# (prefix + 8 characters of [a-z0-9_]) of the writers whose targets live there.
_ROOT_TEMP = re.compile(
    r'hook-(?:error|health)\.json\.[0-9a-f]{32}\.tmp'
    r'|\.(?:beyin|beyin-update|index|inbox|hygiene|hermes-platforms|parallel-sessions|scheduled|release)-[a-z0-9_]{8}'
    r'|\.(?:token-counts|passages)-[a-z0-9_]{8}\.tmp')
_FOLDER_TEMP = {
    'hook-queue': re.compile(r'[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp'),
    'receipt-reminders': re.compile(r'[0-9a-f]{64}\.edited\.[0-9a-f]{32}\.tmp'),
}

# An event may go when it is not the table's newest row, is old or beyond its
# record's newest EVENTS_PER_RECORD, and is not the newest event of a current record.
_PRUNABLE = '''
    FROM events e
    WHERE e.sequence < (SELECT MAX(sequence) FROM events)
      AND (e.sequence <= :cutoff
           OR e.sequence < COALESCE((SELECT n.sequence FROM events n WHERE n.record_id = e.record_id
                                     ORDER BY n.sequence DESC LIMIT 1 OFFSET :offset), 0))
      AND (NOT EXISTS (SELECT 1 FROM records r WHERE r.id = e.record_id)
           OR EXISTS (SELECT 1 FROM events n WHERE n.record_id = e.record_id AND n.sequence > e.sequence))
'''


def _busy(exc):
    text = str(exc).lower()
    return 'locked' in text or 'busy' in text


def _marks(db):
    row = db.execute('SELECT value FROM metadata WHERE key=?', (MARKS_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        value = []
    marks = []
    for item in value if isinstance(value, list) else ():
        if (isinstance(item, list) and len(item) == 2 and
                all(type(part) is int and part >= 0 for part in item)):
            marks.append((item[0], item[1]))
    return sorted(marks)


def _cutoff(marks, now):
    """Largest sequence that existed at least EVENT_RETENTION_DAYS ago, or 0."""
    threshold = now - EVENT_RETENTION_DAYS * 86400
    return max((sequence for at, sequence in marks if at <= threshold), default=0)


def _record_mark(db, now):
    """Save today's MAX(sequence); keep only the marks that can still decide an age."""
    # A mark from a clock that ran ahead says nothing about age; drop it.
    marks = [mark for mark in _marks(db) if mark[0] <= now]
    if not marks or now - marks[-1][0] >= MARK_INTERVAL_SECONDS:
        top = db.execute('SELECT COALESCE(MAX(sequence), 0) FROM events').fetchone()[0]
        marks.append((int(now), int(top)))
    cutoff = _cutoff(marks, now)
    threshold = now - EVENT_RETENTION_DAYS * 86400
    old = [mark for mark in marks if mark[0] <= threshold and mark[1] == cutoff][-1:]
    kept = old + [mark for mark in marks if mark[0] > threshold]
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
               (MARKS_KEY, json.dumps([list(mark) for mark in kept], separators=(',', ':'))))
    return cutoff


def _parameters(cutoff, limit=None):
    values = {'cutoff': int(cutoff), 'offset': EVENTS_PER_RECORD - 1}
    if limit is not None:
        values['limit'] = int(limit)
    return values


def prune_events(db, now, limit, record_mark=True):
    """Delete at most `limit` prunable events; the caller holds a write transaction."""
    cutoff = _record_mark(db, now) if record_mark else _cutoff(_marks(db), now)
    return db.execute('DELETE FROM events WHERE sequence IN (SELECT e.sequence ' + _PRUNABLE +
                      ' ORDER BY e.sequence LIMIT :limit)', _parameters(cutoff, limit)).rowcount


def prunable_events(db, now):
    """(count, payload bytes) of the events retention would remove now; reads only."""
    count, size = db.execute('SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(e.record AS BLOB))), 0) ' + _PRUNABLE,
                             _parameters(_cutoff(_marks(db), now))).fetchone()
    return count, size


def _incremental_vacuum(db, pages=INCREMENTAL_VACUUM_PAGES):
    """Return up to `pages` free pages to the file system when the file allows it."""
    if db.execute('PRAGMA auto_vacuum').fetchone()[0] != 2:
        return 0
    free = db.execute('PRAGMA freelist_count').fetchone()[0]
    count = min(int(free), int(pages))
    if count > 0:
        db.execute('PRAGMA incremental_vacuum(%d)' % count).fetchall()
    return count


def stale_temp_files(state, now=None):
    """Our own leftover temp files older than TEMP_MAX_AGE_SECONDS; never follows a link."""
    now = time.time() if now is None else now
    state = Path(state)
    found = []
    for folder, pattern in ((state, _ROOT_TEMP), *((state / name, rule) for name, rule in _FOLDER_TEMP.items())):
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            if not pattern.fullmatch(entry.name):
                continue
            try:
                info = os.lstat(entry.path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > TEMP_MAX_AGE_SECONDS:
                found.append(Path(entry.path))
    return sorted(found)


def sweep_temp_files(state, now=None):
    removed = 0
    for path in stale_temp_files(state, now):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass  # gone already, or still held open on Windows; the next sweep retries
    return removed


def maintain(store, now=None, batch=PRUNE_BATCH):
    """Bounded background maintenance for the queue worker; throttled to once a day."""
    now = time.time() if now is None else now
    result = {'status': 'done', 'events_removed': 0, 'pages_freed': 0, 'temp_files_removed': 0}
    try:
        with store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM metadata WHERE key=?', (CHECK_KEY,)).fetchone()
            try:
                last = float(row[0]) if row else None
            except (TypeError, ValueError):
                last = None
            if last is not None and 0 <= now - last < CHECK_INTERVAL_SECONDS:
                return dict(result, status='throttled')
            result['events_removed'] = prune_events(db, now, batch)
            if result['events_removed'] < batch:
                # Backlog gone: wait a day. A full batch lets the next drain continue.
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (CHECK_KEY, repr(float(now))))
            result['pages_freed'] = _incremental_vacuum(db)
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise
        return dict(result, status='busy')
    result['temp_files_removed'] = sweep_temp_files(store.state_dir, now)
    return result


def _sizes(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        free = db.execute('PRAGMA freelist_count').fetchone()[0]
        mode = db.execute('PRAGMA auto_vacuum').fetchone()[0]
    return {'database_bytes': pages * page_size, 'free_bytes': free * page_size,
            'auto_vacuum': AUTO_VACUUM.get(mode, 'unknown')}


def compact(store, dry_run=False, now=None):
    """Explicit `state-compact`: prune everything due now, sweep temp files, then VACUUM."""
    now = time.time() if now is None else now
    before = _sizes(store.database)
    if dry_run:
        with store._connect() as db:
            count, size = prunable_events(db, now)
        return dict(before, status='planned', events_prunable=count, event_bytes_prunable=size,
                    stale_temp_files=len(stale_temp_files(store.state_dir, now)))
    result = {'status': 'compacted', 'events_removed': 0, 'database_bytes_before': before['database_bytes']}
    try:
        while True:
            # One transaction per batch keeps hooks and the sync worker waiting briefly at most.
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                batch = prune_events(db, now, COMPACT_BATCH)
                if batch < COMPACT_BATCH:
                    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (CHECK_KEY, repr(float(now))))
            result['events_removed'] += batch
            if batch < COMPACT_BATCH:
                break
    except sqlite3.OperationalError as exc:
        if not _busy(exc):
            raise
        result.update(status='busy', vacuum='skipped')  # the uncommitted batch rolled back whole
    result['temp_files_removed'] = sweep_temp_files(store.state_dir, now)
    if result['status'] == 'compacted':
        try:
            with closing(sqlite3.connect(store.database, timeout=store.busy_timeout, isolation_level=None)) as db:
                # Takes effect in this VACUUM; later frees return pages without another full rewrite.
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
                db.execute('VACUUM')
            result['vacuum'] = 'done'
        except sqlite3.OperationalError as exc:
            result['status'] = 'busy' if _busy(exc) else 'needs_attention'
            result['vacuum'] = 'busy' if _busy(exc) else 'failed: ' + str(exc)
    after = _sizes(store.database)
    result.update(database_bytes_after=after['database_bytes'], free_bytes=after['free_bytes'],
                  auto_vacuum=after['auto_vacuum'])
    return result


def storage_report(state, now=None):
    """Doctor's read-only view: sizes, event count and leftover temp files."""
    state = Path(state)
    database = state / 'memory.sqlite3'
    report = {'stale_temp_files': len(stale_temp_files(state, now)),
              'event_retention': {'days': EVENT_RETENTION_DAYS, 'per_record': EVENTS_PER_RECORD}}
    if database.is_symlink() or not database.is_file():
        return dict(report, status='absent')
    report.update(_sizes(database))
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        report['events'] = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    free = report['free_bytes']
    report['compact_suggested'] = free >= COMPACT_HINT_BYTES and free * 4 >= report['database_bytes']
    return dict(report, status='ok')
