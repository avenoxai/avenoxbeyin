"""Priority opening (#528) must not waste room the protected core leaves over.

Synthetic sources only. The overview and the newest handoff card are a floor, not a
ceiling: older parallel-session cards and whole topic cards still arrive when they fit,
and every cut carries an explicit marker.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion

DIRECTORY = 'Companion'


class OpeningRoomTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opening-room-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / DIRECTORY).mkdir(parents=True)
        self.store = runtime.MemoryStore(root / 'state', self.vault)

    def write(self, name, text):
        source = f'{DIRECTORY}/{name}'
        (self.vault / source).write_text(text, encoding='utf-8')
        self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                               updated_at='2026-11-01T00:00:00Z'))

    def seed(self, history=40, second_card=80):
        self.rules = [f'- Kural {i}: Şifre ve Müşteri ayrıntısını koru, sentetik talimat {i}.' for i in range(22)]
        self.write('Kurallar.md', '# Kurallar\n' + '\n'.join(
            rule + ' **neden:** ' + 'Gerekçe ayrıntısı. ' * 12 for rule in self.rules) + '\n')
        self.write('Core.md', '# Kimlik\n' + 'Kimlik ayrıntısı. ' * 120)
        self.write('Threads.md', '## Active Threads\n' + '\n\n'.join(
            f'### Konu {i}\n**Durum:** DURUM_{i} hazır.\n**Sonraki adım:** ADIM_{i} çalıştır.\n\n'
            + f'GECMIS_{i} ' + 'Geçmiş ayrıntı. ' * history for i in range(9)) + '\n## Closed Threads\n')
        self.card_a = '## 2026-11-01 10:00 · a · 11111111\nKART_A ' + 'devir. ' * 80 + '\n'
        self.card_b = '## 2026-11-01 09:00 · b · 22222222\nKART_B ' + 'devir. ' * second_card + 'KART_B_SON\n'
        self.write('Last-Session.md', '# Son oturum\n' + self.card_a + self.card_b)
        self.write('Journal.md', '# Journal\n## 2026-11-01\n' + 'Gözlem. ' * 100)

    def test_room_after_the_overview_keeps_older_handoff_cards(self):
        self.seed()
        text = companion.context(self.store, 9500, 's' * 24, 'hermes')
        self.assertLessEqual(len(text), 9500)
        for rule in self.rules:
            self.assertIn(rule, text)
        for i in range(9):
            self.assertIn(f'DURUM_{i}', text)
            self.assertIn(f'ADIM_{i}', text)
        self.assertIn(self.card_a, text)
        # A parallel session's card fits in the room the overview left: it is not dropped.
        self.assertIn(self.card_b, text)
        self.assertLess(text.index(self.card_a), text.index(self.card_b))

    def test_whole_topic_cards_fill_room_the_overview_leaves(self):
        self.seed(history=8)
        # Every source fits at 9,500; at 7,000 the topics need their overview.
        self.assertNotIn('Thread overview', companion.context(self.store, 9500, 's' * 24, 'hermes'))
        text = companion.context(self.store, 7000, 's' * 24, 'hermes')
        self.assertLessEqual(len(text), 7000)
        for i in range(9):
            self.assertIn(f'DURUM_{i}', text)
            self.assertIn(f'ADIM_{i}', text)
        shown = [i for i in range(9) if f'GECMIS_{i}' in text]
        self.assertTrue(shown, 'room after the overview should carry whole topic cards')
        self.assertIn('Thread overview: history omitted', text)
        for i in shown:
            # A topic card arrives whole or as its overview, never cut in its history.
            card = f'GECMIS_{i} ' + 'Geçmiş ayrıntı. ' * 8
            self.assertIn(card.rstrip(), text)

    def test_cut_handoff_detail_is_marked_and_the_newest_card_stays_whole(self):
        self.seed(second_card=900)
        for budget in (5000, 7000, 9500):
            with self.subTest(budget=budget):
                text = companion.context(self.store, budget, 's' * 24, 'hermes')
                self.assertLessEqual(len(text), budget)
                self.assertIn(self.card_a, text)
                self.assertNotIn('KART_B_SON', text)
                section = text[text.index(f'[{DIRECTORY}/Last-Session.md]'):]
                section = section[:section.find(f'\n[{DIRECTORY}/', 1)]
                self.assertIn('[truncated: read source]', section)

    def test_thread_view_is_whole_overview_or_marked(self):
        text = '## Active Threads\n' + '\n\n'.join(
            f'### Topic {i}\nStatus: ready {i}\nNext: verify {i}\n\nHISTORY_{i} ' + 'old ' * 30 for i in range(5))
        overview = companion.thread_overview(text)
        self.assertEqual(companion.thread_view(text, len(text)), text)
        self.assertEqual(companion.thread_view(text, len(overview)), overview)
        for budget in range(0, len(text) + 1, 37):
            with self.subTest(budget=budget):
                view = companion.thread_view(text, budget)
                self.assertLessEqual(len(view), budget)
                self.assertEqual(view, companion.thread_view(text, budget))
                if len(overview) <= budget < len(text):
                    for i in range(5):
                        self.assertIn(f'Status: ready {i}', view)
                        self.assertIn(f'Next: verify {i}', view)
                    self.assertIn('read source', view)


if __name__ == '__main__':
    unittest.main()
