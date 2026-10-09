"""Explicit optional advisor; local retrieval and source authority remain in V3."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

import beyin_v3_jev_client as client
from beyin_v3_secrets import redact
from beyin_v3 import pack_context


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _safe(store, value):
    # Reject instead of silently judging redacted evidence. Never persist matches.
    # Scan raw strings too: JSON escapes a newline as \n, which hides a line-leading key.
    text = json.dumps(value, ensure_ascii=False) + '\n' + '\n'.join(_strings(value))
    _, count = redact(text, store.state_dir)
    if count:
        raise ValueError('advisor_sensitive_input')


def _fresh(store, records):
    try:
        for record in records:
            path = store.vault_root / store._source(record['source'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != record['source_sha256']:
                return False
        return True
    except (ValueError, OSError, KeyError):
        return False


def _clear(advice, code):
    return dict(advice, scores={}, facet_scores={}, relations={}, degraded=True,
                diagnostics=advice.get('diagnostics', []) + [code])


def remote_allowed(record):
    """Local-only is a data policy, not a lower ranking or permission to send a title."""
    return (record.get('visibility') != 'private' and record.get('remote_allowed', True) is True
            and record.get('sensitivity', 'internal') in ('public', 'internal', 'normal'))


def _signature(records):
    # Include index-only policy metadata as well as the source revision and hash.
    return {r['id']: hashlib.sha256(json.dumps({k: v for k, v in r.items() if k not in ('text', 'text_truncated')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for r in records}


def _interleave(groups):
    """Round-robin coverage without a local-only prefix that can consume all slots."""
    seen, result = set(), []
    for offset in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if offset < len(group) and group[offset]['id'] not in seen:
                record = group[offset]
                result.append(record)
                seen.add(record['id'])
    return result


MANUAL_POOL = 16
MANUAL_EXCERPT = 800


def _packaged(store, query, cards, project, versions, facets, mode, transport):
    """One advisor call, or up to client.PACKAGE_LIMIT when the pool is over one request's budget.

    Sixteen ordinary notes with 800-character excerpts exceed the default 24,000-character
    request, so the whole pool used to be refused before any network call and the command
    silently kept local order. Packages are planned locally and sent once each, never
    retried; if any package fails, the whole advice is degraded and local order stands, so
    a partial ranking never reorders only part of the pool.
    """
    parts = client.packages(store.state_dir, query, cards, facets=facets)
    if parts is None or len(parts) == 1:
        # None: over the package limit; evaluate reports budget_exceeded before any call.
        advice = client.evaluate(store.state_dir, query, cards, scope='project:' + project,
                                 source_versions=versions, facets=facets, transport=transport)
        return dict(advice, packages=0 if parts is None else 1)

    def ask(part):
        return client.evaluate(store.state_dir, query, part, scope='project:' + project,
                               source_versions={c['id']: versions[c['id']] for c in part},
                               facets=facets, transport=transport)
    # A local single-worker server (laya) would only queue parallel requests.
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1 if mode.get('provider') == 'laya' else len(parts)) as pool:
        advices = list(pool.map(ask, parts))
    # Counters cover every package that was sent, including a failed one.
    usage = {k: sum(a['usage'][k] for a in advices) for k in ('input_tokens', 'output_tokens')
             if all(k in a.get('usage', {}) for a in advices)}
    counters = dict(packages=len(parts), usage=usage,
                    network_requests=sum(a['network_requests'] for a in advices),
                    http_requests=sum(a['http_requests'] for a in advices),
                    latency_ms=round((time.monotonic() - started) * 1000, 3),
                    request_hash=None, request_hashes=[a['request_hash'] for a in advices])
    failed = [a for a in advices if a['degraded']]
    if failed:
        return dict(failed[0], **counters)
    advice = dict(advices[0], **counters,
                  diagnostics=list(dict.fromkeys(d for a in advices for d in a['diagnostics'])),
                  cache_hit=all(a['cache_hit'] for a in advices),
                  scores={k: v for a in advices for k, v in a['scores'].items()},
                  facet_scores={j: {k: v for a in advices for k, v in a['facet_scores'].get(j, {}).items()}
                                for j in range(len(facets))})
    if len({a['mode'] for a in advices}) != 1:
        advice = _clear(advice, 'configuration_changed')
    return advice


def advise_context(store, query, *, project, audience='internal', statuses=None,
                   limit=5, budget_chars=8000, transport=None):
    """Retrieve a bounded pool, judge permitted cards, then pack final source records."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError('advisor_requires_explicit_project')
    if audience not in ('public', 'internal'):
        raise ValueError('advisor_private_audience_not_supported')
    params = dict(project=project, audience=audience, statuses=statuses)
    result = store.retrieve(query, **params, limit=limit, budget_chars=budget_chars)
    mode = client.inspect_config(store.state_dir)
    if not mode['valid'] or mode['mode'] == 'off' or 'context' not in mode.get('features', []):
        result['jev'] = dict(mode='off', degraded=not mode['valid'],
                             diagnostics=mode.get('diagnostics', []), scores={})
        return result
    records = store.candidates(query, **params, limit=MANUAL_POOL)
    before = _signature(records)
    remote = [r for r in records if remote_allowed(r)]
    # A local-only note needs meaningful local evidence; common-word matches don't
    # gain unconditional admission merely because the provider cannot inspect them.
    local_only = [r for r in store.candidates(query, **params, strict=True, limit=MANUAL_POOL)
                  if not remote_allowed(r)]
    try:
        cards = [dict(id=r['id'], title=r.get('title', '')[:160], statement=json.dumps(dict(text=r['text'][:MANUAL_EXCERPT],
                      excerpt_truncated=len(r['text']) > MANUAL_EXCERPT, project=r.get('project'), status=r.get('status'), updated_at=r.get('updated_at')), ensure_ascii=False),
                      scope='project:' + project, domains=[r.get('kind', 'note')]) for r in remote]
        _safe(store, [query, cards])
    except (ValueError, OSError):
        result['jev'] = _clear({'mode': mode['mode']}, 'advisor_sensitive_or_invalid_input')
        return result
    # Semicolon-separated subrequests are independent facets, not fabricated topics.
    facets = [part.strip() for part in query.split(';') if part.strip()]
    facets = facets if 1 <= len(facets) <= 3 else [query]
    advice = _packaged(store, query, cards, project, before, facets, mode, transport)
    current = store.candidates(query, **params, limit=MANUAL_POOL)
    if before != _signature(current) or not _fresh(store, records):
        result = store.retrieve(query, **params, limit=limit, budget_chars=budget_chars)
        advice = _clear(advice, 'source_or_index_changed')
    elif client.inspect_config(store.state_dir) != mode:
        advice = _clear(advice, 'configuration_changed')
    elif advice['mode'] == 'on' and not advice['degraded']:
        groups = []
        for scores in advice.get('facet_scores', {}).values():
            # Score is an expected ordinal rubric level, NOT probability of correctness.
            groups.append(sorted([r for r in remote if scores.get(r['id'], 0) >= 1.0],
                                 key=lambda r: (-scores.get(r['id'], 0), r['id'])))
        ranked = _interleave([_interleave(groups), local_only])
        result = pack_context(ranked, limit, budget_chars, result.get('stale_count', 0))
    result['jev'] = advice
    return result


AUTO_MIN_CHARS = 12  # a one or two word prompt carries no reliable topic
AUTO_PROMPT = 2000
AUTO_EXCERPT = 600
AUTO_POOL = 8
AUTO_LIMIT = 5
# Live synthetic calibration: greetings scored <= 0.16 on the gate and real topics >= 0.39;
# past the gate, off-topic notes stayed <= 0.31 and on-topic ones >= 0.76. A strict local match
# already has lexical evidence, so it is kept unless clearly off topic. A loose match enters
# only when Jev is confident. Shadow mode is how a vault checks these on its own notes.
# The values live in client.THRESHOLDS, keyed by (provider, checkpoint); these names stay for
# existing importers and are the Jev row.
_JEV = client.THRESHOLDS[('typesafe', '*')]
AUTO_GATE = _JEV['gate']
AUTO_KEEP = _JEV['keep']
AUTO_RESCUE = _JEV['rescue']


def limits(mode):
    """Thresholds for the configured provider and checkpoint (Jev's unless provider is laya)."""
    return client.thresholds(mode.get('provider'), mode.get('model'))


def auto_context(store, harness, query, context, *, budget_chars, timeout_cap=2.0, transport=None, project=None):
    """Per-turn hook path: strict local matches plus a loose lexical pool, judged in one call.

    The strict matcher wants two shared words, so a paraphrased question finds nothing. The
    loose pool passes the same visibility, trust, freshness and supersession gates; only the
    word-overlap bar is lower, and a loose record enters only above AUTO_RESCUE. Any failure
    or doubt returns the strict result untouched.
    """
    mode = client.inspect_config(store.state_dir)
    if (not mode['valid'] or mode['mode'] == 'off' or 'auto_context' not in mode['features']
            or not isinstance(query, str) or len(query.strip()) < AUTO_MIN_CHARS):
        return context
    strict = context.get('records') or []
    if any(r.get('visibility') == 'private' for r in strict):
        return context
    eligible = _eligible(store, project)
    valid_strict = [eligible[r['id']] for r in strict if r['id'] in eligible]
    if _signature(strict) != _signature(valid_strict):
        context = store.context_for(harness, query, project=project, strict=True, budget_chars=budget_chars)
        strict = context['records']
    known = {r['id'] for r in strict}
    loose = [r for r in store.candidates(query, project=project, limit=AUTO_POOL * 2)
             if r['id'] not in known and not str(r.get('source', '')).startswith(store.STRICT_EXCLUDE)]
    # Local-only records get local eligibility, but consume no remote-candidate slot.
    union = _interleave([strict, loose])
    records = [r for r in union if remote_allowed(r)][:AUTO_POOL]
    records += [r for r in strict if not remote_allowed(r)][:AUTO_LIMIT]
    keys = {'k' + str(i): r for i, r in enumerate(records) if str(r.get('text', '')).strip() and remote_allowed(r)}
    if not keys:
        return context
    before = _signature(records)
    cards = [dict(id=k, title=str(r.get('title', ''))[:160], excerpt=r['text'][:AUTO_EXCERPT],
                  project=str(r.get('project') or ''), status=str(r.get('status') or ''),
                  updated_at=str(r.get('updated_at') or '')) for k, r in keys.items()]
    outcome = dict(event='auto_context', mode=mode['mode'], strict=len(strict), loose=len(records) - len(strict))
    try:
        _safe(store, [query, cards])
    except (ValueError, OSError):
        client.log_event(store.state_dir, dict(outcome, outcome='skipped_sensitive'))
        return context
    advice = client.evaluate(store.state_dir, query[:AUTO_PROMPT], cards, scope='project:' + project if project else 'auto', purpose='auto_context',
                             source_versions={k: _signature([r])[r['id']] for k, r in keys.items()},
                             transport=transport, timeout_cap=timeout_cap)
    # Reapply index-only permissions/scope/supersession too, even on transport failure.
    current = _eligible(store, project)
    surviving = [current[r['id']] for r in records if r['id'] in current]
    if before != _signature(surviving) or not _fresh(store, records):
        return store.context_for(harness, query, project=project, strict=True, budget_chars=budget_chars)
    if client.inspect_config(store.state_dir) != mode:
        return context
    scores = advice['scores']
    if advice['degraded'] or advice['mode'] == 'off' or not scores:
        return context
    bar = limits(mode)
    passed = scores['topical'] >= bar['gate']
    by_record = {r['id']: scores[k] for k, r in keys.items()}
    keep = [r for r in records if passed and by_record.get(r['id'], 1.0 if r['id'] in known else 0.0)
            >= (bar['keep'] if r['id'] in known else bar['rescue'])]
    dropped = len([r for r in strict if r not in keep])
    rescued = len([r for r in keep if r['id'] not in known])
    # Laya never reaches `on` (client.SHADOW_ONLY); unmeasured thresholds would only log as well.
    client.log_event(store.state_dir, dict(outcome, outcome='scored' if bar['verified'] else 'scored_unverified',
                                           gate_passed=passed, dropped=dropped, rescued=rescued))
    if advice['mode'] != 'on' or not bar['verified'] or not (dropped or rescued):
        return context
    if not _fresh(store, records) or client.inspect_config(store.state_dir) != mode:
        return context
    ranked_remote = sorted([r for r in keep if remote_allowed(r)],
                           key=lambda r: (-by_record.get(r['id'], 0), r['id']))
    ranked_local = [r for r in keep if not remote_allowed(r) and r['id'] in known]
    packed = pack_context(_interleave([ranked_remote, ranked_local]), AUTO_LIMIT, budget_chars)
    selected_ids = {r['id'] for r in packed['records']}
    # Report delivered membership changes, not pre-budget model intentions.
    result = dict(context, **packed)
    result['jev'] = dict(dropped=len(known - selected_ids), added=len(selected_ids - known))
    return result


def _eligible(store, project):
    # Existing runtime gates enforce scope, visibility, trust, freshness and supersession.
    eligible = store._retrieve('', project=project, audience='internal',
                               limit=100000, budget_chars=10000000, snapshot=True)['records']
    return {r['id']: r for r in eligible if not r.get('text_truncated')}


def _verified_evidence(store, evidence, project, by_id=None):
    by_id = _eligible(store, project) if by_id is None else by_id
    refs = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'record_id', 'source_sha256', 'quote'}:
            raise ValueError('invalid_evidence_fields')
        record = by_id.get(item['record_id'])
        quote = item['quote']
        if (record is None or item['source_sha256'] != record['source_sha256']
                or not isinstance(quote, str) or not quote.strip() or quote not in record['text']):
            raise ValueError('evidence_not_current_or_exact')
        raw = (store.vault_root / store._source(record['source'])).read_text(encoding='utf-8')
        if quote not in raw:
            raise ValueError('evidence_not_in_source')
        refs.append(record)
    if not _fresh(store, refs):
        raise ValueError('evidence_changed')
    return refs


def review_candidate(store, proposal, *, project, transport=None):
    """Judge exact source evidence, without ingesting or approving a proposal."""
    if not isinstance(proposal, dict) or set(proposal) != {'status', 'project', 'claim', 'evidence'}:
        raise ValueError('invalid_proposal_fields')
    proposal = json.loads(json.dumps(proposal, ensure_ascii=False))
    if len(json.dumps(proposal, ensure_ascii=False)) > 24000:
        raise ValueError('proposal_too_large')
    if (proposal['status'] != 'proposed' or not isinstance(project, str) or not project.strip()
            or proposal['project'] != project or not isinstance(proposal['claim'], str)
            or not proposal['claim'].strip()):
        raise ValueError('proposal_scope_or_status_invalid')
    evidence = proposal['evidence']
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ValueError('proposal_requires_evidence')

    refs = _verified_evidence(store, evidence, project)
    mode = client.inspect_config(store.state_dir)
    enabled = mode['valid'] and mode['mode'] != 'off' and 'review' in mode.get('features', [])
    if enabled and not all(remote_allowed(r) for r in refs):
        return dict(status='advisory_only', approved=False, memory_written=False,
                    jev=_clear({'mode': mode['mode']}, 'source_local_only'))
    if enabled:
        _safe(store, proposal)
    versions = _signature(refs)
    contexts = []
    if enabled:
        try:
            contexts = _context(refs, evidence, context_limit(mode))
            _safe(store, contexts)
        except (ValueError, OSError):
            return dict(status='advisory_only', approved=False, memory_written=False,
                        jev=_clear({'mode': mode['mode']}, 'source_context_unavailable'))
    card = dict(id='proposal', title='Unapproved proposal', scope='project:' + project, domains=[],
                statement=json.dumps({'stored_claim': proposal['claim'],
                                      'evidence_quotes': [e['quote'] for e in evidence], 'source_context': contexts}, ensure_ascii=False))
    result = client.evaluate(store.state_dir, proposal['claim'], [card], scope='project:' + project,
                             source_versions=versions, purpose='evidence_review',
                             facets=['Do the exact quotes support the entire claim in its original scope?'],
                             transport=transport)
    try:
        now = _verified_evidence(store, evidence, project)
        if versions != _signature(now):
            raise ValueError('evidence_changed')
        if client.inspect_config(store.state_dir) != mode:
            raise ValueError('configuration_changed')
    except (ValueError, OSError):
        result = _clear(result, 'source_or_configuration_changed')
    return dict(status='advisory_only', approved=False, memory_written=False, jev=result)


BATCH = 8            # claims per request; hard items weaken late in a longer list
CONTEXT_CHARS = 400  # retained public constant; new checks require complete bounded context
CONTEXT_LIMIT = 4000  # Jev; a provider with a smaller window reports context_limit
CONFIDENCE_GATE = _JEV['confidence']
VERDICTS = dict(supports='supported', contradicts='contradicted', says_nothing='insufficient')


def source_context(record):
    """Complete indexed text plus canonical scope/lifecycle metadata, never a summary."""
    value = dict(text=record['text'], metadata={k: record[k] for k in
                 ('project', 'status', 'kind', 'updated_at') if k in record})
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _context(refs, citations, limit=CONTEXT_LIMIT):
    """A cancelled status or far-away correction must not disappear around a quote."""
    windows = list(dict.fromkeys(source_context(r) for r in refs))
    if sum(len(text) for text in windows) > limit:
        raise ValueError('source_context_incomplete')
    return windows


def context_limit(mode):
    return mode.get('context_limit') or CONTEXT_LIMIT


def _calibration(result, mode):
    """Mark decisions made with thresholds nobody measured for this provider."""
    if mode.get('valid') and mode.get('mode') != 'off' and not limits(mode)['verified']:
        result['calibration'] = 'unverified_for_provider'
    return result


def verify_answer(store, claims, *, project, transport=None):
    """Advisory claim checks; exact local evidence gates precede remote scoring."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError('advisor_requires_explicit_project')
    if not isinstance(claims, list) or not 1 <= len(claims) <= 20:
        raise ValueError('claims_limit_1_20')
    raw = json.dumps(claims, ensure_ascii=False)
    if len(raw) > 32000:
        raise ValueError('claims_too_large')
    claims = json.loads(raw)
    mode = client.inspect_config(store.state_dir)
    enabled = mode['valid'] and mode['mode'] != 'off' and 'answer' in mode.get('features', [])
    if enabled:
        _safe(store, claims)
    # Validate the whole request before any network call.
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != {'text', 'citations'}
                or not isinstance(claim['text'], str) or not claim['text'].strip()
                or not isinstance(claim['citations'], list) or len(claim['citations']) > 8):
            raise ValueError('invalid_claim')
        for cite in claim['citations']:
            if (not isinstance(cite, dict) or set(cite) != {'record_id', 'source_sha256', 'quote'}
                    or any(not isinstance(v, str) or not v.strip() for v in cite.values())):
                raise ValueError('invalid_citation')
    results, items, snapshots = [], {}, {}
    by_id = _eligible(store, project)  # one full-index pass per phase, not per claim
    for index, claim in enumerate(claims):
        item = dict(index=index, verdict='insufficient', mechanical_verified=False, diagnostics=[])
        results.append(item)
        if not claim['citations']:
            item['diagnostics'] = ['citation_missing']
            continue
        try:
            refs = _verified_evidence(store, claim['citations'], project, by_id)
        except (ValueError, OSError):
            item['diagnostics'] = ['citation_not_current_or_exact']
            continue
        item['mechanical_verified'] = True
        if not enabled:
            item.update(verdict='uncertain', diagnostics=['semantic_off'])
            continue
        if not all(remote_allowed(r) for r in refs):
            item.update(verdict='uncertain', diagnostics=['source_local_only'])
            continue
        try:
            context = _context(refs, claim['citations'], context_limit(mode))
        except ValueError:
            item.update(verdict='uncertain', diagnostics=['source_context_incomplete'])
            continue
        try:
            _safe(store, context)
        except ValueError:
            # The surrounding text goes to the provider too, so it passes the same gate.
            item.update(verdict='degraded', diagnostics=['context_sensitive'])
            continue
        snapshots[index] = _signature(refs)
        items[index] = dict(id='k' + str(index), claim=claim['text'],
                            quotes=[c['quote'] for c in claim['citations']], context=context)
    if items:
        mode = client.inspect_config(store.state_dir)

        def ask(indexes):
            advice = client.evaluate(store.state_dir, '', [items[i] for i in indexes], scope='project:' + project,
                                     source_versions={str(i): snapshots[i] for i in indexes},
                                     purpose='answer_check', transport=transport)
            if 'budget_exceeded' in advice.get('diagnostics', []) and len(indexes) > 1:
                # The budget is checked before any network call; halve and retry.
                half = len(indexes) // 2
                return {**ask(indexes[:half]), **ask(indexes[half:])}
            return {i: advice for i in indexes}
        order = list(items)
        # A local single-worker server (laya) would only queue parallel batches.
        with ThreadPoolExecutor(max_workers=1 if mode.get('provider') == 'laya' else 4) as pool:
            advices = {}
            for part in pool.map(ask, [order[i:i + BATCH] for i in range(0, len(order), BATCH)]):
                advices.update(part)
        try:
            by_id = _eligible(store, project)
        except (ValueError, OSError):
            by_id = {}
        for index, versions in snapshots.items():
            item, advice = results[index], advices[index]
            try:
                refs = _verified_evidence(store, claims[index]['citations'], project, by_id)
                if versions != _signature(refs):
                    raise ValueError('evidence_changed')
                if client.inspect_config(store.state_dir) != mode:
                    raise ValueError('configuration_changed')
            except (ValueError, OSError):
                item.update(verdict='degraded', mechanical_verified=False, diagnostics=['source_or_configuration_changed'])
                continue
            if advice.get('degraded'):
                item.update(verdict='degraded', diagnostics=advice.get('diagnostics', []))
            elif advice.get('mode') != 'on':
                item.update(verdict='uncertain', diagnostics=['semantic_' + advice.get('mode', 'unavailable')])
            else:
                relation = advice['relations']['k' + str(index)]
                item.update(relation=relation['choice'], confidence=relation['confidence'])
                if relation['confidence'] >= limits(mode)['confidence']:
                    item['verdict'] = VERDICTS[relation['choice']]
                else:
                    item.update(verdict='uncertain', diagnostics=['low_confidence'])
    return _calibration(dict(status='advisory_only', claims=results, approved=False,
                             memory_written=False, rewrites=False), mode)
