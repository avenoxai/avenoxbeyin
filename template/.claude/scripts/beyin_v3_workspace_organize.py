"""P5a: approved attachment links and byte-exact physical copies.

The plan is a bounded, versioned value. Only ``copy`` writes a file. Canonical
Markdown stays with SyncEngine's writer and is never copied here.
"""
import os
from pathlib import Path
import tempfile
import unicodedata

from beyin_v3_lint import scoped_vault_index, plan_attached_source_impact
from beyin_v3_sources import SourceResolver
from beyin_v3_sync import atomic
from beyin_v3_workspaces import (Registry, WorkspaceError, canonical_bytes,
                                 content_hash, relative_name, safe_path)

PLAN_VERSION = 'workspace-organization/0.1.0'
JOURNAL_VERSION = 'workspace-copy/0.1.0'
COPY_SUFFIXES = {'.pdf', '.docx', '.odt', '.xlsx', '.pptx', '.txt', '.csv', '.zip',
                 '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.tif', '.tiff',
                 '.svg', '.psd', '.ai', '.mp3', '.wav', '.mp4', '.mov'}
MANAGED_TARGET_PARTS = {'scripts', 'skills', 'hooks', 'tasks', 'receipts',
                        'node_modules', '__pycache__'}


def _attachment_name(name):
    relative_name(name)
    if Path(name).suffix.lower() not in COPY_SUFFIXES or any(part.startswith('.') for part in name.split('/')):
        raise WorkspaceError('unsupported_copy_type')
    return name


def _alias_check(root, name):
    """Fail closed on case/Unicode aliases before touching a destination."""
    parent = Path(root)
    for part in name.split('/'):
        if parent.is_dir():
            key = unicodedata.normalize('NFC', part).casefold()
            for child in parent.iterdir():
                if child.name != part and unicodedata.normalize('NFC', child.name).casefold() == key:
                    raise WorkspaceError('destination_alias_conflict')
        parent = parent / part


def _plan_hash(plan):
    return content_hash(canonical_bytes({k: v for k, v in plan.items() if k != 'plan_sha256'}))


def _validate_plan(plan):
    required = {'contract_version', 'type', 'workspace_id', 'grant_ref', 'action',
                'source', 'target', 'impact', 'destination_status', 'plan_sha256'}
    if not isinstance(plan, dict) or set(plan) != required or plan['contract_version'] != PLAN_VERSION or plan['type'] != 'OrganizationPlan':
        raise WorkspaceError('invalid_organization_plan')
    if plan['action'] not in ('link', 'unchanged', 'copy') or not isinstance(plan['workspace_id'], str) or not plan['workspace_id'] or not isinstance(plan['grant_ref'], str) or not plan['grant_ref']:
        raise WorkspaceError('invalid_organization_plan')
    source = plan['source']
    if not isinstance(source, dict) or set(source) != {'source_id', 'relative_path', 'content_sha256'} or not isinstance(source['source_id'], str) or not source['source_id'] or not isinstance(source['content_sha256'], str) or len(source['content_sha256']) != 64:
        raise WorkspaceError('invalid_organization_plan')
    _attachment_name(source['relative_path'])
    target = plan['target']
    if plan['action'] == 'copy':
        if not isinstance(target, dict) or set(target) != {'source_id', 'relative_path'} or not isinstance(target['source_id'], str) or not target['source_id']:
            raise WorkspaceError('invalid_organization_plan')
        _attachment_name(target['relative_path'])
        if plan['destination_status'] not in ('absent', 'conflict'):
            raise WorkspaceError('invalid_organization_plan')
    elif target is not None or plan['destination_status'] != 'not_requested':
        raise WorkspaceError('invalid_organization_plan')
    if not isinstance(plan['impact'], dict) or plan['impact'].get('source_sha256') != source['content_sha256']:
        raise WorkspaceError('invalid_organization_plan')
    if plan['plan_sha256'] != _plan_hash(plan):
        raise WorkspaceError('organization_plan_changed')


class WorkspaceOrganizer:
    def __init__(self, vault, state, *, read_only=False):
        self.registry = Registry(vault, state)
        self.resolver = SourceResolver(self.registry, readers={}, cache_write=False)
        self.read_only = read_only

    def _source(self, workspace_id, grant_ref, source_id, name):
        _attachment_name(name)
        snapshot = self.registry.snapshot()
        descriptor = next((item for item in (snapshot or {}).get('sources', []) if item['id'] == source_id), None)
        if descriptor is None or descriptor['kind'] == 'vault':
            raise WorkspaceError('scope_denied')
        path, _, _ = self.resolver.resolve(workspace_id, source_id, name, grant_ref)
        raw = self.resolver._read_bytes(path)
        fresh, _, _ = self.resolver.resolve(workspace_id, source_id, name, grant_ref)
        if content_hash(self.resolver._read_bytes(fresh)) != content_hash(raw):
            raise WorkspaceError('stale_source')
        return path, raw

    def _target(self, workspace_id, grant_ref, source_id, name):
        _attachment_name(name)
        path, _, binding = self.resolver.resolve(workspace_id, source_id, name, grant_ref, access='write_copy')
        root=Path(binding['locator']).resolve()
        if root == self.registry.vault or not root.is_relative_to(self.registry.vault):
            raise WorkspaceError('copy_target_denied')
        if any(part.startswith('.') or part.casefold() in MANAGED_TARGET_PARTS
               for part in root.relative_to(self.registry.vault).parts):
            raise WorkspaceError('copy_target_denied')
        if any(part.casefold() in MANAGED_TARGET_PARTS for part in name.split('/')[:-1]):
            raise WorkspaceError('copy_target_denied')
        _alias_check(binding['locator'], name)
        return path

    def _impact(self, workspace_id, grant_ref, source_id, name):
        index = scoped_vault_index(self.registry.vault, self.registry.state, workspace_id, grant_ref)
        return plan_attached_source_impact(self.registry.vault, self.registry.state, index,
                                           workspace_id, grant_ref, source_id, name)

    def preview(self, workspace_id, grant_ref, source_id, source, *, action='link',
                target_source_id=None, target=None):
        if action not in ('link', 'unchanged', 'copy'):
            raise WorkspaceError('invalid_organization_action')
        source_path, raw = self._source(workspace_id, grant_ref, source_id, source)
        impact = self._impact(workspace_id, grant_ref, source_id, source)
        if impact['source_sha256'] != content_hash(raw):
            raise WorkspaceError('stale_source')
        destination = None
        status = 'not_requested'
        if action == 'copy':
            if not target_source_id or not target:
                raise WorkspaceError('copy_target_required')
            target_path = self._target(workspace_id, grant_ref, target_source_id, target)
            if Path(target).suffix.lower()!=Path(source).suffix.lower():
                raise WorkspaceError('copy_type_changed')
            if target_path == source_path:
                raise WorkspaceError('copy_self_denied')
            destination = {'source_id': target_source_id, 'relative_path': target}
            status = 'conflict' if target_path.exists() else 'absent'
        elif target_source_id is not None or target is not None:
            raise WorkspaceError('unexpected_copy_target')
        plan = {'contract_version': PLAN_VERSION, 'type': 'OrganizationPlan',
                'workspace_id': workspace_id, 'grant_ref': grant_ref, 'action': action,
                'source': {'source_id': source_id, 'relative_path': source,
                           'content_sha256': content_hash(raw)},
                'target': destination, 'impact': impact, 'destination_status': status}
        plan['plan_sha256'] = _plan_hash(plan)
        return plan

    def apply(self, plan):
        if self.read_only:
            raise WorkspaceError('read_only_runtime')
        _validate_plan(plan)
        with self.registry.coordinated():
            wid, grant = plan['workspace_id'], plan['grant_ref']
            source = plan['source']
            source_path, raw = self._source(wid, grant, source['source_id'], source['relative_path'])
            if content_hash(raw) != source['content_sha256']:
                raise WorkspaceError('stale_source')
            if self._impact(wid, grant, source['source_id'], source['relative_path']) != plan['impact']:
                raise WorkspaceError('organization_plan_changed')
            if plan['action'] != 'copy':
                return {'status': 'link_proposed' if plan['action']=='link' else 'unchanged',
                        'writes_performed': False, 'next_action':
                        'Use an approved canonical record or membership writer.' if plan['action']=='link' else None,
                        'plan_sha256': plan['plan_sha256'], 'source': source}
            target = plan['target']
            path = self._target(wid, grant, target['source_id'], target['relative_path'])
            if path == source_path:
                raise WorkspaceError('copy_self_denied')
            if Path(target['relative_path']).suffix.lower()!=Path(source['relative_path']).suffix.lower():
                raise WorkspaceError('copy_type_changed')
            journal = safe_path(self.registry.state, 'workspace-copy/' + plan['plan_sha256'] + '.json')
            expected = {'contract_version': JOURNAL_VERSION, 'plan_sha256': plan['plan_sha256'],
                        'workspace_id': wid, 'source': source, 'target': target}
            prior = None
            if journal.exists():
                from beyin_v3_workspaces import parse_document
                prior = parse_document(journal.read_bytes())
                if {k: prior.get(k) for k in expected} != expected or prior.get('status') not in ('pending', 'verified'):
                    raise WorkspaceError('copy_provenance_conflict')
            elif plan['destination_status'] != 'absent':
                raise WorkspaceError('destination_conflict')
            if not self.resolver.authority.allows_organization_plan(plan):
                raise WorkspaceError('plan_authority_required')
            if prior is not None and prior['status'] == 'verified':
                if not path.is_file() or content_hash(self.resolver._read_bytes(path)) != source['content_sha256']:
                    raise WorkspaceError('destination_changed')
                self._target(wid, grant, target['source_id'], target['relative_path'])
                return {'status': 'replayed', 'writes_performed': False,
                        'plan_sha256': plan['plan_sha256'], 'destination_sha256': source['content_sha256']}
            created=False
            if path.exists():
                if prior is None or not path.is_file() or content_hash(self.resolver._read_bytes(path)) != source['content_sha256']:
                    raise WorkspaceError('destination_conflict')
            else:
                if prior is None:
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    atomic(journal, canonical_bytes(dict(expected, status='pending')).decode('utf-8'))
                path.parent.mkdir(parents=True, exist_ok=True)
                self._target(wid, grant, target['source_id'], target['relative_path'])
                fd,temp_name=tempfile.mkstemp(prefix='.beyin-copy-',suffix='.tmp',dir=path.parent)
                temp=Path(temp_name)
                temp_identity=temp.stat()
                try:
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if content_hash(self.resolver._read_bytes(temp)) != source['content_sha256']:
                        raise WorkspaceError('copy_stage_changed')
                    self._target(wid, grant, target['source_id'], target['relative_path'])
                    try:
                        os.link(temp, path)
                    except FileExistsError as exc:
                        raise WorkspaceError('destination_conflict') from exc
                    except OSError as exc:
                        raise WorkspaceError('exclusive_publish_unavailable') from exc
                    created=True
                finally:
                    # Only remove this invocation's staging file. An older
                    # interrupted temp belongs to recovery, not this replay.
                    try:
                        now=temp.stat()
                        if (now.st_dev,now.st_ino)==(temp_identity.st_dev,temp_identity.st_ino):
                            temp.unlink()
                    except FileNotFoundError:
                        pass
            # Both source and target grants, source bytes, and target bytes are
            # checked again before recording a verified effect.
            fresh_source, fresh_raw = self._source(wid, grant, source['source_id'], source['relative_path'])
            self._target(wid, grant, target['source_id'], target['relative_path'])
            if fresh_source != source_path or content_hash(fresh_raw) != source['content_sha256']:
                raise WorkspaceError('stale_source')
            if content_hash(self.resolver._read_bytes(path)) != source['content_sha256']:
                raise WorkspaceError('destination_changed')
            atomic(journal, canonical_bytes(dict(expected, status='verified')).decode('utf-8'))
            return {'status': 'copied' if created else 'reconciled', 'writes_performed': created,
                    'plan_sha256': plan['plan_sha256'], 'destination_sha256': source['content_sha256']}
