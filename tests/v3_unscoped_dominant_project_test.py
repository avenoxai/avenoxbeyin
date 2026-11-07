#!/usr/bin/env python3
"""Unscoped explicit context: opt-in dominant-project demotion and two-word admission (#532).

Both rules are off unless the runtime retrieval.json turns them on; the default
`beyin.py context` result is the same as before.
"""
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
QUERIES = ('deploy schedule', 'lasagna recipe tonight', 'garden update', 'recipe', 'reciep',
           'school deploy schedule', 'invoice archive', 'bode plot phase margin')
OPT_IN = {'unscoped_min_shared': 2, 'unscoped_dominant_weight': 0.25}


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
        # One course project holds 22 of 27 notes, far above half of the index.
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
        # Two closed tasks; the one outside the course project matches the query more strongly.
        self.ingest('closed-school', 'Invoice archive closed. Invoice archive.', 'school/old-invoice.md',
                    project='school', kind='task', status='done')
        self.ingest('closed-home', 'Invoice archive closed, invoice archive done, invoice archive, invoice.',
                    'notes/old-invoice.md', kind='task', status='done')

    def ingest(self, id, text, source, project=None, kind='note', status='active'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        record = {'id': id, 'kind': kind, 'status': status, 'visibility': 'internal',
                  'text': text, 'facts': {}, 'source': source, 'updated_at': '2026-09-01T10:00:00Z'}
        if project:
            record['project'] = project
        self.store.ingest(record)

    def settings(self, value):
        (self.store.state_dir / 'retrieval.json').write_text(json.dumps(value), encoding='utf-8')

    def ids(self, query, **kwargs):
        kwargs.setdefault('explicit', True)
        return [r['id'] for r in self.store.retrieve(query, **kwargs)['records']]

    def test_default_explicit_context_is_unchanged(self):
        # No retrieval.json, an unrelated one, or invalid values: explicit lookups rank like before.
        for value in (None, {'strict_floor': 0.25}, {'unscoped_min_shared': 3, 'unscoped_dominant_weight': 0},
                      {'unscoped_min_shared': True, 'unscoped_dominant_weight': 1.5},
                      {'unscoped_min_shared': '2', 'unscoped_dominant_weight': '0.25'}):
            if value is not None:
                self.settings(value)
            with self.subTest(settings=value):
                for query in QUERIES:
                    self.assertEqual(self.ids(query), self.ids(query, explicit=False), query)
        self.assertEqual(self.ids('deploy schedule')[:2], ['lab', 'ops'])
        self.assertEqual(self.ids('lasagna recipe tonight'), ['cooking'])

    def test_settings_parse_the_opt_in_keys(self):
        import beyin_v3_passage as passage
        self.assertEqual(self.store._unscoped_settings(), (1, 1.0))
        self.settings(dict(OPT_IN, strict_floor=0.3))
        self.assertEqual(passage.unscoped_settings(self.store.state_dir), {'min_shared': 2, 'dominant_weight': 0.25})
        self.assertEqual(passage.settings(self.store.state_dir)['floor'], 0.3)  # the strict keys still apply
        self.assertEqual(self.store._unscoped_settings(), (2, 0.25))
        self.settings({'unscoped_dominant_weight': 1})
        self.assertEqual(self.store._unscoped_settings(), (1, 1.0))

    def test_dominant_project_is_demoted_not_removed(self):
        # By term frequency alone the lab note wins; the shared note is the better answer
        # for an unscoped question, and the lab note stays in the result.
        self.settings({'unscoped_dominant_weight': 0.25})
        self.assertEqual(self.ids('deploy schedule')[:2], ['ops', 'lab'])
        # Demotion alone keeps the one-word overlap (admission is a separate setting).
        self.assertEqual(self.ids('lasagna recipe tonight'), ['cooking'])

    def test_query_naming_the_project_is_not_demoted(self):
        self.settings(OPT_IN)
        self.assertEqual(self.ids('school deploy schedule')[0], 'lab')

    def test_scoped_query_is_unchanged(self):
        before = self.ids('deploy schedule', project='school')
        self.settings(OPT_IN)
        self.assertEqual(self.ids('deploy schedule', project='school'), before)
        self.assertEqual(before[0], 'lab')

    def test_small_index_has_no_dominant_project(self):
        self.settings(OPT_IN)
        self.store.DOMINANT_PROJECT_MIN_RECORDS = 1000
        self.assertEqual(self.ids('deploy schedule')[:2], ['lab', 'ops'])

    def test_demotion_scales_only_the_lexical_weight(self):
        # A closed task carries a negative weight; scaling the whole weight would raise the
        # demoted one above the stronger match outside the dominant project.
        before = self.ids('invoice archive')
        self.assertEqual(before[:2], ['closed-home', 'closed-school'])
        self.settings(OPT_IN)
        self.assertEqual(self.ids('invoice archive')[:2], ['closed-home', 'closed-school'])

    def test_out_of_scope_question_abstains(self):
        # One incidental shared word ("recipe") is not enough for a multiword question.
        self.settings({'unscoped_min_shared': 2})
        result = self.store.retrieve('lasagna recipe tonight', explicit=True)
        self.assertEqual(result['records'], [])
        self.assertTrue(result['abstained'])
        # Admission alone does not reorder.
        self.assertEqual(self.ids('deploy schedule')[:2], ['lab', 'ops'])

    def test_plain_retrieve_and_candidates_are_unchanged(self):
        # Companion retrieval and the advisor pools call these without explicit=True.
        self.settings(OPT_IN)
        self.assertEqual(self.ids('lasagna recipe tonight', explicit=False), ['cooking'])
        self.assertEqual([r['id'] for r in self.store.candidates('lasagna recipe tonight')], ['cooking'])
        self.assertEqual(self.ids('deploy schedule', explicit=False)[:2], ['lab', 'ops'])

    def test_context_command_applies_the_rules_only_when_opted_in(self):
        def context(*args):
            done = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                   '--state', str(self.runtime), 'context', *args, '--no-sync'],
                                  capture_output=True, text=True, encoding='utf-8', env=inherited_env())
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)
        out = context('lasagna recipe tonight')
        self.assertEqual(([r['id'] for r in out['records']], out['abstained']), (['cooking'], False))
        self.assertEqual([r['id'] for r in context('deploy schedule')['records']][:2], ['lab', 'ops'])
        self.settings(OPT_IN)
        out = context('lasagna recipe tonight')
        self.assertEqual((out['records'], out['abstained']), ([], True))
        self.assertEqual([r['id'] for r in context('deploy schedule')['records']][:2], ['ops', 'lab'])
        self.assertEqual([r['id'] for r in context('deploy schedule', '--project', 'school')['records']][0], 'lab')

    def test_one_word_query_keeps_its_single_match(self):
        self.settings(OPT_IN)
        self.assertEqual(self.ids('recipe'), ['cooking'])

    def test_one_word_typo_keeps_its_single_match(self):
        # "reciep" also searches "recipe"; the fix stands for the one query word, so the
        # two-word gate must not count the misspelling as a second word.
        self.assertEqual(self.ids('reciep', explicit=False), ['cooking'])
        self.settings(OPT_IN)
        self.assertEqual(self.ids('reciep'), ['cooking'])

    def test_word_naming_the_records_project_admits_it(self):
        # "garden" names the garden project, so its card qualifies on that word alone,
        # while the weekly note that shares only "update" does not.
        self.settings(OPT_IN)
        self.assertEqual(self.ids('garden update'), ['garden'])

    def test_scope_file_folder_decides_the_project(self):
        # Records without a project field belong to the project their folder maps to in
        # .beyin-projects.json; the folder name and the project id both name it.
        root = Path(self.tmp.name) / 'scoped'
        vault = root / 'vault'
        vault.mkdir(parents=True)
        store = self.module.MemoryStore(root / 'runtime', vault)
        self.addCleanup(lambda: evaluator.close_store(store))
        notes = [(f'ders{i}', f'Ders notu: {topic}. Örnek çözümler: {topic}.', f'Ödev Notları/{i:02d}.md')
                 for i, topic in enumerate(TOPICS)]
        notes += [('lab', 'Lab kit deploy schedule. Deploy schedule for the lab kits: deploy each kit on schedule.',
                   'Ödev Notları/lab.md'),
                  ('ops', 'Production releases: we deploy on Fridays, see the schedule page. Rollback owner.',
                   'knowledge/releases.md'),
                  ('weekly', 'Weekly update: hiring, offsite, travel budget.', 'notes/weekly.md')]
        for id, text, source in notes:
            path = vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            store.ingest({'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal', 'text': text,
                          'facts': {}, 'source': source, 'updated_at': '2026-09-01T10:00:00Z'})
        (vault / '.beyin-projects.json').write_text(json.dumps({'folders': {'Ödev Notları': 'İşletme'}}),
                                                    encoding='utf-8')
        ids = lambda query: [r['id'] for r in store.retrieve(query, explicit=True)['records']]
        self.assertEqual(ids('deploy schedule')[:2], ['lab', 'ops'])
        (store.state_dir / 'retrieval.json').write_text(json.dumps({'unscoped_dominant_weight': 0.25}), encoding='utf-8')
        self.assertEqual(ids('deploy schedule')[:2], ['ops', 'lab'])
        for named in ('ÖDEV deploy schedule', 'odev deploy schedule', 'İŞLETME deploy schedule', 'işletme deploy schedule'):
            self.assertEqual(ids(named)[0], 'lab', named)

    def test_strict_path_is_unchanged(self):
        strict = [r['id'] for r in self.store.candidates('deploy schedule', strict=True)]
        self.settings(OPT_IN)
        self.assertEqual(strict, [r['id'] for r in self.store.candidates('deploy schedule', strict=True)])
        self.assertEqual(strict[0], 'lab')


if __name__ == '__main__':
    unittest.main()
