"""Opt-in conversation checkpoints: metadata-only hints, explicit short Markdown writes.

This prototype never reads transcript message bodies or infers what a conversation meant. The
switch and counters stay in state; only an explicit write creates a searchable
source. Unknown turn origins are deliberately silent.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

SETTINGS = 'chat-trace.json'
SESSIONS = 'chat-trace-sessions'
MAX_PARAGRAPH = 450
MAX_NOTE_BYTES = 256_000


def _read(path, limit=4096):
    from beyin_v3_notes import _unlinked
    _unlinked(path)
    with path.open(encoding='utf-8') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('chat trace metadata is too large')
    return json.loads(raw)


def settings(state):
    """Read without creating anything; malformed/unknown settings disable the feature."""
    try:
        value = _read(Path(state) / SETTINGS)
        valid = (isinstance(value, dict) and set(value) == {'schema', 'enabled'}
                 and type(value['schema']) is int and value['schema'] == 1
                 and type(value['enabled']) is bool)
        return {'enabled': value['enabled'] if valid else False, 'valid': valid}
    except FileNotFoundError:
        return {'enabled': False, 'valid': True}
    except (OSError, ValueError):
        return {'enabled': False, 'valid': False}


def configure(state, enabled):
    from beyin_v3_notes import _unlinked
    from beyin_v3_sync import atomic
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    _unlinked(state / SETTINGS)
    atomic(state / SETTINGS, json.dumps({'schema': 1, 'enabled': bool(enabled)}) + '\n')
    return settings(state)


def _human_content(payload):
    """Reject synthetic and delegated messages before considering an opt-out."""
    from beyin_v3_hook import HARNESS_SYNTHETIC_PROMPT_PREFIXES, prompt_text
    origin = payload.get('origin')
    kind = origin.get('kind') if isinstance(origin, dict) else origin
    if (payload.get('no_memory') is True or payload.get('agent_id') or payload.get('agent_type')
            or not prompt_text(payload).strip() or kind in ('task-notification', 'peer', 'coordinator')
            or prompt_text(payload).lstrip().startswith(HARNESS_SYNTHETIC_PROMPT_PREFIXES)):
        return False
    return True


def human_turn(harness, payload):
    """Conservative pilot policy; these signals are not a client-wide identity API."""
    if not _human_content(payload):
        return False
    if harness == 'claude':
        entry = os.environ.get('CLAUDE_CODE_ENTRYPOINT', '')
        attended = os.environ.get('CLAUDE_CODE_SESSION_ATTENDED', '').lower()
        if entry in ('sdk-cli', 'cli') or attended in ('0', 'false'):
            return False
        return entry == 'claude-desktop' or attended in ('1', 'true')
    if harness != 'codex' or payload.get('originator') == 'codex_exec' or payload.get('agent_id'):
        return False
    # Codex 0.163.0 does not include originator in UserPromptSubmit JSON. Read only
    # the bounded first metadata line; no message body or transcript is retained.
    try:
        from beyin_v3_notes import _unlinked
        raw = payload.get('transcript_path')
        if not isinstance(raw, str) or not Path(raw).is_absolute() or not raw.endswith('.jsonl'):
            return False
        path = Path(raw)
        _unlinked(path)
        if not stat.S_ISREG(path.stat().st_mode):
            return False
        with path.open('rb') as source:
            line = source.readline(65_537)
        if len(line) > 65_536:
            return False
        record = json.loads(line)
        meta = record.get('payload', {})
        return (record.get('type') == 'session_meta' and isinstance(meta, dict)
                and meta.get('id') == payload.get('session_id')
                and meta.get('originator') == 'codex_cli_rs' and meta.get('source') == 'cli')
    except (OSError, ValueError, AttributeError):
        return False


@contextmanager
def _lock(state):
    """Non-blocking process lock; a busy writer can never hold up a prompt hook."""
    from beyin_v3_notes import _unlinked
    folder = Path(state) / SESSIONS
    _unlinked(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = folder / 'writer.lock'
    _unlinked(lock)
    with lock.open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield folder
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _marker(folder, harness, session, now):
    if harness not in ('claude', 'codex') or not re.fullmatch(r'[0-9a-f]{24}', session):
        raise ValueError('use the 24-character Receipt session value and its harness')
    path = folder / (harness + '-' + session + '.json')
    try:
        value = _read(path)
        if (not isinstance(value, dict) or value.get('schema') != 1
                or any(type(value.get(k)) is not int or not 0 <= value[k] <= 1_000_000
                       for k in ('turns', 'covered', 'notices', 'next_due'))
                or value['covered'] > value['turns'] or value['notices'] > 3
                or type(value.get('since')) not in (int, float)
                or not 0 <= value['since'] <= now
                or type(value.get('suppressed')) is not bool
                or not isinstance(value.get('last_event'), str)):
            raise ValueError('invalid chat trace counter; remove this session marker to reset')
    except FileNotFoundError:
        value = {'schema': 1, 'turns': 0, 'covered': 0, 'notices': 0, 'next_due': 2,
                 'since': now, 'suppressed': False, 'last_event': ''}
    return path, value


def hint(state, harness, payload):
    """Only called on UserPromptSubmit. Fail open; never return a Stop decision."""
    if not settings(state)['enabled']:
        return ''
    from beyin_v3_hook import _get_session_receipt, prompt_text
    from beyin_v3_sync import atomic
    suppress = '[kaydetme]' in prompt_text(payload).casefold()
    # Human refusal survives silent clients, but agent quotations cannot opt out.
    if not _human_content(payload) or (not suppress and not human_turn(harness, payload)):
        return ''
    raw_session = payload.get('session_id')
    if not isinstance(raw_session, str) or raw_session in ('', 'unknown') or len(raw_session) > 512:
        return ''
    session = hashlib.sha256(raw_session.encode()).hexdigest()[:24]
    try:
        with _lock(state) as folder:
            now = time.time()
            path, value = _marker(folder, harness, session, now)
            if value['suppressed']:
                return ''
            if suppress:
                value['suppressed'] = True
                atomic(path, json.dumps(value))
                return ''
            event_id = payload.get('event_id') or payload.get('turn_id')
            event = hashlib.sha256(event_id.encode()).hexdigest() if isinstance(event_id, str) and event_id else ''
            if event and event == value['last_event']:
                return ''
            # A receipt closes the counted turns before this prompt, not this new turn.
            if _get_session_receipt(Path(state) / 'memory.sqlite3', harness, session, value['since']):
                value.update(covered=value['turns'], since=now, next_due=value['turns'] + 2)
            value['turns'] += 1
            value['last_event'] = event
            due = value['turns'] >= value['next_due'] and value['notices'] < 3
            if due:
                value['notices'] += 1
                value['next_due'] = value['turns'] + 8
            atomic(path, json.dumps(value))
            if due:
                return ('[Sohbet izi] Anlamli bir sonuc varsa tek kisa paragrafla kaydet: '
                        'python3 beyin.py chat-trace write --harness ' + harness + ' --session ' + session +
                        ' --file IZ_DOSYASI. Tarihle birlikte en cok 450 karakter; hassas bilgiyi ekleme. '
                        'Kaydedilecek sonuc yoksa devam et. [kaydetme] bu oturumun iz ipuclarini susturur.\n')
    except Exception:
        pass
    return ''


def write(vault, state, harness, session, text):
    """Append only the explicitly supplied summary through the existing source writer."""
    if not settings(state)['enabled']:
        raise ValueError('chat trace is off; explicitly enable it before writing')
    if not isinstance(text, str) or len(text) > 4096 or not text.strip():
        raise ValueError('supply a short nonempty summary, never a transcript')
    today = datetime.now().astimezone().date().isoformat()
    paragraph = today + ': ' + ' '.join(text.split())
    if len(paragraph) > MAX_PARAGRAPH:
        raise ValueError('dated trace paragraph exceeds 450 characters; shorten it explicitly')
    from beyin_v3_sync import SyncEngine, atomic, parse, render
    from beyin_v3_notes import NoteConflict, locked
    with _lock(state) as folder:
        path, value = _marker(folder, harness, session, time.time())
        if value['suppressed']:
            raise ValueError('chat trace was suppressed for this session by [kaydetme]')
        engine = SyncEngine(vault, state)
        source = 'knowledge/conversations/' + today[:7] + '.md'
        with locked(engine):
            previous = engine.note_read(source)
            content = previous['content']
            metadata = {'kind': 'note', 'title': 'Sohbet izleri ' + today[:7],
                        'visibility': 'internal', 'remote_allowed': False, 'chat_trace': 1}
            if content is None:
                body = ('# Sohbet izleri ' + today[:7] + '\n\n'
                        'Kisa oturum ozetleri; ajan beyanidir, dogrulanmis karar kaydi degildir.\n\n' + paragraph + '\n')
                expected = render(metadata, body)
                result = engine.note_edit({'source': source, 'expected_sha256': None,
                                           'content': expected})
            else:
                if len(content.encode('utf-8')) > MAX_NOTE_BYTES:
                    raise ValueError('monthly trace note is full; review it before adding more')
                current, body = parse(content)
                if any(current.get(k) != metadata[k] for k in ('chat_trace', 'visibility', 'remote_allowed')):
                    raise ValueError('existing monthly note has different ownership/privacy; preserved')
                paragraphs = [part.strip() for part in body.replace('\r\n', '\n').split('\n\n')]
                if paragraph in paragraphs:
                    expected = content
                    result = {'status': 'unchanged', 'source': source}
                else:
                    newline = '\r\n' if '\r\n' in content else '\n'
                    separator = ('' if content.endswith(newline * 2) else
                                 newline if content.endswith(newline) else newline * 2)
                    expected = content + separator + paragraph + newline
                    result = engine.note_edit({'source': source, 'expected_sha256': previous['sha256'],
                                               'edits': [{'old': content, 'new': expected}]})
            if result.get('status') not in ('written', 'unchanged'):
                raise NoteConflict('chat trace write was not confirmed; checkpoint left open')
            # Cooperative locking does not cover Obsidian or git. Check our exact
            # intended bytes again before acknowledging the checkpoint, including
            # the replay path which performed no write of its own.
            if engine.note_read(source)['content'] != expected:
                raise NoteConflict('chat trace changed before acknowledgement; checkpoint left open')
        # A committed, verified trace closes this checkpoint. Replaying after a crash
        # finds the exact paragraph before acknowledging, so it cannot duplicate it.
        value.update(covered=value['turns'], since=time.time(), next_due=value['turns'] + 2)
        atomic(path, json.dumps(value))
        return {'status': result['status'], 'source': source, 'paragraph_chars': len(paragraph),
                'covered_turns': value['covered'], 'remote_allowed': False}
