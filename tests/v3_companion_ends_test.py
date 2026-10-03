"""Both-ends clipping of a rule set: whole lines only, and the budget is used (#151)."""
from pathlib import Path
import random
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_companion import clip, ends

MARKER = re.compile(r'\n\[truncated: (rules \d+-\d+ \(\d+ of \d+\)|\d+ characters) omitted here; read source\]\n')


def split(text, result):
    match = MARKER.search(result)
    head, closing = result[:match.start()], result[match.end():]
    return head, closing, match[1]


class EndsTest(unittest.TestCase):
    def rules(self, seed, count, widths):
        rng = random.Random(seed)
        return '# Kurallar\n' + ''.join(f'- kural {i}: ' + 'x' * rng.randint(0, rng.choice(widths)) + '\n'
                                        for i in range(count))

    def test_text_that_fits_is_returned_untouched(self):
        text = self.rules(1, 5, (40,))
        self.assertEqual(ends(text, len(text)), text)
        self.assertEqual(ends(text, len(text) + 500), text)

    def test_ends_keep_whole_lines_and_leave_no_whole_line_unused(self):
        cases = 0
        for seed in range(400):
            text = self.rules(seed, 10 + seed % 90, (20, 60, 200))
            budget = random.Random(seed).randint(200, len(text) - 1)
            result = ends(text, budget)
            if result is None:
                continue
            cases += 1
            with self.subTest(seed=seed, budget=budget):
                self.assertLessEqual(len(result), budget)
                head, closing, named = split(text, result)
                self.assertTrue(text.startswith(head))
                self.assertTrue(text.endswith(closing))
                # The marker names the rules that start in the gap, or the characters
                # when no rule starts there.
                start = len(text) - len(closing)
                self.assertGreater(start - len(head), 0)
                rules = [match.start() for match in re.finditer(r'^- ', text, re.M)]
                kept = sum(rule < len(head) for rule in rules)
                dropped = sum(len(head) <= rule < start for rule in rules)
                self.assertEqual(named, f'rules {kept + 1}-{kept + dropped} ({dropped} of {len(rules)})'
                                 if dropped else f'{start - len(head)} characters')
                # Whole lines, unless one line alone is longer than its end's share.
                self.assertTrue(head.endswith('\n') or '\n' not in head)
                self.assertTrue(text[:len(text) - len(closing)].endswith('\n') or '\n' not in closing[:-1])
                # Maximal: neither the next opening line nor the previous closing line fits
                # in what the widest possible marker leaves.
                total = len(rules)
                widest = max(len(f'\n[truncated: {len(text)} characters omitted here; read source]\n'),
                             len(f'\n[truncated: rules {total}-{total} ({total} of {total}) omitted here; read source]\n'))
                slack = budget - widest - len(head) - len(closing)
                following = text[len(head):text.find('\n', len(head)) + 1]
                previous = text[text.rfind('\n', 0, start - 1) + 1:start]
                self.assertTrue(len(following) >= start - len(head) or len(following) > slack, following)
                self.assertTrue(len(previous) >= start - len(head) or len(previous) > slack, previous)
        self.assertGreater(cases, 300)

    def test_short_lines_fill_the_budget(self):
        text = ''.join(f'- kural {i:03d}: kisa bir kural\n' for i in range(300))
        for budget in (500, 1000, 2000, 3000, 5000):
            with self.subTest(budget=budget):
                result = clip(text, budget, both=True)
                self.assertLessEqual(len(result), budget)
                self.assertGreaterEqual(len(result), budget - 2 * 26)
                self.assertIn('kural 000', result)
                self.assertIn('kural 299', result)

    def test_marker_names_the_omitted_rules(self):
        total = 30
        text = ''.join(f'- kural {i:02d}: kisa bir kural\n' for i in range(1, total + 1))
        result = ends(text, 600)
        match = re.search(r'\n\[truncated: rules (\d+)-(\d+) \((\d+) of (\d+)\) omitted here; read source\]\n', result)
        self.assertIsNotNone(match, result)
        first, last, omitted, counted = (int(group) for group in match.groups())
        kept_before = result[:match.start()].count('- kural')
        kept_after = result[match.end():].count('- kural')
        self.assertEqual(counted, total)
        self.assertEqual(first, kept_before + 1)
        self.assertEqual(last, total - kept_after)
        self.assertEqual(omitted, last - first + 1)
        self.assertLessEqual(len(result), 600)

    def test_text_without_list_items_keeps_the_character_count(self):
        text = ''.join(f'duz paragraf satiri {i:02d}\n' for i in range(40))
        head, closing, named = split(text, ends(text, 600))
        self.assertEqual(named, f'{len(text) - len(head) - len(closing)} characters')


if __name__ == '__main__':
    unittest.main()
