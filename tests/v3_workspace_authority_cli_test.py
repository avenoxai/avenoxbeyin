"""Trusted authority control is explicit, separated from apply and pending-safe."""
import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
import zipfile
from unittest.mock import patch

import v3_workspace_builder_c1_test as c1
from beyin_v3_workspaces import validate_document
from v3_workspace_test_io import run_json_cli

ROOT=Path(__file__).resolve().parents[1]


class AuthorityCliTest(unittest.TestCase):
    def setUp(self):
        self.h=c1.RealC1BuilderTest();self.h.setUp();self.addCleanup(self.h.tmp.cleanup)
        self.grant={'reference':'user-request','machine_id':'fixture-machine','permissions':[{
            'workspace_id':'custom','source_id':'material','locator':str(self.h.source),'access':'read'}],
            'audiences':['internal'],'evidence':'Explicit synthetic human consent to this exact local source.'}

    def call(self,action,payload,command='workspace-authority',raw=None):
        process=run_json_cli([sys.executable,'-B',str(ROOT/'scripts/beyin_v3.py'),'--vault',str(self.h.vault),'--state',str(self.h.state),
            command,action,'--file','-'],input=json.dumps(payload, ensure_ascii=False) if raw is None else raw,capture_output=True,timeout=30)
        result=json.loads(process.stdout);validate_document(result)
        return process,result

    def test_grant_plan_and_undo_consent_are_distinct_from_apply(self):
        apply={'plan':self.h.plan,'idempotency_key':'authority-cli'}
        process,_=self.call('apply',apply,command='workspace-builder');self.assertEqual(process.returncode,4)
        process,result=self.call('grant',self.grant);self.assertEqual(process.returncode,0);self.assertEqual(result['data']['workspace_health'],'not_tested')
        process,_=self.call('apply',apply,command='workspace-builder');self.assertEqual(process.returncode,4)
        process,_=self.call('approve-plan',{'plan':self.h.plan,'evidence':'Explicit exact synthetic plan consent'});self.assertEqual(process.returncode,0)
        process,_=self.call('apply',apply,command='workspace-builder');self.assertEqual(process.returncode,5)
        process,_=self.call('revert',{'idempotency_key':'authority-cli'},command='workspace-builder');self.assertEqual(process.returncode,4)
        process,_=self.call('approve-revert',{'plan':self.h.plan,'evidence':'Explicit exact synthetic undo consent'});self.assertEqual(process.returncode,0)
        process,_=self.call('revert',{'idempotency_key':'authority-cli'},command='workspace-builder');self.assertEqual(process.returncode,0)
        process,_=self.call('revoke',{'reference':'user-request','evidence':'User explicitly revokes this reference'});self.assertEqual(process.returncode,0)
        self.assertNotIn('user-request',self.h.auth._load()['grants'])
        self.assertFalse((self.h.state/'memory.sqlite3').exists())

    def test_invalid_or_missing_evidence_never_creates_authority(self):
        for payload in (dict(self.grant,evidence=''),dict(self.grant,approved=True),{'reference':'user-request'},dict(self.grant,evidence={'claim':'not a human signal'})):
            process,result=self.call('grant',payload);self.assertIn(process.returncode,(2,4));self.assertFalse(self.h.state.exists())
        for raw in ('{"key":1,"key":2}','{"value":NaN}','{"PRIVATE_RAW_SOURCE":',' '*(4*1024*1024+1)):
            process,result=self.call('grant',None,raw=raw);self.assertEqual(process.returncode,2)
            self.assertNotIn('PRIVATE',process.stdout+process.stderr);self.assertFalse(self.h.state.exists())

    def test_installed_entry_accepts_explicit_authority_without_note_or_index_write(self):
        from v3_package_helpers import build_package,isolated_env,run_python
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);env=isolated_env(root/'home');package=root/'package'
            archive=build_package(root/'release.zip','3.10.1',env)
            with zipfile.ZipFile(archive) as release:release.extractall(package)
            installed=run_python(package/'scripts/install_v3.py',['--vault',self.h.vault,'--state',self.h.state],root,env)
            self.assertEqual(installed.returncode,0,installed.stderr.decode(errors='replace'))
            notes={p:p.read_bytes() for p in self.h.vault.rglob('*.md')}
            indexed=(self.h.state/'memory.sqlite3').read_bytes() if (self.h.state/'memory.sqlite3').exists() else None
            process=run_json_cli([sys.executable,'-I','-B',str(self.h.vault/'beyin.py'),'workspace-authority','grant','--file','-','--json'],
                input=json.dumps(self.grant, ensure_ascii=False),cwd=root,env=env,capture_output=True,timeout=30)
            self.assertEqual(process.returncode,0,process.stderr);result=json.loads(process.stdout);validate_document(result)
            self.assertEqual(result['data']['operation_id'],'workspace-authority')
            self.assertEqual(notes,{p:p.read_bytes() for p in self.h.vault.rglob('*.md')})
            after=(self.h.state/'memory.sqlite3').read_bytes() if (self.h.state/'memory.sqlite3').exists() else None
            self.assertEqual(after,indexed)
            self.assertIn('user-request',self.h.auth._load()['grants'])

    def test_changed_plan_cannot_reuse_hash_or_implicitly_grant(self):
        self.call('grant',self.grant)
        changed=copy.deepcopy(self.h.plan);changed['data']['verify_steps'].append('fresh_session')
        process,_=self.call('approve-plan',{'plan':changed,'evidence':'Changed payload is not the original exact plan'})
        self.assertEqual(process.returncode,3);self.assertFalse(self.h.auth._load()['plans'])

    def test_pending_revoke_and_undo_remain_available(self):
        self.h.approve()
        def crash(phase,index=None):
            if phase=='after_replace' and index==0:raise RuntimeError('interrupted')
        with patch('beyin_v3_provision.transaction_hook',crash),self.assertRaisesRegex(RuntimeError,'interrupted'):
            self.h.engine.apply(self.h.plan,'pending-authority-cli')
        process,_=self.call('revoke',{'reference':'user-request','evidence':'Human revokes while pending'});self.assertEqual(process.returncode,0)
        process,_=self.call('recover',{'idempotency_key':'pending-authority-cli'},command='workspace-builder');self.assertEqual(process.returncode,4)
        process,_=self.call('approve-revert',{'plan':self.h.plan,'evidence':'Human authorizes exact pending undo'});self.assertEqual(process.returncode,0)
        process,_=self.call('grant',self.grant);self.assertEqual(process.returncode,0)
        process,_=self.call('revert',{'idempotency_key':'pending-authority-cli'},command='workspace-builder');self.assertEqual(process.returncode,0)
        self.assertFalse(self.h.registry.pending())


if __name__=='__main__':unittest.main()
