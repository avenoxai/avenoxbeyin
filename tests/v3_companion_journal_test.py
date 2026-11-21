"""Journal excerpt selection: the newest entry wins in both writing directions."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('beyin_v3_companion_excerpt',
                                              ROOT / 'template/.claude/scripts/beyin_v3_companion.py')
companion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = companion
spec.loader.exec_module(companion)

LATEST = '## 2026-09-18 (öğlen{time})\nLATEST_ENTRY: bugünün son notu.\n'
EARLIER = '## 2026-09-18 (sabah{time})\nOLDER_ENTRY: bugünün ilk notu.\n'
ANCIENT = '## 2026-08-01\nANCIENT_ENTRY: eski not.\n'


def journal(newest_first, times):
    today = [LATEST.format(time=', 14:00' if times else ''), EARLIER.format(time=', 09:00' if times else '')]
    entries = today + [ANCIENT] if newest_first else [ANCIENT] + today[::-1]
    return '# Journal\n' + ''.join(entries)


class JournalExcerptTest(unittest.TestCase):
    def test_latest_same_date_entry_wins_in_every_writing_direction(self):
        for newest_first in (True, False):
            for times in (True, False):
                with self.subTest(newest_first=newest_first, times=times):
                    excerpt = companion.excerpt('Journal.md', journal(newest_first, times))
                    self.assertIn('LATEST_ENTRY', excerpt)
                    self.assertNotIn('OLDER_ENTRY', excerpt)
                    self.assertNotIn('ANCIENT_ENTRY', excerpt)

    def test_single_date_journal_picks_the_bottom_like_an_undated_one(self):
        # Every heading on one date gives no direction; the bottom goes first (#163).
        dated = '# Journal\n## 2026-09-30 (sabah)\nOLDER_ENTRY\n## 2026-09-30 (öğlen)\nLATEST_ENTRY\n'
        undated = '# Journal\n## Sabah\nOLDER_ENTRY\n## Öğlen\nLATEST_ENTRY\n'
        for text in (dated, undated):
            excerpt = companion.excerpt('Journal.md', text)
            self.assertIn('LATEST_ENTRY', excerpt)
            self.assertNotIn('OLDER_ENTRY', excerpt)

    def test_distinct_dates_and_undated_journals_are_unchanged(self):
        newest_first = '# Journal\n## 2026-09-17\nLATEST_ENTRY\n## 2020-01-01\nANCIENT_ENTRY\n'
        appended = '# Journal\n## 2020-01-01\nANCIENT_ENTRY\n## 2026-09-17\nLATEST_ENTRY\n'
        undated = '# Journal\n## İlk gözlem\nANCIENT_ENTRY\n## Sonraki gözlem\nLATEST_ENTRY\n'
        for text in (newest_first, appended, undated):
            excerpt = companion.excerpt('Journal.md', text)
            self.assertIn('LATEST_ENTRY', excerpt)
            self.assertNotIn('ANCIENT_ENTRY', excerpt)

    def test_undated_newest_entry_at_the_writing_end_wins_in_a_mixed_journal(self):
        cases = {
            # The reproduction from #134: newest undated on top of a single dated entry.
            'top_single': '# Journal\n## Yeni gözlem\nLATEST_ENTRY\n## 2026-09-20\nANCIENT_ENTRY\n',
            'bottom_single': '# Journal\n## 2026-09-20\nANCIENT_ENTRY\n## Yeni gözlem\nLATEST_ENTRY\n',
            'newest_first': '# Journal\n## Yeni gözlem\nLATEST_ENTRY\n## 2026-09-20\nANCIENT_ENTRY\n'
                            '## 2026-09-01\nANCIENT_ENTRY\n',
            'appended': '# Journal\n## 2026-09-01\nANCIENT_ENTRY\n## 2026-09-20\nANCIENT_ENTRY\n'
                        '## Yeni gözlem\nLATEST_ENTRY\n',
        }
        for label, text in cases.items():
            with self.subTest(label):
                excerpt = companion.excerpt('Journal.md', text)
                self.assertIn('LATEST_ENTRY', excerpt)
                self.assertNotIn('ANCIENT_ENTRY', excerpt)

    def test_undated_entry_away_from_the_writing_end_does_not_win(self):
        # A known direction decides the end: an undated heading on the far side is older.
        appended = ('# Journal\n## Nasıl yazılır\nANCIENT_ENTRY\n## 2026-09-01\nANCIENT_ENTRY\n'
                    '## 2026-09-20\nLATEST_ENTRY\n')
        newest_first = ('# Journal\n## 2026-09-20\nLATEST_ENTRY\n## 2026-09-01\nANCIENT_ENTRY\n'
                        '## Eski gözlem\nANCIENT_ENTRY\n')
        for text in (appended, newest_first):
            excerpt = companion.excerpt('Journal.md', text)
            self.assertIn('LATEST_ENTRY', excerpt)
            self.assertNotIn('ANCIENT_ENTRY', excerpt)

    def test_other_sources_are_returned_untouched(self):
        self.assertEqual(companion.excerpt('Core.md', '# Kimlik\n## 2026-09-18\nx\n'), '# Kimlik\n## 2026-09-18\nx\n')



class ThreadsExcerptTest(unittest.TestCase):
    """Active threads end at the closed section under every heading the engine writes."""
    def test_turkish_and_english_active_headings_stop_at_closed(self):
        for active in ('## Active Threads', '## Aktif konular', '## Açık konular', '## AÇIK KONULAR',
                       '## Acik konular', '## Açık', '## Open', '## Active topics',
                       '## Open questions', '## ACTIVE TOPICS'):
            for closed in ('## Closed Threads', '## Kapanan konular', '## Kapalı'):
                with self.subTest(active=active, closed=closed):
                    text = f'# Konular\n\n{active}\n### ACTIVE_THREAD\n\n{closed}\n### CLOSED_THREAD\n'
                    excerpt = companion.excerpt('Threads.md', text)
                    self.assertTrue(excerpt.startswith(active))
                    self.assertIn('ACTIVE_THREAD', excerpt)
                    self.assertNotIn('CLOSED_THREAD', excerpt)

    def test_heading_that_only_starts_like_open_is_not_the_active_section(self):
        for heading in ('Açıklama', 'Activity', 'Opening'):
            with self.subTest(heading=heading):
                text = f'# Konular\n## {heading}\nINTRO\n## Kapanan konular\n### CLOSED_THREAD\n'
                self.assertEqual(companion.excerpt('Threads.md', text), text)

if __name__ == '__main__':
    unittest.main()
