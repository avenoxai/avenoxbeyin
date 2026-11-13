#!/usr/bin/env python3
"""Paired query experiments keep the corpus and every target label fixed."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env
import compare_v3_queries as paired

FIXTURE = ROOT / 'tests/fixtures/v3/turkish_query_pairs.json'
VARIANTS = FIXTURE.with_name('turkish_query_variants.json')


class QueryPairsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.variants = json.loads(VARIANTS.read_text(encoding='utf-8'))

    def test_complete_rewrites_preserve_labels_and_input(self):
        before = copy.deepcopy(self.fixture)
        cases = paired.paired_cases(self.fixture, self.variants)
        self.assertEqual(self.fixture, before)
        self.assertEqual(len(cases), len(before['cases']))
        for raw, rewritten in zip(before['cases'], cases):
            self.assertEqual({k: v for k, v in raw.items() if k != 'query'},
                             {k: v for k, v in rewritten.items() if k != 'query'})
        self.assertEqual(cases[0]['query'], 'Redis')

    def test_rejects_missing_extra_duplicate_empty_and_label_changes(self):
        invalid = [self.variants['variants'][:-1],
                   self.variants['variants'] + [{'id': 'extra', 'query': 'x'}],
                   self.variants['variants'] + [self.variants['variants'][0]]]
        for field, value in [('query', ''), ('target_id', 'other')]:
            changed = copy.deepcopy(self.variants['variants'])
            changed[0][field] = value
            invalid.append(changed)
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                paired.paired_cases(self.fixture, {'variants': changes})

    def test_rejects_invalid_labels_and_unsafe_or_duplicate_sources(self):
        for source in ('../outside.md', '/tmp/outside.md', 'C:/outside.md',
                       'a\\b.md', 'a/../b.md', 'a//b.md', 'a\x00.md', 'CON.md', 'a./b.md', 'a?/b.md',
                       self.fixture['records'][1]['source'].upper(),
                       self.fixture['records'][1]['source']):
            fixture = copy.deepcopy(self.fixture)
            fixture['records'][0]['source'] = source
            with self.subTest(source=source), self.assertRaises(ValueError):
                paired.paired_cases(fixture, self.variants)
        for key, value in [('text', None), ('facts', []), ('title', {})]:
            fixture = copy.deepcopy(self.fixture)
            fixture['records'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                paired.paired_cases(fixture, self.variants)
        fixture = copy.deepcopy(self.fixture)
        fixture['cases'][0]['category'] = []
        with self.assertRaises(ValueError):
            paired.paired_cases(fixture, self.variants)
        for target in ('unknown', [], None):
            fixture = copy.deepcopy(self.fixture)
            fixture['cases'][0]['target_id'] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                paired.paired_cases(fixture, self.variants)

    def test_frozen_fixture_digest_and_actual_paired_execution(self):
        expected = FIXTURE.with_suffix('.sha256').read_text().strip()
        self.assertEqual(expected, '582e5f057d3075951c309400b884639c313dbdb471b8b9173327d3d1aa951307')
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), expected)
        before = (FIXTURE.read_bytes(), VARIANTS.read_bytes())
        report = paired.evaluate(FIXTURE, VARIANTS)
        self.assertEqual((FIXTURE.read_bytes(), VARIANTS.read_bytes()), before)
        self.assertEqual(report['cases'], 10)
        self.assertEqual(set(report['modes']), {'non_strict', 'context'})
        for mode in report['modes'].values():
            self.assertEqual(len(mode['pairs']), 10)
            for metric in paired.METRICS:
                self.assertEqual(mode['delta'][metric], round(
                    mode['rewritten'][metric] - mode['baseline'][metric], 3))
            for control in ('var', 'about', 'interest', 'filler'):
                pair = next(p for p in mode['pairs'] if p['case_id'] == control)
                self.assertEqual(pair['change'], 'unchanged')
        # Diagnostic, not a ranking gate: don't require a particular gain.
        self.assertIn('not precision', report['limits'][0])

    def test_cli_reports_errors_and_does_not_publish_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = subprocess.run([sys.executable, str(ROOT / 'scripts/compare_v3_queries.py'),
                                     '--fixture', str(FIXTURE), '--variants', str(VARIANTS),
                                     '--modes', 'non_strict'], cwd=tmp, env=inherited_env(),
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertEqual(json.loads(output.stdout)['cases'], 10)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        for limit in (1, 10):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'must be 5'):
                paired.evaluate(FIXTURE, VARIANTS, limit=limit)


if __name__ == '__main__':
    unittest.main()
