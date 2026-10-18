import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_workspace_helpers import FIX,ROOT,install_fixture
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_workspaces import WorkspaceError,content_hash,canonical_bytes
from beyin_v3_workspace_context import WorkspaceRuntime


class WorkspaceMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.runtime,self.spec,self.request=install_fixture(self.base,'academic')
        self.service=MemoryIntentService(self.runtime);self.context=self.runtime.context(self.request)['data']
        self.intent=copy.deepcopy(next(d for d in json.loads((FIX/'academic/golden.json').read_text())['documents'] if d['type']=='MemoryWriteIntent'))
        data=self.intent['data'];data.update(context_token=self.context['context_token'],binding_snapshot_sha256=self.context['binding_snapshot_sha256'])
        records={r['id']:r for r in self.context['records']}
        for change in data['changes']:
            target=change['target'];record=records[target['record_id']]
            target.update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
            change['evidence_refs']=[records['academic-result']['source_ref']]
            for relation in change['relation_changes']:relation['evidence_refs']=[records['academic-result']['source_ref']]

    def test_existing_note_task_relation_and_fresh_process_recall(self):
        vault=self.runtime.registry.vault;before=set(vault.rglob('*.md'))
        result=self.service.apply(self.intent)['data']
        self.assertTrue(all(p['state']=='verified' for p in result['parts']))
        self.assertEqual(set(vault.rglob('*.md')),before)
        fresh=WorkspaceRuntime(vault,self.runtime.registry.state).context(self.request)['data']
        learning=next(r for r in fresh['records'] if r['id']=='academic-learning')
        self.assertEqual(learning['revision'],2);self.assertIn('Yeni sonuç',learning['text'])
        task=next(r for r in fresh['records'] if r['id']=='academic-task');self.assertEqual(task['revision'],2)
        self.assertTrue(any(r['type']=='result_of' for r in fresh['relations']))
        requestfile=self.base/'request.json';requestfile.write_text(json.dumps(self.request))
        code='import json,sys;sys.path.insert(0,sys.argv[1]);from beyin_v3_workspace_context import WorkspaceRuntime;print(json.dumps(WorkspaceRuntime(sys.argv[2],sys.argv[3]).context(json.load(open(sys.argv[4])))))'
        process=subprocess.run([sys.executable,'-c',code,str(ROOT/'template/.claude/scripts'),str(vault),str(self.runtime.registry.state),str(requestfile)],capture_output=True,text=True,check=True)
        observed=json.loads(process.stdout)['data'];self.assertTrue(any(r['id']=='academic-learning' and 'Yeni sonuç' in r['text'] for r in observed['records']))

    def test_replay_and_idempotency_collision(self):
        self.service.apply(self.intent)
        self.assertEqual(self.service.apply(self.intent)['data']['operation_status'],'replayed')
        changed=copy.deepcopy(self.intent);changed['data']['changes'][0]['body']+='changed'
        with self.assertRaisesRegex(WorkspaceError,'idempotency_collision'):self.service.apply(changed)

    def test_manual_edit_before_apply_or_after_replay_is_preserved(self):
        path=self.runtime.registry.vault/'notes/academic-learning.md';path.write_text(path.read_text()+'Kullanıcının düzenlemesi.\n')
        preserved=path.read_bytes()
        with self.assertRaisesRegex(WorkspaceError,'revision_conflict'):self.service.apply(self.intent)
        self.assertEqual(path.read_bytes(),preserved)

    def test_no_memory_and_revoked_authority_write_nothing(self):
        request=copy.deepcopy(self.request);request['data']['no_memory']=True
        context=self.runtime.context(request)['data'];intent=copy.deepcopy(self.intent);intent['data']['context_token']=context['context_token']
        before={p:p.read_bytes() for p in self.runtime.registry.vault.rglob('*.md')}
        with self.assertRaisesRegex(WorkspaceError,'no_memory'):self.service.apply(intent)
        self.runtime.authority.revoke('academic-user')
        with self.assertRaisesRegex(WorkspaceError,'scope_denied'):self.service.apply(self.intent)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_mixed_intent_invalid_new_note_rejects_before_any_source_write(self):
        from beyin_v3_workspaces import envelope
        vault=self.runtime.registry.vault;state=self.runtime.registry.state
        profile=copy.deepcopy(self.spec['desired_profile']);profile['revision']+=1
        profile['note_roots'].append('workspaces')
        path=vault/'.beyin-workspaces/workspaces/academic.json'
        self.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(path.read_bytes()))
        context=self.runtime.context(self.request)['data']
        intent=copy.deepcopy(self.intent)
        intent['data'].update(context_token=context['context_token'],profile_revision=profile['revision'],
                              binding_snapshot_sha256=context['binding_snapshot_sha256'])
        invalid=copy.deepcopy(intent['data']['changes'][0])
        invalid.update(id='invalid-note',action='create_note',body='Distinct new workspace note',metadata_changes={},relation_changes=[])
        invalid['target'].update(record_id='new-workspace-note',relative_path='workspaces/academic.md',expected_revision=None,expected_source_sha256=None)
        intent['data']['changes'].append(invalid)
        before={p:p.read_bytes() for p in vault.rglob('*.md')}
        with patch.object(self.service.engine,'update_note') as writer,self.assertRaisesRegex(WorkspaceError,'invalid_create_note_target') as raised:
            self.service.apply(intent)
        writer.assert_not_called();self.assertEqual(raised.exception.change_id,'invalid-note')
        self.assertEqual(before,{p:p.read_bytes() for p in vault.rglob('*.md')})
        journal=state/'workspace-write-intents'/(content_hash(intent['data']['idempotency_key'])+'.json')
        self.assertFalse(journal.exists())

    def test_existing_task_outside_tasks_keeps_adoption_and_update_path(self):
        from beyin_v3_workspaces import envelope
        vault=self.runtime.registry.vault
        old=vault/'tasks/academic-task.md';new=vault/'projects/legacy-task.md'
        new.parent.mkdir();old.rename(new)
        profile=copy.deepcopy(self.spec['desired_profile']);profile['revision']+=1
        profile['note_roots'].append('projects')
        path=vault/'.beyin-workspaces/workspaces/academic.json'
        self.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(path.read_bytes()))
        self.service.engine.sync()
        adopted=copy.deepcopy(self.spec);adopted['desired_profile']=profile
        self.runtime.adopt(envelope('WorkspaceSpec',adopted))
        context=self.runtime.context(self.request)['data']
        record=next(r for r in context['records'] if r['id']=='academic-task')
        intent=copy.deepcopy(self.intent)
        change=next(c for c in intent['data']['changes'] if c['action']=='update_task')
        change['target'].update(relative_path='projects/legacy-task.md',expected_revision=record['revision'],
                                expected_source_sha256=record['source_ref']['content_sha256'])
        intent['data'].update(changes=[change],context_token=context['context_token'],profile_revision=profile['revision'],
                              binding_snapshot_sha256=context['binding_snapshot_sha256'])
        result=self.service.apply(intent)['data']
        self.assertEqual(result['operation_status'],'applied');self.assertTrue(new.exists());self.assertFalse(old.exists())
        updated=next(r for r in self.runtime.context(self.request)['data']['records'] if r['id']=='academic-task')
        self.assertEqual(updated['revision'],record['revision']+1)

    def test_canonical_refresh_preserves_success_if_view_helper_fails(self):
        import types
        module=types.ModuleType('beyin_v3_workspace_views')
        def failed(runtime,document,result,*,_coordinated=False):
            result['data']['parts']=[]  # A mutating failed helper cannot erase evidence.
            raise ValueError('PRIVATE_VIEW_PATH')
        module.refresh_after_apply=failed
        with patch.dict(sys.modules,{'beyin_v3_workspace_views':module}):
            result=self.service.apply(self.intent)['data']
        self.assertEqual(result['operation_status'],'applied')
        self.assertTrue(result['parts']);self.assertTrue(all(p['state']=='verified' for p in result['parts']))
        self.assertEqual(result['verification'][0]['state'],'passed')
        self.assertIn('managed_view_stale',[issue['code'] for issue in result['issues']])
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_shared_callback_carries_existing_lock_and_can_be_opted_out(self):
        import types
        from unittest.mock import Mock
        module=types.ModuleType('beyin_v3_workspace_views')
        module.refresh_after_apply=Mock(side_effect=lambda runtime,document,result,**kwargs:result)
        with patch.dict(sys.modules,{'beyin_v3_workspace_views':module}):
            with self.runtime.registry.coordinated():
                result=self.service.apply(self.intent,_coordinated=True)
            module.refresh_after_apply.assert_called_once()
            self.assertTrue(module.refresh_after_apply.call_args.kwargs['_coordinated'])
            module.refresh_after_apply.reset_mock()
            self.service.apply(self.intent,_refresh_views=False)
            module.refresh_after_apply.assert_not_called()
        self.assertEqual(result['data']['operation_status'],'applied')

    def test_internal_view_mode_preserves_crlf_and_missing_final_lf_through_readback(self):
        from beyin_v3_sync import parse,render
        path=self.runtime.registry.vault/'notes/academic-learning.md'
        metadata,_=parse(path.read_bytes().decode('utf-8'))
        before_body='Kullanıcı üst satırı\r\n<!-- beyin-workspace:academic:start -->\r\nEski görünüm\r\n<!-- beyin-workspace:academic:end -->\r\nSon kullanıcı satırı'
        path.write_bytes(render(metadata,before_body).encode('utf-8'))
        self.service.engine.sync()
        context=self.runtime.context(self.request)['data']
        record=next(r for r in context['records'] if r['id']=='academic-learning')
        intent=copy.deepcopy(self.intent);change=copy.deepcopy(intent['data']['changes'][0])
        change.update(body=before_body.replace('Eski görünüm','Yeni görünüm'),metadata_changes={},relation_changes=[])
        change['target'].update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
        intent['data'].update(changes=[change],context_token=context['context_token'],binding_snapshot_sha256=context['binding_snapshot_sha256'])
        result=self.service.apply(intent,_refresh_views=False,_preserve_body=True)['data']
        self.assertEqual(result['operation_status'],'applied')
        self.assertEqual(parse(path.read_bytes().decode('utf-8'))[1],change['body'])
        self.assertFalse(path.read_bytes().endswith(b'\n'))
        self.assertEqual(self.service.apply(intent,_refresh_views=False,_preserve_body=True)['data']['operation_status'],'replayed')
        with self.assertRaisesRegex(WorkspaceError,'idempotency_collision'):
            self.service.apply(intent,_refresh_views=False)

    def test_default_update_still_appends_final_lf(self):
        from beyin_v3_sync import parse
        intent=copy.deepcopy(self.intent);change=intent['data']['changes'][0]
        change.update(body='Ordinary note without final LF',metadata_changes={},relation_changes=[])
        intent['data']['changes']=[change]
        self.service.apply(intent,_refresh_views=False)
        path=self.runtime.registry.vault/change['target']['relative_path']
        self.assertEqual(parse(path.read_bytes().decode('utf-8'))[1],change['body']+'\n')

    def test_preserve_body_does_not_bypass_protection_or_frontmatter_validation(self):
        from beyin_v3_sync import parse
        intent=copy.deepcopy(self.intent);change=intent['data']['changes'][0]
        change.update(body='Body requiring protection review',metadata_changes={},relation_changes=[])
        intent['data']['changes']=[change]
        path=self.runtime.registry.vault/change['target']['relative_path'];before=path.read_bytes()
        with patch.object(self.service.engine,'_protect',return_value=('Protected replacement',1)):
            with self.assertRaisesRegex(WorkspaceError,'managed_view_body_requires_review'):
                self.service.apply(intent,_refresh_views=False,_preserve_body=True)
        self.assertEqual(path.read_bytes(),before)
        change['body']='---\n{"kind":"task"}\n---\nInjected header'
        with self.assertRaisesRegex(ValueError,'invalid note update'):
            self.service.apply(intent,_refresh_views=False,_preserve_body=True)
        self.assertEqual(path.read_bytes(),before)

    def test_original_write_dispatches_once_and_derived_view_does_not_redispatch(self):
        import types
        module=types.ModuleType('beyin_v3_workspace_views')
        def refresh(runtime,document,result,*,_coordinated=False):
            derived=copy.deepcopy(document);change=copy.deepcopy(derived['data']['changes'][0])
            change.update(id='derived-view',action='create_note',body='Distinct derived homepage body',metadata_changes={},relation_changes=[])
            change['target'].update(record_id='derived-homepage',relative_path='notes/derived-homepage.md',expected_revision=None,expected_source_sha256=None)
            derived['data'].update(intent_id='derived-view',idempotency_key='derived-view',changes=[change])
            MemoryIntentService(runtime).apply(derived,_coordinated=_coordinated,_refresh_views=False,_preserve_body=True)
            return result
        module.refresh_after_apply=refresh
        with patch.dict(sys.modules,{'beyin_v3_workspace_views':module}),patch('beyin_v3_workspace_workflow.WorkflowRunner.dispatch',return_value=[]) as dispatch:
            result=self.service.apply(self.intent)
        self.assertEqual(result['data']['operation_status'],'applied');dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1],[c['target']['record_id'] for c in self.intent['data']['changes']])
        self.assertTrue((self.runtime.registry.vault/'notes/derived-homepage.md').exists())
        self.assertNotIn('managed_view_stale',[issue['code'] for issue in result['data']['issues']])

    def test_crash_after_writer_before_checkpoint_resumes_without_duplication(self):
        def fail(phase,index=None):
            if phase=='writer_returned' and index==0:raise RuntimeError('synthetic crash')
        with patch('beyin_v3_workspace_memory.transaction_hook',fail),self.assertRaisesRegex(RuntimeError,'synthetic crash'):
            self.service.apply(self.intent)
        self.service.apply(self.intent)
        records=self.runtime.context(self.request)['data']['records']
        self.assertEqual(next(r for r in records if r['id']=='academic-learning')['revision'],2)
        self.assertEqual(next(r for r in records if r['id']=='academic-task')['revision'],2)


if __name__=='__main__':unittest.main()
