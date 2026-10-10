"""Optional capture tool (yakala): exercised through a real install, the installed entry and hook.

Offline: `isle` runs with every helper tool hidden, so nothing reaches the network and the
popup/hotkey paths are never started.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INBOX = '📥 000-Inbox/Yakala'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


class YakalaUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()

    def test_canonical_merges_url_forms(self):
        self.assertEqual(yakala.canonical('https://youtu.be/abc123?t=40'), yakala.canonical('https://www.youtube.com/watch?v=abc123&list=x'))
        self.assertEqual(yakala.canonical('https://twitter.com/a/status/42'), yakala.canonical('https://x.com/b/status/42?s=20'))
        self.assertNotEqual(yakala.canonical('https://a.com/x'), yakala.canonical('https://a.com/y'))

    def test_kind_and_slug(self):
        self.assertEqual(yakala.kind_of('https://m.youtube.com/watch?v=1'), 'youtube')
        self.assertEqual(yakala.kind_of('https://x.com/a/status/1'), 'x')
        self.assertEqual(yakala.kind_of('https://mail.google.com/mail/u/0/#inbox/1'), 'mail')
        self.assertEqual(yakala.kind_of(None, app='Mail'), 'mail')
        self.assertEqual(yakala.kind_of(None), 'metin')
        self.assertEqual(yakala.slug('Kısa tut, İyi düşün: ağ'), 'kisa-tut-iyi-dusun-ag')
        self.assertEqual(yakala.url_slug('https://www.youtube.com/watch?v=AbC'), 'youtube-abc')
        self.assertEqual(yakala.url_slug('https://x.com/karpathy/status/99'), 'x-karpathy-99')

    def test_capture_writes_one_card_and_dedupes(self):
        first = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=abc', why='limit gözlemi')
        again = yakala.capture(self.vault, url='https://youtu.be/abc', why='ikinci bakış')
        self.assertEqual(first['status'], 'yakalandi')
        self.assertEqual(again['status'], 'mevcut_karta_eklendi')
        self.assertEqual(again['id'], first['id'])
        cards = yakala.cards(self.vault)
        self.assertEqual(len(cards), 1)
        meta, body = cards[0]['meta'], cards[0]['body']
        self.assertEqual((meta['durum'], meta['kaynak_turu'], meta['visibility']), ('bekliyor', 'youtube', 'internal'))
        self.assertIn('limit gözlemi', body)
        self.assertIn('ikinci bakış', body)
        self.assertEqual(yakala.pending(self.vault), 1)

    def test_capture_requires_something(self):
        with self.assertRaises(ValueError):
            yakala.capture(self.vault, why='yalniz neden')

    def test_private_kinds_and_files(self):
        source = Path(self.tmp.name) / 'not.txt'
        source.write_text('dosyadaki ders', encoding='utf-8')
        result = yakala.capture(self.vault, files=[source], why='belge')
        card = yakala.find_card(self.vault, result['id'])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertTrue((self.vault / INBOX / 'dosyalar/not.txt').is_file())
        text = yakala.capture(self.vault, text='pano metni')
        self.assertEqual(yakala.find_card(self.vault, text['id'])['meta']['visibility'], 'private')

    def test_process_offline_and_finish(self):
        none = {name: None for name in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
        note = yakala.capture(self.vault, text='Bağlam kaybı en pahalı kalem.', why='ders')
        clipped = self.vault / INBOX / 'clip.md'
        clipped.write_text('---\ntur: yakala\ndurum: bekliyor\nkaynak_turu: makale\nurl: "https://ornek.com/yazi"\n---\n\n'
                           '# Yazı\n\n## Neden\n\n\n\n## Icerik\n\n' + ('Uzun makale metni. ' * 30) + '\n', encoding='utf-8')
        video = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=zzz')
        with patch.object(yakala, 'tools', return_value=none):
            result = yakala.process(self.vault)
        by_id = {entry['id']: entry for entry in result['kartlar']}
        self.assertEqual(by_id[note['id']]['yontem'], 'metin')
        self.assertEqual(by_id['clip']['yontem'], 'web-clipper')
        self.assertEqual(by_id[video['id']]['durum'], 'hata')
        raw = (self.vault / by_id['clip']['ham']).read_text(encoding='utf-8')
        self.assertIn('Uzun makale metni.', raw)
        self.assertIn('visibility: private', raw)
        # The clipped page body moves out of the indexed card into the dot folder.
        self.assertNotIn('Uzun makale metni.', (self.vault / INBOX / 'clip.md').read_text(encoding='utf-8'))
        self.assertEqual(yakala.pending(self.vault), 0)
        concept = self.vault / 'knowledge/concepts/baglam.md'
        concept.parent.mkdir(parents=True)
        concept.write_text('# Bağlam\n', encoding='utf-8')
        done = yakala.finish(self.vault, note['id'], ['knowledge/concepts/baglam.md'], 'tek ders')
        self.assertEqual(done['status'], 'islendi')
        card = yakala.find_card(self.vault, note['id'])
        self.assertEqual(card['meta']['durum'], 'islendi')
        self.assertIn('[[knowledge/concepts/baglam]]', card['body'])
        with self.assertRaises(ValueError):
            yakala.finish(self.vault, note['id'], ['knowledge/concepts/yok.md'])

    def test_vtt_text(self):
        raw = ('WEBVTT\nKind: captions\n\n00:00:00.000 --> 00:00:02.000\nmerhaba <c>dunya</c>\n\n'
               '00:00:02.000 --> 00:00:04.000\nmerhaba dunya\nikinci satir\n\n00:00:40.000 --> 00:00:42.000\nyeni paragraf\n'
               '\n01:00:05.000 --> 01:00:06.000\nsaat\n')
        text = yakala.vtt_text(raw)
        self.assertEqual(text.count('merhaba dunya'), 1)
        self.assertIn('**0:00** merhaba dunya ikinci satir', text)
        self.assertIn('**0:40** yeni paragraf', text)
        self.assertIn('**1:00:05** saat', text)

    def test_repair_mojibake(self):
        good = 'Agent loop’ta en pahalı şey bağlam kaybı'
        self.assertEqual(yakala.repair_mojibake(good.encode('utf-8').decode('mac_roman')), good)
        self.assertEqual(yakala.repair_mojibake('pahalÄ± ÅŸey'), 'pahalı şey')  # cp1252 variant
        for text in ('Müşteri Şifre Arşiv', 'café', 'plain'):
            self.assertEqual(yakala.repair_mojibake(text), text)

    def test_clipper_template_is_importable(self):
        template = yakala.clipper_template()
        for field in ('name', 'behavior', 'properties', 'noteContentFormat', 'noteNameFormat', 'path'):
            self.assertIn(field, template)
        self.assertEqual(template['path'], INBOX)
        names = {prop['name']: prop['value'] for prop in template['properties']}
        self.assertEqual((names['tur'], names['durum']), ('yakala', 'bekliyor'))
        self.assertTrue(all(prop['type'] in ('text', 'multitext', 'number', 'checkbox', 'date', 'datetime')
                            for prop in template['properties']))

    def test_hotkey_specs(self):
        self.assertEqual(yakala.parse_hotkey('ctrl+alt+b'), (['ctrl', 'alt'], 'b'))
        self.assertEqual(yakala.parse_hotkey('Option+Command+K'), (['alt', 'cmd'], 'K'))
        self.assertEqual(yakala.parse_hotkey('⌘⇧space'), (['shift', 'cmd'], 'space'))
        self.assertEqual(yakala.parse_hotkey('cmd++'), (['cmd'], '+'))
        self.assertEqual(yakala.parse_hotkey('cmd+"'), (['cmd'], '"'))
        self.assertEqual(yakala.parse_hotkey('f9'), ([], 'f9'))
        for bad in ('', 'b', 'hyper+b', 'cmd+', 'cmd+pageup'):
            with self.assertRaises(ValueError):
                yakala.parse_hotkey(bad)
        self.assertEqual(yakala.windows_hotkey('ctrl+alt+b'), ('CTRL+ALT+B', 'Ctrl+Alt+B'))
        self.assertEqual(yakala.windows_hotkey('alt+shift+f2')[0], 'ALT+SHIFT+F2')
        for bad in ('cmd+b', 'shift+b', 'ctrl+"'):
            with self.assertRaises(ValueError):
                yakala.windows_hotkey(bad)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS key codes')
    def test_mac_hotkey_codes(self):
        self.assertEqual(yakala.mac_hotkey('ctrl+alt+b')[:2], (11, 0x1800))
        self.assertEqual(yakala.mac_hotkey('cmd+shift+space'), (49, 0x300, '⇧⌘Space'))
        self.assertEqual(yakala.mac_hotkey('alt+f5')[:2], (96, 0x800))

    def test_vault_before_or_after_command(self):
        yakala.capture(self.vault, url='https://ornek.com/a')
        for argv in (['--vault', str(self.vault), '--json', 'liste'], ['liste', '--vault', str(self.vault), '--json']):
            with patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
                yakala.main(argv)
            self.assertEqual(json.loads(out.getvalue())['bekleyen'], 1)

    def test_bad_hotkey_writes_nothing(self):
        state = Path(self.tmp.name) / 'state'
        # The spec is parsed before any platform call, so the macOS branch rejects it on every OS.
        with patch.object(yakala.sys, 'platform', 'darwin'):
            with self.assertRaises(ValueError):
                yakala.install(self.vault, state, spec='hyper+b')
        self.assertFalse((self.vault / INBOX).exists())
        self.assertFalse(state.exists())

    def test_uninstall_leaves_another_vaults_listener(self):
        import plistlib
        agent = Path(self.tmp.name) / 'agent.plist'
        agent.write_bytes(plistlib.dumps({'ProgramArguments': ['python3', 'x.py', 'dinle', '--vault', '/baska/vault']}))
        state = Path(self.tmp.name) / 'state'
        with patch.object(yakala, '_launch_agent', return_value=agent), patch.object(yakala.sys, 'platform', 'darwin'), \
                patch.object(yakala.subprocess, 'run') as run:
            result = yakala.uninstall(self.vault, state)
        run.assert_not_called()
        self.assertTrue(agent.exists())
        self.assertNotIn('LaunchAgent', result['kaldirilan'])

    def test_linux_hotkey_specs(self):
        self.assertEqual(yakala.linux_hotkey('ctrl+alt+b'), ('Ctrl+Alt+B', 201326658, '<Control><Alt>b', 'Ctrl+Alt+B'))
        self.assertEqual(yakala.linux_hotkey('ctrl+shift+f5'), ('Ctrl+Shift+F5', 0x04000000 + 0x02000000 + 0x01000034,
                                                                '<Control><Shift>F5', 'Ctrl+Shift+F5'))
        self.assertEqual(yakala.linux_hotkey('super+space'), ('Meta+Space', 0x10000020, '<Super>space', 'Super+Space'))
        for bad in ('shift+b', 'ctrl+f13', 'ctrl+"', 'ctrl+enter', 'f5'):
            with self.assertRaises(ValueError):
                yakala.linux_hotkey(bad)

    def _linux_env(self, desktop, gnome_list="@as []"):
        """Patched Linux session: records every external call, answers the few the code reads."""
        home = Path(self.tmp.name) / 'home'
        calls = []

        def fake_run(command, **kwargs):
            calls.append([str(part) for part in command])
            out = ''
            if command[:3] == ['gsettings', 'get', yakala.GNOME_KEYS]:
                out = self.gnome_list
            elif command[:2] == ['gsettings', 'set'] and command[3] == 'custom-keybindings':
                self.gnome_list = command[4]
            elif command[-1] == 'org.kde.kglobalaccel.Component.isActive':
                out = '(true,)'
            elif command[-1] == '201326658':
                out = "([(['beyne-at.desktop', '_launch'], [201326658])],)"
            return subprocess.CompletedProcess(command, 0, out.encode(), b'')
        self.gnome_list = gnome_list
        patches = [patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala.subprocess, 'run', fake_run),
                   patch.object(yakala, '_which', lambda *names: '/usr/bin/' + names[0]),
                   patch.dict(yakala.os.environ, {'HOME': str(home), 'XDG_DATA_HOME': str(home / 'data'),
                                                  'XDG_CURRENT_DESKTOP': desktop})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return home / 'data/applications/beyne-at.desktop', calls

    def test_linux_install_kde_and_uninstall(self):
        entry, calls = self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'
        done = yakala.install(self.vault, state, spec='ctrl+alt+b')
        self.assertEqual((done['kisayol'], done['kisayol_calisiyor']), ('Ctrl+Alt+B', True))
        text = entry.read_text(encoding='utf-8')
        self.assertIn('pencere --vault', text)
        self.assertIn(str(state.resolve() / 'yakala'), text)
        write = next(c for c in calls if 'kwriteconfig' in c[0])
        for part in ('services', 'beyne-at.desktop', '_launch', 'Ctrl+Alt+B'):
            self.assertIn(part, write)
        keys = next(c for c in calls if 'org.kde.KGlobalAccel.setShortcutKeys' in c)
        self.assertEqual(keys[-2:], ['[([201326658],)]', '6'])
        self.assertTrue(yakala.status(self.vault, state)['dinleyici_calisiyor'])
        removed = yakala.uninstall(self.vault, state)['kaldirilan']
        self.assertIn('beyne-at.desktop', removed)
        self.assertFalse(entry.exists())
        self.assertTrue(any('--delete' in c for c in calls))
        self.assertTrue(any('org.kde.KGlobalAccel.unregister' in c for c in calls))

    def test_linux_install_gnome_keeps_existing_bindings(self):
        entry, calls = self._linux_env('ubuntu:GNOME', "['/org/other/custom0/']")
        state = Path(self.tmp.name) / 'state'
        for _ in range(2):
            self.assertEqual(yakala.install(self.vault, state)['kisayol'], 'Ctrl+Alt+B')
        self.assertEqual(yakala._gnome_list(), ['/org/other/custom0/', yakala.GNOME_PATH])
        binding = [c for c in calls if c[:2] == ['gsettings', 'set'] and c[-2] == 'binding']
        self.assertEqual(binding[-1][-1], '<Control><Alt>b')
        yakala.uninstall(self.vault, state)
        self.assertEqual(yakala._gnome_list(), ['/org/other/custom0/'])
        self.assertFalse(entry.exists())

    def test_linux_install_other_desktop_gives_hint(self):
        entry, _ = self._linux_env('XFCE')
        done = yakala.install(self.vault, Path(self.tmp.name) / 'state')
        self.assertIsNone(done['kisayol'])
        self.assertIn('pencere --vault', done['ipucu'])
        self.assertTrue(entry.is_file())
        self.assertIn(done['ipucu'], yakala.human(done, 'kur'))

    def test_desktop_exec_quotes_spaces(self):
        self.assertEqual(yakala._desktop_quote('/tmp/a b/x'), '"/tmp/a b/x"')
        self.assertEqual(yakala._desktop_quote('/tmp/x'), '/tmp/x')

    def test_linux_fallback_saves_clipboard_text_with_reason(self):
        with patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala, '_which', lambda *n: '/usr/bin/' + n[0]), \
                patch.object(yakala, '_try', return_value=(0, 'tek satir neden\n')) as ask:
            result = yakala._popup_fallback(self.vault, {'metin': 'kopyalanan paragraf'})
        self.assertEqual(ask.call_args[0][0][0], 'kdialog')
        self.assertIn('kopyalanan paragraf', ask.call_args[0][0][4])
        self.assertEqual(result['status'], 'yakalandi')
        card = yakala.cards(self.vault)[0]
        self.assertIn('kopyalanan paragraf', card['body'])
        self.assertIn('tek satir neden', card['body'])

    def test_linux_fallback_cancel(self):
        with patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala, '_which', lambda *n: '/usr/bin/' + n[0]), \
                patch.object(yakala, '_try', return_value=(1, '')):
            self.assertEqual(yakala._popup_fallback(self.vault, {'metin': 'x'}), {'status': 'vazgecildi'})
        self.assertEqual(yakala.cards(self.vault), [])

    def test_linux_context_reads_clipboard(self):
        def context(clip):
            with patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala, '_which', lambda *n: '/usr/bin/' + n[0]), \
                    patch.dict(yakala.os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}), patch.object(yakala, '_try', return_value=(0, clip)):
                return yakala.gather_context()
        note = Path(self.tmp.name) / 'a b.txt'
        note.write_text('x', encoding='utf-8')
        self.assertEqual(context('https://ornek.com/a\nikinci'), {'url': 'https://ornek.com/a'})
        self.assertEqual(context(note.as_uri() + '\n'), {'dosyalar': [str(note)]})
        self.assertEqual(context('duz metin'), {'metin': 'duz metin'})
        self.assertEqual(context('  \n'), {})

    def test_inbox_report_skips_processed_cards(self):
        import beyin_v3_hygiene as hygiene
        waiting = yakala.capture(self.vault, url='https://ornek.com/a')
        done = yakala.capture(self.vault, text='bitti')
        card = yakala.find_card(self.vault, done['id'])
        yakala.write_card(card['path'], dict(card['meta'], durum='islendi'), card['body'])
        rows = hygiene.inbox_report(self.vault)['folders']
        self.assertEqual([row['notes'] for row in rows if row['folder'] == '📥 000-Inbox'], [1])
        self.assertTrue(waiting['id'])

    def test_clipper_card_with_quoted_yaml_is_pending(self):
        folder = self.vault / INBOX
        folder.mkdir(parents=True)
        (folder / 'a.md').write_text('---\ntur: yakala\ndurum: "bekliyor"\nurl: "https://a.com"\n---\n\n# A\n', encoding='utf-8')
        (folder / 'b.md').write_text('---\ntur: not\ndurum: bekliyor\n---\n', encoding='utf-8')
        self.assertEqual(yakala.pending(self.vault), 1)


class YakalaInstalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'Örnek Beyin', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), APPDATA=str(root / 'appdata'))
        self.env.pop('BEYIN_V3_FILTER_HARNESS_TURNS', None)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def entry(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def session_start(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': 's1', 'event_id': 's1-1', 'cwd': str(self.vault)}
        result = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault',
                                 str(self.vault), '--state', str(self.state), '--harness', 'claude'],
                                input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                                env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_installed_module_and_entry(self):
        self.assertTrue((self.vault / '.claude/scripts/beyin_v3_yakala.py').is_file())
        added = self.entry('ekle', 'https://x.com/a/status/7', '--neden', 'okunacak')
        self.assertEqual(added['kaynak_turu'], 'x')
        listed = self.entry('liste')
        self.assertEqual(listed['bekleyen'], 1)

    def test_session_notice_is_opt_in(self):
        self.entry('ekle', 'https://ornek.com/yazi')
        self.assertNotIn('Yakalanan', self.session_start())
        installed = self.entry('kur', '--kisayol-yok')
        self.assertIsNone(installed['kisayol'])
        self.assertTrue((self.state / 'yakala.json').is_file())
        skill = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        self.assertIn('python3 beyin.py yakala isle', skill.read_text(encoding='utf-8'))
        self.assertTrue((self.vault / installed['web_clipper_sablonu']).is_file())
        self.assertIn('Yakalanan 1 kaynak bekliyor', self.session_start())
        removed = self.entry('kaldir')
        self.assertTrue(removed['notlar_korundu'])
        self.assertFalse(skill.exists())
        self.assertFalse((self.state / 'yakala.json').exists())
        self.assertEqual(len(list((self.vault / INBOX).glob('*.md'))), 1)
        self.assertNotIn('Yakalanan', self.session_start())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Start menu and Send To shortcuts')
    def test_windows_shortcuts(self):
        appdata = Path(self.env['APPDATA'])
        installed = self.entry('kur')
        self.assertEqual(installed['kisayol'], 'Ctrl+Alt+B')
        start = appdata / 'Microsoft/Windows/Start Menu/Programs/Beyne At.lnk'
        sendto = appdata / 'Microsoft/Windows/SendTo/Beyne At.lnk'
        self.assertTrue(start.is_file() and sendto.is_file())
        self.assertIsInstance(yakala.windows_context(), dict)
        self.entry('kaldir')
        self.assertFalse(start.exists() or sendto.exists())

    def test_user_skill_with_same_name_is_kept(self):
        own = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        own.parent.mkdir(parents=True)
        own.write_text('kendi skill\'im\n', encoding='utf-8')
        self.entry('kur', '--kisayol-yok')
        self.entry('kaldir')
        self.assertEqual(own.read_text(encoding='utf-8'), 'kendi skill\'im\n')


if __name__ == '__main__':
    unittest.main()
