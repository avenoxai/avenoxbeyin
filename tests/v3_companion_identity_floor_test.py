"""A vault that keeps only Soul.md gives it the identity floor Core.md gets (#591)."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion

DIRECTORY = 'Companion'
IDENTITY_BODY = '# Kimlik\n' + 'IDENTITY_CANARY: Kullanıcının düşünme ortağı ve uzun kimlik ilkeleri. ' * 60
BODIES = {
    'Kurallar.md': '# Kurallar\n' + ''.join(f'- kural {i}: kritik çalışma kuralı.\n' for i in range(150)),
    'Last-Session.md': '# Son oturum\n' + 'HANDOFF_CANARY: Ayrıntılı oturum devir kartı ve yapılan işler. ' * 60,
    'Threads.md': '# Konular\n## Active Threads\n' + 'THREAD_CANARY: Çok oturum süren önemli proje başlığı. ' * 60,
    # Same length as the identity body: an unfloored file of that size is the yardstick.
    'Journal.md': '# Journal\n## 2026-10-12\n' + 'X' * (len(IDENTITY_BODY) - len('## 2026-10-12\n')),
}
BUDGETS = (2000, 3000, 5000, 8000)


def sections(text):
    parts = re.split(r'\n\[' + DIRECTORY + r'/([^\]\n]+)\]\n', text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


class IdentityFloorTest(unittest.TestCase):
    def store(self, identity, stale=()):
        tmp = tempfile.TemporaryDirectory(prefix='identity-floor-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault = root / 'vault'
        (vault / DIRECTORY).mkdir(parents=True)
        store = runtime.MemoryStore(root / 'state', vault)
        for name, body in dict({name: IDENTITY_BODY for name in identity}, **BODIES).items():
            source = f'{DIRECTORY}/{name}'
            (vault / source).write_text(body, encoding='utf-8')
            store.ingest(dict(id=source, source=source, text=body, kind='note', status='active',
                              updated_at='2026-10-01T00:00:00Z'))
        for name in stale:
            # Edited after the last sync: the snapshot excludes it as stale.
            (vault / DIRECTORY / name).write_text(IDENTITY_BODY + 'edit\n', encoding='utf-8')
        return store

    def opening(self, store, budget):
        text = companion.context(store, budget, '0' * 24, 'claude')
        self.assertLessEqual(len(text), budget)
        return sections(text)

    def test_soul_alone_gets_the_identity_floor_core_gets(self):
        core, soul = self.store(['Core.md']), self.store(['Soul.md'])
        for budget in BUDGETS:
            with self.subTest(budget=budget):
                core_open, soul_open = self.opening(core, budget), self.opening(soul, budget)
                self.assertIn('IDENTITY_CANARY', soul_open['Soul.md'])
                # Both identity files are clipped, so the floor decides their share.
                self.assertLess(len(soul_open['Soul.md']), len(IDENTITY_BODY))
                self.assertEqual(len(soul_open['Soul.md']), len(core_open['Core.md']))
                # An unfloored file of the same size gets less than the identity file; the
                # water-fill order alone can give the earlier file one character more.
                self.assertGreater(len(soul_open['Soul.md']), len(soul_open['Journal.md']) + 1)

    def test_soul_beside_core_stays_an_unfloored_style_note(self):
        # #316: with both files the floor stays on Core.md; Soul.md shares like Journal.md.
        both = self.store(['Core.md', 'Soul.md'])
        for budget in BUDGETS:
            with self.subTest(budget=budget):
                opened = self.opening(both, budget)
                self.assertLessEqual(abs(len(opened['Soul.md']) - len(opened['Journal.md'])), 1)
                self.assertGreater(len(opened['Core.md']), len(opened['Soul.md']) + 1)

    def test_soul_takes_the_floor_while_core_is_excluded_as_stale(self):
        # The floor belongs to the identity that opens: a stale Core.md stays out and is listed.
        store = self.store(['Core.md', 'Soul.md'], stale=['Core.md'])
        for budget in BUDGETS:
            with self.subTest(budget=budget):
                text = companion.context(store, budget, '0' * 24, 'claude')
                self.assertIn('Missing/unavailable companion sources: Core.md.\n', text)
                opened = sections(text)
                self.assertNotIn('Core.md', opened)
                self.assertGreater(len(opened['Soul.md']), len(opened['Journal.md']) + 1)


if __name__ == '__main__':
    unittest.main()
