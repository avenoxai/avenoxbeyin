"""I1: actual D1/D2 configuration and actual C1/C3/C4 daily use. No model calls."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from v3_workspace_helpers import FIX,ROOT
from beyin_v3_workspaces import Registry,LocalAuthority,envelope,validate_for_provision,content_hash,canonical_bytes
from beyin_v3_workspace_plan import plan_workspace
from beyin_v3_provision import Provisioner
from beyin_v3_workspace_context import WorkspaceRuntime
from beyin_v3_workspace_lifecycle import LifecycleService
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_workspace_workflow import WorkflowRunner
from beyin_v3_sync import SyncEngine
from beyin_v3_parser_assets import managed_files
from beyin_v3_sources import probe_capabilities


def provision_fixture(base,case,*,activate=True):
    base=Path(base).resolve();vault=base/'vault';vault.mkdir(exist_ok=True);state=base/'state'
    docs={d['type']:d['data'] for d in json.loads((FIX/case/'input.json').read_text())['documents']}
    spec=copy.deepcopy(docs['WorkspaceSpec']);reference=case+'-user';spec['authority_ref']=reference
    shutil.copytree(FIX/case/'files/vault',vault,dirs_exist_ok=True)
    permissions=[]
    for binding,suffix in zip(spec['binding_requests'],['vault','source-a','source-b']):
        path=vault if suffix=='vault' else base/(case+'-'+suffix)
        if suffix!='vault':shutil.copytree(FIX/case/'files'/suffix,path)
        binding.update(locator=str(path),authority_ref=reference)
        permissions.extend(dict(g,source_id=binding['source_id'],locator=str(path)) for g in binding['grants'])
    for name,raw in managed_files().items():
        path=vault/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    registry=Registry(vault,state);authority=LocalAuthority(registry)
    authority.record_approval(reference,machine_id='fixture-machine',permissions=permissions,evidence='Explicit synthetic I1 request')
    snapshot=registry.snapshot(registry_id='synthetic-brain');files=registry.capture_files();bindings_hash=registry.bindings(snapshot)[1]
    capabilities=probe_capabilities(vault)
    plan=plan_workspace(envelope('WorkspaceSpec',spec),envelope('RegistrySnapshot',snapshot),bindings_hash,files,
        validate_document=validate_for_provision,capabilities=capabilities)
    assert registry.capture_files()==files
    authority.approve_plan(plan,evidence='Exact synthetic I1 plan preview')
    provisioner=Provisioner(vault,state,validate_document=validate_for_provision,
        check_authority=authority.check_authority,check_snapshot=authority.check_snapshot)
    result=provisioner.apply(plan,'I1-'+case)
    assert result['data']['workspace_health']=='verification_pending'
    SyncEngine(vault,state).sync();runtime=WorkspaceRuntime(vault,state);runtime.adopt(envelope('WorkspaceSpec',spec))
    if activate:runtime.activate()
    request=copy.deepcopy(docs['RequestContext']);request['grant_ref']=reference
    return runtime,spec,envelope('RequestContext',request),provisioner,plan


def bound_intent(case,context):
    document=copy.deepcopy(next(d for d in json.loads((FIX/case/'golden.json').read_text())['documents'] if d['type']=='MemoryWriteIntent'))
    data=document['data'];data.update(context_token=context['context_token'],binding_snapshot_sha256=context['binding_snapshot_sha256'],profile_revision=context['profile_revision'])
    records={r['id']:r for r in context['records']};evidence=records[case+'-result']['source_ref']
    for change in data['changes']:
        target=change['target'];record=records[target['record_id']]
        target.update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
        change['evidence_refs']=[evidence]
        for relation in change['relation_changes']:relation['evidence_refs']=[evidence]
    return document


class WorkspaceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()

    def test_builder_created_academic_and_custom_run_real_daily_workflow(self):
        for case in ('academic','custom','project','career'):
            runtime,spec,request,provisioner,plan=provision_fixture(self.base,case)
            context=runtime.context(request)['data'];intent=bound_intent(case,context)
            result_record=next(r for r in context['records'] if r['id']==case+'-result')
            event=envelope('WorkflowEvent',{'event_id':case+'-I1-event','workspace_id':case,'event_kind':'record_changed',
                'record':result_record,'evidence_refs':[result_record['source_ref']]})
            wf=spec['desired_profile']['workflows'][0]
            run_id=content_hash(canonical_bytes([case,wf['id'],wf['revision'],case+'-I1-event']))
            intent['data']['idempotency_key']=run_id+'/persist'
            before=set(runtime.registry.vault.rglob('*.md'))
            complete=WorkflowRunner(runtime).advance(event,wf['id'],request,proposals={'propose':intent})['data']
            self.assertEqual(complete['status'],'completed');self.assertEqual(set(runtime.registry.vault.rglob('*.md')),before)
            updated=runtime.context(request)['data'];self.assertTrue(any(r['id']==case+'-learning' and r['revision']==2 for r in updated['records']))
            request_path=self.base/(case+'-request.json');request_path.write_text(json.dumps(request))
            cli=subprocess.run([sys.executable,str(ROOT/'scripts/beyin_v3.py'),'--vault',str(runtime.registry.vault),'--state',str(runtime.registry.state),
                'workspace-context','--file',str(request_path)],capture_output=True,text=True)
            self.assertEqual(cli.returncode,0,cli.stderr);self.assertTrue(any(r['id']==case+'-learning' and r['revision']==2 for r in json.loads(cli.stdout)['data']['records']))

    def test_disconnect_archive_restore_preserve_sources_and_require_revert_authority(self):
        runtime,spec,request,provisioner,initial_plan=provision_fixture(self.base,'academic')
        source=self.base/'academic-source-a/material.txt';original=source.read_bytes()
        old_ref=next(s for s in runtime.context(request)['data']['source_refs'] if s['source_id']=='academic-materials')
        lifecycle=LifecycleService(runtime)
        def apply_spec(spec,key):
            registry=runtime.registry;snapshot=registry.snapshot();files=registry.capture_files()
            plan=plan_workspace(spec,envelope('RegistrySnapshot',snapshot),registry.bindings(snapshot)[1],files,validate_document=validate_for_provision,capabilities={})
            runtime.authority.approve_plan(plan,evidence='Explicit synthetic lifecycle request')
            engine=Provisioner(registry.vault,registry.state,validate_document=validate_for_provision,check_authority=runtime.authority.check_authority,check_snapshot=runtime.authority.check_snapshot)
            engine.apply(plan,key);return engine,plan
        engine,disconnect=apply_spec(lifecycle.spec('academic','disconnect','academic-user',source_id='academic-materials'),'disconnect')
        with self.assertRaises(ValueError):runtime.resolver.read(old_ref,'academic','academic-user')
        self.assertEqual(source.read_bytes(),original)
        with self.assertRaises(ValueError):engine.revert('disconnect')
        runtime.authority.approve_revert(disconnect,evidence='Explicit synthetic undo of source disconnect')
        engine.revert('disconnect');self.assertEqual(runtime.resolver.read(old_ref,'academic','academic-user'),original)
        apply_spec(lifecycle.spec('academic','archive','academic-user'),'archive')
        self.assertEqual(runtime.context(request)['data']['state'],'denied')
        historical=runtime.context(request,include_archived=True)['data'];self.assertTrue(historical['records'])
        apply_spec(lifecycle.spec('academic','restore','academic-user'),'restore')
        self.assertEqual(runtime.context(request)['data']['state'],'ready');self.assertEqual(source.read_bytes(),original)


if __name__=='__main__':unittest.main()
