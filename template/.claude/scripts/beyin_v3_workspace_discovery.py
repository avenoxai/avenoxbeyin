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
