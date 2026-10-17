"""Pure byte-level planning helpers for the conversational workspace builder.

These helpers do not define a WorkspaceProfile, permission model or writer. The
versioned G0 contract and C's validator supply those. No filesystem access, model
call, clock, random ID or installation takes place here.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import difflib
import hashlib
import json
import math
import re
import unicodedata


def canonical_bytes(value):
    """Canonical JSON bytes; reject non-JSON values rather than stringify them."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError('canonical JSON requires finite JSON values')

    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def content_hash(content):
    """Missing and empty files differ. Compare exact bytes, including line ends."""
    if content is None:
        return None
    if not isinstance(content, bytes):
        raise ValueError('file snapshot must contain bytes or None')
    return hashlib.sha256(content).hexdigest()


def relative_name(name):
    """Portable target name only, without resolving or granting any path access."""
    if not isinstance(name, str) or not name or len(name) > 1024:
        raise ValueError('invalid relative target')
    parts = name.split('/')
    if ('\\' in name or ':' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in parts)):
        raise ValueError('unsafe relative target')
    for part in parts:
        if any(c in part for c in '<>"|?*') or re.fullmatch(
                r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I):
            raise ValueError('target is not portable')
    return name


@dataclass(frozen=True)
class FileDelta:
    """Internal diff, not an executable operation or authority token."""
    scope: str
    name: str
    before: bytes | None
    after: bytes | None

    def __post_init__(self):
        if self.scope not in ('vault', 'state'):
            raise ValueError('only vault/state diffs are supported')
        relative_name(self.name)
        content_hash(self.before)
        content_hash(self.after)
        if self.before == self.after:
            raise ValueError('unchanged files do not belong in the write-set')

    def summary(self):
        return {'scope': self.scope, 'name': self.name,
                'action': 'create' if self.before is None else 'delete' if self.after is None else 'update',
                'before_sha256': content_hash(self.before),
                'after_sha256': content_hash(self.after),
                'before_bytes': len(self.before) if self.before is not None else None,
                'after_bytes': len(self.after) if self.after is not None else None}


def file_deltas(snapshot, rendered):
    """Diff explicitly supplied targets. Unmentioned files are always preserved.

    Keys are (scope, relative_name). A missing snapshot key means absent; an
    explicit rendered None means deletion. C/D's contract adapter must validate
    ownership and allowed operations before rendering; this is not that policy.
    """
    if not isinstance(snapshot, dict) or not isinstance(rendered, dict):
        raise ValueError('explicit snapshots and rendered targets required')
    normalized = {}
    all_targets = set(snapshot) | set(rendered)
    for key in all_targets:
        if not isinstance(key, tuple) or len(key) != 2:
            raise ValueError('target key must be (scope, relative_name)')
        scope, name = key
        if scope not in ('vault', 'state'):
            raise ValueError('only vault/state diffs are supported')
        relative_name(name)
        alias = (scope, unicodedata.normalize('NFC', name).casefold())
        if alias in normalized and normalized[alias] != key:
            raise ValueError('case/Unicode target collision')
        normalized[alias] = key
    # A planned parent file would otherwise fail halfway through a transaction.
    for scope, name in all_targets:
        for end in range(1, len(name.split('/'))):
            parent = '/'.join(name.split('/')[:end])
            if (scope, unicodedata.normalize('NFC', parent).casefold()) in normalized:
                raise ValueError('file/parent target collision')
    changes = []
    for (scope, name), after in sorted(rendered.items()):
        before = snapshot.get((scope, name))
        content_hash(before)
        content_hash(after)
        if before != after:
            changes.append(FileDelta(scope, name, before, after))
    return tuple(changes)


def write_set_hash(changes):
    """Digest exact targets and old/new bytes, independent of caller ordering."""
    summaries = [change.summary() for change in changes]
    summaries.sort(key=lambda item: (item['scope'], item['name']))
    targets = [(item['scope'], item['name']) for item in summaries]
    if len(targets) != len(set(targets)):
        raise ValueError('duplicate write-set target')
    return hashlib.sha256(canonical_bytes(summaries)).hexdigest()


def preview_files(changes, *, max_chars=12000):
    """Human-readable exact diff; truncation remains explicit to the caller.

    Authority must bind the complete write-set, never this bounded display.
    Binary/invalid UTF-8 gets hashes and sizes instead of a misleading text diff.
    """
    if type(max_chars) is not int or max_chars < 128:
        raise ValueError('preview budget must be at least 128 characters')
    chunks = []
    for change in sorted(changes, key=lambda item: (item.scope, item.name)):
        item = change.summary()
        chunks.append(f"{item['action']}: {change.scope}/{change.name}\n"
                      f"SHA-256: {item['before_sha256'] or '(absent)'} -> {item['after_sha256'] or '(absent)'}\n")
        try:
            old = (change.before or b'').decode('utf-8')
            new = (change.after or b'').decode('utf-8')
            if '\x00' in old + new:
                raise UnicodeError('binary')
        except UnicodeError:
            chunks.append(f"Binary: {item['before_bytes']} -> {item['after_bytes']} bytes\n")
            continue
        # keepends=False makes a missing final newline visible in its own marker.
        diff = '\n'.join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                        fromfile='before/' + change.name, tofile='after/' + change.name,
                        lineterm=''))
        chunks.append(diff + '\n')
        if bool(old.endswith('\n')) != bool(new.endswith('\n')):
            chunks.append('Final newline changed.\n')
    full = '\n'.join(chunks)
    truncated = len(full) > max_chars
    return {'text': full[:max_chars] + ('\n[Preview truncated; inspect the complete write-set.]' if truncated else ''),
            'truncated': truncated, 'write_set_hash': write_set_hash(changes)}


VERSION = 'workspace/0.1.0'


def _envelope(kind, data):
    return {'contract_version': VERSION, 'type': kind, 'data': data}


def _checked(document, kind, validate):
    if not callable(validate):
        raise ValueError('contract_validator_required')
    if not isinstance(document, dict) or document.get('contract_version') != VERSION:
        raise ValueError('unsupported_contract')
    if document.get('type') != kind:
        raise ValueError('invalid_schema')
    if validate(copy.deepcopy(document)) is not None:
        raise ValueError('contract_validator_must_raise_on_rejection')
    return document['data']


def _document_bytes(document):
    return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _parse_document(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('duplicate_json_key')
            obj[key] = value
        return obj
    def constant(value):
        raise ValueError('nonfinite_json_number')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _operation_id(prefix, identity):
    value = prefix + '-' + identity
    return value if len(value) <= 96 else prefix[:31] + '-' + hashlib.sha256(value.encode()).hexdigest()


def plan_workspace(spec_document, registry_document, binding_snapshot_sha256, files,
                   *, validate_document=None, capabilities=None):
    """Pure G0 WorkspaceSpec -> ProvisionPlan, using C1's validator callback.

    `files` is the complete read-only C1 snapshot as {(root, relative_path): bytes}.
    C1 supplies schema/semantic validation. No second profile/authority schema or
    writer is defined here. Missing capabilities remain unresolved, never ready.
    """
    spec = _checked(spec_document, 'WorkspaceSpec', validate_document)
    registry = _checked(registry_document, 'RegistrySnapshot', validate_document)
    # Caller-owned documents and byte snapshots must never be changed by planning.
    spec, registry = copy.deepcopy(spec), copy.deepcopy(registry)
    if not isinstance(files, dict):
        raise ValueError('explicit_snapshot_required')
    file_deltas(files, {})  # Validate portable, noncolliding names without IO.
    registry_files = [{'relative_path': name, 'content_sha256': content_hash(raw)}
                      for (root, name), raw in sorted(files.items())
                      if root == 'vault' and name.startswith('.beyin-workspaces/') and raw is not None]
    if registry_files != registry['files'] or content_hash(canonical_bytes(registry_files)) != registry['snapshot_sha256']:
        raise ValueError('registry_snapshot_mismatch')
    bindings, profiles, sources = {}, {}, {}
    format_document = None
    for (root, name), raw in sorted(files.items()):
        if raw is None:
            continue
        if root == 'state' and name.startswith('workspace-bindings/'):
            document = _parse_document(raw)
            binding = _checked(document, 'SourceBinding', validate_document)
            if name != 'workspace-bindings/' + binding['source_id'] + '.json':
                raise ValueError('binding_identity_path_mismatch')
            bindings[binding['source_id']] = document
        elif root == 'vault' and name.startswith('.beyin-workspaces/'):
            document = _parse_document(raw)
            if name == '.beyin-workspaces/format.json':
                data = _checked(document, 'RegistryFormat', validate_document)
                if data != {'registry_id': registry['registry_id'], 'layout': 'per_workspace_json'}:
                    raise ValueError('registry_format_mismatch')
                format_document = document
            elif name.startswith('.beyin-workspaces/workspaces/'):
                data = _checked(document, 'WorkspaceProfile', validate_document)
                if name != '.beyin-workspaces/workspaces/' + data['id'] + '.json':
                    raise ValueError('profile_identity_path_mismatch')
                profiles[data['id']] = data
            elif name.startswith('.beyin-workspaces/sources/'):
                data = _checked(document, 'SourceDescriptor', validate_document)
                if name != '.beyin-workspaces/sources/' + data['id'] + '.json':
                    raise ValueError('source_identity_path_mismatch')
                sources[data['id']] = data
            else:
                raise ValueError('unsupported_registry_file')
    if sorted(profiles.values(), key=lambda d: d['id']) != sorted(registry['profiles'], key=lambda d: d['id']):
        raise ValueError('profile_projection_mismatch')
    if sorted(sources.values(), key=lambda d: d['id']) != sorted(registry['sources'], key=lambda d: d['id']):
        raise ValueError('source_projection_mismatch')
    binding_inventory = [{'source_id': identity, 'revision': doc['data']['revision'],
                          'content_sha256': content_hash(canonical_bytes(doc))}
                         for identity, doc in sorted(bindings.items())]
    if content_hash(canonical_bytes(binding_inventory)) != binding_snapshot_sha256:
        raise ValueError('binding_snapshot_mismatch')
    if format_document is None and (profiles or sources):
        raise ValueError('registry_format_missing')
    desired = spec['desired_profile']
    operations = []
    rendered = {}

    def add(identity, root, name, owner, document, dependencies=()):
        raw = files.get((root, name))
        # Preserve formatting of a semantically unchanged configuration document.
        if raw is not None and _parse_document(raw) == document:
            return
        content = _document_bytes(document)
        rendered[(root, name)] = content
        operations.append({'id': identity, 'kind': 'create' if raw is None else 'update',
                           'target': {'root': root, 'relative_path': name}, 'ownership': owner,
                           'before_sha256': content_hash(raw), 'after_sha256': content_hash(content),
                           'content_utf8': content.decode('utf-8'), 'depends_on': list(dependencies)})

    if format_document is None:
        add('create-format', 'vault', '.beyin-workspaces/format.json', 'registry',
            _envelope('RegistryFormat', {'registry_id': registry['registry_id'], 'layout': 'per_workspace_json'}))
    seen = set()
    for descriptor in spec['sources']:
        identity = descriptor['id']
        if identity in seen:
            raise ValueError('duplicate_source')
        seen.add(identity)
        if identity in sources and sources[identity] != descriptor:
            raise ValueError('shared_source_change_requires_coordinated_plan')
        sources[identity] = descriptor
        add(_operation_id('source', identity), 'vault', '.beyin-workspaces/sources/' + identity + '.json',
            'registry', _envelope('SourceDescriptor', descriptor))
    if not set(desired['source_ids']).issubset(sources):
        raise ValueError('source_descriptor_missing')
    seen = set()
    for request in spec['binding_requests']:
        identity = request['source_id']
        # A disconnect must revoke the old local binding while removing portable
        # membership. Both identities are already owned by this same workspace.
        owned_sources=set(desired['source_ids'])|set(profiles.get(desired['id'],{}).get('source_ids',[]))
        if identity in seen or identity not in owned_sources:
            raise ValueError('duplicate_or_unrelated_binding')
        if identity not in desired['source_ids'] and any(
                grant['workspace_id'] == desired['id'] for grant in request['grants']):
            raise ValueError('disconnected_source_grant')
        seen.add(identity)
        if sources[identity]['kind'] != 'vault' and any(grant['access'] == 'write_memory' for grant in request['grants']):
            raise ValueError('external_source_write_denied')
        for grant in request['grants']:
            if grant['workspace_id'] != desired['id']:
                old = bindings.get(identity, {}).get('data', {}).get('grants', [])
                if grant not in old:
                    raise ValueError('unrelated_workspace_grant')
        existing = bindings.get(identity, {}).get('data')
        if existing:
            old_other = [grant for grant in existing['grants'] if grant['workspace_id'] != desired['id']]
            new_other = [grant for grant in request['grants'] if grant['workspace_id'] != desired['id']]
            if sorted(old_other, key=canonical_bytes) != sorted(new_other, key=canonical_bytes):
                raise ValueError('unrelated_workspace_grant')
        if existing != request and request['revision'] != (existing['revision'] + 1 if existing else 1):
            raise ValueError('binding_revision_conflict')
        if existing and (existing['source_id'] != identity or existing['machine_id'] != request['machine_id']):
            raise ValueError('binding_machine_conflict')
        add(_operation_id('bind', identity), 'state', 'workspace-bindings/' + identity + '.json',
            'binding', _envelope('SourceBinding', request))
    existing = profiles.get(desired['id'])
    if existing != desired and desired['revision'] != (existing['revision'] + 1 if existing else 1):
        raise ValueError('profile_revision_conflict')
    add('publish-profile', 'vault', '.beyin-workspaces/workspaces/' + desired['id'] + '.json',
        'registry', _envelope('WorkspaceProfile', desired), [operation['id'] for operation in operations])
    file_deltas(files, rendered)  # Full create/update set must be portable too.
    capabilities = capabilities or {}
    unresolved = list(spec['unresolved'])
    for identity in desired['source_ids']:
        effective = next((request for request in spec['binding_requests'] if request['source_id'] == identity),
                         bindings.get(identity, {}).get('data', {}))
        if not any(grant['workspace_id'] == desired['id'] and grant['access'] == 'read'
                   for grant in effective.get('grants', [])):
            unresolved.append('missing_read_binding:' + identity)
    unresolved.extend('missing_capability:' + name for name in spec['required_capabilities']
                      if capabilities.get(name) != 'ready')
    plan = {'plan_id': _operation_id(spec['spec_id'], 'plan'), 'plan_sha256': '0' * 64,
            'spec_sha256': content_hash(canonical_bytes(spec_document)),
            'snapshot_sha256': registry['snapshot_sha256'], 'binding_snapshot_sha256': binding_snapshot_sha256,
            'operations': operations,
            'required_authority': sorted({spec['authority_ref'], *(request['authority_ref'] for request in spec['binding_requests'])}),
            'verify_steps': ['source_read', 'related_records', 'canonical_update', 'index_readback', 'fresh_session']
                            + (['workflow_resume'] if desired['workflows'] else []),
            'rollback_limits': ['Restore only operation-owned files still matching after_sha256; preserve later user edits.'],
            'unresolved': list(dict.fromkeys(unresolved))}
    plan['plan_sha256'] = content_hash(canonical_bytes({key: value for key, value in plan.items()
                                                       if key not in ('plan_id', 'plan_sha256')}))
    result = _envelope('ProvisionPlan', plan)
    _checked(result, 'ProvisionPlan', validate_document)
    return result


def preview_workspace(document, files, *, validate_document=None, max_chars=12000):
    """Short user preview plus a separately inspectable exact file diff."""
    plan = _checked(document, 'ProvisionPlan', validate_document)
    expected = content_hash(canonical_bytes({key: value for key, value in plan.items()
                                            if key not in ('plan_id', 'plan_sha256')}))
    if plan['plan_sha256'] != expected:
        raise ValueError('plan_changed')
    rendered = {}
    lines = []
    for operation in plan['operations']:
        target = operation['target']; key = (target['root'], target['relative_path'])
        raw = operation['content_utf8'].encode('utf-8')
        if (key in rendered or content_hash(files.get(key)) != operation['before_sha256']
                or content_hash(raw) != operation['after_sha256']):
            raise ValueError('preview_precondition_changed')
        rendered[key] = raw
        content = _parse_document(raw)
        if content['type'] == 'WorkspaceProfile':
            profile = content['data']
            lines += ['Alan: ' + profile['name'], 'Amaç: ' + profile['purpose']]
            lines += ['Süreç: ' + workflow['name'] for workflow in profile['workflows']]
            previous = _parse_document(files[key])['data'] if files.get(key) is not None else {}
            remaining = {workflow['id'] for workflow in profile['workflows']}
            lines += ['Kaldırılan süreç: ' + workflow['name'] for workflow in previous.get('workflows', [])
                      if workflow['id'] not in remaining]
            lines += ['Çıkarılan kaynak bağlantısı: ' + identity for identity in previous.get('source_ids', [])
                      if identity not in profile['source_ids']]
            if previous.get('lifecycle') != profile['lifecycle']:
                lines.append('Alan durumu: ' + profile['lifecycle'])
        elif content['type'] == 'SourceBinding':
            binding = content['data']
            labels = {'read': 'okuma', 'write_memory': 'onaylı vault hafızası yazımı'}
            lines.append('Kaynak: ' + binding['locator'])
            if not binding['grants']:
                lines.append('Erişim: hiçbir alana yetki verilmez.')
            lines += ['Erişim: ' + grant['workspace_id'] + ' - ' + labels[grant['access']]
                      for grant in binding['grants']]
            previous = _parse_document(files[key])['data'] if files.get(key) is not None else {}
            lines += ['Kaldırılan erişim: ' + grant['workspace_id'] + ' - ' + labels[grant['access']]
                      for grant in previous.get('grants', []) if grant not in binding['grants']]
    deltas = file_deltas(files, rendered)
    if deltas:
        lines.append('Dosya değişiklikleri:')
        lines += [('Oluştur: ' if delta.before is None else 'Güncelle: ')
                  + delta.scope + '/' + delta.name for delta in deltas]
    else:
        lines.append('Yapılandırmada değişiklik gerekmiyor.')
    if plan['unresolved']:
        lines.append('Önce tamamlanması gerekenler: ' + ', '.join(plan['unresolved']))
    lines.append('Özgün kaynaklar ve planda olmayan kullanıcı dosyaları korunur.')
    lines.append('Günlük kaynak/iş/öğrenim, mevcut kaydı güncelleme, tekrar bulma ve yeni istemci oturumu ayrıca doğrulanacak.')
    return {'text': '\n'.join(lines), 'plan_sha256': plan['plan_sha256'],
            'file_preview': preview_files(deltas, max_chars=max_chars), 'unresolved': list(plan['unresolved'])}
