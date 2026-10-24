"""Deterministic receipt indexes; never rewrite existing human daily/knowledge."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
import math


# metadata row: the folded directory names the last sync kept out of the index, as a JSON list.
EXCLUDED_DIRS_ROW = 'index_excluded_dirs'
RECEIPT_VIEW_SOURCES_ROW = 'receipt_view_sources'
RECEIPT_VIEW_RECEIPT_SUFFIX = '.md.view-provenance.json'


def _record_excluded_dirs(engine, db):
    """Store what this sync's scan excluded, inside the sync's own transaction.

    The receipt readers below have the database, not the state directory, and a hook reads it
    without syncing first. They must know which paths lost their record to the user's list,
    and index.json cannot tell them: it may have changed since the index was built. This row
    is committed together with the record deletions (or re-indexing) the list caused, so it
    always describes the records the same reader sees. A release without the row ignores it.
    """
    keys = getattr(engine, 'excluded_dir_keys', None)
    if keys is None:
        return  # no scan ran on this engine; the stored row still describes the index
    stored = db.execute('SELECT value FROM metadata WHERE key=?', (EXCLUDED_DIRS_ROW,)).fetchone()
    value = json.dumps(sorted(keys), ensure_ascii=False) if keys else None
    if value is None:
        if stored:
            db.execute('DELETE FROM metadata WHERE key=?', (EXCLUDED_DIRS_ROW,))
    elif not stored or stored[0] != value:
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (EXCLUDED_DIRS_ROW, value))


def _excluded_dir_keys(db):
    try:
        stored = db.execute('SELECT value FROM metadata WHERE key=?', (EXCLUDED_DIRS_ROW,)).fetchone()
        value = json.loads(stored[0]) if stored else []
    except (sqlite3.OperationalError, ValueError):  # a bare receipts-only database has no metadata
        return frozenset()
    return frozenset(key for key in value if isinstance(key, str)) if isinstance(value, list) else frozenset()


def _hidden_ref_sources(db, refs):
    """Sources that default internal context never shows: indexed records behind the _eligible
    visibility/trust gate, and every path under a directory the last sync kept out of the index.

    A path under an excluded directory has no record, so nothing says whether it is private.
    Excluding a directory must not turn a withheld ref into a shown one, so such a path is
    withheld like a private one, whatever the file says.
    """
    refs = sorted(set(refs))
    from beyin_v3_hygiene import sensitive_source
    hidden = {ref for ref in refs if sensitive_source(ref)}
    # View provenance survives removal of its indexed privacy/trust metadata.
    # All claimed paths and proven aliases are withheld, including in fresh state.
    try:
        row = db.execute('SELECT value FROM metadata WHERE key=?', (RECEIPT_VIEW_SOURCES_ROW,)).fetchone()
        hidden.update(set(refs).intersection(json.loads(row[0]) if row else []))
    except sqlite3.OperationalError:  # receipts-only reader
        pass
    excluded = _excluded_dir_keys(db)
    if excluded:
        from beyin_v3_index import dir_key
        hidden.update(ref for ref in refs
                      if any(dir_key(part) in excluded for part in ref.replace('\\', '/').split('/')[:-1]))
    for start in range(0, len(refs), 500):
        chunk = refs[start:start + 500]
        try:
            rows = db.execute('SELECT r.payload FROM markdown_sources m JOIN records r ON r.id = m.id WHERE m.source IN (' +
                              ','.join('?' * len(chunk)) + ')', chunk).fetchall()
        except sqlite3.OperationalError:  # a bare receipts-only database has no source index
            return hidden
        for (payload,) in rows:
            record = json.loads(payload)
            if (record.get('visibility', 'internal') not in ('public', 'internal') or record.get('trust') == 'untrusted' or
                    record.get('trusted') is False or record.get('status') == 'untrusted' or record.get('kind') == 'untrusted'):
                hidden.add(record.get('source'))
    return hidden


def _vault_file(vault, relative):
    try:
        root = Path(vault).resolve()
        target = (root / relative).resolve()
        return target.is_relative_to(root) and target.is_file()
    except (OSError, ValueError, TypeError):
        return False


def _withheld_view_receipts(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_view_receipts'").fetchone():
        return {row[0] for row in db.execute('SELECT id FROM receipt_view_receipts')}
    return set()


def latest_receipts(db, vault, sessions=None):
    """Receipts an automatic context may show, newest first by created_at, then event_id, as (source, event).

    The order is the receipt's own stamp, never the file mtime that a git pull or a sync
    client rewrites (#112). Only receipts with a readable stamp, a summary and a regular
    source file inside the vault are yielded; with sessions, only those sessions. A receipt
    whose source or any ref is a private or untrusted record is skipped whole: its summary
    describes that record. The SessionStart hook and the global bridge share this gate.
    """
    vault = Path(vault)
    withheld = _withheld_view_receipts(db)
    candidates = []
    for (payload,) in db.execute('SELECT payload FROM receipts'):
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or not isinstance(event.get('summary'), str):
            continue
        if event['event_id'] in withheld or (sessions is not None and event.get('session') not in sessions):
            continue
        instant = _receipt_instant(event.get('created_at'))
        if instant is not None:
            candidates.append((instant, event['event_id'], event))
    for _, ident, event in sorted(candidates, key=lambda item: item[:2], reverse=True):
        source = 'receipts/' + hashlib.sha256(ident.encode()).hexdigest() + '.md'
        refs = [ref for ref in event.get('refs') or [] if isinstance(ref, str)] if isinstance(event.get('refs'), list) else []
        if (not (vault / source).is_symlink() and _vault_file(vault, source) and
                not _hidden_ref_sources(db, [source, *refs])):
            yield source, event


def _bound_day(bound, zone):
    """Local calendar day of a recap bound; a date bound already is that day."""
    return bound.astimezone(zone).date() if isinstance(bound, datetime) else bound


def _within(bound, stamp, day, lower):
    """One recap bound: an instant is compared with the receipt's instant, a date with its local day."""
    if bound is None:
        return True
    value = stamp if isinstance(bound, datetime) else day
    return bound <= value if lower else value <= bound


def recent_receipts(db, days=7, limit=20, today=None, vault=None, since=None, until=None):
    """A bounded, source-linked activity view; summaries remain agent claims.

    With a vault, receipts whose own source file is gone are omitted, and refs that no longer
    exist or that point at private/untrusted records are withheld and only counted.

    Without since/until the window is the last `days` UTC calendar days ending today. Either
    bound replaces that window with an explicit range (#229). A date bound is a local calendar
    day, the day receipt_day gives and daily/v3 is named by: since keeps receipts whose local
    day is that day or later, until keeps them through the end of that day, so the same date in
    both returns that one day. A datetime bound is an exact instant, inclusive. Days are compared
    as dates and no midnight instant is computed, so a day with a clock change needs no special
    case. Without since there is no lower bound and 'from' is None; without until the range ends
    with today's local day. 'from' and 'through' stay dates: the local day of each bound.
    """
    if not 1 <= days <= 366 or not 1 <= limit <= 100:
        raise ValueError('recap days must be 1..366 and limit must be 1..100')
    ranged = since is not None or until is not None
    if ranged:
        zone = _local_zone()
        if until is None:
            until = today or datetime.now(timezone.utc).astimezone(zone).date()
    else:
        today = today or datetime.now(timezone.utc).date()
        start = today - timedelta(days=days - 1)
    matches = []
    undated = 0
    withheld = _withheld_view_receipts(db)
    for (payload,) in db.execute('SELECT payload FROM receipts'):
        try:
            event = json.loads(payload)
            stamp = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            stamp = stamp.astimezone(timezone.utc)
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            undated += 1
            continue
        day = None
        if ranged:
            try:
                day = stamp.astimezone(zone).date()
            except (ValueError, OverflowError, OSError):
                # No local day exists for this instant (Windows localtime rejects some); never invent one.
                undated += 1
                continue
            if not (_within(since, stamp, day, True) and _within(until, stamp, day, False)):
                continue
        elif not start <= stamp.date() <= today:
            continue
        ident = event.get('event_id')
        if not isinstance(ident, str) or not ident or not isinstance(event.get('summary'), str):
            undated += 1
            continue
        if ident in withheld:
            continue
        source = 'receipts/' + hashlib.sha256(ident.encode()).hexdigest() + '.md'
        matches.append((stamp, ident, source, event, day))
    missing_sources = 0
    if vault is not None:
        # One directory listing instead of a stat per receipt; symlinked receipts are never adopted by sync.
        receipts_dir = Path(vault) / 'receipts'
        try:
            names = {entry.name for entry in os.scandir(receipts_dir) if entry.is_file(follow_symlinks=False)}
        except OSError:
            names = set()
        present = [match for match in matches if match[2][len('receipts/'):] in names]
        missing_sources = len(matches) - len(present)
        matches = present
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    shown = [(stamp, source, event, day, [ref for ref in event.get('refs') or [] if isinstance(ref, str)]
              if isinstance(event.get('refs'), list) else []) for stamp, _, source, event, day in matches[:limit]]
    hidden = _hidden_ref_sources(db, [ref for *_, refs in shown for ref in refs]) if vault is not None else set()
    items = []
    for stamp, source, event, day, refs in shown:
        kept, private, missing = [], 0, 0
        for ref in refs:
            if ref in hidden:
                private += 1
            elif vault is not None and not _vault_file(vault, ref):
                missing += 1
            else:
                kept.append(ref)
        item = {'created_at': stamp.isoformat(), 'summary': event['summary'], 'source': source, 'refs': kept}
        if isinstance(event.get('links'), list) and event['links']:
            item['links'] = [link for link in event['links'] if isinstance(link, str)]
        if day is not None:
            item['day'] = day.isoformat()  # the local day that placed it in the range; created_at stays UTC
        if private or missing:
            item['refs_withheld'] = {'private': private, 'missing': missing}
        items.append(item)
    if ranged:
        window = {'from': None if since is None else _bound_day(since, zone).isoformat(),
                  'through': _bound_day(until, zone).isoformat(), 'timezone': 'local'}
    else:
        window = {'from': start.isoformat(), 'through': today.isoformat(), 'timezone': 'UTC'}
    result = {
        'status': 'ok', **window, 'audience': 'internal', 'total': len(matches), 'shown': len(items),
        'truncated': len(matches) > limit, 'undated_omitted': undated,
        'items': items,
        'meaning': 'Agent-authored outcomes, not independently verified facts.',
    }
    if missing_sources:
        result['missing_source_omitted'] = missing_sources
    return result


def _checkpoint_schema(db, state=None):
    db.execute('CREATE TABLE IF NOT EXISTS receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
    if 'turn_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN turn_at REAL DEFAULT 0')
    if 'prompt_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN prompt_at REAL DEFAULT 0')
    # Start of the latest turn that edited a file (#212); a later conversational turn keeps it.
    if 'edit_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN edit_at REAL DEFAULT 0')
    for column in ('project', 'project_id'):
        if column not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
            db.execute(f'ALTER TABLE receipt_checkpoints ADD COLUMN {column} TEXT')
    if state is not None:
        backfill_edit_at(db, state)


def backfill_edit_at(db, state):
    """Backfill missing edit_at timestamps for receipt checkpoints from historical hook-done events (#212)."""
    if not state:
        return 0
    state_dir = Path(state)
    if db.execute("SELECT 1 FROM metadata WHERE key='edit_at_backfilled'").fetchone():
        return 0
    hook_done = state_dir / "hook-done"
    if not hook_done.is_dir():
        return 0

    unfilled = {
        (row[1], row[0]): {'harness': row[1], 'turn_at': row[2], 'at': row[3]}
        for row in db.execute("SELECT session, harness, turn_at, at FROM receipt_checkpoints WHERE edit_at IS NULL OR edit_at = 0")
    }
    if not unfilled:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('edit_at_backfilled', '1')")
        return 0

    edits = {}
    prompt_turns = {}
    try:
        for path in hook_done.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(data, dict) or data.get('no_memory'):
                continue
            harness, session = data.get('harness'), data.get('session')
            if not isinstance(harness, str) or not isinstance(session, str):
                continue
            sess = (harness, session)
            if sess not in unfilled:
                continue
            ev = data.get('event')
            ev_at = data.get('at', 0)
            if type(ev_at) not in (int, float) or not math.isfinite(ev_at) or ev_at <= 0:
                continue
            if ev in ('UserPromptSubmit', 'SessionStart') and ev_at:
                prompt_turns.setdefault(sess, []).append(ev_at)
            elif ev == 'PostToolUse' and ev_at:
                edits.setdefault(sess, []).append(ev_at)
    except OSError:
        return 0

    count = 0
    for sess, edit_times in edits.items():
        sess_info = unfilled[sess]
        turns = sorted(prompt_turns.get(sess, []))
        turn_edit_starts = []
        for e_at in edit_times:
            t_candidates = [t for t in turns if t <= e_at]
            if t_candidates:
                turn_edit_starts.append(max(t_candidates))
            elif sess_info['turn_at'] and sess_info['turn_at'] <= e_at:
                turn_edit_starts.append(sess_info['turn_at'])
            else:
                turn_edit_starts.append(e_at)
        if turn_edit_starts:
            target_edit_at = max(turn_edit_starts)
            db.execute("UPDATE receipt_checkpoints SET edit_at = MAX(COALESCE(edit_at, 0), ?) WHERE harness = ? AND session = ?", (target_edit_at, sess[0], sess[1]))
            count += 1

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('edit_at_backfilled', '1')")
    return count


def record_checkpoints(engine, events):
    with engine.store._connect() as db:
        _checkpoint_schema(db)
        for event in sorted(events, key=lambda item: item.get('at', 0)):
            if not event.get('session') or event.get('no_memory'):
                continue
            if event.get('synthetic') and event.get('event') in ('UserPromptSubmit', 'SessionStart'):
                # A harness or app turn no person typed (task notification, Codex Desktop suggestion fork)
                # opens no checkpoint and moves no turn boundary, so it cannot reopen finished work (#373).
                continue
            values = (event['harness'], event['session'], event['at'])
            if event.get('event') == 'UserPromptSubmit' or (event.get('event') == 'SessionStart' and event.get('prompted')):
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at,turn_at,prompt_at) VALUES (?,?,0,?,?) ON CONFLICT(harness,session) DO UPDATE SET turn_at=MAX(turn_at,excluded.turn_at), prompt_at=MAX(prompt_at,excluded.prompt_at)',
                           (event['harness'], event['session'], event['at'], event['at']))
            elif event.get('event') == 'SessionStart':
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at,turn_at) VALUES (?,?,0,?) ON CONFLICT(harness,session) DO UPDATE SET turn_at=MAX(turn_at,excluded.turn_at)', values)
            elif event.get('event') in ('Stop', 'SessionEnd'):
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at) VALUES (?,?,?) ON CONFLICT(harness,session) DO UPDATE SET at=MAX(at,excluded.at)', values)
            elif event.get('event') == 'PostToolUse':
                # Installed matchers send only file-editing tools; remember the turn that edited, not the edit.
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at,edit_at) VALUES (?,?,0,?) ON CONFLICT(harness,session) DO UPDATE SET edit_at=MAX(edit_at,CASE WHEN turn_at>0 AND turn_at<=excluded.edit_at THEN turn_at ELSE excluded.edit_at END)', values)
            if event.get('project') and event.get('project_id'):
                db.execute('UPDATE receipt_checkpoints SET project=?,project_id=? WHERE harness=? AND session=?',
                           (event['project'], event['project_id'], event['harness'], event['session']))


def _latest_receipts(db):
    """Latest receipt time per (harness, session); a missing or unreadable created_at never covers a checkpoint."""
    latest = {}
    for (payload,) in db.execute('SELECT payload FROM receipts'):
        try:
            receipt = json.loads(payload)
            created = datetime.fromisoformat(receipt['created_at']).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        key = (receipt.get('harness'), receipt.get('session'))
        latest[key] = max(created, latest.get(key, created))
    return latest


def _coverage_threshold(at, turn_at, edit_at):
    """Earliest receipt time that closes a checkpoint.

    A receipt must follow the start of the latest turn that edited a file, so a later
    "thanks" or status question does not reopen finished work (#212), while a later turn
    that edits again still needs its own receipt. Without an observed edit (other harnesses,
    rows from before edit_at existed) the latest prompt boundary applies as before.
    """
    return edit_at or turn_at or at


def receipt_coverage(db, now=None, state=None):
    if now is None:
        now = time.time()
    seven_days = now - 7 * 86400
    thirty_days = now - 30 * 86400
    _checkpoint_schema(db, state=state)
    latest = _latest_receipts(db)
    windows = {
        'all_time': {'total': 0, 'covered': 0, 'missing': 0, 'no_observed_edit': 0, 'without_prompt': 0, 'without_prompt_with_receipt': 0},
        'last_7d': {'total': 0, 'covered': 0, 'missing': 0, 'no_observed_edit': 0, 'without_prompt': 0, 'without_prompt_with_receipt': 0},
        'last_30d': {'total': 0, 'covered': 0, 'missing': 0, 'no_observed_edit': 0, 'without_prompt': 0, 'without_prompt_with_receipt': 0},
    }
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id,prompt_at,edit_at FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        # Only count sessions that saw a user prompt: UserPromptSubmit, or a SessionStart carrying the first prompt (#78)
        chk_at = row[2]
        active = (('all_time', True), ('last_30d', chk_at >= thirty_days), ('last_7d', chk_at >= seven_days))
        if not row[6]:
            # Still kept out of the ratio, but counted, receipts included: an unexplained
            # small total reads as missing data. Any session without a prompt marker lands
            # here: every session outside the vault (the global bridge installs no
            # UserPromptSubmit), and vault sessions recorded before the marker existed or
            # closed before a prompt was observed.
            has_receipt = (row[0], row[1]) in latest
            for w_name, w_active in active:
                if w_active:
                    windows[w_name]['without_prompt'] += 1
                    windows[w_name]['without_prompt_with_receipt'] += has_receipt
            continue
        threshold = _coverage_threshold(row[2], row[3], row[7])
        matched = latest.get((row[0], row[1]), float('-inf')) >= threshold
        is_no_edit = not bool(row[7])
        for w_name, w_active in active:
            if w_active:
                windows[w_name]['total'] += 1
                if is_no_edit:
                    windows[w_name]['no_observed_edit'] += 1
                if matched:
                    windows[w_name]['covered'] += 1
                else:
                    windows[w_name]['missing'] += 1

    return {
        'ratio': round(windows['all_time']['covered'] / windows['all_time']['total'], 3) if windows['all_time']['total'] else None,
        'covered': windows['all_time']['covered'],
        'total': windows['all_time']['total'],
        'missing': windows['all_time']['missing'],
        'no_observed_edit': windows['all_time']['no_observed_edit'],
        'without_prompt': windows['all_time']['without_prompt'],
        'without_prompt_with_receipt': windows['all_time']['without_prompt_with_receipt'],
        'last_7d': {
            'ratio': round(windows['last_7d']['covered'] / windows['last_7d']['total'], 3) if windows['last_7d']['total'] else None,
            'covered': windows['last_7d']['covered'],
            'total': windows['last_7d']['total'],
            'missing': windows['last_7d']['missing'],
            'no_observed_edit': windows['last_7d']['no_observed_edit'],
            'without_prompt': windows['last_7d']['without_prompt'],
            'without_prompt_with_receipt': windows['last_7d']['without_prompt_with_receipt'],
        },
        'last_30d': {
            'ratio': round(windows['last_30d']['covered'] / windows['last_30d']['total'], 3) if windows['last_30d']['total'] else None,
            'covered': windows['last_30d']['covered'],
            'total': windows['last_30d']['total'],
            'missing': windows['last_30d']['missing'],
            'no_observed_edit': windows['last_30d']['no_observed_edit'],
            'without_prompt': windows['last_30d']['without_prompt'],
            'without_prompt_with_receipt': windows['last_30d']['without_prompt_with_receipt'],
        }
    }


def refresh_gaps(engine, db):
    atomic = engine.projection_helpers()[1]
    _checkpoint_schema(db, state=engine.state)
    latest = _latest_receipts(db)
    gaps = []
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id,edit_at,prompt_at FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3] or not row[7]:
            continue
        threshold = _coverage_threshold(row[2], row[3], row[6])
        matched = latest.get((row[0], row[1]), float('-inf')) >= threshold
        if not matched:
            gaps.append({'harness': row[0], 'session': row[1], 'checkpoint_at': row[2], 'turn_at': row[3], 'scope': 'session_only' if row[0] == 'antigravity' else 'turn' if row[3] else 'terminal_only'})
            if row[4] and row[5]:
                gaps[-1].update(project=row[4], project_id=row[5])
    coverage = receipt_coverage(db, now=time.time(), state=engine.state)
    atomic(engine.state/'receipt-gaps.json', json.dumps({
        'potential_missing_receipts': len(gaps),
        'receipt_coverage': coverage,
        'checkpoints': gaps,
        'scope_limits': {'antigravity': 'session_only; later per-turn boundaries unsupported', 'missing_prompt_event': 'terminal_only; receipt attribution may be incomplete'},
        'meaning': 'Checkpoint without a matching structured receipt; may be trivial or deliberately omitted. No summary inferred.'
    }))


def _local_zone():
    """Zone for the day a person reads in a view name; None is this machine's local zone. Tests patch this."""
    return None


def _receipt_instant(created_at):
    """Aware UTC instant of a receipt stamp, or None when it cannot be read. A naive stamp is UTC, as in recap."""
    try:
        stamp = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


RECEIPT_VIEW_REASONS = frozenset((
    'manual receipt view edit preserved',
    'untracked receipt view preserved',
    'receipt view changed during projection',
    'stale receipt view unreadable; preserved',
    'manual receipt view edit preserved; view no longer generated',
    'stale receipt view could not be removed',
    'receipt view recovery failed; preserved',
))


def is_receipt_view_source(source, *, proven=False):
    """Legacy reserved paths, or a flat path with independent ownership evidence.

    A flat filename alone is never evidence: ordinary human daily notes use it too.
    Callers may prove ownership with tracking rows, generated metadata or an archive.
    """
    return isinstance(source, str) and (source == 'knowledge/v3/outcomes.md' or
        re.fullmatch(r'daily/v3/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md', source) is not None or
        (proven and re.fullmatch(r'daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md', source) is not None))


def _receipt_view_table(relative):
    # Older engines reject flat paths in receipt_views during every sync.
    return ('receipt_flat_views' if re.fullmatch(r'daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md', relative)
            else 'receipt_views')


def receipt_view_rows(db):
    """Read both ownership tables, including before the first projection."""
    rows = {}
    for table in ('receipt_views', 'receipt_flat_views'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            rows.update(db.execute(f'SELECT path, hash FROM {table}'))
    return sorted(rows.items())


def _prepare_receipt_views(db):
    for table in ('receipt_views', 'receipt_flat_views'):
        db.execute(f'CREATE TABLE IF NOT EXISTS {table}(path TEXT PRIMARY KEY, hash TEXT NOT NULL)')
    # Preserve ownership from the initial flat-layout implementation, including
    # edited views whose original hash is required for safe recovery.
    for relative, digest in db.execute('SELECT path, hash FROM receipt_views').fetchall():
        if _receipt_view_table(relative) == 'receipt_flat_views':
            db.execute('INSERT OR IGNORE INTO receipt_flat_views VALUES (?,?)', (relative, digest))
            db.execute('DELETE FROM receipt_views WHERE path=?', (relative,))


def is_receipt_view_conflict(item):
    # Both provenance and diagnosis are required. A source conflict must never
    # become writable merely because its name falls under a generated directory.
    return (isinstance(item, dict) and is_receipt_view_source(item.get('source'), proven=True) and
            item.get('reason') in RECEIPT_VIEW_REASONS)


def _archive_receipt_view(engine, path, expected_hash, _hash):
    """Durably preserve one proven view in a unique, vault-local archive."""
    relative = path.relative_to(engine.root).as_posix()
    path = engine._path(relative, existing=True)
    raw = path.read_bytes()
    if _hash(raw) != expected_hash:
        raise ValueError('receipt view changed before archive')
    archive_dir = path.parent / '.archive'
    # _path proves all parents, even when the prospective leaf does not exist.
    engine._path((archive_dir / 'probe.md').relative_to(engine.root).as_posix())
    archive_dir.mkdir(parents=True, exist_ok=True)
    engine._path((archive_dir / 'probe.md').relative_to(engine.root).as_posix())
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    fd, filename = tempfile.mkstemp(prefix=path.stem + '-' + stamp + '-', suffix=path.suffix, dir=archive_dir)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    # A recovered hard link retains the old view inode after the canonical
    # path is replaced. Keep its proven path beside the archive so a rebuilt
    # SQLite state can still recognize it. The archive bytes stay exact.
    identity = engine._source_identity(path)
    aliases = []
    for source in sorted(getattr(engine, 'receipt_view_sources', ())):
        if source == relative:
            continue
        try:
            _, resolved = engine._scan_path(source)
            if engine._source_identity(resolved) == identity:
                aliases.append({'source': source, 'identity': identity})
        except (ValueError, OSError):
            continue
    if aliases:
        sidecar = Path(str(filename) + '.provenance.json')
        provenance = json.dumps({'schema': 1, 'view': relative,
                                 'archive': Path(filename).name,
                                 'sha256': expected_hash, 'aliases': aliases},
                                ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(provenance) > 65536:
            raise ValueError('receipt view alias provenance exceeds size limit')
        with sidecar.open('xb') as out:
            out.write(provenance)
            out.flush()
            os.fsync(out.fileno())
    # The file fsync does not persist its name, or the newly created .archive
    # entry. Required POSIX directory barriers must succeed before destroying
    # the original. Windows does not expose directory fsync through this API.
    if os.name != 'nt':
        for directory in (archive_dir, path.parent):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    # Preserve a concurrent user edit as a conflict; the first bytes remain archived.
    path = engine._path(relative, existing=True)
    if _hash(path.read_bytes()) != expected_hash:
        raise ValueError('receipt view changed after archive')
    return Path(filename)


def archived_receipt_view_aliases(engine, warnings):
    """Recover proven live alias paths from adjacent archive metadata after state loss.

    The sidecar's archive digest is checked before use. A copied vault may
    change inode numbers, so unchanged alias bytes at the recorded path are
    also accepted; unrelated equal-content files at other paths are not.
    """
    aliases = set()
    for root in (engine.root / 'knowledge/v3', engine.root / 'daily/v3', engine.root / 'daily'):
        archive_dir = root / '.archive'
        if archive_dir.is_symlink() or not archive_dir.is_dir():
            continue
        try:
            engine._path((archive_dir / 'probe.md').relative_to(engine.root).as_posix())
        except (ValueError, OSError):
            warnings.append({'source': archive_dir.relative_to(engine.root).as_posix(),
                             'reason': 'receipt view provenance directory unsafe'})
            continue
        for sidecar in sorted(archive_dir.glob('*.md.provenance.json')):
            relative = sidecar.relative_to(engine.root).as_posix()
            try:
                if sidecar.is_symlink() or sidecar.stat().st_size > 65536:
                    raise ValueError('unsafe receipt view provenance')
                engine._path(relative, existing=True)
                data = json.loads(sidecar.read_text(encoding='utf-8'))
                archive = sidecar.with_name(sidecar.name[:-len('.provenance.json')])
                if (not isinstance(data, dict) or data.get('schema') != 1 or
                        data.get('archive') != archive.name or
                        not is_receipt_view_source(data.get('view'), proven=True) or
                        Path(data['view']).parent != root.relative_to(engine.root) or
                        not isinstance(data.get('aliases'), list) or archive.is_symlink()):
                    raise ValueError('invalid receipt view provenance')
                engine._path(archive.relative_to(engine.root).as_posix(), existing=True)
                digest = hashlib.sha256()
                with archive.open('rb') as handle:
                    for block in iter(lambda: handle.read(65536), b''):
                        digest.update(block)
                if digest.hexdigest() != data.get('sha256'):
                    raise ValueError('receipt view archive digest mismatch')
                if any(not isinstance(item, dict) or not isinstance(item.get('source'), str)
                       for item in data['aliases']):
                    raise ValueError('invalid receipt view alias')
                for item in data['aliases']:
                    source = item['source']
                    try:
                        _, resolved = engine._scan_path(source)
                        # Inode numbers can be recycled after unlink. Even an
                        # identical recorded identity needs unchanged view bytes.
                        if hashlib.sha256(resolved.read_bytes()).hexdigest() == data['sha256']:
                            aliases.add(source)
                    except (ValueError, OSError):
                        # One missing or unreadable alias must not discard other
                        # live aliases recorded beside the same archived view.
                        continue
            except (ValueError, OSError, UnicodeError, KeyError, TypeError) as exc:
                warnings.append({'source': relative, 'reason': 'receipt view provenance unreadable: ' + str(exc)})
    return aliases


def receipt_day(created_at, zone=None):
    """Local calendar day (YYYY-MM-DD) that names daily/v3 for this stamp; None for an unreadable stamp.

    Stamps stay UTC in receipts and the database; only the human-read file name follows the local day,
    the same day beyin_v3_bridge calls "today" (#149).
    """
    instant = _receipt_instant(created_at)
    if instant is None:
        return None
    try:
        return instant.astimezone(zone).date().isoformat()
    except (ValueError, OverflowError, OSError):  # Windows localtime rejects some extreme instants
        return None


def _receipt_view_contents(engine, db, warnings=None, *, include_human_daily=False):
    """Render receipts without side effects; include_human_daily is for comparison only."""
    _hash, _, render = engine.projection_helpers()
    from beyin_v3_preferences import read_daily_layout
    layout = read_daily_layout(engine.root)
    tracked = {row[0] for row in receipt_view_rows(db)}
    migration = engine.state/'v2-migration.json'
    previous = json.loads(migration.read_text(encoding='utf-8')) if migration.exists() else {}
    historical = set(previous.get('historical_receipts', []))
    zone = _local_zone()
    grouped = defaultdict(list)
    for row in db.execute('SELECT payload FROM receipts ORDER BY id'):
        event = json.loads(row[0])
        source = 'receipts/' + _hash(event['event_id']) + '.md'
        if source in historical or not event.get('created_at'):
            continue
        day = receipt_day(event['created_at'], zone)
        if day is None:
            # Never invent a day from the raw text (2026-13-45); the receipt stays in recap's undated count.
            if warnings is not None:
                warnings.append({'source': source, 'reason': 'unreadable receipt created_at; omitted from daily/v3'})
            continue
        grouped[day].append((_receipt_instant(event['created_at']), event['created_at'], source, event['summary']))
    desired = {}
    outcomes = []
    for day, items in sorted(grouped.items()):
        entries = []
        for _, at, source, summary in sorted(items):
            entries.append(f'## {at}\n\n{summary}\n\nSource: [[{source}]]\n')
            outcomes.append(f'- {day}: {summary}\n  Source: [[{source}]]\n')
        relative = f'daily/{day}.md' if layout == 'flat' else f'daily/v3/{day}.md'
        # Omit human notes before sync claims desired paths as view provenance.
        # Outcomes were collected above and remain available in the shared index.
        if include_human_daily or layout != 'flat' or not _human_daily(engine, relative, tracked, warnings):
            desired[relative] = render({'generated': True, 'kind': 'receipt-index'}, '# Recorded outcomes\n\nAgent-authored claims, not independently verified facts.\n\n'+'\n'.join(entries))
    if outcomes:
        desired['knowledge/v3/outcomes.md'] = render({'generated': True, 'kind': 'receipt-index'}, '# Outcome source index\n\nThis index links semantic receipts. It is not an automatic knowledge compiler.\n\n'+''.join(outcomes))
    return desired


def _human_daily(engine, relative, tracked, warnings=None):
    """Unowned flat notes without receipt-index metadata win, including during recovery."""
    if not relative.startswith('daily/') or relative.startswith('daily/v3/'):
        return False
    try:
        # Path proof must happen even for tracked paths. A symlink or an
        # unreadable human daily is a skipped source, never a failed vault sync.
        path = engine._path(relative)
        if relative in tracked or not path.exists():
            return False
        from beyin_v3_sync import parse
        try:
            metadata = parse(path.read_text(encoding='utf-8'))[0]
            return metadata.get('generated') is not True or metadata.get('kind') != 'receipt-index'
        except (ValueError, UnicodeError):
            return True
    except (ValueError, OSError) as exc:
        issue = {'source': relative, 'reason': str(exc)}
        if warnings is not None and issue not in warnings:
            warnings.append(issue)
        return True


def _durable_receipt_quarantine(engine, db, sources, warnings):
    """Keep per-event exclusion evidence beside immutable receipts, across state loss.

    An event id is immutable. A marker hides only that id, not every future
    receipt referring to a human daily that reuses the former view's path.
    Independent, deterministic markers merge across machines without a shared
    read/modify/write manifest. They never authorize writes to a view or note.
    """
    _hash, atomic, _ = engine.projection_helpers()
    try:
        directory = engine._path('receipts')
        names = set(os.listdir(directory)) if directory.exists() else set()
    except (ValueError, OSError) as exc:
        warnings.append({'source': 'receipts', 'reason': 'receipt quarantine provenance unavailable: ' + str(exc)})
        return False
    withheld = _withheld_view_receipts(db)
    durable = True
    checked_marker = False
    for ident, payload in db.execute('SELECT id,payload FROM receipts').fetchall():
        refs = json.loads(payload).get('refs', [])
        proven = isinstance(refs, list) and any(isinstance(ref, str) and
                     (ref in sources or is_receipt_view_source(ref)) for ref in refs)
        digest = _hash(ident)
        name = digest + RECEIPT_VIEW_RECEIPT_SUFFIX
        if not proven and ident not in withheld and name not in names:
            continue
        # An unreadable marker remains fail-closed for this id alone. Its
        # warning also prevents destroying the remaining live view evidence.
        db.execute('INSERT OR IGNORE INTO receipt_view_receipts VALUES (?)', (ident,))
        relative = 'receipts/' + name
        expected = {'schema': 1, 'kind': 'receipt-view-quarantine', 'event_id_sha256': digest}
        try:
            path = engine._path(relative)
            if name in names or path.exists():
                with path.open('rb') as handle:
                    raw = handle.read(4097)
                data = json.loads(raw) if len(raw) <= 4096 else None
                if data != expected or type(data.get('schema')) is not int:
                    raise ValueError('invalid receipt quarantine marker')
            else:
                atomic(path, json.dumps(expected, ensure_ascii=True, sort_keys=True) + '\n')
            checked_marker = True
        except (ValueError, OSError, UnicodeError) as exc:
            warnings.append({'source': relative, 'reason': 'receipt quarantine provenance unavailable: ' + str(exc)})
            durable = False
    # atomic's best-effort directory barrier is insufficient before a view
    # can be unlinked. Batch the required barriers after all markers, including
    # receipts/ itself if created here. Windows has no directory fsync API.
    if checked_marker and os.name != 'nt':
        try:
            for parent in (directory, engine.root):
                descriptor = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            warnings.append({'source': 'receipts', 'reason': 'receipt quarantine provenance not durable: ' + str(exc)})
            durable = False
    return durable


def quarantine_receipt_view_history(engine, db, sources, warnings):
    """Withhold snapshots through this sync for proven views, without rewriting audit bytes.

    A per-source event cutoff survives view retirement, but does not ban a future
    ordinary note reusing that source or id after projection provenance is gone.
    """
    # Receipt summaries that referred to a claimed view must not become public
    # merely because that view is retired or an ordinary note reuses its path.
    # Canonical view paths are also recognizable from immutable receipt refs
    # after a database reset, when prior tracking rows no longer exist. Flat
    # paths require sources evidence; their names alone also fit human notes.
    db.execute('CREATE TABLE IF NOT EXISTS receipt_view_receipts(id TEXT PRIMARY KEY)')
    durable = _durable_receipt_quarantine(engine, db, sources, warnings)
    if not sources:
        return durable
    db.execute('CREATE TABLE IF NOT EXISTS receipt_view_history('
               'source TEXT PRIMARY KEY, through_sequence INTEGER NOT NULL)')
    sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) FROM events').fetchone()[0]
    db.executemany('INSERT INTO receipt_view_history VALUES (?,?) '
                   'ON CONFLICT(source) DO UPDATE SET through_sequence=excluded.through_sequence '
                   'WHERE receipt_view_history.through_sequence < excluded.through_sequence',
                   [(source, sequence) for source in sorted(sources)])
    return durable


def _adopt_stale_receipt_views(engine, db, desired, _hash):
    """Recover stale ownership without calling possibly edited bytes engine-authored."""
    discovered = dict(getattr(engine, 'discovered_receipt_views', {}))
    tracked = {row[0] for row in receipt_view_rows(db)}
    # A state built by the original flat-layout code may already have saved
    # its orphan as an alias. Such paths bypass _scan's metadata parsing; prove
    # the header here as well, without treating all alias paths as owned views.
    for relative in getattr(engine, 'receipt_view_sources', ()):
        if (relative in desired or relative in tracked or relative in discovered or
                not is_receipt_view_source(relative, proven=True)):
            continue
        try:
            from beyin_v3_sync import parse
            raw = engine._path(relative, existing=True).read_bytes()
            metadata = parse(raw.decode('utf-8'))[0]
            if metadata.get('generated') is True and metadata.get('kind') == 'receipt-index':
                discovered[relative] = _hash(raw)
        except (ValueError, OSError, UnicodeError):
            continue  # the scan already reported unsafe paths; no new ownership
    comparison_views = None
    for relative, observed_hash in discovered.items():
        if relative in desired:
            continue
        # Metadata proves an orphan belongs to receipt projections; it does
        # not prove its present bytes were never edited. Only an exact match
        # to the opposite layout's deterministic output permits clean removal.
        alternate = ('daily/' + relative[len('daily/v3/'):] if relative.startswith('daily/v3/')
                     else 'daily/v3/' + relative[len('daily/'):] if relative.startswith('daily/') else None)
        expected = desired.get(alternate)
        if expected is None and alternate is not None:
            # A human-owned target is absent from the write plan, but its
            # deterministic receipt bytes can still prove the old view unchanged.
            if comparison_views is None:
                comparison_views = _receipt_view_contents(engine, db, include_human_daily=True)
            expected = comparison_views.get(alternate)
        owned_hash = observed_hash if expected is not None and observed_hash == _hash(expected) else ''
        db.execute(f'INSERT OR IGNORE INTO {_receipt_view_table(relative)} VALUES (?,?)', (relative, owned_hash))


def project_receipts(engine, db, warnings=None, recover_views=False, recovered=None, desired=None,
                     allow_view_writes=True):
    _hash, atomic, _ = engine.projection_helpers()
    _record_excluded_dirs(engine, db)
    _prepare_receipt_views(db)
    if not allow_view_writes:
        # Indexing and context gates can still commit. Keep the last live
        # provenance until the marker can be saved, then retry on the next sync.
        refresh_gaps(engine, db)
        return []
    if desired is None:
        desired = _receipt_view_contents(engine, db, warnings)
    _adopt_stale_receipt_views(engine, db, desired, _hash)
    conflicts = []
    tracked_views = dict(receipt_view_rows(db))
    for relative, content in desired.items():
        # A note may have appeared since the desired view plan was built.
        if _human_daily(engine, relative, tracked_views, warnings):
            continue
        path = engine._path(relative)
        old = _hash(path.read_bytes()) if path.exists() else None
        desired_hash = _hash(content)
        tracked = tracked_views.get(relative)
        if old != desired_hash and old is not None and (tracked is None or old != tracked):
            if recover_views:
                try:
                    archive_path = _archive_receipt_view(engine, path, old, _hash)
                    atomic(path, content)
                except (OSError, ValueError):
                    conflicts.append({'source': relative, 'reason': 'receipt view recovery failed; preserved'})
                    continue
                db.execute(f'INSERT OR REPLACE INTO {_receipt_view_table(relative)} VALUES (?,?)', (relative, desired_hash))
                if recovered is not None:
                    recovered.append({'source': relative, 'archive': archive_path.relative_to(engine.root).as_posix(),
                                      'status': 'recovered'})
                continue
            reason = 'manual receipt view edit preserved' if tracked is not None else 'untracked receipt view preserved'
            conflicts.append({'source': relative, 'reason': reason})
            continue
        if old != desired_hash:
            # Recheck immediately before atomic replacement; remote writers still require reconciliation.
            if (_hash(path.read_bytes()) if path.exists() else None) != old:
                conflicts.append({'source': relative, 'reason': 'receipt view changed during projection'})
                continue
            atomic(path, content)
        if tracked != desired_hash:
            db.execute(f'INSERT OR REPLACE INTO {_receipt_view_table(relative)} VALUES (?,?)', (relative, desired_hash))
    conflicts.extend(_retire_stale_views(engine, db, desired, _hash, recover_views=recover_views, recovered=recovered))
    refresh_gaps(engine, db)
    return conflicts


def _retire_stale_views(engine, db, desired, _hash, recover_views=False, recovered=None):
    """Remove views this state wrote that are no longer projected, e.g. a UTC-named daily/v3 file after #149.

    Only a file whose bytes still match the hash recorded when it was written is removed; an edited
    file is never touched and stays a visible conflict until the person moves or deletes it.
    """
    conflicts = []
    for relative, tracked in receipt_view_rows(db):
        if relative in desired:
            continue
        if not is_receipt_view_source(relative, proven=True):
            conflicts.append({'source': relative, 'reason': 'unexpected receipt view path; preserved'})
            continue
        try:
            path = engine._path(relative)
            current = _hash(path.read_bytes()) if path.is_file() else None
        except (ValueError, OSError):
            conflicts.append({'source': relative, 'reason': 'stale receipt view unreadable; preserved'})
            continue
        if current is None and not path.exists():
            db.execute(f'DELETE FROM {_receipt_view_table(relative)} WHERE path=?', (relative,))
            continue
        if current != tracked:
            if recover_views:
                try:
                    archive_path = _archive_receipt_view(engine, path, current, _hash)
                    path.unlink()
                except (OSError, ValueError):
                    conflicts.append({'source': relative, 'reason': 'receipt view recovery failed; preserved'})
                    continue
                db.execute(f'DELETE FROM {_receipt_view_table(relative)} WHERE path=?', (relative,))
                if recovered is not None:
                    recovered.append({'source': relative, 'archive': archive_path.relative_to(engine.root).as_posix(),
                                      'status': 'archived_and_retired'})
                continue
            conflicts.append({'source': relative, 'reason': 'manual receipt view edit preserved; view no longer generated'})
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            conflicts.append({'source': relative, 'reason': 'stale receipt view could not be removed'})
            continue
        db.execute(f'DELETE FROM {_receipt_view_table(relative)} WHERE path=?', (relative,))
    return conflicts


# YAML lines (updated: 2026-09-25) and one-line JSON frontmatter ("updated": "2026-09-25").
_FRONTMATTER_KEY = r'(?:^|[\s{,])["\']?%s["\']?\s*[:=]\s*'
_FRONTMATTER_GENERATED = re.compile(_FRONTMATTER_KEY % 'generated' + r'true\b', re.M)
_FRONTMATTER_UPDATED = re.compile(_FRONTMATTER_KEY % '(?:updated|modified)' +
                                  r'["\']?([0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?)', re.M)


# Agent concepts live in knowledge/; the official template keeps human-curated notes in 500-Knowledge/.
# The Stop hook imports these, so its reminder and doctor count the same notes.
DISTILLATION_ROOTS = ('knowledge/', '🧠 500-Knowledge/', '500-Knowledge/')
# Generated views and the V2 compiler seeds change without any agent distilling.
NOT_DISTILLATION = ('knowledge/v3/', 'knowledge/index.md', 'knowledge/log.md')


def is_distilled_note(relative):
    relative = relative.replace('\\', '/')
    return (relative.startswith(DISTILLATION_ROOTS) and relative.endswith('.md') and
            not any(relative == item or relative.startswith(item) for item in NOT_DISTILLATION))


def knowledge_freshness(vault, db, now=None):
    """Diagnose knowledge distillation recency and count receipts since then."""
    if now is None:
        now = time.time()
    vault = Path(vault)
    latest_mtime = None
    latest_source = None

    for root in DISTILLATION_ROOTS:
        knowledge_dir = vault / root
        if not knowledge_dir.is_dir():
            continue
        for path in sorted(knowledge_dir.rglob('*.md')):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.relative_to(vault).as_posix()
            except ValueError:
                continue
            if not is_distilled_note(relative):
                continue
            try:
                mtime = path.stat().st_mtime
                with path.open('rb') as handle:  # frontmatter only; a whole (possibly evicted) note is never read
                    head = handle.read(4096).decode('utf-8', errors='ignore')
            except OSError:
                continue
            end_idx = head.find('\n---', 3) if head.startswith('---') else -1
            frontmatter = head[3:end_idx] if end_idx != -1 else ''
            if _FRONTMATTER_GENERATED.search(frontmatter):
                continue
            # git checkout, clone and iCloud restore reset mtime; a recorded updated/modified date wins.
            m = _FRONTMATTER_UPDATED.search(frontmatter)
            # A bare date keeps the same-day mtime, so receipts earlier that day do not count as later.
            if m and not (len(m.group(1)) == 10 and datetime.fromtimestamp(mtime).date().isoformat() == m.group(1)):
                try:
                    mtime = datetime.fromisoformat(m.group(1).replace(' ', 'T')).timestamp()
                except (ValueError, OverflowError, OSError):
                    pass
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime
                latest_source = relative

    total_receipts = 0
    receipts_since = 0
    # A store error propagates: doctor reports "unavailable" instead of a silent zero.
    for (payload,) in db.execute('SELECT payload FROM receipts'):
        try:
            receipt = json.loads(payload)
            created = datetime.fromisoformat(receipt['created_at']).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        total_receipts += 1
        if latest_mtime is None or created >= latest_mtime:
            receipts_since += 1

    days_ago = max(0, int((now - latest_mtime) / 86400)) if latest_mtime is not None else None

    return {
        'last_distilled_at': latest_mtime,
        'days_ago': days_ago,
        'receipts_since': receipts_since,
        'total_receipts': total_receipts,
        'latest_source': latest_source,
    }


def _fold_tr(text):
    return (text.replace('\u0130', 'i').replace('I', 'i').lower().replace('\u0307', '')
            .translate(str.maketrans('ıüşğöç', 'iusgoc')))


# Only the V2 wording that hands knowledge/ to the compiler. Bare "compiler",
# "derleyici" or "dokunma" also matched V3 rules such as "notlara dokunmadan önce".
_V2_COMPILER_RULE = re.compile(r'derleyici\s+(?:yonetir|yazar|gunceller)|elle\s+duzenlemeyin|'
                               r'managed\s+by\s+the\s+compiler|compiler[- ]managed')


def check_instruction_conflicts(vault):
    """Detect contradictory V2 compiler instructions outside the managed V3 block."""
    vault = Path(vault)
    conflicts = []
    START = "<!-- beyin-v3:start -->"
    END = "<!-- beyin-v3:end -->"
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = vault / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            outside = re.sub(r"\n*" + re.escape(START) + r".*?" + re.escape(END), "", text, flags=re.S)
            for line in outside.splitlines():
                line_str = line.strip()
                folded = _fold_tr(line_str)
                if 'knowledge' in folded and _V2_COMPILER_RULE.search(folded):
                    conflicts.append({
                        'file': name,
                        'snippet': line_str[:120],
                        'reason': 'legacy_v2_compiler_instruction'
                    })
                    break
        except Exception:
            pass
    return conflicts
