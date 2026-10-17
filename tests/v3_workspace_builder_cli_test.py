"""Real early CLI dispatch, without a hidden MemoryStore or unsafe error dump."""
import json
import copy
from pathlib import Path
import subprocess
import sys
import unittest
import tempfile
import zipfile

import v3_workspace_builder_c1_test as c1
from beyin_v3_parser_assets import managed_files
from beyin_v3_workspaces import validate_document

ROOT=Path(__file__).resolve().parents[1]


class BuilderCliTest(unittest.TestCase):
    def setUp(self):
        self.h=c1.RealC1BuilderTest();self.h.setUp();self.addCleanup(self.h.tmp.cleanup)
        for name,raw in managed_files().items():
            path=self.h.vault/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)

    def call(self,action,payload=None,*,raw=None,file='-'):
        result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/beyin_v3.py'),'--vault',str(self.h.vault),'--state',str(self.h.state),
            'workspace-builder',action,'--file',file],input=raw if raw is not None else json.dumps(payload),capture_output=True,text=True,timeout=40)
        return result,json.loads(result.stdout)

    def test_plan_preview_stdin_are_read_only_without_index(self):
        before={str(p):p.read_bytes() for p in self.h.vault.rglob('*') if p.is_file()}
        process,result=self.call('plan',{'spec':self.h.spec,'registry_id':'fixture'})
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(result['capabilities']['pdf_text'],'ready')
        self.assertFalse(self.h.state.exists())
        process,preview=self.call('preview',{'plan':result['plan']})
        self.assertEqual(process.returncode,0);self.assertEqual(preview,result['preview'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.h.vault.rglob('*') if p.is_file()})
        self.assertFalse(self.h.state.exists())

    def test_gitless_installed_builder_cli_plan_uses_managed_modules(self):
        from v3_package_helpers import build_package,isolated_env,run_python
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);env=isolated_env(root/'home');package=root/'package'
            archive=build_package(root/'release.zip','3.10.1',env)
            with zipfile.ZipFile(archive) as release:release.extractall(package)
            installed=run_python(package/'scripts/install_v3.py',['--vault',self.h.vault,'--state',self.h.state],root,env)
            self.assertEqual(installed.returncode,0,installed.stderr.decode(errors='replace'))
            before={str(p):p.read_bytes() for parent in (self.h.vault,self.h.state) for p in parent.rglob('*') if p.is_file()}
            cli=self.h.vault/'beyin.py';spec=copy.deepcopy(self.h.spec)
            spec['data']['unresolved']=['synthetic_unresolved']
            payload=json.dumps({'spec':spec,'registry_id':'fixture'})
            command=[sys.executable,'-I','-B',str(cli),'workspace-builder','plan','--file','-']
            process=subprocess.run(command+['--json'],input=payload,cwd=root,env=env,capture_output=True,text=True,timeout=40)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout);self.assertEqual(result['capabilities']['pdf_text'],'ready')
            human=subprocess.run(command+['--human'],input=payload,cwd=root,env=env,capture_output=True,text=True,timeout=40)
            self.assertEqual(human.returncode,0,human.stderr)
            self.assertIn('synthetic_unresolved',human.stdout)
            self.assertIn('Önce tamamlanması gerekenler',human.stdout)
            self.assertNotIn('Islem sonucu: tamamlandi',human.stdout)
            preview=subprocess.run([sys.executable,'-I','-B',str(cli),'workspace-builder','preview','--file','-','--human'],
                input=json.dumps({'plan':result['plan']}),cwd=root,env=env,capture_output=True,text=True,timeout=40)
            self.assertEqual(preview.returncode,0,preview.stderr);self.assertEqual(preview.stdout,human.stdout)
            self.assertFalse((package/'.git').exists())
            self.assertEqual(before,{str(p):p.read_bytes() for parent in (self.h.vault,self.h.state) for p in parent.rglob('*') if p.is_file()})

    def test_malformed_json_and_missing_file_are_safe_typed_stdout(self):
        for kwargs in ({'raw':'{"secret":"PRIVATE_RAW_SOURCE'}, {'raw':'{"key":1,"key":2}'}, {'raw':'{"value":NaN}'}, {'file':str(self.h.vault/'PRIVATE_MISSING_PATH.json')}):
            process,result=self.call('plan',**kwargs)
            self.assertIn(process.returncode,(2,5));validate_document(result)
            self.assertNotIn('PRIVATE',process.stdout+process.stderr)
            self.assertFalse(self.h.state.exists())

    def test_apply_replay_and_revert_preserve_pending_health_and_exact_authority(self):
        payload={'plan':self.h.plan,'idempotency_key':'cli-exact'}
        process,error=self.call('apply',payload);self.assertEqual(process.returncode,4)
        self.h.approve()
        process,result=self.call('apply',payload)
        self.assertEqual(process.returncode,5);self.assertEqual(result['data']['workspace_health'],'verification_pending')
        process,result=self.call('apply',payload);self.assertEqual(process.returncode,5);self.assertEqual(result['data']['operation_status'],'replayed')
        self.assertFalse((self.h.state/'memory.sqlite3').exists())
        process,result=self.call('revert',{'idempotency_key':'cli-exact'});self.assertEqual(process.returncode,4)
        self.h.auth.approve_revert(self.h.plan,evidence='Exact synthetic CLI undo')
        process,result=self.call('revert',{'idempotency_key':'cli-exact'});self.assertEqual(process.returncode,0)
        self.assertFalse((self.h.state/'memory.sqlite3').exists())


if __name__=='__main__':unittest.main()
