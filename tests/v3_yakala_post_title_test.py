"""Standalone regressions extracted from Yakala agent review (#384).

Synthetic input and Win32 call models only; no agent client is required.
"""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y  # noqa: E402


class PostTitleReviewTest(unittest.TestCase):
    def test_time_inside_handle_or_display_name_is_not_the_split(self):
        cases = [('abc2h', 'Author @abc2h · 2h Actual post'),
                 ('normal', '2h channel @normal · 2h Actual post'),
                 ('normal', 'Channel 2h @normal · 2h Actual post'),
                 ('normal', 'Channel 2h @normal 2h Actual post'),
                 ('ABC2h', 'Author @abc2H · 2h Actual post')]
        for handle, article in cases:
            with self.subTest(article=article):
                self.assertEqual(y._post_title('https://x.com/' + handle + '/status/1', article, '2h'),
                                 '@' + handle + ': Actual post')

    def test_missing_time_after_handle_keeps_full_article(self):
        article = '2h display name @abc2h Without a time link'
        self.assertEqual(y._post_title('https://x.com/abc2h/status/1', article, '2h'), article)

    def test_handle_only_in_body_keeps_the_first_time_split(self):
        # The header omits the handle and the body mentions it after the real time.
        cases = [('Alice 2h Follow @alice', '@alice: Follow @alice'),
                 ('Alice 2h Follow @alice back in 2h please', '@alice: Follow @alice back in 2h please')]
        for article, title in cases:
            with self.subTest(article=article):
                self.assertEqual(y._post_title('https://x.com/alice/status/1', article, '2h'), title)



if __name__ == '__main__':
    unittest.main()
