"""Rejected-claim lookup: one local read path for jev-memory and candidate generators."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
from beyin_v3_memory_assessment import assess_memory

REJECTED = 'Kullanıcı amber diyagramları tercih ediyor.'


class RejectedMatchesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(Path(self.tmp.name) / 'state', self.vault)
        self.calls = []
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-test-key'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def note(self, ident, text, project='demo', **kw):
        record = dict(id=ident, source=ident + '.md', text=text, project=project,
                      updated_at='2026-09-21T00:00:00Z', **kw)
        path = self.vault / record['source']
        path.write_text(text, encoding='utf-8')
        return self.store.ingest(record)

    def rejected(self, ident='old', text=REJECTED, kind='inference', **kw):
        fields = dict(validity='rejected', rejected_reason='REASON_CANARY: user corrected it.',
                      rejected_at='2026-09-24')
        fields.update(kw)
        return self.note(ident, text, kind=kind, **fields)

    def ids(self, text, project='demo', **kw):
        return [m['record_id'] for m in runtime.rejected_matches(self.store, text, project, **kw)]

    def test_threshold_is_pinned(self):
        self.assertEqual((runtime.REJECTED_MIN_SHARED, runtime.REJECTED_MIN_COVERAGE), (2, 0.6))

    def test_same_claim_matches_across_case_unicode_form_and_suffixes(self):
        self.rejected()
        for text in (REJECTED,
                     'KULLANICI AMBER DİYAGRAMLARI TERCİH EDİYOR',
                     unicodedata.normalize('NFD', REJECTED),
                     'Kullanıcının diyagramlarda amber rengini tercih ettiği görüldü.',
                     'kullanicilar amber diyagramlari tercihleri'):
            with self.subTest(text=text):
                self.assertEqual(self.ids(text), ['old'])

    def test_coverage_boundary_and_minimum_shared_terms(self):
        # Five terms after normalization: amber, diyagram, ediyor, kullanic, tercih.
        self.rejected()
        self.assertEqual(self.ids('amber diyagram tercih'), ['old'])       # 3/5 = 0.6
        self.assertEqual(self.ids('amber diyagram'), [])                   # 2/5 = 0.4
        self.assertEqual(self.ids('Kullanıcı mavi tabloları seviyor'), [])
        self.rejected('short', 'Amber.')
        self.assertEqual(self.ids('amber'), [])                            # 1 shared term is never enough

    def test_title_counts_even_when_the_body_is_long(self):
        body = 'Gözlem notu. ' + 'Ayrıntı cümlesi tekrar eder. ' * 40
        self.rejected('titled', body, title='Hakan görünür olmak istemiyor')
        self.assertEqual(self.ids('Hakan görünür olmak istemiyor mu?'), ['titled'])
        self.assertEqual(self.ids('Hakan görünürlükten kaçınıyor'), [])    # lexical only: paraphrase is out of scope

    def test_short_title_is_a_topic_not_a_claim(self):
        self.rejected('coffee', 'Taha espresso sever ve sabahları iki shot içer.', kind='preference',
                      title='Kahve tercihi')
        self.assertEqual(self.ids("Taha'nın kahve tercihi filtre kahve."), [])
        self.assertEqual(self.ids('Taha espresso sever, sabahları iki shot içer.'), ['coffee'])

    def test_negation_is_out_of_scope(self):
        # Lexical matching cannot see negation: a correction of the rejected claim still matches.
        # The route only becomes inspect_sources, so the agent reads the source before deciding.
        self.rejected('lang', 'Taha Python dilini tercih ediyor.')
        self.assertEqual(self.ids('Taha Python dilini tercih etmiyor, Go dilini tercih ediyor.'), ['lang'])

    def test_only_rejected_inferences_and_preferences_in_scope(self):
        self.rejected('pref', kind='preference')
        self.note('legacy', REJECTED, kind='inference', status='rejected')
        self.note('current', REJECTED, kind='inference')
        self.note('plain-note', REJECTED, validity='rejected')             # kind defaults to note
        self.rejected('elsewhere', project='other')
        self.rejected('private', visibility='private')
        self.rejected('untrusted', trust='untrusted')
        self.assertEqual(self.ids(REJECTED), ['legacy', 'pref'])
        self.assertEqual(self.ids(REJECTED, audience='private'), ['legacy', 'pref', 'private'])
        self.assertEqual(self.ids(REJECTED, audience='public'), [])

    def test_stale_source_is_not_trusted_as_rejected(self):
        self.rejected()
        (self.vault / 'old.md').write_text('Edited after sync.', encoding='utf-8')
        self.assertEqual(self.ids(REJECTED), [])

    def test_result_carries_identity_not_rejected_text(self):
        self.rejected()
        match = runtime.rejected_matches(self.store, REJECTED, 'demo')
        self.assertEqual(match, [dict(record_id='old', source='old.md', rejected_at='2026-09-24', coverage=1.0)])

    def test_order_limit_and_invalid_input(self):
        self.rejected('a-full')
        self.rejected('b-partial', 'Kullanıcı amber diyagramları ve mavi tabloları tercih ediyor.')
        self.assertEqual(self.ids(REJECTED), ['a-full', 'b-partial'])
        self.assertEqual(self.ids(REJECTED, limit=1), ['a-full'])
        self.assertEqual(self.ids('ve bu'), [])
        for args in ((None, 'demo'), (REJECTED, ''), (REJECTED, None)):
            with self.assertRaises(ValueError):
                runtime.rejected_matches(self.store, *args)
        with self.assertRaises(ValueError):
            runtime.rejected_matches(self.store, REJECTED, 'demo', limit=-1)

    # --- jev-memory ---

    def config(self, mode='on'):
        (self.store.state_dir / 'jev.json').write_text(json.dumps(dict(mode=mode)), encoding='utf-8')

    def transport(self, url, body, key, timeout):
        self.calls.append(body)
        answers = {}
        for ident, question in body['questions'].items():
            choice = dict(support='supports', commitment='asserted', kind='decision').get(ident, 'unrelated')
            answers[ident] = dict(type=question['type'], choice=choice, confidence=.99,
                                  probabilities={k: float(k == choice) for k in question['criteria']})
        return dict(answers=answers, usage=dict(input_tokens=10))

    def proposal(self, claim):
        row = self.note('evidence', 'Toplantıda kullanıcı amber diyagramları tercih ediyor dedi.')
        return dict(status='proposed', project='demo', claim=claim, prior_record_ids=[], evidence=[dict(
            record_id=row['id'], source_sha256=row['source_sha256'], quote=row['text'])])

    def test_memory_assessment_blocks_candidate_route_for_rejected_claim(self):
        proposal = self.proposal('Kullanıcı amber diyagramları tercih ediyor.')
        self.config()
        self.assertEqual(assess_memory(self.store, proposal, project='demo', transport=self.transport)['route'],
                         'candidate_for_agent_review')
        self.rejected()
        before = self.store.history('old')
        result = assess_memory(self.store, proposal, project='demo', transport=self.transport)
        self.assertEqual(result['route'], 'inspect_sources')
        self.assertEqual(result['previously_rejected'], ['old'])
        self.assertIn('previously_rejected', result['diagnostics'])
        self.assertFalse(result['memory_written'])
        self.assertEqual(self.store.history('old'), before)
        sent = json.dumps(self.calls, ensure_ascii=False)
        self.assertNotIn('REASON_CANARY', sent)
        self.assertNotIn('"old"', sent)

    def test_memory_assessment_reports_rejection_without_semantic_mode(self):
        self.rejected()
        result = assess_memory(self.store, self.proposal(REJECTED), project='demo')
        self.assertEqual(result['diagnostics'], ['semantic_off', 'previously_rejected'])
        self.assertEqual(result['route'], 'local_source_review')
        self.assertEqual(self.calls, [])

    def test_memory_assessment_unchanged_without_rejection(self):
        self.rejected()
        result = assess_memory(self.store, self.proposal('Kullanıcı mavi tabloları seviyor.'), project='demo')
        self.assertNotIn('previously_rejected', result)
        self.assertEqual(result['diagnostics'], ['semantic_off'])


if __name__ == '__main__':
    unittest.main()
