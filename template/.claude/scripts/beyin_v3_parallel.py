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
import math
import os
from pathlib import Path
import tempfile
import time
import re
import uuid

SETTINGS_FILE = 'parallel-sessions.json'
MARKERS = 'session-markers'
PRESENCE_DIR = '.presence'
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


def _presence_folder(vault, create=False):
    if vault is None:
        return None
    folder = Path(vault) / PRESENCE_DIR
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


def _load_presence(path):
    try:
        with open(path, encoding='utf-8') as source:
            raw = source.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('schema') != 1 or
                not isinstance(value.get('session'), str) or not re.fullmatch(r'[0-9a-f]{24}', value['session']) or
                not isinstance(value.get('state'), str) or not re.fullmatch(r'[0-9a-f]{16,32}', value['state']) or
                type(value.get('last_at')) not in (int, float) or not math.isfinite(value['last_at'])):
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


def _state_key(state, create=False):
    """A local identity, since two devices can use exactly the same state pathname.

    Doctor only reads. Creation happens only on an enabled, real prompt; state is
    machine-local and this file must stay out of the vault's sync.
    """
    path = Path(state) / 'parallel-presence-device.json'
    if path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        key = value.get('device') if isinstance(value, dict) else None
        return key if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{32}', key) else None
    except FileNotFoundError:
        if not create:
            return None
        key = uuid.uuid4().hex
        try:
            with path.open('x', encoding='utf-8') as out:
                json.dump({'schema': 1, 'device': key}, out)
                out.flush(); os.fsync(out.fileno())
            return key
        except FileExistsError:
            return _state_key(state)
    except (OSError, ValueError):
        return None


def touch(state, harness, session_id, now=None, vault=None):
    """Refresh this session's marker; return the notice line for newly seen sessions, or ''.

    Runs on a real user prompt only. Prunes markers older than a day and keeps at most
    128. Announces each other session once per session, at most two named per line.
    When vault is provided, maintains cross-machine presence files in vault/.presence (#205).
    """
    try:
        name = _marker_name(harness, session_id)
        folder = _folder(state, create=True) if name else None
        if folder is None:
            return ''
        now = time.time() if now is None else now
        session_key = receipt_session(session_id)
        current_state_key = _state_key(state, create=True) if vault is not None else None
        own = folder / name
        saved = None if own.is_symlink() else _load(own)
        announced = [key for key in (saved or {}).get('announced', []) if isinstance(key, str)][-MAX_MARKERS:]

        # Maintain vault/.presence/<session_key>.json for cross-machine awareness (#205)
        pfolder = _presence_folder(vault, create=True) if vault is not None and current_state_key else None
        if pfolder is not None and session_key:
            pown = pfolder / f"{session_key}.json"
            psaved = None if pown.is_symlink() else _load_presence(pown)
            # Throttle writes to at most once per 300 seconds if file exists and is recent
            if psaved is None or (now - psaved.get('last_at', 0)) >= 300:
                pmarker = {
                    'schema': 1,
                    'session': session_key,
                    'harness': harness,
                    'last_at': now,
                    'state': current_state_key,
                }
                _write(pown, pmarker)

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
        active_local_sessions = set()
        for modified, entry in entries[:MAX_MARKERS - 1]:
            if entry.name.startswith('.marker-') and entry.name.endswith('.tmp'):
                continue
            if now - modified > ACTIVE_SECONDS:
                break  # sorted newest first; last_at never trails the file time
            key = entry.name[:24]
            value = _load(entry.path)
            if value is not None and -60 <= now - value['last_at'] <= ACTIVE_SECONDS:
                active_local_sessions.add(value['session'])
                if key not in announced:
                    fresh.append(dict(value, key=key))

        # Also inspect vault/.presence/ for cross-machine peer sessions (#205)
        if pfolder is not None and pfolder.is_dir():
            try:
                with os.scandir(pfolder) as plisting:
                    for pentry in plisting:
                        if not pentry.name.endswith('.json') or pentry.is_symlink():
                            continue
                        if pentry.name == f"{session_key}.json":
                            continue
                        try:
                            if not pentry.is_file():
                                continue
                            pmod = pentry.stat().st_mtime
                            pdata = _load_presence(pentry.path)
                            if pdata is None or pentry.name != pdata['session'] + '.json':
                                continue
                            peer_session = pdata['session']
                            # If this presence file was written by this same machine/state:
                            if pdata.get('state') == current_state_key:
                                if peer_session not in active_local_sessions:
                                    try:
                                        os.unlink(pentry.path)
                                    except OSError:
                                        pass
                                    continue
                            else:
                                if now - pmod > MAX_AGE_SECONDS or not -60 <= now - pdata['last_at'] <= ACTIVE_SECONDS:
                                    if now - pmod > MAX_AGE_SECONDS or now - pdata['last_at'] > MAX_AGE_SECONDS:
                                        try:
                                            os.unlink(pentry.path)
                                        except OSError:
                                            pass
                                    continue
                            if peer_session in active_local_sessions:
                                continue
                            active_local_sessions.add(peer_session)
                            if peer_session not in announced:
                                fresh.append(dict(pdata, key=peer_session))
                        except OSError:
                            continue
            except OSError:
                pass

        fresh.sort(key=lambda item: item['last_at'], reverse=True)
        marker = {'schema': 1, 'harness': harness, 'session': session_key,
                  'first_at': saved['first_at'] if saved else now, 'last_at': now,
                  'announced': (announced + [item['key'] for item in fresh])[-MAX_MARKERS:]}
        if not _write(own, marker):
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
            handle.flush()
            os.fsync(handle.fileno())
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


def end(state, harness, session_id, vault=None):
    """SessionEnd removes this session's own marker, so /clear leaves no ghost session."""
    try:
        name = _marker_name(harness, session_id)
        folder = _folder(state) if name else None
        if folder is not None:
            (folder / name).unlink(missing_ok=True)
        if vault is not None and session_id:
            pfolder = _presence_folder(vault)
            if pfolder is not None:
                session_key = receipt_session(session_id)
                (pfolder / f"{session_key}.json").unlink(missing_ok=True)
    except OSError:
        pass


def active_session_keys(state, vault=None, now=None):
    """Return the short keys of active local and remote sessions without writing markers."""
    if state is None:
        return set()
    _, sessions = _sessions(state, vault, time.time() if now is None else now)
    return {session[:8] for session in sessions}


def _sessions(state, vault, now):
    folder = _folder(state)
    all_sessions = {}
    active_sessions = set()
    current_state_key = _state_key(state)
    if folder is not None and folder.is_dir():
        try:
            with os.scandir(folder) as listing:
                for entry in listing:
                    if entry.name.endswith('.json') and not entry.is_symlink() and entry.is_file():
                        val = _load(entry.path)
                        if val is not None:
                            all_sessions[val['session']] = val
                            if now - entry.stat().st_mtime <= ACTIVE_SECONDS and -60 <= now - val['last_at'] <= ACTIVE_SECONDS:
                                active_sessions.add(val['session'])
        except OSError:
            pass
    if vault is not None:
        pfolder = _presence_folder(vault)
        if pfolder is not None and pfolder.is_dir():
            try:
                with os.scandir(pfolder) as plisting:
                    for pentry in plisting:
                        if pentry.name.endswith('.json') and not pentry.is_symlink() and pentry.is_file():
                            pval = _load_presence(pentry.path)
                            if pval is not None:
                                peer = pval['session']
                                if pval.get('state') == current_state_key and peer not in all_sessions:
                                    continue
                                if peer not in all_sessions:
                                    all_sessions[peer] = pval
                                    if now - pentry.stat().st_mtime <= ACTIVE_SECONDS and -60 <= now - pval['last_at'] <= ACTIVE_SECONDS:
                                        active_sessions.add(peer)
            except OSError:
                pass
    return len(all_sessions), active_sessions


def doctor(state, vault=None, now=None):
    """Read-only: the switch and how many markers look active. Never creates the folder."""
    now = time.time() if now is None else now
    value, valid = read_settings(state)
    result = {'enabled': value, 'valid': valid, 'markers': 0, 'active': 0}
    marker_count, active_sessions = _sessions(state, vault, now)
    result['markers'] = marker_count
    result['active'] = len(active_sessions)
    return result
