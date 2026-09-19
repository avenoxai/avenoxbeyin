"""Deterministic receipt indexes; never rewrite existing human daily/knowledge."""
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path


def _checkpoint_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, boundary_kind TEXT NOT NULL DEFAULT "legacy_unknown", PRIMARY KEY(harness,session))')
    if 'turn_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN turn_at REAL DEFAULT 0')
    if 'boundary_kind' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN boundary_kind TEXT NOT NULL DEFAULT "legacy_unknown"')
    db.execute('CREATE TABLE IF NOT EXISTS receipt_reviews(target_harness TEXT, target_session TEXT, turn_at REAL, checkpoint_at REAL, payload TEXT NOT NULL, PRIMARY KEY(target_harness,target_session,turn_at,checkpoint_at))')
    db.execute('CREATE TABLE IF NOT EXISTS receipt_checkpoint_archive(harness TEXT, session TEXT, at REAL, turn_at REAL, boundary_kind TEXT, archived_at TEXT, PRIMARY KEY(harness,session,at,turn_at))')


def _record(db, event):
    harness, session, at = event.get('harness'), event.get('session'), event.get('at')
    if (not harness or not session or isinstance(at, bool) or not isinstance(at, (int, float)) or
            not math.isfinite(at) or event.get('no_memory')):
        return
    kind = event.get('event')
    boundary = kind == 'UserPromptSubmit' or (harness == 'antigravity' and kind == 'SessionStart')
    if boundary:
        boundary_kind = 'prompt' if kind == 'UserPromptSubmit' else 'session_only'
        db.execute('INSERT INTO receipt_checkpoints(harness,session,at,turn_at,boundary_kind) VALUES (?,?,0,?,?) ON CONFLICT(harness,session) DO UPDATE SET turn_at=MAX(turn_at,excluded.turn_at), boundary_kind=CASE WHEN excluded.turn_at>=turn_at THEN excluded.boundary_kind ELSE boundary_kind END', (harness, session, at, boundary_kind))
    elif kind in ('Stop', 'SessionEnd'):
        db.execute('UPDATE receipt_checkpoints SET at=CASE WHEN at<turn_at THEN ? ELSE MIN(at,?) END WHERE harness=? AND session=? AND turn_at>0 AND ?>=turn_at', (at, at, harness, session, at))


def _repair_legacy(engine, db):
    legacy_rows = {(row[0], row[1]): row for row in db.execute('SELECT harness,session,at,turn_at,boundary_kind FROM receipt_checkpoints WHERE boundary_kind="legacy_unknown"')}
    legacy = set(legacy_rows)
    if not legacy:
        return
    events = []
    for directory in ('hook-done', 'hook-queue'):
        for path in (engine.state/directory).glob('*.json'):
            try:
                event = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if (event.get('harness'), event.get('session')) in legacy:
                events.append(event)
    recovered = set()
    for key, row in legacy_rows.items():
        retained = [event for event in events if (event.get('harness'), event.get('session')) == key and
                    not event.get('no_memory') and not isinstance(event.get('at'), bool) and
                    isinstance(event.get('at'), (int, float)) and math.isfinite(event['at'])]
        original_boundary = any(event.get('event') in ('UserPromptSubmit', 'SessionStart') and
                                event.get('at') == row[3] for event in retained)
        original_terminal = any(event.get('event') in ('Stop', 'SessionEnd') and
                                event.get('at') == row[2] for event in retained)
        has_usable_boundary = any(event.get('event') == 'UserPromptSubmit' or
                                  (event.get('harness') == 'antigravity' and event.get('event') == 'SessionStart')
                                  for event in retained)
        if original_boundary and original_terminal and has_usable_boundary:
            recovered.add(key)
    for key in recovered:
        row = legacy_rows[key]
        db.execute('INSERT OR IGNORE INTO receipt_checkpoint_archive VALUES (?,?,?,?,?,?)',
                   tuple(row) + (datetime.now().isoformat(),))
        db.execute('DELETE FROM receipt_checkpoints WHERE harness=? AND session=?', key)
    for event in sorted(events, key=lambda item: item.get('at', 0)):
        if (event.get('harness'), event.get('session')) in recovered:
            _record(db, event)


def record_checkpoints(engine, events):
    with engine.store._connect() as db:
        _checkpoint_schema(db)
        for event in sorted(events, key=lambda item: item.get('at', 0)):
            _record(db, event)


def refresh_gaps(engine, db):
    atomic = engine.projection_helpers()[1]
    _checkpoint_schema(db)
    _repair_legacy(engine, db)
    receipts = [json.loads(row[0]) for row in db.execute('SELECT payload FROM receipts')]
    reviews = [json.loads(row[0]) for row in db.execute('SELECT payload FROM receipt_reviews')]
    gaps = []
    reviewed = [{'harness': review['target_harness'], 'session': review['target_session'],
                 'turn_at': review['turn_at'], 'checkpoint_at': review['checkpoint_at'],
                 'disposition': review['disposition'], 'review_source': review['source']}
                for review in reviews]
    for row in db.execute('SELECT harness,session,at,turn_at,boundary_kind FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        threshold = row[3] or row[2]
        matched = any(r.get('harness') == row[0] and r.get('session') == row[1] and
                      datetime.fromisoformat(r.get('created_at', '1970-01-01T00:00:00+00:00')).timestamp() >= threshold for r in receipts)
        review = next((r for r in reviews if r['target_harness'] == row[0] and r['target_session'] == row[1] and r['turn_at'] == row[3] and r['checkpoint_at'] == row[2]), None)
        item = {'harness': row[0], 'session': row[1], 'checkpoint_at': row[2], 'turn_at': row[3], 'scope': row[4]}
        if not review and not matched:
            gaps.append(item)
    atomic(engine.state/'receipt-gaps.json', json.dumps({'potential_missing_receipts': len(gaps), 'checkpoints': gaps, 'reviewed_checkpoints': len(reviewed), 'reviewed': reviewed, 'scope_limits': {'antigravity': 'session_only; later per-turn boundaries unsupported', 'legacy_unknown': 'prompt provenance could not be reconstructed; review explicitly'}, 'meaning': 'Unreviewed checkpoint without a matching structured receipt; may be trivial or deliberately omitted. No summary inferred.'}))


def project_receipts(engine, db):
    _hash, atomic, render = engine.projection_helpers()
    db.execute('CREATE TABLE IF NOT EXISTS receipt_views(path TEXT PRIMARY KEY, hash TEXT NOT NULL)')
    migration = engine.state/'v2-migration.json'
    previous = json.loads(migration.read_text(encoding='utf-8')) if migration.exists() else {}
    historical = set(previous.get('historical_receipts', []))
    grouped = defaultdict(list)
    for row in db.execute('SELECT payload FROM receipts ORDER BY id'):
        event = json.loads(row[0])
        source = 'receipts/' + _hash(event['event_id']) + '.md'
        if source in historical or not event.get('created_at'):
            continue
        date = event['created_at'][:10]
        grouped[date].append((event['created_at'], source, event['summary']))
    desired = {}
    outcomes = []
    for day, items in sorted(grouped.items()):
        entries = []
        for at, source, summary in sorted(items):
            entries.append(f'## {at}\n\n{summary}\n\nSource: [[{source}]]\n')
            outcomes.append(f'- {day}: {summary}\n  Source: [[{source}]]\n')
        desired[f'daily/v3/{day}.md'] = render({'generated': True, 'kind': 'receipt-index'}, '# Recorded outcomes\n\nAgent-authored claims, not independently verified facts.\n\n'+'\n'.join(entries))
    if outcomes:
        desired['knowledge/v3/outcomes.md'] = render({'generated': True, 'kind': 'receipt-index'}, '# Outcome source index\n\nThis index links semantic receipts. It is not an automatic knowledge compiler.\n\n'+''.join(outcomes))
    conflicts = []
    for relative, content in desired.items():
        path = engine._path(relative)
        old = _hash(path.read_bytes()) if path.exists() else None
        desired_hash = _hash(content)
        tracked = db.execute('SELECT hash FROM receipt_views WHERE path=?', (relative,)).fetchone()
        if old != desired_hash and old is not None and (not tracked or old != tracked[0]):
            conflicts.append({'source': relative, 'reason': 'manual receipt view edit preserved'})
            continue
        if old != desired_hash:
            # Recheck immediately before atomic replacement; remote writers still require reconciliation.
            if (_hash(path.read_bytes()) if path.exists() else None) != old:
                conflicts.append({'source': relative, 'reason': 'receipt view changed during projection'})
                continue
            atomic(path, content)
        db.execute('INSERT OR REPLACE INTO receipt_views VALUES (?,?)', (relative, desired_hash))
    refresh_gaps(engine, db)
    return conflicts
