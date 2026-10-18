"""A post under the pointer (X, Instagram) is named by its own permalink, never by a guess."""
import contextlib
import ctypes as C
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

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

    def test_instagram_sound_page_is_no_post(self):
        # A reel shows its sound (/reels/audio/<id>/) as a link above the media; it is read
        # before the post's own link, so it must not turn into a made-up /reel/audio/ post.
        for href in ('https://www.instagram.com/reels/audio/1234567890123456/',
                     'https://www.instagram.com/reels/audio/', 'https://www.instagram.com/reel/audio/42/'):
            with self.subTest(href=href):
                self.assertIsNone(y._post_permalink(href))
        self.assertEqual(y._post_permalink('https://www.instagram.com/reel/AudioClip1/'),
                         'https://www.instagram.com/reel/AudioClip1/')


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


class PostSiteTest(unittest.TestCase):
    def test_only_x_and_instagram_pages_are_post_sites(self):
        for url in ('https://x.com/home', 'https://twitter.com/a', 'https://mobile.twitter.com/a/status/1',
                    'https://www.instagram.com/', 'https://instagram.com/p/abcde/', 'https://X.COM./home'):
            with self.subTest(url=url):
                self.assertTrue(y._post_site(url))
        for url in ('https://www.bbc.com/news/1', 'https://notx.com/', 'https://x.com.example.org/',
                    'https://example.org/x.com', 'https://[broken/', '', None):
            with self.subTest(url=url):
                self.assertFalse(y._post_site(url))


class UIAModel:
    """A page under the pointer: one article (the pointer is inside it) holding a tweet link,
    and no link under the pointer itself. Only in-memory objects; no UI Automation is touched."""

    POINT = [500, 400]

    def __init__(self):
        self.rects = {201: (0, 2000, 100, 20), 301: (0, 0, 1000, 1000)}  # link elsewhere, article here
        self.strings = {(202, 30045): 'https://x.com/someone/status/123', (202, 30005): '17 Eki',
                        (201, 30045): 'https://x.com/someone/status/123', (201, 30005): 'tweet',
                        (301, 30005): 'Haber metni'}
        self.arrays = {103: [201], 105: [301], 106: [202]}
        self.buffers, self.conditions = [], []
        self.user = Mock()
        self.user.WindowFromPoint.return_value = 42
        self.user.GetAncestor.return_value = 42
        self.ole = types.SimpleNamespace(CoInitializeEx=Mock(return_value=0), CoUninitialize=Mock(),
                                         CoCreateInstance=Mock(side_effect=lambda *a: setattr(a[-1]._obj, 'value', 100) or 0))
        self.auto = types.SimpleNamespace(VariantClear=Mock(return_value=0), SysAllocString=Mock(return_value=7),
                                          SafeArrayGetUBound=Mock(side_effect=self.bound),
                                          SafeArrayAccessData=Mock(side_effect=self.access),
                                          SafeArrayUnaccessData=Mock(return_value=0))

    def bound(self, array, dimension, upper):
        upper._obj.value = 3
        return 0

    def access(self, array, data):
        buffer = (C.c_double * 4)(*self.rects[array])
        self.buffers.append(buffer)
        data._obj.value = C.addressof(buffer)
        return 0

    def call(self, obj, slot, *args, **_):
        handle = obj.value
        if slot == 2:
            return 0
        if handle == 100 and slot == 6:  # ElementFromHandle
            args[-1]._obj.value = 101
        elif handle == 100 and slot == 23:  # CreatePropertyCondition: hyperlink 102, article 104
            self.conditions.append(args[0])
            args[-1]._obj.value = 102 if args[0] == 30003 else 104
        elif handle == 101 and slot == 6:  # FindAll on the page
            args[-1]._obj.value = 103 if args[1].value == 102 else 105
        elif handle == 301 and slot == 6:  # FindAll links inside the article
            args[-1]._obj.value = 106
        elif handle in self.arrays and slot == 3:
            args[0]._obj.value = len(self.arrays[handle])
        elif handle in self.arrays and slot == 4:
            args[1]._obj.value = self.arrays[handle][args[0]]
        elif slot == 10:  # GetCurrentPropertyValue
            variant = args[1]._obj
            if args[0] == 30001:
                if handle in self.rects:
                    variant.vt, variant.v1 = 0x2005, handle
            elif (handle, args[0]) in self.strings:
                buffer = C.create_unicode_buffer(self.strings[(handle, args[0])])
                self.buffers.append(buffer)
                variant.vt, variant.v1 = 8, C.addressof(buffer)
        else:
            raise AssertionError((handle, slot))
        return 0

    @contextlib.contextmanager
    def install(self):
        with patch.object(C, 'WinDLL', return_value=self.user, create=True), \
                patch.object(C, 'windll', types.SimpleNamespace(ole32=self.ole, oleaut32=self.auto), create=True), \
                patch.object(C, 'oledll', types.SimpleNamespace(ole32=self.ole), create=True), \
                patch.object(y, '_com_call', side_effect=self.call):
            yield self


class ArticleScopeTest(unittest.TestCase):
    """A news page is one big article; a tweet it cites must not replace the page as the source."""

    def test_article_fallback_runs_only_when_the_caller_names_a_post_site(self):
        model = UIAModel()
        with model.install():
            self.assertIsNone(y._uia_link_at_cursor(42, UIAModel.POINT))
        self.assertEqual(model.conditions, [30003])  # the article condition is never even built
        model = UIAModel()
        with model.install():
            self.assertEqual(y._uia_link_at_cursor(42, UIAModel.POINT, posts=True),
                             {'value': 'https://x.com/someone/status/123', 'name': 'Haber metni'})

    def test_enrich_asks_for_posts_only_on_an_x_or_instagram_page(self):
        context = {'hwnd': 42, 'uygulama': 'chrome', 'pencere': 'Haber - Google Chrome', 'imlec': [10, 20]}
        for page, posts in (('https://www.bbc.com/news/1', False), ('https://x.com/home', True), (None, False)):
            with self.subTest(page=page), patch.object(y, '_win_foreground', return_value=42), \
                    patch.object(y, '_win_title', return_value=context['pencere']), \
                    patch.object(y, 'windows_browser_url', return_value=page), \
                    patch.object(y, 'windows_hovered_link', return_value=(None, None)) as hover:
                y.windows_enrich(context)
            self.assertIs(hover.call_args.kwargs.get('posts'), posts)

    def test_hovered_link_request_carries_posts_only_when_set(self):
        with patch.object(y, '_windows_read', return_value={}) as read:
            y.windows_hovered_link(42, 'chrome', (5, -7))
            y.windows_hovered_link(42, 'chrome', (5, -7), posts=True)
        self.assertEqual([c.args[1] for c in read.call_args_list],
                         [{'hwnd': 42, 'at': [5, -7]}, {'hwnd': 42, 'at': [5, -7], 'posts': True}])

    def test_reader_passes_posts_and_rejects_a_non_boolean(self):
        def run(request):
            stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode('utf-8')))
            stdout = types.SimpleNamespace(buffer=io.BytesIO())
            with patch.object(y.sys, 'stdin', stdin), patch.object(y.sys, 'stdout', stdout), \
                    patch.object(y.os, 'name', 'nt'), patch.object(y, '_uia_link_at_cursor', return_value=None) as link:
                y._windows_reader_main('uia_link')
            return y._parse_read_packet(stdout.buffer.getvalue()), link
        for request, posts in (({'hwnd': 42}, False), ({'hwnd': 42, 'posts': True}, True)):
            with self.subTest(request=request):
                result, link = run(request)
                self.assertEqual(result, {})
                self.assertIs(link.call_args.kwargs['posts'], posts)
        for value in (1, 'true', None):
            with self.subTest(value=value):
                result, link = run({'hwnd': 42, 'posts': value})
                self.assertEqual(result.get('error'), 'ValueError')
                link.assert_not_called()

    def test_opera_takes_posts_from_its_own_verified_document(self):
        for page, posts in (('https://www.instagram.com/', True), ('https://www.youtube.com/', False)):
            document = {'value': page, 'name': 'Sayfa'}
            with self.subTest(page=page), patch.object(y, '_opera_renderer', return_value=(55, 100)), \
                    patch.object(y, '_msaa_document', return_value=dict(document)), \
                    patch.object(y, '_uia_link_at_cursor', return_value=None) as hover:
                self.assertEqual(y._opera_snapshot(42, [10, 20]), document)
            self.assertTrue(hover.call_args_list)
            self.assertTrue(all(c.kwargs.get('posts') is posts for c in hover.call_args_list))


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
