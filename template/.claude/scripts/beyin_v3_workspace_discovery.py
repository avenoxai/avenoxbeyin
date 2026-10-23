"""Bounded, read-only evidence for the active agent's workspace design.

A catalog is metadata, never permission to extract its files. Only explicit
selectors with *existing* workspace authority reach the shared source resolver.
Source excerpts are data: they cannot select a workspace, approve a grant,
adopt a record or change the user-proposed design. No model or probe is run.
"""
import copy
from pathlib import Path

from beyin_v3_sources import SourceResolver
from beyin_v3_workspaces import (Registry, WorkspaceError, content_hash, envelope,
    local_root_matches, safe_path, _valid_local_locator, validate_document)

DESIGN_FIELDS = frozenset({'record_types', 'relation_types', 'working_rules', 'workflows'})
MAX_EVIDENCE = 16


def _bounded(value, low, high, code):
    if type(value) is not int or not low <= value <= high:
        raise WorkspaceError(code)
    return value


def _text(value, code):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise WorkspaceError(code)
    return value


def _no_memory(payload):
    value = payload.get('no_memory', False)
    if type(value) is not bool:
        raise WorkspaceError('invalid_no_memory')
    return value


def _matches(registry, snapshot, workspace_id=None, name=None, roots=()):
    """Report exact technical matches; never choose by source prose or keywords."""
    bindings, _ = registry.bindings(snapshot)
    result = []
    for profile in (snapshot or {}).get('profiles', []):
        reasons = []
        if profile['id'] == workspace_id:
            reasons.append('workspace_id')
        if name and profile['name'].casefold() == name.casefold():
            reasons.append('name')
        if any(sid in profile['source_ids'] and any(local_root_matches(binding['locator'], root) for root in roots)
               for sid, binding in bindings.items()):
            reasons.append('source_root')
        if reasons:
            result.append(dict(workspace_id=profile['id'], name=profile['name'],
                revision=profile['revision'], lifecycle=profile['lifecycle'], reasons=reasons,
                next_action='review_existing_workspace'))
    return result[:32], len(result) > 32


def discover(vault, state, payload, *, readers=None):
    """Return metadata and bounded fresh excerpts; never persist context/cache.

    ``sources`` explicitly scopes metadata traversal only. ``evidence`` is a
    list of {source_id, relative_path, content_sha256?}; each selector requires
    workspace_id + grant_ref and is checked independently by SourceResolver.
    A supplied digest is a freshness precondition, not an authorization token.
    Document readers must be supplied by the caller; none are probed here.
    """
    allowed = {'sources', 'goal', 'workspace_id', 'name', 'grant_ref', 'evidence',
        'max_depth', 'max_items', 'budget_chars', 'max_source_bytes', 'no_memory', 'audience'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise WorkspaceError('invalid_discovery_request')
    budget = _bounded(payload.get('budget_chars', 4000), 0, 12000, 'invalid_discovery_budget')
    byte_limit = _bounded(payload.get('max_source_bytes', 262144), 1, 1048576, 'invalid_discovery_budget')
    evidence = payload.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise WorkspaceError('invalid_discovery_evidence')
    for selector in evidence:
        if (not isinstance(selector, dict) or not {'source_id', 'relative_path'} <= set(selector)
                or set(selector) - {'source_id', 'relative_path', 'content_sha256'}):
            raise WorkspaceError('invalid_discovery_evidence')
        # Reuse SourceRef validation even before access; reject traversal and bad hashes.
        validate_document(envelope('SourceRef', dict(selector,
            content_sha256=selector.get('content_sha256', '0' * 64), fragment={'kind': 'whole'},
            extractor={'id': 'text', 'version': '1', 'settings_sha256': content_hash(b'{}')})))
    audience = payload.get('audience', 'internal')
    if audience not in ('internal', 'private', 'public'):
        raise WorkspaceError('invalid_discovery_audience')
    for field in ('goal', 'workspace_id', 'name', 'grant_ref'):
        if field in payload:
            _text(payload[field], 'invalid_discovery_request')
    result = dict(catalog=[], evidence=[], existing_matches=[], matches_truncated=False,
        suggestion='needs_design', missing_information=[], issues=[], truncated=False,
        capabilities=[{'name': 'metadata_catalog', 'state': 'ready'},
            {'name': 'authorized_text_evidence', 'state': 'ready'},
            {'name': 'document_evidence', 'state': 'not_tested',
             'detail': 'Only explicitly configured readers are used; no capability probe runs.'}],
        source_content_role='untrusted_data', writes_performed=False)
    if _no_memory(payload):
        result['missing_information'] = ['no_memory: source and registry discovery skipped']
        result['suggestion'] = 'discovery_disabled'
        return result
    # Keep the single bounded metadata scanner; its list never feeds extraction.
    from beyin_v3_workspace_setup import _catalog
    import json
    catalog_document, _ = _catalog({key: payload[key] for key in ('sources', 'max_depth', 'max_items') if key in payload}
        if 'sources' in payload else dict(sources=[], **{key: payload[key] for key in ('max_depth', 'max_items') if key in payload}))
    catalog = json.loads(next(issue['detail'] for issue in catalog_document['data']['issues'] if issue['code'] == 'setup_artifacts'))
    result.update(catalog=catalog['catalog'], truncated=catalog['truncated'])
    registry = Registry(vault, state)
    snapshot = registry.snapshot()
    bindings, binding_hash = registry.bindings(snapshot)
    roots = [Path(s['path']).resolve() for s in payload.get('sources', [])]
    result['existing_matches'], result['matches_truncated'] = _matches(registry, snapshot,
        payload.get('workspace_id'), payload.get('name'), roots)
    result['suggestion'] = 'review_existing_workspace' if result['existing_matches'] else 'propose_new_workspace'
    if not payload.get('goal'):
        result['missing_information'].append('user_goal')
    if not evidence:
        result['missing_information'].append('source_evidence_not_requested: catalog is metadata only')
    if evidence and not (payload.get('workspace_id') and payload.get('grant_ref')):
        result['missing_information'].append('existing_workspace_authority_required_for_evidence')
        result['issues'].append({'code': 'scope_denied'})
        return result
    resolver = SourceResolver(registry, readers={} if readers is None else readers, cache_write=False,
        max_source_bytes=byte_limit)
    used_bytes, remaining = 0, budget
    for index, selector in enumerate(evidence):
        if remaining <= 0 or used_bytes >= byte_limit:
            result['truncated'] = True
            break
        try:
            wid, grant = payload['workspace_id'], payload['grant_ref']
            # Preflight size before the shared vault resolver parses frontmatter.
            # This gate adds no access: the full resolver still independently
            # checks privacy, trust, record scope and current authority below.
            profile = next((p for p in (snapshot or {}).get('profiles', []) if p['id'] == wid), None)
            binding = bindings.get(selector['source_id'])
            if (not profile or profile['lifecycle'] != 'active' or not binding
                    or selector['source_id'] not in profile['source_ids']
                    or not resolver.authority.allows(grant, binding, wid, audience=audience)):
                raise WorkspaceError('scope_denied')
            root = Path(binding['locator'])
            if not _valid_local_locator(root):
                raise WorkspaceError('source_unavailable')
            path = safe_path(root, selector['relative_path'], existing=True)
            size = path.stat().st_size
            if used_bytes + size > byte_limit:
                result['issues'].append({'code': 'discovery_source_budget', 'selector_index': index})
                result['truncated'] = True
                continue
            resolver.max_source_bytes = byte_limit - used_bytes
            extracted = resolver.extract(wid, selector['source_id'], selector['relative_path'], grant, audience=audience)
            used_bytes += extracted['source_bytes']
            if selector.get('content_sha256', extracted['source_sha256']) != extracted['source_sha256']:
                raise WorkspaceError('stale_source')
            if extracted['state'] != 'ready':
                result['issues'].append({'code': extracted['state'], 'selector_index': index})
            for chunk in extracted['chunks']:
                text = chunk['text'][:remaining]
                if not text:
                    if chunk['text']:
                        result['truncated'] = True
                    break
                result['evidence'].append(dict(source_ref=chunk['source_ref'], excerpt=text,
                    excerpt_truncated=len(text) < len(chunk['text']), role='untrusted_source_data'))
                remaining -= len(text)
                if len(text) < len(chunk['text']):
                    result['truncated'] = True
        except (WorkspaceError, OSError) as exc:
            result['issues'].append({'code': getattr(exc, 'code', 'source_unavailable'), 'selector_index': index})
    # Recheck every emitted reference after all reads: neither an earlier
    # successful extraction nor its hash preserves revoked authority.
    fresh_evidence = []
    resolver.max_source_bytes = byte_limit
    for item in result['evidence']:
        try:
            resolver.read(item['source_ref'], payload['workspace_id'], payload['grant_ref'], audience=audience)
            fresh_evidence.append(item)
        except (WorkspaceError, OSError) as exc:
            result['issues'].append({'code': getattr(exc, 'code', 'source_unavailable')})
    latest = registry.snapshot()
    if ((snapshot or {}).get('snapshot_sha256') != (latest or {}).get('snapshot_sha256')
            or registry.bindings(latest)[1] != binding_hash):
        fresh_evidence = []
        result['issues'].append({'code': 'snapshot_changed'})
    result['evidence'] = fresh_evidence
    if not fresh_evidence:
        result['missing_information'].append('no_fresh_authorized_evidence: content claims remain unknown')
    return result


def design(vault, state, payload):
    """Validate the active agent's user-shaped design, without applying it.

    Partial design is intentional: only supplied sections are returned for
    prepare to overlay. Existing sections remain intact. This validates the
    current G0 profile contract, not the user's domain semantics or readiness.
    """
    required = {'workspace_id', 'name', 'purpose', 'design'}
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {'no_memory'}:
        raise WorkspaceError('invalid_design_request')
    for field in ('workspace_id', 'name', 'purpose'):
        _text(payload[field], 'invalid_design_request')
    proposed = payload['design']
    if not isinstance(proposed, dict) or set(proposed) - DESIGN_FIELDS:
        raise WorkspaceError('invalid_profile_design')
    no_memory = _no_memory(payload)
    registry = Registry(vault, state)
    snapshot = None if no_memory else registry.snapshot()
    current = next((p for p in (snapshot or {}).get('profiles', []) if p['id'] == payload['workspace_id']), None)
    profile = copy.deepcopy(current) if current else dict(id=payload['workspace_id'], revision=1,
        name=payload['name'], purpose=payload['purpose'], lifecycle='draft', source_ids=[], note_roots=['notes/workspaces'],
        working_rules=[], record_types=[], relation_types=[], workflows=[], extensions={},
        memory_policy={'prefer_update_existing': True, 'new_note': 'distinct_only', 'automatic_learning': 'enabled'},
        access_policy={'cross_workspace': 'explicit_grant', 'external_sources': 'read_only', 'automatic_audience': 'internal'})
    profile.update(name=payload['name'], purpose=payload['purpose'], **copy.deepcopy(proposed))
    validate_document(envelope('WorkspaceProfile', profile))
    for section in ('record_types', 'working_rules', 'workflows'):
        ids = [item['id'] for item in profile[section]]
        if len(ids) != len(set(ids)):
            raise WorkspaceError('duplicate_design_id')
    if len(profile['relation_types']) != len(set(profile['relation_types'])):
        raise WorkspaceError('duplicate_design_id')
    matches, truncated = ([], False) if no_memory else _matches(registry, snapshot, payload['workspace_id'], payload['name'])
    missing = []
    if not profile['record_types']:
        missing.append('record_types: daily record structure is not designed')
    if not profile['working_rules']:
        missing.append('working_rules: user-specific operating rules are not designed')
    return dict(profile_design=copy.deepcopy(proposed), effective_profile=envelope('WorkspaceProfile', profile),
        existing_matches=matches, matches_truncated=truncated,
        suggestion='update_existing_workspace' if current else ('review_existing_workspace' if matches else 'propose_new_workspace'),
        missing_information=missing, capabilities=[
            {'name': 'profile_contract', 'state': 'ready', 'detail': 'Record fields, relations, rules and workflow structure validated.'},
            {'name': 'daily_workspace', 'state': 'not_tested', 'detail': 'Profile design is a proposal; source scope, Markdown home, activation and readback require setup.'},
            {'name': 'workflow_execution', 'state': 'not_tested', 'detail': 'Finite shared services only; semantic proposals return to the active agent. No scheduler is created.'}],
        writes_performed=False)


def preview(vault, state, payload, *, readers=None):
    """Preview an agent's reasoned need decision and exact, authorized record candidates.

    This is planning data, not an adoption or a writer intent. In particular a
    matching title, company or issue label cannot authorize an update.
    """
    if not isinstance(payload, dict) or set(payload) - {'decision', 'candidate_record_ids'} - {
            'sources', 'goal', 'workspace_id', 'name', 'grant_ref', 'evidence',
            'max_depth', 'max_items', 'budget_chars', 'max_source_bytes', 'no_memory', 'audience'}:
        raise WorkspaceError('invalid_discovery_request')
    decision = payload.get('decision')
    if (not isinstance(decision, dict) or set(decision) != {'kind', 'rationale'}
            or decision['kind'] not in ('new_workspace', 'extend_workspace', 'record_only')):
        raise WorkspaceError('invalid_need_decision')
    _text(decision['rationale'], 'invalid_need_decision')
    ids = payload.get('candidate_record_ids', [])
    if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(i, str) or not i or len(i) > 128 for i in ids):
        raise WorkspaceError('invalid_candidate_records')
    if len(ids) != len(set(ids)):
        raise WorkspaceError('invalid_candidate_records')
    request = {k: v for k, v in payload.items() if k not in ('decision', 'candidate_record_ids')}
    found = discover(vault, state, request, readers=readers)
    result = dict(discovery=found, decision=copy.deepcopy(decision), record_candidates=[],
                  review=[], proposed_operations=[], writes_performed=False)
    if found['suggestion'] == 'discovery_disabled':
        result['review'].append('no_memory')
        return result
    wid, grant = request.get('workspace_id'), request.get('grant_ref')
    matches = {m['workspace_id'] for m in found['existing_matches']}
    if found['matches_truncated']:
        result['review'].append('workspace_matches_truncated')
    if decision['kind'] == 'new_workspace':
        if matches:
            result['review'].append('existing_workspace_must_be_distinguished')
        else:
            result['proposed_operations'].append({'kind': 'prepare_workspace', 'state': 'review_required'})
    elif not wid or wid not in matches or not grant:
        result['review'].append('existing_workspace_and_authority_required')
    else:
        result['proposed_operations'].append({'kind': 'extend_workspace' if decision['kind'] == 'extend_workspace'
                                               else 'record_only', 'workspace_id': wid, 'state': 'preview_only'})
    if not ids and not found['evidence']:
        result['review'].append('candidate_records_not_selected')
        return result
    if (not wid or wid not in matches or not grant or
            (found['issues'] and (not ids or any(issue['code'] != 'scope_denied' for issue in found['issues'])))):
        result['review'].append('candidate_scope_or_source_review')
        return result
    # A read-only store never creates context tokens, cache entries, grants or
    # adoption state.
    from beyin_v3_workspace_context import WorkspaceRuntime, membership_hash, record_owner, scope_ids, _membership_state
    try:
        runtime = WorkspaceRuntime(vault, state, read_only=True)
    except ValueError as exc:
        if str(exc) != 'read-only runtime is not initialized':
            raise
        result['review'].append('record_index_unavailable')
        return result
    registry = runtime.registry
    snapshot = registry.snapshot()
    bindings, binding_hash = registry.bindings(snapshot)
    membership_before = membership_hash(registry)
    # A share preview examines candidates before target membership exists.
    # Preserve the store's freshness, trust and audience checks, then require
    # live target read and current owner write authority per candidate.
    audience = request.get('audience', 'internal')
    refs = [item['source_ref'] for item in found['evidence']]
    selected_ids = set(ids)
    selected_hashes = {ref['content_sha256'] for ref in refs}
    selected = [record for record in runtime.store._records()
                if record['id'] in selected_ids or record['source_sha256'] in selected_hashes]
    eligible, _ = runtime.store._eligible(audience, records=selected,
        workspace_policy=lambda record, audience: True)
    profiles = {p['id']: p for p in snapshot['profiles']}
    def candidate_allowed(record):
        if record.get('visibility') == 'private' or record.get('trust') == 'untrusted':
            return False
        path = safe_path(Path(vault), record['source'], existing=True)
        target = profiles.get(wid)
        if not target or target['lifecycle'] != 'active':
            return False
        if not any(sid in target['source_ids'] and
                local_root_matches(binding['locator'], registry.vault) and
                runtime.authority.allows(grant, binding, wid, audience=audience)
                for sid, binding in bindings.items()):
            return False
        if wid in scope_ids(registry, record):
            return True
        owner = record_owner(registry, record)
        if owner is None or (not record.get('workspace_id') and record['id'] not in _membership_state(registry)[1]):
            return True
        owner_profile = profiles.get(owner)
        if not owner_profile or owner_profile['lifecycle'] != 'active':
            return False
        for sid, binding in bindings.items():
            if sid not in owner_profile['source_ids'] or not local_root_matches(binding['locator'], registry.vault):
                continue
            try:
                resolved, _, _ = runtime.resolver.resolve(owner, sid, record['source'], grant,
                                                           access='write_memory')
                if resolved == path:
                    return True
            except WorkspaceError:
                continue
        return False
    by_id = {record['id']: record for record in eligible if candidate_allowed(record)}
    # Exact source identity or byte digest can nominate an existing canonical
    # record. Neither relation decides semantic equivalence or grants editing.
    matching_ids = [record['id'] for record in by_id.values() if any(
        ref['content_sha256'] == record['source_sha256'] for ref in refs)]
    if refs and not matching_ids and not ids:
        result['review'].append('no_exact_record_match')
    if len(matching_ids) > 32:
        result['review'].append('candidate_matches_truncated')
    ids = list(dict.fromkeys(ids + matching_ids[:32]))[:32]
    result['expected_membership_sha256'] = membership_before
    for record_id in ids:
        record = by_id.get(record_id)
        if record is None:
            result['review'].append('record_unavailable_or_denied:' + record_id)
            continue
        exact_source = any(ref['relative_path'] == record['source'] and
                           ref['content_sha256'] == record['source_sha256'] and
                           bindings.get(ref['source_id'], {}).get('locator') == str(Path(vault).resolve())
                           for ref in refs)
        exact_hash = any(ref['content_sha256'] == record['source_sha256'] for ref in refs)
        relation = 'exact_source' if exact_source else 'same_content_hash' if exact_hash else 'unconfirmed'
        result['record_candidates'].append(dict(record_id=record_id, workspace_id=wid,
            source=record['source'], source_sha256=record['source_sha256'], revision=record['revision'],
            relation=relation, action='review_for_update_or_membership' if relation != 'unconfirmed'
                                else 'review_relation'))
        if relation == 'unconfirmed':
            result['review'].append('record_relation_unconfirmed:' + record_id)
    latest = registry.snapshot()
    latest_profile = next((p for p in latest['profiles'] if p['id'] == wid), None)
    fresh_sources = True
    for ref in refs:
        try:
            runtime.resolver.read(ref, wid, grant, audience=request.get('audience', 'internal'))
        except (WorkspaceError, OSError):
            fresh_sources = False
    for candidate in result['record_candidates']:
        try:
            path = safe_path(Path(vault), candidate['source'], existing=True)
            if content_hash(path.read_bytes()) != candidate['source_sha256']:
                fresh_sources = False
        except (WorkspaceError, OSError, ValueError):
            fresh_sources = False
    if (latest['snapshot_sha256'] != snapshot['snapshot_sha256'] or
            registry.bindings(latest)[1] != binding_hash or membership_hash(registry) != membership_before or
            not fresh_sources or latest_profile is None or
            not any(b['source_id'] in latest_profile['source_ids'] and
                    runtime.authority.allows(grant, b, wid, audience=request.get('audience', 'internal'))
                    for b in registry.bindings(latest)[0].values())):
        result['record_candidates'] = []
        result['review'].append('authorization_or_snapshot_changed')
    return result
