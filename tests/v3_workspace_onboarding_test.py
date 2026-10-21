"""Synthetic CI regressions; no native clients, network, models or real vaults."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_workspace_onboarding as onboarding
from beyin_v3_workspace_context import WorkspaceRuntime
from beyin_v3_workspace_views import initialize
from beyin_v3_workspaces import Registry, LocalAuthority, WorkspaceError, envelope, validate_document
from beyin_v3_builder import provisioner
from beyin_v3_sync import SyncEngine, parse, render


def artifacts(result):
    validate_document(result)
    return json.loads(next(i['detail'] for i in result['data']['issues'] if i['code'] == 'setup_artifacts'))


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'; self.vault.mkdir()
        self.state = self.root / 'state'
        self.external = self.root / 'external'; self.external.mkdir()
        self.payload = dict(workspace_id='fitness', name='Fitness', purpose='Antrenman kararlarını kaynaklarıyla izlemek',
            sources=[dict(path=str(self.external), name='Programlar')])

    def stage(self, payload=None):
        result, code = onboarding.execute(self.vault, self.state, 'stage', payload or self.payload)
        self.assertEqual(code, 0, result)
        staged = artifacts(result)
        saved = json.loads(Path(staged['preparation_path']).read_text(encoding='utf-8'))
        return staged, saved

    def approve(self, saved):
        """Trusted synthetic human-scope/plan approval; never invoked by product code."""
        authority = LocalAuthority(Registry(self.vault, self.state))
        for request in saved['approval_requests']:
            authority.record_approval(request['reference'], machine_id=request['machine_id'],
                permissions=request['permissions'], audiences=request['audiences'], evidence='Explicit synthetic scope consent',
                preserve_current=request['preserve_current'], expected_grant_sha256=request['expected_grant_sha256'])
        authority.approve_plan(saved['plan'], evidence='Explicit synthetic consent to this exact saved plan')

    def request(self, wid='fitness'):
        return envelope('RequestContext', dict(request_id='request-' + wid, session_id='synthetic-ci-session',
            client='synthetic-ci', query='Çalışma alanını hazırla', grant_ref='setup-review', audience='internal', no_memory=False,
            budget_chars=1000, signals=[dict(kind='explicit_user', workspace_id=wid, subject_refs=[])]))

    def complete(self, staged, wid='fitness', key='finish'):
        return onboarding.execute(self.vault, self.state, 'complete', dict(preparation_id=staged['preparation_id'],
            request=self.request(wid), idempotency_key=key))

    def test_stage_is_compact_durable_and_never_approves_or_initializes(self):
        staged, saved = self.stage()
        self.assertNotIn('plan', staged); self.assertNotIn('spec', staged); self.assertNotIn('preview', staged)
        self.assertEqual(json.loads(Path(staged['plan_path']).read_text(encoding='utf-8')), saved['plan'])
        self.assertEqual(staged['scope']['homepage'], 'notes/workspaces/fitness.md')
        self.assertEqual(list(self.vault.iterdir()), [])
        self.assertFalse((self.state / 'workspace-authorities.json').exists())
        self.assertFalse((self.state / 'workspace-runtime.json').exists())
        self.assertTrue(all(r['preserve_current'] for r in staged['approval_requests']))
        repeated, _ = self.stage()
        self.assertEqual(repeated['preparation_id'], staged['preparation_id'])
        denied, code = self.complete(staged)
        self.assertEqual(code, 4, denied)
        self.assertFalse(artifacts(denied)['configuration_applied'])
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_exact_c1_approval_initializes_first_page_and_leaves_activation_pending(self):
        staged, saved = self.stage(); self.approve(saved)
        # A real canonical first page is sufficient; no dummy successful intent,
        # global activate call, all-record adoption or scratch note is involved.
        with patch.object(WorkspaceRuntime, 'activate', side_effect=AssertionError('activation forbidden')):
            result, code = self.complete(staged)
        self.assertEqual(code, 5, result)
        progress = artifacts(result)
        self.assertTrue(progress['configuration_applied']); self.assertTrue(progress['homepage_verified'])
        self.assertEqual(progress['stage'], 'activation_pending')
        page = self.vault / staged['scope']['homepage']; self.assertTrue(page.is_file())
        metadata, body = parse(page.read_bytes().decode('utf-8'))
        self.assertEqual(metadata['workspace_id'], 'fitness'); self.assertEqual(metadata['visibility'], 'internal')
        self.assertIn('Antrenman kararlarını', body)
        self.assertIn('<!-- beyin-workspace:fitness:start -->', body)
        self.assertFalse((self.state / 'workspace-runtime.json').exists())
        self.assertFalse((self.state / 'workspace-adoptions.json').exists())
        before = page.read_bytes()
        with patch.object(onboarding, 'provisioner', side_effect=AssertionError('config already done')):
            again, code = self.complete(staged)
        self.assertEqual(code, 5, again); self.assertTrue(artifacts(again)['homepage_verified'])
        self.assertEqual(page.read_bytes(), before)

    def test_homepage_batches_registry_reads_for_many_canonical_notes(self):
        notes = self.vault / 'notes' / 'bench'
        notes.mkdir(parents=True)
        for index in range(40):
            name = 'synthetic-%02d' % index
            (notes / (name + '.md')).write_bytes(render(dict(
                id=name, kind='note', revision=1, workspace_id='fitness',
                visibility='internal', title='Synthetic ' + str(index)),
                'Canonical synthetic body').encode('utf-8'))
        SyncEngine(self.vault, self.state).sync()
        staged, saved = self.stage(); self.approve(saved)

        snapshot = Registry.snapshot
        counts = [0]
        def count_snapshot(registry, *args, **kwargs):
            counts[0] += 1
            return snapshot(registry, *args, **kwargs)
        with patch.object(Registry, 'snapshot', count_snapshot):
            result, code = self.complete(staged)
        self.assertEqual(code, 5, result)
        self.assertTrue(artifacts(result)['homepage_verified'])
        self.assertLess(counts[0], 50, 'registry should not be read for each canonical record')
        self.assertIn('Synthetic 0', (self.vault / staged['scope']['homepage']).read_text(encoding='utf-8'))

    def test_grant_revoked_after_batched_eligibility_does_not_mint_context(self):
        staged, saved = self.stage(); self.approve(saved)
        result, code = self.complete(staged)
        self.assertEqual(code, 5, result)

        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        authority = LocalAuthority(runtime.registry)
        original = runtime.store._eligible
        request = self.request()
        request['data']['request_id'] = 'revocation-after-batch'
        calls = [0]
        token_dir = self.state / 'workspace-contexts'
        tokens_before = set(token_dir.iterdir()) if token_dir.exists() else set()
        def revoke_after_scan(*args, **kwargs):
            result = original(*args, **kwargs)
            calls[0] += 1
            authority.revoke('setup-review')
            return result
        with patch.object(runtime.store, '_eligible', revoke_after_scan):
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
                runtime.context(request, include_external_sources=False)
        self.assertEqual(calls, [1])
        self.assertEqual(set(token_dir.iterdir()) if token_dir.exists() else set(), tokens_before)

    def test_revocation_during_homepage_scan_denies_replay(self):
        staged, saved = self.stage(); self.approve(saved)
        result, code = self.complete(staged)
        self.assertEqual(code, 5, result)

        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        original = runtime.store._eligible
        calls = [0]
        def revoke_during_homepage(*args, **kwargs):
            result = original(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 2:
                LocalAuthority(runtime.registry).revoke('setup-review')
            return result
        with patch.object(runtime.store, '_eligible', revoke_during_homepage):
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
                initialize(runtime, self.request(), 'revoked-replay')
        self.assertEqual(calls, [2])

    def test_homepage_skips_external_content_but_normal_context_reads_it(self):
        # The setup homepage lists approved source labels, not extracted
        # excerpts. Normal user retrieval must still read those sources.
        (self.external / 'searchable.txt').write_text(
            'Independent synthetic external evidence', encoding='utf-8')
        staged, saved = self.stage(); self.approve(saved)
        with patch('beyin_v3_sources.SourceResolver.files',
                   side_effect=AssertionError('homepage enumerated external files')) as files, \
             patch('beyin_v3_sources.SourceResolver.extract',
                   side_effect=AssertionError('homepage extracted external bytes')) as extract:
            result, code = self.complete(staged)
            replay, replay_code = self.complete(staged)
        self.assertEqual(code, 5, result)
        self.assertEqual(replay_code, 5, replay)
        self.assertTrue(artifacts(result)['homepage_verified'])
        self.assertTrue(artifacts(replay)['homepage_verified'])
        files.assert_not_called()
        extract.assert_not_called()
        homepage = self.vault / staged['scope']['homepage']
        self.assertIn('Programlar', homepage.read_text(encoding='utf-8'))

        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        request = self.request()
        request['data']['query'] = 'Independent synthetic external evidence'
        context = runtime.context(request)['data']
        self.assertEqual(context['state'], 'ready')
        self.assertTrue(any(
            record['kind'] == 'source' and 'Independent synthetic external evidence' in record['text']
            for record in context['records']), context)

        # Skipping external extraction must not bypass authority revocation.
        LocalAuthority(Registry(self.vault, self.state)).revoke('setup-review')
        with self.assertRaisesRegex(WorkspaceError, 'workspace_context_required'):
            initialize(runtime, request, 'revoked-homepage')

    def test_page_failure_preserves_config_and_resumes_without_reapplying(self):
        staged, saved = self.stage(); self.approve(saved)
        with patch('beyin_v3_workspace_views.initialize', side_effect=WorkspaceError('managed_view_adoption_required')):
            failed, code = self.complete(staged)
        self.assertNotEqual(code, 0)
        progress = artifacts(failed)
        self.assertTrue(progress['configuration_applied']); self.assertFalse(progress['homepage_verified'])
        self.assertEqual(progress['stage'], 'homepage_pending')
        current, _ = onboarding.status(self.vault, self.state, 'finish')
        self.assertEqual(artifacts(current)['stage'], 'homepage_pending')
        with patch.object(onboarding, 'provisioner', side_effect=AssertionError('do not repeat apply')):
            resumed, code = self.complete(staged)
        self.assertEqual(code, 5, resumed); self.assertTrue(artifacts(resumed)['homepage_verified'])

    def test_status_never_certifies_current_page_or_revoked_authority(self):
        staged, saved = self.stage(); self.approve(saved)
        completed, _ = self.complete(staged)
        self.assertTrue(artifacts(completed)['homepage_verified'])
        (self.vault / staged['scope']['homepage']).unlink()
        LocalAuthority(Registry(self.vault, self.state)).revoke('setup-review')
        observed, _ = onboarding.status(self.vault, self.state, 'finish')
        self.assertFalse(artifacts(observed)['homepage_verified'])
        self.assertTrue(artifacts(observed)['homepage_previously_verified'])
        self.assertEqual(artifacts(observed)['verification_scope'], 'journal_snapshot')

    def test_review_discloses_inherited_audiences_for_new_permissions(self):
        first, saved = self.stage(); self.approve(saved); self.complete(first)
        authority = LocalAuthority(Registry(self.vault, self.state))
        prior = authority._load()['grants']['setup-review']
        authority.record_approval('setup-review', machine_id=saved['approval_requests'][0]['machine_id'],
            permissions=prior['permissions'], audiences=('internal', 'private'), evidence='Explicit synthetic private scope')
        second, _ = self.stage(dict(self.payload, workspace_id='decisions'))
        scope = second['scope']['effective_authority'][0]
        self.assertEqual(scope['audiences'], ['internal', 'private'])
        self.assertTrue(all(p['workspace_id'] == 'decisions' for p in scope['new_permissions']))

    def test_interrupted_canonical_write_reuses_exact_checkpointed_intent(self):
        staged, saved = self.stage(); self.approve(saved)
        def fault(phase, index=None):
            if phase == 'writer_returned':
                raise OSError('synthetic crash after canonical writer')
        with patch('beyin_v3_workspace_memory.transaction_hook', side_effect=fault):
            failed, _ = self.complete(staged)
        self.assertTrue(artifacts(failed)['configuration_applied'])
        self.assertFalse(artifacts(failed)['homepage_verified'])
        page = self.vault / staged['scope']['homepage']; before = page.read_bytes()
        with patch('beyin_v3_workspace_views.initialize', side_effect=AssertionError('must resume saved intent')):
            resumed, code = self.complete(staged)
        self.assertEqual(code, 5, resumed)
        self.assertTrue(artifacts(resumed)['homepage_verified']); self.assertEqual(page.read_bytes(), before)
        metadata, _ = parse(before.decode('utf-8')); self.assertEqual(metadata['revision'], 1)

    def test_shared_binding_review_preserves_first_workspace_grants_and_ref(self):
        first, saved = self.stage(); self.approve(saved); self.complete(first)
        authority = LocalAuthority(Registry(self.vault, self.state))
        before = copy.deepcopy(authority._load()['grants']['setup-review'])
        second_payload = dict(self.payload, workspace_id='decisions', name='Karar günlüğü', purpose='Kararları izlemek')
        second, saved_second = self.stage(second_payload)
        request = next(r for r in saved_second['approval_requests'] if r['reference'] == 'setup-review')
        self.assertTrue(all(p in request['permissions'] for p in before['permissions']))
        self.assertTrue(all(p['workspace_id'] == 'decisions' for p in request['permission_delta']))
        self.assertEqual(request['audiences'], before['audiences'])
        self.assertIsNotNone(request['expected_grant_sha256'])
        self.approve(saved_second)
        result, code = self.complete(second, wid='decisions', key='second')
        self.assertEqual(code, 5, result); self.assertTrue(artifacts(result)['homepage_verified'])
        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        self.assertEqual(runtime.context(self.request())['data']['workspace_id'], 'fitness')
        self.assertEqual(runtime.context(self.request('decisions'))['data']['workspace_id'], 'decisions')
        bindings, _ = runtime.registry.bindings()
        self.assertTrue(all(b['authority_ref'] == 'setup-review' for b in bindings.values()))

    def test_revoked_unrelated_binding_grants_are_not_resurrected_by_review(self):
        first, saved = self.stage(); self.approve(saved); self.complete(first)
        LocalAuthority(Registry(self.vault, self.state)).revoke('setup-review')
        second, saved_second = self.stage(dict(self.payload, workspace_id='decisions'))
        for request in saved_second['approval_requests']:
            self.assertIsNone(request['expected_grant_sha256'])
            self.assertTrue(all(p['workspace_id'] == 'decisions' for p in request['permissions']))
        # Existing bindings still mention fitness; exact C1 plan approval must
        # reject resurrecting the revoked authority, rather than silently expand.
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            self.approve(saved_second)

    def test_existing_unscoped_home_is_not_adopted_and_private_records_do_not_leak(self):
        staged, saved = self.stage(); self.approve(saved)
        provisioner(self.vault, self.state).apply(saved['plan'], 'fixture-config')
        page = self.vault / staged['scope']['homepage']; page.parent.mkdir(parents=True, exist_ok=True)
        raw = 'Utilisateur\r\nTexte privé sans marqueur'.encode('utf-8'); page.write_bytes(raw)
        SyncEngine(self.vault, self.state).sync()
        runtime = WorkspaceRuntime(self.vault, self.state, readers={})
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            initialize(runtime, self.request(), 'adoption-forbidden')
        self.assertEqual(page.read_bytes(), raw)
        page.unlink(); SyncEngine(self.vault, self.state).sync()
        hidden = self.vault / 'notes/workspaces/private.md'
        hidden.write_bytes(render(dict(id='private', kind='note', revision=1, workspace_id='fitness',
            visibility='private', title='PRIVATE-TITLE'), 'PRIVATE-BODY').encode('utf-8'))
        SyncEngine(self.vault, self.state).sync()
        initialize(runtime, self.request(), 'private-filter')
        self.assertNotIn('PRIVATE', page.read_text(encoding='utf-8'))

    def test_changed_preparation_no_memory_wrong_scope_and_key_collision_are_denied(self):
        staged, saved = self.stage(); self.approve(saved)
        wrong = self.request('other')
        result, code = onboarding.execute(self.vault, self.state, 'complete', dict(
            preparation_id=staged['preparation_id'], request=wrong, idempotency_key='wrong'))
        self.assertEqual(code, 4, result)
        self.assertFalse(any(self.vault.iterdir()))
        no_memory = self.request(); no_memory['data']['no_memory'] = True
        result, code = onboarding.execute(self.vault, self.state, 'complete', dict(
            preparation_id=staged['preparation_id'], request=no_memory, idempotency_key='disabled'))
        self.assertEqual(code, 4, result)
        self.complete(staged)
        changed_request = self.request(); changed_request['data']['session_id'] = 'new-session'
        result, code = onboarding.execute(self.vault, self.state, 'resume', dict(
            preparation_id=staged['preparation_id'], request=changed_request, idempotency_key='finish'))
        self.assertEqual(code, 5, result)
        self.assertTrue(artifacts(result)['homepage_verified'])
        other, _ = self.stage(dict(self.payload, workspace_id='other'))
        result, code = onboarding.execute(self.vault, self.state, 'resume', dict(
            preparation_id=other['preparation_id'], request=self.request('other'), idempotency_key='finish'))
        self.assertEqual(code, 3, result)
        Path(staged['preparation_path']).write_text(json.dumps(dict(saved, homepage='notes/evil.md')), encoding='utf-8')
        result, code = self.complete(staged, key='tampered')
        self.assertEqual(code, 3, result)
        self.assertIn('preparation_conflict', [i['code'] for i in result['data']['issues']])


if __name__ == '__main__':
    unittest.main()
