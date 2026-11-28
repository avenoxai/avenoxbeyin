"""Read-only Codex session discovery for #875. No CLI, hook or persistence entry point.

WAL readers take shared-memory read locks in the existing -shm file and may
update its read marks. They never write database pages or WAL contents. Live
writer changes mean before/after byte equality cannot verify this. Each
database gets its own read snapshot. The reader requires existing WAL sidecars
and rejects results if new regular files are observed directly in the
directory containing the databases. If the writer removes sidecars during
open, SQLite may recreate them before that check.
"""
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from beyin_v3_chat_backfill import (
    ASSISTANT_CHARS, LIST_DEFAULT, LIST_MAX, MAX_FILES, MAX_LINE, MAX_SCAN_BYTES,
    OPT_OUT, OPT_OUT_BYTES, PAGE_CHARS, PAGE_ITEM, SESSION, USER_CHARS,
    _day, _filtered, _key, _vault_keys, session_hash,
)
from beyin_v3_hook import HARNESS_SYNTHETIC_PROMPT_PREFIXES

CONTEXT = re.compile(
    r'<(environment_context|user_instructions|permissions instructions|INSTRUCTIONS|'
    r'recommended_plugins|external_codex_apps_open_page|in_app_browser_context|'
    r'turn_aborted)>[\s\S]*?(?:</\1>|\Z)'
    r'|(?m:^[ \t]*# AGENTS\.md instructions[^\n]*(?:\n|$))'
)
THREAD_COLUMNS = ('id', 'rollout_path', 'created_at', 'updated_at', 'source', 'title', 'cwd')
ITEM_COLUMNS = ('thread_id', 'rollout_ordinal', 'created_at_ms', 'item_json', 'item_type')


class _UnsupportedSQLite(ValueError):
    """The reason is a fixed code suitable for returning to the caller."""


def _text_parts(parts, allowed):
    if not isinstance(parts, list):
        raise ValueError
    text = []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get('type'), str):
            raise ValueError
        if part['type'] in allowed:
            if not isinstance(part.get('text'), str):
                raise ValueError
            text.append(part['text'])
    return '\n'.join(text)


def _object(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
        raise OSError
    return info


def _directory(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_reparse_tag', 0)


def _fingerprint(info):
    # Windows fstat and lstat can report different ctimes for the same open file.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class _Session:
    key: str
    day: date
    last_day: date
    status: str
    size: int = 0
    turns: int = 0
    messages: list = field(default_factory=list)
    digest_units: int = 0
    truncated: bool = False

    def add(self, role, text, state):
        if role == 'user':
            text = CONTEXT.sub('', text).strip()
            if text.startswith(HARNESS_SYNTHETIC_PROMPT_PREFIXES):
                return
            if OPT_OUT in text.casefold():
                self.status = 'opted_out'
                self.messages.clear()
                return
            self.turns += bool(text)
        if not text or state is None or self.truncated or self.status != 'ready':
            return
        limit = USER_CHARS if role == 'user' else ASSISTANT_CHARS
        message = {'role': role, 'text': _filtered(text, limit, state, Counter())}
        cost = len(json.dumps(message, ensure_ascii=True)) + PAGE_ITEM
        if self.digest_units + cost > PAGE_CHARS:
            self.truncated = True
        else:
            self.messages.append(message)
            self.digest_units += cost

    def public(self):
        return {'session': self.key, 'date': self.day.isoformat(),
                'last_activity': self.last_day.isoformat(), 'human_turns': self.turns,
                'kib': max(1, round(self.size / 1024))}


def _codex_cwd(cwd):
    if not cwd.startswith(('\\\\?\\', '//?/')):
        return cwd
    path = cwd[4:]
    if path[:4].replace('\\', '/').casefold() == 'unc/':
        return cwd[:2] + path[4:]
    return path


def _classify(raw_id, cwd, day, interactive, keys):
    if not isinstance(raw_id, str) or not raw_id or not isinstance(cwd, str) or not cwd or day is None:
        raise ValueError
    status = 'ready'
    if _key(_codex_cwd(cwd)) not in keys:
        status = 'other_vault'
    elif not interactive:
        status = 'non_interactive'
    return _Session(session_hash(raw_id), day, day, status)


def _rollout(path, info, keys, selected, state):
    with path.open('rb') as handle:
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(info):
            raise OSError
        first = handle.readline(MAX_LINE + 1)
        if len(first) > MAX_LINE:
            raise ValueError
        record = _object(first)
        meta = record.get('payload')
        if record.get('type') != 'session_meta' or not isinstance(meta, dict):
            raise ValueError
        session = _classify(meta.get('id'), meta.get('cwd'), _day(meta.get('timestamp')),
                            meta.get('originator') == 'codex_cli_rs' and meta.get('source') == 'cli', keys)
        if session.status != 'ready':
            return session
        if info.st_size > MAX_SCAN_BYTES:
            session.status = 'blocked'
            return session
        session.size = scanned = len(first)
        malformed = False
        while line := handle.readline(min(MAX_LINE, MAX_SCAN_BYTES - scanned) + 1):
            scanned += len(line)
            if scanned > MAX_SCAN_BYTES or len(line) > MAX_LINE:
                # A skipped record can contain an escaped opt-out marker.
                session.status = 'blocked'
                break
            if not line.strip():
                continue
            try:
                row = _object(line)
                day = _day(row.get('timestamp'))
                if day is not None:
                    session.last_day = max(session.last_day, day)
                if row.get('type') != 'response_item':
                    continue
                payload = row.get('payload')
                if not isinstance(payload, dict):
                    raise ValueError
                role = payload.get('role')
                if (payload.get('type') != 'message' or role not in ('user', 'assistant')
                        or payload.get('channel') in ('analysis', 'summary')):
                    continue
                text = _text_parts(payload.get('content'), ('input_text', 'output_text', 'text')).strip()
            except (ValueError, RecursionError):
                malformed = True
                if OPT_OUT_BYTES in line.lower():
                    session.status = 'opted_out'
                    break
                continue
            session.add(role, text, state if session.key == selected else None)
            if session.status == 'opted_out':
                break
        if session.status == 'ready':
            if scanned != info.st_size or _fingerprint(_regular(path)) != _fingerprint(info):
                session.status = 'blocked'
            elif malformed:
                session.status = 'unreadable'
        session.size = scanned
        return session


def _rollout_files(root, counts):
    try:
        if not _directory(root):
            counts['unreadable'] += 1
            return
    except FileNotFoundError:
        return
    except OSError:
        counts['unreadable'] += 1
        return

    pending = [root]
    while pending:
        parent = pending.pop()
        dirs, names = [], []
        try:
            if parent != root and not _directory(parent):
                counts['unreadable'] += 1
                continue
            with os.scandir(parent) as entries:
                for entry in entries:
                    try:
                        # Check the current entry because scandir may cache directory metadata.
                        info = os.lstat(entry.path)
                    except OSError:
                        counts['unreadable'] += 1
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
                        counts['unreadable'] += 1
                    elif stat.S_ISDIR(info.st_mode):
                        dirs.append(entry.name)
                    elif entry.name.startswith('rollout-') and entry.name.endswith('.jsonl'):
                        names.append(entry.name)
        except OSError:
            counts['unreadable'] += 1
        pending.extend(parent / name for name in sorted(dirs, reverse=True))
        yield from (parent / name for name in sorted(names))


def _sqlite_signature(path):
    """Return an immutable file fingerprint, or None for an existing WAL pair."""
    info = _regular(path)
    sidecars = set()
    for suffix in ('-journal', '-wal', '-shm'):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        if suffix == '-journal':
            raise _UnsupportedSQLite('rollback_journal')
        sidecars.add(sidecar)
    if len(sidecars) == 1:
        raise _UnsupportedSQLite('partial_sidecars')
    for sidecar in sidecars:
        _regular(sidecar)
    return None if sidecars else _fingerprint(info)


def _home_files(home):
    names = set()
    with os.scandir(home) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not getattr(info, 'st_reparse_tag', 0):
                names.add(entry.name)
    return names


def _check_no_new_files(home, before):
    if _home_files(home) - before:
        raise _UnsupportedSQLite('new_home_files')


def _sqlite_table(db, name, columns):
    # Fixed table/column names; reject views and virtual tables.
    kind = db.execute('SELECT type FROM pragma_table_list WHERE schema = ? AND name = ?',
                      ('main', name)).fetchone()
    if kind != ('table',):
        raise _UnsupportedSQLite('missing_table')
    actual = {row[1] for row in db.execute('PRAGMA main.table_info(' + name + ')')}
    if not set(columns) <= actual:
        raise _UnsupportedSQLite('missing_column')


def _sqlite_items(history, raw_id, session, state):
    rows = history.execute(
        "SELECT rollout_ordinal, created_at_ms, item_type, typeof(item_json),"
        " length(CAST(item_json AS BLOB)),"
        " CASE WHEN length(CAST(item_json AS BLOB)) <= ? THEN item_json END"
        " FROM thread_items WHERE thread_id = ? AND"
        " (item_type IN ('userMessage', 'agentMessage') OR typeof(item_type) != 'text')"
        " ORDER BY rollout_ordinal, created_at_ms", (MAX_LINE, raw_id),
    )
    for ordinal, stamp, kind, storage, size, data in rows:
        if type(ordinal) is not int or type(stamp) is not int or not isinstance(kind, str) or storage != 'text':
            raise _UnsupportedSQLite('unexpected_type')
        session.size += size
        if data is None or session.size > MAX_SCAN_BYTES:
            session.status = 'blocked'
            return
        item = _object(data)
        if kind == 'userMessage':
            text = _text_parts(item.get('content'), ('text',)).strip()
            role = 'user'
        else:
            text = item.get('text')
            if not isinstance(text, str):
                raise ValueError
            text, role = text.strip(), 'assistant'
        session.last_day = max(session.last_day, datetime.fromtimestamp(stamp / 1000).date())
        session.add(role, text, state)
        if session.status == 'opted_out':
            return


def _sqlite(home, keys, selected, state):
    paths = [home / name for name in ('state_5.sqlite', 'thread_history_1.sqlite')]
    try:
        signatures = [_sqlite_signature(path) for path in paths]
        with ExitStack() as stack:
            if None in signatures:
                stack.callback(_check_no_new_files, home, _home_files(home))
            databases = []
            for path, signature, table, columns in zip(
                    paths, signatures, ('threads', 'thread_items'), (THREAD_COLUMNS, ITEM_COLUMNS)):
                # Recheck sidecars as close as possible to SQLite's first access.
                if _sqlite_signature(path) != signature:
                    raise _UnsupportedSQLite('changed_during_read')
                options = '?mode=ro' if signature is None else '?mode=ro&immutable=1'
                db = stack.enter_context(closing(sqlite3.connect(
                    path.absolute().as_uri() + options, uri=True)))
                db.execute('PRAGMA query_only = ON')
                db.execute('PRAGMA temp_store = MEMORY')
                db.execute('PRAGMA trusted_schema = OFF')
                stack.enter_context(db)
                db.execute('BEGIN')
                _sqlite_table(db, table, columns)
                databases.append(db)
            users, history = databases
            rows = users.execute('SELECT ' + ', '.join(THREAD_COLUMNS)
                                 + ' FROM threads ORDER BY created_at, id LIMIT ?', (MAX_FILES + 1,))
            found = {}
            for raw_id, rollout, created, updated, source, title, cwd in rows:
                if len(found) == MAX_FILES:
                    raise _UnsupportedSQLite('thread_limit')
                if (not all(isinstance(value, str) for value in (raw_id, rollout, source, title, cwd))
                        or type(created) is not int or type(updated) is not int):
                    raise _UnsupportedSQLite('unexpected_type')
                # SQLite has no originator column. Accept only its positive CLI source label.
                session = _classify(raw_id, cwd, datetime.fromtimestamp(created).date(), source == 'cli', keys)
                if session.key in found:
                    raise _UnsupportedSQLite('duplicate_thread')
                if session.status == 'ready':
                    session.last_day = max(session.day, datetime.fromtimestamp(updated).date())
                    _sqlite_items(history, raw_id, session, state if session.key == selected else None)
                found[session.key] = session
        if any(signature is not None and _sqlite_signature(path) != signature
               for path, signature in zip(paths, signatures)):
            raise _UnsupportedSQLite('changed_during_read')
        return found, None
    except _UnsupportedSQLite as error:
        return {}, str(error)
    except FileNotFoundError:
        return {}, 'missing_database'
    except (OSError, sqlite3.Error):
        return {}, 'unreadable_database'
    except (ValueError, OverflowError, RecursionError):
        return {}, 'unexpected_data'


def _merge(preferred, alternate):
    # Honor opt-outs and conflicting scope/origin evidence from either readable source.
    priorities = ('opted_out', 'other_vault', 'non_interactive', 'blocked')
    for status in priorities:
        if status in (preferred.status, alternate.status):
            return preferred if preferred.status == status else alternate
    return alternate if preferred.status == 'unreadable' else preferred


def _discover(vault, codex_home, selected=None, state=None):
    keys, home = _vault_keys(vault), Path(codex_home)
    counts = Counter(other_vault=0, opted_out=0, non_interactive=0, unreadable=0,
                     unsupported=0, empty=0)
    found = {}
    for index, path in enumerate(_rollout_files(home / 'sessions', counts)):
        if index == MAX_FILES:
            counts['file_limit'] += 1
            break
        try:
            session = _rollout(path, _regular(path), keys, selected, state)
        except (OSError, ValueError, OverflowError, RecursionError):
            counts['unreadable'] += 1
            continue
        previous = found.get(session.key)
        found[session.key] = _merge(previous, session) if previous else session

    database, reason = _sqlite(home, keys, selected, state)
    if reason:
        counts['unsupported'] += 1
    for key, session in database.items():
        if key in found:
            found[key] = _merge(found[key], session)
        elif not counts['file_limit']:
            # An unseen rollout could otherwise override this SQLite-only entry.
            found[key] = session
    selectable = []
    for session in found.values():
        if session.status != 'ready':
            counts['unreadable' if session.status == 'blocked' else session.status] += 1
        elif not session.turns:
            counts['empty'] += 1
        else:
            selectable.append(session)
    return selectable, counts, reason


def plan(vault, codex_home, limit=LIST_DEFAULT):
    """List metadata for one vault. No message text is retained or returned."""
    if type(limit) is not int or not 1 <= limit <= LIST_MAX:
        raise ValueError('limit must be 1..%d' % LIST_MAX)
    sessions, counts, reason = _discover(vault, codex_home)
    sessions.sort(key=lambda item: (item.day, item.key))
    counts['pending'] = len(sessions)
    result = {'status': 'dry_run', 'writes': 0, 'counts': dict(counts),
              'sqlite': 'unsupported' if reason else 'supported',
              'pending': [item.public() for item in sessions[:limit]]}
    if reason:
        result['sqlite_reason'] = reason
    return result


def show(vault, codex_home, session, *, state=None):
    """Return one bounded, redacted digest for an explicitly selected session hash."""
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        raise ValueError('expected a session hash')
    directory = Path(state) if state is not None else Path(vault) / 'state'
    sessions, _, _ = _discover(vault, codex_home, session, directory)
    for item in sessions:
        if item.key == session:
            return {'status': 'digest', 'session': session, 'messages': item.messages,
                    'truncated': item.truncated}
    return {'status': 'unavailable', 'session': session}
