import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'template/.claude/scripts'))
import beyin_v3_workspaces as ws
from v3_workspace_test_io import load_utf8_json

FIX=ROOT/'tests/fixtures/v3/workspaces/v0.1.0'


class WorkspaceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.vault=self.base/'vault';self.vault.mkdir()
        self.state=self.base/'state';self.registry=ws.Registry(self.vault,self.state)
        self.source=self.base/'materials';self.source.mkdir()

    def test_embedded_schema_exactly_matches_g0(self):
        self.assertEqual(ws.SCHEMA,load_utf8_json(ROOT/'docs/v3/workspaces/v0.1.0/schema.json'))

    def test_all_frozen_documents_and_bad_cases(self):
        count=0
        for path in FIX.rglob('*.json'):
            for doc in load_utf8_json(path).get('documents',[]):
                ws.validate_document(doc);count+=1
                if doc['type']=='ProvisionPlan':
                    for op in doc['data']['operations']:
                        ws.validate_document(ws.parse_document(op['content_utf8']));count+=1
        self.assertEqual(count,76)
        for case in load_utf8_json(FIX/'schema-negative.json')['cases']:
            with self.subTest(case=case['id']),self.assertRaises(ws.WorkspaceError):
                ws.validate_document(case['document'])

    def test_json_protocol_preserves_utf8_bytes_and_string_callers(self):
        value = {'text': 'Türkçe — 日本語 😀', 'body': 'önce\r\nsonra'}
        text = json.dumps(value, ensure_ascii=False)
        for raw in (text, text.encode('utf-8'), bytearray(text.encode('utf-8'))):
            with self.subTest(input_type=type(raw).__name__):
                self.assertEqual(ws.parse_document(raw), value)

    def test_json_protocol_rejects_utf16_and_utf32_bytes(self):
        text = json.dumps({'text': 'Türkçe'}, ensure_ascii=False)
        for encoding in ('utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be'):
            for buffer_type in (bytes, bytearray):
                with self.subTest(encoding=encoding, input_type=buffer_type.__name__):
                    with self.assertRaisesRegex(ws.WorkspaceError, '^invalid_json$'):
                        ws.parse_document(buffer_type(text.encode(encoding)))

    def test_duplicate_json_keys_and_invalid_numbers(self):
        for raw in [b'{"id":1,"id":2}',b'{"number":NaN}',b'{"number":Infinity}']:
            with self.assertRaises(ws.WorkspaceError):ws.parse_document(raw)

    def test_paths_are_not_strings_with_prefix_permission(self):
        for name in ['../escape','/absolute','a/../b','C:/file','a\\b','a//b','a/CON.txt']:
            with self.subTest(name=name),self.assertRaises(ws.WorkspaceError):ws.safe_path(self.vault,name)
        (self.vault/'linked').symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(ws.WorkspaceError):ws.safe_path(self.vault,'linked/file')

    def test_snapshot_is_pure_and_absent_registry_stays_legacy(self):
        self.assertIsNone(self.registry.snapshot())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.registry.snapshot(registry_id='test-brain')['files'],[])
        self.assertEqual(list(self.vault.iterdir()),[])

    def test_per_file_cas_and_revision_protect_user_edit(self):
        fmt=ws.envelope('RegistryFormat',{'registry_id':'test-brain','layout':'per_workspace_json'})
        digest=self.registry.compare_and_save(fmt,None)
        self.assertEqual(self.registry.snapshot()['registry_id'],'test-brain')
        with self.assertRaises(ws.WorkspaceError):self.registry.compare_and_save(fmt,None)
        source=ws.envelope('SourceDescriptor',{'id':'materials','kind':'directory','label':'Dersler'})
        self.registry.compare_and_save(source,None)
        profile=copy.deepcopy(load_utf8_json(FIX/'academic/golden.json')['documents'][0])
        profile['data']['source_ids']=['materials']
        h=self.registry.compare_and_save(profile,None)
        profile['data']['revision']=2
        self.registry.compare_and_save(profile,h)
        with self.assertRaises(ws.WorkspaceError):self.registry.compare_and_save(profile,h)
        self.assertEqual(self.registry.snapshot()['profiles'][0]['revision'],2)

    def test_pending_publication_and_corrupt_registry_fail_closed(self):
        self.state.mkdir();pending=self.state/'workspace-provision';pending.mkdir();(pending/'pending.json').write_text('{}')
        with self.assertRaisesRegex(ws.WorkspaceError,'operation_pending'):self.registry.snapshot()
        (pending/'pending.json').unlink()
        (self.vault/'.beyin-workspaces').mkdir();(self.vault/'.beyin-workspaces/format.json').write_text('{broken')
        with self.assertRaises(ws.WorkspaceError):self.registry.snapshot()

    def test_workflow_cycle_and_forward_reference(self):
        profile=load_utf8_json(FIX/'academic/golden.json')['documents'][0]
        profile['data']['workflows'][0]['steps'][0]['depends_on']=['verify']
        with self.assertRaisesRegex(ws.WorkspaceError,'workflow_cycle'):ws.validate_document(profile)
        profile=load_utf8_json(FIX/'academic/golden.json')['documents'][0]
        profile['data']['workflows'][0]['steps'][0]['inputs']['result']={'ref':'steps.verify.output'}
        with self.assertRaisesRegex(ws.WorkspaceError,'forward_reference'):ws.validate_document(profile)

    def test_local_grant_is_not_inferred_from_binding(self):
        auth=ws.LocalAuthority(self.registry)
        binding={'source_id':'materials','machine_id':'machine','revision':1,'locator':str(self.source),
                 'grants':[{'workspace_id':'academic','access':'read'}],'authority_ref':'user'}
        self.assertFalse(auth.allows('user',binding,'academic'))
        permission={'source_id':'materials','workspace_id':'academic','access':'read','locator':str(self.source)}
        auth.record_approval('user',machine_id='machine',permissions=[permission],evidence='Explicit synthetic user request')
        self.assertTrue(auth.allows('user',binding,'academic'))
        self.assertFalse(auth.allows('user',binding,'other'))
        self.assertFalse(auth.allows('user',binding,'academic',audience='private'))
        auth.revoke('user');self.assertFalse(auth.allows('user',binding,'academic'))

    def test_local_approval_rejects_non_system_symlink(self):
        linked = self.base/'linked-local-source'
        linked.symlink_to(self.source, target_is_directory=True)
        auth = ws.LocalAuthority(self.registry)
        with self.assertRaisesRegex(ws.WorkspaceError, 'invalid_local_locator'):
            auth.record_approval('user', machine_id='machine', permissions=[{
                'workspace_id':'academic', 'source_id':'materials',
                'locator':str(linked), 'access':'read'}],
                evidence='Synthetic consent for a redirected source')
        self.assertFalse(self.state.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows path spelling aliases')
    def test_windows_casefolded_locator_is_not_a_redirect(self):
        # Windows path names can differ in case and in 8.3/long-name spelling.
        alias = Path(str(self.source).swapcase())
        self.assertTrue(alias.is_dir())
        auth = ws.LocalAuthority(self.registry)
        auth.record_approval('windows-alias', machine_id='machine', permissions=[{
            'workspace_id':'academic', 'source_id':'materials',
            'locator':str(alias), 'access':'read'}],
            evidence='Explicit synthetic approval of the same directory')
        self.assertIn('windows-alias', auth._load()['grants'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS /var system alias only')
    def test_macos_var_alias_keeps_exact_grant_and_canonical_vault_check(self):
        system_alias = Path('/var')
        canonical = Path('/private/var')
        if not system_alias.is_symlink() or system_alias.resolve() != canonical:
            self.skipTest('System /var alias is not present')
        if not self.source.is_relative_to(canonical):
            self.skipTest('Temporary directory is outside /private/var')
        source_alias = system_alias / self.source.relative_to(canonical)
        vault_alias = system_alias / self.vault.relative_to(canonical)
        auth = ws.LocalAuthority(self.registry)
        auth.record_approval('user', machine_id='machine', permissions=[{
            'workspace_id':'academic', 'source_id':'materials',
            'locator':str(source_alias), 'access':'read'}, {
            'workspace_id':'academic', 'source_id':'vault',
            'locator':str(vault_alias), 'access':'write_memory'}],
            evidence='Synthetic approval for resolved macOS temporary paths')
        self.assertTrue(auth.allows('user', {
            'source_id':'materials', 'machine_id':'machine', 'locator':str(source_alias),
            'authority_ref':'user', 'grants':[{'workspace_id':'academic','access':'read'}]},
            'academic'))
        self.assertTrue(auth.allows('user', {
            'source_id':'vault', 'machine_id':'machine', 'locator':str(vault_alias),
            'authority_ref':'user', 'grants':[{'workspace_id':'academic','access':'write_memory'}]},
            'academic', 'write_memory'))
        redirected = source_alias/'extra-link'
        redirected.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaisesRegex(ws.WorkspaceError, 'invalid_local_locator'):
            auth.record_approval('blocked', machine_id='machine', permissions=[{
                'workspace_id':'academic', 'source_id':'materials',
                'locator':str(redirected), 'access':'read'}],
                evidence='Must not follow an additional symlink')

    def test_parent_traversal_locator_is_not_a_spelling_alias(self):
        locator = self.source/'..'/'materials'
        with self.assertRaisesRegex(ws.WorkspaceError, 'invalid_local_locator'):
            ws.LocalAuthority(self.registry).record_approval('blocked', machine_id='machine',
                permissions=[{'source_id':'materials', 'workspace_id':'academic',
                    'access':'read', 'locator':str(locator)}], evidence='Synthetic lexical escape')
        self.assertFalse(self.state.exists())

    def test_external_write_permission_is_rejected(self):
        with self.assertRaisesRegex(ws.WorkspaceError,'external_source_write_denied'):
            ws.LocalAuthority(self.registry).record_approval('user',machine_id='machine',permissions=[{
                'source_id':'materials','workspace_id':'academic','access':'write_memory','locator':str(self.source)}],evidence='test')

    def test_plan_guards_normalize_owned_publication_only(self):
        fixture=load_utf8_json(FIX/'academic/input.json')
        fmt=next(d for d in fixture['documents'] if d['type']=='RegistryFormat')
        self.registry.compare_and_save(fmt,None)
        plan=copy.deepcopy(next(d for d in load_utf8_json(FIX/'academic/golden.json')['documents'] if d['type']=='ProvisionPlan'))
        permissions=[]
        for op in plan['data']['operations']:
            if op['ownership']=='binding':
                binding=json.loads(op['content_utf8'])
                source=binding['data']['source_id']
                path=self.vault if source.endswith('vault') else self.base/source
                path.mkdir(exist_ok=True)
                binding['data']['locator']=str(path)
                op['content_utf8']=ws.render_document(binding).decode()
                op['after_sha256']=ws.content_hash(op['content_utf8'])
                permissions.extend(dict(g,source_id=source,locator=str(path)) for g in binding['data']['grants'])
        data=plan['data'];data['plan_sha256']=ws.content_hash(ws.canonical_bytes({k:v for k,v in data.items() if k not in ('plan_id','plan_sha256')}))
        auth=ws.LocalAuthority(self.registry)
        auth.record_approval('fixture-user-request',machine_id='fixture-machine',permissions=permissions,evidence='synthetic approval')
        auth.approve_plan(plan,evidence='synthetic exact plan preview')
        auth.check_snapshot(plan,[])
        op=data['operations'][0];p=ws.safe_path(self.vault,op['target']['relative_path']);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(op['content_utf8'].encode('utf-8'))
        auth.check_snapshot(plan,[])
        format_path=self.vault/'.beyin-workspaces/format.json';format_path.write_bytes(format_path.read_bytes()+b' ')
        with self.assertRaisesRegex(ws.WorkspaceError,'snapshot_changed'):auth.check_snapshot(plan,[])
        auth.revoke('fixture-user-request')
        with self.assertRaisesRegex(ws.WorkspaceError,'authority_required'):auth.check_authority(plan,'recover')


if __name__=='__main__':unittest.main()
