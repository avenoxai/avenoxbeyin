#!/usr/bin/env python3
"""Automatic context names the prompt words its records do not contain (#909, after #768)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from v3_package_helpers import inherited_env, isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3  # noqa: E402

NOTES = {
    'knowledge/supplier.md': 'Supplier notes: the Anatolia ceramics supplier ships every second Tuesday; '
                             'minimum order is forty boxes.',
    'projects/orbit/decisions.md': 'Orbit API decisions: we chose PostgreSQL over MongoDB; the Orbit rate '
                                   'limit is 120 requests per minute.',
    'knowledge/deploy.md': 'Deploy runbook for the Orbit API: build the image, run the migration job, '
                           'then switch traffic. Rollback uses the previous image tag.',
}


# A pasted hex run (calldata, bytecode): one [a-z0-9]+ word of 9000 characters.
BLOB = ''.join('0123456789abcdef'[(i * 7 + 3) % 16] for i in range(9000))


def envelope(context):
    start = context.find('{"records"')
    if start < 0:
        return None
    return json.JSONDecoder().raw_decode(context[start:])[0]


class QueryTermCoverageTest(unittest.TestCase):
    def test_reports_unmatched_words_in_prompt_order(self):
        terms, missing = beyin_v3.query_term_coverage(
            'what is the Orbit GraphQL schema', [{'text': 'Orbit API decisions'}])
        self.assertEqual(terms, ['orbit', 'graphql', 'schema'])
        self.assertEqual(missing, ['graphql', 'schema'])

    def test_wrappers_fillers_short_tokens_and_repeats_are_not_terms(self):
        terms, missing = beyin_v3.query_term_coverage(
            'how often and how many boxes should we order? melamin hakkında neler biliyoruz, melamin 7 x', [])
        self.assertEqual(terms, ['boxes', 'order', 'melamin'])
        self.assertEqual(missing, terms)

    def test_turkish_folding_and_metadata_values_count(self):
        record = {'text': 'İTHAL ürünler.', 'title': 'Zircon', 'facts': {'material': 'copper\nnickel'},
                  'aliases': ['cobalt']}
        terms, missing = beyin_v3.query_term_coverage('ithal ÜRÜNLER zircon cobalt nickel şeker', [record])
        self.assertEqual(terms, ['ithal', 'urunler', 'zircon', 'cobalt', 'nickel', 'seker'])
        self.assertEqual(missing, ['seker'])

    def test_clipped_text_counts_only_as_delivered(self):
        record = {'text': 'supplier' + beyin_v3.CONTEXT_MARKER, 'text_truncated': True}
        self.assertEqual(beyin_v3.query_term_coverage('supplier zircon', [record])[1], ['zircon'])

    def test_hyphenated_name_is_one_term_held_by_its_joined_spelling(self):
        # Retrieval reads claude-mem as one name that a note may write as claudemem (#430).
        self.assertEqual(beyin_v3.query_term_coverage('claude-mem kurulumu nasıl', [{'text': 'claudemem kurulum'}]),
                         (['claude-mem', 'kurulumu'], []))
        self.assertEqual(beyin_v3.query_term_coverage('e-posta SMTP portu', [{'text': 'eposta SMTP port'}]),
                         (['e-posta', 'smtp', 'portu'], []))

    def test_hyphenated_name_needs_every_part_when_not_joined(self):
        self.assertEqual(beyin_v3.query_term_coverage('claude-mem kurulumu', [{'text': 'claude mem kurulum'}])[1], [])
        self.assertEqual(beyin_v3.query_term_coverage('claude-mem kurulumu', [{'text': 'Claude kurulum'}])[1],
                         ['claude-mem'])
        self.assertEqual(beyin_v3.query_term_coverage('gpt-4o claude-mem claude', [])[0], ['gpt-4o', 'claude-mem'])

    def test_possessive_is_held_by_its_short_root(self):
        # Retrieval answers boyum with a note that says Boy (_query_roots); coverage must agree.
        self.assertEqual(beyin_v3.query_term_coverage('benim boyum kaç cm?', [{'text': 'Boy: 180 cm.'}]),
                         (['boyum', 'cm'], []))
        self.assertEqual(beyin_v3.query_term_coverage('BOYUM KAÇ CM', [{'text': 'Boy: 180 cm.'}]),
                         (['boyum', 'cm'], []))
        self.assertEqual(beyin_v3.query_term_coverage('benim boyum kaç cm?', [{'text': 'Kilo: 75 kg, 180 cm.'}])[1],
                         ['boyum'])

    def test_short_root_counts_once_and_keeps_its_exceptions(self):
        self.assertEqual(beyin_v3.query_term_coverage('boyum boyun', [{'text': 'Boy: 180'}]), (['boyum'], []))
        self.assertEqual(beyin_v3.query_term_coverage('boy boyum', [])[0], ['boy'])
        # A word of its own and a pronoun never fall back to the shorter word.
        self.assertEqual(beyin_v3.query_term_coverage('durum raporu', [{'text': 'dur rapor'}])[1], ['durum'])
        self.assertEqual(beyin_v3.query_term_coverage('bunu sunu', [{'text': 'bun sun'}])[1], ['bunu', 'sunu'])
        # A plain word before a hyphenated name does not hide the name.
        self.assertEqual(beyin_v3.query_term_coverage('claude claude-mem', [])[0], ['claude', 'claude-mem'])

    def test_pasted_blob_is_not_a_term(self):
        # A pasted calldata or base64 run is one [a-z0-9]+ word; listing it whole would spend the budget.
        blob = '0x' + 'ab12' * 2000
        self.assertEqual(beyin_v3.query_term_coverage('why does this calldata hit the Orbit rate limit? ' + blob,
                                                      [{'text': 'Orbit rate limit'}]),
                         (['calldata', 'hit', 'orbit', 'rate', 'limit'], ['calldata', 'hit']))
        sha = 'f' * 64
        self.assertEqual(beyin_v3.query_term_coverage(f'commit {sha} {sha}0', [])[0], ['commit', sha])

    def test_empty_prompt_has_no_terms(self):
        self.assertEqual(beyin_v3.query_term_coverage('', [{'text': 'x'}]), ([], []))
        self.assertEqual(beyin_v3.query_term_coverage(None, []), ([], []))

    def test_project_and_source_stem_count_without_generic_folders_or_extension(self):
        record = {'source': 'archivebox/alpha/deploy.md', 'project': 'alpha', 'title': 'Dagitim notu',
                  'text': 'Port 8080 uzerinden systemd servisi calisir.'}
        self.assertEqual(beyin_v3.query_term_coverage('alpha deploy systemd 8080', [record]),
                         (['alpha', 'deploy', 'systemd', '8080'], []))
        # Parent folder names and the .md extension must not falsely satisfy unrelated terms.
        self.assertEqual(beyin_v3.query_term_coverage('archivebox md graphql systemd', [record])[1],
                         ['archivebox', 'md', 'graphql'])


class HookVault(unittest.TestCase):
    notes = NOTES

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-hook-coverage-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.vault = root / 'vault'
        self.state = root / 'state'
        home = root / 'home'
        home.mkdir()
        # inherited_env drops the BEYIN_* settings of the shell running the suite (an agent session's
        # BEYIN_V3_SKIP, a user's BEYIN_V3_ABSTAIN_COVERAGE); the threshold tests pass their own value.
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), TEMP=str(root), TMP=str(root),
                                 PATH=isolated_path(), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                                 BEYIN_V3_NO_SPAWN='1')
        self.env.pop('BEYIN_V3_ABSTAIN_COVERAGE', None)
        for relative, text in self.notes.items():
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(text + '\n')
        from beyin_v3_sync import SyncEngine
        SyncEngine(self.vault, self.state).sync()
        self.turn = 0

    def invoke(self, prompt, **extra):
        self.turn += 1
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': f'coverage-{self.turn}',
                   'event_id': f'coverage-{self.turn}', 'cwd': str(self.vault), 'prompt': prompt}
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=dict(self.env, **extra), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        return output.get('hookSpecificOutput', {}).get('additionalContext', '')


class HookUnmatchedTermsTest(HookVault):
    def test_partly_covered_prompt_names_its_unmatched_words(self):
        body = envelope(self.invoke('what is the Orbit API GraphQL schema version'))
        self.assertIn('projects/orbit/decisions.md', [r['source'] for r in body['records']])
        self.assertFalse(body['abstained'])
        self.assertEqual(body['unmatched_terms'], ['graphql', 'schema', 'version'])

    def test_fully_covered_prompt_carries_no_field(self):
        body = envelope(self.invoke('what is the minimum order for the ceramics supplier'))
        self.assertEqual([r['source'] for r in body['records']], ['knowledge/supplier.md'])
        self.assertNotIn('unmatched_terms', body)

    def test_long_prompt_lists_a_bounded_number_within_the_budget(self):
        words = ' '.join(f'zircon{i:03d}' for i in range(300))
        context = self.invoke('Orbit API rate limit ' + words)
        body = envelope(context)
        self.assertEqual(len(body['unmatched_terms']), 8)
        self.assertEqual(body['unmatched_terms'][0], 'zircon000')
        self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)

    def test_unmatched_prompt_still_injects_nothing(self):
        self.assertEqual(self.invoke('recommend a recipe for lentil soup'), '')


class HookFieldNeverCostsARecordTest(HookVault):
    """With the threshold off a turn delivers exactly what it delivered before #909; unmatched_terms
    only takes room the records left. Before the fix a pasted blob went into the field whole and the
    envelope gave up its records for it: an empty context where 3.17.0 answered."""
    notes = dict(NOTES, **{
        f'ops/orbit-runbook-{n}.md': f'Orbit deploy runbook {n}. ' + ' '.join(
            ('traffic', 'canary', 'replica', 'gateway', 'quota', 'window', 'shard', 'tenant')[(n * 3 + i * i) % 8]
            for i in range(700)) + '.'
        for n in range(4)}, **{'notes/hiz.md': 'Orbit API hız sınırı dakikada 120 istek; aşılırsa 429 döner.'})
    PROMPTS = {
        'short': 'Orbit rate limit GraphQL schema',
        'giant word': 'why does this calldata hit the Orbit rate limit? 0x' + BLOB,
        'eight long words': 'Orbit rate limit ' + ' '.join(f'z{k}' + BLOB[k * 600:(k + 1) * 600 - 2] for k in range(8)),
        'budget fill': 'deploy runbook rollback canary zircon',
        'budget fill and blob': 'deploy runbook rollback canary zircon 0x' + BLOB[:4000],
        'turkish': 'Orbit API hız sınırı ve GraphQL şeması nedir?',
        'supplier': 'ceramics supplier minimum order and shipping insurance',
    }

    def without_feature(self, prompt):
        """The same turn with no unmatched terms found, which is the hook before #909."""
        self.turn += 1
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': f'coverage-{self.turn}',
                   'event_id': f'coverage-{self.turn}', 'cwd': str(self.vault), 'prompt': prompt}
        code = ('import runpy, sys\n'
                f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
                'import beyin_v3\n'
                'beyin_v3.query_term_coverage = lambda query, records: ([], [])\n'
                f'sys.argv = [{str(HOOK)!r}] + sys.argv[1:]\n'
                f'runpy.run_path({str(HOOK)!r}, run_name="__main__")\n')
        result = subprocess.run([sys.executable, '-c', code, '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout).get('hookSpecificOutput', {}).get('additionalContext', '')

    def set_budget(self, budget):
        preferences = {'context_mode': 'turn'} if budget is None else {'context_chars': budget, 'context_mode': 'turn'}
        with open(self.vault / '.beyin-preferences.json', 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(preferences, handle)

    def test_field_leaves_every_delivered_byte_as_before(self):
        def after_receipt_line(text):
            return text.split('\n', 1)[1] if text else ''  # the receipt line carries the minute
        shown = delivered = 0
        for budget in (1000, 2000, None):
            self.set_budget(budget)
            for name, prompt in self.PROMPTS.items():
                with self.subTest(budget=budget, prompt=name):
                    context, before = self.invoke(prompt), self.without_feature(prompt)
                    self.assertLessEqual(len(context.encode('utf-16-le')) // 2, 10000)
                    body = envelope(context) or {}
                    delivered += bool(body.get('records'))
                    if 'unmatched_terms' in body:
                        shown += 1
                        context = context.replace(', "unmatched_terms": ' + json.dumps(body['unmatched_terms']), '')
                    self.assertEqual(after_receipt_line(context), after_receipt_line(before))
        # Most turns deliver, and the field still appears wherever it fits (13 of 18 when written).
        self.assertGreater(delivered, 12)
        self.assertGreater(shown, 8)

    def test_one_giant_word_keeps_the_record_and_lists_only_short_words(self):
        for size in (3000, 5000, 9000):
            with self.subTest(size=size):
                body = envelope(self.invoke('why does this calldata hit the Orbit rate limit? 0x' + BLOB[:size]))
                self.assertEqual([r['source'] for r in body['records']], ['projects/orbit/decisions.md'])
                self.assertEqual(body['unmatched_terms'], ['calldata', 'hit'])

    def test_eight_long_words_keep_the_record(self):
        body = envelope(self.invoke(self.PROMPTS['eight long words']))
        self.assertEqual([r['source'] for r in body['records']], ['projects/orbit/decisions.md'])
        self.assertNotIn('unmatched_terms', body)


class HookAbstainThresholdTest(HookVault):
    """Opt-in abstention (#909): BEYIN_V3_ABSTAIN_COVERAGE; off by default, byte for byte."""
    PARTIAL = 'what is the Orbit API GraphQL schema version'  # 2 of 5 terms: coverage 2/4
    COVERED = 'what is the Orbit API rate limit'
    CHATTY = ('Before we continue the parser refactor, the docs cleanup and the release notes '
              'checklist for tomorrow, remind me what the Orbit rate limit is')

    def test_low_coverage_match_is_withheld_behind_a_note(self):
        self.assertFalse(envelope(self.invoke(self.PARTIAL, BEYIN_V3_ABSTAIN_COVERAGE='0.5'))['abstained'])
        context = self.invoke(self.PARTIAL, BEYIN_V3_ABSTAIN_COVERAGE='0.6')
        body = envelope(context)
        self.assertEqual(body['records'], [])
        self.assertTrue(body['abstained'])
        self.assertEqual(body['unmatched_terms'], ['graphql', 'schema', 'version'])
        self.assertIn('No reliable related record', body['note'])
        self.assertIn('2 of 5 prompt terms (coverage 0.50, threshold 0.6)', body['note'])
        self.assertNotIn('Orbit API decisions', context)

    def test_covered_match_is_delivered_with_the_threshold_on(self):
        for prompt in (self.COVERED, self.CHATTY):
            with self.subTest(prompt=prompt):
                on = envelope(self.invoke(prompt, BEYIN_V3_ABSTAIN_COVERAGE='0.75'))
                self.assertFalse(on['abstained'])
                self.assertIn('projects/orbit/decisions.md', [r['source'] for r in on['records']])
                self.assertNotIn('note', on)

    def test_threshold_one_requires_every_term(self):
        body = envelope(self.invoke('Orbit rate limit tomorrow', BEYIN_V3_ABSTAIN_COVERAGE='1'))
        self.assertTrue(body['abstained'])
        self.assertEqual(body['unmatched_terms'], ['tomorrow'])

    def test_off_and_invalid_values_keep_the_default_output(self):
        def strip_clock(text):
            return text.split('\n', 1)[1]  # the receipt line carries the minute
        for prompt in (self.PARTIAL, self.COVERED, 'recommend a recipe for lentil soup'):
            default = strip_clock(self.invoke(prompt) or '\n')
            for value in ('', '0', '-1', '1.5', 'nan', 'inf', 'yes'):
                with self.subTest(prompt=prompt, value=value):
                    self.assertEqual(strip_clock(self.invoke(prompt, BEYIN_V3_ABSTAIN_COVERAGE=value) or '\n'),
                                     default)

    def test_prompt_without_matches_stays_silent(self):
        self.assertEqual(self.invoke('recommend a recipe for lentil soup', BEYIN_V3_ABSTAIN_COVERAGE='0.6'), '')


class HookHyphenatedNameTest(HookVault):
    """A hyphenated prompt name matched by its joined spelling is covered (#430 with #909)."""
    notes = {
        'knowledge/mem.md': 'claudemem kurulum adımları: npm ile kur, sonra config dosyasını düzenle.',
        'knowledge/eposta.md': 'eposta ayarları: SMTP sunucusu mail.example.org, port 587.',
    }
    CASES = (('claude-mem kurulumu nasıl', 'knowledge/mem.md'), ('e-posta SMTP portu', 'knowledge/eposta.md'))

    def test_default_turn_does_not_call_the_name_unmatched(self):
        for prompt, source in self.CASES:
            with self.subTest(prompt=prompt):
                body = envelope(self.invoke(prompt))
                self.assertEqual([r['source'] for r in body['records']], [source])
                self.assertNotIn('unmatched_terms', body)

    def test_threshold_keeps_the_matching_record(self):
        for prompt, source in self.CASES:
            with self.subTest(prompt=prompt):
                body = envelope(self.invoke(prompt, BEYIN_V3_ABSTAIN_COVERAGE='0.5'))
                self.assertFalse(body['abstained'])
                self.assertEqual([r['source'] for r in body['records']], [source])


class HookShortRootTest(HookVault):
    """A possessive question answered by its short root is covered (TurkishShortRootTest with #909)."""
    notes = {
        'body/saglik.md': 'Boy: 180 cm. Kilo: 75 kg.',
        'notes/pano.md': 'Pano ölçüsü 120 cm x 80 cm, parçalar 20 cm.',
        'notes/kamera.md': 'Kamera odak mesafesi 30 cm.',
    }
    PROMPTS = ('benim boyum kaç cm?', 'BOYUM KAÇ CM')

    def test_default_turn_does_not_call_the_possessive_unmatched(self):
        for prompt in self.PROMPTS:
            with self.subTest(prompt=prompt):
                body = envelope(self.invoke(prompt))
                self.assertEqual([r['source'] for r in body['records']], ['body/saglik.md'])
                self.assertFalse(body['abstained'])
                self.assertNotIn('unmatched_terms', body)

    def test_threshold_keeps_the_answering_record(self):
        for prompt in self.PROMPTS:
            with self.subTest(prompt=prompt):
                body = envelope(self.invoke(prompt, BEYIN_V3_ABSTAIN_COVERAGE='0.6'))
                self.assertFalse(body['abstained'])
                self.assertEqual([r['source'] for r in body['records']], ['body/saglik.md'])
                self.assertNotIn('note', body)


class HookProjectAndSourceStemCoverageTest(HookVault):
    """A record retrieved by its project field or file stem must not be withheld as uncovered."""
    notes = {
        'projects/alpha/deploy.md': '---\nproject: alpha\nkind: fact\ntitle: Dagitim notu\n---\n'
                                    'Port 8080 uzerinden systemd servisi calisir.',
    }

    def test_project_and_stem_terms_are_covered_and_pass_abstention_threshold(self):
        body = envelope(self.invoke('alpha deploy systemd 8080', BEYIN_V3_ABSTAIN_COVERAGE='0.6'))
        self.assertFalse(body['abstained'])
        self.assertEqual([r['source'] for r in body['records']], ['projects/alpha/deploy.md'])
        self.assertNotIn('unmatched_terms', body)
        self.assertNotIn('note', body)


if __name__ == '__main__':
    unittest.main()
