"""Public-runtime regressions. Transport fixtures are NOT live Jev accuracy evidence."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from v3_package_helpers import clean_environ

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_continuity as continuity
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client
from beyin_v3_memory_assessment import assess_memory


class DecisionQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(Path(self.tmp.name) / 'state', self.vault)
        self.state = self.store.state_dir
        self.calls = []
        self.env = clean_environ(TYPESAFE_API_KEY='synthetic-test-key')
        self.env.start()
        self.addCleanup(self.env.stop)

    def note(self, ident, text='Quartz deployment rollback procedure.', **kw):
        record = dict(id=ident, source=ident + '.md', title=ident, text=text, project='demo',
                      updated_at='2026-09-21T00:00:00Z', **kw)
        path = self.vault / record['source']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(record)

    def config(self, mode='on', **kw):
        (self.state / 'jev.json').write_text(json.dumps(dict(mode=mode, **kw)), encoding='utf-8')

    def transport(self, url, body, key, timeout):
        self.calls.append(body)
        answers = {}
        for ident, question in body['questions'].items():
            kind = question['type']
            if kind == 'score':
                record_id = body['state']['candidates'][int(ident.split('_c')[1])]['id']
                answers[ident] = dict(type=kind, score=2.0 if record_id == 'target' else 0.0)
            elif kind == 'noul':
                answers[ident] = dict(type=kind, noul=.99 if ident == 'topical' or body['state']['notes'][ident]['title'] == 'target' else .01)
            else:
                choice = dict(support='supports', commitment='asserted', kind='decision').get(ident, 'duplicate')
                if ident[1:].isdigit() and ident.startswith('k'):
                    choice = 'supports'
                choices = question['criteria']
                answers[ident] = dict(type=kind, choice=choice, confidence=.99,
                                     probabilities={k: float(k == choice) for k in choices})
        return dict(answers=answers, usage=dict(input_tokens=10))

    def proposal(self, record, **kw):
        return dict(status='proposed', project='demo', claim=record['text'], evidence=[dict(
            record_id=record['id'], source_sha256=record['source_sha256'], quote=record['text'])], **kw)

    def test_aliases_are_retrieved_and_private_aliases_stay_private(self):
        self.note('target', 'Klipler aynı karede kesilir.', aliases=['audio offset', 'ses ofseti'])
        self.note('hidden', 'Private.', aliases=['audio offset'], visibility='private')
        self.assertEqual([r['id'] for r in self.store.retrieve('audio offset')['records']], ['target'])
        self.assertEqual([r['id'] for r in self.store.retrieve('ses ofseti', strict=True)['records']], ['target'])

    def test_alias_count_and_type_are_bounded(self):
        self.note('target', 'Neutral.', aliases=[None, 42] + ['abc'] * 30 + ['shouldnotmatch'])
        self.assertTrue(self.store.retrieve('shouldnotmatch')['abstained'])

    def test_oversized_metadata_does_not_consume_source_slot(self):
        self.note('z', 'Quartz deployment rollback procedure.', facts={'huge': 'x' * 10000})
        self.note('target')
        result = self.store.retrieve('Quartz deployment rollback procedure', limit=1, budget_chars=1000)
        self.assertEqual([r['id'] for r in result['records']], ['target'])
        self.assertLessEqual(result['used_chars'], 1000)

    def test_candidate_pool_is_independent_of_delivery_budget(self):
        self.note('z', 'Quartz deployment ' * 10000)
        self.note('target')
        self.assertEqual(len(self.store.candidates('Quartz deployment', limit=2)), 2)
        with self.assertRaises(ValueError):
            self.store.candidates('x', limit=129)

    def test_final_hook_envelope_remains_parseable(self):
        self.note('target', ('Quartz deployment "quoted"\\\n' * 100))
        context = self.store.retrieve('Quartz deployment', budget_chars=900)
        text, delivered = runtime.render_context(context, 1000, prefix='Header\n', suffix='History' * 100)
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(json.loads(text[len('Header\n'):]), delivered)
        self.assertTrue(delivered['records'])
        self.assertEqual([r['id'] for r in delivered['records']], [c['id'] for c in delivered['citations']])
        self.assertEqual(runtime.render_context(context, 2)[0], '')

    def test_manual_on_can_rescue_a_record_beyond_final_limit(self):
        self.note('z')
        self.note('target')
        local = self.store.retrieve('Quartz deployment', project='demo', limit=1)
        self.assertEqual(local['records'][0]['id'], 'z')
        self.config()
        result = advisor.advise_context(self.store, 'Quartz deployment', project='demo', limit=1, transport=self.transport)
        self.assertEqual([r['id'] for r in result['records']], ['target'])
        self.assertEqual(len(self.calls[0]['state']['candidates']), 2)

    def test_render_preserves_upstream_omissions_and_clipping(self):
        self.note('target', 'Quartz deployment ' * 200)
        self.note('other')
        context = self.store.retrieve('Quartz deployment', limit=1, budget_chars=900)
        self.assertTrue(context['records'][0]['text_truncated'])
        self.assertEqual(context['omitted_count'], 1)
        text, delivered = runtime.render_context(context, 2000)
        self.assertEqual(json.loads(text), delivered)
        self.assertTrue(delivered['truncated'])
        self.assertEqual(delivered['omitted_count'], 1)
        # An additional envelope omission must be counted too.
        _, empty = runtime.render_context(context, 300)
        self.assertEqual(empty['omitted_count'], 2)

    def test_high_confidence_prior_conflict_requires_source_inspection(self):
        row = self.note('target', 'Quartz launches on Monday.')
        self.note('prior', 'Quartz launches on Tuesday.')
        self.config()
        def conflict(url, body, key, timeout):
            response = self.transport(url, body, key, timeout)
            response['answers']['relation_p0'].update(choice='contradiction',
                probabilities={k: float(k == 'contradiction') for k in body['questions']['relation_p0']['criteria']})
            return response
        result = assess_memory(self.store, self.proposal(row, prior_record_ids=['prior']),
                               project='demo', transport=conflict)
        self.assertEqual(result['route'], 'inspect_sources')
        self.assertIn('prior_conflict', result['diagnostics'])
        self.assertFalse(result['memory_written'])

    def test_manual_shadow_keeps_exact_local_membership_order_and_budget(self):
        self.note('z'); self.note('target'); self.config('shadow')
        local = self.store.retrieve('Quartz deployment', project='demo', limit=1)
        result = advisor.advise_context(self.store, 'Quartz deployment', project='demo', limit=1, transport=self.transport)
        self.assertEqual({k: v for k, v in result.items() if k != 'jev'}, local)
        self.assertEqual(len(self.calls), 1)

    def test_local_only_titles_and_bodies_never_enter_provider_cards(self):
        self.note('z', 'Quartz deployment local secret.', remote_allowed=False, aliases=['PRIVATE_TITLE_CANARY'])
        self.note('target'); self.config()
        result = advisor.advise_context(self.store, 'Quartz deployment', project='demo', limit=1, transport=self.transport)
        self.assertEqual(result['records'][0]['id'], 'target')
        self.assertNotIn('local secret', json.dumps(self.calls))
        self.assertNotIn('PRIVATE_TITLE_CANARY', json.dumps(self.calls))

    def test_local_only_common_word_does_not_override_semantic_abstention(self):
        self.note('local', 'Donanım test raporu.', remote_allowed=False)
        self.note('remote', 'Test archive.'); self.config()
        result = advisor.advise_context(self.store, 'test eder misin', project='demo', transport=self.transport)
        self.assertTrue(result['abstained'])

    def test_auto_local_only_records_do_not_consume_remote_candidate_quota(self):
        for i in range(9): self.note('z' + str(i), remote_allowed=False)
        self.note('target'); self.config(features=['auto_context'])
        local = self.store.retrieve('Quartz deployment', project='demo', strict=True)
        result = advisor.auto_context(self.store, 'claude', 'Quartz deployment', local, project='demo',
                                      budget_chars=900, transport=self.transport)
        self.assertEqual(result['records'][0]['id'], 'target')
        self.assertEqual([v['title'] for v in self.calls[0]['state']['notes'].values()], ['target'])
        self.assertEqual(result['jev']['added'], 1)

    def test_auto_explicit_project_does_not_send_another_projects_note(self):
        row = self.note('target')
        other = self.note('other')
        self.store.update_task('other', 1, {'project': 'elsewhere'})
        self.config(features=['auto_context'])
        local = self.store.retrieve('Quartz deployment', project='demo', strict=True)
        advisor.auto_context(self.store, 'claude', 'Quartz deployment', local, project='demo',
                             budget_chars=5000, transport=self.transport)
        self.assertNotIn('other', json.dumps(self.calls))

    def test_every_disabled_surface_avoids_credentials_cache_transport_and_secret_patterns(self):
        row = self.note('target')
        proposal = self.proposal(row)
        claim = dict(text=row['text'], citations=proposal['evidence'])
        for config in ({'mode': 'off'}, {'mode': 'on', 'features': []}):
            self.config(**config)
            with patch.object(client, '_environment', side_effect=AssertionError('credential read')), \
                 patch.object(client, '_cache_path', side_effect=AssertionError('cache')), \
                 patch.object(client, '_transport', side_effect=AssertionError('network')), \
                 patch.object(advisor, '_safe', side_effect=AssertionError('secret pattern read')):
                self.assertFalse(client.status(self.state)['key_checked'])
                self.assertFalse(advisor.advise_context(self.store, 'Quartz deployment', project='demo')['jev']['scores'])
                self.assertFalse(advisor.review_candidate(self.store, proposal, project='demo')['approved'])
                self.assertEqual(advisor.verify_answer(self.store, [claim], project='demo')['claims'][0]['verdict'], 'uncertain')
                self.assertFalse(assess_memory(self.store, proposal, project='demo')['memory_written'])
            self.assertFalse((self.state / '.cache').exists())
            self.assertFalse((self.state / 'jev-calls.jsonl').exists())

    def test_kill_switch_status_does_not_read_credentials(self):
        self.config(); (self.state / 'jev.disabled').touch()
        with patch.object(client, '_environment', side_effect=AssertionError('credential read')):
            self.assertFalse(client.status(self.state)['key_checked'])

    def test_config_generation_invalidates_cache_after_reenable(self):
        self.note('target'); self.config()
        def ask():
            return advisor.advise_context(self.store, 'Quartz deployment', project='demo', transport=self.transport)['jev']
        self.assertFalse(ask()['cache_hit'])
        self.assertTrue(ask()['cache_hit'])
        client.set_mode(self.state, 'off'); client.set_mode(self.state, 'on')
        self.assertFalse(ask()['cache_hit'])
        self.assertEqual(len(self.calls), 2)

    def test_model_and_rubric_races_clear_results_without_writing_cache(self):
        self.note('target')
        for field, value in [('model', 'different-model'), ('rubric_version', 'new-rubric'), ('features', [])]:
            self.config()
            def mutate(url, body, key, timeout):
                self.config(**{field: value})
                return self.transport(url, body, key, timeout)
            result = advisor.advise_context(self.store, 'Quartz deployment', project='demo', transport=mutate)['jev']
            self.assertTrue(result['degraded'])
            self.assertEqual(result['scores'], {})
            self.assertFalse(list((self.state / '.cache/jev').glob('*.json')))

    def test_index_only_revocation_during_auto_call_cannot_escape_in_output(self):
        self.note('target'); self.config(features=['auto_context'])
        local = self.store.retrieve('Quartz deployment', strict=True)
        def revoke(url, body, key, timeout):
            self.store.update_task('target', 1, {'visibility': 'private'})
            return self.transport(url, body, key, timeout)
        result = advisor.auto_context(self.store, 'claude', 'Quartz deployment', local,
                                      budget_chars=5000, transport=revoke)
        self.assertTrue(result['abstained'])

    def test_revocation_between_retrieval_and_call_is_filtered_before_network(self):
        self.note('target'); self.config(features=['auto_context'])
        local = self.store.retrieve('Quartz deployment', strict=True)
        self.store.update_task('target', 1, {'trust': 'untrusted'})
        result = advisor.auto_context(self.store, 'claude', 'Quartz deployment', local,
                                      budget_chars=5000, transport=self.transport)
        self.assertFalse(self.calls)
        self.assertTrue(result['abstained'])

    def test_cancellation_far_beyond_old_quote_window_is_sent(self):
        row = self.note('target', 'Launch on Monday. ' + 'Background. ' * 60 + 'This plan is cancelled.')
        self.config()
        claims = [dict(text='Launch on Monday.', citations=[dict(record_id='target',
                  source_sha256=row['source_sha256'], quote='Launch on Monday.')])]
        advisor.verify_answer(self.store, claims, project='demo', transport=self.transport)
        self.assertIn('This plan is cancelled.', self.calls[0]['state']['items']['k0']['evidence']['source_context'][0])

    def test_memory_dimensions_are_batched_without_writing_a_record_or_task(self):
        row = self.note('target'); self.note('prior')
        self.config()
        before = self.store.database.read_bytes()
        history = self.store.history('target')
        result = assess_memory(self.store, self.proposal(row, prior_record_ids=['prior']), project='demo', transport=self.transport)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(set(self.calls[0]['questions']), {'support', 'commitment', 'kind', 'relation_p0'})
        self.assertEqual(result['route'], 'candidate_for_agent_review')
        self.assertEqual(result['prior_relations'][0]['choice'], 'duplicate')
        self.assertFalse(any(result[k] for k in ('approved', 'memory_written', 'task_created', 'task_completed')))
        self.assertEqual(self.store.history('target'), history)
        self.assertEqual(self.store.database.read_bytes(), before)
        self.assertEqual(len(self.store._retrieve('', snapshot=True)['records']), 2)

    def test_tentative_statement_remains_tentative_at_high_confidence(self):
        row = self.note('target', 'Belki yarın yayınlayabiliriz. Henüz karar vermedim.')
        self.config()
        def tentative(url, body, key, timeout):
            response = self.transport(url, body, key, timeout)
            answer = response['answers']['commitment']
            answer.update(choice='tentative', probabilities=dict(asserted=0.0, tentative=1.0, not_stated=0.0))
            return response
        result = assess_memory(self.store, self.proposal(row, prior_record_ids=[]), project='demo', transport=tentative)
        self.assertEqual(result['route'], 'inspect_sources')
        self.assertFalse(result['approved'])

    def test_memory_low_confidence_and_contradiction_route_to_source_inspection(self):
        row = self.note('target'); self.config()
        def uncertain(url, body, key, timeout):
            response = self.transport(url, body, key, timeout)
            response['answers']['support'].update(choice='contradicts', confidence=.5,
                                                  probabilities=dict(supports=0.0, contradicts=1.0, says_nothing=0.0))
            return response
        result = assess_memory(self.store, self.proposal(row, prior_record_ids=[]), project='demo', transport=uncertain)
        self.assertEqual(result['route'], 'inspect_sources')
        self.assertIn('uncertain_decision', result['diagnostics'])

    def test_memory_different_project_prior_is_rejected_before_network(self):
        row = self.note('target'); self.note('other')
        self.store.update_task('other', 1, {'project': 'elsewhere'})
        self.config()
        with self.assertRaisesRegex(ValueError, 'prior_not_current_or_in_scope'):
            assess_memory(self.store, self.proposal(row, prior_record_ids=['other']), project='demo', transport=self.transport)
        self.assertFalse(self.calls)

    def test_memory_local_only_evidence_and_prior_do_not_leave(self):
        row = self.note('target', remote_allowed=False); self.config()
        result = assess_memory(self.store, self.proposal(row), project='demo', transport=self.transport)
        self.assertEqual(result['diagnostics'], ['source_local_only'])
        self.assertFalse(self.calls)

    def test_memory_shadow_has_no_applied_dimensions_or_changed_route(self):
        row = self.note('target'); self.config('shadow')
        result = assess_memory(self.store, self.proposal(row), project='demo', transport=self.transport)
        self.assertEqual(result['dimensions'], {})
        self.assertEqual(result['route'], 'local_source_review')
        self.assertEqual(result['diagnostics'], ['shadow_not_applied'])

    def test_memory_source_revision_race_clears_decisions(self):
        row = self.note('target'); self.config()
        def mutate(url, body, key, timeout):
            self.store.update_task('target', 1, {'remote_allowed': False})
            return self.transport(url, body, key, timeout)
        result = assess_memory(self.store, self.proposal(row), project='demo', transport=mutate)
        self.assertFalse(result['mechanical_verified'])
        self.assertFalse(result['dimensions'])
        self.assertEqual(result['diagnostics'], ['source_or_configuration_changed'])

    def test_memory_changed_decision_requires_non_older_source_dates(self):
        row = self.note('target'); self.note('prior')
        self.store.update_task('prior', 1, {'updated_at': '2026-09-22T00:00:00Z'})
        self.config()
        def changed(url, body, key, timeout):
            response = self.transport(url, body, key, timeout)
            q = 'relation_p0'
            response['answers'][q].update(choice='changed_decision',
                probabilities={k: float(k == 'changed_decision') for k in body['questions'][q]['criteria']})
            return response
        result = assess_memory(self.store, self.proposal(row, prior_record_ids=['prior']), project='demo', transport=changed)
        self.assertEqual(result['route'], 'inspect_sources')
        self.assertIn('temporal_order_unverified', result['diagnostics'])

    def test_log_whitelist_and_real_percentiles(self):
        client.log_event(self.state, dict(prompt='SENSITIVE', note='SENSITIVE', purpose='retrieval', mode='on',
                                        latency_ms=10, network_requests=1))
        client.log_event(self.state, dict(purpose='retrieval', mode='on', latency_ms=20, network_requests=1))
        summary = client.status(self.state)['last_24h']
        self.assertEqual(summary['median_latency_ms'], 15)
        self.assertEqual(summary['p95_latency_ms'], 20)
        self.assertEqual(summary['network_requests'], 2)
        self.assertNotIn('SENSITIVE', (self.state / 'jev-calls.jsonl').read_text())

    def anchor(self, query='Quartz deployment rollback', project='demo', now=100):
        self.note('target')
        local = self.store.retrieve(query, project=project, strict=True)
        continuity.remember(self.store, 'claude', 'session', query, local, now=now)
        return local

    def follow(self, query, **kw):
        local = self.store.retrieve(query, strict=True)
        return continuity.resolve(self.store, kw.pop('harness', 'claude'), kw.pop('session', 'session'),
                                  query, local, budget_chars=5000, now=kw.pop('now', 101), **kw)

    def test_free_continuations_are_compositional_not_prefix_only(self):
        self.anchor()
        for query in ('onu da test et', 'biraz kurcala sonra sonucu anlat', 'biraz takıl genel faydalarını raporla sonra',
                      'bizimkinde?', 'aynı şekilde geri al'):
            with self.subTest(query=query):
                context, inherited = self.follow(query)
                self.assertTrue(inherited)
                self.assertEqual([r['id'] for r in context['records']], ['target'])

    def test_unfamiliar_new_topic_and_greetings_do_not_inherit(self):
        self.anchor()
        for query in ('WebSocket bağlantısı nasıl toparlanır?', 'başka konu bulutları anlat', 'teşekkürler görüşürüz'):
            self.assertFalse(self.follow(query)[1])

    def test_session_harness_project_ttl_and_revision_boundaries(self):
        self.anchor()
        for kw in ({'session': 'other'}, {'session': 'unknown'}, {'harness': 'codex'}, {'project': 'other'}, {'now': 1301}):
            self.assertFalse(self.follow('onu da test et', **kw)[1])
        self.store.update_task('target', 1, {'remote_allowed': False})
        self.assertFalse(self.follow('onu da test et')[1])

    def test_vague_continuations_do_not_extend_ttl_or_persist_prompt(self):
        self.anchor()
        context, inherited = self.follow('onu da test et', now=1000)
        continuity.remember(self.store, 'claude', 'session', 'RAW_QUERY_CANARY', context, inherited=inherited, now=1000)
        self.assertFalse(self.follow('onu da test et', now=1301)[1])
        saved = next((self.state / 'topic-refs').glob('*.json')).read_text()
        self.assertNotIn('Quartz', saved)
        self.assertNotIn('RAW_QUERY_CANARY', saved)
        self.assertNotIn('session', saved)

    def test_zero_delivered_sources_cannot_establish_an_anchor(self):
        row = self.note('target')
        context = self.store.retrieve('Quartz deployment')
        _, delivered = runtime.render_context(context, 2)
        continuity.remember(self.store, 'claude', 'session', 'Quartz deployment', delivered, now=100)
        self.assertFalse(self.follow('onu da test et')[1])

    def test_anchor_files_stay_at_the_cap_and_an_update_deletes_nothing(self):
        delivered = dict(records=[dict(id='target', revision=1, source_sha256='0' * 64, project='demo')])
        folder = self.state / 'topic-refs'
        for index in range(continuity.MAX_SESSIONS):
            continuity.remember(self.store, 'claude', 'session-' + str(index), 'Quartz deployment', delivered, now=100)
            os.utime(continuity._path(self.store, 'claude', 'session-' + str(index)), (1000 + index, 1000 + index))
        oldest = continuity._path(self.store, 'claude', 'session-0')
        # The newest session writes again: nothing else goes.
        continuity.remember(self.store, 'claude', 'session-' + str(continuity.MAX_SESSIONS - 1), 'Quartz deployment',
                            delivered, now=100)
        self.assertEqual(len(list(folder.glob('*.json'))), continuity.MAX_SESSIONS)
        self.assertTrue(oldest.exists())
        # A new session takes the oldest one's place; the count never passes the cap.
        continuity.remember(self.store, 'claude', 'session-new', 'Quartz deployment', delivered, now=100)
        self.assertEqual(len(list(folder.glob('*.json'))), continuity.MAX_SESSIONS)
        self.assertFalse(oldest.exists())


if __name__ == '__main__':
    unittest.main()
