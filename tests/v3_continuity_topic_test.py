"""A follow-up is familiar only to the passage the session actually received."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_continuity as continuity


class ContinuityTopicTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='v3-continuity-topic-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(Path(temporary.name) / 'state', self.vault)
        self.rollback = 'Quartz rollback switch uses amber profile.'
        self.deployment = 'Copper deployment restart uses indigo protocol.'
        body = ('# Manual\n\n## Rollback\n\n' + self.rollback
                # Whole-source spare-room delivery is valid. Keep this fixture
                # larger than its envelope so it tests an actually unseen section.
                + '\n\n## Background\n\n' + 'Unrelated furniture records. ' * 240
                + '\n\n## Deployment\n\n' + self.deployment + '\n')
        (self.vault / 'guide.md').write_text(body, encoding='utf-8')
        self.store.ingest(dict(id='guide', title='Manual', source='guide.md', text=body))
        query = 'quartz rollback switch'
        context = self.store.context_for('claude', query, strict=True, budget_chars=5000)
        text, delivered = runtime.render_context(context, 5000)
        self.assertIn(self.rollback, text)
        self.assertNotIn(self.deployment, text)
        continuity.remember(self.store, 'claude', 'session', query, delivered, now=100)

    def resolve(self, query, local):
        return continuity.resolve(self.store, 'claude', 'session', query, local,
                                  budget_chars=5000, now=101)

    def test_new_section_words_keep_current_local_evidence(self):
        query = 'copper deployment anlat'
        local = self.store.context_for('claude', query, strict=True, budget_chars=5000)
        self.assertIn(self.deployment, runtime.render_context(local, 5000)[0])
        result, inherited = self.resolve(query, local)
        self.assertFalse(inherited)
        self.assertIs(result, local)
        self.assertIn(self.deployment, runtime.render_context(result, 5000)[0])

    def test_vague_deictic_followup_preserves_delivered_passage(self):
        for query in ('bunu detaylandır', 'şunu kontrol et', 'explain this'):
            with self.subTest(query=query):
                result, inherited = self.resolve(query, runtime.pack_context([]))
                self.assertTrue(inherited)
                text, _ = runtime.render_context(result, 5000)
                self.assertIn(self.rollback, text)
                self.assertNotIn(self.deployment, text)

    def test_concrete_words_in_delivered_passage_still_allow_followup(self):
        result, inherited = self.resolve('amber switch anlat', runtime.pack_context([]))
        self.assertTrue(inherited)
        text, _ = runtime.render_context(result, 5000)
        self.assertIn(self.rollback, text)
        self.assertNotIn(self.deployment, text)


if __name__ == '__main__':
    unittest.main()
