#!/usr/bin/env python3
"""Non-strict retrieval ranks by BM25 over stem counts, so a long hub note cannot win every query."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v3_non_strict_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)

HUB = ('Project hub. ' + ' '.join(f'topic{i}' for i in range(400)) +
       ' Mentions in passing: deploy, calendar, budget.')
TARGET = 'Deploy calendar: releases go out on Tuesdays after the freeze.'
OTHER = 'Grocery list for the weekend: apples, bread, olive oil.'


class NonStrictRankTest(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-non-strict-rank-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = self.module.MemoryStore(root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))
        # The hub is newer, so it would also win any tie on updated_at.
        self.ingest('hub', HUB, 'projects/hub.md', '2026-10-01T10:00:00Z')
        self.ingest('target', TARGET, 'knowledge/deploy-calendar.md', '2026-09-01T10:00:00Z')
        self.ingest('grocery', OTHER, 'notes/grocery.md', '2026-09-01T10:00:00Z')

    def ingest(self, id, text, source, updated_at):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.store.ingest({'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                           'text': text, 'facts': {}, 'source': source, 'updated_at': updated_at})

    def ids(self, query, **kwargs):
        return [r['id'] for r in self.store._retrieve(query, candidate_only=True, **kwargs)]

    def test_focused_note_outranks_hub_that_shares_more_terms(self):
        # The hub shares all three terms, the target two; a raw count puts the hub first.
        self.assertEqual(self.ids('deploy calendar budget')[:2], ['target', 'hub'])

    def test_non_strict_still_returns_weak_matches(self):
        # Ordering changes, recall does not: a single shared term still qualifies.
        self.assertEqual(self.ids('budget'), ['hub'])

    def test_candidates_use_the_same_order(self):
        self.assertEqual([r['id'] for r in self.store.candidates('deploy calendar budget')][:2], ['target', 'hub'])

    def test_note_about_a_term_outranks_one_that_mentions_it(self):
        # The note about rollbacks has the larger vocabulary, so a presence-only weight
        # prefers the short mention; only how often the terms occur tells them apart.
        about = ('Rollback plan. Rollback the release first; rollback order matters. Each rollback '
                 'is verified, and the rollback plan names an owner, a window, the schema, '
                 'the cache, the queue, the dashboards and the pager rotation.')
        mention = 'Weekly sync: hiring, offsite, rollback plan.'
        self.ingest('mention', mention, 'notes/weekly-sync.md', '2026-10-02T10:00:00Z')
        self.ingest('about', about, 'knowledge/rollback.md', '2026-09-01T10:00:00Z')
        self.assertEqual(self.ids('rollback plan')[:2], ['about', 'mention'])

    def test_strict_thresholds_unchanged(self):
        self.assertEqual(self.ids('budget', strict=True), [])


if __name__ == '__main__':
    unittest.main()
