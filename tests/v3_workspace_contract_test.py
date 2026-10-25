"""G0 fixture integrity and cross-interface invariants, not a workspace implementation.

Only stdlib is required. Full Draft 2020-12 validation is recorded separately in G0 evidence.
No real vault, client, model, network, extractor or application writer is used here.
"""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/fixtures/v3/workspaces/v0.1.0'
DOC = ROOT / 'docs/v3/workspaces/v0.1.0'
VERSION = 'workspace/0.1.0'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def documents(case, side):
    return {d['type']: d['data'] for d in read(FIX / case / (side+'.json'))['documents']}


class WorkspaceContractTest(unittest.TestCase):
    cases = ('academic', 'custom', 'project', 'career')

    def test_frozen_fixture_inventory_and_hashes(self):
        manifest = read(FIX / 'manifest.json')
        self.assertEqual(manifest['fixture_version'], VERSION)
        actual = {p.relative_to(FIX).as_posix(): sha(p.read_bytes())
                  for p in FIX.rglob('*') if p.is_file() and p.name != 'manifest.json'}
        self.assertEqual(actual, manifest['files'])

    def test_schema_refs_are_local_and_resolve(self):
        schema = read(DOC / 'schema.json')
        self.assertEqual(schema['$id'], 'urn:avenox:workspace:0.1.0')
        self.assertEqual(schema['properties']['contract_version']['const'], VERSION)

        def visit(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    self.assertTrue(value['$ref'].startswith('#/$defs/'))
                    self.assertIn(value['$ref'].split('/')[-1], schema['$defs'])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(schema)

    def test_builder_profile_is_exact_runtime_profile(self):
        for case in self.cases:
            with self.subTest(case=case):
                inp, out = documents(case, 'input'), documents(case, 'golden')
                profile = out['WorkspaceProfile']
                self.assertEqual(inp['WorkspaceSpec']['desired_profile'], profile)
                publish = out['ProvisionPlan']['operations'][-1]
                self.assertEqual(json.loads(publish['content_utf8'])['data'], profile)
                self.assertEqual(out['WorkContext']['profile_revision'], profile['revision'])
                self.assertEqual(out['MemoryWriteIntent']['profile_revision'], profile['revision'])

    def test_locator_only_in_local_binding_or_local_plan(self):
        for case in self.cases:
            inp, out = documents(case, 'input'), documents(case, 'golden')
            portable = [out['WorkspaceProfile'], inp['WorkspaceSpec']['sources'], out['WorkContext'], out['MemoryWriteIntent']]
            for value in portable:
                self.assertNotIn('locator', canonical(value))
                self.assertNotIn('/synthetic/', canonical(value))
            for op in out['ProvisionPlan']['operations']:
                if 'locator' in op['content_utf8']:
                    self.assertEqual(op['ownership'], 'binding')
                    self.assertEqual(op['target']['root'], 'state')

    def test_source_refs_match_real_synthetic_bytes(self):
        for case in self.cases:
            inp, out = documents(case, 'input'), documents(case, 'golden')
            fixture = read(FIX / case / 'input.json')
            remap = fixture['locator_remap']
            bindings = {b['source_id']: remap[b['locator']] for b in inp['WorkspaceSpec']['binding_requests']}
            for source in out['WorkContext']['source_refs']:
                path = FIX / case / bindings[source['source_id']] / source['relative_path']
                self.assertEqual(sha(path.read_bytes()), source['content_sha256'])
            for item in fixture['source_files']:
                self.assertEqual(sha((FIX / case / item['path']).read_bytes()), item['content_sha256'])

    def test_same_named_sources_are_distinct(self):
        refs = documents('academic', 'golden')['WorkContext']['source_refs'][-2:]
        self.assertEqual(refs[0]['relative_path'], refs[1]['relative_path'])
        self.assertNotEqual(refs[0]['source_id'], refs[1]['source_id'])
        self.assertNotEqual(refs[0]['content_sha256'], refs[1]['content_sha256'])

    def test_plan_has_concrete_content_hashes_and_stable_hash(self):
        for case in self.cases:
            inp, out = documents(case, 'input'), documents(case, 'golden')
            plan = out['ProvisionPlan']
            spec_envelope = next(d for d in read(FIX / case / 'input.json')['documents'] if d['type'] == 'WorkspaceSpec')
            self.assertEqual(plan['spec_sha256'], sha(canonical(spec_envelope)))
            self.assertEqual(plan['plan_sha256'], sha(canonical({k:v for k,v in plan.items() if k not in ('plan_id','plan_sha256')})))
            self.assertEqual(plan['snapshot_sha256'], inp['RegistrySnapshot']['snapshot_sha256'])
            self.assertEqual(plan['snapshot_sha256'], sha(canonical(inp['RegistrySnapshot']['files'])))
            targets = []
            for op in plan['operations']:
                self.assertEqual(sha(op['content_utf8']), op['after_sha256'])
                self.assertIsNone(op['before_sha256'])
                self.assertEqual(op['kind'], 'create')
                self.assertIn(op['target']['root'], ('vault','state'))
                targets.append((op['target']['root'],op['target']['relative_path']))
            self.assertEqual(len(set(targets)), len(targets))
            self.assertEqual(plan['operations'][-1]['depends_on'], [op['id'] for op in plan['operations'][:-1]])

    def test_write_intent_reuses_current_record_and_evidence(self):
        for case in self.cases:
            out = documents(case, 'golden')
            context, intent = out['WorkContext'], out['MemoryWriteIntent']
            records = {r['id']:r for r in context['records']}
            self.assertEqual(intent['context_token'], context['context_token'])
            self.assertEqual(intent['binding_snapshot_sha256'], context['binding_snapshot_sha256'])
            for change in intent['changes']:
                current = records[change['target']['record_id']]
                self.assertEqual(change['target']['expected_revision'], current['revision'])
                self.assertEqual(change['target']['expected_source_sha256'], current['source_ref']['content_sha256'])
                self.assertEqual(change['target']['source_id'], current['source_ref']['source_id'])
                self.assertTrue(change['evidence_refs'])
            self.assertEqual([c['action'] for c in intent['changes']], ['update_note','update_task'])
            self.assertEqual(intent['changes'][1]['body'], '')
            self.assertEqual(intent['changes'][1]['relation_changes'][0]['type'], 'result_of')
            self.assertEqual(intent['changes'][1]['relation_changes'][0]['to']['record_id'], case+'-task')

    def test_academic_integrates_task_result_and_unknown_weight(self):
        out = documents('academic', 'golden')
        records = out['WorkContext']['records']
        self.assertEqual({r['entity_type'] for r in records}, {'course','assignment','grade_feedback','learning'})
        self.assertEqual(records[1]['status'], 'active')
        self.assertEqual(records[1]['due_at'], '2026-10-23')
        self.assertEqual(records[2]['facts']['score'], 82)
        self.assertIsNone(records[2]['facts']['weight'])
        self.assertIn('result.weight', out['WorkContext']['missing_facts'])

    def test_workflow_same_engine_actions_and_resumable_state(self):
        topologies = []
        for case in self.cases:
            inp, out = documents(case,'input'), documents(case,'golden')
            wf = out['WorkspaceProfile']['workflows'][0]
            topologies.append([(s['id'],s['depends_on'],s['action']) for s in wf['steps']])
            seen = set()
            for step in wf['steps']:
                self.assertTrue(set(step['depends_on']) <= seen)
                self.assertNotIn(step['id'], seen)
                seen.add(step['id'])
            event, run = inp['WorkflowEvent'], out['WorkflowRun']
            self.assertEqual(wf['trigger']['entity_type'], event['record']['entity_type'])
            self.assertEqual(run['definition_sha256'], sha(canonical(wf)))
            self.assertEqual(run['run_id'], sha(canonical([case,wf['id'],wf['revision'],event['event_id']])))
            self.assertEqual(run['status'], 'awaiting_agent')
            self.assertEqual([s['state'] for s in run['steps']], ['awaiting_agent','pending','pending'])
            self.assertEqual([s['idempotency_key'] for s in run['steps']], [run['run_id']+'/'+s['id'] for s in wf['steps']])
        self.assertTrue(all(topology == topologies[0] for topology in topologies))

    def test_golden_never_claims_product_execution(self):
        for case in self.cases:
            data = read(FIX / case / 'golden.json')
            self.assertEqual(data['status'], 'golden_expectation_only')
            result = documents(case,'golden')['OperationResult']
            self.assertEqual(result['workspace_health'], 'verification_pending')
            self.assertTrue(all(c['state']=='not_tested' for c in result['capabilities']))
            self.assertTrue(all(v['state']=='pending' for v in result['verification']))
            self.assertEqual(data['acceptance']['external_source_writes'], 0)
            self.assertEqual(data['acceptance']['new_note_count'], 0)

    def test_holdout_and_boundaries_are_separate(self):
        holdout = read(FIX / 'holdout.json')
        self.assertEqual(holdout['split'], 'holdout')
        self.assertTrue(holdout['not_yet_executed'])
        custom_types = {r['entity_type'] for r in documents('custom','golden')['WorkContext']['records']}
        self.assertFalse(custom_types & set(holdout['entity_types']))
        cases = {c['id'] for c in read(FIX / 'boundary-cases.json')['cases']}
        self.assertTrue({'private','untrusted','relation','legacy','manual-edit','revocation','workflow-replay','workflow-approval'} <= cases)

    def test_boundary_inputs_include_real_canary_and_manual_edit_bytes(self):
        data = read(FIX / 'access-boundaries.json')
        records = [d['data'] for d in data['documents'] if d['type']=='Record']
        for record in records:
            source = record['source_ref']
            self.assertEqual(source['content_sha256'], sha((FIX/'boundary/files/vault'/source['relative_path']).read_bytes()))
        binding = data['documents'][0]['data']
        self.assertTrue(all(g['workspace_id']=='academic' for g in binding['grants']))
        self.assertEqual(data['workspace_members'], ['academic','other-workspace'])
        self.assertEqual(records[0]['visibility'], 'private')
        self.assertEqual(records[1]['trust'], 'untrusted')
        manual = read(FIX / 'manual-edit.json')
        self.assertNotEqual(manual['expected_source_sha256'], manual['actual_source_sha256'])
        self.assertEqual(sha((FIX/manual['preserve']).read_bytes()), manual['actual_source_sha256'])


if __name__ == '__main__':
    unittest.main()
