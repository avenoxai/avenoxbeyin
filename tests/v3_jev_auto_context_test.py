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
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client

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


if __name__ == '__main__':
    unittest.main()
