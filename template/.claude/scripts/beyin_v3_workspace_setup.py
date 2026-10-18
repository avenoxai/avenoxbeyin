"""Small read-only setup adapter; configuration and memory keep their own writers."""
import copy
import json
import os
import posixpath
from pathlib import Path
import re

from beyin_v3_workspaces import (Registry, LocalAuthority, WorkspaceError, envelope,
    content_hash, safe_path, validate_document)
from beyin_v3_builder import plan, _failure


def _result(artifacts):
    result = envelope('OperationResult', dict(operation_id='workspace-setup', operation_status='planned',
        workspace_health='verification_pending', parts=[], capabilities=[], verification=[],
        issues=[{'code': 'setup_artifacts', 'detail': json.dumps(artifacts, ensure_ascii=False)}],
        next_action='Review the exact plan and source scope; obtain authority before applying.'))
    validate_document(result)
    return result, 0


def _root(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise WorkspaceError('invalid_source_root')
    path = Path(value)
    # Reject aliases and every redirected ancestor before metadata discovery.
    if '..' in path.parts or str(path.resolve()) != str(path):
        raise WorkspaceError('redirected_path')
    safe_path(Path(path.anchor), path.relative_to(path.anchor).as_posix())
    if not path.is_dir():
        raise WorkspaceError('source_unavailable')
    return path


def _sources(value):
    if not isinstance(value, list) or len(value) > 32:
        raise WorkspaceError('invalid_sources')
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'path', 'name'} or not isinstance(item['name'], str) or not item['name'].strip():
            raise WorkspaceError('invalid_sources')
        result.append((_root(item['path']), item['name']))
    if len({str(p) for p, _ in result}) != len(result):
        raise WorkspaceError('invalid_duplicate_source')
    return result


def _catalog(payload):
    if set(payload) - {'sources', 'max_depth', 'max_items'}:
        raise WorkspaceError('invalid_setup_request')
    roots = _sources(payload['sources'])
    depth, limit = payload.get('max_depth', 2), payload.get('max_items', 200)
    if type(depth) is not int or not 0 <= depth <= 4 or type(limit) is not int or not 1 <= limit <= 500:
        raise WorkspaceError('invalid_catalog_limits')
    rows, truncated = [], False
    # scandir is lazy: directory size cannot allocate an unbounded list. The
    # shared resolver's safe_path rejects symlinks/reparse points; no extraction.
    pending = [(root, root, 0, label) for root, label in reversed(roots)]
    visited = 0
    while pending:
        root, directory, level, label = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > limit:
                    truncated = True
                    pending.clear()
                    break
                if entry.is_symlink():
                    continue
                name = Path(entry.path).relative_to(root).as_posix()
                try:
                    path = safe_path(root, name)
                    info = path.stat()
                    is_dir = path.is_dir()
                    if not (is_dir or path.is_file()):
                        continue
                except (ValueError, OSError):
                    continue
                rows.append(dict(root=str(root), source_name=label, relative_path=name,
                    kind='directory' if is_dir else 'file', bytes=None if is_dir else info.st_size,
                    modified_ns=info.st_mtime_ns))
                if is_dir:
                    if level < depth:
                        pending.append((root, path, level + 1, label))
                    else:
                        truncated = True
        if visited > limit:
            break
    return _result({'catalog': rows, 'truncated': truncated})


def _prepare(vault, state, payload):
    required = {'workspace_id', 'name', 'purpose', 'sources'}
    if not required <= set(payload) or set(payload) - required - {'homepage', 'note_roots', 'registry_id', 'machine_id', 'authority_ref'}:
        raise WorkspaceError('invalid_setup_request')
    wid = payload['workspace_id']
    if not isinstance(wid, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', wid):
        raise WorkspaceError('invalid_workspace_id')
    for key in ('name', 'purpose'):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise WorkspaceError('invalid_setup_request')
    roots = _sources(payload['sources'])
    registry = Registry(vault, state)
    existing = registry.snapshot()
    registry_id = payload.get('registry_id', (existing or {}).get('registry_id', 'local'))
    bindings, _ = registry.bindings(existing)
    machine = payload.get('machine_id') or LocalAuthority(registry)._load()['machine_id'] or 'local-' + content_hash(str(Path(state).resolve()))[:24]
    authority = payload.get('authority_ref', 'setup-review')
    descriptors = {s['id']: s for s in (existing or {}).get('sources', [])}
    profile = next((p for p in (existing or {}).get('profiles', []) if p['id'] == wid), None)
    if profile is None:
        profile = dict(id=wid, revision=0, name=payload['name'], purpose=payload['purpose'], lifecycle='active',
            source_ids=[], note_roots=['notes/workspaces'], working_rules=[],
            memory_policy={'prefer_update_existing': True, 'new_note': 'distinct_only', 'automatic_learning': 'enabled'},
            access_policy={'cross_workspace': 'explicit_grant', 'external_sources': 'read_only', 'automatic_audience': 'internal'},
            record_types=[], relation_types=[], workflows=[], extensions={})
    is_new = profile['revision'] == 0
    profile = copy.deepcopy(profile)
    old = copy.deepcopy(profile)
    homepage = payload.get('homepage', profile['extensions'].get('setup_homepage', 'notes/workspaces/' + wid + '.md'))
    safe_path(Path(vault), homepage)
    profile.update(name=payload['name'], purpose=payload['purpose'])
    unresolved = []
    if 'note_roots' in payload:
        profile['note_roots'] = payload['note_roots']
    elif 'homepage' in payload:
        parent = posixpath.dirname(homepage)
        if not parent:
            raise WorkspaceError('invalid_homepage_note_root')
        if is_new:
            # The explicitly requested page's narrowest directory is visible
            # in the exact profile plan; no authority record is created here.
            profile['note_roots'] = [parent]
        elif not any(homepage.startswith(r.rstrip('/') + '/') for r in profile['note_roots']):
            unresolved.append('homepage_note_root_review:' + parent)
    if safe_path(Path(vault), homepage).exists():
        unresolved.append('homepage_scope_adoption_review')
    profile['extensions']['setup_homepage'] = homepage
    requested = [(Path(vault).resolve(), 'Workspace memory')] + roots
    sources, requests, ids = [], [], []
    for root, label in requested:
        kind = 'vault' if root == Path(vault).resolve() else 'directory'
        match = next((sid for sid, binding in bindings.items() if binding['locator'] == str(root)
                      and descriptors[sid]['kind'] == kind), None)
        sid = match or ('memory-' if kind == 'vault' else 'source-') + content_hash(str(root))[:24]
        if sid in ids:
            continue
        ids.append(sid)
        sources.append(copy.deepcopy(descriptors.get(sid, {'id': sid, 'kind': kind, 'label': label})))
        previous = bindings.get(sid)
        grants = copy.deepcopy(previous['grants']) if previous else []
        for access in (('read', 'write_memory') if kind == 'vault' else ('read',)):
            grant = {'workspace_id': wid, 'access': access}
            if grant not in grants:
                grants.append(grant)
        binding = dict(source_id=sid, machine_id=previous['machine_id'] if previous else machine,
            revision=previous['revision'] if previous else 1, locator=str(root), grants=grants,
            authority_ref=previous['authority_ref'] if previous else authority)
        if previous and binding != previous:
            binding['revision'] += 1
        requests.append(binding)
    profile['source_ids'] = ids
    if profile != old:
        profile['revision'] += 1
    spec = envelope('WorkspaceSpec', dict(spec_id='setup-' + content_hash(wid)[:24], user_goal=payload['purpose'],
        desired_profile=profile, sources=sources, binding_requests=requests, adopt_record_refs=[],
        required_capabilities=[], unresolved=unresolved, authority_ref=authority))
    validate_document(spec)
    document, preview = plan(vault, state, spec, registry_id=registry_id, capabilities={})
    return _result(dict(spec=spec, plan=document, preview=preview, homepage=homepage))


def execute(vault, state, action, payload):
    """Return the existing OperationResult envelope and G0 exit code; no implicit consent."""
    try:
        if not isinstance(payload, dict):
            raise WorkspaceError('invalid_setup_request')
        if action == 'catalog':
            return _catalog(payload)
        if action == 'prepare':
            return _prepare(vault, state, payload)
        if action == 'organize' and set(payload) == {'intent', 'idempotency_key'}:
            from beyin_v3_workspace_context import WorkspaceRuntime
            from beyin_v3_workspace_views import organize
            runtime = WorkspaceRuntime(vault, state, readers={})
            result = organize(runtime, payload['intent'], payload['idempotency_key'])
            return result, 5 if result['data']['workspace_health'] == 'verification_pending' else 0
        raise WorkspaceError('invalid_setup_request')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return _failure(exc)
