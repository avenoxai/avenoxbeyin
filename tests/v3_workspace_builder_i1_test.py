"""Early real D->C integration with text sources; PDF/DOCX/live acceptance is separate."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_workspaces import Registry, LocalAuthority, envelope, validate_document
from beyin_v3_workspace_plan import plan_workspace
from beyin_v3_provision import Provisioner
from beyin_v3_workspace_context import membership_hash, WorkspaceRuntime
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_sync import SyncEngine
from v3_workspace_test_io import load_utf8_json

FIX = ROOT / 'tests/fixtures/v3/workspaces/v0.1.0'


def validate(document):
    validate_document(document)


class BuilderI1Test(unittest.TestCase):
    def test_d_provisions_c_context_updates_existing_and_fresh_process_retrieves(self):
        for case in ('academic', 'custom'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); vault = base / 'Vault'; state = base / 'State'
                shutil.copytree(FIX / case / 'files/vault', vault)
                (vault / 'Core.md').write_bytes(b'PRESERVE SYNTHETIC IDENTITY')
                docs = {d['type']: copy.deepcopy(d) for d in load_utf8_json(FIX / case / 'input.json')['documents']}
                spec = docs['WorkspaceSpec']; data = spec['data']; reference = case + '-user'
                # Deliberately narrow the synthetic early I1 request to the actual
                # text-source capability. Never claim mandatory PDF/DOCX final acceptance.
                data['required_capabilities'] = ['text', 'markdown', 'workflow']
                data['authority_ref'] = reference
                permissions = []
                for binding, suffix in zip(data['binding_requests'], ('vault', 'source-a', 'source-b')):
                    locator = vault if suffix == 'vault' else base / suffix
                    if suffix != 'vault': shutil.copytree(FIX / case / 'files' / suffix, locator)
                    binding.update(locator=str(locator), authority_ref=reference)
                    permissions.extend(dict(g, source_id=binding['source_id'], locator=str(locator)) for g in binding['grants'])
                registry = Registry(vault, state); auth = LocalAuthority(registry)
                snapshot = registry.snapshot(registry_id='fixture')
                _, binding_hash = registry.bindings(snapshot)
                plan = plan_workspace(spec, envelope('RegistrySnapshot', snapshot), binding_hash,
                    registry.capture_files(), validate_document=validate,
                    capabilities={name: 'ready' for name in data['required_capabilities']})
                self.assertFalse(state.exists())
                auth.record_approval(reference, machine_id='fixture-machine', permissions=permissions,
                    evidence='Synthetic user authorizes only these source roots and accesses')
                auth.approve_plan(plan, evidence='Synthetic user consents to exact D preview')
                result = Provisioner(vault, state, validate_document=validate,
                    check_authority=auth.check_authority, check_snapshot=auth.check_snapshot).apply(plan, case)
                self.assertEqual(result['data']['workspace_health'], 'verification_pending')
                self.assertEqual(registry.snapshot()['profiles'][0], data['desired_profile'])
                SyncEngine(vault, state).sync()
                runtime = WorkspaceRuntime(vault, state); runtime.adopt(spec, membership_hash(runtime.registry)); runtime.activate()
                request = docs['RequestContext']; request['data']['grant_ref'] = reference
                context = runtime.context(request)['data']
                ids = {r['id'] for r in context['records']}
                self.assertTrue({case + '-topic', case + '-task', case + '-result', case + '-learning'} <= ids)
                self.assertTrue(context['source_refs']); self.assertTrue(context['relations'])
                intent = copy.deepcopy(next(d for d in load_utf8_json(FIX / case / 'golden.json')['documents']
                    if d['type'] == 'MemoryWriteIntent'))
                intent['data'].update(context_token=context['context_token'], binding_snapshot_sha256=context['binding_snapshot_sha256'])
                records = {r['id']: r for r in context['records']}
                for change in intent['data']['changes']:
                    record = records[change['target']['record_id']]
                    change['target'].update(expected_revision=record['revision'], expected_source_sha256=record['source_ref']['content_sha256'])
                    change['evidence_refs'] = [records[case + '-result']['source_ref']]
                    for relation in change['relation_changes']: relation['evidence_refs'] = list(change['evidence_refs'])
                before = set(vault.rglob('*.md'))
                service = MemoryIntentService(runtime)
                observed = service.apply(intent)['data']
                self.assertTrue(all(p['state'] == 'verified' for p in observed['parts']))
                self.assertEqual(service.apply(intent)['data']['operation_status'], 'replayed')
                self.assertEqual(set(vault.rglob('*.md')), before)
                requestfile = base / 'request.json'; requestfile.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
                code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                        'from beyin_v3_workspace_context import WorkspaceRuntime;'
                        'print(json.dumps(WorkspaceRuntime(sys.argv[2],sys.argv[3]).context(json.load(open(sys.argv[4],encoding="utf-8")))))')
                fresh = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'template/.claude/scripts'),
                    str(vault), str(state), str(requestfile)], capture_output=True, timeout=30, check=True)
                found = json.loads(fresh.stdout)['data']['records']
                learning = next(r for r in found if r['id'] == case + '-learning')
                self.assertEqual(learning['revision'], 2)
                expected_body = next(c['body'] for c in intent['data']['changes'] if c['target']['record_id'] == case + '-learning')
                self.assertIn(expected_body, learning['text'])
                self.assertEqual((vault / 'Core.md').read_bytes(), b'PRESERVE SYNTHETIC IDENTITY')
                auth.revoke(reference)
                denied = WorkspaceRuntime(vault, state).context(request)['data']
                self.assertEqual(denied['state'], 'denied'); self.assertFalse(denied['records'])
