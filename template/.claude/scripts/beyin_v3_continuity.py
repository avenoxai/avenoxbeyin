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

from beyin_v3 import HARNESSES, STOPWORDS, CONTEXT_MARKER, _tokens, pack_context

TTL_SECONDS = 1200
MAX_SESSIONS = 128
MAX_REFS = 3
MAX_RANGES = 8
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
        if (not isinstance(value, dict) or value.get('version') != 2 or
                type(value.get('at')) not in (int, float) or not 0 <= now - value['at'] < TTL_SECONDS or
                (value.get('project') is not None and not isinstance(value['project'], str)) or
                not isinstance(value.get('refs'), list) or not 1 <= len(value['refs']) <= MAX_REFS):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _current(store, saved):
    if any(not isinstance(ref, dict) or not isinstance(ref.get('id'), str)
           or not ref['id'] for ref in saved['refs']):
        return []
    indexed = store._records()
    # Verify only the saved refs, through the same gates _retrieve applies to an empty
    # snapshot query: visibility, trust, rejection, project scope and source freshness
    # (_eligible), supersession resolved over every indexed record, so a hidden trusted
    # superseder still retires its target, and the default retired-status filter. The
    # previous whole-vault snapshot hashed every source to look up at most MAX_REFS ids,
    # and in a vault over its 10M-character packing budget could drop a valid anchor.
    ids = {ref.get('id') for ref in saved['refs']}
    eligible, _ = store._eligible('internal', saved['project'], records=[r for r in indexed if r['id'] in ids])
    superseded = store._superseded_ids(indexed)
    from beyin_v3_passage import pool, settings
    eligible = pool(store, eligible, exclude=settings(store.state_dir)['exclude'], superseded=superseded)
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


def _restore(record, ref):
    """Reconstruct source ranges only after _current verified hash/revision and policy."""
    from beyin_v3_evidence import reference, render_spans
    from beyin_v3_passage import split_blocks, _partial
    ranges = ref.get('ranges')
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_RANGES:
        return None
    spans, blocks, previous = [], split_blocks(record['text']), -1
    for item in ranges:
        if (not isinstance(item, list) or len(item) != 2 or
                any(type(offset) is not int for offset in item)):
            return None
        start, end = item
        if not 0 <= start < end <= len(record['text']) or start < previous:
            return None
        head = next((head for head, left, right in blocks if left <= start < right), '')
        spans.append((head, start, end))
        previous = end
    text, _ = render_spans(record, spans)
    excerpt = dict(record, text=text, text_truncated=_partial(record, [(record, h, s, e, '') for h, s, e in spans]))
    return excerpt, reference(record, spans, 'continuity')


def resolve(store, harness, session, query, local, *, budget_chars, project=None, now=None):
    """Return (context, inherited). No provider import, raw prompt persistence or writes."""
    now = time.time() if now is None else now
    terms = _tokens(query) - STOPWORDS
    if not terms or terms & FAREWELL or terms & SHIFT:
        return local, False
    saved = _read(_path(store, harness, session), now)
    if saved is None or (project is not None and saved['project'] != project):
        return local, False
    # This necessary predicate needs no records or source freshness checks.
    continuation = bool(terms & DEICTIC or terms & ACTIONS) and len(terms) <= 16
    if not continuation:
        return local, False
    try:
        records = _current(store, saved)
    except (ValueError, OSError):
        return local, False
    if not records:
        return local, False
    selected, evidence = [], {}
    for record, ref in zip(records, saved['refs']):
        restored = _restore(record, ref)
        if restored is None:
            return local, False
        excerpt, proof = restored
        selected.append(excerpt)
        evidence[record['id']] = proof
    # A source may cover several subjects. Only the delivered ranges establish
    # the active topic; words from an unseen section must not replace a current
    # query's local evidence with the previous passage.
    vocabulary = set().union(*(_tokens(r.get('title', '') + ' ' + r['text']) for r in selected))
    familiar = vocabulary | ACTIONS | DEICTIC | GENERIC
    if terms - familiar:
        return local, False
    context = pack_context(selected, MAX_REFS, budget_chars)
    context['continuity'] = 'local_source_refs'
    context.evidence = {r['id']: evidence[r['id']] for r in context['records']}
    return context, True


def remember(store, harness, session, query, delivered, *, inherited=False, now=None):
    """Persist hash-bound delivered ranges, never source text or a raw prompt."""
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
    from beyin_v3_evidence import delivered_ranges
    refs, indexed = [], None
    for record in records[:MAX_REFS]:
        if not all(k in record for k in ('id', 'revision', 'source_sha256')):
            continue
        ref = getattr(delivered, 'evidence', {}).get(record['id'])
        ranges = delivered_ranges(ref, record) if ref else []
        if ref is None:
            # Note-level fallback/older callers still get exact prefix ranges. A
            # passage without a reference is never guessed back to the note head.
            if indexed is None:
                indexed = {r['id']: r for r in store._records()}
            original = indexed.get(record['id'])
            text = record['text']
            if record.get('text_truncated') and text.endswith(CONTEXT_MARKER):
                text = text[:-len(CONTEXT_MARKER)]
            if (text and original and all(original.get(k) == record[k] for k in ('revision', 'source_sha256'))
                    and original['text'].startswith(text)):
                ranges = [[0, len(text)]]
        if not ranges or len(ranges) > MAX_RANGES:
            path.unlink(missing_ok=True)
            return  # Never silently replace one member of an ambiguous topic.
        refs.append(dict({k: record[k] for k in ('id', 'revision', 'source_sha256')}, ranges=ranges))
    value = dict(version=2, at=now, project=next(iter(projects)), refs=refs)
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
