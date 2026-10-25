"""Browser source decisions for macOS/Linux; no desktop, network or clipboard calls."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y

PAGE = 'https://example.org/page'
HOVER = 'https://example.org/link'
CLIP = 'https://example.org/copied'


class BrowserSourceChoicesTest(unittest.TestCase):
    def context(self, **extra):
        return dict(url=PAGE, url_imlec=HOVER, baslik='Page', baslik_imlec='Link', **extra)

    def test_hover_page_and_clipboard_remain_separate_selectable_sources(self):
        choices = y._browser_source_choices(self.context(), CLIP)
        self.assertEqual([(c['key'], c['on']) for c in choices], [('i', True), ('s', False), ('p', False)])
        self.assertEqual(y._source_arguments(choices)['title'], 'Link')
        for key, expected, count in (('s', HOVER, 2), ('p', HOVER, 3), ('i', PAGE, 2)):
            y._source_toggle(choices, next(c for c in choices if c['key'] == key))
            self.assertEqual(y._source_arguments(choices)['url'], expected)
            self.assertEqual(sum(c['on'] for c in choices), count)
        self.assertTrue(all(c['label'].isascii() for c in choices))

    def test_text_and_image_are_off_until_explicitly_selected(self):
        picture = dict(path='/tmp/copied.png', width=2, height=2)
        choices = y._browser_source_choices(self.context(), 'copied text', picture, fresh=True)
        self.assertEqual(y._source_arguments(choices), dict(url=HOVER, title='Link', text=None, files=[], extra_urls=[]))
        for key in ('m', 'g'):
            y._source_toggle(choices, next(c for c in choices if c['key'] == key))
        self.assertEqual(y._source_arguments(choices),
                         dict(url=HOVER, title='Link', text='copied text', files=['/tmp/copied.png'], extra_urls=[]))

    def test_unknown_clipboard_age_does_not_offer_a_text_tile(self):
        choices = y._browser_source_choices(self.context(), 'old text')
        self.assertFalse(any(c['kind'] == 'text' for c in choices))

    def test_quote_can_join_the_browser_sources_and_duplicates_are_hidden(self):
        quote = 'https://example.org/quote'
        choices = y._browser_source_choices(self.context(url_alinti=quote))
        y._source_toggle(choices, next(c for c in choices if c['key'] == 'a'))
        self.assertEqual(y._source_arguments(choices)['extra_urls'], [quote])
        choices = y._browser_source_choices(self.context(url_alinti=PAGE))
        self.assertFalse(any(c['key'] == 'a' for c in choices))

    def test_copied_page_is_not_duplicated_and_hover_still_leads(self):
        choices = y._browser_source_choices(self.context(), PAGE)
        self.assertEqual(len(choices), 2)
        self.assertEqual(y._source_arguments(choices)['url'], HOVER)

    def test_ordinary_preview_and_copied_files_keep_the_existing_flow(self):
        for context in ({}, {'url': PAGE}, {'url': PAGE, 'url_imlec': PAGE},
                        {'url_imlec': HOVER}, self.context(dosyalar=['/tmp/file.txt'])):
            with self.subTest(context=context):
                self.assertIsNone(y._browser_source_choices(context, CLIP))

    def test_missing_scheme_private_and_unsafe_urls_never_become_choices(self):
        for value in ('example.org/path', 'http://127.0.0.1/', 'javascript:alert(1)',
                      'https://user:pass@example.org/', 'https://example.org/\nlink', None, 5):
            for field in ('url', 'url_imlec'):
                with self.subTest(value=value, field=field):
                    context = self.context()
                    context[field] = value
                    self.assertIsNone(y._browser_source_choices(context))

    def test_deselecting_browser_sources_saves_no_invisible_default_url(self):
        choices = y._browser_source_choices(self.context())
        y._source_toggle(choices, choices[0])
        self.assertEqual(y._source_arguments(choices), dict(url=None, title=None, text=None, files=[], extra_urls=[]))


if __name__ == '__main__':
    unittest.main()
