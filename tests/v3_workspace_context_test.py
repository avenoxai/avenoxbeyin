import copy
import json
from pathlib import Path
import tempfile
import unittest

from v3_workspace_helpers import install_fixture
from beyin_v3_workspaces import envelope,WorkspaceError,canonical_bytes,LocalAuthority
from beyin_v3_workspace_context import WorkspaceRuntime,request_scope
from beyin_v3_sync import SyncEngine


class WorkspaceContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.runtime,self.spec,self.request=install_fixture(self.base,'academic')

    def test_course_task_grade_learning_and_sources_work_together(self):
        result=self.runtime.context(self.request)['data']
        ids={r['id'] for r in result['records']}
        self.assertTrue({'academic-topic','academic-task','academic-result','academic-learning'}<=ids)
        grade=next(r for r in result['records'] if r['id']=='academic-result')
        self.assertEqual(grade['facts']['score'],82);self.assertIsNone(grade['facts']['weight'])
        self.assertIn('academic-result.weight',result['missing_facts'])
        self.assertTrue(result['relations']);self.assertTrue(result['rules'])
        self.assertTrue(any(s['source_id']=='academic-materials' for s in result['source_refs']))

    def test_custom_uses_same_runtime_and_cross_workspace_relation_gives_no_access(self):
        install_fixture(self.base,'custom')
        path=self.runtime.registry.vault/'notes/academic-result.md'
        text=path.read_text(encoding='utf-8').replace('"workspace_relations": [','"workspace_relations": [{"id":"forbidden-edge","workspace_id":"academic","type":"belongs_to","from":{"workspace_id":"academic","record_id":"academic-task"},"to":{"workspace_id":"custom","record_id":"custom-topic"},"evidence_refs":[]},')
        path.write_text(text, encoding='utf-8');SyncEngine(self.runtime.registry.vault,self.runtime.registry.state).sync()
        academic=self.runtime.context(self.request)['data']
        self.assertFalse(any(r['workspace_id']=='custom' for r in academic['records']))
        self.assertNotIn('forbidden-edge',canonical_bytes(academic).decode())
        runtime,spec,request= self.runtime,self.spec,self.request
        doc=copy.deepcopy(request);doc['data']['grant_ref']='custom-user';doc['data']['signals']=[{'kind':'explicit_user','workspace_id':'custom','subject_refs':[{'workspace_id':'custom','record_id':'custom-task'}]}]
        doc['data']['query']='Son çekim sonucuyla devam et'
        custom=runtime.context(doc)['data'];self.assertTrue(any(r['id']=='custom-learning' for r in custom['records']))

    def test_private_untrusted_and_unknown_scope_never_enter_any_read_path(self):
        vault,state=self.runtime.registry.vault,self.runtime.registry.state
        for id,extra in [('private-canary',{'visibility':'private'}),('untrusted-canary',{'trust':'untrusted'}),('unknown-scope',{})]:
            metadata={'id':id,'kind':'note','revision':1,'visibility':'internal','workspace_id':'academic'}
            if id=='unknown-scope':metadata.pop('workspace_id')
            metadata.update(extra);(vault/('notes/'+id+'.md')).write_text('---\n'+json.dumps(metadata)+'\n---\nCANARY\n')
        SyncEngine(vault,state).sync();self.runtime.activate()
        result=self.runtime.context(self.request)
        self.assertNotIn('CANARY',canonical_bytes(result).decode())
        with request_scope('academic','academic-user'):
            self.assertFalse(self.runtime.store.retrieve('CANARY')['records'])
            self.assertFalse(self.runtime.store.source_snapshot(['private-canary.md','untrusted-canary.md','unknown-scope.md'])['records'])
            self.assertFalse(self.runtime.store.history('private-canary'))
        self.assertFalse(self.runtime.store.retrieve('Algoritmalar')['records'])

    def test_automatic_routing_and_session_isolation(self):
        install_fixture(self.base,'custom');self.runtime.activate()
        first=self.runtime.auto_context('codex','Algoritmalar ödev karmaşıklık',session_id='one')['data']
        self.assertEqual(first['workspace_id'],'academic')
        second=self.runtime.auto_context('claude','Düşük ışık çekim',session_id='two')['data']
        self.assertEqual(second['workspace_id'],'custom')
        follow=self.runtime.auto_context('codex','devam',session_id='one')['data']
        self.assertEqual(follow['workspace_id'],'academic')
        unknown=self.runtime.auto_context('codex','xyz',session_id='fresh')['data']
        self.assertEqual(unknown['state'],'ambiguous');self.assertFalse(unknown['records'])

    def test_explicit_shared_method_does_not_share_course_rule(self):
        custom,custom_spec,custom_request=install_fixture(self.base,'custom')
        from beyin_v3_sync import render
        vault=self.runtime.registry.vault;state=self.runtime.registry.state
        path=vault/'notes/general-method.md'
        path.write_text(render({'id':'general-method','kind':'note','revision':1,'visibility':'internal'},'Compare evidence before changing a method.'), encoding='utf-8')
        SyncEngine(vault,state).sync()
        for spec in (self.spec,custom_spec):
            adopted=copy.deepcopy(spec);adopted['adopt_record_refs']=[{'workspace_id':spec['desired_profile']['id'],'record_id':'general-method'}]
            self.runtime.adopt(envelope('WorkspaceSpec',adopted))
        for request in (self.request,custom_request):
            request=copy.deepcopy(request);wid=request['data']['signals'][0]['workspace_id']
            request['data']['signals'][0]['subject_refs'].append({'workspace_id':wid,'record_id':'general-method'})
            context=self.runtime.context(request)['data']
            self.assertTrue(any(r['id']=='general-method' for r in context['records']))
            for rule in context['rules']:
                self.assertTrue(all(r['workspace_id']==wid for r in rule['subject_refs']))
            self.assertFalse(any(r['workspace_id']!=wid for r in context['records']))

    def test_read_only_active_context_does_not_write_session_context_or_cache(self):
        from beyin_v3 import MemoryStore
        self.runtime.activate();vault=self.runtime.registry.vault;state=self.runtime.registry.state
        store=MemoryStore(state,vault,read_only=True)
        before={p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()}
        result=store.context_for('codex','Algoritmalar ödev karmaşıklık',workspace_session='read-only')
        self.assertTrue(result['records']);self.assertEqual(result['context_token'],'no-memory')
        after={p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()}
        self.assertEqual(after,before)
        runtime=WorkspaceRuntime(vault,state,read_only=True)
        for call in (runtime.activate,lambda:runtime.adopt(envelope('WorkspaceSpec',self.spec)),lambda:runtime.sync(self.request)):
            with self.assertRaisesRegex(WorkspaceError,'read_only_runtime'):call()

    def test_new_process_equivalent_store_and_revoked_authority(self):
        before=self.runtime.context(self.request)['data']
        fresh=WorkspaceRuntime(self.runtime.registry.vault,self.runtime.registry.state)
        after=fresh.context(self.request)['data']
        self.assertEqual(before['records'],after['records'])
        self.runtime.authority.revoke('academic-user')
        result=fresh.context(self.request)['data'];self.assertEqual(result['state'],'denied');self.assertFalse(result['records'])


if __name__=='__main__':unittest.main()
