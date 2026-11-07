"""Optional knowledge routing card (#595): knowledge/INDEKS.md replaces the article list in the opening.

Real Markdown files go through the real sync, so the privacy, trust and staleness gates are
the ones the hook meets, not fake index rows."""
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_companion as companion
import beyin_v3 as runtime
from beyin_v3_sync import SyncEngine

COMPANION = '🔮 850-Companion'
NOTES = {
    'Core.md': '# Core\nKimlik notu.\n',
    'Kurallar.md': '# Kurallar\n- Genel kural bir.\n',
    'Last-Session.md': '## 2026-11-01 10:00 · etiket · abcd1234\nDevir kartı.\n',
    'Threads.md': '## Active Threads\n### Thread: T1\n**Status:** active\n',
    'Journal.md': '# Journal\n## 2026-11-01\nNot.\n',
}
ARTICLE_LIST = ('# Bilgi Tabanı İndeksi\n\n| Makale | Özet | Kaynak |\n| --- | --- | --- |\n' +
                ''.join(f'| [[concepts/makale-{i:03d}]] | LIST_ROW_{i:03d} makalenin uzun özeti burada durur. | daily |\n'
                        for i in range(150)))
CARD = ('---\ntitle: Bilgi tabanı indeksi\n---\n# Bilgi tabanı: ne zaman aç\n\n'
        '150 makale; içerikleri açılışa girmez, yalnız sorulursa aç.\n\n'
        '| Konu | Durum | Ne zaman aç |\n| --- | --- | --- |\n'
        '| [[concepts/musteri-notlari\\|Müşteri notları]] | 🟡 | CARD_CANARY Sözleşme maddesi ya da teslim tarihi konuşulursa |\n'
        '| [[concepts/arac-listesi\\|Araç listesi]] | 🟢 | Yeni bir araç seçilirken (Şifre ve Arşiv notları ayrı) |\n')


class RoutingCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-routing-card-')
        self.addCleanup(self.tmp.cleanup)
        self.vaults = 0
        self.build()

    def build(self, article_list=True):
        """A fresh vault with the companion notes and, unless asked otherwise, the article list."""
        self.vaults += 1
        root = Path(self.tmp.name) / str(self.vaults)
        self.vault, self.state = root / 'vault', root / 'state'
        (self.vault / COMPANION).mkdir(parents=True)
        (self.vault / 'knowledge').mkdir()
        self.state.mkdir()
        for name, body in NOTES.items():
            (self.vault / COMPANION / name).write_text(body, encoding='utf-8')
        if article_list:
            (self.vault / 'knowledge/index.md').write_text(ARTICLE_LIST, encoding='utf-8')
        self.store = runtime.MemoryStore(self.state, self.vault)
        self.addCleanup(self.store.close)

    def sync(self):
        result = SyncEngine(self.vault, self.state).sync()
        self.assertEqual(result['status'], 'succeeded', result)

    def opening(self, budget=10000, **kwargs):
        return companion.context(self.store, budget, 'session-1', 'claude', **kwargs)

    def test_card_replaces_the_article_list_once_at_every_budget(self):
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD, encoding='utf-8')
        self.sync()
        for budget in (1000, 2000, 3000, 5000, 8000, 10000, 16000, 24000):
            with self.subTest(budget=budget):
                text = self.opening(budget)
                self.assertLessEqual(len(text), budget)
                self.assertNotIn('LIST_ROW_', text)
                self.assertNotIn('[Knowledge map: knowledge/index.md]', text)
                if budget >= 3000:
                    self.assertIn('[Knowledge map: knowledge/INDEKS.md]\n# Bilgi tabanı: ne zaman aç', text)
                    self.assertIn('CARD_CANARY Sözleşme maddesi', text)
                    self.assertIn('Şifre ve Arşiv', text)
                    self.assertNotIn('title: Bilgi tabanı indeksi', text)  # frontmatter is not body
                    # Shown whole as the map, so the ambient snapshot does not repeat it.
                    self.assertEqual(text.count('CARD_CANARY'), 1)
                    self.assertNotIn('[Related source: knowledge/INDEKS.md]', text)

    def test_unqueried_opening_leaves_the_list_out_and_a_question_brings_it_back(self):
        # The newest note leads the unqueried snapshot, so without the card standing in for the
        # list the snapshot would add the whole article list below the card.
        (self.vault / 'knowledge/index.md').write_text(
            '---\nupdated_at: 2026-11-07T09:00:00Z\n---\n' + ARTICLE_LIST, encoding='utf-8')
        (self.vault / 'knowledge/INDEKS.md').write_text(
            CARD.replace('title: Bilgi', 'updated_at: 2026-01-01T00:00:00Z\ntitle: Bilgi'), encoding='utf-8')
        self.sync()
        snapshot = self.store.snapshot_context(budget_chars=1400)
        self.assertEqual(snapshot['records'][0]['source'], 'knowledge/index.md')
        ambient = self.opening()
        self.assertIn('[Knowledge map: knowledge/INDEKS.md]', ambient)
        self.assertNotIn('LIST_ROW_', ambient)
        self.assertNotIn('[Related source: knowledge/index.md]', ambient)
        asked = self.opening(query='makale özeti hangi makalede')
        self.assertIn('[Knowledge map: knowledge/INDEKS.md]', asked)
        self.assertIn('[Related source: knowledge/index.md]', asked)
        self.assertEqual(asked.count('CARD_CANARY'), 1)  # a card shown whole is not repeated either

    def test_without_a_card_the_article_list_stays_the_map(self):
        self.sync()
        text = self.opening()
        self.assertIn('[Knowledge map: knowledge/index.md]\n# Bilgi Tabanı İndeksi', text)
        self.assertIn('LIST_ROW_000', text)
        self.assertIn('[truncated: read source]', text)

    def test_card_alone_is_enough(self):
        self.build(article_list=False)
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD, encoding='utf-8')
        self.sync()
        text = self.opening()
        self.assertIn('[Knowledge map: knowledge/INDEKS.md]', text)
        self.assertNotIn('knowledge/index.md', text)

    def test_private_untrusted_or_changed_card_falls_back_without_leaking(self):
        cases = {
            'private': CARD.replace('title: Bilgi', 'visibility: private\ntitle: Bilgi').replace('CARD_CANARY', 'PRIVATE_CANARY'),
            'untrusted': CARD.replace('title: Bilgi', 'trust: untrusted\ntitle: Bilgi').replace('CARD_CANARY', 'UNTRUSTED_CANARY'),
        }
        for case, body in cases.items():
            with self.subTest(case=case):
                self.build()
                (self.vault / 'knowledge/INDEKS.md').write_text(body, encoding='utf-8')
                self.sync()
                for query in ('', 'müşteri sözleşme maddesi teslim tarihi'):
                    text = self.opening(query=query)
                    self.assertIn('[Knowledge map: knowledge/index.md]', text)
                    self.assertNotIn('INDEKS.md', text)
                    self.assertNotIn(case.upper() + '_CANARY', text)
        # A card edited after the last sync is not the indexed one: the map is the verified list.
        self.build()
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD, encoding='utf-8')
        self.sync()
        self.assertIn('[Knowledge map: knowledge/INDEKS.md]', self.opening())
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD.replace('CARD_CANARY', 'UNSYNCED_CANARY'), encoding='utf-8')
        text = self.opening()
        self.assertIn('[Knowledge map: knowledge/index.md]', text)
        self.assertNotIn('UNSYNCED_CANARY', text)
        self.assertNotIn('CARD_CANARY', text)

    def test_turkish_spellings_in_either_unicode_form(self):
        for name in ('İNDEKS.md', unicodedata.normalize('NFD', 'İNDEKS.md'), 'indeks.md', 'İndeks.md'):
            with self.subTest(name=name.encode()):
                self.build()
                (self.vault / 'knowledge' / name).write_text(CARD, encoding='utf-8')
                self.sync()
                text = self.opening()
                self.assertIn('CARD_CANARY', text)
                self.assertNotIn('LIST_ROW_', text)
                self.assertNotIn('[Knowledge map: knowledge/index.md]', text)

    def test_vault_without_a_card_asks_only_for_the_list(self):
        # Each requested name is another scan of the ingested records at every SessionStart.
        self.sync()
        calls, original = [], self.store.source_snapshot

        def spy(names, **kwargs):
            calls.append(list(names))
            return original(names, **kwargs)
        self.store.source_snapshot = spy
        self.assertEqual(companion.knowledge_map(self.store)['source'], 'knowledge/index.md')
        self.assertEqual(calls, [['index.md']])
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD, encoding='utf-8')
        self.sync()
        self.assertEqual(companion.knowledge_map(self.store)['source'], 'knowledge/INDEKS.md')
        self.assertEqual(calls[-1], ['INDEKS.md', 'index.md'])

    def test_cards_in_other_folders_do_not_become_the_opening_map(self):
        for folder in ('knowledge/concepts', 'projects/musteri', COMPANION):
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
            (self.vault / folder / 'INDEKS.md').write_text(CARD.replace('CARD_CANARY', 'ELSEWHERE_CANARY'),
                                                           encoding='utf-8')
        self.sync()
        text = self.opening()
        self.assertIn('[Knowledge map: knowledge/index.md]', text)
        self.assertIn('LIST_ROW_000', text)
        self.assertNotIn('[Knowledge map: knowledge/concepts/INDEKS.md]', text)

    def test_long_card_is_clipped_visibly_and_the_list_stays_out(self):
        rows = ''.join(f'| [[concepts/konu-{i:03d}]] | 🟢 | ROW_{i:03d} bu konu konuşulursa aç |\n' for i in range(200))
        (self.vault / 'knowledge/INDEKS.md').write_text(CARD + rows, encoding='utf-8')
        self.sync()
        for budget in (3000, 10000, 24000):
            with self.subTest(budget=budget):
                text = self.opening(budget)
                self.assertLessEqual(len(text), budget)
                self.assertIn('[Knowledge map: knowledge/INDEKS.md]', text)
                self.assertIn('CARD_CANARY', text)
                self.assertNotIn('ROW_199', text)
                self.assertIn('[truncated: read source]', text)
                self.assertNotIn('LIST_ROW_', text)


if __name__ == '__main__':
    unittest.main()
