"""Standalone installed-runtime acceptance. Only test data comes from checkout."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

vault,state,fixtures,cli=map(Path,sys.argv[1:5])
ocr_enabled=len(sys.argv)>5 and sys.argv[5]=='require-ocr'
sys.path.insert(0,str(vault/'.claude/scripts'))
import beyin_v3_workspaces as ws
from beyin_v3_sources import probe_capabilities
from beyin_v3_workspace_context import WorkspaceRuntime
from beyin_v3_workspace_plan import plan_workspace
from beyin_v3_provision import Provisioner
from beyin_v3_sync import SyncEngine,parse,render
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_workspace_workflow import WorkflowRunner
from beyin_v3_workspace_lifecycle import LifecycleService
assert Path(ws.__file__).is_relative_to(vault)
ocr_raw=(fixtures.parent/'runtime/ocr-scan-tr-en.pdf').read_bytes()
capabilities=probe_capabilities(vault,ocr_probe=(ocr_raw,('Türkçe öğrenim','English ceramic firing')) if ocr_enabled else None)
assert all(capabilities.get(k)=='ready' for k in ('text','markdown','pdf_text','docx_text','workflow')),capabilities
results=[]
for case in ('academic','ceramics'):
    source_case='academic' if case=='academic' else 'custom'
    docs={d['type']:d['data'] for d in json.loads((fixtures/source_case/'input.json').read_text(encoding='utf-8'))['documents']}
    spec=copy.deepcopy(docs['WorkspaceSpec'])
    replacements={'custom':case,'shooting_topic':'recipe','shooting_task':'firing_task','shooting_result':'test_result','technique':'lesson'} if case=='ceramics' else {}
    def transform(value):
        if isinstance(value,dict):return {k:transform(v) for k,v in value.items()}
        if isinstance(value,list):return [transform(v) for v in value]
        if isinstance(value,str):
            for old,new in replacements.items():value=value.replace(old,new)
        return value
    spec=transform(spec)
    if case=='ceramics':
        holdout=json.loads((fixtures/'holdout.json').read_text(encoding='utf-8'));spec['user_goal']=holdout['user_goal']
        if ocr_enabled:spec['required_capabilities'].append('ocr_tr_en')
        spec['desired_profile'].update(name='Seramik denemeleri',purpose=holdout['user_goal'])
    reference=case+'-user';spec['authority_ref']=reference
    for original in (fixtures/source_case/'files/vault').rglob('*.md'):
        relative=transform(original.relative_to(fixtures/source_case/'files/vault').as_posix())
        path=vault/relative;path.parent.mkdir(parents=True,exist_ok=True)
        metadata,body=parse(original.read_text(encoding='utf-8'));metadata=transform(metadata)
        if case=='ceramics':
            body={'topic':'Reçete A; pişirim 1040 derece.', 'result':'Son denemede yüzey çatladı. Pişirim sonucu kaynaklı gözlem.',
                  'learning':'Önceki reçeteyi ve pişirim sonucunu karşılaştır.', 'task':'Sonraki kontrollü pişirimi hazırla.'}[original.stem.split('-')[-1]]+'\n'
        path.write_text(render(metadata,body), encoding='utf-8')
    permissions=[]
    for binding,suffix in zip(spec['binding_requests'],('vault','source-a','source-b')):
        path=vault if suffix=='vault' else state.parent/(case+'-'+suffix)
        if suffix!='vault':shutil.copytree(fixtures/source_case/'files'/suffix,path)
        binding.update(locator=str(path),authority_ref=reference)
        permissions.extend(dict(g,source_id=binding['source_id'],locator=str(path)) for g in binding['grants'])
    if case=='ceramics' and ocr_enabled:(state.parent/(case+'-source-a')/'tarama.pdf').write_bytes(ocr_raw)
    registry=ws.Registry(vault,state);authority=ws.LocalAuthority(registry)
    authority.record_approval(reference,machine_id='fixture-machine',permissions=permissions,evidence='Explicit synthetic installed acceptance request')
    snapshot=registry.snapshot(registry_id='installed-brain')
    plan=plan_workspace(ws.envelope('WorkspaceSpec',spec),ws.envelope('RegistrySnapshot',snapshot),registry.bindings(snapshot)[1],registry.capture_files(),
        validate_document=ws.validate_for_provision,capabilities=capabilities)
    authority.approve_plan(plan,evidence='Exact installed plan')
    provisioner=Provisioner(vault,state,validate_document=ws.validate_for_provision,check_authority=authority.check_authority,check_snapshot=authority.check_snapshot)
    provisioner.apply(plan,'installed-'+case)
    SyncEngine(vault,state).sync();runtime=WorkspaceRuntime(vault,state);runtime.adopt(ws.envelope('WorkspaceSpec',spec));runtime.activate()
    request=transform(copy.deepcopy(docs['RequestContext']));request['grant_ref']=reference
    if case=='ceramics':request['query']='yüzey çatladı ceramic firing reçete'
    request=ws.envelope('RequestContext',request)
    context=runtime.context(request)['data']
    if case=='ceramics' and ocr_enabled:assert any(r['source_ref']['relative_path']=='tarama.pdf' and 'Türkçe öğrenim' in r['text'] for r in context['records'])
    records={r['id']:r for r in context['records']}
    intent=transform(copy.deepcopy(next(d for d in json.loads((fixtures/source_case/'golden.json').read_text(encoding='utf-8'))['documents'] if d['type']=='MemoryWriteIntent')))
    data=intent['data'];data.update(context_token=context['context_token'],binding_snapshot_sha256=context['binding_snapshot_sha256'],profile_revision=context['profile_revision'])
    evidence=records[case+'-result']['source_ref']
    for change in data['changes']:
        record=records[change['target']['record_id']]
        change['target'].update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
        change['evidence_refs']=[evidence]
        for relation in change['relation_changes']:relation['evidence_refs']=[evidence]
        if case=='ceramics' and change['action']=='update_note':change['body']='Yüzey çatlaması gözlendi; reçete ve pişirim eğrisi birlikte karşılaştırılacak. [[ceramics-topic]] ![[kil.png]]\n'
    event=ws.envelope('WorkflowEvent',{'event_id':case+'-installed-result','workspace_id':case,'event_kind':'record_changed','record':records[case+'-result'],'evidence_refs':[evidence]})
    wf=spec['desired_profile']['workflows'][0]
    run_id=ws.content_hash(ws.canonical_bytes([case,wf['id'],wf['revision'],event['data']['event_id']]))
    data['idempotency_key']=run_id+'/persist';runner=WorkflowRunner(runtime)
    before=set(vault.rglob('*.md'))
    assert runner.advance(event,wf['id'],request,proposals={'propose':intent})['data']['status']=='completed'
    assert runner.advance(event,wf['id'],request)['data']['status']=='completed'
    assert set(vault.rglob('*.md'))==before
    request_path=state.parent/(case+'-request.json');request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    recall=subprocess.run([sys.executable,'-I',str(cli),'--vault',str(vault),'--state',str(state),'workspace-context','--file',str(request_path)],capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert recall.returncode==0,recall.stderr
    recalled=json.loads(recall.stdout)['data'];learning=next(r for r in recalled['records'] if r['id']==case+'-learning')
    assert learning['revision']==2 and learning['source_ref']['content_sha256']
    if case=='ceramics':assert 'çatlaması' in learning['text'] and '[[ceramics-topic]]' in learning['text']
    source=state.parent/(case+'-source-a/material.txt');raw=source.read_bytes()
    spec_doc=LifecycleService(runtime).spec(case,'disconnect',reference,source_id=case+'-materials')
    snap=registry.snapshot();plan=plan_workspace(spec_doc,ws.envelope('RegistrySnapshot',snap),registry.bindings(snap)[1],registry.capture_files(),validate_document=ws.validate_for_provision,capabilities=capabilities)
    authority.approve_plan(plan,evidence='Explicit installed disconnect');provisioner.apply(plan,'disconnect-'+case)
    assert source.read_bytes()==raw
    assert not any(r['source_id']==case+'-materials' for r in runtime.context(request)['data']['source_refs'])
    results.append({'case':case,'workflow':'completed','fresh_process_revision':learning['revision'],'disconnect_preserved_source':True})
sys.stdout.buffer.write(json.dumps({'capabilities':capabilities,'scenarios':results}, ensure_ascii=False).encode('utf-8'))
