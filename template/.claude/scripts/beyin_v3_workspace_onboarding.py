"""Durable setup continuation; C1 approval and the canonical writer remain owners.

Staging configures nothing. Its compact approval requests are data for a trusted
active agent/UI that has actually obtained consent; they are never auto-approved.
Configuration completion, Markdown completion and native activation are separate.
"""
import copy
import json
import re
import sqlite3
from contextlib import contextmanager

from beyin_v3_workspaces import (Registry, LocalAuthority, WorkspaceError,
    canonical_bytes, content_hash, envelope, parse_document, safe_path, validate_document)
from beyin_v3_sync import atomic
from beyin_v3_builder import provisioner, _failure


def _artifacts(result):
    return json.loads(next(i['detail'] for i in result['data']['issues'] if i['code'] == 'setup_artifacts'))


def _result(artifacts, *, status='planned', parts=None, issues=None, next_action=None):
    result = envelope('OperationResult', dict(operation_id='workspace-onboarding', operation_status=status,
        workspace_health='verification_pending', parts=parts or [], capabilities=[], verification=[],
        issues=(issues or []) + [{'code': 'setup_artifacts', 'detail': json.dumps(artifacts, ensure_ascii=False)}],
        next_action=next_action or 'Gösterilen somut kapsam ve kaydedilmiş plan için kullanıcı onayını bir kez al.'))
    validate_document(result)
    return result


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise WorkspaceError('invalid_preparation_id')
    return value


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise WorkspaceError('bounded_idempotency_key_required')
    return content_hash(value)


@contextmanager
def _locked(state):
    """Serialize only this adapter; underlying writers retain their shared CAS lock."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = safe_path(state, 'workspace-onboarding-lock.sqlite3')
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute('BEGIN IMMEDIATE')
        yield
    except sqlite3.OperationalError as exc:
        raise WorkspaceError('busy') from exc
    finally:
        db.close()


def _approval_requests(registry, spec):
    """Describe reviewed deltas; preserve grant refs, old permissions and audiences.

    Never resurrect permissions for other workspaces merely because a binding
    still mentions them. C1 will reject a plan whose previous grant was revoked.
    """
    authority = LocalAuthority(registry)._load()
    wid = spec['desired_profile']['id']
    additions = {}
    machines = set()
    for binding in spec['binding_requests']:
        machines.add(binding['machine_id'])
        for grant in binding['grants']:
            if grant['workspace_id'] != wid:
                continue
            permission = dict(workspace_id=wid, source_id=binding['source_id'],
                locator=binding['locator'], access=grant['access'])
            for reference in {binding['authority_ref'], spec['authority_ref']}:
                entries = additions.setdefault(reference, [])
                if permission not in entries:
                    entries.append(permission)
    if len(machines) != 1:
        raise WorkspaceError('binding_machine_conflict')
    requests = []
    for reference, permissions in sorted(additions.items()):
        prior = authority['grants'].get(reference, {})
        merged = copy.deepcopy(prior.get('permissions', []))
        delta = [p for p in permissions if p not in merged]
        merged.extend(delta)
        audiences = list(prior.get('audiences', []))
        audience_delta = [] if 'internal' in audiences else ['internal']
        audiences.extend(audience_delta)
        requests.append(dict(reference=reference, machine_id=next(iter(machines)),
            permissions=merged, audiences=audiences, permission_delta=delta,
            audience_delta=audience_delta,
            preserve_current=True,
            expected_grant_sha256=content_hash(canonical_bytes(prior)) if prior else None))
    return requests


def stage(vault, state, payload):
    """Durably retain an exact read-only preparation; write only adapter state.

    setup.prepare remains entirely read-only. A new immutable preparation is a
    separate explicit operation, so the first human review can survive sessions.
    """
    from beyin_v3_workspace_setup import execute as setup_execute
    result, code = setup_execute(vault, state, 'prepare', payload)
    if code:
        return result, code
    prepared = _artifacts(result)
    registry = Registry(vault, state)
    spec = prepared['spec']['data']
    prepared['approval_requests'] = _approval_requests(registry, spec)
    prepared['scope'] = dict(workspace_id=spec['desired_profile']['id'],
        name=spec['desired_profile']['name'], purpose=spec['desired_profile']['purpose'],
        homepage=prepared['homepage'], homepage_action='managed_markdown_initialize',
        note_roots=spec['desired_profile']['note_roots'],
        sources=[dict(source_id=b['source_id'], path=b['locator'], access=[g['access']
            for g in b['grants'] if g['workspace_id'] == spec['desired_profile']['id']])
            for b in spec['binding_requests']],
        plan_sha256=prepared['plan']['data']['plan_sha256'],
        design={key: copy.deepcopy(spec['desired_profile'][key]) for key in
            ('record_types', 'relation_types', 'working_rules', 'workflows')},
        effective_authority=[dict(reference=r['reference'], audiences=r['audiences'],
            new_permissions=r['permission_delta']) for r in prepared['approval_requests']],
        unresolved=spec['unresolved'], activation='explicit_policy_decision_required')
    identity = content_hash(canonical_bytes(prepared))
    with _locked(registry.state):
        path = safe_path(registry.state, 'workspace-onboarding/preparations/' + identity + '.json')
        if path.exists():
            if parse_document(path.read_bytes()) != prepared:
                raise WorkspaceError('preparation_conflict')
        else:
            atomic(path, canonical_bytes(prepared).decode('utf-8'))
        plan_path = safe_path(registry.state, 'workspace-onboarding/preparations/' + identity + '.plan.json')
        if plan_path.exists():
            if parse_document(plan_path.read_bytes()) != prepared['plan']:
                raise WorkspaceError('preparation_conflict')
        else:
            atomic(plan_path, canonical_bytes(prepared['plan']).decode('utf-8'))
    return _result(dict(preparation_id=identity, scope=prepared['scope'],
        plan_sha256=prepared['plan']['data']['plan_sha256'],
        approval_requests=prepared['approval_requests'], preparation_path=str(path), plan_path=str(plan_path))), 0


def _load(registry, identity):
    path = safe_path(registry.state, 'workspace-onboarding/preparations/' + _digest(identity) + '.json', True)
    prepared = parse_document(path.read_bytes())
    if content_hash(canonical_bytes(prepared)) != identity:
        raise WorkspaceError('preparation_conflict')
    validate_document(prepared['spec']); validate_document(prepared['plan'])
    return prepared


def _journal_path(registry, key):
    return safe_path(registry.state, 'workspace-onboarding/operations/' + _key(key) + '.json')


def _staged_authority(registry, prepared):
    """Check current grant readback; this adapter never restores missing scope.

    Approval callers must CAS expected_grant_sha256 when recording a delta.
    Completion independently relies on current C1 exact-plan authority.
    """
    current = LocalAuthority(registry)._load()
    for request in prepared['approval_requests']:
        grant = current['grants'].get(request['reference'])
        if grant is None or any(p not in grant['permissions'] for p in request['permissions']):
            raise WorkspaceError('authority_required')
        if any(a not in grant['audiences'] for a in request['audiences']):
            raise WorkspaceError('authority_required')


def _progress(journal, *, historical=False):
    config = journal.get('configuration')
    prior_homepage = journal.get('homepage')
    homepage = prior_homepage if not historical else None
    parts = [dict(id='configuration', state='verified' if config else 'pending'),
        dict(id='managed-homepage', state='verified' if homepage else 'pending'),
        dict(id='native-activation', state='pending')]
    stage_name = 'activation_pending' if homepage else 'homepage_pending' if config else 'configuration_pending'
    return _result(dict(preparation_id=journal['preparation_id'], stage=stage_name,
        configuration_applied=bool(config), homepage_verified=bool(homepage),
        homepage_previously_verified=bool(prior_homepage), verification_scope='journal_snapshot' if historical else 'current_operation',
        activation_pending=True),
        status='applied' if config else 'planned', parts=parts, issues=journal.get('last_issues', []),
        next_action=('Bu çıktı geçmiş işlem kaydıdır; güncel sayfa ve yetkiyi aynı anahtar ve gerçek RequestContext ile resume ederek doğrula.' if historical else
            'Markdown hazır; global etkinleştirme politikasını açıkça kararlaştır ve yeni oturumda doğrula.' if homepage else
            'Yapılandırma uygulandı; aynı işlem anahtarıyla yetkili Markdown aşamasını sürdür.' if config else
            'Somut kapsam ve kaydedilmiş planın C1 onayını doğrula; aynı anahtarla devam et.'))


def complete(vault, state, preparation_id, request_document, idempotency_key):
    """Resume config/page stages; never approve, activate, or adopt existing notes.

    The real caller supplies its actual RequestContext (including session). A
    resumed session is freshly resolved; a journaled canonical intent keeps its
    original payload/context so its own idempotency/CAS journal stays valid.
    Config success with verification_pending is a completed config stage.
    """
    validate_document(request_document)
    if request_document['type'] != 'RequestContext':
        raise WorkspaceError('invalid_schema')
    request = request_document['data']
    if request['no_memory']:
        raise WorkspaceError('no_memory')
    registry = Registry(vault, state)
    prepared = _load(registry, preparation_id)
    spec = prepared['spec']['data']
    wid = spec['desired_profile']['id']
    if (request['grant_ref'] != spec['authority_ref'] or request['audience'] != 'internal'
            or {s['workspace_id'] for s in request['signals'] if s['kind'] == 'explicit_user'} != {wid}):
        raise WorkspaceError('scope_denied')
    path = _journal_path(registry, idempotency_key)
    with _locked(registry.state):
        if path.exists():
            journal = parse_document(path.read_bytes())
            if journal['preparation_id'] != preparation_id:
                raise WorkspaceError('idempotency_collision')
        else:
            journal = dict(preparation_id=preparation_id, request=copy.deepcopy(request_document),
                configuration=None, homepage=None, homepage_intent=None)
            atomic(path, canonical_bytes(journal).decode('utf-8'))
        try:
            _staged_authority(registry, prepared)
            if not journal['configuration']:
                result = provisioner(vault, state).apply(prepared['plan'], 'onboarding-config:' + _key(idempotency_key))
                if result['data']['operation_status'] not in ('applied', 'replayed'):
                    raise WorkspaceError('configuration_verification_pending')
                journal['configuration'] = result
                atomic(path, canonical_bytes(journal).decode('utf-8'))
            # Recheck current profile; never write the prepared page into a
            # differently configured workspace after a concurrent profile edit.
            snapshot = registry.snapshot()
            profile = next((p for p in snapshot['profiles'] if p['id'] == wid), None)
            if profile != spec['desired_profile']:
                raise WorkspaceError('profile_changed')
            from beyin_v3_workspace_context import WorkspaceRuntime
            from beyin_v3_workspace_views import initialize
            from beyin_v3_workspace_memory import MemoryIntentService
            runtime = WorkspaceRuntime(vault, state, readers={})
            if journal['homepage_intent']:
                resolved = runtime.context(request_document)['data']
                if resolved['workspace_id'] != wid or resolved['state'] not in ('ready', 'partial'):
                    raise WorkspaceError('workspace_context_required')
                result = MemoryIntentService(runtime).apply(journal['homepage_intent'],
                    _refresh_views=False, _preserve_body=True)
            else:
                def checkpoint(intent):
                    journal['homepage_intent'] = copy.deepcopy(intent)
                    atomic(path, canonical_bytes(journal).decode('utf-8'))
                result = initialize(runtime, request_document, 'onboarding-home:' + _key(idempotency_key), before_apply=checkpoint)
            if result['data']['operation_status'] not in ('applied', 'replayed') or any(
                    p['state'] not in ('verified', 'skipped') for p in result['data']['parts']):
                raise WorkspaceError('managed_view_verification_pending')
            journal['homepage'] = result
            journal['last_issues'] = []
            atomic(path, canonical_bytes(journal).decode('utf-8'))
            return _progress(journal), 5
        except (ValueError, OSError, KeyError, TypeError) as exc:
            failure, code = _failure(exc)
            journal['homepage'] = None
            journal['last_issues'] = failure['data']['issues']
            atomic(path, canonical_bytes(journal).decode('utf-8'))
            result = _progress(journal)
            # Preserve successful config facts; failures never restart setup or
            # advertise a canonical page which its writer has not verified.
            if not journal['configuration']:
                result['data']['operation_status'] = failure['data']['operation_status']
            return result, code


def status(vault, state, idempotency_key):
    registry = Registry(vault, state)
    path = _journal_path(registry, idempotency_key)
    if not path.exists():
        raise WorkspaceError('onboarding_operation_unavailable')
    return _progress(parse_document(path.read_bytes()), historical=True), 5


def execute(vault, state, action, payload):
    """Small shared-CLI adapter, with no privilege or implicit human-consent path."""
    try:
        if not isinstance(payload, dict):
            raise WorkspaceError('invalid_onboarding_request')
        if action == 'stage':
            return stage(vault, state, payload)
        if action in ('complete', 'resume') and set(payload) == {'preparation_id', 'request', 'idempotency_key'}:
            return complete(vault, state, payload['preparation_id'], payload['request'], payload['idempotency_key'])
        if action == 'status' and set(payload) == {'idempotency_key'}:
            return status(vault, state, payload['idempotency_key'])
        raise WorkspaceError('invalid_onboarding_request')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return _failure(exc)
