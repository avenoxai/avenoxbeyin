"""Session-scoped context deduplication (#363): prevent re-injecting already delivered records.

Each session tracks delivered records under `<state>/session-delivery/`, named by the hash
of harness and session id. The file holds only `{version, harness, at, records: {id: {sha, windows}}, receipt}`;
the raw session id is not stored. A record is a duplicate only if its id, its source_sha256 and its
text window match a delivery earlier in the active session. If the source file was edited on disk, its
sha256 changes and the note is delivered again.

The latest receipt block that a turn appends after its records is a session fact as well: `receipt`
is the sha256 of the receipt text last delivered, so the same receipt is said once per session and
again only when a new receipt (or an edited one) changes that text, or after the ledger is cleared.
A release without this key ignores it, so a rollback reads the ledger as before.

Turn context is passage based (#83): one long note answers different prompts with different windows
under the same id and sha. A window is keyed by the hash of its first WINDOW_PREFIX characters, which
budget clipping never touches (the smallest excerpt keeps 150), so another section of a note already
seen is still delivered while the same window, clipped or not, is held back.
The ledger keeps at most MAX_RECORDS ids and MAX_WINDOWS windows per id (oldest dropped first), so it
never outgrows MAX_FILE_BYTES and silently resets; session files older than MAX_AGE_SECONDS are pruned
on write.

The toggle lives in `<state>/context-dedupe.json` (default off), never in `.beyin-preferences.json`:
an older release rejects unknown preference keys, so a rollback would break its doctor and hooks.
Every function here fails open: any error in reading or writing the ledger returns all records as novel
and the receipt as new.
The session ledger is cleared on compaction (PreCompact or SessionStart with source: compact) and on SessionEnd.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

SETTINGS_FILE = 'context-dedupe.json'
DELIVERY_DIR = 'session-delivery'
MAX_SESSIONS = 128
MAX_AGE_SECONDS = 24 * 3600
MAX_FILE_BYTES = 262144
MAX_RECORDS = 512    # ~140 bytes per id with one window: far below MAX_FILE_BYTES
MAX_WINDOWS = 8      # windows remembered per id
WINDOW_PREFIX = 150  # beyin_v3.MIN_CONTEXT_TEXT: clipping keeps at least this much of a window


def _session_file(state, harness, session, create=False):
    if not isinstance(session, str) or not session or session == 'unknown' or len(session) > 512:
        return None
    folder = Path(state) / DELIVERY_DIR
    if folder.is_symlink():
        return None
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256((str(harness) + '\0' + session).encode('utf-8')).hexdigest()
    path = folder / (digest + '.json')
    return None if path.is_symlink() else path


def read_settings(state):
    """Return (enabled, valid). Missing file: off and valid. Damaged file: off and invalid."""
    if os.environ.get('BEYIN_V3_CONTEXT_DEDUPE') == '1':
        return True, True
    path = Path(state) / SETTINGS_FILE
    if not path.exists() and not path.is_symlink():
        return False, True
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) - {'schema', 'enabled'} or data.get('schema', 1) != 1 \
                or type(data.get('enabled')) is not bool:
            raise ValueError('shape')
        return data['enabled'], True
    except (ValueError, OSError):
        return False, False


def save_settings(state, enabled):
    """Atomically persist enabled setting."""
    if type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    path = Path(state) / SETTINGS_FILE
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {'schema': 1, 'enabled': enabled}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=folder, delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump(data, handle)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def is_enabled(state):
    """Convenience predicate for hook fastpath."""
    enabled, _ = read_settings(state)
    return enabled


def _window(record):
    text = record.get('text')
    text = text if isinstance(text, str) else ''
    return hashlib.sha256(text[:WINDOW_PREFIX].encode('utf-8')).hexdigest()[:16]


def receipt_key(receipt):
    """Stable key of a rendered receipt block: its text names the receipt source and carries its body."""
    return hashlib.sha256(receipt.encode('utf-8')).hexdigest()


def _read_ledger(path, now=None):
    """Return (records, receipt key or None) of a current ledger; ({}, None) for anything else."""
    try:
        if path is None or not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return {}, None
        now = time.time() if now is None else now
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('version') != 1:
            return {}, None
        at = value.get('at')
        if type(at) not in (int, float) or not 0 <= now - at < MAX_AGE_SECONDS:
            return {}, None
        records = value.get('records')
        if not isinstance(records, dict):
            return {}, None
        receipt = value.get('receipt')
        return ({rid: entry for rid, entry in records.items()
                 if isinstance(entry, dict) and isinstance(entry.get('sha'), str)
                 and isinstance(entry.get('windows'), list)},
                receipt if isinstance(receipt, str) else None)
    except (OSError, ValueError):
        return {}, None


def filter_novel(state, harness, session, records, now=None):
    """Separate records into (novel, duplicates).

    A record is a duplicate only if both its id and its source_sha256 were already recorded
    in this session's delivery ledger. Fail open: on any error, all records are novel.
    """
    if not records:
        return [], []
    if not is_enabled(state):
        return list(records), []
    path = _session_file(state, harness, session)
    if path is None:
        return list(records), []
    delivered, _ = _read_ledger(path, now=now)
    if not delivered:
        return list(records), []
    novel, duplicates = [], []
    for r in records:
        if not isinstance(r, dict):
            novel.append(r)
            continue
        rid = r.get('id')
        sha = r.get('source_sha256')
        entry = delivered.get(rid) if rid and sha else None
        if entry is not None and entry['sha'] == sha and _window(r) in entry['windows']:
            duplicates.append(r)
        else:
            novel.append(r)
    return novel, duplicates


def drop_delivered(state, harness, session, context, now=None):
    """Return (context without held-back records, duplicates).

    The returned context keeps the same type and its unclipped `sources` for the kept records
    (beyin_v3.PackedContext), so render_context repacks them from the originals and hands the
    budget of the held-back records to the new ones instead of re-clipping earlier clips.
    """
    records = list(context.get('records') or [])
    novel, duplicates = filter_novel(state, harness, session, records, now=now)
    if not duplicates:
        return context, []
    kept = type(context)(context)
    kept['records'] = novel
    sources = getattr(context, 'sources', ())
    if len(sources) == len(records):
        held = {id(r) for r in duplicates}
        kept.sources = tuple(s for s, r in zip(sources, records) if id(r) not in held)
    ids = {r['id'] for r in novel}
    if hasattr(context, 'wider'):
        kept.wider = {key: r for key, r in context.wider.items() if key in ids}
        kept.extensions = {key: r for key, r in getattr(context, 'extensions', {}).items() if key in ids}
    if hasattr(context, 'evidence'):
        kept.evidence = {key: ref for key, ref in context.evidence.items() if key in ids}
    return kept, duplicates


def receipt_delivered(state, harness, session, receipt, now=None):
    """True only if this exact receipt text was already delivered in the session. Fail open: False."""
    if not isinstance(receipt, str) or not receipt or not is_enabled(state):
        return False
    path = _session_file(state, harness, session)
    if path is None:
        return False
    return _read_ledger(path, now=now)[1] == receipt_key(receipt)


def record_delivered(state, harness, session, records, now=None, receipt=None):
    """Update the session delivery ledger with newly delivered records and, when the turn
    showed it, the receipt text. Fail open."""
    receipt = receipt_key(receipt) if isinstance(receipt, str) and receipt else None
    if not records and receipt is None:
        return
    if not is_enabled(state):
        return
    now = time.time() if now is None else now
    path = _session_file(state, harness, session, create=True)
    if path is None:
        return
    existing, delivered_receipt = _read_ledger(path, now=now)
    changed = receipt is not None and receipt != delivered_receipt
    receipt = receipt or delivered_receipt
    for r in records:
        if not (isinstance(r, dict) and r.get('id') and isinstance(r.get('source_sha256'), str)):
            continue
        before = existing.pop(r['id'], None)  # re-insert: the dict order is the drop order
        windows = before['windows'] if before is not None and before['sha'] == r['source_sha256'] else []
        window = _window(r)
        windows = ([w for w in windows if w != window] + [window])[-MAX_WINDOWS:]
        existing[r['id']] = {'sha': r['source_sha256'], 'windows': windows}
        changed = True
    if not changed:
        return
    for rid in list(existing)[:max(0, len(existing) - MAX_RECORDS)]:
        del existing[rid]
    payload = {
        'version': 1,
        'harness': harness,
        'at': now,
        'records': existing,
    }
    if receipt is not None:
        payload['receipt'] = receipt
    temporary = None
    try:
        parent = path.parent
        files = []
        for candidate in parent.glob('*.json'):
            if candidate.is_symlink() or candidate == path:
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue  # another hook removed it meanwhile
            if now - mtime >= MAX_AGE_SECONDS:
                candidate.unlink(missing_ok=True)
            else:
                files.append((mtime, candidate))
        files.sort(key=lambda item: item[0], reverse=True)
        for _, candidate in files[MAX_SESSIONS - 1:]:
            candidate.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=parent, delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump(payload, handle)
            handle.write('\n')
        os.replace(temporary, path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def reset_session(state, harness, session):
    """Remove the delivery ledger for a session (e.g. on compaction or session end)."""
    try:
        path = _session_file(state, harness, session)
        if path is not None and path.is_file():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def doctor(state):
    """Read-only: status of the dedupe toggle and number of cached session delivery ledgers."""
    value, valid = read_settings(state)
    result = {'enabled': value, 'valid': valid, 'sessions': 0}
    folder = Path(state) / DELIVERY_DIR
    if folder.is_dir() and not folder.is_symlink():
        try:
            with os.scandir(folder) as listing:
                result['sessions'] = sum(1 for e in listing if e.name.endswith('.json') and not e.is_symlink())
        except OSError:
            pass
    return result
