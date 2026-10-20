"""Request-scoped workspace policy and integrated, source-backed daily context."""
from contextlib import contextmanager,nullcontext
from contextvars import ContextVar
import copy
import json
from pathlib import Path

from beyin_v3_workspaces import (Registry,LocalAuthority,WorkspaceError,canonical_bytes,content_hash,
    envelope,parse_document,safe_path,validate_document,local_root_matches)
from beyin_v3_sources import SourceResolver

CURRENT=ContextVar('beyin_workspace_request',default=None)


def enabled(vault,state):
    path=safe_path(state,'workspace-runtime.json')
    if not path.exists():return False
    value=parse_document(path.read_bytes())
    if set(value)!={'enabled'} or type(value['enabled']) is not bool:
        raise WorkspaceError('invalid_workspace_runtime')
    return value['enabled']


def bound_project_for_cwd(vault, state, cwd):
    """Resolve a local binding with existing read authority; names grant nothing."""
    try:
        current = Path(cwd).resolve(strict=True)
        registry = Registry(vault, state)
        snapshot = registry.snapshot()
        if not snapshot:
            return None
        bindings, _ = registry.bindings(snapshot)
        authority = LocalAuthority(registry)
        candidates = []
        for profile in snapshot['profiles']:
            if profile['lifecycle'] != 'active':
                continue
            for sid in profile['source_ids']:
                binding = bindings.get(sid)
                if not binding:
                    continue
                root = Path(binding['locator'])
                if not root.is_absolute() or not root.is_dir():
                    continue
                root = root.resolve(strict=True)
                if current.is_relative_to(root) and authority.allows(binding['authority_ref'], binding, profile['id']):
                    candidates.append((len(root.parts), profile['id']))
        if not candidates:
            return None
        depth = max(n for n, _ in candidates)
        selected = {wid for n, wid in candidates if n == depth}
        if len(selected) != 1:
            return None
        wid = selected.pop()
        mapping = safe_path(state, 'workspace-legacy-map.json')
        aliases = [name for name, target in parse_document(mapping.read_bytes()).items() if target == wid] if mapping.exists() else []
        return aliases[0] if len(aliases) == 1 else wid
    except (OSError, ValueError, RuntimeError, WorkspaceError):
        return None


def scope_ids(registry,record):
    declared=record.get('workspace_id')
    if isinstance(declared,str) and declared:return {declared}
    path=safe_path(registry.state,'workspace-adoptions.json')
    if not path.exists():return set()
    values=parse_document(path.read_bytes())
    return set(values.get(record['id'],[]))


def record_allowed(store,record,audience='internal'):
    current=CURRENT.get()
    if not enabled(store.vault_root,store.state_dir) and current is None:return True
    if not current:return False
    registry=Registry(store.vault_root,store.state_dir)
    if current['workspace_id'] not in scope_ids(registry,record):return False
    snapshot=registry.snapshot();profiles={p['id']:p for p in snapshot['profiles']}
    profile=profiles.get(current['workspace_id'])
    if not profile or (profile['lifecycle']!='active' and not (current.get('include_archived') and profile['lifecycle']=='archived')):return False
    bindings,_=registry.bindings(snapshot);authority=LocalAuthority(registry)
    for source in snapshot['sources']:
        if source['kind']=='vault' and source['id'] in profile['source_ids']:
            binding=bindings.get(source['id'])
            if binding and local_root_matches(binding['locator'],registry.vault) and authority.allows(current['grant_ref'],binding,profile['id'],audience=audience):
                return True
    return False


@contextmanager
def request_scope(workspace_id,grant_ref,*,include_archived=False):
    token=CURRENT.set({'workspace_id':workspace_id,'grant_ref':grant_ref,'include_archived':bool(include_archived)})
    try:yield
    finally:CURRENT.reset(token)


class WorkspaceRuntime:
    def __init__(self,vault,state,*,readers=None,read_only=False,store=None):
        self.read_only=bool(read_only or getattr(store,'read_only',False))
        self.registry=Registry(vault,state);self.authority=LocalAuthority(self.registry)
        self.resolver=SourceResolver(self.registry,readers=readers,cache_write=not self.read_only)
        from beyin_v3 import MemoryStore
        self.store=store or MemoryStore(state,vault,read_only=read_only)

    def sync(self,request_document):
        """Sync canonical edits, then dispatch only fresh authorized record changes."""
        if self.read_only:raise WorkspaceError('read_only_runtime')
        validate_document(request_document)
        request=request_document['data']
        from beyin_v3_sync import SyncEngine,atomic
        pending=None
        if not request['no_memory']:
            pending=safe_path(self.registry.state,'workspace-sync-events/'+content_hash(canonical_bytes(request_document))+'.json')
        with self.registry.coordinated():
            if pending is not None and pending.exists():before=parse_document(pending.read_bytes())['before']
            else:
                before={r['id']:r.get('source_sha256') for r in self.store._records()}
                if pending is not None:atomic(pending,canonical_bytes({'before':before}).decode())
        checked=SyncEngine(self.registry.vault,self.registry.state).sync()
        if checked.get('conflicts'):raise WorkspaceError('source_sync_conflict')
        changed=[r['id'] for r in self.store._records() if before.get(r['id'])!=r.get('source_sha256')]
        from beyin_v3_workspace_workflow import WorkflowRunner
        checked['workflow_runs']=WorkflowRunner(self).dispatch(request_document,changed) if changed else []
        if pending is not None and pending.exists():pending.unlink()
        return checked

    def activate(self):
        if self.read_only:raise WorkspaceError('read_only_runtime')
        snapshot=self.registry.snapshot()
        if not snapshot:raise WorkspaceError('registry_required')
        from beyin_v3_sync import atomic
        with self.registry.coordinated():atomic(safe_path(self.registry.state,'workspace-runtime.json'),'{"enabled":true}')

    def adopt(self,spec_document,*,_coordinated=False):
        if self.read_only:raise WorkspaceError('read_only_runtime')
        validate_document(spec_document)
        if spec_document['type']!='WorkspaceSpec':raise WorkspaceError('invalid_schema')
        spec=spec_document['data'];wid=spec['desired_profile']['id']
        snapshot=self.registry.snapshot();profile=next((p for p in snapshot['profiles'] if p['id']==wid),None)
        if profile!=spec['desired_profile']:raise WorkspaceError('profile_changed')
        records={r['id']:r for r in self.store._records()};bindings,_=self.registry.bindings(snapshot)
        vault_bindings=[b for id,b in bindings.items() if id in profile['source_ids'] and local_root_matches(b['locator'],self.registry.vault)]
        if not any(self.authority.allows(spec['authority_ref'],b,wid) for b in vault_bindings):raise WorkspaceError('scope_denied')
        ids=[]
        for reference in spec['adopt_record_refs']:
            record=records.get(reference['record_id'])
            if reference['workspace_id']!=wid or not record:raise WorkspaceError('adoption_record_unavailable')
            if record.get('workspace_id') not in (None,wid):raise WorkspaceError('adoption_scope_conflict')
            if record.get('visibility')=='private' or record.get('trust')=='untrusted':raise WorkspaceError('adoption_requires_review')
            ids.append(record['id'])
        from beyin_v3_sync import atomic
        with (nullcontext() if _coordinated else self.registry.coordinated()):
            path=safe_path(self.registry.state,'workspace-adoptions.json')
            values=parse_document(path.read_bytes()) if path.exists() else {}
            for id in ids:values[id]=sorted(set(values.get(id,[]))|{wid})
            atomic(path,canonical_bytes(values).decode())
        return ids

    def _base(self,request,snapshot,bindings_hash,state='ready',workspace=None):
        return dict(request_id=request['request_id'],state=state,context_token='unresolved',workspace_id=workspace,
            profile_revision=None,snapshot_sha256=snapshot['snapshot_sha256'],binding_snapshot_sha256=bindings_hash,
            subject_refs=[],rules=[],records=[],relations=[],source_refs=[],missing_facts=[],issues=[])

    def context(self,document,*,include_archived=False,rules_project=None):
        validate_document(document)
        if document['type']!='RequestContext':raise WorkspaceError('invalid_schema')
        request=document['data'];snapshot=self.registry.snapshot()
        if not snapshot:raise WorkspaceError('registry_required')
        bindings,binding_hash=self.registry.bindings(snapshot)
        candidates={p['id']:p for p in snapshot['profiles'] if (p['lifecycle']=='active' or (include_archived and p['lifecycle']=='archived')) and any(
            id in p['source_ids'] and self.authority.allows(request['grant_ref'],b,p['id'],audience=request['audience']) for id,b in bindings.items())}
        signals=[s for s in request['signals'] if s['workspace_id'] in candidates]
        explicit={s['workspace_id'] for s in signals if s['kind']=='explicit_user'}
        selected=explicit or {s['workspace_id'] for s in signals}
        result=self._base(request,snapshot,binding_hash)
        if len(selected)!=1:
            result['state']='ambiguous' if candidates else 'denied';result['issues']=[{'code':'ambiguous_context' if candidates else 'scope_denied'}]
            return envelope('WorkContext',result)
        wid=selected.pop();profile=candidates[wid];result['workspace_id']=wid;result['profile_revision']=profile['revision']
        from beyin_v3 import _tokens,STOPWORDS
        terms=_tokens(request['query'])-STOPWORDS
        with request_scope(wid,request['grant_ref'],include_archived=include_archived):
            eligible,_=self.store._eligible(request['audience'])
        if not include_archived:
            from beyin_v3 import RETIRED_STATUSES,_status_word
            superseded=self.store._superseded_ids()
            eligible=[r for r in eligible if r['id'] not in superseded and _status_word(r) not in RETIRED_STATUSES]
        subject_ids={r['record_id'] for s in signals if s['workspace_id']==wid for r in s['subject_refs'] if r['workspace_id']==wid}
        by_id={r['id']:r for r in eligible}
        subject_ids &= by_id.keys()
        if not subject_ids:
            scores=[(len(terms & _tokens(r.get('title','')+' '+r['text'])),r['id']) for r in eligible]
            subject_ids={id for score,id in sorted(scores,reverse=True)[:4] if score>0}
        selected_ids=set(subject_ids)
        # A small generic graph closure, filtering both endpoints before expansion.
        relations=[]
        for record in eligible:
            for relation in record.get('workspace_relations',[]):
                try:validate_document(envelope('Relation',relation))
                except WorkspaceError:result['issues'].append({'code':'invalid_relation'});continue
                a,b=relation['from'],relation['to']
                if relation['workspace_id']==wid and a['workspace_id']==b['workspace_id']==wid and a['record_id'] in by_id and b['record_id'] in by_id:
                    relations.append(relation)
        for _ in range(3):
            for relation in relations:
                a,b=relation['from']['record_id'],relation['to']['record_id']
                if {a,b}&selected_ids:selected_ids.update((a,b))
        rules, rules_truncated = self.behavior_rules(profile, request, selected_ids, rules_project)
        if rules_truncated:result['issues'].append({'code':'rules_truncated'})
        records=[];sources=[];remaining=max(0,min(request['budget_chars'],20000)-sum(len(r['text']) for r in rules))
        vault_sources=[s['id'] for s in snapshot['sources'] if s['kind']=='vault' and s['id'] in profile['source_ids']
                       and s['id'] in bindings and local_root_matches(bindings[s['id']]['locator'],self.registry.vault)]
        if not vault_sources and selected_ids:raise WorkspaceError('source_unbound')
        for id in sorted(selected_ids,key=lambda id:(id not in subject_ids,id))[:128]:
            record=by_id[id]
            if remaining<=0:break
            text=record['text'][:remaining]
            if not text:continue
            sr={'source_id':vault_sources[0],'relative_path':record['source'],'content_sha256':record['source_sha256'],
                'fragment':{'kind':'whole'},'extractor':{'id':'markdown','version':'1','settings_sha256':content_hash(b'{}')}}
            facts=record.get('facts',{})
            flat={k:v for k,v in facts.items() if v is None or type(v) in (str,int,float,bool) or (isinstance(v,list) and all(x is None or type(x) in (str,int,float,bool) for x in v))}
            if len(flat)!=len(facts):result['issues'].append({'code':'unsupported_nested_metadata'})
            projected=dict(id=id,workspace_id=wid,entity_type=record.get('entity_type',record.get('kind','note')),kind=record.get('kind','note'),
                revision=record['revision'],source_ref=sr,text=text,facts=flat,relations=[r for r in relations if r in record.get('workspace_relations',[])],
                visibility=record['visibility'],trust='trusted')
            for key in ('status','due_at','next_action'):
                if key in record:projected[key]=record[key]
            if 'due_at' in projected:projected['due_at']=projected['due_at'][:10]
            validate_document(envelope('Record',projected));records.append(projected);sources.append(sr);remaining-=len(text)
            result['missing_facts'].extend(id+'.'+k for k,v in flat.items() if v is None)
        for descriptor in snapshot['sources']:
            if descriptor['kind']=='vault' or descriptor['id'] not in profile['source_ids'] or remaining<=0:continue
            try:
                names=self.resolver.files(wid,descriptor['id'],request['grant_ref'],audience=request['audience'],archived=include_archived)
                for name in names:
                    if remaining<=0:break
                    extracted=self.resolver.extract(wid,descriptor['id'],name,request['grant_ref'],audience=request['audience'],archived=include_archived)
                    if extracted['state']!='ready':result['issues'].append({'code':extracted['state']})
                    for chunk in extracted['chunks']:
                        if remaining<=0:break
                        if terms and not terms&_tokens(chunk['text']):continue
                        sr=chunk['source_ref'];text=chunk['text'][:remaining]
                        if not text:continue
                        id='src-'+content_hash(canonical_bytes([sr['source_id'],sr['relative_path'],sr['fragment']]))[:32]
                        records.append(dict(id=id,workspace_id=wid,entity_type='source_fragment',kind='source',revision=1,
                            source_ref=sr,text=text,facts={},relations=[],visibility='internal',trust='trusted'))
                        sources.append(sr);remaining-=len(text)
            except WorkspaceError as exc:result['issues'].append({'code':exc.code})
        result.update(records=records,source_refs=sources,subject_refs=[{'workspace_id':wid,'record_id':id} for id in sorted(subject_ids)],
            relations=[r for r in relations if r['from']['record_id'] in {x['id'] for x in records} and r['to']['record_id'] in {x['id'] for x in records}],
            rules=rules)
        if result['issues']:result['state']='partial'
        if self.registry.snapshot()['snapshot_sha256']!=snapshot['snapshot_sha256'] or self.registry.bindings()[1]!=binding_hash:
            raise WorkspaceError('snapshot_changed')
        for source in sources:self.resolver.read(source,wid,request['grant_ref'],audience=request['audience'],archived=include_archived)
        if request['no_memory'] or self.read_only:
            result['context_token']='no-memory'
            return envelope('WorkContext',result)
        token=content_hash(canonical_bytes([request,profile['revision'],snapshot['snapshot_sha256'],binding_hash,include_archived]))
        result['context_token']=token
        from beyin_v3_sync import atomic
        atomic(safe_path(self.registry.state,'workspace-contexts/'+token+'.json'),canonical_bytes({'request':request,'workspace_id':wid,
            'profile_revision':profile['revision'],'snapshot_sha256':snapshot['snapshot_sha256'],'binding_snapshot_sha256':binding_hash}).decode())
        return envelope('WorkContext',result)

    def behavior_rules(self, profile, request, selected_ids, project=None):
        """Project > workspace > global; every source passes existing workspace gates."""
        from beyin_v3_companion import directory, project_rule_directory, filter_scoped_rules
        from beyin_v3_projects import read_project_scopes, _scope_key
        wid = profile['id']
        mapping = safe_path(self.registry.state, 'workspace-legacy-map.json')
        aliases = parse_document(mapping.read_bytes()) if mapping.exists() else {}
        # A rules-only query signal must describe this authorized workspace.
        if project and aliases.get(project, project) != wid:
            project = None
        if not project:
            names = [name for name, target in aliases.items() if target == wid]
            project = names[0] if len(names) == 1 else wid
        project_dirs = []
        scopes = read_project_scopes(self.registry.vault)
        for folder, name in (scopes or {}).get('folders', []):
            if _scope_key(name) == _scope_key(project):
                project_dirs.append(project_rule_directory(self.registry.vault, folder))
        for folder in ('projects', 'Projeler', 'projeler'):
            project_dirs.append(project_rule_directory(self.registry.vault, folder+'/'+project))
        def source_rules(directories):
            result=[];seen=set()
            for folder in directories:
                if folder is None:
                    continue
                relative=folder.relative_to(self.registry.vault).as_posix()
                snapshot=self.store.source_snapshot(['Kurallar.md','.rules.md','rules.md'], source_directory=relative,
                    budget_chars=20000, text_transform=lambda name,text: filter_scoped_rules(text,project))
                for record in snapshot['records']:
                    if record['source'] not in seen:
                        seen.add(record['source'])
                        result.append(dict(id='source-'+content_hash(record['source'])[:24],
                            text='['+record['source']+'] '+record['text'],subject_refs=[]))
            return result
        with request_scope(wid,request['grant_ref']):
            specific=source_rules(project_dirs)
            general=source_rules([directory(self.registry.vault)])
        workspace=[r for r in profile['working_rules'] if not r['subject_refs'] or any(
            x['workspace_id']==wid and x['record_id'] in selected_ids for x in r['subject_refs'])]
        layers=[('project',specific),('workspace',workspace),('global',general)]
        layers=[(name,rules) for name,rules in layers if rules]
        remaining=min(3000,request['budget_chars']//4);result=[];truncated=False
        for index,(name,rules) in enumerate(layers):
            share=remaining//(len(layers)-index)
            for rule in rules:
                item=copy.deepcopy(rule)
                overhead=len(json.dumps(dict(item,text=''),ensure_ascii=False))+2
                if share<=overhead+14:
                    truncated=True
                    break
                text='['+name+'] '+item['text']
                capacity=share-overhead
                if len(text)>capacity:
                    text=text[:max(0,capacity-14)]+' [truncated]';truncated=True
                item['text']=text
                used=len(json.dumps(item,ensure_ascii=False))+2
                result.append(item);share-=used;remaining-=used
        return result,truncated

    def auto_context(self,client,query,*,session_id,project=None,audience='internal',budget_chars=8000,no_memory=False,rules_project=None):
        """Use existing grants and per-session continuity; never infer new authority."""
        from beyin_v3 import _tokens,STOPWORDS
        snapshot=self.registry.snapshot();bindings,_=self.registry.bindings(snapshot)
        if project is not None:
            mapping_path=safe_path(self.registry.state,'workspace-legacy-map.json')
            if mapping_path.exists():project=parse_document(mapping_path.read_bytes()).get(project,project)
        grants=self.authority._load()['grants'];options=[];terms=_tokens(query)-STOPWORDS
        for profile in snapshot['profiles']:
            if profile['lifecycle']!='active':continue
            references=[ref for ref in grants if any(id in profile['source_ids'] and self.authority.allows(ref,b,profile['id'],audience=audience) for id,b in bindings.items())]
            if not references:continue
            reference=sorted(references)[0]
            with request_scope(profile['id'],reference):records,_=self.store._eligible(audience)
            score=len(terms&_tokens(profile['name']+' '+profile['purpose']))
            score+=max([len(terms&_tokens(r.get('title','')+' '+r['text'])) for r in records] or [0])
            if project is not None:score=100000 if project==profile['id'] else -1
            options.append((score,profile['id'],reference))
        path=safe_path(self.registry.state,'workspace-sessions/'+content_hash(str(session_id))+'.json')
        previous=parse_document(path.read_bytes()) if path.exists() else {}
        options.sort(reverse=True);choice=None
        if len(options)==1 and options[0][0]>=0:choice=options[0]
        if options and options[0][0]>0 and (len(options)==1 or options[0][0]>options[1][0]):choice=options[0]
        if choice is None and project is None and (not options or options[0][0]==0):
            choice=next((o for o in options if o[1]==previous.get('workspace_id')),None)
        request={'request_id':'request-'+content_hash(canonical_bytes([session_id,query,project]))[:32],
            'session_id':str(session_id),'client':client,'query':query or 'Güncel çalışma bağlamı',
            'grant_ref':choice[2] if choice else 'unresolved','audience':audience,'no_memory':bool(no_memory or self.read_only),
            'signals':[] if not choice else [{'kind':'explicit_user' if project else 'conversation_subject','workspace_id':choice[1],'subject_refs':[]}],
            'budget_chars':budget_chars}
        if not choice:
            result=self._base(request,snapshot,self.registry.bindings(snapshot)[1],'ambiguous' if options else 'denied')
            result['issues']=[{'code':'ambiguous_context' if options else 'scope_denied'}]
            return envelope('WorkContext',result)
        context=self.context(envelope('RequestContext',request),rules_project=rules_project)
        from beyin_v3_sync import atomic
        if not no_memory and not self.read_only:atomic(path,canonical_bytes({'workspace_id':choice[1]}).decode())
        return context

    def legacy_context(self,client,query,**kwargs):
        import os
        import uuid
        from beyin_v3 import pack_context
        session_id=kwargs.pop('workspace_session',None) or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID') or 'manual-'+uuid.uuid4().hex
        budget=kwargs.get('budget_chars',8000)
        context=self.auto_context(client,query,session_id=session_id,project=kwargs.get('project'),audience=kwargs.get('audience','internal'),budget_chars=budget,rules_project=kwargs.get('rules_project'))['data']
        records=[]
        for source in context['records']:
            r=copy.deepcopy(source);sr=r['source_ref']
            r['source']=sr['relative_path'] if r['kind']!='source' else sr['source_id']+':'+sr['relative_path']+'#'+json.dumps(sr['fragment'],ensure_ascii=False,separators=(',',':'))
            r['source_sha256']=sr['content_sha256'];records.append(r)
            r['remote_allowed']=False  # Local workspace grants are not remote-provider consent.
        result=pack_context(records,kwargs.get('limit',5),budget,0)
        result.update(workspace_id=context['workspace_id'],workspace_state=context['state'],context_token=context['context_token'],
                      profile_revision=context['profile_revision'],issues=context['issues'],working_rules=context['rules'],
                      rule_precedence='Davranış kuralı önceliği: proje > workspace > global; erişim/yetki sınırları değişmez.')
        return result
