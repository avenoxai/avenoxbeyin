"""Fenced example headings never split live thread or handoff cards. Synthetic only."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_companion as companion


class FenceHeadingsTest(unittest.TestCase):
    CASES = (
        ('```markdown', '```not-a-close', '```'),
        ('~~~markdown', '~~~ not-a-close', '~~~'),
        ('````markdown', '```', '`````'),
        ('```markdown', '~~~', '```'),
        ('   ```markdown', '   ``` not-a-close', '   ``` \t'),
    )

    def test_heading_offsets_ignore_examples_until_a_bare_matching_close(self):
        for level in (2, 3):
            for newline in ('\n', '\r\n'):
                for opener, invalid_close, closer in self.CASES:
                    with self.subTest(level=level, newline=newline, opener=opener,
                                      invalid_close=invalid_close):
                        first, example, later = ('#' * level + ' ' + label
                                                 for label in ('Current', 'Example', 'Later'))
                        text = newline.join((first, opener, invalid_close, example,
                                             'Example content.', closer, later, 'Live content.'))
                        self.assertEqual(companion.headings(text, level),
                                         [text.index(first), text.index(later)])

    def test_latest_handoff_keeps_current_tail_after_a_suffixed_fence(self):
        for opener, invalid_close, closer in self.CASES:
            with self.subTest(opener=opener, invalid_close=invalid_close):
                current = ('# Handoff\n## 2026-11-10 12:00\nCurrent work.\n' + opener + '\n' +
                           invalid_close + '\n## 2026-01-01 Example\nExample card.\n' + closer +
                           '\nCURRENT_NEXT_STEP: request the review.\n')
                text = current + '\n## 2026-11-09 12:00\nOLDER_HANDOFF_CANARY\n'
                self.assertEqual(companion.handoff_card(text), current)
                self.assertEqual(companion.latest_handoff(text), current)

    def test_unterminated_example_does_not_create_another_handoff(self):
        text = ('## 2026-11-10 12:00\nCurrent work.\n```markdown\n```not-a-close\n'
                '## 2026-01-01 Example\nThis heading is still in the example.\n')
        self.assertEqual(companion.headings(text, 2), [0])
        self.assertEqual(companion.latest_handoff(text), text)

    def test_thread_cards_use_real_topic_boundaries_after_a_suffixed_fence(self):
        text = ('## Active Threads\n### Current\nStatus: current.\nNext: review.\n\n'
                '```markdown\n```not-a-close\n### Example\nExample text.\n```\n\n'
                '### Later\nStatus: waiting.\nNext: follow up.\n')
        prefix, cards = companion.thread_cards(text)
        self.assertEqual(prefix, '## Active Threads\n')
        self.assertEqual([whole.split('\n', 1)[0] for whole, _ in cards],
                         ['### Current', '### Later'])
        self.assertIn('### Example', cards[0][0])  # The full card keeps its source example.
        self.assertEqual(companion.thread_view(text, len(text)), text)


if __name__ == '__main__':
    unittest.main()
