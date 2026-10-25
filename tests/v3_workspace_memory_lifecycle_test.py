"""#454 scenarios adapted to the existing workspace/source lifecycle.

Receipts and candidate ledgers are explicitly not evidence of synthesized knowledge.
All durable claims below are authored in source Markdown before they are retrieved.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_workspace_helpers import ROOT, install_fixture
from v3_workspace_test_io import run_json_cli
from beyin_v3_sync import SyncEngine, render
from beyin_v3_workspace_context import WorkspaceRuntime, request_scope
import beyin_v3_learning as learning
from beyin_v3_workspaces import envelope, content_hash


class WorkspaceMemoryLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.runtime, self.spec, self.request = install_fixture(self.base, 'academic')
        self.vault, self.state = self.runtime.registry.vault, self.runtime.registry.state
        self.engine = SyncEngine(self.vault, self.state)
        # A human-authored project rule is already adopted before runtime activation.
        rule = self.vault / 'projects/academic/Kurallar.md'
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(render(dict(id='lifecycle-rule', kind='fact', revision=1, visibility='internal', workspace_id='academic'), '# Proje kuralı\nHenüz karar verilmedi.'), encoding='utf-8')
        self.engine.sync()
        # Synthetic user setup explicitly authorizes this project rule directory.
        profile = copy.deepcopy(self.spec['desired_profile'])
        profile['revision'] += 1
        profile['note_roots'].append('projects/academic')
        profile_path = self.vault / '.beyin-workspaces/workspaces/academic.json'
        self.runtime.registry.compare_and_save(envelope('WorkspaceProfile', profile), content_hash(profile_path.read_bytes()))
        self.runtime.activate()

    def source(self, source, id, text, **metadata):
        path = self.vault / source; path.parent.mkdir(parents=True, exist_ok=True)
        values = dict(id=id, kind='fact', revision=1, visibility='internal', workspace_id='academic', **metadata)
        with request_scope('academic', 'academic-user'):
            if source.startswith(('notes/', 'knowledge/')):
                self.engine.note_create(source, text, values)
            else:
                current = self.engine.note_read(source)
                self.engine.note_edit(dict(source=source, expected_sha256=current['sha256'], edits=[dict(old='Henüz karar verilmedi.', new=text)]))
        return path

    def session(self, number, ids, query='Kaynak karar düzeltme kural görev'):
        request = copy.deepcopy(self.request)
        request['data'].update(session_id='lifecycle-session-' + str(number), query=query)
        request['data']['signals'][0]['subject_refs'] = [dict(workspace_id='academic', record_id=id) for id in ids]
        return request

    def test_five_source_sessions_and_sixth_real_process_recall(self):
        # Session 1: user source exists independently of all outcome receipts.
        self.source('notes/lifecycle-source.md', 'lifecycle-source', '# Kaynak\nÖlçüm: örnek veriyle kilit bekleme süresi 5 saniyeydi.')
        self.assertTrue(self.runtime.context(self.session(1, ['lifecycle-source']))['data']['records'])
        # Session 2: an agent authors actual reusable, source-linked knowledge.
        concept = self.source('knowledge/concepts/lifecycle-decision.md', 'lifecycle-decision', '# Karar\nKilit bekleme sınırı 5 saniye.\nKaynak: [[notes/lifecycle-source]].')
        self.assertTrue(self.runtime.context(self.session(2, ['lifecycle-decision']))['data']['records'])
        # Session 3: explicit correction updates the canonical note; old value stays history.
        with request_scope('academic', 'academic-user'):
            before = self.engine.note_read('knowledge/concepts/lifecycle-decision.md')
            self.engine.note_edit(dict(source=before['source'], expected_sha256=before['sha256'], edits=[dict(old='Kilit bekleme sınırı 5 saniye.', new='Düzeltme: Windows ağ paylaşımında kilit bekleme sınırı 15 saniye; önceki 5 saniye kararı geçersiz.\nGerekçe: kullanıcı düzeltmesi, [[notes/lifecycle-source]].')]))
        corrected = self.runtime.context(self.session(3, ['lifecycle-decision']))['data']['records']
        self.assertTrue(any('15 saniye' in r['text'] for r in corrected))
        # Session 4: a project rule has explicit scope and a source link.
        self.source('projects/academic/Kurallar.md', 'lifecycle-rule', '# Proje kuralı\n[kapsam: academic] Yalnız ölçüm kaynağıyla desteklenen süreyi kullan.\nKaynak: [[knowledge/concepts/lifecycle-decision]].')
        self.assertTrue(self.runtime.context(self.session(4, ['lifecycle-rule']))['data']['records'])
        # Session 5: a strict task is created/updated using the existing canonical task API.
        with request_scope('academic', 'academic-user'):
            created = self.engine.task_create('tasks/lifecycle-task.md', 'Düzeltilmiş süreyi belgede doğrula.', dict(id='lifecycle-task', kind='task', status='active', owner='fixture-user', visibility='internal', workspace_id='academic', completion_contract='strict', completion_criterion='Düzeltilmiş süre kaynak notunda mevcut.'))
            done = self.engine.update_task('lifecycle-task', created['revision'], dict(status='done', evidence_refs=['knowledge/concepts/lifecycle-decision.md']))
        self.assertEqual(done['status'], 'done')
        self.assertTrue(self.runtime.context(self.session(5, ['lifecycle-task']))['data']['records'])
        # Receipt-only candidate is not promoted to distilled, and cannot create a concept.
        learning.stage_candidate(self.state, 'receipt-only-session', 'Receipt-only claim is not synthesized knowledge', source='notes/lifecycle-source.md', scope='academic')
        concept_before = concept.read_bytes()
        receipt = self.engine.receipt('lifecycle-receipt-only', 'İşlem iddiası.\nÖğrenilen: Receipt-only claim is not synthesized knowledge', ['notes/lifecycle-source.md'], 'codex', session='receipt-only-session')
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertTrue(learning.list_candidates(self.state, session='receipt-only-session', status='candidate'))
        self.assertFalse(learning.list_candidates(self.state, session='receipt-only-session', status='distilled'))
        self.assertEqual(concept.read_bytes(), concept_before)
        # Session 6 is an actual new interpreter/store, not a re-used in-memory object.
        ids = ['lifecycle-source', 'lifecycle-decision', 'lifecycle-rule', 'lifecycle-task']
        request = self.session(6, [], 'Kilit bekleme ölçüm Windows ağ paylaşımında düzeltme süre kaynak belgede doğrula'); request_path = self.base / 'sixth.json'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        code = 'import json,sys;sys.path.insert(0,sys.argv[1]);from beyin_v3_workspace_context import WorkspaceRuntime;request=json.load(open(sys.argv[4],encoding="utf-8"));sys.stdout.buffer.write(json.dumps(WorkspaceRuntime(sys.argv[2],sys.argv[3]).context(request),ensure_ascii=False).encode("utf-8"))'
        process = run_json_cli([sys.executable, '-B', '-c', code, str(ROOT/'template/.claude/scripts'), str(self.vault), str(self.state), str(request_path)], capture_output=True, check=True)
        records = {r['id']:r for r in json.loads(process.stdout)['data']['records']}
        self.assertTrue(set(ids) <= records.keys())
        self.assertIn('15 saniye', records['lifecycle-decision']['text'])
        self.assertNotIn('Kilit bekleme sınırı 5 saniye.', records['lifecycle-decision']['text'])
        self.assertEqual(records['lifecycle-task']['status'], 'done')
        for id in ids:
            self.assertTrue(records[id]['source_ref']['content_sha256'])
            self.assertTrue(records[id]['source_ref']['relative_path'])
        with request_scope('academic', 'academic-user'):
            history = self.engine.store.history('lifecycle-decision')
        self.assertTrue(any('Kilit bekleme sınırı 5 saniye.' in r.get('record', {}).get('text', '') for r in history))

    def test_scope_private_trust_and_revoked_grant_in_later_session(self):
        install_fixture(self.base, 'custom')
        for id, metadata in [('private-canary', {'visibility':'private'}), ('untrusted-canary', {'trust':'untrusted'}), ('false-trust-canary', {'trusted':False}), ('other-scope-canary', {'workspace_id':'custom'})]:
            values = dict(id=id, kind='fact', revision=1, visibility='internal', workspace_id='academic'); values.update(metadata)
            path = self.vault / ('notes/' + id + '.md'); path.write_text(render(values, 'LIFECYCLE_CANARY'), encoding='utf-8')
        self.engine.sync(); self.runtime.activate()
        request = self.session(6, ['private-canary', 'untrusted-canary', 'false-trust-canary', 'other-scope-canary'], 'LIFECYCLE_CANARY')
        fresh = WorkspaceRuntime(self.vault, self.state)
        self.assertNotIn('LIFECYCLE_CANARY', json.dumps(fresh.context(request)))
        self.runtime.authority.revoke('academic-user')
        result = fresh.context(request)['data']
        self.assertEqual(result['state'], 'denied'); self.assertFalse(result['records'])


if __name__ == '__main__': unittest.main()
