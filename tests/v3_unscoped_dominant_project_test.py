#!/usr/bin/env python3
"""Unscoped explicit context: a dominant project is demoted, weak one-word matches abstain."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v3_dominant_project_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env  # noqa: E402

TOPICS = ('laplace transform', 'transfer function', 'bode plot', 'root locus', 'state space',
          'nyquist criterion', 'pid tuning', 'step response', 'phase margin', 'gain margin',
          'sampling theorem', 'fourier series', 'z transform', 'convolution', 'impulse response',
          'filter design', 'stability test', 'pole placement', 'observer design', 'lead compensator')


class UnscopedDominantProjectTest(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-dominant-project-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.runtime = root / 'runtime'
        self.store = self.module.MemoryStore(self.runtime, self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))
        # One course project holds 21 of 25 notes, far above half of the index.
        for i, topic in enumerate(TOPICS):
            self.ingest(f'course{i}', f'Lecture notes on {topic}. Worked examples on {topic}.',
                        f'school/{topic.replace(" ", "-")}.md', project='school')
        self.ingest('lab', 'Lab kit deploy schedule. Deploy schedule for the lab kits: deploy each '
                    'kit on schedule.', 'school/lab-kits.md', project='school')
        self.ingest('ops', 'Production releases: we deploy on Fridays, see the schedule page. Rollback '
                    'owner, change window and a status page update.', 'knowledge/releases.md')
        self.ingest('garden', 'Garden plan: tomatoes, basil and the watering rota.', 'projects/garden/garden.md',
                    project='garden')
        self.ingest('weekly', 'Weekly update: hiring, offsite, travel budget.', 'notes/weekly.md')
        self.ingest('cooking', 'Bread recipe: flour, water, salt, yeast.', 'notes/bread-recipe-card.md')

    def ingest(self, id, text, source, project=None):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        record = {'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                  'text': text, 'facts': {}, 'source': source, 'updated_at': '2026-09-01T10:00:00Z'}
        if project:
            record['project'] = project
        self.store.ingest(record)

    def ids(self, query, **kwargs):
        kwargs.setdefault('explicit', True)
        return [r['id'] for r in self.store.retrieve(query, **kwargs)['records']]

    def without_demotion(self, query, **kwargs):
        self.store.DOMINANT_PROJECT_WEIGHT = 1.0
        try:
            return self.ids(query, **kwargs)
        finally:
            del self.store.DOMINANT_PROJECT_WEIGHT

    def test_dominant_project_is_demoted_not_removed(self):
        # By term frequency alone the lab note wins; the shared note is the better answer
        # for an unscoped question, and the lab note stays in the result.
        self.assertEqual(self.without_demotion('deploy schedule')[:2], ['lab', 'ops'])
        self.assertEqual(self.ids('deploy schedule')[:2], ['ops', 'lab'])

    def test_query_naming_the_project_is_not_demoted(self):
        self.assertEqual(self.ids('school deploy schedule')[0], 'lab')

    def test_scoped_query_is_unchanged(self):
        scoped = self.ids('deploy schedule', project='school')
        self.assertEqual(scoped, self.without_demotion('deploy schedule', project='school'))
        self.assertEqual(scoped[0], 'lab')

    def test_small_index_has_no_dominant_project(self):
        self.store.DOMINANT_PROJECT_MIN_RECORDS = 1000
        self.assertEqual(self.ids('deploy schedule')[:2], ['lab', 'ops'])

    def test_out_of_scope_question_abstains(self):
        # One incidental shared word ("recipe") is not enough for a multiword question.
        result = self.store.retrieve('lasagna recipe tonight', explicit=True)
        self.assertEqual(result['records'], [])
        self.assertTrue(result['abstained'])

    def test_plain_retrieve_and_candidates_are_unchanged(self):
        # Companion retrieval and the advisor pools call these without explicit=True.
        self.assertEqual(self.ids('lasagna recipe tonight', explicit=False), ['cooking'])
        self.assertEqual([r['id'] for r in self.store.candidates('lasagna recipe tonight')], ['cooking'])
        self.assertEqual(self.ids('deploy schedule', explicit=False)[:2], ['lab', 'ops'])

    def test_context_command_applies_the_rules(self):
        def context(*args):
            done = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                   '--state', str(self.runtime), 'context', *args, '--no-sync'],
                                  capture_output=True, text=True, encoding='utf-8', env=inherited_env())
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)
        out = context('lasagna recipe tonight')
        self.assertEqual((out['records'], out['abstained']), ([], True))
        self.assertEqual([r['id'] for r in context('deploy schedule')['records']][:2], ['ops', 'lab'])
        self.assertEqual([r['id'] for r in context('deploy schedule', '--project', 'school')['records']][0], 'lab')

    def test_one_word_query_keeps_its_single_match(self):
        self.assertEqual(self.ids('recipe'), ['cooking'])

    def test_word_naming_the_records_project_admits_it(self):
        # "garden" names the garden project, so its card qualifies on that word alone,
        # while the weekly note that shares only "update" does not.
        self.assertEqual(self.ids('garden update'), ['garden'])

    def test_strict_path_is_unchanged(self):
        strict = [r['id'] for r in self.store.candidates('deploy schedule', strict=True)]
        self.store.DOMINANT_PROJECT_WEIGHT = 1.0
        self.store.EXPLICIT_MIN_SHARED = 1
        self.assertEqual(strict, [r['id'] for r in self.store.candidates('deploy schedule', strict=True)])
        self.assertEqual(strict[0], 'lab')


if __name__ == '__main__':
    unittest.main()
