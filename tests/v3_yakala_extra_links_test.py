"""#486: a local or private extra link is kept as a reference; the capture is not cancelled."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402

NO_TOOLS = {key: None for key in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}


class LocalExtraLinkTest(unittest.TestCase):
    """#486: a local or private extra address never cancels the capture; it keeps the card private."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='yakala-ek-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / 'vault'
        self.vault.mkdir()

    def test_local_extra_links_are_kept_apart_and_never_fetched(self):
        local = ['http://127.0.0.1/doc', 'http://192.168.1.20/wiki/Sayfa', 'http://intranet/rapor',
                 'http://nas.local/paylasim', 'http://[fd00::1]/panel']
        new = yakala.capture(self.vault, url='https://example.com/makale',
                             extra_urls=['https://example.org/kaynak'] + local)
        card = yakala.find_card(self.vault, new['id'])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertEqual(card['meta']['ek_urller'], ['https://example.org/kaynak'])
        self.assertEqual(card['meta']['ek_urller_yerel'], local)
        self.assertIn('## Yerel ağ bağlantıları\n\n' + yakala.LOCAL_LINKS_NOTE, card['body'])
        fetched = []

        def fetch(url, *args, **kwargs):
            fetched.append(url)
            return 'Acik makale metni. ' * 20
        with patch.object(yakala, 'tools', return_value=dict(NO_TOOLS, npx='npx')), \
                patch.object(yakala, '_safe_network_url', side_effect=lambda url: url), \
                patch.object(yakala, 'via_defuddle', side_effect=fetch), \
                patch.object(yakala, 'via_stdlib', side_effect=AssertionError('fallback not needed')):
            entry = yakala.process(self.vault, [new['id']])['kartlar'][0]
        self.assertEqual(fetched, ['https://example.com/makale'])
        self.assertEqual((entry['ek_urller'], entry['ek_urller_yerel']), (['https://example.org/kaynak'], local))
        self.assertIn('`ek_urller_yerel`', yakala.SKILL_MD)

    def test_private_card_stays_out_of_shared_retrieval(self):
        """The privacy promise on real index rows: the card with a local link is found only privately."""
        import beyin_v3_sync as sync
        saved = yakala.capture(self.vault, url='https://example.com/makale', why='Yerel ag kanaryasi sentetik',
                               extra_urls=['http://192.168.1.20/wiki/Sayfa'])
        self.assertEqual(yakala.find_card(self.vault, saved['id'])['meta']['visibility'], 'private')
        state = tempfile.TemporaryDirectory(prefix='yakala-ek-state-')
        self.addCleanup(state.cleanup)
        engine = sync.SyncEngine(self.vault, Path(state.name))
        self.addCleanup(engine.store.close)
        self.assertEqual(engine.sync()['status'], 'succeeded')
        self.assertEqual(engine.store.retrieve('Yerel ag kanaryasi sentetik')['records'], [])
        private = engine.store.retrieve('Yerel ag kanaryasi sentetik', audience='private')['records']
        self.assertEqual(len(private), 1)

    def test_a_local_link_on_a_recapture_makes_the_existing_card_private(self):
        first = yakala.capture(self.vault, url='https://example.com/makale')
        self.assertEqual(yakala.find_card(self.vault, first['id'])['meta']['visibility'], 'internal')
        again = yakala.capture(self.vault, url='https://example.com/makale', why='ikinci bakış',
                               extra_urls=['http://10.1.2.3/ic-belge'])
        card = yakala.find_card(self.vault, again['id'])
        self.assertEqual((again['status'], card['meta']['visibility'], card['meta']['ek_urller_yerel']),
                         ('mevcut_karta_eklendi', 'private', ['http://10.1.2.3/ic-belge']))
        self.assertEqual(card['body'].count('## Yerel ağ bağlantıları'), 1)
        yakala.capture(self.vault, url='https://example.com/makale', extra_urls=['http://10.1.2.4/ikinci'])
        card = yakala.find_card(self.vault, again['id'])
        self.assertEqual(card['meta']['ek_urller_yerel'], ['http://10.1.2.3/ic-belge', 'http://10.1.2.4/ikinci'])
        self.assertEqual(card['body'].count('## Yerel ağ bağlantıları'), 1)

    def test_edited_or_synced_extras_never_block_a_new_capture(self):
        first = yakala.capture(self.vault, url='https://example.com/makale', extra_urls=['https://example.org/a'])
        path = self.vault / first['path']
        for stored in (['https://example.org/a', 'http://192.168.1.5/wiki'], 'https://example.org/a',
                       ['--version', 42, 'file:///etc/passwd', 'https://example.org/a'], {'url': 'x'}):
            meta, body = yakala.read_card(path)
            meta['ek_urller'] = stored
            yakala.write_card(path, meta, body)
            with self.subTest(stored=stored):
                result = yakala.capture(self.vault, url='https://example.com/makale', why='yeni not')
                self.assertEqual(result['status'], 'mevcut_karta_eklendi')
                self.assertIn('yeni not', yakala.find_card(self.vault, first['id'])['body'])
        with patch.object(yakala, 'tools', return_value=NO_TOOLS), \
                patch.object(yakala, 'extract', return_value=(None, None, 'yok')):
            entry = yakala.process(self.vault, [first['id']])['kartlar'][0]
        self.assertNotIn('ek_urller', entry)  # a mapping is no list of addresses: nothing is handed on

    def test_an_edited_url_field_does_not_stop_the_queue(self):
        broken = yakala.capture(self.vault, url='https://example.com/bozuk', extra_urls=['http://10.0.0.9/x'])
        note = yakala.capture(self.vault, text='Bağımsız not')
        path = self.vault / broken['path']
        meta, body = yakala.read_card(path)
        meta['url'] = ['https://example.com/bozuk']
        yakala.write_card(path, meta, body)
        with patch.object(yakala, 'tools', return_value=NO_TOOLS):
            entries = yakala.process(self.vault, [broken['id'], note['id']])['kartlar']
        self.assertEqual([entry['durum'] for entry in entries], ['hata', 'cikarildi'])
        self.assertEqual(entries[0]['ek_urller_yerel'], ['http://10.0.0.9/x'])

    def test_primary_url_stays_public_only(self):
        for url in ('http://127.0.0.1/doc', 'http://intranet/rapor'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                yakala.capture(self.vault, url=url, extra_urls=['https://example.org/a'])
        self.assertEqual(yakala.cards(self.vault), [])


if __name__ == '__main__':
    unittest.main()
