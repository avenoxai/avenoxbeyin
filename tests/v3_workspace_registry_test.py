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

FIX=ROOT/'tests/fixtures/v3/workspaces/v0.1.0'


class WorkspaceRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.vault=self.base/'vault';self.vault.mkdir()
        self.state=self.base/'state';self.registry=ws.Registry(self.vault,self.state)
        self.source=self.base/'materials';self.source.mkdir()

    def test_embedded_schema_exactly_matches_g0(self):
        self.assertEqual(ws.SCHEMA,json.loads((ROOT/'docs/v3/workspaces/v0.1.0/schema.json').read_text()))

    def test_all_frozen_documents_and_bad_cases(self):
        count=0
        for path in FIX.rglob('*.json'):
            for doc in json.loads(path.read_text()).get('documents',[]):
                ws.validate_document(doc);count+=1
                if doc['type']=='ProvisionPlan':
                    for op in doc['data']['operations']:
                        ws.validate_document(ws.parse_document(op['content_utf8']));count+=1
        self.assertEqual(count,76)
        for case in json.loads((FIX/'schema-negative.json').read_text())['cases']:
            with self.subTest(case=case['id']),self.assertRaises(ws.WorkspaceError):
                ws.validate_document(case['document'])

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
        profile=copy.deepcopy(json.loads((FIX/'academic/golden.json').read_text())['documents'][0])
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
        profile=json.loads((FIX/'academic/golden.json').read_text())['documents'][0]
        profile['data']['workflows'][0]['steps'][0]['depends_on']=['verify']
        with self.assertRaisesRegex(ws.WorkspaceError,'workflow_cycle'):ws.validate_document(profile)
        profile=json.loads((FIX/'academic/golden.json').read_text())['documents'][0]
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

    def test_external_write_permission_is_rejected(self):
        with self.assertRaisesRegex(ws.WorkspaceError,'external_source_write_denied'):
            ws.LocalAuthority(self.registry).record_approval('user',machine_id='machine',permissions=[{
                'source_id':'materials','workspace_id':'academic','access':'write_memory','locator':str(self.source)}],evidence='test')

    def test_plan_guards_normalize_owned_publication_only(self):
        fixture=json.loads((FIX/'academic/input.json').read_text())
        fmt=next(d for d in fixture['documents'] if d['type']=='RegistryFormat')
        self.registry.compare_and_save(fmt,None)
        plan=copy.deepcopy(next(d for d in json.loads((FIX/'academic/golden.json').read_text())['documents'] if d['type']=='ProvisionPlan'))
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
        op=data['operations'][0];p=ws.safe_path(self.vault,op['target']['relative_path']);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(op['content_utf8'])
        auth.check_snapshot(plan,[])
        format_path=self.vault/'.beyin-workspaces/format.json';format_path.write_text(format_path.read_text()+' ')
        with self.assertRaisesRegex(ws.WorkspaceError,'snapshot_changed'):auth.check_snapshot(plan,[])
        auth.revoke('fixture-user-request')
        with self.assertRaisesRegex(ws.WorkspaceError,'authority_required'):auth.check_authority(plan,'recover')


if __name__=='__main__':unittest.main()
