"""Finite G0 workflows over the shared memory/provisioning services.

Semantic proposals return to the active agent. No model, scheduler or shell is
started. Durable checkpoints and writer idempotency make retry explicit.
"""
import copy
import json

from beyin_v3_workspaces import WorkspaceError,canonical_bytes,content_hash,envelope,parse_document,safe_path,validate_document
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_sync import atomic


def transaction_hook(phase,step=None):
    """Durable run fault seam."""


class WorkflowRunner:
    def __init__(self,runtime,*,handlers=None):
        self.runtime=runtime;self.registry=runtime.registry;self.handlers=handlers or {}

    def _provisioner(self):
        from beyin_v3_provision import Provisioner
        from beyin_v3_workspaces import validate_for_provision
        return Provisioner(self.registry.vault,self.registry.state,validate_document=validate_for_provision,
            check_authority=self.runtime.authority.check_authority,check_snapshot=self.runtime.authority.check_snapshot)

    def _resume_provision(self,event_document,workflow_id,request_document):
        # A D2 crash can leave a pending publication which ordinary registry reads
        # must reject. Resume only a saved exact event/request/plan from this run.
        if not self.registry.pending():return
        directory=safe_path(self.registry.state,'workspace-workflow')
        if not directory.exists():return
        for path in directory.glob('*.json'):
            saved=parse_document(path.read_bytes());run=saved['run']
            if (run['workflow_id']!=workflow_id or run['workspace_id']!=event_document['data']['workspace_id']
                    or run['event_id']!=event_document['data']['event_id']):continue
            if saved['event_sha256']!=content_hash(canonical_bytes(event_document)) or saved.get('request_sha256')!=content_hash(canonical_bytes(request_document)):
                raise WorkspaceError('workflow_idempotency_collision')
            for output in saved['outputs'].values():
                if isinstance(output,dict) and 'applied_plan_ref' in output:
                    self._provisioner().apply(output['applied_plan_ref'],output['operation_key'])
            return

    def resume(self,run_id,request_document,*,proposals=None):
        if not isinstance(run_id,str) or len(run_id)!=64 or any(c not in '0123456789abcdef' for c in run_id):raise WorkspaceError('invalid_workflow_run')
        saved=parse_document(safe_path(self.registry.state,'workspace-workflow/'+run_id+'.json',True).read_bytes())
        return self.advance(saved['event'],saved['run']['workflow_id'],request_document,proposals=proposals)

    def dispatch(self,request_document,changes):
        """Turn verified canonical record changes into durable finite events.

        The caller supplies before/after IDs, never an event payload from a file.
        Only records in a fresh authorized context can trigger a workflow.
        """
        validate_document(request_document)
        if request_document['data']['no_memory']:return []
        request_document=copy.deepcopy(request_document)
        for signal in request_document['data']['signals']:
            signal['subject_refs'] += [{'workspace_id':signal['workspace_id'],'record_id':id} for id in changes]
        context=self.runtime.context(request_document)['data']
        if context['state'] not in ('ready','partial'):return []
        profile=next(p for p in self.registry.snapshot()['profiles'] if p['id']==context['workspace_id'])
        if profile['memory_policy']['automatic_learning']!='enabled':return []
        results=[]
        for record in context['records']:
            if record['id'] not in changes or record['kind']=='source':continue
            kind='task_changed' if record['kind']=='task' else 'record_changed'
            event=envelope('WorkflowEvent',{'event_id':'change-'+content_hash(canonical_bytes([context['workspace_id'],record['id'],record['revision'],record['source_ref']['content_sha256']]))[:64],
                'workspace_id':context['workspace_id'],'event_kind':kind,'record':record,'evidence_refs':[record['source_ref']]})
            for workflow in profile['workflows']:
                if workflow['trigger']!={'event_kind':kind,'entity_type':record['entity_type']}:continue
                try:results.append(self.advance(event,workflow['id'],request_document))
                except WorkspaceError as exc:
                    if exc.code!='workflow_condition_not_met':raise
        return results

    def advance(self,event_document,workflow_id,request_document,*,proposals=None,causal_run_id=None):
        validate_document(event_document);validate_document(request_document)
        if event_document['type']!='WorkflowEvent' or request_document['type']!='RequestContext':raise WorkspaceError('invalid_schema')
        event=event_document['data'];request=request_document['data']
        if request['no_memory']:raise WorkspaceError('no_memory')
        self._resume_provision(event_document,workflow_id,request_document)
        snapshot=self.registry.snapshot();profile=next((p for p in snapshot['profiles'] if p['id']==event['workspace_id']),None)
        if not profile or profile['lifecycle']!='active':raise WorkspaceError('scope_denied')
        workflow=next((w for w in profile['workflows'] if w['id']==workflow_id),None)
        if not workflow or workflow['trigger']!={'event_kind':event['event_kind'],'entity_type':event['record']['entity_type']}:
            raise WorkspaceError('workflow_trigger_mismatch')
        if profile['memory_policy']['automatic_learning']!='enabled':raise WorkspaceError('automatic_learning_disabled')
        context=self.runtime.context(request_document)['data']
        if context['workspace_id']!=event['workspace_id']:raise WorkspaceError('scope_denied')
        actual=next((r for r in context['records'] if r['id']==event['record']['id']),None)
        if (not actual or actual['revision']!=event['record']['revision'] or actual['source_ref']['content_sha256']!=event['record']['source_ref']['content_sha256']
                or any(actual.get(k)!=event['record'].get(k) for k in ('text','entity_type','kind','facts','status'))):raise WorkspaceError('stale_or_forged_workflow_event')
        self.runtime.resolver.read(actual['source_ref'],event['workspace_id'],request['grant_ref'],audience=request['audience'])
        for evidence in event['evidence_refs']:
            self.runtime.resolver.read(evidence,event['workspace_id'],request['grant_ref'],audience=request['audience'])
        for condition in workflow['conditions']:
            value=event
            for key in condition['field'].split('.')[1:]:value=value.get(key) if isinstance(value,dict) else None
            match=value is not None if condition['operator']=='exists' else value==condition['value']
            if not match:raise WorkspaceError('workflow_condition_not_met')
        run_id=content_hash(canonical_bytes([event['workspace_id'],workflow_id,workflow['revision'],event['event_id']]))
        if causal_run_id==run_id:raise WorkspaceError('workflow_self_retrigger')
        definition_hash=content_hash(canonical_bytes(workflow));event_hash=content_hash(canonical_bytes(event_document))
        path=safe_path(self.registry.state,'workspace-workflow/'+run_id+'.json');proposals=proposals or {}
        with self.registry.coordinated():
            if path.exists():
                saved=parse_document(path.read_bytes());run=saved['run']
                if saved['event_sha256']!=event_hash or run['definition_sha256']!=definition_hash:raise WorkspaceError('workflow_idempotency_collision')
                outputs=saved['outputs']
                if run['status']=='completed':
                    for output in outputs.values():
                        if isinstance(output,dict) and 'intent_ref' in output:
                            output['operation_ref']=MemoryIntentService(self.runtime).apply(output['intent_ref'],_coordinated=True)
                        if isinstance(output,dict) and 'applied_plan_ref' in output:
                            self._provisioner().apply(output['applied_plan_ref'],output['operation_key'],_coordinated=True)
            else:
                run=dict(run_id=run_id,workspace_id=event['workspace_id'],workflow_id=workflow_id,workflow_revision=workflow['revision'],
                    definition_sha256=definition_hash,event_id=event['event_id'],context_token=context['context_token'],status='running',
                    steps=[{'step_id':s['id'],'idempotency_key':run_id+'/'+s['id'],'state':'pending','result_ref':None} for s in workflow['steps']],next_action=None)
                outputs={};saved={'event_sha256':event_hash,'request_sha256':content_hash(canonical_bytes(request_document)),'event':copy.deepcopy(event_document),'request':copy.deepcopy(request_document),'run':run,'outputs':outputs}
            def checkpoint():
                validate_document(envelope('WorkflowRun',run))
                atomic(path,json.dumps(saved,ensure_ascii=False,allow_nan=False))
            checkpoint();transaction_hook('run_saved')
            steps={s['step_id']:s for s in run['steps']}
            definitions={s['id']:s for s in workflow['steps']}
            # Definitions may list a valid DAG in any order; execute ready nodes.
            while any(s['state']!='completed' for s in run['steps']):
                ready=[definition for definition in workflow['steps'] if steps[definition['id']]['state']!='completed'
                       and all(steps[d]['state']=='completed' for d in definition['depends_on'])]
                if not ready:raise WorkspaceError('workflow_cycle_or_missing_step')
                for definition in ready:
                    step=steps[definition['id']];action=definition['action'];inputs={}
                    for key,binding in definition['inputs'].items():
                        if 'literal' in binding:inputs[key]=binding['literal'];continue
                        reference=binding['ref'];value={'event':event,'context':context,'steps':outputs}
                        for keypart in reference.split('.'):
                            if not isinstance(value,dict) or keypart not in value:raise WorkspaceError('workflow_input_unavailable')
                            value=value[keypart]
                        inputs[key]=value
                    allowed_inputs={'propose_memory_write':{'result','target','task_id'},'apply_memory_write':{'intent'},
                        'update_task':{'task_id'},'link_records':{'from','to','owner','type'},'propose_provision':{'spec'},
                        'apply_provision':{'plan'},'verify_readback':{'intent','operation'},'request_user_input':{'question'}}
                    if set(inputs)-allowed_inputs.get(action,set()):raise WorkspaceError('workflow_invalid_inputs')
                    if action=='propose_memory_write':
                        if inputs.get('result')!=actual['id']:raise WorkspaceError('workflow_invalid_inputs')
                        if any(inputs[k] not in {r['id'] for r in context['records']} for k in ('target','task_id') if k in inputs):raise WorkspaceError('scope_denied')
                    if action=='request_user_input' and (not isinstance(inputs.get('question'),str) or not inputs['question']):raise WorkspaceError('workflow_invalid_inputs')
                    step['state']='running';run['status']='running';run['next_action']=None;checkpoint()
                    transaction_hook('before_step',step['step_id'])
                    if action=='propose_memory_write':
                        proposed=proposals.get(step['step_id'])
                        if proposed is None:
                            step['state']='awaiting_agent';run['status']='awaiting_agent';run['next_action']='Active agent must propose a source-backed MemoryWriteIntent for step '+step['step_id']
                            checkpoint();return envelope('WorkflowRun',run)
                        validate_document(proposed)
                        if proposed['type']!='MemoryWriteIntent' or proposed['data']['context_token']!=context['context_token']:raise WorkspaceError('workflow_proposal_context_changed')
                        outputs[step['step_id']]={'intent_ref':copy.deepcopy(proposed)}
                    elif action=='request_user_input':
                        answer=proposals.get(step['step_id'])
                        if not isinstance(answer,dict) or set(answer)!={'answer'} or not isinstance(answer['answer'],str) or not answer['answer'].strip():
                            step['state']='awaiting_user';run['status']='awaiting_user';run['next_action']=inputs['question']
                            checkpoint();return envelope('WorkflowRun',run)
                        outputs[step['step_id']]={'answer':answer['answer']}
                    elif action in ('update_task','link_records'):
                        proposed=proposals.get(step['step_id']) or outputs.get(step['step_id'],{}).get('intent_ref')
                        if proposed is None:
                            step['state']='awaiting_agent';run['status']='awaiting_agent';run['next_action']='Active agent must supply a typed '+action+' intent'
                            checkpoint();return envelope('WorkflowRun',run)
                        validate_document(proposed)
                        if proposed['type']!='MemoryWriteIntent' or proposed['data']['context_token']!=context['context_token']:raise WorkspaceError('workflow_proposal_context_changed')
                        allowed={'update_task'} if action=='update_task' else {'link'}
                        if not proposed['data']['changes'] or any(c['action'] not in allowed for c in proposed['data']['changes']):raise WorkspaceError('workflow_action_mismatch')
                        if proposed['data']['idempotency_key']!=step['idempotency_key']:raise WorkspaceError('workflow_step_key_required')
                        outputs[step['step_id']]={'intent_ref':copy.deepcopy(proposed)};checkpoint()
                        result=MemoryIntentService(self.runtime).apply(proposed,_coordinated=True)
                        outputs[step['step_id']]['operation_ref']=result
                    elif action=='propose_provision':
                        proposed=proposals.get(step['step_id']) or outputs.get(step['step_id'],{}).get('spec_ref')
                        if proposed is None:
                            step['state']='awaiting_agent';run['status']='awaiting_agent';run['next_action']='Active agent must propose a WorkspaceSpec'
                            checkpoint();return envelope('WorkflowRun',run)
                        validate_document(proposed)
                        if proposed['type']!='WorkspaceSpec' or proposed['data']['desired_profile']['id']!=event['workspace_id'] or proposed['data']['authority_ref']!=request['grant_ref']:raise WorkspaceError('scope_denied')
                        from beyin_v3_workspace_plan import plan_workspace
                        from beyin_v3_sources import probe_capabilities
                        from beyin_v3_workspaces import validate_for_provision
                        snapshot=self.registry.snapshot()
                        plan=plan_workspace(proposed,envelope('RegistrySnapshot',snapshot),self.registry.bindings(snapshot)[1],self.registry.capture_files(),
                            validate_document=validate_for_provision,capabilities=probe_capabilities(self.registry.vault))
                        outputs[step['step_id']]={'spec_ref':copy.deepcopy(proposed),'plan_ref':plan}
                    elif action=='apply_provision':
                        plan=inputs.get('plan')
                        if not isinstance(plan,dict):raise WorkspaceError('workflow_plan_required')
                        validate_document(plan)
                        if plan['type']!='ProvisionPlan' or not any(isinstance(o,dict) and o.get('plan_ref')==plan for o in outputs.values()):raise WorkspaceError('scope_denied')
                        try:self.runtime.authority.check_authority(plan,'apply')
                        except (PermissionError,WorkspaceError):
                            step['state']='awaiting_user';run['status']='awaiting_user';run['next_action']='Review and authorize the exact provision plan for '+step['step_id']
                            checkpoint();return envelope('WorkflowRun',run)
                        outputs[step['step_id']]={'applied_plan_ref':copy.deepcopy(plan),'operation_key':step['idempotency_key']};checkpoint()
                        result=self._provisioner().apply(plan,step['idempotency_key'],_coordinated=True)
                        outputs[step['step_id']]['operation_ref']=result
                    elif action=='apply_memory_write':
                        proposed=inputs.get('intent')
                        if not isinstance(proposed,dict):raise WorkspaceError('workflow_intent_required')
                        if proposed['data']['idempotency_key']!=step['idempotency_key']:raise WorkspaceError('workflow_step_key_required')
                        result=MemoryIntentService(self.runtime).apply(proposed,_coordinated=True)
                        outputs[step['step_id']]={'operation_ref':result}
                    elif action=='verify_readback':
                        proposed=inputs.get('intent')
                        if not isinstance(proposed,dict):
                            operation=inputs.get('operation')
                            owned=next((o for o in outputs.values() if isinstance(o,dict) and 'applied_plan_ref' in o and o.get('operation_ref')==operation),None)
                            if owned is None:raise WorkspaceError('workflow_operation_required')
                            self._provisioner().apply(owned['applied_plan_ref'],owned['operation_key'],_coordinated=True)
                            fresh=self.runtime.context(request_document)['data']
                            if fresh['state'] not in ('ready','partial'):raise WorkspaceError('workflow_verification_failed')
                            outputs[step['step_id']]={'verified':True}
                            step['state']='completed';step['result_ref']=run_id+'/'+step['step_id'];checkpoint();continue
                        result=MemoryIntentService(self.runtime).apply(proposed,_coordinated=True)
                        if not all(p['state'] in ('verified','skipped') for p in result['data']['parts']):raise WorkspaceError('workflow_verification_failed')
                        outputs[step['step_id']]={'verified':True,'operation_ref':result}
                    elif action in self.handlers and action not in ('apply_provision','propose_provision'):
                        outputs[step['step_id']]=self.handlers[action](inputs,step['idempotency_key'],context)
                    else:
                        step['state']='awaiting_user';run['status']='awaiting_user';run['next_action']='A typed shared service or user input is required for '+action
                        checkpoint();return envelope('WorkflowRun',run)
                    transaction_hook('effect_returned',step['step_id'])
                    step['state']='completed';step['result_ref']=run_id+'/'+step['step_id'];checkpoint()
            view_stale=any(isinstance(output,dict) and any(issue.get('code')=='managed_view_stale'
                for issue in output.get('operation_ref',{}).get('data',{}).get('issues',[])) for output in outputs.values())
            run['status']='completed'
            run['next_action']=('managed_view_stale: Canonical changes are saved; inspect and retry the authorized managed view refresh.' if view_stale else None)
            checkpoint()
        return envelope('WorkflowRun',run)
