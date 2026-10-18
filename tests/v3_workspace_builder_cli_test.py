"""Real early CLI dispatch, without a hidden MemoryStore or unsafe error dump."""
import json
import copy
import os
from pathlib import Path
import sys
import subprocess
import unittest
import tempfile
import zipfile

import v3_workspace_builder_c1_test as c1
from beyin_v3_parser_assets import managed_files
from beyin_v3_workspaces import validate_document
from v3_workspace_test_io import run_json_cli

ROOT=Path(__file__).resolve().parents[1]


class BuilderCliTest(unittest.TestCase):
    def setUp(self):
        self.h=c1.RealC1BuilderTest();self.h.setUp();self.addCleanup(self.h.tmp.cleanup)
        for name,raw in managed_files().items():
            path=self.h.vault/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)

    def call(self,action,payload=None,*,raw=None,file='-'):
        result=run_json_cli([sys.executable,'-B',str(ROOT/'scripts/beyin_v3.py'),'--vault',str(self.h.vault),'--state',str(self.h.state),
            'workspace-builder',action,'--file',file],input=raw if raw is not None else json.dumps(payload, ensure_ascii=False),capture_output=True,timeout=40)
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

    def test_direct_machine_cli_ignores_utf16_console_encoding(self):
        command = [sys.executable, '-B', str(ROOT/'scripts/beyin_v3.py'),
                   '--vault', str(self.h.vault), '--state', str(self.h.state),
                   'workspace-builder', 'plan', '--file', '-']
        # Do not use -I/-E: they would ignore PYTHONIOENCODING and mask this bug.
        env = dict(os.environ, PYTHONIOENCODING='utf-16')
        for payload in (json.dumps({'spec': self.h.spec, 'registry_id': 'fixture'}, ensure_ascii=False), '{'):
            with self.subTest(valid=payload != '{'):
                process = subprocess.run(command, input=payload.encode('utf-8'),
                                         capture_output=True, env=env, timeout=40)
                result = json.loads(process.stdout.decode('utf-8'))
                self.assertNotIn(b'\x00', process.stdout)
                if payload == '{':
                    self.assertEqual(process.returncode, 2)
                    validate_document(result)
                else:
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual(result['capabilities']['pdf_text'], 'ready')

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
            payload=json.dumps({'spec':spec,'registry_id':'fixture'}, ensure_ascii=False)
            command=[sys.executable,'-I','-B',str(cli),'workspace-builder','plan','--file','-']
            process=run_json_cli(command+['--json'],input=payload,cwd=root,env=env,capture_output=True,timeout=40)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout);self.assertEqual(result['capabilities']['pdf_text'],'ready')
            # Exercise the installed product, with environment settings enabled.
            utf16_env = dict(env, PYTHONIOENCODING='utf-16')
            machine = subprocess.run([sys.executable, '-B', str(cli), 'workspace-builder', 'plan',
                                      '--file', '-', '--json'], input=payload.encode('utf-8'),
                                     cwd=root, env=utf16_env, capture_output=True, timeout=40)
            self.assertEqual(machine.returncode, 0, machine.stderr)
            self.assertEqual(json.loads(machine.stdout.decode('utf-8')), result)
            self.assertNotIn(b'\x00', machine.stdout)
            # Broken bootstrap/config must still report exactly one JSON error
            # when the shared output helper is absent from a partial install.
            helper = self.h.vault/'.claude/scripts/beyin_v3_output.py'
            self.assertTrue(helper.is_file())
            helper_bytes = helper.read_bytes()
            runtime_config = self.h.vault/'.beyin-runtime.json'
            config_bytes = runtime_config.read_bytes()
            try:
                helper.unlink()
                failed_command = subprocess.run([sys.executable, '-B', str(cli), 'workspace-builder', 'plan',
                    '--file', '-', '--json'], input=b'{', cwd=root, env=utf16_env, capture_output=True, timeout=40)
                self.assertEqual(failed_command.returncode, 2)
                validate_document(json.loads(failed_command.stdout.decode('utf-8')))
                self.assertEqual(failed_command.stderr, b'')
                runtime_config.write_bytes(b'{')
                failed = subprocess.run([sys.executable, '-B', str(cli), 'doctor', '--json'],
                    cwd=root, env=utf16_env, capture_output=True, timeout=40)
                self.assertEqual(failed.returncode, 1)
                self.assertEqual(failed.stdout, b'')
                error = json.loads(failed.stderr.decode('utf-8'))
                self.assertEqual(error['error'], 'JSONDecodeError')
                self.assertNotIn(b'Traceback', failed.stderr)
            finally:
                helper.write_bytes(helper_bytes)
                runtime_config.write_bytes(config_bytes)
            # --human is a separate text interface, not the machine JSON helper.
            # Explicitly redirected human output must be lossless even if the
            # parent process runs under a Windows legacy console code page.
            legacy_env = dict(env, PYTHONIOENCODING='cp1252:strict')
            human=subprocess.run(command+['--human'], input=payload.encode('utf-8'),
                cwd=root,env=legacy_env,capture_output=True,timeout=40)
            self.assertEqual(human.returncode,0,human.stderr)
            human_text=human.stdout.decode('utf-8')
            self.assertIn('synthetic_unresolved',human_text)
            self.assertIn('Önce tamamlanması gerekenler',human_text)
            self.assertNotIn('Islem sonucu: tamamlandi',human_text)
            preview=subprocess.run([sys.executable,'-I','-B',str(cli),'workspace-builder','preview','--file','-','--human'],
                input=json.dumps({'plan':result['plan']},ensure_ascii=False).encode('utf-8'),
                cwd=root,env=legacy_env,capture_output=True,timeout=40)
            self.assertEqual(preview.returncode,0,preview.stderr)
            self.assertEqual(preview.stdout, human.stdout)
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
