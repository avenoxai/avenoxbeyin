"""A post under the pointer (X, Instagram) is named by its own permalink, never by a guess."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y

POST = 'https://x.com/Avenoxai/status/2111140017993310368'
QUOTE = 'https://x.com/thsottiaux/status/2111244596131111253'


class PostPermalinkTest(unittest.TestCase):
    def test_x_post_and_its_sub_pages_name_the_post(self):
        for href in (POST, POST + '/photo/1', POST + '/analytics', POST + '?s=20',
                     'https://twitter.com/Avenoxai/status/2111140017993310368',
                     'https://mobile.x.com/Avenoxai/status/2111140017993310368/video/1'):
            with self.subTest(href=href):
                self.assertEqual(y._post_permalink(href), POST)

    def test_instagram_post_and_reel_forms(self):
        self.assertEqual(y._post_permalink('https://www.instagram.com/p/DechiHuBrrb/'),
                         'https://www.instagram.com/p/DechiHuBrrb/')
        self.assertEqual(y._post_permalink('https://www.instagram.com/someone/reel/Dd-T7Z5R_2R/?igsh=1'),
                         'https://www.instagram.com/reel/Dd-T7Z5R_2R/')
        self.assertEqual(y._post_permalink('https://instagram.com/reels/Dd-T7Z5R_2R/'),
                         'https://www.instagram.com/reel/Dd-T7Z5R_2R/')

    def test_other_links_are_no_posts(self):
        for href in ('https://x.com/home', 'https://x.com/Avenoxai', 'https://www.instagram.com/reels/',
                     'https://www.instagram.com/avenoxyz/', 'https://example.org/x.com/a/status/1',
                     'http://x.com/a/status/1', '', None):
            with self.subTest(href=href):
                self.assertIsNone(y._post_permalink(href))


class QuotedPostTest(unittest.TestCase):
    def test_quote_is_offered_unselected_on_its_own_key(self):
        sources = y.windows_sources({'url_imlec': POST, 'url': 'https://x.com/home', 'url_alinti': QUOTE}, '', False)
        quote = next(source for source in sources if source['value'] == QUOTE)
        self.assertEqual((quote['label'], quote['key'], quote['on']), ('Alıntılanan gönderi', 'a', False))
        self.assertEqual([source['value'] for source in sources if source['on']], [POST])

    def test_hovered_link_reports_the_quote_only_when_asked(self):
        reading = {'value': POST, 'name': 'Avenox', 'quote': QUOTE}
        with patch.object(y, '_windows_read', return_value=reading):
            extra = {}
            self.assertEqual(y.windows_hovered_link(42, 'chrome', extra=extra), (POST, 'Avenox'))
            self.assertEqual(extra, {'quote': QUOTE})
            self.assertEqual(y.windows_hovered_link(42, 'chrome'), (POST, 'Avenox'))

    def test_quote_without_a_post_is_dropped(self):
        with patch.object(y, '_windows_read', return_value={'value': None, 'quote': QUOTE}):
            extra = {}
            self.assertEqual(y.windows_hovered_link(42, 'chrome', extra=extra), (None, None))
            self.assertEqual(extra, {})

    def test_opera_snapshot_quote_reaches_the_hovered_link(self):
        snapshot = {'value': 'https://x.com/home', 'hover_value': POST, 'hover_name': 'Avenox', 'hover_quote': QUOTE}
        with patch.object(y, '_windows_opera_snapshot', return_value=snapshot):
            extra = {}
            self.assertEqual(y.windows_hovered_link(42, 'opera', extra=extra), (POST, 'Avenox'))
            self.assertEqual(extra, {'quote': QUOTE})


class PostTitleTest(unittest.TestCase):
    def test_x_title_is_handle_and_text_after_the_time_link(self):
        article = 'Varun Mohan Verified account @_mohansolo 2 hours ago Lots of excitement   for Argon'
        self.assertEqual(y._post_title('https://x.com/_mohansolo/status/1', article, '2 hours ago'),
                         '@_mohansolo: Lots of excitement for Argon')

    def test_localised_or_missing_time_keeps_the_article_text(self):
        article = 'Varun Mohan Onaylanmış hesap @_mohansolo 2 saat Metin'
        self.assertEqual(y._post_title('https://x.com/_mohansolo/status/1', article, '2 saat'), '@_mohansolo: Metin')
        self.assertEqual(y._post_title('https://x.com/_mohansolo/status/1', article, ''), article)
        self.assertEqual(y._post_title('https://x.com/_mohansolo/status/1', 'Varun 2h', '2h'), '@_mohansolo')

    def test_instagram_keeps_text_and_titles_are_bounded(self):
        self.assertEqual(y._post_title('https://www.instagram.com/p/abcde/', 'Photo by VL STUDIO', 'x'), 'Photo by VL STUDIO')
        self.assertEqual(len(y._post_title('https://www.instagram.com/p/abcde/', 'a' * 500)), 200)


if __name__ == '__main__':
    unittest.main()
