"""Popup v2 source policy and card compatibility, with no desktop or network."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_yakala as y
import v3_yakala_pano_test as pano


PAGE = 'https://example.org/page'
HOVER = 'https://example.org/link'
QUOTE = 'https://example.org/quote'
CLIP = 'https://example.org/copied'


class SourcePolicyTest(unittest.TestCase):
    def test_all_urls_are_independent_and_priority_survives_input_order(self):
        sources = y.windows_sources(dict(url=PAGE, url_imlec=HOVER, url_alinti=QUOTE), CLIP, True)
        for key in ('p', 'a', 's'):
            y._source_toggle(sources, next(s for s in sources if s['key'] == key))
        sources.reverse()
        for expected, extras, key in ((HOVER, [PAGE, QUOTE, CLIP], 'i'),
                                      (PAGE, [QUOTE, CLIP], 's'), (QUOTE, [CLIP], 'a'), (CLIP, [], 'p')):
            arguments = y._source_arguments(sources)
            self.assertEqual((arguments['url'], arguments['extra_urls']), (expected, extras))
            y._source_toggle(sources, next(s for s in sources if s['key'] == key))
        self.assertIsNone(y._source_arguments(sources)['url'])

    def test_quote_and_clipboard_use_the_existing_page_identity(self):
        for field in ('url', 'url_imlec'):
            for original, alias in ((PAGE, 'http://www.example.org/page/#section'),
                                    ('https://youtu.be/abc', 'https://youtube.com/watch?v=abc')):
                with self.subTest(field=field, original=original):
                    sources = y.windows_sources({field: original, 'url_alinti': alias}, alias, True)
                    self.assertEqual(len(sources), 1)
                    self.assertTrue(sources[0]['clipboard'])
                    self.assertTrue(sources[0]['on'])

    def test_stale_text_is_absent_fresh_text_is_off_next_to_a_page(self):
        for context in ({}, {'url': PAGE}):
            self.assertFalse(any(s['kind'] == 'text' for s in y.windows_sources(context, 'old', False)))
            sources = y.windows_sources(context, 'fresh', True)
            text = next(s for s in sources if s['key'] == 'm')
            self.assertEqual(text['on'], not context)

    def test_window_picture_is_off_even_with_files_and_links(self):
        sources = y.windows_sources(dict(url=PAGE, dosyalar=['document.txt']), '', False,
                                    shot=dict(path='window.png', width=2, height=2))
        self.assertFalse(next(s for s in sources if s['key'] == 'w')['on'])
        self.assertEqual(y._source_arguments(sources)['files'], ['document.txt'])

    def test_many_files_do_not_create_invalid_multi_digit_key_bindings(self):
        sources = y.windows_sources({'dosyalar': [f'{i}.txt' for i in range(20)]}, '', False)
        self.assertEqual([s['key'] for s in sources[:9]], list('123456789'))
        self.assertTrue(all(not s['key'] for s in sources[9:]))
        self.assertEqual(len(y._source_arguments(sources)['files']), 20)

    def test_shortcuts_normalize_caps_turkish_i_and_ignore_altgr(self):
        system = types.SimpleNamespace(**{**vars(os), 'name': 'nt'})
        with patch.object(y, 'os', system):
            for key in 'ISAPMGW123':
                event = types.SimpleNamespace(keysym=key, keycode=ord(key), state=0x20003)
                self.assertEqual(y._source_shortcut(event), key.lower())
            for keysym in ('ı', 'İ', 'Iabovedot', 'idotless'):
                self.assertEqual(y._source_shortcut(types.SimpleNamespace(keysym=keysym, keycode=73, state=0)), 'i')
            self.assertEqual(y._source_shortcut(types.SimpleNamespace(keysym='a', keycode=65, state=0x20004)), '')
        for keysym in ('ı', 'İ', 'Iabovedot', 'idotless'):
            self.assertEqual(y._source_shortcut(types.SimpleNamespace(keysym=keysym, keycode=0, state=0)), 'i')

    def test_tiles_bind_the_note_field_and_the_whole_surface(self):
        root, sources = pano.Root([]), y.windows_sources({'url': PAGE, 'url_alinti': QUOTE}, '', False)
        canvas, entry = pano.Canvas(root), pano.Entry(root)
        fake = types.ModuleType('tkinter')
        fake.font = types.ModuleType('tkinter.font')
        fake.font.Font = lambda **kw: types.SimpleNamespace(measure=lambda s: len(s) * 6)
        with patch.dict(sys.modules, {'tkinter': fake, 'tkinter.font': fake.font}), \
                patch.object(y, '_font', return_value=('test', 12)):
            y._windows_source_cards(canvas, root, sources, 0, 550, 1, entry=entry)
        self.assertIn('<Alt-KeyPress>', entry.handlers)
        event = types.SimpleNamespace(keysym='a', keycode=0, state=0)
        self.assertEqual(entry.handlers['<Alt-KeyPress>'](event), 'break')
        self.assertTrue(all(s['on'] for s in sources))
        fills = [item.get('fill') for item in canvas.items.values()]
        self.assertIn(y.PALETTE['selected'], fills)
        # A tile background has the same click handler as its text.
        background = next(i for i, item in canvas.items.items() if item.get('fill') == y.PALETTE['selected'])
        canvas.clicks[background, '<Button-1>']()
        self.assertFalse(sources[0]['on'])
        self.assertTrue(sources[1]['on'])

    def test_popup_bounds_fit_small_and_negative_origin_monitors(self):
        for work in ((0, 0, 480, 320), (-1280, -720, 0, 0), (0, 0, 3840, 2160)):
            width, height = y._popup_bounds(work)
            x, top = y._popup_position(work, width, height)
            self.assertGreaterEqual(x, work[0])
            self.assertGreaterEqual(top, work[1])
            self.assertLessEqual(x + width, work[2])
            self.assertLessEqual(top + height, work[3])


class CardCompatibilityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)

    def test_url_attachments_keep_the_title_type_and_privacy_on_both_platforms(self):
        source = self.vault / 'private.txt'
        source.write_text('private attachment', encoding='utf-8')
        for platform in ('nt', 'posix'):
            system = types.SimpleNamespace(**{**vars(os), 'name': platform})
            url = PAGE + '/' + platform
            # Only the card writer is under test; the commit lock's OS primitive belongs to the host.
            with patch.object(y, 'os', system), patch.object(y, '_WindowsCommitLock', lambda _vault: contextlib.nullcontext()):
                saved = y.capture(self.vault, url=url, files=[source])
                y.capture(self.vault, url=url, files=[source])
            card = y.find_card(self.vault, saved['id'])
            self.assertEqual(card['meta']['baslik'], url)
            self.assertEqual(card['meta']['kaynak_turu'], 'makale')
            self.assertEqual(card['meta']['visibility'], 'private')
            self.assertEqual(card['body'].count('[['), 2)

    def test_optional_list_roundtrips_and_old_cards_keep_their_shape(self):
        old = y.capture(self.vault, url=PAGE)
        self.assertNotIn('ek_urller', y.find_card(self.vault, old['id'])['meta'])
        saved = y.capture(self.vault, url=HOVER, extra_urls=[PAGE, QUOTE, PAGE, HOVER])
        card = y.find_card(self.vault, saved['id'])
        self.assertEqual(card['meta']['url'], HOVER)
        self.assertEqual(card['meta']['ek_urller'], [PAGE, QUOTE])
        self.assertIn('## Ek bağlantılar\n\n- ' + PAGE + '\n- ' + QUOTE, card['body'])
        self.assertEqual(len(y.cards(self.vault)), 2)
        with patch.object(y, 'tools', return_value={}), \
                patch.object(y, 'extract', return_value=('captured text', 'test', None)):
            result = y.process(self.vault, [saved['id']])['kartlar'][0]
        self.assertEqual(result['url'], HOVER)
        self.assertEqual(result['ek_urller'], [PAGE, QUOTE])
        self.assertEqual(y.find_card(self.vault, saved['id'])['meta']['ek_urller'], [PAGE, QUOTE])
        y.finish(self.vault, saved['id'], summary='ok')
        self.assertEqual(y.find_card(self.vault, saved['id'])['meta']['ek_urller'], [PAGE, QUOTE])

    def test_recapture_merges_links_instead_of_losing_previous_attachments(self):
        saved = y.capture(self.vault, url=PAGE, extra_urls=[QUOTE])
        again = y.capture(self.vault, url=PAGE, extra_urls=[CLIP, QUOTE])
        self.assertEqual(saved['id'], again['id'])
        y.capture(self.vault, url=PAGE, why='only note')
        card = y.find_card(self.vault, saved['id'])
        self.assertEqual(card['meta']['ek_urller'], [QUOTE, CLIP])
        self.assertEqual(card['body'].count('## Ek bağlantılar'), 1)
        self.assertEqual(card['body'].count('- ' + QUOTE), 1)
        self.assertEqual(card['body'].count('- ' + CLIP), 1)

    def test_listing_pending_and_triage_accept_the_optional_field(self):
        import beyin_v3_triage as triage
        saved = y.capture(self.vault, url=PAGE, extra_urls=[QUOTE])
        card = y.find_card(self.vault, saved['id'])
        self.assertEqual(y.pending(self.vault), 1)
        self.assertEqual(y.listing(self.vault)['kartlar'][0]['url'], PAGE)
        front = triage._front_values(card['path'].read_text(encoding='utf-8'), ('tur', 'durum', 'visibility'))
        self.assertEqual(front, dict(tur='yakala', durum='bekliyor', visibility='internal'))
        self.assertEqual(triage.scan_inbox(self.vault, y.now().timestamp())['count'], 1)
        y.finish(self.vault, saved['id'], summary='ok')
        self.assertEqual(y.pending(self.vault), 0)
        self.assertEqual(triage.scan_inbox(self.vault, y.now().timestamp())['count'], 0)

    def test_extra_url_security_checks_happen_before_any_card_or_file_is_written(self):
        for unsafe in ('http://127.0.0.1/private', 'file:///secret', 'https://user:pass@example.org/', '-option'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                y.capture(self.vault, url=PAGE, extra_urls=[unsafe])
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_secondary_mail_url_keeps_the_whole_card_private(self):
        import beyin_v3_sync as sync
        import beyin_v3_triage as triage
        mailbox = 'https://mail.google.com/mail/u/0/#inbox/synthetic-thread'
        sources = y.windows_sources({'url_imlec': PAGE, 'url': mailbox}, '', False)
        y._source_toggle(sources, next(source for source in sources if source['key'] == 's'))
        saved = y.capture(self.vault, why='Synthetic secondary mail privacy canary',
                          **y._source_arguments(sources))
        card = y.find_card(self.vault, saved['id'])
        self.assertEqual(card['meta']['url'], PAGE)
        self.assertEqual(card['meta']['kaynak_turu'], 'makale')
        self.assertEqual(card['meta']['ek_urller'], [mailbox])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertFalse(triage._scan_allowed(card['path'], self.vault, frozenset()))
        state = tempfile.TemporaryDirectory(prefix='popup-private-state-')
        self.addCleanup(state.cleanup)
        engine = sync.SyncEngine(self.vault, Path(state.name))
        self.addCleanup(engine.store.close)
        self.assertEqual(engine.sync()['status'], 'succeeded')
        self.assertEqual(engine.store.retrieve('secondary mail privacy canary')['records'], [])
        private = engine.store.retrieve('secondary mail privacy canary', audience='private')['records']
        self.assertEqual(len(private), 1)

    def test_adding_a_mail_url_to_an_existing_card_upgrades_privacy(self):
        import beyin_v3_triage as triage
        saved = y.capture(self.vault, url=PAGE)
        self.assertEqual(y.find_card(self.vault, saved['id'])['meta']['visibility'], 'internal')
        mailbox = 'https://outlook.office.com/mail/inbox/id/synthetic-thread'
        again = y.capture(self.vault, url=PAGE, extra_urls=[mailbox])
        self.assertEqual(again['id'], saved['id'])
        # Later recaptures must never downgrade the existing private card.
        y.capture(self.vault, url=PAGE, extra_urls=[QUOTE])
        with patch.object(y, 'tools', return_value={}), \
                patch.object(y, 'extract', return_value=('Synthetic public page', 'test', None)):
            y.process(self.vault, [saved['id']])
        card = y.find_card(self.vault, saved['id'])
        self.assertEqual(card['meta']['ek_urller'], [mailbox, QUOTE])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertFalse(triage._scan_allowed(card['path'], self.vault, frozenset()))


if __name__ == '__main__':
    unittest.main()
