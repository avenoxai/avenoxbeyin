#!/usr/bin/env python3
"""Context retrieval tolerates misspelled query words (zmaan -> zaman)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v3_typo_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)

CALENDAR = 'Takvim: sonraki Claude anketi 9 Kasım, belli bir saat yok.'
GROCERY = 'Hafta sonu alışveriş listesi: elma, ekmek, zeytinyağı.'
BOARD = 'Okul kurulu toplantısı cuma günü.'


class TypoToleranceTest(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-typo-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = self.module.MemoryStore(root / 'runtime', self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))
        self.ingest('calendar', CALENDAR, 'knowledge/takvim.md')
        self.ingest('grocery', GROCERY, 'notes/alisveris.md')
        self.ingest('board', BOARD, 'notes/kurul.md')

    def ingest(self, id, text, source, **metadata):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.store.ingest({'id': id, 'kind': 'note', 'status': 'active', 'visibility': 'internal',
                           'text': text, 'facts': {}, 'source': source, 'updated_at': '2026-10-29T10:00:00Z', **metadata})

    def sources(self, query, **kwargs):
        return [c['source'] for c in self.store.retrieve(query, **kwargs)['citations']]

    def test_misspelled_words_find_the_note(self):
        self.assertEqual(self.sources('cluade ankeit ne zmana'), ['knowledge/takvim.md'])

    def test_unrelated_query_stays_empty(self):
        self.assertEqual(self.sources('kuantum fiziği ödevi'), [])

    def test_substituted_letter_is_another_word(self):
        self.assertEqual(self.sources('kuralları'), [])

    def test_strict_mode_does_not_guess(self):
        self.assertEqual(self.sources('cluade ankeit zmana', strict=True), [])

    def test_typo_candidates_keep_visibility_trust_status_and_freshness_gates(self):
        query = 'cluade ankeit zmana'
        for name, metadata in (
                ('private', {'visibility': 'private'}),
                ('untrusted', {'trust': 'untrusted'}),
                ('archived', {'status': 'archived'})):
            self.ingest(name, CALENDAR, f'notes/{name}.md', **metadata)
        self.ingest('stale', CALENDAR, 'notes/stale.md')
        (self.vault / 'notes/stale.md').write_text('Replaced source.', encoding='utf-8')
        self.assertEqual(self.sources(query), ['knowledge/takvim.md'])
        self.assertEqual(self.sources(query, audience='public'), [])

    def test_typo_candidates_cannot_cross_an_explicit_project(self):
        self.ingest('other', CALENDAR, 'notes/other-project.md', project='other')
        self.assertEqual(self.sources('cluade ankeit zmana', project='other'),
                         ['notes/other-project.md'])


if __name__ == '__main__':
    unittest.main()
