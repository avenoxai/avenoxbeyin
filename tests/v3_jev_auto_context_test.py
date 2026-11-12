"""Per-turn advisor path: opt-in and fail-open. Synthetic and offline."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client
import beyin_v3_passage as passage

QUERY = 'how do we deploy the quartz site'
SECRET = 'sk-' + 'a' * 40


class AutoContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.state = self.store.state_dir
        self.calls = []
        self.answers = dict(topical=0.9, deploy=0.9, pricing=0.05, rollback=0.1, journal=0.99)
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-key'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.record('pricing', 'Quartz pricing: deploy the yearly plan at ninety dollars.')
        self.record('deploy', 'Quartz deploy steps: push to main, the site server pulls.')
        # One shared word only: below the strict matcher's bar, inside the loose pool.
        self.record('rollback', 'Undo a release: revert the previous commit, then deploy again.')

    def record(self, ident, text, **extra):
        path = self.vault / extra.pop('source', ident + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, title=ident, text=text, source=path.relative_to(self.vault).as_posix(), **extra))

    def config(self, mode='on', features=('auto_context',)):
        client.set_mode(self.state, mode, enable=features)

    def transport(self, url, body, key, timeout):
        self.calls.append((body, timeout))
        notes = body['state']['notes']
        answers = {'topical': {'type': 'noul', 'noul': self.answers['topical']}}
        for key_, note in notes.items():
            answers[key_] = {'type': 'noul', 'noul': self.answers[note['title']]}
        return {'answers': answers, 'usage': {'input_tokens': 321}}

    def local(self, query=QUERY):
        return self.store.context_for('claude', query, strict=True)

    def filtered(self, query=QUERY, context=None, transport=None, **kw):
        return advisor.auto_context(self.store, 'claude', query, self.local(query) if context is None else context,
                                    budget_chars=kw.pop('budget_chars', 6000), transport=transport or self.transport, **kw)

    def ids(self, context):
        return [r['id'] for r in context['records']]

    def log(self):
        path = self.state / 'jev-calls.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_strict_matcher_misses_the_one_word_match(self):
        self.assertEqual(sorted(self.ids(self.local())), ['deploy', 'pricing'])

    def test_on_adds_a_loose_match_only_above_the_rescue_bar(self):
        self.config()
        self.answers.update(pricing=0.9, rollback=advisor.AUTO_RESCUE - 0.05)
        self.assertNotIn('rollback', self.ids(self.filtered()))
        self.answers.update(rollback=0.9)
        shutil.rmtree(self.state / '.cache')  # same request body, so the first answer is cached
        result = self.filtered()
        self.assertEqual(sorted(self.ids(result)), ['deploy', 'pricing', 'rollback'])
        self.assertEqual(result['jev'], dict(dropped=0, added=1))
        self.assertEqual(sorted(c['id'] for c in result['citations']), ['deploy', 'pricing', 'rollback'])

    def test_strict_match_survives_a_middling_score_a_loose_one_does_not(self):
        self.config()
        self.answers.update(pricing=0.45, rollback=0.45)
        self.assertEqual(sorted(self.ids(self.filtered())), ['deploy', 'pricing'])

    def test_question_with_no_strict_match_can_still_be_answered(self):
        self.config()
        self.answers.update(rollback=0.9)
        query = 'how to undo what went live'
        self.assertEqual(self.ids(self.local(query)), [])
        self.assertEqual(self.ids(self.filtered(query)), ['rollback'])

    def test_session_logs_and_private_notes_never_enter_the_pool(self):
        self.config()
        self.record('journal', 'Today we tried to deploy twice.', source='daily/2026-01-01.md')
        self.record('hidden', 'Private deploy credentials live elsewhere.', visibility='private')
        self.filtered()
        sent = [n['title'] for n in self.calls[0][0]['state']['notes'].values()]
        self.assertEqual(sorted(sent), ['deploy', 'pricing', 'rollback'])

    def test_a_drop_returns_its_room_to_the_kept_record_text(self):
        detail = ' '.join('Deploy step %d checks the quartz site build.' % i for i in range(20))
        self.record('price-long', 'Quartz pricing: deploy the yearly plan at ninety dollars. ' + detail)
        self.record('deploy-long', 'Quartz deploy steps: push to main, the site server pulls. ' + detail)
        self.answers.update({'price-long': 0.05, 'deploy-long': 0.9})
        self.config()
        local = self.store.context_for('claude', QUERY, strict=True, budget_chars=1500)
        clipped = {r['id']: r['text'] for r in local['records']}
        self.assertIn('price-long', clipped)
        self.assertTrue(next(r for r in local['records'] if r['id'] == 'deploy-long')['text_truncated'])
        result = self.filtered(context=local, budget_chars=1500)
        self.assertNotIn('price-long', self.ids(result))
        kept = next(r for r in result['records'] if r['id'] == 'deploy-long')
        # The first pack cut this text to share the budget; the drop frees room, so the final
        # pack starts again from the retrieved excerpt instead of the earlier clip.
        self.assertGreater(len(kept['text']), len(clipped['deploy-long']))
        self.assertLessEqual(result['used_chars'], 1500)
        # The final envelope is packed from the unclipped originals as well.
        self.assertEqual([s['id'] for s in result.sources], self.ids(result))
        self.assertGreaterEqual(len(next(s for s in result.sources if s['id'] == 'deploy-long')['text']),
                                len(kept['text']))

    def test_budget_bounds_the_final_set(self):
        self.config()
        self.answers.update(pricing=0.9, rollback=0.9)
        result = self.filtered(budget_chars=700)
        self.assertLess(len(result['records']), 3)
        self.assertLessEqual(result['used_chars'], 700)

    def test_no_config_mode_on_without_feature_and_off_never_call(self):
        self.assertEqual(self.filtered(), self.local())
        client.set_mode(self.state, 'on')
        self.assertEqual(self.filtered(), self.local())
        client.set_mode(self.state, 'off', enable=['auto_context'])
        self.assertEqual(self.filtered(), self.local())
        self.assertFalse(self.calls)
        self.assertFalse(self.log())

    def test_on_drops_off_topic_note_and_keeps_citations_in_step(self):
        self.config()
        result = self.filtered()
        self.assertEqual(self.ids(result), ['deploy'])
        self.assertEqual([c['id'] for c in result['citations']], ['deploy'])
        self.assertEqual(result['jev'], dict(dropped=1, added=0))
        self.assertFalse(result['abstained'])
        body, timeout = self.calls[0]
        self.assertLessEqual(timeout, 2.0)
        self.assertEqual(body['state']['request'], QUERY)
        # Keyed state plus backticked paths; list indexes bled between candidates in live measurement.
        for key in body['state']['notes']:
            self.assertIn('`notes.' + key + '`', body['questions'][key]['instructions'])

    def test_nothing_to_change_returns_the_local_result_itself(self):
        self.config()
        self.answers.update(pricing=0.95)
        self.assertEqual(self.filtered(), self.local())

    def test_on_orders_by_score(self):
        self.config()
        self.answers.update(pricing=0.95, deploy=0.6, rollback=0.7)
        self.assertEqual(self.ids(self.filtered()), ['pricing', 'rollback', 'deploy'])

    def test_gate_failure_drops_everything(self):
        self.config()
        self.answers.update(topical=0.1, deploy=0.9, pricing=0.9)
        result = self.filtered()
        self.assertEqual(result['records'], [])
        self.assertTrue(result['abstained'])

    def test_gate_failure_cannot_drop_a_local_only_strict_record_it_never_scored(self):
        self.config()
        self.record('local', 'Quartz deploy site local policy.', remote_allowed=False)
        self.answers.update(topical=0.1)
        local = self.local()
        self.assertIn('local', self.ids(local))
        result = self.filtered(context=local)
        self.assertEqual(self.ids(result), ['local'])
        sent = {note['title'] for note in self.calls[0][0]['state']['notes'].values()}
        self.assertNotIn('local', sent)
        scored = [row for row in self.log() if row.get('event') == 'auto_context']
        self.assertEqual(scored[0]['dropped'], len(local['records']) - 1)

    def test_shadow_calls_but_changes_nothing_and_logs_counters(self):
        self.config('shadow')
        self.assertEqual(self.filtered(), self.local())
        self.assertEqual(len(self.calls), 1)
        scored = [row for row in self.log() if row.get('event') == 'auto_context']
        self.assertEqual({k: scored[0][k] for k in ('strict', 'loose', 'dropped', 'rescued', 'gate_passed')},
                         dict(strict=2, loose=1, dropped=1, rescued=0, gate_passed=True))

    def test_log_holds_no_text_and_no_key(self):
        self.config()
        self.filtered()
        raw = (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8')
        for marker in ('quartz', 'Quartz', 'ninety', 'synthetic-key', 'deploy the'):
            self.assertNotIn(marker, raw)
        self.assertEqual(client.status(self.state)['last_24h']['calls'], 1)

    def test_failures_keep_local_context(self):
        self.config()
        local = self.local()
        def broken(url, body, key, timeout): raise OSError('down')
        self.assertEqual(self.filtered(transport=broken), local)
        def malformed(url, body, key, timeout): return {'answers': {'topical': {'type': 'noul', 'noul': 7}}}
        self.assertEqual(self.filtered(transport=malformed), local)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.filtered(), local)

    def test_short_prompt_secret_and_private_never_leave(self):
        self.config()
        self.assertEqual(self.filtered('deploy'), self.local('deploy'))
        leaking = QUERY + ' ' + SECRET
        self.assertEqual(self.filtered(leaking), self.local(leaking))
        context = self.local()
        context['records'][0]['visibility'] = 'private'
        self.assertEqual(self.filtered(context=context), context)
        self.assertFalse(self.calls)
        self.assertEqual([row['outcome'] for row in self.log()], ['skipped_sensitive'])

    def test_kill_switch(self):
        self.config()
        (self.state / 'jev.disabled').write_text('', encoding='utf-8')
        self.assertEqual(self.filtered(), self.local())
        self.assertFalse(self.calls)

    def test_source_changed_during_call_recomputes_safe_local(self):
        self.config()
        local = self.local()
        def edits(url, body, key, timeout):
            (self.vault / 'deploy.md').write_text('changed', encoding='utf-8')
            return self.transport(url, body, key, timeout)
        result = self.filtered(transport=edits)
        self.assertNotIn('deploy', self.ids(result))
        self.assertEqual(result, self.store.context_for('claude', QUERY, strict=True, budget_chars=6000))
        self.assertEqual(result['stale_count'], 1)

    def test_excerpt_is_bounded(self):
        self.record('long', 'Quartz deploy site ' + 'x' * 5000)
        self.answers['long'] = 0.9
        self.config()
        self.filtered()
        self.assertTrue(all(len(n['excerpt']) <= advisor.AUTO_EXCERPT for n in self.calls[0][0]['state']['notes'].values()))

    def test_strict_precedence_in_auto_pool_evaluates_all_strict_candidates(self):
        # 5 strict candidates (sharing 2+ words with query: 'how do we deploy the quartz site')
        for i in range(1, 6):
            self.record(f'strict{i}', f'Quartz deploy site documentation part {i}.')
            self.answers[f'strict{i}'] = 0.9
        # 4 loose candidates (sharing only 1 word: 'deploy')
        for i in range(1, 5):
            self.record(f'loose{i}', f'Single word deploy step variation {i}.')
            self.answers[f'loose{i}'] = 0.9
        self.config('shadow')
        self.filtered()
        sent_notes = self.calls[0][0]['state']['notes']
        sent_titles = {n['title'] for n in sent_notes.values()}
        # All 5 strict candidates must be in the pool sent to the advisor
        for i in range(1, 6):
            self.assertIn(f'strict{i}', sent_titles)
        # Exactly 3 loose candidates fill the remaining slots of AUTO_POOL (8 total)
        self.assertEqual(len(sent_notes), 8)
        scored = [row for row in self.log() if row.get('event') == 'auto_context']
        self.assertEqual(scored[0]['strict'], 5)
        self.assertEqual(scored[0]['loose'], 3)

    def sent_titles(self):
        return [n['title'] for body, _timeout in self.calls for n in body['state']['notes'].values()]

    def test_session_logs_do_not_use_up_the_loose_window(self):
        # #533: the loose pool was cut to 2 * AUTO_POOL first and filtered after, so session logs
        # that repeat the prompt's words took every place and the answer never reached a card.
        for day in range(2 * advisor.AUTO_POOL):
            self.record('log%d' % day, 'Went live, then undo. Undo again, live again.',
                        source='daily/2026-01-%02d.md' % (day + 1))
        self.answers.update(rollback=0.9)
        self.config()
        query = 'how to undo what went live'
        self.assertEqual(self.ids(self.local(query)), [])
        self.assertEqual(self.ids(self.filtered(query)), ['rollback'])
        self.assertEqual(self.sent_titles(), ['rollback'])
        # The window alone fixes it: a vault whose passage index is still pending ranks by words only.
        self.calls.clear()
        shutil.rmtree(self.state / '.cache')
        with patch.object(passage, 'loose_ranking', return_value=[], create=True):
            self.assertEqual(self.ids(self.filtered(query)), ['rollback'])
        self.assertEqual(self.sent_titles(), ['rollback'])

    def long_note_vault(self, **handbook):
        # A strict floor nobody reaches stands in for a paraphrased prompt: only loose matches remain.
        (self.state / 'retrieval.json').write_text(json.dumps({'strict_floor': 0.9}), encoding='utf-8')
        filler = 'Notes on layout, colours, fonts, spacing, images, menus and footers for the pages we publish. '
        sections = ['## Part %d\n\n%s' % (i, filler * 6) for i in range(10)]
        sections[6] = '## Part 6\n\n' + filler * 3 + 'The preview server listens on port 8080. ' + filler * 2
        self.record('handbook', '# Quartz handbook\n\n' + '\n\n'.join(sections), **handbook)
        for i in range(10):
            self.record('tip%d' % i, 'Quartz preview server tip %d: restart the quartz preview server.' % i)
            self.answers['tip%d' % i] = 0.05
        self.answers['handbook'] = 0.9
        return 'which port does the quartz preview server listen on'

    def test_a_long_note_answering_in_one_section_reaches_a_card(self):
        # #533: whole-note BM25 ranks the long note under ten short notes repeating the common
        # words, and the eight cards went to them. Its matching section ranks first by passage.
        query = self.long_note_vault()
        words = [r['id'] for r in self.store.candidates(query, limit=16)]
        self.assertGreater(words.index('handbook'), advisor.AUTO_POOL)
        self.config()
        self.assertEqual(self.ids(self.local(query)), [])
        self.assertEqual(self.ids(self.filtered(query)), ['handbook'])
        sent = self.sent_titles()
        self.assertEqual(len(sent), advisor.AUTO_POOL)
        # The orders alternate: the first word match keeps its card.
        self.assertEqual(sent[:2], [words[0], 'handbook'])

    def test_the_passage_order_passes_the_same_gates(self):
        query = self.long_note_vault(remote_allowed=False)
        self.assertEqual(passage.loose_ranking(self.store, query)[:1], [])  # no strict pass yet
        self.config()
        self.filtered(query)
        self.assertEqual(passage.loose_ranking(self.store, query)[0], 'handbook')
        sent = self.sent_titles()
        self.assertNotIn('handbook', sent)
        self.assertEqual(len(sent), advisor.AUTO_POOL)
        # An index built for another project scope is not reused.
        self.assertEqual(passage.loose_ranking(self.store, query, project='other'), [])

    def test_the_passage_order_reads_hyphenated_names_like_the_strict_pass(self):
        # #430 with #533: the loose order ranks with the strict pass's query terms, so the
        # joined stem of a hyphenated name stands in for its part and is never a term of its own.
        self.record('plugin', 'The claude-mem plugin keeps session notes between runs.')
        query = 'where does claude-mem keep the session notes'
        with patch.object(passage, 'search', wraps=passage.search) as spy:
            self.local(query)
            self.assertTrue(passage.loose_ranking(self.store, query))
        (strict_args, strict_kw), (loose_args, loose_kw) = [(c.args, c.kwargs) for c in spy.call_args_list]
        self.assertTrue(strict_kw.get('compounds'))
        self.assertEqual(loose_kw.get('compounds'), strict_kw['compounds'])
        self.assertEqual(set(loose_args[1]), set(strict_args[1]))
        self.assertNotIn(runtime._stem('claudemem'), set(loose_args[1]))

    def test_a_pending_index_leaves_the_word_order_alone(self):
        query = self.long_note_vault()
        self.local(query)
        self.assertEqual(passage.loose_ranking(self.store, query)[0], 'handbook')
        # A later turn whose cold build runs out of time falls back to the note-level path;
        # the earlier pass's index must not rank its loose pool.
        with patch.object(passage, 'build', side_effect=passage.IndexPending('pending')):
            self.local(query)
        self.assertEqual(passage.loose_ranking(self.store, query), [])

    def test_strict_exclude_applied_to_loose_candidates(self):
        (self.state / 'retrieval.json').write_text(json.dumps({'strict_exclude': ['archive/']}), encoding='utf-8')
        # Loose candidate in archive/ folder
        self.record('archived_loose', 'Single word deploy archived note.', source='archive/archived_deploy.md')
        self.answers['archived_loose'] = 0.99
        self.config()
        result = self.filtered()
        self.assertNotIn('archived_loose', self.ids(result))
        sent_titles = {n['title'] for n in self.calls[0][0]['state']['notes'].values()}
        self.assertNotIn('archived_loose', sent_titles)


class HookWiringTest(unittest.TestCase):
    """The hook reaches the advisor only behind jev.json, and survives its failure."""
    def run_hook(self, vault, state, prompt):
        import beyin_v3_hook as hook
        payload = json.dumps(dict(hook_event_name='UserPromptSubmit', session_id='s', prompt=prompt))
        output = io.StringIO()
        argv = ['hook', '--vault', str(vault), '--state', str(state), '--harness', 'claude']
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(payload)), \
                patch.dict(os.environ, inherited_env(BEYIN_V3_NO_SPAWN='1', TYPESAFE_API_KEY='synthetic-key'), clear=True), \
                redirect_stdout(output):
            hook.main()
        return json.loads(output.getvalue())

    def test_hook_filters_when_enabled_and_is_unchanged_otherwise(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, state = Path(tmp) / 'vault', Path(tmp) / 'state'
            (vault / 'notes').mkdir(parents=True)
            (vault / 'notes/deploy.md').write_text('---\nid: deploy\n---\nQuartz deploy steps: push to main.\n', encoding='utf-8')
            (vault / 'notes/pricing.md').write_text('---\nid: pricing\n---\nQuartz pricing: deploy the yearly plan.\n', encoding='utf-8')
            from beyin_v3_sync import SyncEngine
            SyncEngine(vault.resolve(), state.resolve()).sync()
            before = self.run_hook(vault, state, QUERY)
            text = before['hookSpecificOutput']['additionalContext']
            self.assertIn('pricing', text)
            client.set_mode(state.resolve(), 'on', enable=['auto_context'])

            def transport(url, body, key, timeout):
                return {'answers': {q: {'type': 'noul', 'noul': 0.05 if q != 'topical' and 'pricing' in body['state']['notes'][q]['excerpt'] else 0.9}
                                    for q in body['questions']}}
            with patch.object(client, '_transport', transport):
                after = self.run_hook(vault, state, QUERY)
            text = after['hookSpecificOutput']['additionalContext']
            self.assertIn('deploy steps', text)
            self.assertNotIn('yearly plan', text)
            self.assertIn('"dropped": 1', text)
            with patch.object(advisor, 'auto_context', side_effect=RuntimeError('boom')):
                failed = self.run_hook(vault, state, QUERY)
            self.assertIn('yearly plan', failed['hookSpecificOutput']['additionalContext'])

    def test_hook_continuation_keeps_the_delivered_tail_without_an_advisor_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, state = Path(tmp) / 'vault', Path(tmp) / 'state'
            (vault / 'notes').mkdir(parents=True)
            marker = 'Quartz rollback uses the amber switch.'
            (vault / 'notes/guide.md').write_text('---\nid: guide\n---\n# Overview\n\n' +
                'Unrelated background. ' * 120 + '\n\n## Recovery\n\n' + marker, encoding='utf-8')
            from beyin_v3_sync import SyncEngine
            SyncEngine(vault.resolve(), state.resolve()).sync()
            client.set_mode(state.resolve(), 'on', enable=['auto_context'])
            def transport(url, body, key, timeout):
                return {'answers': {q: {'type': 'noul', 'noul': .99} for q in body['questions']}}
            with patch.object(client, '_transport', transport):
                first = self.run_hook(vault, state, 'explain quartz rollback switch')
            self.assertIn(marker, first['hookSpecificOutput']['additionalContext'])
            with patch.object(advisor, 'auto_context', side_effect=AssertionError('continuation called advisor')) as remote:
                follow = self.run_hook(vault, state, 'bunu detaylandır')
            remote.assert_not_called()
            self.assertIn(marker, follow['hookSpecificOutput']['additionalContext'])
            self.assertEqual('Unrelated background.' in first['hookSpecificOutput']['additionalContext'],
                             'Unrelated background.' in follow['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
