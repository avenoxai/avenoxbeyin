import copy
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'template/.claude/scripts'))
from beyin_v3_workspaces import Registry,LocalAuthority,envelope,render_document
from v3_workspace_test_io import load_utf8_json
from beyin_v3_workspace_context import WorkspaceRuntime, membership_hash
from beyin_v3_sync import SyncEngine

FIX=ROOT/'tests/fixtures/v3/workspaces/v0.1.0'


def install_fixture(base,case):
    base=Path(base).resolve();vault=base/'vault';vault.mkdir(exist_ok=True);state=base/'state'
    inp=load_utf8_json(FIX/case/'input.json');docs={d['type']:d['data'] for d in inp['documents']}
    spec=copy.deepcopy(docs['WorkspaceSpec']);reference=case+'-user'
    registry=Registry(vault,state)
    if registry.snapshot() is None:registry.compare_and_save(envelope('RegistryFormat',docs['RegistryFormat']),None)
    for source in spec['sources']:registry.compare_and_save(envelope('SourceDescriptor',source),None)
    registry.compare_and_save(envelope('WorkspaceProfile',spec['desired_profile']),None)
    shutil.copytree(FIX/case/'files/vault',vault,dirs_exist_ok=True)
    permissions=[]
    for binding,suffix in zip(spec['binding_requests'],['vault','source-a','source-b']):
        locator=vault if suffix=='vault' else base/(case+'-'+suffix)
        if suffix!='vault':shutil.copytree(FIX/case/'files'/suffix,locator)
        binding.update(locator=str(locator),authority_ref=reference)
        path=state/'workspace-bindings'/(binding['source_id']+'.json');path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(render_document(envelope('SourceBinding',binding)))
        permissions.extend(dict(g,source_id=binding['source_id'],locator=str(locator)) for g in binding['grants'])
    LocalAuthority(registry).record_approval(reference,machine_id='fixture-machine',permissions=permissions,evidence='Explicit synthetic fixture authority')
    spec['authority_ref']=reference
    sync=SyncEngine(vault,state);sync.sync()
    runtime=WorkspaceRuntime(vault,state);runtime.adopt(envelope('WorkspaceSpec',spec), membership_hash(runtime.registry))
    request=copy.deepcopy(docs['RequestContext']);request['grant_ref']=reference
    return runtime,spec,envelope('RequestContext',request)
