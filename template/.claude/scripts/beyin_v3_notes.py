"""Cooperative, byte-preserving writes of human Markdown sources (stdlib only).

The persistent OS lock is vault-local: an MSIX AppData redirect can split even a
pinned state path between clients. Uncooperative editors do not take this lock.
"""
from contextlib import contextmanager
import hashlib
import base64
import errno
import json
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


class NoteConflict(Exception):
    pass


class NotePartialWrite(NoteConflict):
    """Replacement completed; the caller must not assume the source is unchanged."""
    def __init__(self, details, cause):
        super().__init__('source written but verification failed after replace (' +
                         type(cause).__name__ + '); reread source and run sync before retrying')
        self.write_occurred = True
        self.details = dict(details, write_occurred=True, cause=type(cause).__name__)


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
    engine._refuse_excluded(source)
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
    # Recovery can run inside note-create/note-edit's sync. Reuse only this
    # thread's lock; a forked child and other threads still need their own OS lock.
    held = getattr(_HELD_LOCKS, 'held', None)
    if held is None:
        held = _HELD_LOCKS.held = {}
    key = (os.getpid(), os.path.normcase(str(engine.root)))
    if key in held:
        yield held[key]
        return
    folder = engine.root / LOCAL_DIRECTORY
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
    with lock.open('a+b') as handle:
        # Windows can lock byte zero beyond EOF. Do not write a sentinel before
        # acquisition: another handle may already own that byte of an empty file.
        deadline = time.monotonic() + timeout
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
        try:
            held[key] = folder
            try:
                yield folder
            finally:
                held.pop(key, None)
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read(engine, source):
    from beyin_v3_sync import _hash
    source_path(engine, source)
    with locked(engine):
        path = source_path(engine, source)
        source = path.relative_to(engine.root).as_posix()
        _refuse_tracked_view(engine, source)
        raw = _bytes(path)
        text = raw.decode('utf-8') if raw is not None else None
        if text is not None:
            _human(text)
    return {'source': source, 'sha256': _hash(raw) if raw is not None else None,
            'content': text}


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


def _idempotency_path(folder, key):
    directory = folder / 'idempotency'
    _unlinked(directory)
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (key + '.json')
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


def _replay_idempotency(engine, path, source, fingerprint, source_path):
    from beyin_v3_sync import _hash
    if not path.is_file():
        return None
    saved = json.loads(path.read_text(encoding='utf-8'))
    if saved.get('source') != source:
        raise ValueError('idempotency_key already used for a different source')
    if saved.get('request_sha256') != fingerprint:
        raise ValueError('idempotency_key already used for a different request')
    current = _bytes(source_path)
    if current is not None and _hash(current) == saved.get('sha256'):
        # Recover a write interrupted after source replacement but before final publication.
        result = engine.sync()
        if engine._source_issues(result, source):
            raise NoteConflict('idempotent source requires projection review')
        return dict({k: v for k, v in saved.items() if k != 'request_sha256'}, idempotent=True)
    return None


def _apply(engine, request, committed):
    from beyin_v3_sync import _hash, _safe_replace
    if not isinstance(request, dict):
        raise ValueError('note-edit request must be an object')
    source = request.get('source')
    source_path(engine, source)
    if 'expected_sha256' not in request and 'expected_source_hash' not in request:
        raise ValueError('expected_sha256 is required; use note-read first')
    if 'expected_sha256' in request and 'expected_source_hash' in request:
        if request['expected_sha256'] != request['expected_source_hash']:
            raise ValueError('expected_sha256 and expected_source_hash must match if both provided')
    expected = request.get('expected_source_hash', request.get('expected_sha256'))
    if expected is not None and (not isinstance(expected, str) or
                                 not re.fullmatch('[0-9a-f]{64}', expected)):
        raise ValueError('expected_sha256 must be a SHA-256 hex digest or null')
    idempotency_key = request.get('idempotency_key')
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError('idempotency_key must be a nonempty string')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', idempotency_key):
            raise ValueError('idempotency_key contains invalid characters')
    evidence_refs = request.get('evidence_refs')
    if evidence_refs is not None:
        if isinstance(evidence_refs, str):
            evidence_refs = [evidence_refs]
        if not isinstance(evidence_refs, list) or not all(isinstance(r, str) and r.strip() for r in evidence_refs):
            raise ValueError('evidence_refs must be a list of nonempty strings')
    with locked(engine) as folder:
        path = source_path(engine, source)  # Revalidate after waiting for the lock.
        source = path.relative_to(engine.root).as_posix()
        _refuse_tracked_view(engine, source)
        idempotency_file = None
        fingerprint = _request_digest(dict(request, source=source, expected_sha256=expected))
        if idempotency_key:
            idempotency_file = _idempotency_path(folder, idempotency_key)
            replay = _replay_idempotency(engine, idempotency_file, source, fingerprint, path)
            if replay is not None:
                return replay
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
            text = raw.decode('utf-8')
            _human(text)
            for edit in request['edits']:
                if not isinstance(edit, dict):
                    raise ValueError('each edit must be an object')
                old, new = edit.get('old'), edit.get('new')
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise ValueError('each edit requires nonempty old and string new')
                position = text.find(old)
                if position < 0 or text.find(old, position + 1) >= 0:
                    raise NoteConflict('edit anchor must occur exactly once')
                text = text.replace(old, new, 1)
        _human(text)  # Do not create a managed source or convert a human note into one.
        updated = text.encode('utf-8')  # Keeps BOM and untouched CRLF bytes exactly.
        if updated == raw:
            res = {'status': 'unchanged', 'source': source, 'sha256': actual}
            if evidence_refs:
                res['evidence_refs'] = evidence_refs
            if idempotency_file:
                _save_idempotency(idempotency_file, res, fingerprint)
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
            if evidence_refs:
                backup_dict['evidence_refs'] = evidence_refs
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
            if idempotency_file:
                _save_idempotency(idempotency_file,
                    {'status': 'written', 'source': source, 'sha256': _hash(updated),
                     'backup': details['backup'], **({'evidence_refs': evidence_refs} if evidence_refs else {})},
                    fingerprint)
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
        with engine.store._connect() as db:
            row = db.execute('SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id '
                             'WHERE m.source=?', (source,)).fetchone()
        if (engine._source_issues(result, source) or not row or
                json.loads(row[0]).get('source_sha256') != _hash(updated) or
                _bytes(source_path(engine, source)) != updated):
            raise NoteConflict('source written but index readback failed; reread and run sync')
        written_details = {
            'status': 'written',
            'source': source,
            'sha256': _hash(updated),
            'backup': backup.relative_to(engine.root).as_posix() if backup else None,
        }
        if evidence_refs:
            written_details['evidence_refs'] = evidence_refs
        return engine._write_diagnostics(written_details, result)
