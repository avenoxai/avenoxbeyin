import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_workspace_helpers import FIX,ROOT,install_fixture
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_workspaces import WorkspaceError,content_hash,canonical_bytes
from beyin_v3_workspace_context import WorkspaceRuntime


class WorkspaceMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.runtime,self.spec,self.request=install_fixture(self.base,'academic')
        self.service=MemoryIntentService(self.runtime);self.context=self.runtime.context(self.request)['data']
        self.intent=copy.deepcopy(next(d for d in json.loads((FIX/'academic/golden.json').read_text())['documents'] if d['type']=='MemoryWriteIntent'))
        data=self.intent['data'];data.update(context_token=self.context['context_token'],binding_snapshot_sha256=self.context['binding_snapshot_sha256'])
        records={r['id']:r for r in self.context['records']}
        for change in data['changes']:
            target=change['target'];record=records[target['record_id']]
            target.update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
            change['evidence_refs']=[records['academic-result']['source_ref']]
            for relation in change['relation_changes']:relation['evidence_refs']=[records['academic-result']['source_ref']]

    def test_existing_note_task_relation_and_fresh_process_recall(self):
        vault=self.runtime.registry.vault;before=set(vault.rglob('*.md'))
        result=self.service.apply(self.intent)['data']
        self.assertTrue(all(p['state']=='verified' for p in result['parts']))
        self.assertEqual(set(vault.rglob('*.md')),before)
        fresh=WorkspaceRuntime(vault,self.runtime.registry.state).context(self.request)['data']
        learning=next(r for r in fresh['records'] if r['id']=='academic-learning')
        self.assertEqual(learning['revision'],2);self.assertIn('Yeni sonuç',learning['text'])
        task=next(r for r in fresh['records'] if r['id']=='academic-task');self.assertEqual(task['revision'],2)
        self.assertTrue(any(r['type']=='result_of' for r in fresh['relations']))
        requestfile=self.base/'request.json';requestfile.write_text(json.dumps(self.request))
        code='import json,sys;sys.path.insert(0,sys.argv[1]);from beyin_v3_workspace_context import WorkspaceRuntime;print(json.dumps(WorkspaceRuntime(sys.argv[2],sys.argv[3]).context(json.load(open(sys.argv[4])))))'
        process=subprocess.run([sys.executable,'-c',code,str(ROOT/'template/.claude/scripts'),str(vault),str(self.runtime.registry.state),str(requestfile)],capture_output=True,text=True,check=True)
        observed=json.loads(process.stdout)['data'];self.assertTrue(any(r['id']=='academic-learning' and 'Yeni sonuç' in r['text'] for r in observed['records']))

    def test_replay_and_idempotency_collision(self):
        self.service.apply(self.intent)
        self.assertEqual(self.service.apply(self.intent)['data']['operation_status'],'replayed')
        changed=copy.deepcopy(self.intent);changed['data']['changes'][0]['body']+='changed'
        with self.assertRaisesRegex(WorkspaceError,'idempotency_collision'):self.service.apply(changed)

    def test_manual_edit_before_apply_or_after_replay_is_preserved(self):
        path=self.runtime.registry.vault/'notes/academic-learning.md';path.write_text(path.read_text()+'Kullanıcının düzenlemesi.\n')
        preserved=path.read_bytes()
        with self.assertRaisesRegex(WorkspaceError,'revision_conflict'):self.service.apply(self.intent)
        self.assertEqual(path.read_bytes(),preserved)

    def test_no_memory_and_revoked_authority_write_nothing(self):
        request=copy.deepcopy(self.request);request['data']['no_memory']=True
        context=self.runtime.context(request)['data'];intent=copy.deepcopy(self.intent);intent['data']['context_token']=context['context_token']
        before={p:p.read_bytes() for p in self.runtime.registry.vault.rglob('*.md')}
        with self.assertRaisesRegex(WorkspaceError,'no_memory'):self.service.apply(intent)
        self.runtime.authority.revoke('academic-user')
        with self.assertRaisesRegex(WorkspaceError,'scope_denied'):self.service.apply(self.intent)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_crash_after_writer_before_checkpoint_resumes_without_duplication(self):
        def fail(phase,index=None):
            if phase=='writer_returned' and index==0:raise RuntimeError('synthetic crash')
        with patch('beyin_v3_workspace_memory.transaction_hook',fail),self.assertRaisesRegex(RuntimeError,'synthetic crash'):
            self.service.apply(self.intent)
        self.service.apply(self.intent)
        records=self.runtime.context(self.request)['data']['records']
        self.assertEqual(next(r for r in records if r['id']=='academic-learning')['revision'],2)
        self.assertEqual(next(r for r in records if r['id']=='academic-task')['revision'],2)


if __name__=='__main__':unittest.main()
