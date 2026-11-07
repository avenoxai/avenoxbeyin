"""Cooperative, byte-preserving writes of human Markdown sources (stdlib only).

The persistent OS lock is vault-local: an MSIX AppData redirect can split even a
pinned state path between clients. Uncooperative editors do not take this lock.
"""
from contextlib import contextmanager
import hashlib
import base64
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

LOCAL_DIRECTORY = '.beyin-note-write'
_HELD_LOCKS = threading.local()
# The retry journal is local and bounded. Pruning only forgets a replay answer: a late
# retry then meets the ordinary expected-hash or source-exists check and cannot write twice.
IDEMPOTENCY_MAX_RECORDS = 256
IDEMPOTENCY_MAX_AGE = 7 * 24 * 3600
_IDEMPOTENCY_KEY = re.compile(r'[A-Za-z0-9_-]{1,128}')
_IDEMPOTENCY_RECORD = re.compile(r'[0-9a-f]{64}\.json')
_IDEMPOTENCY_TEMPORARY = re.compile(r'\.idempotency-[^/\\]*\.tmp')


class NoteConflict(Exception):
    pass


class NotePartialWrite(NoteConflict):
    """Replacement completed; the caller must not assume the source is unchanged."""
    def __init__(self, details, cause):
        super().__init__('source written but verification failed after replace (' +
                         type(cause).__name__ + '); reread source and run sync before retrying')
        self.write_occurred = True
        self.details = dict(details, write_occurred=True, cause=type(cause).__name__)


class Utf8InputError(UnicodeError):
    """Text that is not standard UTF-8; raised before any source write (#543).

    Lone surrogates and CESU-8 encoded supplementary characters are rejected, never
    passed through or replaced. A UnicodeError, so also a ValueError for callers.
    """


_SURROGATE = re.compile('[\ud800-\udfff]')


def _encoded_surrogate(raw, at, first, last):
    """The surrogate a 3-byte ED sequence encodes, as CESU-8/WTF-8 do; 0 otherwise."""
    if (len(raw) >= at + 3 and raw[at] == 0xED and first <= raw[at + 1] <= last
            and 0x80 <= raw[at + 2] <= 0xBF):
        return 0xD000 | ((raw[at + 1] & 0x3F) << 6) | (raw[at + 2] & 0x3F)
    return 0


def _pair(high, low):
    return 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)


def _byte_problem(raw, start, where):
    high = _encoded_surrogate(raw, start, 0xA0, 0xAF)
    low = _encoded_surrogate(raw, start + 3, 0xB0, 0xBF) if high else 0
    if low:
        return 'CESU-8 encoded U+%X (surrogate pair U+%04X U+%04X) at %s' % (
            _pair(high, low), high, low, where)
    single = _encoded_surrogate(raw, start, 0xA0, 0xBF)
    if single:
        return 'encoded lone surrogate U+%04X at %s' % (single, where)
    return 'invalid UTF-8 sequence starting with byte 0x%02X at %s' % (raw[start], where)


def utf8_problem(raw, start):
    """Describe the invalid UTF-8 at raw[start], e.g. from UnicodeDecodeError.start."""
    return _byte_problem(raw, start, 'byte %d' % start)


def decode_utf8(raw, what='source'):
    """Strict UTF-8 decode whose error names CESU-8 and lone surrogates."""
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise Utf8InputError(what + ' is not valid UTF-8: ' + utf8_problem(raw, exc.start) +
                             '; it was left unchanged, re-save it as standard UTF-8 '
                             '(lone surrogates and CESU-8 are rejected)') from exc


def require_utf8_text(value, what):
    """Reject a str holding surrogate code points; other types are left to the caller.

    A str only keeps surrogates that could not form a character: JSON decodes a valid
    \\ud83d\\ude00 escape pair to one character. Adjacent high/low code points came from
    CESU-8 or UTF-16 units decoded one by one; POSIX argv and streams carry
    undecodable bytes as U+DC80..U+DCFF (surrogateescape).
    """
    if not isinstance(value, str):
        return value
    match = _SURROGATE.search(value)
    if match is None:
        return value
    at = match.start()
    where = 'character %d' % at
    first = ord(value[at])
    second = ord(value[at + 1]) if at + 1 < len(value) else 0
    if 0xD800 <= first <= 0xDBFF and 0xDC00 <= second <= 0xDFFF:
        problem = 'CESU-8/UTF-16 surrogate pair U+%04X U+%04X instead of U+%X at %s' % (
            first, second, _pair(first, second), where)
    else:
        problem = 'unpaired surrogate U+%04X at %s' % (first, where)
        if 0xDC80 <= first <= 0xDCFF:
            end = at
            while end < len(value) and 0xDC80 <= ord(value[end]) <= 0xDCFF:
                end += 1
            escaped = bytes(ord(char) - 0xDC00 for char in value[at:end])
            if _encoded_surrogate(escaped, 0, 0xA0, 0xBF):
                problem = _byte_problem(escaped, 0, where) + ' (undecodable input bytes)'
            else:
                problem += ' (undecodable input byte 0x%02X)' % escaped[0]
    raise Utf8InputError(what + ' is not valid UTF-8: ' + problem +
                         '; nothing was written, send standard UTF-8 '
                         '(lone surrogates and CESU-8 are rejected)')


def require_utf8_values(value, what):
    """require_utf8_text over JSON-like nested dict keys, values and list items."""
    if isinstance(value, dict):
        for key, item in value.items():
            require_utf8_text(key, what + ' key')
            require_utf8_values(item, what + '.' + key if isinstance(key, str) else what)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            require_utf8_values(item, '%s[%d]' % (what, index))
    else:
        require_utf8_text(value, what)
    return value


def _require_request_text(request):
    require_utf8_text(request.get('content'), 'content')
    edits = request.get('edits')
    if isinstance(edits, list):
        for index, edit in enumerate(edits):
            if isinstance(edit, dict):
                for key in ('old', 'new'):
                    require_utf8_text(edit.get(key), 'edits[%d].%s' % (index, key))


def _unlinked(path):
    """Also reject junctions on Python 3.11, which has no Path.is_junction()."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
        raise ValueError('symlink or junction path rejected')


def _checked_source(engine, source):
    from beyin_v3_sync import EXCLUDED_DIRS, EXCLUDED_FILES
    if not isinstance(source, str) or not source or '\\' in source or ':' in source:
        raise ValueError('use a vault-relative Markdown path with / separators')
    # A surrogate would reach the filesystem as a CESU-8/raw-byte or UTF-16 name.
    require_utf8_text(source, 'source')
    parts = source.split('/')
    if any(not part or part in ('.', '..') or part.startswith('.') or
           part.endswith((' ', '.')) for part in parts):
        raise ValueError('only visible vault-relative sources are supported')
    # Windows trims trailing dots/spaces and treats these names as device paths.
    if any(re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)
           for part in parts):
        raise ValueError('device paths are not sources')
    folded = [part.casefold() for part in parts]
    if Path(source).suffix.lower() != '.md':
        raise ValueError('only Markdown sources are supported')
    if (folded[0] in ('tasks', 'receipts') or
            folded[:2] in (['daily', 'v3'], ['knowledge', 'v3'])):
        raise ValueError('managed source; use the dedicated V3 command')
    if (folded[-1] in EXCLUDED_FILES or folded[-1].startswith('setup-') or
            any(part in EXCLUDED_DIRS for part in folded[:-1])):
        raise ValueError('configuration or excluded source rejected')
    from beyin_v3 import sensitive_source
    if sensitive_source(source):
        raise ValueError('source is under a sensitive directory excluded from the memory index')
    path = engine._path(source)
    # Sync follows a junction that stays inside the vault; a cooperative writer is stricter
    # and never writes through any link or reparse point between the vault and the note.
    cursor = path
    while cursor != engine.root and cursor != cursor.parent:
        _unlinked(cursor)
        cursor = cursor.parent
    return path


def source_path(engine, source):
    """Use the directory entry spelling, including case and Windows short aliases.

    A casefolded SQL lookup would conflate distinct files on a case-sensitive
    volume. Match actual filesystem identity, preferring an exact entry name,
    and recheck the stored path so an 8.3 alias cannot bypass source exclusions.
    """
    path = _checked_source(engine, source)
    cursor = engine.root
    for part in path.relative_to(engine.root).parts:
        candidate = cursor / part
        _unlinked(candidate)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            cursor = candidate
            continue
        with os.scandir(cursor) as entries:
            children = list(entries)
        exact = next((entry for entry in children if entry.name == part), None)
        if exact is not None:
            cursor = cursor / exact.name
            continue
        # DirEntry.stat reports zero st_ino/st_dev on Windows; use lstat for
        # both identities instead of its cached FindFirstFile attributes.
        matches = [entry.name for entry in children
                   if os.path.samestat(info, (cursor / entry.name).lstat())]
        if len(matches) != 1:
            raise ValueError('ambiguous source alias; use the stored path spelling')
        cursor = cursor / matches[0]
    return _checked_source(engine, cursor.relative_to(engine.root).as_posix())


def _fsync_parent(path):
    # Windows does not expose directory fsync through this API. On POSIX, a
    # failed fsync must report a committed but unverified write, not success.
    if os.name != 'nt':
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _human(text):
    from beyin_v3_sync import parse
    metadata, _ = parse(text)
    if (str(metadata.get('kind', '')).casefold() in ('task', 'receipt', 'receipt-index')
            or metadata.get('generated')):
        raise ValueError('managed source; use the dedicated V3 command')


def _refuse_tracked_view(engine, source):
    # A generated flat daily can lose its frontmatter during an external edit.
    # Current ownership rows still make it a managed view. Use the canonical
    # source, not a filename pattern or historical provenance of a retired view.
    with engine.store._connect() as db:
        # #411 keeps flat ownership outside the table understood by older engines.
        # Check both schemas so either PR can be installed independently.
        for table in ('receipt_views', 'receipt_flat_views'):
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists and db.execute(f'SELECT 1 FROM {table} WHERE path=?', (source,)).fetchone():
                raise ValueError('managed source; use the dedicated V3 command')


def _bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def locked(engine, timeout=15):
    """Engine entry point; all human-note writers share the vault protocol."""
    with vault_locked(engine.root, timeout=timeout) as folder:
        yield folder


@contextmanager
def vault_locked(vault, timeout=15):
    """Persistent vault-wide OS lock, also usable without a database/SyncEngine.

    Order: note lock -> optional companion lock -> database transaction.
    Recovery already holding the DB may only try this lock with timeout=0.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError('timeout must be a number')
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError('timeout must be finite and non-negative')
    vault = Path(vault).resolve()
    # Recovery can run inside note-create/note-edit's sync. Reuse only this
    # thread's lock; a forked child and other threads still need their own OS lock.
    held = getattr(_HELD_LOCKS, 'held', None)
    if held is None:
        held = _HELD_LOCKS.held = {}
    key = (os.getpid(), os.path.normcase(str(vault)))
    if key in held:
        yield held[key]
        return
    folder = vault / LOCAL_DIRECTORY
    _unlinked(folder)
    folder.mkdir(exist_ok=True, mode=0o700)
    # Also protects existing installs whose top-level .gitignore is user-owned.
    ignore = folder / '.gitignore'
    _unlinked(ignore)
    try:
        with ignore.open('x', encoding='utf-8') as out:
            out.write('*\n')
    except FileExistsError:
        pass
    lock = folder / 'writer.lock'
    _unlinked(lock)
    deadline = time.monotonic() + timeout
    while True:
        _unlinked(lock)
        with lock.open('a+b') as handle:
            # Windows can lock byte zero beyond EOF. Do not write a sentinel before
            # acquisition: another handle may already own that byte of an empty file.
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise NoteConflict('writer busy; reread the source and retry') from exc
                    time.sleep(0.05)

            stale = False
            try:
                st1 = os.fstat(handle.fileno())
                _unlinked(lock)
                st2 = os.stat(lock)
                if st1.st_dev != st2.st_dev or st1.st_ino != st2.st_ino:
                    stale = True
            except OSError:
                stale = True

            if stale:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                if time.monotonic() >= deadline:
                    raise NoteConflict('writer busy; reread the source and retry')
                time.sleep(0.05)
                continue

            try:
                held[key] = folder
                try:
                    yield folder
                    return
                finally:
                    held.pop(key, None)
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read(engine, source):
    from beyin_v3_sync import _hash
    source_path(engine, source)
    with locked(engine):
        path = source_path(engine, source)
        source = path.relative_to(engine.root).as_posix()
        _refuse_tracked_view(engine, source)
        raw = _bytes(path)
        text = decode_utf8(raw) if raw is not None else None
        if text is not None:
            _human(text)
    # A missing note reads as nulls so the caller can create it; `exists` says so
    # without a null check (#587).
    return {'source': source, 'exists': raw is not None,
            'sha256': _hash(raw) if raw is not None else None, 'content': text}


def apply(engine, request):
    committed = {}
    try:
        return _apply(engine, request, committed)
    except Exception as exc:
        # Includes cleanup, path checks, directory fsync, sync, DB readback,
        # diagnostics and lock release. Every exception after replace is explicit.
        if committed:
            raise NotePartialWrite(committed, exc) from exc
        raise


def check_idempotency_key(key):
    if key is None:
        return None
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError('idempotency_key must be 1-128 ASCII letters, digits, _ or -')
    return key


def evidence_refs(engine, refs, own_source=None, existing=True):
    """Note evidence follows the task contract: existing vault sources, stored as POSIX paths.

    Free-form session or candidate ids are not sources; cite the session's receipt
    source instead. Note metadata is delivered with the note, so a source under a
    sensitive folder is rejected rather than named. existing=False only canonicalizes,
    which lets a retry be matched before its evidence is checked again.
    """
    if refs is None:
        return []
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        raise ValueError('evidence_refs must be a list of source paths')
    from beyin_v3 import sensitive_source
    own = None
    if existing and own_source is not None:
        own = engine._source_identity(engine._path(own_source))
    canonical, seen = [], set()
    for ref in refs:
        try:
            path = engine._path(ref, existing=existing)
        except ValueError:
            raise ValueError('evidence ref must be an existing vault source') from None
        relative = path.relative_to(engine.root).as_posix()
        if sensitive_source(relative):
            raise ValueError('evidence ref is under a sensitive directory excluded from the memory index')
        if existing:
            identity = engine._source_identity(path)
            if identity in seen:
                raise ValueError('evidence_refs must not repeat a source')
            if own is not None and identity == own:
                raise ValueError('evidence ref cannot be the note source itself')
            seen.add(identity)
        canonical.append(relative)
    return canonical


def _index_excluded(engine, source):
    from beyin_v3_index import dir_key, exclusions as index_exclusions
    excluded_keys = index_exclusions(engine.state, engine.root)[0] if engine.state else frozenset()
    return bool(excluded_keys and any(dir_key(part) in excluded_keys for part in Path(source).parts[:-1]))


def _idempotency_path(folder, key):
    directory = folder / 'idempotency'
    _unlinked(directory)
    directory.mkdir(exist_ok=True, mode=0o700)
    _unlinked(directory)
    # Case-insensitive volumes would merge keys that differ only in case; a digest keeps
    # every key distinct and keeps caller text out of file names.
    path = directory / (hashlib.sha256(key.encode('ascii')).hexdigest() + '.json')
    _unlinked(path)
    return path


def _request_digest(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _save_idempotency(path, details, fingerprint):
    from beyin_v3_sync import _safe_replace
    _unlinked(path.parent)
    _unlinked(path)
    fd, temporary = tempfile.mkstemp(prefix='.idempotency-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(dict(details, request_sha256=fingerprint), out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        _unlinked(path.parent)
        _unlinked(path)
        _safe_replace(temporary, path)
        _fsync_parent(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _prune_idempotency(directory, keep, now=None):
    """Keep at most IDEMPOTENCY_MAX_RECORDS recent records; the caller holds the writer lock."""
    now = time.time() if now is None else now
    records = []
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        record = _IDEMPOTENCY_RECORD.fullmatch(name)
        if not record and not _IDEMPOTENCY_TEMPORARY.fullmatch(name):
            continue
        try:
            info = os.lstat(directory / name)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
            continue
        if not record:
            # Only lock holders write here, so a temporary left now is from a crash.
            _discard(directory / name)
        elif name != keep.name:
            records.append((info.st_mtime, name))
    records.sort(reverse=True)
    for position, (modified, name) in enumerate(records):
        if position >= IDEMPOTENCY_MAX_RECORDS - 1 or now - modified > IDEMPOTENCY_MAX_AGE:
            _discard(directory / name)


def _discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass  # Pruning is best effort; it never fails the write it follows.


def _journal_idempotency(path, details, fingerprint, state):
    """'pending' is written before the source changes; 'committed' after it is verified."""
    _save_idempotency(path, dict(details, state=state), fingerprint)
    _prune_idempotency(path.parent, path)


def _seal_idempotency(path, details, fingerprint, output):
    # The source write is already verified, so a journal failure here must not turn it
    # into a conflict. The record stays pending, which still replays matching bytes.
    try:
        _save_idempotency(path, dict(details, state='committed'), fingerprint)
    except (OSError, ValueError):
        output['idempotency_journal'] = 'pending'
    return output


def _replay_idempotency(engine, path, source, fingerprint, source_path, refs=()):
    """Answer a retry from the journal, or None when the keyed request never replaced the source."""
    from beyin_v3_sync import _hash
    _unlinked(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        saved = json.loads(raw.decode('utf-8'))
    except ValueError:
        saved = None
    if (not isinstance(saved, dict) or saved.get('state') not in ('pending', 'committed') or
            not isinstance(saved.get('sha256'), str)):
        raise ValueError('idempotency record unreadable; use a new idempotency_key')
    if saved.get('source') != source:
        raise ValueError('idempotency_key already used for a different source')
    if saved.get('request_sha256') != fingerprint:
        raise ValueError('idempotency_key already used for a different request')
    current = _bytes(source_path)
    if current is None or _hash(current) != saved['sha256']:
        if saved['state'] == 'pending':
            return None  # Interrupted before replacement: the ordinary checks decide.
        # A completed request is never applied again, even if the source was reverted.
        raise NoteConflict('idempotency_key request already completed and the source changed since; '
                           'note-read again')
    answer = {k: v for k, v in saved.items() if k not in ('request_sha256', 'state')}
    if saved.get('status') == 'unchanged':
        # The original answer wrote nothing and verified no projection; neither does its replay.
        return dict(answer, **({'evidence_refs': list(refs)} if refs else {}), idempotent=True)
    # Recover a write interrupted after source replacement but before final publication.
    result = engine.sync()
    if _index_excluded(engine, source):
        if _bytes(source_path) != current:
            raise NoteConflict('idempotent source requires projection review')
    else:
        with engine.store._connect() as db:
            row = db.execute('SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id '
                             'WHERE m.source=?', (source,)).fetchone()
        if (engine._source_issues(result, source) or _bytes(source_path) != current or not row or
                json.loads(row[0]).get('source_sha256') != saved['sha256']):
            raise NoteConflict('idempotent source requires projection review')
    if saved['state'] != 'committed':
        _seal_idempotency(path, answer, fingerprint, answer)
    if refs:
        answer['evidence_refs'] = list(refs)
    answer['idempotent'] = True
    return answer


def _apply(engine, request, committed):
    from beyin_v3_sync import _hash, _safe_replace
    if not isinstance(request, dict):
        raise ValueError('note-edit request must be an object')
    source = request.get('source')
    source_path(engine, source)
    # expected_source_hash is an alias with the same meaning; both may be sent only if equal.
    if 'expected_sha256' not in request and 'expected_source_hash' not in request:
        raise ValueError('expected_sha256 is required; use note-read first')
    if 'expected_sha256' in request and 'expected_source_hash' in request:
        if request['expected_sha256'] != request['expected_source_hash']:
            raise ValueError('expected_sha256 and expected_source_hash must match if both provided')
    expected = request.get('expected_source_hash', request.get('expected_sha256'))
    if expected is not None and (not isinstance(expected, str) or
                                 not re.fullmatch('[0-9a-f]{64}', expected)):
        raise ValueError('expected_sha256 must be a SHA-256 hex digest or null')
    _require_request_text(request)  # Before the lock: invalid text writes nothing at all.
    idempotency_key = check_idempotency_key(request.get('idempotency_key'))
    refs = evidence_refs(engine, request.get('evidence_refs'), existing=False)
    with locked(engine) as folder:
        path = source_path(engine, source)  # Revalidate after waiting for the lock.
        source = path.relative_to(engine.root).as_posix()
        _refuse_tracked_view(engine, source)
        idempotency_file = None
        # Bind the key to the canonical request, so the alias spelling cannot split a retry.
        fingerprint = _request_digest(dict({field: request[field] for field in ('content', 'edits')
                                            if field in request},
                                           operation='note-edit', source=source,
                                           expected_sha256=expected, evidence_refs=refs))
        if idempotency_key is not None:
            idempotency_file = _idempotency_path(folder, idempotency_key)
            replay = _replay_idempotency(engine, idempotency_file, source, fingerprint, path, refs)
            if replay is not None:
                return replay
        refs = evidence_refs(engine, refs, source)
        raw = _bytes(path)
        actual = _hash(raw) if raw is not None else None
        if actual != expected:
            raise NoteConflict('source changed; note-read again and rebuild the targeted edits')
        if raw is None:
            if 'edits' in request or not isinstance(request.get('content'), str):
                raise ValueError('new source requires content and expected_sha256:null')
            text = request['content']
        else:
            if 'content' in request or not isinstance(request.get('edits'), list) or not request['edits']:
                raise ValueError('existing source requires nonempty targeted edits, not content')
            text = decode_utf8(raw)
            _human(text)
            for edit in request['edits']:
                if not isinstance(edit, dict):
                    raise ValueError('each edit must be an object')
                old, new = edit.get('old'), edit.get('new')
                # The empty source has one unambiguous insertion point. The full
                # source hash was checked under the writer lock above.
                empty_source_edit = raw == b'' and len(request['edits']) == 1 and old == ''
                if not isinstance(old, str) or (not old and not empty_source_edit) or not isinstance(new, str):
                    raise ValueError('each edit requires nonempty old and string new')
                position = text.find(old)
                if position < 0 or text.find(old, position + 1) >= 0:
                    raise NoteConflict('edit anchor must occur exactly once')
                text = text.replace(old, new, 1)
        _human(text)  # Do not create a managed source or convert a human note into one.
        updated = text.encode('utf-8')  # Keeps BOM and untouched CRLF bytes exactly.
        if updated == raw:
            res = {'status': 'unchanged', 'source': source, 'sha256': actual}
            if idempotency_file:
                _journal_idempotency(idempotency_file, res, fingerprint, 'committed')
            if refs:
                res['evidence_refs'] = refs
            return res
        # Preserve the exact requested bytes. With the opt-in filter enabled,
        # reject a secret-bearing result instead of silently rewriting its anchors.
        if engine._protect(text)[1]:
            raise ValueError('secret_filter rejected note-edit content; remove secrets before retrying')
        backup = None
        if raw is not None:
            backups = folder / 'backups'
            _unlinked(backups)
            backups.mkdir(exist_ok=True, mode=0o700)
            backup = backups / (uuid.uuid4().hex + '.json')
            backup_dict = {'source': source, 'sha256': actual,
                           'bytes_base64': base64.b64encode(raw).decode('ascii')}
            if refs:
                backup_dict['evidence_refs'] = refs
            with backup.open('x', encoding='utf-8', newline='') as out:
                json.dump(backup_dict, out)
                out.flush()
                os.fsync(out.fileno())
        path.parent.mkdir(parents=True, exist_ok=True)
        path = source_path(engine, source)
        fd, temporary = tempfile.mkstemp(prefix='.beyin-note-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(updated)
                out.flush()
                os.fsync(out.fileno())
            path = source_path(engine, source)
            _refuse_tracked_view(engine, path.relative_to(engine.root).as_posix())
            if _bytes(path) != raw:
                raise NoteConflict('external edit detected before replace; source preserved')
            details = {'source': source, 'written_sha256': _hash(updated),
                       'backup': backup.relative_to(engine.root).as_posix() if backup else None}
            journal = {'status': 'written', 'source': source, 'sha256': _hash(updated),
                       'backup': details['backup']}
            if idempotency_file:
                # Journal before replacement: a failure here leaves the source untouched.
                _journal_idempotency(idempotency_file, journal, fingerprint, 'pending')
            _safe_replace(temporary, path)
            committed.update(details)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _fsync_parent(path)
        if _bytes(source_path(engine, source)) != updated:
            raise NoteConflict('external edit detected after replace; reread source')
        # Keep the lock through indexing so another participating writer cannot
        # change this source between the write and projection readback.
        result = engine.sync()
        if not _index_excluded(engine, source):
            with engine.store._connect() as db:
                row = db.execute('SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id '
                                 'WHERE m.source=?', (source,)).fetchone()
            if (engine._source_issues(result, source) or not row or
                    json.loads(row[0]).get('source_sha256') != _hash(updated) or
                    _bytes(source_path(engine, source)) != updated):
                raise NoteConflict('source written but index readback failed; reread and run sync')
        else:
            if _bytes(source_path(engine, source)) != updated:
                raise NoteConflict('external edit detected after replace; reread source')
        written_details = dict(journal)
        if idempotency_file:
            _seal_idempotency(idempotency_file, journal, fingerprint, written_details)
    if refs:
        written_details['evidence_refs'] = refs
    return engine._write_diagnostics(written_details, result)
