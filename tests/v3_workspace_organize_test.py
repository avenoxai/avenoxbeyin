"""P5a physical copy and no-write organization regressions."""
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
import sys
import json
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

from beyin_v3_workspace_organize import ANCHORED_PUBLISH, WorkspaceOrganizer
from beyin_v3_workspaces import (WorkspaceError, envelope, render_document,
                                  content_hash)
from v3_workspace_helpers import install_fixture

SPEC = importlib.util.spec_from_file_location('organization_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class OrganizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime, _, _ = install_fixture(self.base, 'academic')
        self.target_root = self.runtime.registry.vault / 'assets'
        self.target_root.mkdir()
        self.runtime.registry.compare_and_save(envelope('SourceDescriptor',
            dict(id='academic-working', kind='directory', label='Working assets')), None)
        profile_path = self.runtime.registry.vault / '.beyin-workspaces/workspaces/academic.json'
        old = self.runtime.registry.read(self.runtime.registry.vault,
            '.beyin-workspaces/workspaces/academic.json', 'WorkspaceProfile')
        old['data']['revision'] += 1
        old['data']['source_ids'].append('academic-working')
        self.runtime.registry.compare_and_save(old, content_hash(profile_path.read_bytes()))
        binding = dict(source_id='academic-working', machine_id='fixture-machine', revision=1,
            locator=str(self.target_root), grants=[dict(workspace_id='academic', access='read')],
            authority_ref='academic-user')
        binding_path = self.runtime.registry.state / 'workspace-bindings/academic-working.json'
        binding_path.write_bytes(render_document(envelope('SourceBinding', binding)))
        self.runtime.activate()
        self.organizer = WorkspaceOrganizer(self.runtime.registry.vault, self.runtime.registry.state)
        self.source = self.base / 'academic-source-a/logo.png'
        self.source.write_bytes(b'\x89PNG\r\noriginal\x00bytes')
        self.target = self.target_root / 'working/logo.png'
        self.source_hash = self.source.read_bytes()

    def permit_copy(self):
        bindings, _ = self.runtime.registry.bindings()
        permissions = [dict(workspace_id='academic', source_id=b['source_id'],
                            locator=b['locator'], access=g['access'])
                       for b in bindings.values() for g in b['grants']]
        permissions.append(dict(workspace_id='academic', source_id='academic-working',
                                locator=str(self.target_root), access='write_copy'))
        self.runtime.authority.record_approval('academic-user', machine_id='fixture-machine',
                                               permissions=permissions,
                                               evidence='Synthetic approved logo working copy')

    def preview(self, **kwargs):
        return self.organizer.preview('academic', 'academic-user', 'academic-materials', 'logo.png', **kwargs)

    def approve(self, plan):
        self.runtime.authority.approve_organization_plan(plan, evidence='Synthetic exact copy plan approval')

    def test_link_and_unchanged_default_do_not_write(self):
        for action in ('link', 'unchanged'):
            plan = self.preview(action=action)
            self.assertEqual(plan['contract_version'], 'workspace-organization/0.1.0')
            self.assertEqual(self.organizer.apply(plan)['status'],
                             'link_proposed' if action == 'link' else 'unchanged')
            self.assertFalse(self.target.exists())
        self.assertEqual(self.preview()['action'], 'link')
        accepted = self.base / 'academic-source-a/accepted-offer.pdf'
        accepted.write_bytes(b'%PDF-1.4\naccepted offer')
        linked = self.organizer.preview('academic', 'academic-user', 'academic-materials',
                                        'accepted-offer.pdf')
        self.assertEqual(self.organizer.apply(linked)['status'], 'link_proposed')
        self.assertEqual(accepted.read_bytes(), b'%PDF-1.4\naccepted offer')
        self.assertEqual(self.source.read_bytes(), self.source_hash)

    def test_exact_copy_and_replay_preserve_source_and_destination(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.assertEqual(plan['destination_status'], 'absent')
        with self.assertRaisesRegex(WorkspaceError, 'plan_authority_required'):
            self.organizer.apply(plan)
        self.approve(plan)
        self.assertEqual(self.organizer.apply(plan)['status'], 'copied')
        self.assertEqual(self.target.read_bytes(), self.source_hash)
        self.assertEqual(self.source.read_bytes(), self.source_hash)
        self.assertEqual(self.organizer.apply(plan)['status'], 'replayed')
        self.assertEqual(list(self.target_root.rglob('logo.png')), [self.target])
        self.assertEqual(self.target.read_bytes(), self.source_hash)

    def test_copy_requires_separate_target_authority_and_refuses_markdown(self):
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.permit_copy()
        with self.assertRaisesRegex(WorkspaceError, 'unsupported_copy_type'):
            self.organizer.preview('academic', 'academic-user', 'academic-materials', 'material.txt',
                                   action='copy', target_source_id='academic-working', target='copy.md')
        self.assertFalse(self.target.exists())

    def test_external_directory_stays_read_only_with_copy_grant(self):
        self.permit_copy()
        bindings, _ = self.runtime.registry.bindings()
        external = bindings['academic-extra']['locator']
        current = self.runtime.authority._load()['grants']['academic-user']['permissions']
        extra = dict(workspace_id='academic', source_id='academic-extra',
                     locator=external, access='write_copy')
        self.runtime.authority.record_approval('academic-user', machine_id='fixture-machine',
            permissions=current + [extra], evidence='Synthetic external target challenge')
        with self.assertRaisesRegex(WorkspaceError, 'copy_target_denied'):
            self.preview(action='copy', target_source_id='academic-extra', target='working/logo.png')
        self.assertFalse((Path(external) / 'working/logo.png').exists())

    def test_managed_vault_directory_and_code_suffix_are_denied(self):
        self.permit_copy()
        code = self.base / 'academic-source-a/tool.py'
        code.write_bytes(b'print(1)')
        with self.assertRaisesRegex(WorkspaceError, 'unsupported_copy_type'):
            self.organizer.preview('academic', 'academic-user', 'academic-materials',
                'tool.py', action='copy', target_source_id='academic-working', target='tool.py')
        managed = self.runtime.registry.vault / '.claude/scripts'
        managed.mkdir(parents=True, exist_ok=True)
        binding_path = self.runtime.registry.state / 'workspace-bindings/academic-working.json'
        binding = self.runtime.registry.read(self.runtime.registry.state,
            'workspace-bindings/academic-working.json', 'SourceBinding')
        binding['data']['locator'] = str(managed)
        binding['data']['revision'] += 1
        binding_path.write_bytes(render_document(binding))
        bindings, _ = self.runtime.registry.bindings()
        permissions = [dict(workspace_id='academic', source_id=b['source_id'],
                            locator=b['locator'], access=g['access'])
                       for b in bindings.values() for g in b['grants']]
        permissions.append(dict(workspace_id='academic', source_id='academic-working',
                                locator=str(managed), access='write_copy'))
        self.runtime.authority.record_approval('academic-user', machine_id='fixture-machine',
            permissions=permissions, evidence='Synthetic managed directory challenge')
        with self.assertRaisesRegex(WorkspaceError, 'copy_target_denied'):
            self.preview(action='copy', target_source_id='academic-working', target='logo.png')
        self.assertFalse((managed / 'logo.png').exists())

    def test_conflict_and_tampering_never_overwrite(self):
        self.permit_copy()
        self.target.parent.mkdir()
        self.target.write_bytes(b'user edit')
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.assertEqual(plan['destination_status'], 'conflict')
        with self.assertRaisesRegex(WorkspaceError, 'destination_conflict'):
            self.organizer.apply(plan)
        self.assertEqual(self.target.read_bytes(), b'user edit')
        self.target.unlink()
        ready = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        ready['target']['relative_path'] = 'working/other.png'
        with self.assertRaisesRegex(WorkspaceError, 'organization_plan_changed'):
            self.organizer.apply(ready)

    def test_stale_source_revocation_and_alias_block_copy(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(WorkspaceError, 'stale_source'):
            self.organizer.apply(plan)
        self.assertFalse(self.target.exists())
        self.source.write_bytes(self.source_hash)
        self.runtime.authority.revoke('academic-user')
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            self.organizer.apply(plan)
        self.permit_copy()
        (self.target_root / 'Working').mkdir()
        with self.assertRaisesRegex(WorkspaceError, 'destination_alias_conflict'):
            self.organizer.apply(plan)
        self.assertFalse(self.target.exists())

    def test_pending_journal_reconciles_same_bytes_but_partial_file_conflicts(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        from beyin_v3_workspace_organize import JOURNAL_VERSION
        from beyin_v3_workspaces import canonical_bytes
        journal = self.runtime.registry.state / 'workspace-copy' / (plan['plan_sha256'] + '.json')
        journal.parent.mkdir(parents=True)
        expected = dict(contract_version=JOURNAL_VERSION, plan_sha256=plan['plan_sha256'],
                        workspace_id='academic', source=plan['source'], target=plan['target'],
                        status='pending')
        journal.write_bytes(canonical_bytes(expected))
        self.target.parent.mkdir()
        self.target.write_bytes(b'partial')
        with self.assertRaisesRegex(WorkspaceError, 'destination_conflict'):
            self.organizer.apply(plan)
        self.assertEqual(self.target.read_bytes(), b'partial')
        self.target.write_bytes(self.source_hash)
        self.assertEqual(self.organizer.apply(plan)['status'], 'reconciled')
        self.assertEqual(self.organizer.apply(plan)['status'], 'replayed')

    def test_readback_failure_keeps_pending_and_never_duplicates(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        original = self.organizer._target_bytes
        def corrupt_read(path):
            if path == self.target and path.exists():
                return b'corrupt readback'
            return original(path)
        with patch.object(self.organizer, '_target_bytes', side_effect=corrupt_read):
            with self.assertRaisesRegex(WorkspaceError, 'destination_changed'):
                self.organizer.apply(plan)
        self.assertEqual(self.target.read_bytes(), self.source_hash)
        self.assertEqual(self.organizer.apply(plan)['status'], 'reconciled')
        self.assertEqual(list(self.target_root.rglob('logo.png')), [self.target])

    def test_exclusive_publish_failure_leaves_no_partial_final(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        with patch('beyin_v3_workspace_organize.os.link', side_effect=OSError('hard links unavailable')):
            with self.assertRaisesRegex(WorkspaceError, 'exclusive_publish_unavailable'):
                self.organizer.apply(plan)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target_root.rglob('*.tmp')), [])
        self.assertEqual(self.organizer.apply(plan)['status'], 'copied')

    def test_pending_replay_does_not_delete_older_staging_file(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        from beyin_v3_workspace_organize import JOURNAL_VERSION
        from beyin_v3_workspaces import canonical_bytes
        journal = self.runtime.registry.state / 'workspace-copy' / (plan['plan_sha256'] + '.json')
        journal.parent.mkdir(parents=True)
        journal.write_bytes(canonical_bytes(dict(contract_version=JOURNAL_VERSION,
            plan_sha256=plan['plan_sha256'], workspace_id='academic', source=plan['source'],
            target=plan['target'], status='pending')))
        self.target.parent.mkdir()
        older = self.target.parent / (self.target.name + '.beyin-copy-' + plan['plan_sha256'][:16] + '.tmp')
        older.write_bytes(b'older human or interrupted bytes')
        self.assertEqual(self.organizer.apply(plan)['status'], 'copied')
        self.assertEqual(older.read_bytes(), b'older human or interrupted bytes')

    def test_unknown_version_field_and_changed_destination_are_rejected(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        for edit in (dict(contract_version='workspace-organization/9.0.0'),
                     dict(unexpected=True), dict(plan_sha256='0' * 64)):
            with self.subTest(edit=edit):
                with self.assertRaises(WorkspaceError):
                    self.organizer.apply(dict(plan, **edit))
        self.organizer.apply(plan)
        self.target.write_bytes(b'manual destination edit')
        with self.assertRaisesRegex(WorkspaceError, 'destination_changed'):
            self.organizer.apply(plan)
        self.assertEqual(self.target.read_bytes(), b'manual destination edit')

    def test_target_symlink_and_unicode_alias_are_rejected(self):
        self.permit_copy()
        other = self.target_root / 'elsewhere'
        other.mkdir()
        (self.target_root / 'working').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, 'redirected_path'):
            self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        (self.target_root / 'working').unlink()
        (self.target_root / 'Café').mkdir()
        with self.assertRaisesRegex(WorkspaceError, 'destination_alias_conflict'):
            self.preview(action='copy', target_source_id='academic-working', target='Cafe\u0301/logo.png')

    @unittest.skipUnless(ANCHORED_PUBLISH, 'directory-descriptor publish is unavailable')
    def test_parent_swap_during_publish_cannot_write_outside_vault(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        outside = self.base / 'outside'
        outside.mkdir()
        moved = self.target_root / 'moved-working'
        original_link = __import__('os').link

        def swap_then_link(source, destination, **kwargs):
            self.target.parent.rename(moved)
            self.target.parent.symlink_to(outside, target_is_directory=True)
            return original_link(source, destination, **kwargs)

        with patch('beyin_v3_workspace_organize.os.link', side_effect=swap_then_link):
            with self.assertRaisesRegex(WorkspaceError, 'redirected_path'):
                self.organizer.apply(plan)
        self.assertFalse((outside / 'logo.png').exists())
        self.assertEqual((moved / 'logo.png').read_bytes(), self.source_hash)

    @unittest.skipUnless(ANCHORED_PUBLISH, 'directory-descriptor fsync is unavailable')
    def test_directory_sync_failure_keeps_journal_pending(self):
        self.permit_copy()
        plan = self.preview(action='copy', target_source_id='academic-working', target='working/logo.png')
        self.approve(plan)
        original_fsync = os.fsync
        def fail_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory sync unavailable')
            return original_fsync(fd)
        with patch('beyin_v3_workspace_organize.os.fsync', side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(WorkspaceError, 'copy_durability_unavailable'):
                self.organizer.apply(plan)
        journal = self.runtime.registry.state / 'workspace-copy' / (plan['plan_sha256'] + '.json')
        self.assertEqual(json.loads(journal.read_text(encoding='utf-8'))['status'], 'pending')
        self.assertEqual(self.target.read_bytes(), self.source_hash)
        self.assertEqual(self.organizer.apply(plan)['status'], 'reconciled')

    def test_cli_preview_and_apply_use_same_plan(self):
        self.permit_copy()
        request_path = self.base / 'request.json'
        request_path.write_text(json.dumps(dict(workspace_id='academic', grant_ref='academic-user',
            source_id='academic-materials', source='logo.png', action='copy',
            target_source_id='academic-working', target='working/logo.png')), encoding='utf-8')
        flags = ['--vault', str(self.runtime.registry.vault), '--state', str(self.runtime.registry.state)]
        plan, code = cli.main(flags + ['workspace-organize', 'preview', '--file', str(request_path)], return_result=True)
        self.assertEqual(code, 0)
        approval = self.base / 'approval.json'
        approval.write_text(json.dumps(dict(plan=plan, evidence='Synthetic exact approval')), encoding='utf-8')
        authorized, code = cli.main(flags + ['workspace-authority', 'approve-organization',
                                               '--file', str(approval)], return_result=True)
        self.assertEqual(code, 0)
        self.assertEqual(authorized['data']['operation_status'], 'applied')
        request_path.write_text(json.dumps(plan), encoding='utf-8')
        result, code = cli.main(flags + ['workspace-organize', 'apply', '--file', str(request_path)], return_result=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'copied')
        self.assertEqual(self.target.read_bytes(), self.source_hash)


if __name__ == '__main__':
    unittest.main()
