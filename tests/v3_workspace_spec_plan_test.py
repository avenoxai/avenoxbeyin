"""Pure G0 plan adapter tests. Mock validator is not C1 or I1 acceptance.

Full JSON Schema/golden comparison is run separately against the G0 deliverable.
No actual filesystem writer, permission grant or runtime context is used here.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
from beyin_v3_workspace_plan import (VERSION, _document_bytes, _envelope, canonical_bytes,
                                     content_hash, plan_workspace, preview_workspace)


def snapshots(files):
    inventory, profiles, sources, bindings = [], [], [], []
    for (root, name), raw in sorted(files.items()):
        if root == 'vault' and name.startswith('.beyin-workspaces/'):
            inventory.append({'relative_path': name, 'content_sha256': content_hash(raw)})
            doc = json.loads(raw)
            if doc['type'] == 'WorkspaceProfile':
                profiles.append(doc['data'])
            if doc['type'] == 'SourceDescriptor':
                sources.append(doc['data'])
        if root == 'state' and name.startswith('workspace-bindings/'):
            doc = json.loads(raw)
            bindings.append({'source_id': doc['data']['source_id'], 'revision': doc['data']['revision'],
                             'content_sha256': content_hash(canonical_bytes(doc))})
    registry = _envelope('RegistrySnapshot', {'registry_id': 'fixture',
        'snapshot_sha256': content_hash(canonical_bytes(inventory)), 'files': inventory,
        'profiles': profiles, 'sources': sources})
    return registry, content_hash(canonical_bytes(bindings))


def fixture(identity='custom'):
    profile = {'id': identity, 'revision': 1, 'name': 'Kullanıcı alanı ' + identity,
        'purpose': 'Kaynak, iş, sonuç ve öğrenim birlikte.', 'lifecycle': 'active',
        'source_ids': ['material'], 'note_roots': ['notes', 'knowledge'], 'working_rules': [],
        'memory_policy': {'prefer_update_existing': True, 'new_note': 'distinct_only', 'automatic_learning': 'enabled'},
        'access_policy': {'cross_workspace': 'explicit_grant', 'external_sources': 'read_only', 'automatic_audience': 'internal'},
        'record_types': [], 'relation_types': [], 'workflows': [], 'extensions': {}}
    spec = _envelope('WorkspaceSpec', {'spec_id': identity + '-setup', 'user_goal': profile['purpose'],
        'desired_profile': profile, 'sources': [{'id': 'material', 'kind': 'directory', 'label': 'Sentetik kaynak'}],
        'binding_requests': [{'source_id': 'material', 'machine_id': 'fixture-machine', 'revision': 1,
            'locator': '/synthetic/source', 'grants': [{'workspace_id': identity, 'access': 'read'}],
            'authority_ref': 'user-request'}], 'adopt_record_refs': [],
        'required_capabilities': ['pdf_text', 'docx_text'], 'unresolved': [], 'authority_ref': 'user-request'})
    files = {('vault', '.beyin-workspaces/format.json'): _document_bytes(_envelope('RegistryFormat',
             {'registry_id': 'fixture', 'layout': 'per_workspace_json'})),
             ('vault', 'Core.md'): b'UNCHANGED_USER_IDENTITY', ('vault', 'notes/user.md'): b'user prose'}
    return spec, files


class SpecPlanTest(unittest.TestCase):
    def setUp(self):
        self.validated = []

    def validate(self, document):
        # Development boundary double: production must inject the C1 validator.
        self.assertEqual(document['contract_version'], VERSION)
        self.validated.append(document['type'])

    def plan(self, spec, files, **kwargs):
        registry, binding_hash = snapshots(files)
        return plan_workspace(spec, registry, binding_hash, files, validate_document=self.validate,
                              capabilities=kwargs.pop('capabilities', {'pdf_text': 'ready', 'docx_text': 'ready'}), **kwargs)

    def published(self, plan, files):
        result = copy.deepcopy(files)
        for operation in plan['data']['operations']:
            target = operation['target']
            result[(target['root'], target['relative_path'])] = operation['content_utf8'].encode('utf-8')
        return result

    def test_one_generic_engine_deterministic_pure_and_same_profile(self):
        for identity in ('academic', 'career', 'project', 'unseen-ceramics'):
            spec, files = fixture(identity)
            original = copy.deepcopy((spec, files))
            with patch('builtins.open', side_effect=AssertionError('planner IO')):
                first = self.plan(spec, files)
                second = self.plan(copy.deepcopy(spec), dict(reversed(list(files.items()))))
            self.assertEqual(first, second)
            self.assertEqual((spec, files), original)
            operations = first['data']['operations']
            self.assertEqual(json.loads(operations[-1]['content_utf8'])['data'], spec['data']['desired_profile'])
            self.assertEqual(operations[-1]['depends_on'], [op['id'] for op in operations[:-1]])
            self.assertFalse(first['data']['unresolved'])
            self.assertNotIn('Core.md', str(first))
            self.assertNotIn('notes/user.md', str(first))
            self.assertEqual(operations[1]['ownership'], 'binding')
            self.assertEqual(operations[1]['target']['root'], 'state')
        self.assertIn('WorkspaceSpec', self.validated)
        self.assertIn('ProvisionPlan', self.validated)

    def test_noop_preserves_config_formatting_and_revision(self):
        spec, files = fixture()
        published = self.published(self.plan(spec, files), files)
        key = ('vault', '.beyin-workspaces/workspaces/custom.json')
        published[key] = json.dumps(json.loads(published[key]), ensure_ascii=False).encode()
        again = self.plan(spec, published)
        self.assertEqual(again['data']['operations'], [])

    def test_long_spec_identity_is_bounded_and_distinct(self):
        spec, files = fixture()
        spec['data']['spec_id'] = 'a' * 96
        first = self.plan(spec, files)
        self.assertLessEqual(len(first['data']['plan_id']), 96)
        spec['data']['spec_id'] = 'a' * 95 + 'b'
        self.assertNotEqual(first['data']['plan_id'], self.plan(spec, files)['data']['plan_id'])

    def test_disconnect_revokes_old_membership_without_leaving_own_grants(self):
        spec, files = fixture()
        published = self.published(self.plan(spec, files), files)
        spec['data']['desired_profile'].update(revision=2, source_ids=[])
        spec['data']['binding_requests'][0]['revision'] = 2
        with self.assertRaisesRegex(ValueError, 'disconnected_source_grant'):
            self.plan(spec, published)
        spec['data']['binding_requests'][0]['grants'] = []
        plan = self.plan(spec, published)
        self.assertEqual({op['ownership'] for op in plan['data']['operations']}, {'binding', 'registry'})
        self.assertFalse(plan['data']['unresolved'])

    def test_revoked_or_absent_read_binding_stays_unresolved_and_visible(self):
        spec, files = fixture()
        published = self.published(self.plan(spec, files), files)
        spec['data']['binding_requests'][0]['grants'] = []
        spec['data']['binding_requests'][0]['revision'] = 2
        plan = self.plan(spec, published)
        self.assertIn('missing_read_binding:material', plan['data']['unresolved'])
        text = preview_workspace(plan, published, validate_document=self.validate)['text']
        self.assertIn('hiçbir alana yetki verilmez', text)
        self.assertIn('Kaldırılan erişim: custom - okuma', text)
        self.assertNotIn('hafızası yazımı', text)
        spec['data']['binding_requests'] = []
        self.assertIn('missing_read_binding:material', self.plan(spec, files)['data']['unresolved'])

    def test_existing_workspace_is_updated_in_place_with_real_before_hash(self):
        spec, files = fixture()
        published = self.published(self.plan(spec, files), files)
        spec['data']['desired_profile'].update(name='Yeni ad', revision=2)
        changed = self.plan(spec, published)['data']['operations']
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['kind'], 'update')
        self.assertEqual(changed[0]['before_sha256'], content_hash(published[('vault', changed[0]['target']['relative_path'])]))
        spec['data']['desired_profile']['revision'] = 3
        with self.assertRaisesRegex(ValueError, 'profile_revision_conflict'):
            self.plan(spec, published)

    def test_changed_registry_binding_or_projection_rejected(self):
        spec, files = fixture()
        registry, bindings = snapshots(files)
        changed = copy.deepcopy(files)
        changed[('vault', '.beyin-workspaces/format.json')] += b' '
        with self.assertRaisesRegex(ValueError, 'registry_snapshot_mismatch'):
            plan_workspace(spec, registry, bindings, changed, validate_document=self.validate)
        with self.assertRaisesRegex(ValueError, 'binding_snapshot_mismatch'):
            plan_workspace(spec, registry, '0' * 64, files, validate_document=self.validate)
        registry['data']['profiles'] = [spec['data']['desired_profile']]
        with self.assertRaisesRegex(ValueError, 'profile_projection_mismatch'):
            plan_workspace(spec, registry, bindings, files, validate_document=self.validate)

    def test_missing_capability_and_known_unresolved_are_visible(self):
        spec, files = fixture()
        spec['data']['unresolved'] = ['missing_source_location']
        result = self.plan(spec, files, capabilities={'pdf_text': 'partial'})
        self.assertEqual(result['data']['unresolved'], ['missing_source_location',
                         'missing_capability:pdf_text', 'missing_capability:docx_text'])

    def test_validator_is_required_and_its_rejection_propagates(self):
        spec, files = fixture()
        registry, bindings = snapshots(files)
        with self.assertRaisesRegex(ValueError, 'contract_validator_required'):
            plan_workspace(spec, registry, bindings, files)
        def reject(document):
            raise ValueError('invalid_schema_from_C1')
        with self.assertRaisesRegex(ValueError, 'invalid_schema_from_C1'):
            plan_workspace(spec, registry, bindings, files, validate_document=reject)
        spec['contract_version'] = 'workspace/999'
        with self.assertRaisesRegex(ValueError, 'unsupported_contract'):
            plan_workspace(spec, registry, bindings, files, validate_document=self.validate)

    def test_external_write_and_unrelated_grant_rejected(self):
        spec, files = fixture()
        spec['data']['binding_requests'][0]['grants'][0]['access'] = 'write_memory'
        with self.assertRaisesRegex(ValueError, 'external_source_write_denied'):
            self.plan(spec, files)
        spec['data']['binding_requests'][0]['grants'] = [{'workspace_id': 'other', 'access': 'read'}]
        with self.assertRaisesRegex(ValueError, 'unrelated_workspace_grant'):
            self.plan(spec, files)

    def test_unrelated_existing_grants_cannot_be_removed(self):
        spec, files = fixture()
        binding = copy.deepcopy(spec['data']['binding_requests'][0])
        binding['grants'].append({'workspace_id': 'other', 'access': 'read'})
        files[('state', 'workspace-bindings/material.json')] = _document_bytes(_envelope('SourceBinding', binding))
        spec['data']['binding_requests'][0]['revision'] = 2
        with self.assertRaisesRegex(ValueError, 'unrelated_workspace_grant'):
            self.plan(spec, files)

    def test_shared_descriptor_change_missing_and_duplicate_sources(self):
        spec, files = fixture()
        published = self.published(self.plan(spec, files), files)
        spec['data']['sources'][0]['label'] = 'Conflicting shared descriptor'
        with self.assertRaisesRegex(ValueError, 'shared_source_change_requires_coordinated_plan'):
            self.plan(spec, published)
        spec, files = fixture()
        spec['data']['sources'].append(copy.deepcopy(spec['data']['sources'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_source'):
            self.plan(spec, files)
        spec['data']['sources'] = []
        with self.assertRaisesRegex(ValueError, 'source_descriptor_missing'):
            self.plan(spec, files)

    def test_new_registry_has_typed_format_operation(self):
        spec, files = fixture()
        del files[('vault', '.beyin-workspaces/format.json')]
        plan = self.plan(spec, files)
        operation = plan['data']['operations'][0]
        self.assertEqual(operation['id'], 'create-format')
        self.assertIsNone(operation['before_sha256'])
        self.assertEqual(json.loads(operation['content_utf8'])['type'], 'RegistryFormat')

    def test_g0_four_golden_hashes_and_exact_profile_bytes(self):
        root = Path(__file__).resolve().parent / 'fixtures/v3/workspaces/v0.1.0'
        for case in ('academic', 'custom', 'project', 'career'):
            inp = json.loads((root / case / 'input.json').read_text(encoding='utf-8'))
            docs = {document['type']: document for document in inp['documents']}
            files = {('vault', '.beyin-workspaces/format.json'): _document_bytes(docs['RegistryFormat'])}
            plan = plan_workspace(docs['WorkspaceSpec'], docs['RegistrySnapshot'], content_hash(b'[]'), files,
                validate_document=self.validate,
                capabilities={capability: 'ready' for capability in docs['WorkspaceSpec']['data']['required_capabilities']})
            out = json.loads((root / case / 'golden.json').read_text(encoding='utf-8'))
            golden = next(document for document in out['documents'] if document['type'] == 'ProvisionPlan')
            self.assertEqual(plan['data']['plan_sha256'], golden['data']['plan_sha256'])
            self.assertEqual(plan['data']['operations'], golden['data']['operations'])

    def test_short_preview_is_bound_to_actual_diff_and_hides_opaque_authority(self):
        spec, files = fixture()
        plan = self.plan(spec, files)
        preview = preview_workspace(plan, files, validate_document=self.validate)
        self.assertIn('Kullanıcı alanı custom', preview['text'])
        self.assertIn('Kaynak: /synthetic/source', preview['text'])
        self.assertIn('Erişim: custom - okuma', preview['text'])
        self.assertNotIn('user-request', preview['text'])
        self.assertIn('yeni istemci oturumu ayrıca', preview['text'])
        self.assertEqual(preview['plan_sha256'], plan['data']['plan_sha256'])
        changed = copy.deepcopy(files)
        changed[('state', 'workspace-bindings/material.json')] = b'user edit'
        with self.assertRaisesRegex(ValueError, 'preview_precondition_changed'):
            preview_workspace(plan, changed, validate_document=self.validate)


if __name__ == '__main__':
    unittest.main()
