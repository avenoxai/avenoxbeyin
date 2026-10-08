"""Bounded local topic references, not a transcript or a source of new facts.

Only sources actually delivered by a hook can become anchors. A vague continuation
may reuse them within the same harness/session/project, for twenty minutes from the
last concrete turn. Reusing an anchor never extends that deadline.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from beyin_v3 import HARNESSES, STOPWORDS, _tokens, pack_context

TTL_SECONDS = 1200
MAX_SESSIONS = 128
MAX_REFS = 3
# Token composition rather than a list of complete prompts/prefixes. Novel content
# words veto inheritance; false negatives are safer than carrying a wrong project.
ACTIONS = _tokens('yap et incele araştır kurcala takıl dene test anlat raporla göster uygula '
                  'kaydır geri al devam kontrol bak aç detaylandır değiştir do inspect test '
                  'check investigate explain report continue apply move undo try')
DEICTIC = _tokens('onu bunu şunu aynı şekilde bizimkinde bizimkini biraz birlikte beraber '
                 'bunun onun bunları onları orada böyle sonra şimdi daha da peki it that '
                 'this those same together further then more')
GENERIC = _tokens('genel fayda faydaları faydalarını sonuç sonucu detay ayrıntı nasıl olur '
                 'eder misin yapar mısın acaba lütfen general benefits result details please')
FAREWELL = _tokens('teşekkür teşekkürler sağol görüşürüz teşekkür ederim thanks goodbye bye')
SHIFT = _tokens('başka farklı konu instead unrelated')


def _path(store, harness, session, create=False):
    if harness not in HARNESSES or not isinstance(session, str) or session in ('', 'unknown') or len(session) > 512:
        return None
    folder = store.state_dir / 'topic-refs'
    if folder.is_symlink():
        return None
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256((harness + '\0' + session).encode()).hexdigest()
    path = folder / (digest + '.json')
    return None if path.is_symlink() else path


def _read(path, now):
    try:
        if path is None or path.stat().st_size > 16000:
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('version') != 1 or
                type(value.get('at')) not in (int, float) or not 0 <= now - value['at'] < TTL_SECONDS or
                (value.get('project') is not None and not isinstance(value['project'], str)) or
                not isinstance(value.get('refs'), list) or not 1 <= len(value['refs']) <= MAX_REFS):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _current(store, saved):
    # Reapply all source, index, supersession, visibility, trust and project gates.
    eligible = store._retrieve('', project=saved['project'], snapshot=True,
                               limit=100000, budget_chars=10000000)['records']
    by_id = {r['id']: r for r in eligible if not r.get('text_truncated')}
    records = []
    for ref in saved['refs']:
        if not isinstance(ref, dict):
            return []
        record = by_id.get(ref.get('id'))
        if record is None or record.get('project') != saved['project'] or any(record.get(k) != ref.get(k) for k in ('revision', 'source_sha256')):
            return []  # Never silently combine a changed source with an old topic.
        records.append(record)
    return records


def resolve(store, harness, session, query, local, *, budget_chars, project=None, now=None):
    """Return (context, inherited). No provider import, raw prompt persistence or writes."""
    now = time.time() if now is None else now
    terms = _tokens(query) - STOPWORDS
    if not terms or terms & FAREWELL or terms & SHIFT:
        return local, False
    saved = _read(_path(store, harness, session), now)
    if saved is None or (project is not None and saved['project'] != project):
        return local, False
    try:
        records = _current(store, saved)
    except (ValueError, OSError):
        return local, False
    if not records:
        return local, False
    vocabulary = set().union(*(_tokens(r.get('title', '') + ' ' + r['text']) for r in records))
    familiar = vocabulary | ACTIONS | DEICTIC | GENERIC
    # A command with a deictic, or an action referring only to the active subject.
    continuation = bool(terms & DEICTIC or terms & ACTIONS) and len(terms) <= 16
    if not continuation or terms - familiar:
        return local, False
    return dict(pack_context(records, MAX_REFS, budget_chars), continuity='local_source_refs'), True


def remember(store, harness, session, query, delivered, *, inherited=False, now=None):
    """Persist only IDs/revisions/hashes for delivered sources. Fail closed on ambiguity."""
    path = _path(store, harness, session)
    if path is None:
        return
    if inherited:
        return  # Do not refresh the concrete-topic TTL with an endless vague chain.
    terms = _tokens(query) - STOPWORDS
    records = delivered.get('records', [])
    projects = {r.get('project') for r in records}
    if (not terms or terms & (FAREWELL | SHIFT) or not records or len(projects) != 1 or
            (next(iter(projects)) is not None and not isinstance(next(iter(projects)), str))):
        path.unlink(missing_ok=True)
        return
    # Only meaningful concrete turns establish anchors; generic commands alone cannot.
    if not (terms - ACTIONS - DEICTIC - GENERIC):
        return
    now = time.time() if now is None else now
    value = dict(version=1, at=now, project=next(iter(projects)), refs=[
        {k: r[k] for k in ('id', 'revision', 'source_sha256')}
        for r in records[:MAX_REFS] if all(k in r for k in ('id', 'revision', 'source_sha256'))])
    if not value['refs']:
        return
    temporary = None
    try:
        path = _path(store, harness, session, create=True)
        if path is None:
            return
        # This directory is owned by this feature; bound both lifetime and count. The prune runs
        # before the write, so it leaves room for this session's file: MAX_SESSIONS in all.
        files = sorted((p for p in path.parent.glob('*.json') if not p.is_symlink() and p != path),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for candidate in files[MAX_SESSIONS - 1:]:
            candidate.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump(value, handle)
        os.replace(temporary, path)
    except (OSError, ValueError):
        pass
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
