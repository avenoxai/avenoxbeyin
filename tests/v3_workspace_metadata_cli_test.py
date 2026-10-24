"""Metadata discovery must remain useful without touching any source or index."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from v3_workspace_helpers import ROOT,FIX
from v3_workspace_test_io import load_utf8_json, run_json_cli
from beyin_v3_workspaces import render_document,envelope,validate_document


def load_cli():
    spec=importlib.util.spec_from_file_location('workspace_metadata_cli',ROOT/'scripts/beyin_v3.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class WorkspaceMetadataCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.vault=self.root/'vault';self.vault.mkdir();self.state=self.root/'state'

    def call(self,*args):
        process=run_json_cli([sys.executable,'-B',str(ROOT/'scripts/beyin_v3.py'),
            '--vault',str(self.vault),'--state',str(self.state),'workspace',*args],capture_output=True,timeout=15)
        self.assertFalse(process.stderr,process.stderr)
        return process,json.loads(process.stdout)

    def install_registry(self):
        documents=load_utf8_json(FIX/'academic/input.json')['documents']
        spec=next(d['data'] for d in documents if d['type']=='WorkspaceSpec')
        base=self.vault/'.beyin-workspaces';(base/'workspaces').mkdir(parents=True);(base/'sources').mkdir()
        (base/'format.json').write_bytes(render_document(envelope('RegistryFormat',{'registry_id':'fixture','layout':'per_workspace_json'})))
        profile=copy.deepcopy(spec['desired_profile']);profile['purpose']='PRIVATE_PURPOSE'
        (base/'workspaces'/(profile['id']+'.json')).write_bytes(render_document(envelope('WorkspaceProfile',profile)))
        for source in spec['sources']:
            (base/'sources'/(source['id']+'.json')).write_bytes(render_document(envelope('SourceDescriptor',source)))
        # Intentionally unreadable metadata is never needed for discovery.
        (self.vault/'notes').mkdir();(self.vault/'notes/secret.md').write_text('PRIVATE_BODY')
        self.state.mkdir();(self.state/'memory.sqlite3').write_bytes(b'PRIVATE_INDEX_NOT_SQLITE')
        (self.state/'workspace-bindings').mkdir();(self.state/'workspace-bindings/broken.json').write_text('PRIVATE_LOCATOR')

    def test_empty_list_never_initializes_runtime(self):
        process,result=self.call('list')
        self.assertEqual(process.returncode,0);self.assertEqual(result['workspace_metadata']['workspaces'],[])
        self.assertFalse(self.state.exists());self.assertEqual(list(self.vault.iterdir()),[])

    def test_list_and_inspect_read_only_metadata_without_sources_or_bindings(self):
        self.install_registry()
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for args in (('list',),('inspect','academic')):
            process,result=self.call(*args);self.assertEqual(process.returncode,0)
            self.assertEqual(result['workspace_metadata']['workspaces'][0]['id'],'academic')
            self.assertNotIn('PRIVATE',process.stdout)
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_pending_and_invalid_requests_are_safe(self):
        for args in (('inspect','PRIVATE_MISSING'),('list','PRIVATE_PATH'),('list','--limit','0')):
            process,result=self.call(*args);self.assertNotEqual(process.returncode,0);validate_document(result)
            self.assertNotIn('PRIVATE',process.stdout);self.assertFalse(self.state.exists())
        (self.state/'workspace-provision').mkdir(parents=True)
        (self.state/'workspace-provision/pending.json').write_text('PRIVATE_PENDING')
        process,result=self.call('list');self.assertEqual(process.returncode,3)
        self.assertEqual(result['data']['issues'][0]['code'],'operation_pending');self.assertNotIn('PRIVATE',process.stdout)

    def test_preflight_error_identifies_change_without_disclosing_path(self):
        from beyin_v3_workspace_memory import CanonicalTargetError
        result,code=load_cli().workspace_failure(CanonicalTargetError('invalid_create_note_target','new-note'))
        validate_document(result);self.assertEqual(code,2)
        self.assertEqual(result['data']['parts'],[{'id':'new-note','state':'failed'}])
        self.assertEqual(result['data']['issues'][0]['code'],'invalid_create_note_target')
        self.assertIn('notes/',result['data']['next_action'])

    def test_raw_workspace_error_payload_is_not_printed(self):
        cli=load_cli()
        for exc in (ValueError('/PRIVATE_SOURCE body'),OSError('/PRIVATE_PATH'),RuntimeError('PRIVATE_TRACE')):
            result,code=cli.workspace_failure(exc)
            validate_document(result);self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotEqual(code,0)


if __name__=='__main__':unittest.main()
