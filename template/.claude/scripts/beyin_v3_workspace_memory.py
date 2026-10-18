"""G0 intents orchestrate the existing SyncEngine; no second source writer."""
import copy
import json
from contextlib import nullcontext
from datetime import datetime,timezone

from beyin_v3_workspaces import (WorkspaceError,canonical_bytes,content_hash,envelope,parse_document,safe_path,validate_document)
from beyin_v3_workspace_context import request_scope
from beyin_v3_sync import SyncEngine,parse,render,atomic


def transaction_hook(phase,index=None):
    """Fault seam for durable intent / shared-writer boundaries."""


class CanonicalTargetError(WorkspaceError):
    """A predictable writer rejection; expose the change ID, never its path."""
    def __init__(self, code, change_id):
        self.change_id = change_id
        super().__init__(code)


def preflight_create_targets(changes):
    """Match canonical new-file roots without restricting legacy update/adoption.

    Run for the entire intent before journaling or applying any part. Profile
    note_roots remain a scope boundary, not a replacement for writer rules.
    """
    for change in changes:
        action = change['action']
        source = change['target']['relative_path']
        if action == 'create_note' and (not source.startswith(('notes/', 'knowledge/')) or not source.endswith('.md')):
            raise CanonicalTargetError('invalid_create_note_target', change['id'])
        if action == 'create_task' and (not source.startswith('tasks/') or not source.endswith('.md')):
            raise CanonicalTargetError('invalid_create_task_target', change['id'])


class MemoryIntentService:
    def __init__(self,runtime):
        self.runtime=runtime;self.registry=runtime.registry
        self.engine=SyncEngine(self.registry.vault,self.registry.state)

    def _context(self,intent):
        token=intent['context_token']
        if token=='no-memory':raise WorkspaceError('no_memory')
        if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):raise WorkspaceError('invalid_context')
        context=parse_document(safe_path(self.registry.state,'workspace-contexts/'+token+'.json',True).read_bytes())
        if context['request']['no_memory']:raise WorkspaceError('no_memory')
        if context['workspace_id']!=intent['workspace_id']:raise WorkspaceError('scope_denied')
        snapshot=self.registry.snapshot();bindings,binding_hash=self.registry.bindings(snapshot)
        profile=next((p for p in snapshot['profiles'] if p['id']==intent['workspace_id']),None)
        if (not profile or profile['lifecycle']!='active' or profile['revision']!=intent['profile_revision']
                or snapshot['snapshot_sha256']!=context['snapshot_sha256'] or binding_hash!=intent['binding_snapshot_sha256']
                or binding_hash!=context['binding_snapshot_sha256']):raise WorkspaceError('context_changed')
        for change in intent['changes']:
            binding=bindings.get(change['target']['source_id'])
            if not binding or not self.runtime.authority.allows(context['request']['grant_ref'],binding,intent['workspace_id'],'write_memory'):
                raise WorkspaceError('scope_denied')
        return context

    def _changes(self,change,context):
        updates=copy.deepcopy(change['metadata_changes'])
        if change['relation_changes']:
            records={r['id']:r for r in self.engine.store._records()}
            from beyin_v3_workspace_context import record_allowed
            for relation in change['relation_changes']:
                for reference in (relation['from'],relation['to']):
                    record=records.get(reference['record_id'])
                    if (reference['workspace_id']!=context['workspace_id'] or not record
                            or not record_allowed(self.engine.store,record,context['request']['audience'])
                            or record.get('trust')=='untrusted' or (record.get('visibility')=='private' and context['request']['audience']!='private')):
                        raise WorkspaceError('scope_denied')
            path=safe_path(self.registry.vault,change['target']['relative_path'],True)
            metadata,_=parse(path.read_text(encoding='utf-8'))
            relations={r['id']:r for r in metadata.get('workspace_relations',[])}
            relations.update({r['id']:r for r in change['relation_changes']})
            updates['workspace_relations']=list(relations.values())
        return updates

    def _prepare(self,change,context,*,_preserve_body=False):
        target=change['target'];action=change['action']
        path,_,_=self.runtime.resolver.resolve(context['workspace_id'],target['source_id'],target['relative_path'],context['request']['grant_ref'],access='write_memory')
        actual=content_hash(path.read_bytes()) if path.exists() else None
        if actual!=target['expected_source_sha256']:raise WorkspaceError('revision_conflict')
        updates=self._changes(change,context);updates,_=self.engine._protect_metadata(updates)
        text,_=self.engine._protect(change['body'])
        if _preserve_body and action=='update_note' and text!=change['body']:
            raise WorkspaceError('managed_view_body_requires_review')
        if action.startswith('create_'):
            current=self.engine.store._records()
            if any(r['id']==target['record_id'] for r in current):raise WorkspaceError('record_exists')
            if any(r.get('text','').strip()==text.strip() for r in current):raise WorkspaceError('duplicate_requires_review')
            metadata=dict(updates,id=target['record_id'],kind='task' if action=='create_task' else 'note',revision=1,
                          workspace_id=context['workspace_id'],visibility='internal')
            facts=metadata.get('facts',{})
            if 'entity_type' in facts:metadata['entity_type']=facts['entity_type']
            if action=='create_task':
                if not isinstance(facts.get('owner'),str) or not facts['owner']:raise WorkspaceError('explicit_task_owner_required')
                metadata['owner']=facts['owner']
                today=datetime.now(timezone.utc).strftime('%Y-%m-%d')
                metadata.setdefault('updated_at',today);metadata.setdefault('created_at',today)
            intended=render(metadata,text+'\n')
        else:
            record=next((r for r in self.engine.store._records() if r['id']==target['record_id']),None)
            if not record or record['revision']!=target['expected_revision'] or record['source']!=target['relative_path']:
                raise WorkspaceError('revision_conflict')
            if (record.get('visibility')=='private' and context['request']['audience']!='private') or record.get('trust')=='untrusted':raise WorkspaceError('scope_denied')
            metadata,old_body=parse(path.read_bytes().decode('utf-8'))
            metadata.update(updates);metadata['revision']=target['expected_revision']+1
            intended=render(metadata,old_body if action in ('update_task','link') else text if (_preserve_body or text.endswith('\n')) else text+'\n')
        return {'source':target['relative_path'],'after_sha256':content_hash(intended),'updates':updates,'metadata':metadata,'text':text,'done':False}

    def apply(self,document,*,_coordinated=False,_refresh_views=True,_preserve_body=False):
        if type(_preserve_body) is not bool:raise WorkspaceError('invalid_body_mode')
        validate_document(document)
        if document['type']!='MemoryWriteIntent':raise WorkspaceError('invalid_schema')
        intent=document['data'];context=self._context(intent)
        if len({(c['target']['source_id'],c['target']['relative_path']) for c in intent['changes']})!=len(intent['changes']):
            raise WorkspaceError('duplicate_write_target')
        key=content_hash(intent['idempotency_key']);journal_path=safe_path(self.registry.state,'workspace-write-intents/'+key+'.json')
        payload_hash=content_hash(canonical_bytes(document));replay=False;parts=[]
        with (nullcontext() if _coordinated else self.registry.coordinated()),request_scope(context['workspace_id'],context['request']['grant_ref']):
            context=self._context(intent)
            preflight_create_targets(intent['changes'])
            for change in intent['changes']:
                for evidence in change['evidence_refs']:
                    self.runtime.resolver.read(evidence,context['workspace_id'],context['request']['grant_ref'],audience=context['request']['audience'])
            if journal_path.exists():
                journal=parse_document(journal_path.read_bytes())
                if journal['payload_sha256']!=payload_hash or journal.get('preserve_body',False)!=_preserve_body:raise WorkspaceError('idempotency_collision')
                replay=all(p.get('done') for p in journal['parts'])
            else:
                journal={'payload_sha256':payload_hash,'preserve_body':_preserve_body,'parts':[{'skip':c['action'],'done':True} if c['action'] in ('skip','review') else self._prepare(c,context,_preserve_body=_preserve_body) for c in intent['changes']]}
                atomic(journal_path,json.dumps(journal,ensure_ascii=False,allow_nan=False));transaction_hook('prepared')
            for index,(change,part) in enumerate(zip(intent['changes'],journal['parts'])):
                if 'skip' in part:parts.append({'id':change['id'],'state':'skipped'});continue
                self._context(intent)
                path=safe_path(self.registry.vault,part['source']);actual=content_hash(path.read_bytes()) if path.exists() else None
                if actual!=part['after_sha256']:
                    if part['done'] or actual!=change['target']['expected_source_sha256']:raise WorkspaceError('revision_conflict')
                    action=change['action'];target=change['target'];transaction_hook('before_writer',index)
                    if action=='update_note':self.engine.update_note(target['record_id'],target['expected_revision'],target['expected_source_sha256'],part['text'],part['updates'],_preserve_body=_preserve_body)
                    elif action=='update_task':self.engine.update_task(target['record_id'],target['expected_revision'],part['updates'])
                    elif action=='create_note':self.engine.note_create(part['source'],part['text'],part['metadata'])
                    elif action=='create_task':self.engine.task_create(part['source'],part['text'],part['metadata'])
                    elif action=='link':
                        record=next(r for r in self.engine.store._records() if r['id']==target['record_id'])
                        if record.get('kind')=='task':self.engine.update_task(target['record_id'],target['expected_revision'],part['updates'])
                        else:
                            # Metadata-only links must preserve the original body,
                            # including its missing final LF, just as preparation hashes it.
                            _,body=parse(path.read_bytes().decode('utf-8'));self.engine.update_note(target['record_id'],target['expected_revision'],target['expected_source_sha256'],body,part['updates'],_preserve_body=True)
                    transaction_hook('writer_returned',index)
                self.engine.sync()
                indexed=next((r for r in self.engine.store._records() if r['id']==change['target']['record_id']),None)
                if not indexed or indexed['source_sha256']!=part['after_sha256'] or content_hash(path.read_bytes())!=part['after_sha256']:raise WorkspaceError('readback_conflict')
                part['done']=True;atomic(journal_path,json.dumps(journal,ensure_ascii=False,allow_nan=False));transaction_hook('checkpoint',index)
                parts.append({'id':change['id'],'state':'verified'})
        workflow_issues=[]
        # Private derived-view writes disable both recursive refresh and workflow
        # redispatch. Ordinary canonical writes still dispatch once as before.
        if not _coordinated and _refresh_views:
            from beyin_v3_workspace_workflow import WorkflowRunner
            try:
                runs=WorkflowRunner(self.runtime).dispatch(envelope('RequestContext',context['request']),[c['target']['record_id'] for c in intent['changes'] if c['action'] not in ('skip','review')])
                workflow_issues=[{'code':'workflow_'+r['data']['status'],'detail':r['data']['run_id']} for r in runs if r['data']['status']!='completed']
            except (ValueError,OSError):workflow_issues=[{'code':'workflow_dispatch_pending'}]
        result=envelope('OperationResult',{'operation_id':intent['intent_id'],'operation_status':'replayed' if replay else 'applied',
            'workspace_health':'verification_pending','parts':parts,'capabilities':[{'name':'canonical_update','state':'ready'}],
            'verification':[{'check':'source_index_readback','state':'passed','evidence_refs':[]}],
            'issues':workflow_issues,'next_action':'Verify retrieval in a fresh process/client; this receipt does not certify all workspace capabilities.'})
        if _refresh_views:
            result=self._refresh_views(document,result,_coordinated=_coordinated)
        return result

    def _refresh_views(self,document,result,*,_coordinated):
        # Canonical source/index readback has already succeeded. A view failure
        # cannot turn that success into an apparent rollback or erase its parts.
        try:
            from beyin_v3_workspace_views import refresh_after_apply
            refreshed=refresh_after_apply(self.runtime,document,copy.deepcopy(result),_coordinated=_coordinated)
            validate_document(refreshed)
            original=result['data'];data=refreshed['data']
            if (refreshed['type']!='OperationResult' or data['operation_id']!=original['operation_id']
                    or data['operation_status']!=original['operation_status'] or data['parts']!=original['parts']
                    or any(check not in data['verification'] for check in original['verification'])):
                raise ValueError('view_result_must_preserve_canonical_write')
            return refreshed
        except Exception:
            # Never include the callback exception: it may contain source text,
            # paths or parser details. The previous view remains a separate concern.
            stale=copy.deepcopy(result);data=stale['data']
            data['workspace_health']='verification_pending'
            data['issues'].append({'code':'managed_view_stale','detail':'Canonical changes succeeded; the managed view could not be refreshed.'})
            data['next_action']='Canonical changes are saved. Inspect the managed view and retry its authorized refresh; verify retrieval in a fresh session.'
            return stale
