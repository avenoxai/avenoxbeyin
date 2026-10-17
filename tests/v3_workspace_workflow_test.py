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

    def test_crash_after_effect_resumes_once(self):
        m=self.memory
        def fail(phase,step=None):
            if phase=='effect_returned' and step=='persist':raise RuntimeError('crash')
        with patch('beyin_v3_workspace_workflow.transaction_hook',fail),self.assertRaisesRegex(RuntimeError,'crash'):
            self.runner.advance(self.event,'result-to-learning',m.request,proposals={'propose':m.intent})
        done=self.runner.advance(self.event,'result-to-learning',m.request)['data']
        self.assertEqual(done['status'],'completed')

    def test_forged_event_no_memory_and_self_trigger_are_denied(self):
        m=self.memory;forged=copy.deepcopy(self.event);forged['data']['record']['facts']['score']=100
        with self.assertRaisesRegex(WorkspaceError,'forged'):self.runner.advance(forged,'result-to-learning',m.request)
        request=copy.deepcopy(m.request);request['data']['no_memory']=True
        with self.assertRaisesRegex(WorkspaceError,'no_memory'):self.runner.advance(self.event,'result-to-learning',request)
        with self.assertRaisesRegex(WorkspaceError,'self_retrigger'):self.runner.advance(self.event,'result-to-learning',m.request,causal_run_id=self.runid)

    def test_completed_run_cannot_hide_later_manual_edit(self):
        m=self.memory;self.runner.advance(self.event,'result-to-learning',m.request,proposals={'propose':m.intent})
        path=m.runtime.registry.vault/'notes/academic-learning.md';path.write_text(path.read_text()+'Yeni kullanıcı düzenlemesi.\n')
        before=path.read_bytes()
        with self.assertRaisesRegex(WorkspaceError,'revision_conflict'):self.runner.advance(self.event,'result-to-learning',m.request)
        self.assertEqual(path.read_bytes(),before)

    def test_natural_sync_change_dispatch_and_no_memory(self):
        m=self.memory
        path=m.runtime.registry.vault/'notes/academic-result.md'
        path.write_text(path.read_text()+'Yeni kaynaklı geribildirim.\n')
        result=m.runtime.sync(m.request)
        self.assertEqual(len(result['workflow_runs']),1)
        run=result['workflow_runs'][0]['data'];self.assertEqual(run['status'],'awaiting_agent')
        saved=json.loads((m.runtime.registry.state/'workspace-workflow'/(run['run_id']+'.json')).read_text())
        self.assertEqual(saved['event']['data']['record']['id'],'academic-result')
        self.assertEqual(m.runtime.sync(m.request)['workflow_runs'],[])
        no=copy.deepcopy(m.request);no['data']['no_memory']=True
        path.write_text(path.read_text()+'No-memory change.\n')
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
                oldbody=parse(taskpath.read_text())[1]
                runner=WorkflowRunner(m.runtime)
                self.assertEqual(runner.advance(event,wf['id'],m.request,proposals={'effect':intent})['data']['status'],'completed')
                self.assertEqual(parse(taskpath.read_text())[1],oldbody)
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
        path.write_text(path.read_text()+'Durable event retry.\n')
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
        saved=json.loads(path.read_text());plan=saved['outputs']['design']['plan_ref']
        m.runtime.authority.approve_plan(plan,evidence='Explicit exact workflow plan preview')
        complete=self.runner.resume(waiting['run_id'],m.request)['data']
        self.assertEqual(complete['status'],'completed')
        self.assertEqual(self.runner.resume(waiting['run_id'],m.request)['data']['status'],'completed')
        profile=next(p for p in m.runtime.registry.snapshot()['profiles'] if p['id']=='academic')
        self.assertEqual(profile['purpose'],spec['data']['desired_profile']['purpose'])

    def test_provision_crash_after_replace_recovers_pending_publication(self):
        m=self.memory;spec=self._provision_workflow()
        waiting=self.runner.advance(self.event,'result-to-learning',m.request,proposals={'design':spec})['data']
        saved=json.loads((m.runtime.registry.state/'workspace-workflow'/(waiting['run_id']+'.json')).read_text())
        m.runtime.authority.approve_plan(saved['outputs']['design']['plan_ref'],evidence='Explicit synthetic crash plan')
        def crash(phase,index=None):
            if phase=='after_replace':raise RuntimeError('D2 crash')
        with patch('beyin_v3_provision.transaction_hook',crash),self.assertRaisesRegex(RuntimeError,'D2 crash'):
            self.runner.resume(waiting['run_id'],m.request)
        self.assertTrue(m.runtime.registry.pending())
        self.assertEqual(self.runner.resume(waiting['run_id'],m.request)['data']['status'],'completed')
        self.assertFalse(m.runtime.registry.pending())


if __name__=='__main__':unittest.main()
