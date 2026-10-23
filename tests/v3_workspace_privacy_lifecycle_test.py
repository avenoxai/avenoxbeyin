"""Cross-source revocation and direct Python surface privacy, using real D1/D2."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

import v3_workspace_integration_test as integration
from beyin_v3_workspaces import envelope,validate_for_provision
from beyin_v3_workspace_plan import plan_workspace
from beyin_v3_provision import Provisioner
from beyin_v3_workspace_lifecycle import LifecycleService
from beyin_v3_workspace_context import request_scope, membership_hash
from beyin_v3_sync import SyncEngine,render


class PrivacyLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.runtime,self.spec,self.request,_,_=integration.provision_fixture(self.base,'academic')

    def apply(self,spec,key):
        r=self.runtime.registry;snapshot=r.snapshot()
        plan=plan_workspace(spec,envelope('RegistrySnapshot',snapshot),r.bindings(snapshot)[1],r.capture_files(),validate_document=validate_for_provision)
        self.runtime.authority.approve_plan(plan,evidence='Explicit synthetic lifecycle plan')
        return Provisioner(r.vault,r.state,validate_document=validate_for_provision,check_authority=self.runtime.authority.check_authority,
                           check_snapshot=self.runtime.authority.check_snapshot).apply(plan,key)

    def test_shared_connect_disconnect_revokes_only_selected_workspace(self):
        integration.provision_fixture(self.base,'custom')
        directory=self.base/'shared';directory.mkdir();source=directory/'shared.txt';source.write_text('Shared quartz source')
        before=source.read_bytes();authority=self.runtime.authority
        authority.record_approval('shared-user',machine_id='fixture-machine',permissions=[
            {'source_id':'shared','workspace_id':id,'access':'read','locator':str(directory)} for id in ('academic','custom')],evidence='Explicit shared read root request')
        descriptor={'id':'shared','kind':'directory','label':'Shared source'}
        binding={'source_id':'shared','machine_id':'fixture-machine','revision':1,'locator':str(directory),
                 'grants':[{'workspace_id':'academic','access':'read'}],'authority_ref':'shared-user'}
        lifecycle=LifecycleService(self.runtime)
        self.apply(lifecycle.spec('academic','connect','shared-user',descriptor=descriptor,binding=binding),'connect-academic')
        binding['revision']=2;binding['grants'].append({'workspace_id':'custom','access':'read'})
        self.apply(lifecycle.spec('custom','connect','shared-user',descriptor=descriptor,binding=binding),'connect-custom')
        ref=self.runtime.resolver.extract('academic','shared','shared.txt','shared-user')['chunks'][0]['source_ref']
        self.assertEqual(self.runtime.resolver.read(ref,'custom','shared-user'),before)
        self.apply(lifecycle.spec('academic','disconnect','shared-user',source_id='shared'),'disconnect-academic-shared')
        with self.assertRaises(ValueError):self.runtime.resolver.read(ref,'academic','shared-user')
        self.assertEqual(self.runtime.resolver.read(ref,'custom','shared-user'),before)
        self.assertEqual(source.read_bytes(),before)

    def test_legacy_mapping_conflict_cannot_adopt_before_rejecting(self):
        vault=self.runtime.registry.vault;state=self.runtime.registry.state
        (vault/'notes/legacy.md').write_text(render({'id':'legacy-new','revision':1,'kind':'note','project':'OldProject','visibility':'internal'},'Legacy evidence'))
        SyncEngine(vault,state).sync();mapping=state/'workspace-legacy-map.json';mapping.write_text(json.dumps({'OldProject':'another'}))
        adoption=state/'workspace-adoptions.json';before=adoption.read_bytes()
        with self.assertRaisesRegex(ValueError,'legacy_mapping_conflict'):
            LifecycleService(self.runtime).adopt_legacy('OldProject','academic','academic-user',membership_hash(self.runtime.registry))
        self.assertEqual(adoption.read_bytes(),before)

    def test_legacy_folder_adoption_does_not_globalize_shared_unscoped(self):
        vault=self.runtime.registry.vault;state=self.runtime.registry.state
        root=vault/'notes/legacy-group';root.mkdir()
        (vault/'.beyin-projects.json').write_text(json.dumps({'folders':{'notes/legacy-group':'OldProject'},'shared_unscoped':True}))
        for name,metadata in [('adopted',{}),('other',{'project':'OtherProject'})]:
            (root/(name+'.md')).write_text(render(dict(id='legacy-'+name,revision=1,kind='note',visibility='internal',**metadata),'Legacy '+name))
        (vault/'notes/unscoped.md').write_text(render(dict(id='legacy-global',revision=1,kind='note',visibility='internal'),'Unscoped'))
        SyncEngine(vault,state).sync();before={p:p.read_bytes() for p in vault.rglob('*.md')}
        adopted=LifecycleService(self.runtime).adopt_legacy('OldProject','academic','academic-user',membership_hash(self.runtime.registry))
        self.assertEqual(adopted,['legacy-adopted'])
        self.assertEqual({p:p.read_bytes() for p in before},before)
        with request_scope('academic','academic-user'):
            ids={r['id'] for r in self.runtime.store._eligible()[0]}
        self.assertIn('legacy-adopted',ids);self.assertNotIn('legacy-other',ids);self.assertNotIn('legacy-global',ids)

    def _plan_engine(self,spec):
        r=self.runtime.registry;snapshot=r.snapshot()
        plan=plan_workspace(spec,envelope('RegistrySnapshot',snapshot),r.bindings(snapshot)[1],r.capture_files(),validate_document=validate_for_provision)
        self.runtime.authority.approve_plan(plan,evidence='Exact synthetic fault/undo plan')
        engine=Provisioner(r.vault,r.state,validate_document=validate_for_provision,check_authority=self.runtime.authority.check_authority,check_snapshot=self.runtime.authority.check_snapshot)
        return plan,engine

    def _permissions(self):
        return [dict(g,source_id=b['source_id'],locator=b['locator']) for b in self.spec['binding_requests'] for g in b['grants']]

    def test_pending_allows_revoke_and_exact_undo_authority_but_not_reader(self):
        authority=self.runtime.authority;r=self.runtime.registry
        profile=r.vault/'.beyin-workspaces/workspaces/academic.json';before=profile.read_bytes()
        plan,engine=self._plan_engine(LifecycleService(self.runtime).spec('academic','archive','academic-user'))
        def crash(phase,index=None):
            if phase=='after_replace':raise RuntimeError('power loss')
        with patch('beyin_v3_provision.transaction_hook',crash),self.assertRaisesRegex(RuntimeError,'power loss'):engine.apply(plan,'pending-control')
        authority.revoke('academic-user')
        with self.assertRaisesRegex(ValueError,'operation_pending'):r.snapshot()
        with self.assertRaisesRegex(ValueError,'authority_required'):engine.recover('pending-control')
        authority.approve_revert(plan,evidence='Explicit undo while pending')
        authority.record_approval('academic-user',machine_id='fixture-machine',permissions=self._permissions(),evidence='Explicit restoration of exact original grants')
        with self.assertRaisesRegex(ValueError,'operation_pending'):r.snapshot()
        engine.revert('pending-control');self.assertEqual(profile.read_bytes(),before);self.assertFalse(r.pending())

    def test_revert_cannot_reopen_old_binding_without_current_permission(self):
        r=self.runtime.registry;authority=self.runtime.authority
        plan,engine=self._plan_engine(LifecycleService(self.runtime).spec('academic','disconnect','academic-user',source_id='academic-materials'))
        engine.apply(plan,'disconnect-for-undo')
        authority.record_approval('academic-user',machine_id='fixture-machine',permissions=[p for p in self._permissions() if p['source_id']!='academic-materials'],evidence='User revokes original source permission')
        authority.approve_revert(plan,evidence='Exact undo request does not itself create a new source grant')
        with self.assertRaisesRegex(ValueError,'scope_denied'):engine.revert('disconnect-for-undo')
        authority.record_approval('academic-user',machine_id='fixture-machine',permissions=self._permissions(),evidence='User explicitly authorizes original source again')
        engine.revert('disconnect-for-undo')
        self.assertIn('academic-materials',next(p for p in r.snapshot()['profiles'] if p['id']=='academic')['source_ids'])

    def test_direct_triage_and_advisor_are_closed_in_active_mode(self):
        from beyin_v3_triage import scan_stale_tasks,scan_knowledge_digest,scan_inbox,scan_companion,triage
        import beyin_v3_jev as advisor
        vault=self.runtime.registry.vault;state=self.runtime.registry.state;transport=Mock()
        for call in (lambda:scan_stale_tasks(vault,time.time(),state=state),lambda:scan_knowledge_digest(vault,time.time(),state=state),
                     lambda:scan_inbox(vault,time.time(),state=state),lambda:scan_companion(vault,state),lambda:triage(vault,state)):
            with self.assertRaisesRegex(ValueError,'scoped_context'):call()
        with request_scope('academic','academic-user'):
            for call in (lambda:advisor.advise_context(self.runtime.store,'query',project='academic',transport=transport),
                         lambda:advisor.verify_answer(self.runtime.store,[],project='academic',transport=transport),
                         lambda:advisor.review_candidate(self.runtime.store,{},project='academic',transport=transport)):
                with self.assertRaisesRegex(ValueError,'scoped_consent'):call()
        transport.assert_not_called()


if __name__=='__main__':unittest.main()
