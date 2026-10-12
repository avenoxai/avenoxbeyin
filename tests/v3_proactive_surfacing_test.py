"""Unit tests for proactive / ambient context surfacing (Issue #288 Item 2)."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
from beyin_v3_hook import proactive_surfacing


class ProactiveSurfacingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-proactive-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.state.mkdir()
        self.store = runtime.MemoryStore(self.state, self.vault)

    def note(self, ident, source, text, **fields):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        record = dict(id=ident, source=source, text=text, status='active',
                      updated_at='2026-10-12T00:00:00Z', **fields)
        return self.store.ingest(record)

    def test_short_or_empty_query_abstains(self):
        self.assertEqual(proactive_surfacing(self.store, ""), [])
        self.assertEqual(proactive_surfacing(self.store, "   "), [])
        self.assertEqual(proactive_surfacing(self.store, "ab"), [])

    def test_surfaces_knowledge_and_decision_records_only(self):
        # 1. Knowledge note
        self.note('jwt-decision', 'knowledge/decisions/auth.md',
                  'JWT token refresh stratejisi ve oturum sonlandırma kararı.',
                  kind='decision', tags=['auth', 'karar'])

        # 2. General scratch note (not knowledge, not decision)
        self.note('jwt-scratch', 'inbox/scratch.md',
                  'JWT token denemesi ve geçici testler.',
                  kind='note')

        surfaced = proactive_surfacing(self.store, "JWT token stratejisi ve oturum")
        surfaced_ids = [r['id'] for r in surfaced]
        self.assertIn('jwt-decision', surfaced_ids)
        self.assertNotIn('jwt-scratch', surfaced_ids)

    def test_delivered_ids_are_excluded(self):
        self.note('cache-learning', 'knowledge/concepts/caching.md',
                  'Redis önbellek kuralı ve TTL stratejisi mimarisi.',
                  kind='learning')

        surfaced = proactive_surfacing(self.store, "Redis önbellek TTL stratejisi", delivered_ids={'cache-learning'})
        self.assertEqual(surfaced, [])

    def test_limit_is_respected(self):
        for i in range(5):
            self.note(f'arch-{i}', f'knowledge/decisions/arch-{i}.md',
                      f'Mimari karar {i} mikroservis yapısı ve iletişim.',
                      kind='decision')

        surfaced = proactive_surfacing(self.store, "Mimari karar mikroservis yapısı", limit=2)
        self.assertLessEqual(len(surfaced), 2)


if __name__ == '__main__':
    unittest.main()
