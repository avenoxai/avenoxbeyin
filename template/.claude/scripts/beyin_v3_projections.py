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


def _hidden_ref_sources(db, refs):
    """Indexed sources that default internal context never shows (the _eligible visibility/trust gate)."""
    refs = sorted(set(refs))
    hidden = set()
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


def latest_receipts(db, vault, sessions=None):
    """Receipts an automatic context may show, newest first by created_at, then event_id, as (source, event).

    The order is the receipt's own stamp, never the file mtime that a git pull or a sync
    client rewrites (#112). Only receipts with a readable stamp, a summary and a regular
    source file inside the vault are yielded; with sessions, only those sessions. A receipt
    whose source or any ref is a private or untrusted record is skipped whole: its summary
    describes that record. The SessionStart hook and the global bridge share this gate.
    """
    vault = Path(vault)
    candidates = []
    for (payload,) in db.execute('SELECT payload FROM receipts'):
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or not isinstance(event.get('summary'), str):
            continue
        if sessions is not None and event.get('session') not in sessions:
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


def recent_receipts(db, days=7, limit=20, today=None, vault=None):
    """A bounded, source-linked activity view; summaries remain agent claims.

    With a vault, receipts whose own source file is gone are omitted, and refs that no longer
    exist or that point at private/untrusted records are withheld and only counted.
    """
    if not 1 <= days <= 366 or not 1 <= limit <= 100:
        raise ValueError('recap days must be 1..366 and limit must be 1..100')
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    matches = []
    undated = 0
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
        if not start <= stamp.date() <= today:
            continue
        ident = event.get('event_id')
        if not isinstance(ident, str) or not ident or not isinstance(event.get('summary'), str):
            undated += 1
            continue
        source = 'receipts/' + hashlib.sha256(ident.encode()).hexdigest() + '.md'
        matches.append((stamp, ident, source, event))
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
    shown = [(stamp, source, event, [ref for ref in event.get('refs') or [] if isinstance(ref, str)]
              if isinstance(event.get('refs'), list) else []) for stamp, _, source, event in matches[:limit]]
    hidden = _hidden_ref_sources(db, [ref for *_, refs in shown for ref in refs]) if vault is not None else set()
    items = []
    for stamp, source, event, refs in shown:
        kept, private, missing = [], 0, 0
        for ref in refs:
            if ref in hidden:
                private += 1
            elif vault is not None and not _vault_file(vault, ref):
                missing += 1
            else:
                kept.append(ref)
        item = {'created_at': stamp.isoformat(), 'summary': event['summary'], 'source': source, 'refs': kept}
        if private or missing:
            item['refs_withheld'] = {'private': private, 'missing': missing}
        items.append(item)
    result = {
        'status': 'ok', 'from': start.isoformat(), 'through': today.isoformat(),
        'timezone': 'UTC', 'audience': 'internal', 'total': len(matches), 'shown': len(items),
        'truncated': len(matches) > limit, 'undated_omitted': undated,
        'items': items,
        'meaning': 'Agent-authored outcomes, not independently verified facts.',
    }
    if missing_sources:
        result['missing_source_omitted'] = missing_sources
    return result


def _checkpoint_schema(db):
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
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id,prompt_at,edit_at FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        # Only count sessions that saw a user prompt: UserPromptSubmit, or a SessionStart carrying the first prompt (#78)
        if not row[6]:
            continue
        chk_at = row[2]
        threshold = _coverage_threshold(row[2], row[3], row[7])
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
    for row in db.execute('SELECT harness,session,at,turn_at,project,project_id,edit_at FROM receipt_checkpoints'):
        if not row[2] or row[2] < row[3]:
            continue
        threshold = _coverage_threshold(row[2], row[3], row[6])
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


def project_receipts(engine, db, warnings=None):
    _hash, atomic, render = engine.projection_helpers()
    db.execute('CREATE TABLE IF NOT EXISTS receipt_views(path TEXT PRIMARY KEY, hash TEXT NOT NULL)')
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
    conflicts.extend(_retire_stale_views(engine, db, desired, _hash))
    refresh_gaps(engine, db)
    return conflicts


def _retire_stale_views(engine, db, desired, _hash):
    """Remove views this state wrote that are no longer projected, e.g. a UTC-named daily/v3 file after #149.

    Only a file whose bytes still match the hash recorded when it was written is removed; an edited
    file is never touched and stays a visible conflict until the person moves or deletes it.
    """
    conflicts = []
    for relative, tracked in db.execute('SELECT path, hash FROM receipt_views ORDER BY path').fetchall():
        if relative in desired:
            continue
        try:
            path = engine._path(relative)
            current = _hash(path.read_bytes()) if path.is_file() else None
        except (ValueError, OSError):
            conflicts.append({'source': relative, 'reason': 'stale receipt view unreadable; preserved'})
            continue
        if current is None and not path.exists():
            db.execute('DELETE FROM receipt_views WHERE path=?', (relative,))
            continue
        if current != tracked:
            conflicts.append({'source': relative, 'reason': 'manual receipt view edit preserved; view no longer generated'})
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            conflicts.append({'source': relative, 'reason': 'stale receipt view could not be removed'})
            continue
        db.execute('DELETE FROM receipt_views WHERE path=?', (relative,))
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
