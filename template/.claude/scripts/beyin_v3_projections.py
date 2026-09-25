"""Deterministic receipt indexes; never rewrite existing human daily/knowledge."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
import time


def _checkpoint_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
    if 'turn_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN turn_at REAL DEFAULT 0')
    if 'prompt_at' not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
        db.execute('ALTER TABLE receipt_checkpoints ADD COLUMN prompt_at REAL DEFAULT 0')
    for column in ('project', 'project_id'):
        if column not in {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}:
            db.execute(f'ALTER TABLE receipt_checkpoints ADD COLUMN {column} TEXT')


def record_checkpoints(engine, events):
    with engine.store._connect() as db:
        _checkpoint_schema(db)
        for event in sorted(events, key=lambda item: item.get('at', 0)):
            if not event.get('session') or event.get('no_memory'):
                continue
            values = (event['harness'], event['session'], event['at'])
            if event.get('event') == 'UserPromptSubmit' or (event.get('event') == 'SessionStart' and event.get('prompted')):
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at,turn_at,prompt_at) VALUES (?,?,0,?,?) ON CONFLICT(harness,session) DO UPDATE SET turn_at=MAX(turn_at,excluded.turn_at), prompt_at=MAX(prompt_at,excluded.prompt_at)',
                           (event['harness'], event['session'], event['at'], event['at']))
            elif event.get('event') == 'SessionStart':
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at,turn_at) VALUES (?,?,0,?) ON CONFLICT(harness,session) DO UPDATE SET turn_at=MAX(turn_at,excluded.turn_at)', values)
            elif event.get('event') in ('Stop', 'SessionEnd'):
                db.execute('INSERT INTO receipt_checkpoints(harness,session,at) VALUES (?,?,?) ON CONFLICT(harness,session) DO UPDATE SET at=MAX(at,excluded.at)', values)
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


def receipt_coverage(db, now=None):
    if now is None:
        now = time.time()
    seven_days = now - 7 * 86400
    thirty_days = now - 30 * 86400
    _checkpoint_schema(db)
    latest = _latest_receipts(db)
    windows = {
        'all_time': {'total': 0, 'covered': 0, 'missing': 0},
        'last_7d': {'total': 0, 'covered': 0, 'missing': 0},
        'last_30d': {'total': 0, 'covered': 0, 'missing': 0},
    }
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id,prompt_at FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        # Only count sessions that saw a user prompt: UserPromptSubmit, or a SessionStart carrying the first prompt (#78)
        if not row[6]:
            continue
        chk_at = row[2]
        threshold = row[3] or row[2]
        matched = latest.get((row[0], row[1]), float('-inf')) >= threshold
        for w_name, w_active in (('all_time', True), ('last_30d', chk_at >= thirty_days), ('last_7d', chk_at >= seven_days)):
            if w_active:
                windows[w_name]['total'] += 1
                if matched:
                    windows[w_name]['covered'] += 1
                else:
                    windows[w_name]['missing'] += 1

    return {
        'ratio': round(windows['all_time']['covered'] / windows['all_time']['total'], 3) if windows['all_time']['total'] else None,
        'covered': windows['all_time']['covered'],
        'total': windows['all_time']['total'],
        'missing': windows['all_time']['missing'],
        'last_7d': {
            'ratio': round(windows['last_7d']['covered'] / windows['last_7d']['total'], 3) if windows['last_7d']['total'] else None,
            'covered': windows['last_7d']['covered'],
            'total': windows['last_7d']['total'],
            'missing': windows['last_7d']['missing'],
        },
        'last_30d': {
            'ratio': round(windows['last_30d']['covered'] / windows['last_30d']['total'], 3) if windows['last_30d']['total'] else None,
            'covered': windows['last_30d']['covered'],
            'total': windows['last_30d']['total'],
            'missing': windows['last_30d']['missing'],
        }
    }


def refresh_gaps(engine, db):
    atomic = engine.projection_helpers()[1]
    _checkpoint_schema(db)
    latest = _latest_receipts(db)
    gaps = []
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        threshold = row[3] or row[2]
        matched = latest.get((row[0], row[1]), float('-inf')) >= threshold
        if not matched:
            gaps.append({'harness': row[0], 'session': row[1], 'checkpoint_at': row[2], 'turn_at': row[3], 'scope': 'session_only' if row[0] == 'antigravity' else 'turn' if row[3] else 'terminal_only'})
            if row[4] and row[5]:
                gaps[-1].update(project=row[4], project_id=row[5])
    coverage = receipt_coverage(db, now=time.time())
    atomic(engine.state/'receipt-gaps.json', json.dumps({
        'potential_missing_receipts': len(gaps),
        'receipt_coverage': coverage,
        'checkpoints': gaps,
        'scope_limits': {'antigravity': 'session_only; later per-turn boundaries unsupported', 'missing_prompt_event': 'terminal_only; receipt attribution may be incomplete'},
        'meaning': 'Checkpoint without a matching structured receipt; may be trivial or deliberately omitted. No summary inferred.'
    }))


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


# YAML lines (updated: 2026-09-25) and one-line JSON frontmatter ("updated": "2026-09-25").
_FRONTMATTER_KEY = r'(?:^|[\s{,])["\']?%s["\']?\s*[:=]\s*'
_FRONTMATTER_GENERATED = re.compile(_FRONTMATTER_KEY % 'generated' + r'true\b', re.M)
_FRONTMATTER_UPDATED = re.compile(_FRONTMATTER_KEY % '(?:updated|modified)' +
                                  r'["\']?([0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?)', re.M)


def knowledge_freshness(vault, db, now=None):
    """Diagnose knowledge distillation recency and count receipts since then."""
    if now is None:
        now = time.time()
    vault = Path(vault)
    knowledge_dir = vault / 'knowledge'
    latest_mtime = None
    latest_source = None

    if knowledge_dir.is_dir():
        for path in sorted(knowledge_dir.rglob('*.md')):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.relative_to(vault).as_posix()
            except ValueError:
                continue
            # Generated views and the V2 compiler seeds are not distillation.
            if relative.startswith('knowledge/v3/') or relative in ('knowledge/index.md', 'knowledge/log.md'):
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

