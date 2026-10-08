"""Opt-in parallel-session notice (#170): one line when another session is open on this vault.

Each session keeps one small marker under `<state>/session-markers/`, named by the hash
of harness and session id. The marker holds only `schema, harness, session, first_at,
last_at, announced`: never the prompt, never the working directory. The runtime state is
per vault and machine-local, so markers never enter git or iCloud and a session elsewhere
on the machine is not counted.

The switch lives in `<state>/parallel-sessions.json` (default off), never in
`.beyin-preferences.json`: an older release rejects unknown preference keys, so a
rollback would break its doctor and hooks. Every function here fails open: a marker
error drops the line, never the hook. The doctor reads, never writes.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

SETTINGS_FILE = 'parallel-sessions.json'
MARKERS = 'session-markers'
ACTIVE_SECONDS = 45 * 60      # another session counts as open if it was active this recently
MAX_AGE_SECONDS = 24 * 3600   # older markers are pruned on every write
MAX_MARKERS = 128
MAX_SHOWN = 2
MAX_MARKER_BYTES = 8192


def read_settings(state):
    """(enabled, valid). Missing file: off and valid. A damaged file reads as off and invalid."""
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


def enabled(state):
    return read_settings(state)[0]


def save_settings(state, value):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    path = state / SETTINGS_FILE
    if path.is_symlink():
        raise ValueError(SETTINGS_FILE + ' must be a regular file')
    fd, temporary = tempfile.mkstemp(prefix='.parallel-sessions-', dir=state)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'schema': 1, 'enabled': bool(value)}, out)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return bool(value)


def receipt_session(session_id):
    """The `Receipt session=` value the hook prints (24 hex); its first 8 name the card."""
    return hashlib.sha256(str(session_id).encode()).hexdigest()[:24]


def _folder(state, create=False):
    folder = Path(state) / MARKERS
    if folder.is_symlink():
        return None
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _marker_name(harness, session_id):
    if not isinstance(harness, str) or not isinstance(session_id, str) or session_id in ('', 'unknown') \
            or len(session_id) > 512:
        return None
    return hashlib.sha256((harness + '\0' + session_id).encode()).hexdigest() + '.json'


def _load(path):
    try:
        with open(path, encoding='utf-8') as source:
            raw = source.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('harness'), str) or
                not isinstance(value.get('session'), str) or len(value['session']) != 24 or
                any(type(value.get(key)) not in (int, float) for key in ('first_at', 'last_at')) or
                not isinstance(value.get('announced'), list)):
            return None
        return value
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _ago(seconds):
    minutes = int(seconds // 60)
    return 'az once' if minutes < 1 else str(minutes) + ' dk once'


def line(others, now):
    """ASCII Turkish, like the other hook lines. `others` is newest first."""
    shown = ', '.join('#' + item['session'][:8] + ' (' + _ago(max(0, now - item['last_at'])) + ')'
                      for item in others[:MAX_SHOWN])
    rest = len(others) - MAX_SHOWN
    return ("[Paralel oturum] Bu vault'ta " + str(len(others)) + ' oturum daha acik: ' + shown +
            (' ve ' + str(rest) + ' tane daha' if rest > 0 else '') +
            '. Ayni dosyaya dokunmadan once diskten yeniden oku; commit oncesi git status.\n')


def touch(state, harness, session_id, now=None):
    """Refresh this session's marker; return the notice line for newly seen sessions, or ''.

    Runs on a real user prompt only. Prunes markers older than a day and keeps at most
    128. Announces each other session once per session, at most two named per line.
    """
    try:
        name = _marker_name(harness, session_id)
        folder = _folder(state, create=True) if name else None
        if folder is None:
            return ''
        now = time.time() if now is None else now
        own = folder / name
        saved = None if own.is_symlink() else _load(own)
        announced = [key for key in (saved or {}).get('announced', []) if isinstance(key, str)][-MAX_MARKERS:]
        entries = []
        with os.scandir(folder) as listing:
            for entry in listing:
                temporary = entry.name.startswith('.marker-') and entry.name.endswith('.tmp')
                if entry.name == name or not (entry.name.endswith('.json') or temporary) or entry.is_symlink():
                    continue
                try:
                    if not entry.is_file():
                        continue
                    modified = entry.stat().st_mtime
                    if now - modified > MAX_AGE_SECONDS:
                        os.unlink(entry.path)
                        continue
                    if temporary:
                        continue  # a write cut short by a killed hook; pruned once it is old
                except OSError:
                    continue  # gone, or held open by another process on Windows
                entries.append((modified, entry))
        entries.sort(key=lambda item: item[0], reverse=True)
        for _, entry in entries[MAX_MARKERS - 1:]:
            try:
                os.unlink(entry.path)
            except OSError:
                pass
        fresh = []
        for modified, entry in entries[:MAX_MARKERS - 1]:
            if now - modified > ACTIVE_SECONDS:
                break  # sorted newest first; last_at never trails the file time
            key = entry.name[:24]
            if key in announced:
                continue
            value = _load(entry.path)
            if value is not None and -60 <= now - value['last_at'] <= ACTIVE_SECONDS:
                fresh.append(dict(value, key=key))
        fresh.sort(key=lambda item: item['last_at'], reverse=True)
        marker = {'schema': 1, 'harness': harness, 'session': receipt_session(session_id),
                  'first_at': saved['first_at'] if saved else now, 'last_at': now,
                  'announced': (announced + [item['key'] for item in fresh])[-MAX_MARKERS:]}
        if not _write(own, marker) and saved is None:
            return ''  # never announce what could not be recorded: no repeat on the next prompt
        return line(fresh, now) if fresh else ''
    except Exception:
        return ''


def _write(path, value):
    temporary = None
    try:
        if path.is_symlink():
            return False
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.marker-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle)
        os.replace(temporary, path)
        temporary = None
        return True
    except OSError:
        return False  # e.g. a Windows sharing violation while another session reads it
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def end(state, harness, session_id):
    """SessionEnd removes this session's own marker, so /clear leaves no ghost session."""
    try:
        name = _marker_name(harness, session_id)
        folder = _folder(state) if name else None
        if folder is not None:
            (folder / name).unlink(missing_ok=True)
    except OSError:
        pass


def doctor(state, now=None):
    """Read-only: the switch and how many markers look active. Never creates the folder."""
    now = time.time() if now is None else now
    value, valid = read_settings(state)
    result = {'enabled': value, 'valid': valid, 'markers': 0, 'active': 0}
    folder = _folder(state)
    if folder is None or not folder.is_dir():
        return result
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                if entry.name.endswith('.json') and not entry.is_symlink() and entry.is_file():
                    result['markers'] += 1
                    if now - entry.stat().st_mtime <= ACTIVE_SECONDS:
                        result['active'] += 1
    except OSError:
        pass
    return result
