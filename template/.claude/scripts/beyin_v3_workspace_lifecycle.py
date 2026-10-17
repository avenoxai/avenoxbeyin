"""C5 creates G0 specs; D1 plans and D2 applies them. No competing writer."""
import copy

from beyin_v3_workspaces import WorkspaceError,envelope,content_hash,canonical_bytes,parse_document,safe_path
from beyin_v3_sync import atomic


class LifecycleService:
    def __init__(self,runtime):self.runtime=runtime;self.registry=runtime.registry

    def spec(self,workspace_id,action,authority_ref,*,source_id=None,descriptor=None,binding=None):
        if action not in ('archive','restore','connect','disconnect'):raise WorkspaceError('unsupported_lifecycle_action')
        snapshot=self.registry.snapshot();profiles={p['id']:p for p in snapshot['profiles']}
        if workspace_id not in profiles:raise WorkspaceError('workspace_unavailable')
        if authority_ref not in self.runtime.authority._load()['grants']:raise WorkspaceError('authority_required')
        old=profiles[workspace_id];profile=copy.deepcopy(old);bindings,_=self.registry.bindings(snapshot)
        sources={s['id']:copy.deepcopy(s) for s in snapshot['sources']};requests={id:copy.deepcopy(b) for id,b in bindings.items() if id in old['source_ids']}
        if action in ('archive','restore'):profile['lifecycle']='archived' if action=='archive' else 'active'
        if action=='disconnect':
            if source_id not in old['source_ids']:raise WorkspaceError('source_not_member')
            profile['source_ids'].remove(source_id)
            if source_id in requests:
                b=requests[source_id];grants=[g for g in b['grants'] if g['workspace_id']!=workspace_id]
                if b['grants']!=grants:b['grants']=grants;b['revision']+=1
        if action=='connect':
            if descriptor is None or binding is None:raise WorkspaceError('binding_request_required')
            envelope('SourceDescriptor',descriptor);envelope('SourceBinding',binding)
            source_id=descriptor['id']
            if binding['source_id']!=source_id:raise WorkspaceError('binding_identity_mismatch')
            if source_id in sources and descriptor!=sources[source_id]:raise WorkspaceError('shared_source_change_requires_plan')
            if source_id not in profile['source_ids']:profile['source_ids'].append(source_id)
            sources[source_id]=copy.deepcopy(descriptor);requests[source_id]=copy.deepcopy(binding)
        if profile!=old:profile['revision']+=1
        ids=set(profile['source_ids'])|set(old['source_ids'])
        return envelope('WorkspaceSpec',{'spec_id':'lifecycle-'+content_hash(canonical_bytes([workspace_id,action,source_id]))[:32],
            'user_goal':action+' '+old['name'],'desired_profile':profile,'sources':[sources[id] for id in sorted(ids)],
            'binding_requests':list(requests.values()),'adopt_record_refs':[],'required_capabilities':[],
            'unresolved':[],'authority_ref':authority_ref})

    def legacy_preview(self):
        from beyin_v3_projects import read_project_scopes
        return read_project_scopes(self.registry.vault)

    def adopt_legacy(self,project,workspace_id,authority_ref):
        snapshot=self.registry.snapshot();profile=next((p for p in snapshot['profiles'] if p['id']==workspace_id),None)
        if profile is None:raise WorkspaceError('workspace_unavailable')
        from beyin_v3_projects import read_project_scopes
        from beyin_v3 import record_scope
        scopes=read_project_scopes(self.registry.vault)
        records=self.runtime.store._records()
        ids=[r['id'] for r in records if (record_scope(r,scopes) if scopes else r.get('project'))==project]
        spec=envelope('WorkspaceSpec',{'spec_id':'legacy-'+content_hash(project)[:32],'user_goal':'Adopt explicitly selected legacy project',
            'desired_profile':profile,'sources':[],'binding_requests':[],
            'adopt_record_refs':[{'workspace_id':workspace_id,'record_id':id} for id in ids],
            'required_capabilities':[],'unresolved':[],'authority_ref':authority_ref})
        with self.registry.coordinated():
            path=safe_path(self.registry.state,'workspace-legacy-map.json')
            mapping=parse_document(path.read_bytes()) if path.exists() else {}
            if project in mapping and mapping[project]!=workspace_id:raise WorkspaceError('legacy_mapping_conflict')
            self.runtime.adopt(spec,_coordinated=True)
            mapping[project]=workspace_id;atomic(path,canonical_bytes(mapping).decode())
        return ids
