#!/usr/bin/env python3
"""Automatic context names the prompt words its records do not contain (#909, after #768)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from v3_package_helpers import isolated_path

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
            'how many boxes should we order? melamin hakkında neler biliyoruz, melamin 7 x', [])
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

    def test_empty_prompt_has_no_terms(self):
        self.assertEqual(beyin_v3.query_term_coverage('', [{'text': 'x'}]), ([], []))
        self.assertEqual(beyin_v3.query_term_coverage(None, []), ([], []))


class HookUnmatchedTermsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-hook-coverage-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.vault = root / 'vault'
        self.state = root / 'state'
        home = root / 'home'
        home.mkdir()
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
                    'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(root), 'TMP': str(root),
                    'PATH': isolated_path(), 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'BEYIN_V3_NO_SPAWN': '1'}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        for relative, text in NOTES.items():
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


if __name__ == '__main__':
    unittest.main()
