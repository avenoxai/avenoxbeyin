"""Managed Markdown bodies, written only through canonical memory intents."""
import copy
import posixpath
from urllib.parse import quote
from pathlib import Path

from beyin_v3_workspaces import WorkspaceError, content_hash, envelope, safe_path, validate_document, local_root_matches
from beyin_v3_workspace_context import authorized_record_scope, scope_ids
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_sync import parse


def merge_block(body, workspace_id, content):
    """Preserve every character outside one exact marker pair; reject ambiguity."""
    start = '<!-- beyin-workspace:' + workspace_id + ':start -->'
    end = '<!-- beyin-workspace:' + workspace_id + ':end -->'
    # A near-match for this workspace must not silently create a second block.
    prefix = '<!-- beyin-workspace:' + workspace_id + ':'
    if body.count(prefix) != body.count(start) + body.count(end):
        raise WorkspaceError('managed_view_marker_conflict')
    if body.count(start) != body.count(end) or body.count(start) > 1:
        raise WorkspaceError('managed_view_marker_conflict')
    block = start + '\n' + content.rstrip('\n') + '\n' + end
    if start not in body:
        return body + ('\n' if body and not body.endswith('\n') else '') + block + '\n'
    left, right = body.index(start), body.index(end)
    for marker, at in ((start, left), (end, right)):
        if (at and body[at - 1] != '\n') or (at + len(marker) < len(body) and body[at + len(marker)] not in '\r\n'):
            raise WorkspaceError('managed_view_marker_conflict')
    if right < left:
        raise WorkspaceError('managed_view_marker_conflict')
    return body[:left] + block + body[right + len(end):]


def _label(value):
    # Record metadata is data, not Markdown controls/HTML instructions.
    return str(value).replace('\r', ' ').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;').replace('[', '\\[').replace(']', '\\]')


def _render(profile, records, sources, homepage, bindings):
    lines = ['## ' + _label(profile['name']), '', _label(profile['purpose']), '', '### Kaynaklar']
    for source in sources:
        if source['id'] in profile['source_ids']:
            binding = bindings.get(source['id'])
            label = _label(source['label'])
            lines.append('- [' + label + '](' + Path(binding['locator']).as_uri() + ')' if binding else '- ' + label)
    lines += ['', '### İşler ve notlar']
    for record in sorted(records, key=lambda r: (r.get('kind') != 'task', r['source'])):
        target = quote(posixpath.relpath(record['source'], posixpath.dirname(homepage)), safe='/.-_')
        label = _label(record.get('title') or record['id'])
        status = ' · ' + _label(record['status']) if record.get('status') else ''
        lines.append('- [' + label + '](' + target + ')' + status)
    if not records:
        lines.append('- Bu kapsamda görünür kayıt yok.')
    lines += ['', '### Görünüm durumu', 'Kanonik kayıtların yetkili kapsamından yenilendi; kaynak dosyalar yerinde korunur.']
    return '\n'.join(lines) + '\n'


def organize(runtime, intent_document, idempotency_key, *, _coordinated=False):
    """Use the original authorized context, rechecked for revocation and snapshot CAS.

    The original write is not repeated. Its old evidence SourceRefs are not read
    again: only newly generated view evidence is checked by the canonical writer.
    """
    validate_document(intent_document)
    if intent_document['type'] != 'MemoryWriteIntent':
        raise WorkspaceError('invalid_schema')
    original = intent_document['data']
    service = MemoryIntentService(runtime)
    context = service._context(original)
    return _organize_context(runtime, context, original['context_token'], idempotency_key,
        _coordinated=_coordinated)


def initialize(runtime, request_document, idempotency_key, *, before_apply=None):
    """Initialize a homepage from a fresh real request, without a pretend write.

    The callback may durably checkpoint the exact generated intent before the
    canonical writer runs, allowing its own journal to resume after interruption.
    This operation records no approval and never activates global runtime policy.
    """
    validate_document(request_document)
    if request_document['type'] != 'RequestContext':
        raise WorkspaceError('invalid_schema')
    request = request_document['data']
    if request['no_memory']:
        raise WorkspaceError('no_memory')
    # Homepage links are based on canonical vault records and approved source
    # labels, not extracted external-document contents. Keep normal query
    # contexts unchanged; the canonical writer still rechecks live authority.
    resolved = runtime.context(request_document, include_external_sources=False)['data']
    if resolved['state'] not in ('ready', 'partial') or resolved['workspace_id'] is None:
        raise WorkspaceError('workspace_context_required')
    context = dict(request=request, workspace_id=resolved['workspace_id'])
    return _organize_context(runtime, context, resolved['context_token'], idempotency_key,
        before_apply=before_apply)


def _organize_context(runtime, context, context_token, idempotency_key, *, _coordinated=False, before_apply=None):
    service = MemoryIntentService(runtime)
    wid = context['workspace_id']
    snapshot = runtime.registry.snapshot()
    profile = next(p for p in snapshot['profiles'] if p['id'] == wid)
    homepage = profile['extensions'].get('setup_homepage')
    if not isinstance(homepage, str):
        raise WorkspaceError('managed_view_not_configured')
    path = safe_path(runtime.registry.vault, homepage)
    bindings, binding_hash = runtime.registry.bindings(snapshot)
    vault_source = next((s['id'] for s in snapshot['sources'] if s['kind'] == 'vault' and s['id'] in profile['source_ids']
        and s['id'] in bindings and local_root_matches(bindings[s['id']]['locator'], runtime.registry.vault)), None)
    if vault_source is None:
        raise WorkspaceError('source_unbound')
    target_record = next((r for r in runtime.store._records() if r['source'] == homepage), None)
    # Authorize this exact destination before reading its body, even when the
    # original successful intent modified a different source/record.
    runtime.resolver.resolve(wid, vault_source, homepage, context['request']['grant_ref'],
        access='write_memory', audience=context['request']['audience'])
    if path.exists():
        runtime.resolver.resolve(wid, vault_source, homepage, context['request']['grant_ref'],
            access='read', audience=context['request']['audience'])
    # Cache the previously validated vault policy only for this eligibility
    # scan; the canonical writer still validates live grants before writing.
    with authorized_record_scope(runtime.registry,snapshot,bindings,wid,context['request']['grant_ref'],
                                 audience='internal'):
        # The shared homepage is internal even for an explicitly private request.
        records, stale_count = runtime.store._eligible('internal')
    if stale_count:
        raise WorkspaceError('stale_source')
    # A grant or registry update during the batched scan must fail closed,
    # including when the managed block is already unchanged (replay path).
    fresh = runtime.registry.snapshot()
    if fresh['snapshot_sha256'] != snapshot['snapshot_sha256']:
        raise WorkspaceError('snapshot_changed')
    if runtime.registry.bindings(fresh)[1] != binding_hash:
        raise WorkspaceError('snapshot_changed')
    if not runtime.authority.allows(context['request']['grant_ref'], bindings[vault_source], wid,
                                    access='read', audience='internal'):
        raise WorkspaceError('scope_denied')
    records = [r for r in records if wid in scope_ids(runtime.registry, r) and r['source'] != homepage
               and not r.get('generated') and r.get('kind') != 'receipt']
    if path.exists():
        if target_record is None or wid not in scope_ids(runtime.registry, target_record):
            raise WorkspaceError('managed_view_adoption_required')
        if target_record.get('visibility', 'internal') != 'internal' or target_record.get('trust') == 'untrusted':
            raise WorkspaceError('scope_denied')
        raw = path.read_bytes()
        if content_hash(raw) != target_record['source_sha256']:
            raise WorkspaceError('revision_conflict')
        _, body = parse(raw.decode('utf-8'))
    else:
        raw, body = None, ''
    visible_sources = [s for s in snapshot['sources'] if s['id'] in bindings and
        runtime.authority.allows(context['request']['grant_ref'], bindings[s['id']], wid, audience='internal')]
    merged = merge_block(body, wid, _render(profile, records, visible_sources, homepage, bindings))
    protected, _ = service.engine._protect(merged)
    if protected != merged:
        raise WorkspaceError('managed_view_body_preservation_conflict')
    if merged == body:
        return envelope('OperationResult', dict(operation_id='workspace-view', operation_status='replayed',
            workspace_health='verification_pending', parts=[{'id': 'managed-homepage', 'state': 'verified'}], capabilities=[],
            verification=[{'check': 'source_index_readback', 'state': 'passed', 'evidence_refs': []}], issues=[],
            next_action='Verify retrieval in a fresh session.'))
    target = dict(record_id=target_record['id'] if target_record else 'workspace-home-' + content_hash(wid)[:24],
        source_id=vault_source, relative_path=homepage,
        expected_revision=target_record['revision'] if target_record else None,
        expected_source_sha256=content_hash(raw) if raw is not None else None)
    view_intent = envelope('MemoryWriteIntent', dict(intent_id='view-' + content_hash(idempotency_key)[:24],
        workspace_id=wid, context_token=context_token, profile_revision=profile['revision'],
        binding_snapshot_sha256=binding_hash, idempotency_key=idempotency_key,
        reason='Refresh only the managed workspace Markdown block.',
        changes=[dict(id='managed-homepage', action='update_note' if target_record else 'create_note', target=target,
            body=merged, metadata_changes={} if target_record else {'title': profile['name']}, relation_changes=[], evidence_refs=[])]))
    if before_apply is not None:
        before_apply(view_intent)
    return service.apply(view_intent, _coordinated=_coordinated, _refresh_views=False, _preserve_body=True)


def refresh_after_apply(runtime, intent_document, operation_result, *, _coordinated=False):
    """Best effort view refresh, preserving the successful canonical-write receipt."""
    result = copy.deepcopy(operation_result)
    validate_document(result)
    if result['data']['operation_status'] not in ('applied', 'replayed'):
        return result
    intent = intent_document['data']
    try:
        snapshot = runtime.registry.snapshot()
        profile = next((p for p in snapshot['profiles'] if p['id'] == intent['workspace_id']), None)
        if not profile or 'setup_homepage' not in profile['extensions']:
            return result
        homepage = profile['extensions']['setup_homepage']
        # Explicit recursion guard even when a caller wires all memory writes.
        if any(c['target']['relative_path'] == homepage for c in intent['changes']):
            return result
        refreshed = organize(runtime, intent_document, 'view:' + intent['idempotency_key'], _coordinated=_coordinated)
        validate_document(refreshed)
        if refreshed['data']['operation_status'] not in ('applied', 'replayed') or any(
                p['state'] not in ('verified', 'skipped') for p in refreshed['data']['parts']):
            raise WorkspaceError('managed_view_verification_pending')
        result['data']['issues'] = [i for i in result['data']['issues'] if i['code'] != 'managed_view_stale']
        result['data']['issues'].extend(refreshed['data']['issues'])
    except (ValueError, OSError, KeyError, TypeError):
        result['data']['issues'].append({'code': 'managed_view_stale',
            'detail': 'Canonical write succeeded; Markdown refresh is unverified. Inspect its journal and refresh with a fresh authorized context.'})
        result['data']['next_action'] = 'Inspect the view refresh journal and retry with a fresh authorized context.'
    result['data']['workspace_health'] = 'verification_pending'
    validate_document(result)
    return result
