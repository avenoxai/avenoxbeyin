"""Local, hash-bound source ranges shared by retrieval, cards and continuity.

Offsets address indexed record text, not raw Markdown bytes. References live on
PackedContext attributes, never in the model envelope or the remote card schema.
"""


def render_spans(record, spans):
    """Render (heading, start, end) spans; map copied source text to output offsets."""
    pieces, mapped, last_head, last_end, length = [], [], None, None, 0

    def append(text):
        nonlocal length
        if not text:
            return length
        if pieces:
            length += 2
        offset = length
        pieces.append(text)
        length += len(text)
        return offset

    for head, start, end in sorted(spans, key=lambda span: span[1]):
        if last_end is not None and start <= last_end:
            if end > last_end:
                mapped.append([last_end, end, length])
                pieces[-1] += record['text'][last_end:end]
                length += end - last_end
            last_end = max(end, last_end)
            continue
        if last_end is not None and record['text'][last_end:start].strip():
            append('[…]')
        if head and head != last_head:
            append(head)
        offset = append(record['text'][start:end])
        mapped.append([start, end, offset])
        last_head, last_end = head, end
    return '\n\n'.join(pieces), mapped


def reference(record, spans, channel):
    text, mapped = render_spans(record, spans)
    return dict(id=record['id'], source_sha256=record['source_sha256'],
                revision=record['revision'], channel=channel,
                variants=[dict(text=text, spans=mapped)])


def delivered_ranges(ref, record):
    """Only source characters actually shown, including a partially clipped last span."""
    from beyin_v3 import CONTEXT_MARKER
    if any(ref.get(k) != record.get(k) for k in ('id', 'revision', 'source_sha256')):
        return []
    shown = record['text']
    # Try the exact text first: a source can itself end with the clipping marker.
    for prefix in (shown, shown[:-len(CONTEXT_MARKER)] if shown.endswith(CONTEXT_MARKER) else shown):
        for variant in ref['variants']:
            if not variant['text'].startswith(prefix):
                continue
            return [[start, start + min(end - start, len(prefix) - offset)]
                    for start, end, offset in variant['spans'] if offset < len(prefix)]
    return []


def _score(text, terms, ignored=None):
    from beyin_v3 import _tokens, _query_roots
    tokens = _tokens(text)
    shared = terms & tokens
    roots = _query_roots(terms, lambda word: int(word in tokens), ignored or set())
    shared |= {term for term, root in roots.items() if root in tokens}
    return len(shared)


def query_terms(query, project=None):
    from beyin_v3 import STOPWORDS, _strict_terms, _tokens
    return _strict_terms(_tokens(query) - STOPWORDS - _tokens(project or ''))


def loose_passages(record, query, project=None):
    """Select within an already admitted loose source; no new admission or policy gate."""
    from beyin_v3_passage import split_blocks, _partial
    from beyin_v3 import STOPWORDS, _tokens
    terms = query_terms(query, project)
    ignored = STOPWORDS | _tokens(project or '')
    blocks = split_blocks(record['text'])
    # Selected body evidence keeps its source-order ties (#530). Shared heading
    # words must not crowd it off the two-passage card. Heading-only matches are
    # a fallback when no body matches, retaining upstream's subsection repair.
    body_matches = any(_score(record['text'][start:end], terms, ignored) for _, start, end in blocks)
    score = lambda span: _score(('' if body_matches else span[0] + '\n') +
                               record['text'][span[1]:span[2]], terms, ignored)
    ranked = sorted(enumerate(blocks), key=lambda item: (-score(item[1]), item[0]))
    if not ranked:
        return record, None, reference(record, [('', 0, len(record['text']))], 'loose')
    first = ranked[0][1]
    second = next((span for _, span in ranked[1:] if score(span)), None)
    spans = [first]
    text, _ = render_spans(record, spans)
    partial = lambda selected: _partial(record, [(record, h, s, e, '') for h, s, e in selected])
    narrow = dict(record, text=text, text_truncated=partial(spans))
    ref = reference(record, spans, 'loose')
    wide = None
    if second is not None:
        spans.append(second)
        text, _ = render_spans(record, spans)
        wide = dict(record, text=text, text_truncated=partial(spans))
        ref['variants'] += reference(record, spans, 'loose')['variants']
    return narrow, wide, ref


def loose_delivery(record, selected, query):
    """Reserve primary evidence alongside the opening after a source is rescued.

    Three standard passage windows protect the opening without making an entire
    long note the minimum delivery. Reserve 500 characters of that allowance
    for the primary matching passage. Wider alternatives keep both parts.
    """
    from beyin_v3 import TOP_CONTEXT_TEXT
    from beyin_v3_passage import split_blocks, _partial
    evidence_chars = 500
    opening = ('', 0, min(len(record['text']), 3 * TOP_CONTEXT_TEXT - evidence_chars))
    broad_opening = ('', 0, min(len(record['text']), 3 * TOP_CONTEXT_TEXT))
    partial = lambda spans: _partial(record, [(record, h, s, e, '') for h, s, e in spans])
    headings = {(s, e): h for h, s, e in split_blocks(record['text'])}
    matched = [(headings.get((s, e), ''), s, e) for s, e, _ in selected['variants'][0]['spans']]
    windows = []
    for head, start, end in matched:
        body = record['text'][start:end]
        excerpt = card_excerpt(query, dict(text=body), limit=evidence_chars)
        offset = body.index(excerpt)
        windows.append((head, start + offset, start + offset + len(excerpt)))
    ref = reference(record, [opening] + windows, 'loose')
    narrow = dict(record, text=ref['variants'][0]['text'], text_truncated=partial([opening] + windows))
    alternatives, seen = [], {narrow['text']}
    for spans in [[('', 0, len(record['text']))], [broad_opening] + matched, [broad_opening] + windows]:
        variant = reference(record, spans, 'loose')['variants'][0]
        if variant['text'] in seen:
            continue
        seen.add(variant['text'])
        alternatives.append(dict(record, text=variant['text'], text_truncated=partial(spans)))
        ref['variants'].append(variant)
    return narrow, alternatives or None, ref


def strict_extensions(record, selected, budget):
    """Whole-source/section alternatives, after all existing passage reservations.

    They include every selected body span. They do not choose candidates or
    change the evidence excerpt shown to the advisor.
    """
    from beyin_v3_passage import split_blocks, _partial
    blocks = split_blocks(record['text'])
    sections = []
    for head, start, end in selected:
        pos = next((i for i, block in enumerate(blocks) if block == (head, start, end)), None)
        if pos is None:
            return [], []
        low = high = pos
        while low and blocks[low - 1][0] == head:
            low -= 1
        while high + 1 < len(blocks) and blocks[high + 1][0] == head:
            high += 1
        sections.append((head, blocks[low][1], blocks[high][2]))
    sections = list(dict.fromkeys(sections))
    alternatives, variants, seen = [], [], {render_spans(record, selected)[0]}
    for spans in [[('', 0, len(record['text']))], sections]:
        # A source text larger than the entire envelope can never fit whole.
        if sum(end - start for _, start, end in spans) > budget:
            continue
        variant = reference(record, spans, 'strict')['variants'][0]
        if variant['text'] in seen:
            continue
        seen.add(variant['text'])
        partial = _partial(record, [(record, h, s, e, '') for h, s, e in spans])
        alternatives.append(dict(record, text=variant['text'], text_truncated=partial))
        variants.append(variant)
    return alternatives, variants


def card_excerpt(query, record, broader=None, ref=None, limit=600, *, project=None):
    """Bounded query windows, with a share for each selected passage, not a note prefix."""
    from beyin_v3_passage import split_blocks
    from beyin_v3 import STOPWORDS, _tokens
    from beyin_v3_jev import _card_heading
    project = record.get('project') if project is None else project
    terms = query_terms(query, project)
    ignored = STOPWORDS | _tokens(project or '')
    selected = broader or record
    # Each original passage gets represented, even if the first one fills the card.
    if ref:
        variant = ref['variants'][-1]
        chunks = [variant['text'][offset:offset + end - start]
                  for start, end, offset in variant['spans']]
        # Heading paths are already in the exact selected excerpt. Keep them with
        # the following body, rather than spending a card slot on a heading alone.
        for i, (_start, _end, offset) in enumerate(variant['spans']):
            before = 0 if i == 0 else variant['spans'][i - 1][2] + variant['spans'][i - 1][1] - variant['spans'][i - 1][0]
            heading = variant['text'][before:offset].strip().strip('[…]').strip()
            if heading:
                heading = _card_heading(heading, max(0, limit // len(chunks) // 3 - 2),
                                        lambda text: _score(text, terms, ignored))
                chunks[i] = heading + '\n\n' + chunks[i]
    else:
        chunks = [selected['text']]
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return selected['text'][:limit]
    separator = '\n[…]\n'
    room = max(0, limit - len(separator) * (len(chunks) - 1))
    allocations = [min(len(chunk), room // len(chunks)) for chunk in chunks]
    spare = room - sum(allocations)
    for i, chunk in enumerate(chunks):
        extra = min(spare, len(chunk) - allocations[i])
        allocations[i] += extra
        spare -= extra
    excerpts = []
    for chunk, budget in zip(chunks, allocations):
        if len(chunk) <= budget:
            excerpts.append(chunk)
            continue
        # Locate a relevant block, then spend its whole share on surrounding
        # context. A short matching sentence must not leave most of the card empty.
        blocks = split_blocks(chunk, target=max(40, budget // 2), overlap=0)
        candidates = []
        for _head, start, end in blocks:
            left = max(0, min(start - max(0, budget - (end - start)) // 2, len(chunk) - budget))
            candidates.append(chunk[left:left + budget])
        excerpts.append(max(candidates, key=lambda text: _score(text, terms, ignored), default=chunk[:budget]))
    return separator.join(excerpts)[:limit]
