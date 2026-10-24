"""G0 configuration provisioning with an independent durable journal.

C1 supplies schema/semantic validation, current authority and snapshot checks.
There is no default authority, second permission store, note writer or CLI here.
Config-only subset: note/task mutations must use the shared C4 SyncEngine service.
"""
from __future__ import annotations

import base64
import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import stat

import beyin_v3_update as updater
from beyin_v3_workspace_plan import (_checked, _parse_document, canonical_bytes,
                                     content_hash, file_deltas, relative_name)

MAX_JOURNAL_BYTES = 16 * 1024 * 1024


class ProvisionConflict(ValueError):
    pass


def transaction_hook(phase, index=None):
    """Crash/fault-injection seam, matching the updater's testing convention."""


def _target(root, name):
    relative_name(name)
    if root.exists() and updater._is_link(root):
        raise ProvisionConflict('redirected_provision_root')
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if updater._is_link(current):
                raise ProvisionConflict('redirected_provision_target')
    if not current.resolve().is_relative_to(root.resolve()):
        raise ProvisionConflict('provision_target_escape')
    return current


def _bytes(path):
    if path.is_symlink():
        raise ProvisionConflict('redirected_provision_target')
    return path.read_bytes() if path.exists() else None


def _flush_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if os.name != 'nt':
            raise


class Provisioner:
    """Requires trusted C services; opaque strings from a plan are never authority.

    validate_document(envelope) -> None or raises (C1 schema + semantic validator).
    check_authority(envelope, action) -> None or raises (local plan-bound authority).
    check_snapshot(envelope, observations) -> None or raises, called under the shared
    lock without reacquiring it. Observations contain G0 target/old/new/current hash;
    C1 normalizes this operation's applied targets to old hashes before comparing
    the complete registry/binding snapshots. This is the recovery boundary.
    """
    def __init__(self, vault, state, *, validate_document, check_authority, check_snapshot):
        if not all(callable(fn) for fn in (validate_document, check_authority, check_snapshot)):
            raise ValueError('trusted_workspace_services_required')
        if Path(vault).is_symlink() or Path(state).is_symlink():
            raise ValueError('redirected_provision_root')
        self.vault, self.state = updater.roots(vault, state)
        self.validate = validate_document
        self.authority = check_authority
        self.snapshot = check_snapshot

    def _validate_plan(self, document):
        plan = _checked(document, 'ProvisionPlan', self.validate)
        expected = content_hash(canonical_bytes({key: value for key, value in plan.items()
                                                if key not in ('plan_id', 'plan_sha256')}))
        if plan['plan_sha256'] != expected:
            raise ProvisionConflict('plan_changed')
        if plan['unresolved']:
            raise ProvisionConflict('unresolved_plan')
        targets, identities = {}, set()
        for operation in plan['operations']:
            identity = operation['id']
            if identity in identities or not set(operation['depends_on']).issubset(identities):
                raise ProvisionConflict('invalid_operation_dependencies')
            identities.add(identity)
            target = operation['target']
            name, root = target['relative_path'], target['root']
            relative_name(name)
            if root not in ('vault', 'state') or (root, name) in targets:
                raise ProvisionConflict('invalid_operation_target')
            raw = operation['content_utf8'].encode('utf-8')
            if content_hash(raw) != operation['after_sha256']:
                raise ProvisionConflict('operation_content_changed')
            if operation['kind'] != ('create' if operation['before_sha256'] is None else 'update'):
                raise ProvisionConflict('operation_precondition_changed')
            # Config targets only. No global/managed runtime or memory writer is opened.
            payload = _parse_document(raw)
            owner = operation['ownership']
            if owner == 'binding' and root == 'state':
                value = _checked(payload, 'SourceBinding', self.validate)
                allowed = 'workspace-bindings/' + value['source_id'] + '.json'
            elif owner == 'registry' and root == 'vault':
                kind = payload.get('type')
                if kind == 'RegistryFormat':
                    _checked(payload, kind, self.validate)
                    allowed = '.beyin-workspaces/format.json'
                elif kind in ('WorkspaceProfile', 'SourceDescriptor'):
                    value = _checked(payload, kind, self.validate)
                    folder = 'workspaces' if kind == 'WorkspaceProfile' else 'sources'
                    allowed = '.beyin-workspaces/' + folder + '/' + value['id'] + '.json'
                else:
                    raise ProvisionConflict('unsupported_config_document')
            else:
                raise ProvisionConflict('shared_writer_required')
            if name != allowed:
                raise ProvisionConflict('ownership_target_mismatch')
            targets[(root, name)] = raw
        file_deltas({}, targets)  # Reject cross-platform alias and parent collisions.
        return copy.deepcopy(plan)

    def _location(self, operation):
        target = operation['target']
        root = self.vault if target['root'] == 'vault' else self.state
        return _target(root, target['relative_path'])

    def _paths(self, key):
        if not isinstance(key, str) or not key or len(key) > 512:
            raise ValueError('bounded_idempotency_key_required')
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
        directory = _target(self.state, 'workspace-provision')
        journal = _target(self.state, 'workspace-provision/' + digest + '.json')
        pending = _target(self.state, 'workspace-provision/pending.json')
        return directory, journal, pending, digest

    def _save(self, path, value):
        raw = canonical_bytes(value)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ValueError('provision_journal_too_large')
        _target(self.state, path.relative_to(self.state).as_posix())
        self._ensure_parent(path)
        updater.atomic(path, raw)
        _flush_directory(path.parent)

    def _ensure_parent(self, path):
        root = self.state if path.is_relative_to(self.state) else self.vault
        current = root
        for part in path.relative_to(root).parent.parts:
            parent = current
            current = current / part
            _target(root, current.relative_to(root).as_posix())
            if not current.exists():
                current.mkdir(mode=0o700)
                _flush_directory(parent)

    def _load(self, path):
        _target(self.state, path.relative_to(self.state).as_posix())
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            raise ValueError('provision_journal_too_large')
        return _parse_document(_bytes(path))

    def _pending(self, pending, digest):
        if pending.exists() and self._load(pending).get('journal') != digest:
            raise ProvisionConflict('operation_pending')

    def _observations(self, plan):
        return [{'target': copy.deepcopy(operation['target']),
                 'before_sha256': operation['before_sha256'],
                 'after_sha256': operation['after_sha256'],
                 'current_sha256': content_hash(_bytes(self._location(operation)))}
                for operation in plan['operations']]

    def _authorize(self, document, action):
        if self.authority(copy.deepcopy(document), action) is not None:
            raise PermissionError('authority_service_must_raise_on_rejection')

    def _validate_journal(self, journal, plan):
        identities = {item['id'] for item in plan['operations']}
        if (journal.get('schema') != 1 or journal.get('phase') not in
                ('prepared', 'applying', 'applied', 'reverting', 'reverted')
                or not isinstance(journal.get('originals'), dict)
                or set(journal['originals']) != identities):
            raise ProvisionConflict('invalid_provision_journal')
        for name in ('intended', 'completed'):
            values = journal.get(name)
            if (not isinstance(values, list) or not all(isinstance(value, str) for value in values)
                    or len(set(values)) != len(values) or not set(values).issubset(identities)):
                raise ProvisionConflict('invalid_provision_journal')
        for item in plan['operations']:
            original = journal['originals'][item['id']]
            if not isinstance(original, dict) or set(original) != {'content', 'mode'}:
                raise ProvisionConflict('invalid_provision_journal')
            value, mode = original['content'], original['mode']
            try:
                before = base64.b64decode(value, validate=True) if value is not None else None
            except (ValueError, TypeError):
                raise ProvisionConflict('journal_backup_changed') from None
            if content_hash(before) != item['before_sha256']:
                raise ProvisionConflict('journal_backup_changed')
            if ((value is None and mode is not None) or
                    (value is not None and (not isinstance(value, str) or type(mode) is not int
                                            or not 0 <= mode <= 0o7777))):
                raise ProvisionConflict('invalid_provision_journal')

    def _guards(self, document, plan, *, recovering=False, owned=()):
        self._authorize(document, 'recover' if recovering else 'apply')
        observations = self._observations(plan)
        for operation, item in zip(plan['operations'], observations):
            expected = ((item['before_sha256'], item['after_sha256'])
                        if recovering and operation['id'] in owned else (item['before_sha256'],))
            if item['current_sha256'] not in expected:
                raise ProvisionConflict('source_hash_conflict')
        self._check_snapshot(document, plan)

    def _check_snapshot(self, document, plan):
        if self.snapshot(copy.deepcopy(document), self._observations(plan)) is not None:
            raise ProvisionConflict('snapshot_service_must_raise_on_rejection')

    def _result(self, document, digest, *, replay=False, reverted=False):
        result = {'contract_version': document['contract_version'], 'type': 'OperationResult', 'data': {
            'operation_id': 'provision-' + digest[:32], 'operation_status': 'replayed' if replay else 'applied',
            'workspace_health': 'not_tested' if reverted else 'verification_pending',
            'parts': [{'id': item['id'], 'state': 'applied'} for item in document['data']['operations']],
            'capabilities': [], 'verification': [], 'issues': [{'code': 'reverted'}] if reverted else [],
            'next_action': 'Verify current runtime/source/index and a fresh client session.' if not reverted
                           else 'Reverted operation-owned configuration; user edits and original sources preserved.'}}
        _checked(result, 'OperationResult', self.validate)
        return result

    def apply(self, document, idempotency_key, *, _coordinated=False):
        """Internal workflow adapter may reuse its held update coordination lock.

        Only config targets are supported. The caller must hold the same registry
        update lock; authority/snapshot/CAS/journal checks still run unchanged.
        """
        document = copy.deepcopy(document)
        plan = self._validate_plan(document)
        directory, journal_path, pending, digest = self._paths(idempotency_key)
        with (nullcontext() if _coordinated else updater.locked(self.vault, self.state, allow_workspace_provision=True)):
            if (self.state / 'update-journal.json').exists():
                raise ProvisionConflict('updater_operation_pending')
            self._pending(pending, digest)
            if journal_path.exists():
                journal = self._load(journal_path)
                if (journal.get('vault') != str(self.vault) or journal.get('state') != str(self.state)
                        or journal.get('plan', {}).get('data', {}).get('plan_sha256') != plan['plan_sha256']):
                    raise ProvisionConflict('idempotency_collision')
                self._validate_plan(journal['plan'])
                self._validate_journal(journal, plan)
                if journal['phase'] == 'reverted':
                    raise ProvisionConflict('operation_already_reverted')
                if journal['phase'] == 'reverting':
                    raise ProvisionConflict('revert_pending')
                if journal['phase'] == 'applied':
                    self._authorize(document, 'replay')
                    if any(item['current_sha256'] != item['after_sha256'] for item in self._observations(plan)):
                        raise ProvisionConflict('replay_source_changed')
                    if pending.exists():
                        pending.unlink(); _flush_directory(directory)
                    return self._result(document, digest, replay=True)
                self._guards_for_revert(plan, journal)
                self._guards(document, plan, recovering=True, owned=journal['intended'])
            else:
                self._guards(document, plan)
                originals = {}
                for item in plan['operations']:
                    path = self._location(item)
                    raw = _bytes(path)
                    originals[item['id']] = {'content': base64.b64encode(raw).decode() if raw is not None else None,
                                              'mode': stat.S_IMODE(path.stat().st_mode) if raw is not None else None}
                journal = {'schema': 1, 'vault': str(self.vault), 'state': str(self.state),
                           'plan': document, 'phase': 'prepared', 'originals': originals, 'completed': [], 'intended': []}
                self._save(journal_path, journal)
                transaction_hook('journal_created')
            self._save(pending, {'journal': digest})
            transaction_hook('pending_published')
            for index, item in enumerate(plan['operations']):
                self._guards(document, plan, recovering=True, owned=journal['intended'])
                path = self._location(item)
                original = journal['originals'][item['id']]
                before = base64.b64decode(original['content'], validate=True) if original['content'] is not None else None
                if content_hash(before) != item['before_sha256']:
                    raise ProvisionConflict('journal_backup_changed')
                if content_hash(_bytes(path)) != item['after_sha256']:
                    journal['phase'] = 'applying'
                    journal['intent'] = item['id']
                    if item['id'] not in journal['intended']:
                        journal['intended'].append(item['id'])
                    self._save(journal_path, journal)
                    transaction_hook('step_intent', index)
                    transaction_hook('before_replace', index)
                    # Re-read after the injection point, preserving an intervening edit.
                    if content_hash(_bytes(self._location(item))) != item['before_sha256']:
                        raise ProvisionConflict('source_hash_conflict')
                    self._ensure_parent(path)
                    updater.atomic(path, item['content_utf8'].encode('utf-8'))
                    if original['mode'] is not None:
                        os.chmod(path, original['mode'])
                    _flush_directory(path.parent)
                    transaction_hook('after_replace', index)
                if item['id'] not in journal['completed']:
                    journal['completed'].append(item['id'])
                self._save(journal_path, journal)
                transaction_hook('step_checkpoint', index)
            self._guards(document, plan, recovering=True, owned=journal['intended'])
            if any(item['current_sha256'] != item['after_sha256'] for item in self._observations(plan)):
                raise ProvisionConflict('source_readback_conflict')
            journal['phase'] = 'applied'
            self._save(journal_path, journal)
            transaction_hook('applied_checkpoint')
            pending.unlink(); _flush_directory(directory)
            transaction_hook('pending_cleared')
        # Never reuse an old health result or call SyncEngine while holding memory lock.
        return self._result(document, digest)

    def recover(self, idempotency_key):
        _, path, _, _ = self._paths(idempotency_key)
        if not path.exists():
            raise ProvisionConflict('operation_not_found')
        return self.apply(self._load(path)['plan'], idempotency_key)

    def revert(self, idempotency_key):
        directory, path, pending, digest = self._paths(idempotency_key)
        with updater.locked(self.vault, self.state, allow_workspace_provision=True):
            if (self.state / 'update-journal.json').exists():
                raise ProvisionConflict('updater_operation_pending')
            self._pending(pending, digest)
            journal = self._load(path)
            if journal.get('vault') != str(self.vault) or journal.get('state') != str(self.state):
                raise ProvisionConflict('journal_root_mismatch')
            document = journal['plan']
            plan = self._validate_plan(document)
            self._validate_journal(journal, plan)
            self._authorize(document, 'revert')
            # Preflight all targets before reverting even one; later edits are preserved.
            self._guards_for_revert(plan, journal)
            if journal['phase'] == 'reverted':
                if pending.exists():
                    pending.unlink(); _flush_directory(directory)
                return self._result(document, digest, replay=True, reverted=True)
            # A later workspace may reference a descriptor created by this plan.
            # Exact target hashes alone do not make deleting that descriptor safe.
            self._check_snapshot(document, plan)
            journal['phase'] = 'reverting'; self._save(path, journal)
            self._save(pending, {'journal': digest})
            transaction_hook('revert_started')
            for index, item in enumerate(reversed(plan['operations'])):
                self._authorize(document, 'revert')
                self._guards_for_revert(plan, journal)
                self._check_snapshot(document, plan)
                target = self._location(item)
                original = journal['originals'][item['id']]
                before = base64.b64decode(original['content'], validate=True) if original['content'] is not None else None
                transaction_hook('revert_intent', index)
                actual = content_hash(_bytes(self._location(item)))
                if actual == item['before_sha256']:
                    continue
                if actual != item['after_sha256']:
                    raise ProvisionConflict('revert_user_edit_conflict')
                if before is None:
                    target.unlink(); _flush_directory(target.parent)
                else:
                    updater.atomic(target, before)
                    os.chmod(target, original['mode'])
                    _flush_directory(target.parent)
                transaction_hook('revert_replace', index)
            self._authorize(document, 'revert')
            self._check_snapshot(document, plan)
            journal['phase'] = 'reverted'; self._save(path, journal)
            transaction_hook('reverted_checkpoint')
            pending.unlink(); _flush_directory(directory)
        return self._result(document, digest, reverted=True)

    def _guards_for_revert(self, plan, journal):
        for item in plan['operations']:
            original = journal['originals'][item['id']]
            before = base64.b64decode(original['content'], validate=True) if original['content'] is not None else None
            if content_hash(before) != item['before_sha256']:
                raise ProvisionConflict('journal_backup_changed')
            expected = ((item['before_sha256'], item['after_sha256']) if item['id'] in journal['intended']
                        else (item['before_sha256'],))
            if content_hash(_bytes(self._location(item))) not in expected:
                raise ProvisionConflict('revert_user_edit_conflict')
            target = self._location(item)
            # Applying preserves an existing POSIX mode and creates files as 0600.
            # A later chmod is a user edit too; never undo it while restoring bytes.
            if os.name != 'nt' and target.exists():
                expected_mode = original['mode'] if original['content'] is not None else 0o600
                if stat.S_IMODE(target.stat().st_mode) != expected_mode:
                    raise ProvisionConflict('revert_user_mode_conflict')
