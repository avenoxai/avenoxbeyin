"""CI regressions for compact setup; local runs are intentionally not required."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_workspace_setup as setup
from beyin_v3_workspace_views import merge_block, refresh_after_apply, organize
from beyin_v3_sync import SyncEngine, parse, render
from beyin_v3_workspaces import LocalAuthority, Registry, WorkspaceError, envelope, validate_document
from beyin_v3_builder import provisioner
from beyin_v3_workspace_context import WorkspaceRuntime
from beyin_v3_workspace_memory import MemoryIntentService


def artifacts(result):
    validate_document(result)
    return json.loads(next(i['detail'] for i in result['data']['issues'] if i['code'] == 'setup_artifacts'))


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.state = self.root / 'state'
        self.source = self.root / 'source'; self.source.mkdir()
        self.payload = dict(workspace_id='sample', name='Kullanıcı alanı', purpose='İşleri izlemek',
            sources=[{'path': str(self.source), 'name': 'Belgeler'}])

    def test_prepare_is_no_write_no_probe_and_uses_real_g0_plan(self):
        with patch('beyin_v3_sources.probe_capabilities', side_effect=AssertionError('probe forbidden')):
            result, code = setup.execute(self.vault, self.state, 'prepare', self.payload)
        self.assertEqual(code, 0)
        data = artifacts(result)
        validate_document(data['spec']); validate_document(data['plan'])
        self.assertEqual(data['homepage'], 'notes/workspaces/sample.md')
        self.assertEqual(list(self.vault.iterdir()), [])
        self.assertFalse(self.state.exists())
        custom = dict(self.payload, homepage='notes/project/home.md')
        result, code = setup.execute(self.vault, self.state, 'prepare', custom)
        self.assertEqual(code, 0)
        self.assertEqual(artifacts(result)['spec']['data']['desired_profile']['note_roots'], ['notes/project'])
        self.assertFalse(self.state.exists())
        with self.assertRaises(WorkspaceError):
            provisioner(self.vault, self.state).apply(data['plan'], 'unapproved')

    def test_catalog_is_bounded_metadata_and_rejects_redirected_roots(self):
        (self.source / 'secret.txt').write_text('DO_NOT_EXTRACT_CANARY')
        sub = self.source / 'nested'; sub.mkdir(); (sub / 'hidden.txt').write_text('CANARY')
        result, code = setup.execute(self.vault, self.state, 'catalog', dict(sources=self.payload['sources'], max_items=1, max_depth=0))
        self.assertEqual(code, 0); data = artifacts(result)
        self.assertLessEqual(len(data['catalog']), 1)
        self.assertTrue(data['truncated'])
        self.assertNotIn('DO_NOT_EXTRACT_CANARY', json.dumps(result))
        self.assertFalse(self.state.exists())
        alias = self.root / 'alias'
        try: alias.symlink_to(self.source, target_is_directory=True)
        except OSError: self.skipTest('symlink unavailable')
        _, code = setup.execute(self.vault, self.state, 'catalog', {'sources': [{'path': str(alias), 'name': 'Alias'}]})
        self.assertNotEqual(code, 0)

    def test_design_is_carried_into_exact_plan_and_rejects_policy_overlays(self):
        design = dict(record_types=[dict(id='trial', label='Deneme', fields={'result': 'string'})],
            relation_types=['based_on'], working_rules=[dict(id='evidence', text='Kaynağı belirt.', subject_refs=[])], workflows=[])
        result, code = setup.execute(self.vault, self.state, 'prepare', dict(self.payload, design=design))
        self.assertEqual(code, 0, result)
        profile = artifacts(result)['spec']['data']['desired_profile']
        for field, value in design.items():
            self.assertEqual(profile[field], value)
        self.assertFalse(self.state.exists())
        denied, code = setup.execute(self.vault, self.state, 'stage', dict(self.payload,
            design={'access_policy': {'cross_workspace': 'all'}}))
        self.assertEqual(code, 2, denied)
        self.assertFalse(self.state.exists())

    def test_real_authorized_write_refresh_replay_and_user_body_preservation(self):
        prepared, _ = setup.execute(self.vault, self.state, 'prepare', self.payload)
        data = artifacts(prepared); registry = Registry(self.vault, self.state); authority = LocalAuthority(registry)
        spec = data['spec']['data']
        permissions = [dict(workspace_id='sample', source_id=b['source_id'], locator=b['locator'], access=g['access'])
            for b in spec['binding_requests'] for g in b['grants']]
        authority.record_approval('setup-review', machine_id=spec['binding_requests'][0]['machine_id'],
            permissions=permissions, evidence='Explicit synthetic scope approval')
        authority.approve_plan(data['plan'], evidence='Explicit synthetic exact plan approval')
        provisioner(self.vault, self.state).apply(data['plan'], 'setup')
        second = copy.deepcopy(self.payload); second['workspace_id'] = 'another'
        next_result, next_code = setup.execute(self.vault, self.state, 'prepare', second)
        self.assertEqual(next_code, 0)
        self.assertNotEqual(artifacts(next_result)['plan']['data']['snapshot_sha256'], data['plan']['data']['snapshot_sha256'])
        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        request = envelope('RequestContext', dict(request_id='request', session_id='synthetic', client='test', query='İşler',
            grant_ref='setup-review', audience='internal', no_memory=False, budget_chars=10000,
            signals=[dict(kind='explicit_user', workspace_id='sample', subject_refs=[])]))
        context = runtime.context(request)['data']
        source_id = next(s['id'] for s in spec['sources'] if s['kind'] == 'vault')
        intent = envelope('MemoryWriteIntent', dict(intent_id='create-item', workspace_id='sample',
            context_token=context['context_token'], profile_revision=1, binding_snapshot_sha256=context['binding_snapshot_sha256'],
            idempotency_key='create-item', reason='Explicit synthetic note', changes=[dict(id='create-item', action='create_note',
                target=dict(record_id='item', source_id=source_id, relative_path='notes/workspaces/item.md', expected_revision=None, expected_source_sha256=None),
                body='Kayıt gövdesi', metadata_changes={'title': 'Örnek iş'}, relation_changes=[], evidence_refs=[])]))
        result = MemoryIntentService(runtime).apply(intent)
        canonical_parts = copy.deepcopy(result['data']['parts'])
        # Also works when central apply already refreshed; explicit replay is safe.
        refreshed = refresh_after_apply(runtime, intent, result)
        self.assertNotIn('managed_view_stale', [i['code'] for i in refreshed['data']['issues']])
        self.assertEqual(refreshed['data']['parts'], canonical_parts)
        self.assertEqual(result['data']['parts'], canonical_parts)
        page = self.vault / data['homepage']; self.assertTrue(page.is_file())
        self.assertIn('[Örnek iş](item.md)', page.read_text(encoding='utf-8'))
        before = page.read_bytes()
        refresh_after_apply(runtime, intent, result)
        self.assertEqual(page.read_bytes(), before)
        # Existing user body includes CRLF and a final suffix without LF.
        metadata, managed = parse(page.read_bytes().decode('utf-8'))
        user_body = 'Üst kullanıcı\r\n' + managed.rstrip('\n') + '\r\nAlt kullanıcı'
        page.write_bytes(render(metadata, user_body).encode('utf-8'))
        # Change a listed record so this exercises canonical update, not no-op.
        item_path = self.vault / 'notes/workspaces/item.md'
        item_metadata, item_body = parse(item_path.read_bytes().decode('utf-8'))
        item_metadata['title'] = 'Yenilenen iş'
        item_path.write_bytes(render(item_metadata, item_body).encode('utf-8'))
        SyncEngine(self.vault, self.state).sync()
        with patch.object(runtime.resolver, 'resolve', wraps=runtime.resolver.resolve) as resolve:
            organize(runtime, intent, 'refresh-user-body')
        self.assertEqual(resolve.call_args_list[0].kwargs['access'], 'write_memory')
        self.assertEqual(resolve.call_args_list[1].kwargs['access'], 'read')
        self.assertEqual(resolve.call_args_list[0].args[2], data['homepage'])
        _, updated_body = parse(page.read_bytes().decode('utf-8'))
        self.assertIn('Yenilenen iş', updated_body)
        self.assertTrue(updated_body.startswith('Üst kullanıcı\r\n'))
        self.assertTrue(updated_body.endswith('\r\nAlt kullanıcı'))
        # Duplicate markers never overwrite the existing page.
        metadata, current_body = parse(page.read_bytes().decode('utf-8'))
        page.write_bytes(render(metadata, current_body + current_body).encode('utf-8'))
        SyncEngine(self.vault, self.state).sync(); before = page.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, 'managed_view_marker_conflict'):
            organize(runtime, intent, 'refresh-duplicate')
        self.assertEqual(page.read_bytes(), before)
        authority.revoke('setup-review')
        denied = refresh_after_apply(runtime, intent, result)
        self.assertIn('managed_view_stale', [i['code'] for i in denied['data']['issues']])
        self.assertEqual(denied['data']['parts'], canonical_parts)
        self.assertEqual(result['data']['parts'], canonical_parts)
        self.assertEqual(page.read_bytes(), before)

    def test_private_request_keeps_shared_homepage_at_internal_audience(self):
        prepared, _ = setup.execute(self.vault, self.state, 'prepare', self.payload)
        data = artifacts(prepared); spec = data['spec']['data']
        registry = Registry(self.vault, self.state); authority = LocalAuthority(registry)
        permissions = [dict(workspace_id='sample', source_id=b['source_id'], locator=b['locator'], access=g['access'])
            for b in spec['binding_requests'] for g in b['grants']]
        authority.record_approval('setup-review', machine_id=spec['binding_requests'][0]['machine_id'],
            permissions=permissions, audiences=('internal', 'private'), evidence='Explicit synthetic private read approval')
        authority.approve_plan(data['plan'], evidence='Explicit synthetic exact plan approval')
        provisioner(self.vault, self.state).apply(data['plan'], 'private-setup')
        for visibility in ('public', 'internal', 'private'):
            metadata = dict(id=visibility + '-item', kind='note', revision=1, workspace_id='sample',
                visibility=visibility, title=visibility + '-TITLE-CANARY', status=visibility + '-STATUS-CANARY')
            path = self.vault / ('notes/workspaces/' + visibility + '-PATH-CANARY.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(render(metadata, visibility + ' body\n').encode('utf-8'))
        SyncEngine(self.vault, self.state).sync()
        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        request = envelope('RequestContext', dict(request_id='private-request', session_id='synthetic', client='test', query='İşler',
            grant_ref='setup-review', audience='private', no_memory=False, budget_chars=10000,
            signals=[dict(kind='explicit_user', workspace_id='sample',
                subject_refs=[dict(workspace_id='sample', record_id='private-item')])]))
        context = runtime.context(request)['data']
        self.assertIn('private-item', [record['id'] for record in context['records']])
        source_id = next(source['id'] for source in spec['sources'] if source['kind'] == 'vault')
        intent = envelope('MemoryWriteIntent', dict(intent_id='private-organize', workspace_id='sample',
            context_token=context['context_token'], profile_revision=1, binding_snapshot_sha256=context['binding_snapshot_sha256'],
            idempotency_key='private-organize', reason='Explicit synthetic refresh', changes=[]))
        # The selection gate must use the destination audience for source metadata too.
        allows = runtime.authority.allows
        def destination_audience(reference, binding, wid, access='read', audience='internal'):
            if binding['source_id'] != source_id and audience == 'internal':
                return False
            return allows(reference, binding, wid, access, audience)
        with patch.object(runtime.authority, 'allows', side_effect=destination_audience):
            result = organize(runtime, intent, 'private-view')
        self.assertEqual(result['data']['operation_status'], 'applied')
        metadata, body = parse((self.vault / data['homepage']).read_bytes().decode('utf-8'))
        self.assertEqual(metadata['visibility'], 'internal')
        for visibility in ('public', 'internal'):
            self.assertIn(visibility + '-TITLE-CANARY', body)
            self.assertIn(visibility + '-PATH-CANARY.md', body)
            self.assertIn(visibility + '-STATUS-CANARY', body)
        for canary in ('private-item', 'private-TITLE-CANARY', 'private-PATH-CANARY.md', 'private-STATUS-CANARY',
                       'Belgeler', self.source.as_uri()):
            self.assertNotIn(canary, body)

    def test_marker_conflict_and_exact_outside_body_preservation(self):
        body = 'Kullanıcı üst\n<!-- beyin-workspace:sample:start -->\neski\n<!-- beyin-workspace:sample:end -->\nKullanıcı alt\n'
        merged = merge_block(body, 'sample', 'yeni\n')
        self.assertEqual(merged.split('<!-- beyin-workspace:sample:start -->')[0], 'Kullanıcı üst\n')
        self.assertTrue(merged.endswith('\nKullanıcı alt\n'))
        for invalid in (body + body, body.replace(':end -->', ':broken -->'), body.replace(':start -->', ':end -->', 1)):
            with self.assertRaises(WorkspaceError): merge_block(invalid, 'sample', 'new')


if __name__ == '__main__': unittest.main()
