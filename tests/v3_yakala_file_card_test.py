"""#299: a file card reads only what was captured into its own `dosyalar/`."""
import os
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402

NO_TOOLS = {key: None for key in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}


class FileCardFolderTest(unittest.TestCase):
    """#299: a file card reads what was captured into its own `dosyalar/`, nothing else in the vault."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='yakala-dosya-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'Örnek Beyin'
        self.vault.mkdir()
        kasa = self.vault / '🔐 Kasa'
        kasa.mkdir()
        (kasa / 'Şifreler.md').write_text('KASA-SIFRESI-1234', encoding='utf-8')
        plugin = self.vault / '.obsidian/plugins/ornek'
        plugin.mkdir(parents=True)
        (plugin / 'data.json').write_text('{"token": "EKLENTI-ANAHTARI"}', encoding='utf-8')
        (self.vault / 'Müşteri.md').write_text('MUSTERI-NOTU', encoding='utf-8')
        source = self.root / 'Toplantı özeti.txt'
        source.write_text('Bu dosya açıkça yakalandı.', encoding='utf-8')
        self.new = yakala.capture(self.vault, files=[source])
        self.card = yakala.find_card(self.vault, self.new['id'])

    def relink(self, link):
        meta, _body = yakala.read_card(self.card['path'])
        yakala.write_card(self.card['path'], meta, '## Dosyalar\n\n- [[' + link + ']]\n')
        self.card = yakala.find_card(self.vault, self.new['id'])

    def test_captured_file_is_read_and_other_vault_files_are_refused(self):
        self.assertIn('açıkça yakalandı', yakala.extract(self.vault, self.card, NO_TOOLS)[0])
        inbox = yakala.inbox(self.vault).relative_to(self.vault).as_posix()
        (yakala.inbox(self.vault) / 'baska-kart-notu.md').write_text('KOMSU-KART', encoding='utf-8')
        for link in ('🔐 Kasa/Şifreler.md', '.obsidian/plugins/ornek/data.json', 'Müşteri.md',
                     inbox + '/baska-kart-notu.md'):
            self.relink(link)
            with self.subTest(link=link):
                with self.assertRaisesRegex(ValueError, 'kartin dosyalar klasorunde degil'):
                    yakala.extract(self.vault, self.card, NO_TOOLS)
                with patch.object(yakala, 'tools', return_value=NO_TOOLS):
                    entry = yakala.process(self.vault, [self.new['id']], retry=True)['kartlar'][0]
                self.assertEqual(entry['durum'], 'hata')
                self.assertEqual(list(yakala.inbox(self.vault).glob('.ham/*.md')), [])

    def test_link_spelling_does_not_decide_the_folder(self):
        """A synced link in another Unicode form or case names the same file where the file system does."""
        link = (yakala.inbox(self.vault) / yakala.FILES / 'Toplantı özeti.txt').relative_to(self.vault).as_posix()
        for variant in (unicodedata.normalize('NFD', link), link.replace('dosyalar', 'DOSYALAR')):
            if not (self.vault / variant).is_file():
                continue  # this file system keeps the spellings apart: the link names no file here
            self.relink(variant)
            with self.subTest(variant=variant):
                self.assertIn('açıkça yakalandı', yakala.extract(self.vault, self.card, NO_TOOLS)[0])

    def test_dosyalar_linked_elsewhere_in_the_vault_is_refused(self):
        files = yakala.inbox(self.vault) / yakala.FILES
        moved = files.with_name('eski-dosyalar')
        files.rename(moved)
        try:
            files.symlink_to(self.vault / '🔐 Kasa', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks not supported')
        self.relink(files.relative_to(self.vault).as_posix() + '/Şifreler.md')
        with self.assertRaisesRegex(ValueError, 'kartin dosyalar klasorunde degil'):
            yakala.extract(self.vault, self.card, NO_TOOLS)

    @unittest.skipUnless(os.name == 'nt', 'junction is Windows only')
    def test_windows_junction_for_dosyalar_is_refused(self):
        import _winapi
        files = yakala.inbox(self.vault) / yakala.FILES
        files.rename(files.with_name('eski-dosyalar'))
        _winapi.CreateJunction(str(self.vault / '🔐 Kasa'), str(files))
        self.addCleanup(os.rmdir, files)
        self.relink(files.relative_to(self.vault).as_posix() + '/Şifreler.md')
        with self.assertRaisesRegex(ValueError, 'kartin dosyalar klasorunde degil'):
            yakala.extract(self.vault, self.card, NO_TOOLS)


if __name__ == '__main__':
    unittest.main()
