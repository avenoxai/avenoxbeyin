import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import v3_workspace_memory_test as memory_tests
from beyin_v3_workspaces import envelope,content_hash,canonical_bytes,WorkspaceError
from beyin_v3_workspace_workflow import WorkflowRunner


class WorkspaceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.memory=memory_tests.WorkspaceMemoryTest();self.memory.setUp();self.addCleanup(self.memory.tmp.cleanup)
        m=self.memory;record=next(r for r in m.context['records'] if r['id']=='academic-result')
        self.event=envelope('WorkflowEvent',{'event_id':'academic-feedback-1','workspace_id':'academic','event_kind':'record_changed',
            'record':record,'evidence_refs':[record['source_ref']]})
        self.runner=WorkflowRunner(m.runtime)
        workflow=m.spec['desired_profile']['workflows'][0]
        self.runid=content_hash(canonical_bytes(['academic',workflow['id'],workflow['revision'],'academic-feedback-1']))
        m.intent['data']['idempotency_key']=self.runid+'/persist'

    def test_real_shared_writer_flow_and_replay(self):
        m=self.memory
        waiting=self.runner.advance(self.event,'result-to-learning',m.request)['data']
        self.assertEqual(waiting['status'],'awaiting_agent')
        complete=self.runner.advance(self.event,'result-to-learning',m.request,proposals={'propose':m.intent})['data']
        self.assertEqual(complete['status'],'completed')
        self.assertTrue(all(s['state']=='completed' for s in complete['steps']))
        self.runner.advance(self.event,'result-to-learning',m.request)
        records=m.runtime.context(m.request)['data']['records']
        self.assertEqual(next(r for r in records if r['id']=='academic-learning')['revision'],2)

    def test_proposal_only_completed_run_replay_never_applies_intent(self):
        from beyin_v3_workspace_memory import MemoryIntentService
        original_apply=MemoryIntentService.apply
        for replay_path in ('advance','fresh_runner_resume'):
            with self.subTest(replay_path=replay_path):
                m=memory_tests.WorkspaceMemoryTest();m.setUp();self.addCleanup(m.tmp.cleanup)
                record=next(r for r in m.context['records'] if r['id']=='academic-result')
                event=envelope('WorkflowEvent',{'event_id':'proposal-only-'+replay_path,'workspace_id':'academic',
                    'event_kind':'record_changed','record':record,'evidence_refs':[record['source_ref']]})
                profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=1
                workflow=profile['workflows'][0]
                workflow['steps']=[{'id':'propose','depends_on':[],'action':'propose_memory_write',
                    'inputs':{'result':{'ref':'event.record.id'}}}]
                profile_path=m.runtime.registry.vault/'.beyin-workspaces/workspaces/academic.json'
                m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(profile_path.read_bytes()))
                context=m.runtime.context(m.request)['data']
                run_id=content_hash(canonical_bytes(['academic',workflow['id'],workflow['revision'],event['data']['event_id']]))
                intent=copy.deepcopy(m.intent)
                intent['data'].update(context_token=context['context_token'],profile_revision=profile['revision'],
                    binding_snapshot_sha256=context['binding_snapshot_sha256'],idempotency_key=run_id+'/propose')
                runner=WorkflowRunner(m.runtime)
                def vault_bytes():
                    return {p:p.read_bytes() for p in m.runtime.registry.vault.rglob('*') if p.is_file()}
                vault_files=vault_bytes()

                with patch.object(MemoryIntentService,'apply', autospec=True, side_effect=original_apply) as apply:
                    first=runner.advance(event,workflow['id'],m.request,proposals={'propose':intent})['data']
                    self.assertEqual(first['status'],'completed')
                    self.assertEqual(vault_bytes(),vault_files)

                    if replay_path=='advance':
                        replay=runner.advance(event,workflow['id'],m.request)['data']
                    else:
                        replay=WorkflowRunner(m.runtime).resume(first['run_id'],m.request)['data']
                    self.assertEqual(replay['status'],'completed')
                    self.assertEqual(vault_bytes(),vault_files)
                    apply.assert_not_called()

    def test_completed_memory_replay_requires_own_step_and_completed_journal(self):
        from beyin_v3_workspace_memory import MemoryIntentService
        original_apply=MemoryIntentService.apply
        for failure in ('step-key','missing-journal','incomplete-journal'):
            with self.subTest(failure=failure):
                m=memory_tests.WorkspaceMemoryTest();m.setUp();self.addCleanup(m.tmp.cleanup)
                record=next(r for r in m.context['records'] if r['id']=='academic-result')
                event=envelope('WorkflowEvent',{'event_id':'completed-'+failure,'workspace_id':'academic',
                    'event_kind':'record_changed','record':record,'evidence_refs':[record['source_ref']]})
                workflow=m.spec['desired_profile']['workflows'][0]
                run_id=content_hash(canonical_bytes(['academic',workflow['id'],workflow['revision'],event['data']['event_id']]))
                intent=copy.deepcopy(m.intent);intent['data']['idempotency_key']=run_id+'/persist'
                runner=WorkflowRunner(m.runtime)
                self.assertEqual(runner.advance(event,workflow['id'],m.request,proposals={'propose':intent})['data']['status'],'completed')
                run_path=m.runtime.registry.state/'workspace-workflow'/(run_id+'.json')
                journal_path=m.runtime.registry.state/'workspace-write-intents'/(content_hash(intent['data']['idempotency_key'])+'.json')
                if failure=='step-key':
                    saved=json.loads(run_path.read_bytes())
                    saved['outputs']['persist']['intent_ref']['data']['idempotency_key']=run_id+'/propose'
                    run_path.write_text(json.dumps(saved),encoding='utf-8')
                elif failure=='missing-journal':journal_path.unlink()
                else:
                    journal=json.loads(journal_path.read_bytes());journal['parts'][0]['done']=False
                    journal_path.write_text(json.dumps(journal),encoding='utf-8')
                before={p:p.read_bytes() for p in m.runtime.registry.vault.rglob('*') if p.is_file()}
                error='workflow_idempotency_collision' if failure=='step-key' else 'invalid_write_journal'
                with patch.object(MemoryIntentService,'apply',autospec=True,side_effect=original_apply) as apply:
                    with self.assertRaisesRegex(WorkspaceError,error):WorkflowRunner(m.runtime).resume(run_id,m.request)
                    apply.assert_not_called()
                self.assertEqual({p:p.read_bytes() for p in m.runtime.registry.vault.rglob('*') if p.is_file()},before)

    def test_view_refresh_failure_is_checkpointed_without_losing_canonical_success(self):
        import sys
        import types
        module=types.ModuleType('beyin_v3_workspace_views')
        def failed(*args,**kwargs):raise OSError('PRIVATE_VIEW_PATH')
        module.refresh_after_apply=failed
        with patch.dict(sys.modules,{'beyin_v3_workspace_views':module}):
            complete=self.runner.advance(self.event,'result-to-learning',self.memory.request,proposals={'propose':self.memory.intent})['data']
        self.assertEqual(complete['status'],'completed');self.assertIn('managed_view_stale:',complete['next_action'])
        saved=json.loads((self.memory.runtime.registry.state/'workspace-workflow'/(complete['run_id']+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(saved['run']['next_action'],complete['next_action'])
        operations=[output['operation_ref']['data'] for output in saved['outputs'].values() if 'operation_ref' in output]
        self.assertTrue(operations);self.assertTrue(all(operation['operation_status'] in ('applied','replayed') for operation in operations))
        self.assertNotIn('PRIVATE',json.dumps(saved['run']))

    def test_crash_after_effect_resumes_once(self):
        m=self.memory
        def fail(phase,step=None):
            if phase=='effect_returned' and step=='persist':raise RuntimeError('crash')
        with patch('beyin_v3_workspace_workflow.transaction_hook',fail),self.assertRaisesRegex(RuntimeError,'crash'):
            self.runner.advance(self.event,'result-to-learning',m.request,proposals={'propose':m.intent})
        done=self.runner.advance(self.event,'result-to-learning',m.request)['data']
        self.assertEqual(done['status'],'completed')

    def _self_task_workflow(self,action='update_task'):
        m=memory_tests.WorkspaceMemoryTest();m.setUp();self.addCleanup(m.tmp.cleanup)
        profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=1
        wf=profile['workflows'][0]
        wf.update(trigger={'event_kind':'task_changed','entity_type':'assignment'},conditions=[],
            steps=[{'id':'effect','depends_on':[],'action':'update_task','inputs':{}}])
        if action=='apply_memory_write':
            wf['steps']=[{'id':'propose','depends_on':[],'action':'propose_memory_write',
                'inputs':{'result':{'ref':'event.record.id'}}},
                {'id':'effect','depends_on':['propose'],'action':'apply_memory_write',
                    'inputs':{'intent':{'ref':'steps.propose.intent_ref'}}}]
        path=m.runtime.registry.vault/'.beyin-workspaces/workspaces/academic.json'
        m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(path.read_bytes()))
        m.request['data']['signals']=[{'kind':'explicit_user','workspace_id':'academic',
            'subject_refs':[{'workspace_id':'academic','record_id':'academic-task'}]}]
        m.context=m.runtime.context(m.request)['data']
        record=next(r for r in m.context['records'] if r['id']=='academic-task')
        event=envelope('WorkflowEvent',{'event_id':'self-task-change','workspace_id':'academic',
            'event_kind':'task_changed','record':record,'evidence_refs':[copy.deepcopy(record['source_ref'])]})
        runid=content_hash(canonical_bytes(['academic',wf['id'],wf['revision'],event['data']['event_id']]))
        intent=m._self_evidence_task_intent()
        intent['data'].update(context_token=m.context['context_token'],profile_revision=profile['revision'],
            binding_snapshot_sha256=m.context['binding_snapshot_sha256'],idempotency_key=runid+'/effect')
        return m,WorkflowRunner(m.runtime),event,wf['id'],intent,runid

    def test_self_trigger_task_recovers_owned_effect_at_each_checkpoint(self):
        for phase in ('prepared','before_writer','writer_returned','checkpoint','effect_returned'):
            with self.subTest(phase=phase):
                m,runner,event,wid,intent,runid=self._self_task_workflow()
                def crash(at,step=None):
                    if at==phase:raise RuntimeError('self-trigger crash')
                seam='beyin_v3_workspace_workflow.transaction_hook' if phase=='effect_returned' else 'beyin_v3_workspace_memory.transaction_hook'
                with patch(seam,crash),self.assertRaisesRegex(RuntimeError,'self-trigger crash'):
                    runner.advance(event,wid,m.request,proposals={'effect':intent})
                complete=runner.resume(runid,m.request)['data']
                self.assertEqual(complete['status'],'completed')
                target=intent['data']['changes'][0]['target'];path=m.runtime.registry.vault/target['relative_path']
                from beyin_v3_sync import parse
                self.assertEqual(parse(path.read_bytes().decode('utf-8'))[0]['revision'],target['expected_revision']+1)
                after=path.read_bytes()
                self.assertEqual(runner.resume(runid,m.request)['data']['status'],'completed')
                self.assertEqual(path.read_bytes(),after)

    def test_apply_memory_self_trigger_checkpoints_owned_intent_before_writer(self):
        m,runner,event,wid,intent,runid=self._self_task_workflow(action='apply_memory_write')
        def crash(phase,index=None):
            if phase=='writer_returned':raise RuntimeError('effect before workflow checkpoint')
        with patch('beyin_v3_workspace_memory.transaction_hook',crash),self.assertRaisesRegex(RuntimeError,'effect before workflow checkpoint'):
            runner.advance(event,wid,m.request,proposals={'propose':intent})
        saved=json.loads((m.runtime.registry.state/'workspace-workflow'/(runid+'.json')).read_bytes())
        self.assertEqual(saved['outputs']['effect']['intent_ref'],intent)
        self.assertNotIn('operation_ref',saved['outputs']['effect'])
        self.assertEqual(runner.resume(runid,m.request)['data']['status'],'completed')
        path=m.runtime.registry.vault/intent['data']['changes'][0]['target']['relative_path'];after=path.read_bytes()
        self.assertEqual(runner.resume(runid,m.request)['data']['status'],'completed')
        self.assertEqual(path.read_bytes(),after)

    def test_self_trigger_recovery_rejects_external_changes_and_forged_ownership(self):
        failures=('manual','synced-manual','revoked','request','event','run-id','step-key','missing-journal','definition','unrelated-evidence')
        for failure in failures:
            with self.subTest(failure=failure):
                m,runner,event,wid,intent,runid=self._self_task_workflow()
                if failure=='unrelated-evidence':
                    # Select the unrelated source explicitly, preserving the run's request.
                    source=copy.deepcopy(next(r for r in m.service.engine.store._records() if r['id']=='academic-result'))
                    ref=copy.deepcopy(event['data']['record']['source_ref'])
                    ref.update(relative_path=source['source'],content_sha256=source['source_sha256'])
                    event['data']['evidence_refs'].append(ref)
                    intent['data']['changes'][0]['evidence_refs'].append(copy.deepcopy(ref))
                runner.advance(event,wid,m.request,proposals={'effect':intent})
                target=intent['data']['changes'][0]['target'];path=m.runtime.registry.vault/target['relative_path']
                if failure in ('manual','synced-manual'):
                    path.write_bytes(path.read_bytes()+b'External user edit\n')
                    if failure=='synced-manual':m.service.engine.sync()
                elif failure=='revoked':m.runtime.authority.revoke('academic-user')
                elif failure=='request':m.request['data']['query']+=' changed'
                elif failure=='event':event['data']['record']['text']+=' forged'
                elif failure in ('run-id','step-key'):
                    saved_path=m.runtime.registry.state/'workspace-workflow'/(runid+'.json')
                    saved=json.loads(saved_path.read_bytes())
                    if failure=='run-id':saved['run']['run_id']='0'*64
                    else:saved['outputs']['effect']['intent_ref']['data']['idempotency_key']='unowned/effect'
                    saved_path.write_text(json.dumps(saved),encoding='utf-8')
                elif failure=='missing-journal':
                    journal=m.runtime.registry.state/'workspace-write-intents'/(content_hash(intent['data']['idempotency_key'])+'.json')
                    journal.unlink()
                elif failure=='definition':
                    profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=2
                    profile['workflows'][0].update(trigger={'event_kind':'task_changed','entity_type':'assignment'},conditions=[],
                        name='Changed definition',steps=[{'id':'effect','depends_on':[],'action':'update_task','inputs':{}}])
                    profile_path=m.runtime.registry.vault/'.beyin-workspaces/workspaces/academic.json'
                    m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(profile_path.read_bytes()))
                else:
                    unrelated=m.runtime.registry.vault/'notes/academic-result.md'
                    unrelated.write_bytes(unrelated.read_bytes()+b'Unrelated source edit\n')
                before=path.read_bytes()
                with self.assertRaises(WorkspaceError):runner.advance(event,wid,m.request)
                self.assertEqual(path.read_bytes(),before)

    def test_forged_event_no_memory_and_self_trigger_are_denied(self):
        m=self.memory;forged=copy.deepcopy(self.event);forged['data']['record']['facts']['score']=100
        with self.assertRaisesRegex(WorkspaceError,'forged'):self.runner.advance(forged,'result-to-learning',m.request)
        request=copy.deepcopy(m.request);request['data']['no_memory']=True
        with self.assertRaisesRegex(WorkspaceError,'no_memory'):self.runner.advance(self.event,'result-to-learning',request)
        with self.assertRaisesRegex(WorkspaceError,'self_retrigger'):self.runner.advance(self.event,'result-to-learning',m.request,causal_run_id=self.runid)

    def test_completed_run_cannot_hide_later_manual_edit(self):
        m=self.memory;self.runner.advance(self.event,'result-to-learning',m.request,proposals={'propose':m.intent})
        path=m.runtime.registry.vault/'notes/academic-learning.md';path.write_text(path.read_text(encoding='utf-8')+'Yeni kullanıcı düzenlemesi.\n', encoding='utf-8')
        before=path.read_bytes()
        with self.assertRaisesRegex(WorkspaceError,'revision_conflict'):self.runner.advance(self.event,'result-to-learning',m.request)
        self.assertEqual(path.read_bytes(),before)

    def test_natural_sync_change_dispatch_and_no_memory(self):
        m=self.memory
        path=m.runtime.registry.vault/'notes/academic-result.md'
        path.write_text(path.read_text(encoding='utf-8')+'Yeni kaynaklı geribildirim.\n', encoding='utf-8')
        result=m.runtime.sync(m.request)
        self.assertEqual(len(result['workflow_runs']),1)
        run=result['workflow_runs'][0]['data'];self.assertEqual(run['status'],'awaiting_agent')
        saved=json.loads((m.runtime.registry.state/'workspace-workflow'/(run['run_id']+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(saved['event']['data']['record']['id'],'academic-result')
        self.assertEqual(m.runtime.sync(m.request)['workflow_runs'],[])
        no=copy.deepcopy(m.request);no['data']['no_memory']=True
        path.write_text(path.read_text(encoding='utf-8')+'No-memory change.\n', encoding='utf-8')
        before=set((m.runtime.registry.state/'workspace-workflow').glob('*.json'))
        self.assertEqual(m.runtime.sync(no)['workflow_runs'],[])
        self.assertEqual(set((m.runtime.registry.state/'workspace-workflow').glob('*.json')),before)

    def test_typed_task_link_catalogue_uses_shared_writer_and_preserves_body(self):
        for action in ('update_task','link_records'):
            with self.subTest(action=action):
                m=memory_tests.WorkspaceMemoryTest();m.setUp();self.addCleanup(m.tmp.cleanup)
                profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=1
                wf=profile['workflows'][0];wf['steps']=[{'id':'effect','depends_on':[],'action':action,'inputs':{}}]
                path=m.runtime.registry.vault/'.beyin-workspaces/workspaces/academic.json'
                m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(path.read_bytes()))
                context=m.runtime.context(m.request)['data'];record=next(r for r in context['records'] if r['id']=='academic-result')
                event=envelope('WorkflowEvent',{'event_id':'catalogue-'+action.replace('_','-'),'workspace_id':'academic','event_kind':'record_changed','record':record,'evidence_refs':[record['source_ref']]})
                intent=copy.deepcopy(m.intent);intent['data'].update(context_token=context['context_token'],profile_revision=profile['revision'])
                change=next(c for c in intent['data']['changes'] if c['action']=='update_task')
                if action=='link_records':change.update(action='link',body='',metadata_changes={})
                intent['data']['changes']=[change]
                runid=content_hash(canonical_bytes(['academic',wf['id'],wf['revision'],event['data']['event_id']]))
                intent['data']['idempotency_key']=runid+'/effect'
                taskpath=m.runtime.registry.vault/'tasks/academic-task.md'
                from beyin_v3_sync import parse
                oldbody=parse(taskpath.read_text(encoding='utf-8'))[1]
                runner=WorkflowRunner(m.runtime)
                self.assertEqual(runner.advance(event,wf['id'],m.request,proposals={'effect':intent})['data']['status'],'completed')
                self.assertEqual(parse(taskpath.read_text(encoding='utf-8'))[1],oldbody)
                self.assertEqual(runner.advance(event,wf['id'],m.request)['data']['status'],'completed')

    def test_request_user_input_resumes_without_grant_or_side_effect(self):
        m=self.memory;profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=1
        profile['workflows'][0]['steps']=[{'id':'ask','depends_on':[],'action':'request_user_input','inputs':{'question':{'literal':'Which evidence should be compared?'}}}]
        path=m.runtime.registry.vault/'.beyin-workspaces/workspaces/academic.json'
        m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(path.read_bytes()))
        before={p:p.read_bytes() for p in m.runtime.registry.vault.rglob('*.md')}
        waiting=self.runner.advance(self.event,'result-to-learning',m.request)['data']
        self.assertEqual(waiting['status'],'awaiting_user');self.assertIn('evidence',waiting['next_action'])
        self.assertEqual(self.runner.resume(waiting['run_id'],m.request,proposals={'ask':{'answer':'The latest approved result'}})['data']['status'],'completed')
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_sync_dispatch_retry_after_index_commit_keeps_original_baseline(self):
        m=self.memory;path=m.runtime.registry.vault/'notes/academic-result.md'
        path.write_text(path.read_text(encoding='utf-8')+'Durable event retry.\n', encoding='utf-8')
        with patch.object(WorkflowRunner,'dispatch',side_effect=RuntimeError('dispatch crash')),self.assertRaisesRegex(RuntimeError,'dispatch crash'):
            m.runtime.sync(m.request)
        self.assertTrue(list((m.runtime.registry.state/'workspace-sync-events').glob('*.json')))
        result=m.runtime.sync(m.request)
        self.assertEqual(len(result['workflow_runs']),1)
        self.assertEqual(result['workflow_runs'][0]['data']['status'],'awaiting_agent')
        self.assertFalse(list((m.runtime.registry.state/'workspace-sync-events').glob('*.json')))

    def _provision_workflow(self):
        m=self.memory
        from beyin_v3_workspaces import envelope
        profile=copy.deepcopy(m.spec['desired_profile']);profile['revision']+=1
        wf=profile['workflows'][0]
        wf['steps']=[{'id':'design','depends_on':[],'action':'propose_provision','inputs':{}},
            {'id':'install','depends_on':['design'],'action':'apply_provision','inputs':{'plan':{'ref':'steps.design.plan_ref'}}},
            {'id':'verify','depends_on':['install'],'action':'verify_readback','inputs':{'operation':{'ref':'steps.install.operation_ref'}}}]
        name='.beyin-workspaces/workspaces/academic.json';old=m.runtime.registry.vault/name
        m.runtime.registry.compare_and_save(envelope('WorkspaceProfile',profile),content_hash(old.read_bytes()))
        desired=copy.deepcopy(profile);desired['revision']+=1;desired['purpose']+=' Approved synthetic rule update.'
        spec=copy.deepcopy(m.spec);spec['desired_profile']=desired;spec['required_capabilities']=[]
        spec['binding_requests']=[];spec['adopt_record_refs']=[]
        return envelope('WorkspaceSpec',spec)

    def test_provision_waits_for_exact_authority_then_reuses_lock_and_replays(self):
        m=self.memory;spec=self._provision_workflow()
        waiting=self.runner.advance(self.event,'result-to-learning',m.request,proposals={'design':spec})['data']
        self.assertEqual(waiting['status'],'awaiting_user')
        path=m.runtime.registry.state/'workspace-workflow'/(waiting['run_id']+'.json')
        saved=json.loads(path.read_text(encoding='utf-8'));plan=saved['outputs']['design']['plan_ref']
        m.runtime.authority.approve_plan(plan,evidence='Explicit exact workflow plan preview')
        complete=self.runner.resume(waiting['run_id'],m.request)['data']
        self.assertEqual(complete['status'],'completed')
        self.assertEqual(self.runner.resume(waiting['run_id'],m.request)['data']['status'],'completed')
        profile=next(p for p in m.runtime.registry.snapshot()['profiles'] if p['id']=='academic')
        self.assertEqual(profile['purpose'],spec['data']['desired_profile']['purpose'])

    def test_provision_crash_after_replace_recovers_pending_publication(self):
        m=self.memory;spec=self._provision_workflow()
        waiting=self.runner.advance(self.event,'result-to-learning',m.request,proposals={'design':spec})['data']
        saved=json.loads((m.runtime.registry.state/'workspace-workflow'/(waiting['run_id']+'.json')).read_text(encoding='utf-8'))
        m.runtime.authority.approve_plan(saved['outputs']['design']['plan_ref'],evidence='Explicit synthetic crash plan')
        def crash(phase,index=None):
            if phase=='after_replace':raise RuntimeError('D2 crash')
        with patch('beyin_v3_provision.transaction_hook',crash),self.assertRaisesRegex(RuntimeError,'D2 crash'):
            self.runner.resume(waiting['run_id'],m.request)
        self.assertTrue(m.runtime.registry.pending())
        self.assertEqual(self.runner.resume(waiting['run_id'],m.request)['data']['status'],'completed')
        self.assertFalse(m.runtime.registry.pending())


if __name__=='__main__':unittest.main()
