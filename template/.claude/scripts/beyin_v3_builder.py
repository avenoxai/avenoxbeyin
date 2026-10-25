"""D's narrow adapter to the committed C1 services; CLI wiring belongs to C.

Planning reads configuration only. Applying never records or infers user consent:
C1 LocalAuthority must already contain approval of the concrete plan/write-set.
"""
from beyin_v3_workspaces import Registry, LocalAuthority, WorkspaceError, envelope, validate_document
from beyin_v3_workspace_plan import plan_workspace, preview_workspace
from beyin_v3_provision import Provisioner
import re


def _validate(document):
    validate_document(document)


def plan(vault, state, spec_document, *, registry_id, capabilities=None):
    """Read-only C1 snapshot -> G0 plan plus a short preview and exact diff.

    `capabilities` comes from C2/runtime checks; absent entries remain unresolved.
    This path creates no store, index, operational state or authority record.
    Concurrent snapshot changes are rejected by the pure D1 adapter.
    """
    registry = Registry(vault, state)
    snapshot = registry.snapshot(registry_id=registry_id)
    _, binding_hash = registry.bindings(snapshot)
    files = registry.capture_files()
    document = plan_workspace(spec_document, envelope('RegistrySnapshot', snapshot),
        binding_hash, files, validate_document=_validate, capabilities=capabilities)
    return document, preview_workspace(document, files, validate_document=_validate)


def provisioner(vault, state):
    """Construct the real C1-backed executor, without approving any operation."""
    registry = Registry(vault, state)
    authority = LocalAuthority(registry)
    return Provisioner(vault, state, validate_document=_validate,
        check_authority=authority.check_authority, check_snapshot=authority.check_snapshot)


def execute(vault, state, action, payload):
    """Thin CLI/active-agent adapter; returns (JSON result, G0 exit code).

    The caller supplies typed input, not capabilities or implicit consent. No
    MemoryStore is initialized. Plan/preview do not create state or authority;
    mutating actions require the existing exact C1 approval. The common CLI
    owns argument parsing and prints this result without dumping raw exceptions.
    """
    try:
        fields = {'plan': {'spec', 'registry_id'}, 'preview': {'plan'},
                  'apply': {'plan', 'idempotency_key'},
                  'recover': {'idempotency_key'}, 'revert': {'idempotency_key'}}
        if action not in fields or not isinstance(payload, dict) or set(payload) != fields[action]:
            raise WorkspaceError('invalid_builder_request')
        if action == 'plan':
            if not isinstance(payload['registry_id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', payload['registry_id']):
                raise WorkspaceError('invalid_builder_request')
            validate_document(payload['spec'])
            if payload['spec']['type'] != 'WorkspaceSpec':
                raise WorkspaceError('invalid_schema')
            from beyin_v3_sources import probe_capabilities
            capabilities = probe_capabilities(vault)
            document, preview = plan(vault, state, payload['spec'], registry_id=payload['registry_id'],
                                     capabilities=capabilities)
            return {'plan': document, 'preview': preview, 'capabilities': capabilities}, 0
        if action == 'preview':
            return preview_workspace(payload['plan'], Registry(vault, state).capture_files(),
                                     validate_document=_validate), 0
        executor = provisioner(vault, state)
        if action == 'apply':
            result = executor.apply(payload['plan'], payload['idempotency_key'])
        else:
            result = getattr(executor, action)(payload['idempotency_key'])
        # Applied configuration is deliberately not completed workspace health.
        return result, 5 if result['data']['workspace_health'] == 'verification_pending' else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return _failure(exc)


def _failure(exc):
    code = getattr(exc, 'code', str(exc) if isinstance(exc, ValueError) else '')
    # The existing updater intentionally retains human-readable ValueErrors for
    # its older callers. Translate only its exact known coordination messages;
    # broad substring matching could expose source text or misclassify input.
    code = {
        'runtime/updater busy; retry after active work finishes': 'busy',
        'workspace provisioning pending; recover or revert that operation first': 'operation_pending',
    }.get(code, code) if isinstance(code, str) else code
    # Only stable symbolic codes may cross the UI boundary; never paths, source
    # text, JSON decoder messages or OS exception details.
    if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code):
        code = 'source_unavailable' if isinstance(exc, OSError) else 'invalid_builder_request'
    denied = code.endswith(('_authority_required', '_denied')) or code in (
        'authority_required', 'source_unbound', 'no_memory', 'workspace_context_required')
    conflict = any(word in code for word in ('conflict', 'changed', 'collision', 'pending', 'busy', 'stale'))
    invalid = code.startswith(('invalid_', 'unsupported_contract')) or code in (
        'duplicate_json_key', 'invalid_number', 'bounded_idempotency_key_required')
    exit_code = 4 if denied else 3 if conflict else 2 if invalid else 5
    next_action = ('Gösterilen plan ve somut kaynak kapsamı için C1 kullanıcı yetkisini doğrula.' if denied else
                   'Güncel kaynak ve planı yeniden oku; bekleyen işlemi aynı anahtarla recover/revert yolunda çöz.' if conflict else
                   'Ajanın hazırladığı girdiyi ortak sözleşmeyle doğrula ve yeniden dene.' if invalid else
                   'Eksik bağımlılıkları ve işlemin journal durumunu denetle; hazır olduğunu varsayma.')
    result = envelope('OperationResult', {'operation_id': 'workspace-builder',
        'operation_status': 'denied' if denied else 'conflict' if conflict else 'failed',
        'workspace_health': 'verification_pending', 'parts': [], 'capabilities': [], 'verification': [],
        'issues': [{'code': code}], 'next_action': next_action})
    validate_document(result)
    return result, exit_code
