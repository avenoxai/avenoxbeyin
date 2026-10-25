"""Optional capture tool (yakala): exercised through a real install, the installed entry and hook.

Offline: `isle` runs with every helper tool hidden, so nothing reaches the network and the
popup/hotkey paths are never started.
"""
import ast
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from v3_package_helpers import inherited_env
from v3_package_helpers import clean_environ, isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INBOX = '📥 000-Inbox/Yakala'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


class WindowsSourceDecisionTest(unittest.TestCase):
    def test_fresh_sources_win_without_losing_alternatives(self):
        image = {'path': 'clipboard.png', 'on': False}
        shot = {'path': 'window.png', 'on': True}
        context = {'url': 'https://example.com/page'}
        for fresh in (True, False):  # the browser's link leads; the copied one stays an alternative
            sources = yakala.windows_sources(context, 'https://example.com/copied', fresh, shot=shot)
            self.assertEqual(yakala._source_arguments(sources)['url'], 'https://example.com/page')
            self.assertEqual(len(sources), 3)
        sources = yakala.windows_sources(context, '', True, image, shot)
        self.assertEqual(yakala._source_arguments(sources)['files'], [])
        self.assertEqual(yakala._source_arguments(sources)['url'], 'https://example.com/page')
        self.assertEqual(len(sources), 3)
        sources = yakala.windows_sources({}, 'https://example.com/copied', True, shot=shot)
        self.assertEqual(yakala._source_arguments(sources)['url'], 'https://example.com/copied')  # no browser link

    def test_stale_clipboard_does_not_depend_on_a_successful_screenshot(self):
        for clip in ('private note', 'https://example.com/old'):
            for shot in (None, {'path': 'window.png', 'on': True}):
                sources = yakala.windows_sources({}, clip, False, shot=shot)
                chosen = yakala._source_arguments(sources)
                self.assertIsNone(chosen['text'])
                self.assertIsNone(chosen['url'])
                self.assertEqual(chosen['files'], [])  # a window picture waits for Alt+W, like the stale clipboard

    def test_url_selection_preserves_text_and_images(self):
        sources = yakala.windows_sources({'url': 'https://example.com/page'},
                                         'copied text', True, {'path': 'image.png', 'on': True})
        for key in ('m', 'g'):  # clipboard text and picture are offered off next to a link; switch them on
            yakala._source_toggle(sources, next(source for source in sources if source['key'] == key))
        chosen = yakala._source_arguments(sources)
        self.assertEqual(chosen['url'], 'https://example.com/page')
        self.assertEqual(chosen['text'], 'copied text')
        self.assertEqual(chosen['files'], ['image.png'])


def public_dns_fixture(test):
    # Helpers below are real offline subprocess stand-ins, so DNS must also stay deterministic.
    patcher = patch.object(socket, 'getaddrinfo', return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    patcher.start()
    test.addCleanup(patcher.stop)


# Stands in for `npx defuddle`, `yt-dlp` and `whisper` as a real child process. It answers from
# scenario.json next to it, logs every command line to calls.jsonl and never opens a socket. A subtitle
# request behaves like yt-dlp: --sub-langs is a comma list of regexes, a manual track wins over an
# automatic one of the same name, and each matching track becomes sub.<track>.vtt.
FAKE_TOOL = r'''
import json, re, sys
from pathlib import Path

here = Path(__file__).resolve().parent
name, args = sys.argv[1], sys.argv[2:]
scenario = json.loads((here / 'scenario.json').read_text(encoding='utf-8'))
with open(here / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps([name] + args) + '\n')


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def too_many():
    sys.stderr.write('ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests\n')
    raise SystemExit(1)


if name == 'npx':
    answers = scenario.get('defuddle') or {}
    answer = answers.get(value('--lang') or '', answers.get(''))
    if not answer:
        raise SystemExit(1)
    sys.stdout.write(json.dumps(answer))
elif name.startswith('whisper'):
    if scenario.get('whisper_error') and value('--device') != scenario.get('whisper_works_on'):
        sys.stderr.write('Detecting language...\n' + scenario['whisper_error'] + '\n')
        if scenario.get('whisper_empty_output'):
            Path(value('--output_dir'), 'ses.txt').write_text(' \n', encoding='utf-8')
        raise SystemExit(scenario.get('whisper_exit', 0))
    Path(value('--output_dir'), 'ses.txt').write_text(scenario['audio'], encoding='utf-8')
elif '-J' in args:
    if not scenario.get('video'):
        too_many()
    sys.stdout.write(json.dumps(scenario['video']))
elif '--sub-langs' in args:
    if '--load-info-json' in args:
        json.loads(Path(value('--load-info-json')).read_text(encoding='utf-8'))
    video, offered = scenario.get('video') or {}, {}
    if '--write-subs' in args:
        offered.update({track: 'manual' for track in video.get('subtitles') or {}})
    if '--write-auto-subs' in args:
        for track in video.get('automatic_captions') or {}:
            offered.setdefault(track, 'auto')
    earlier = sum('--sub-langs' in json.loads(line) for line in (here / 'calls.jsonl').read_text(encoding='utf-8').splitlines()) - 1
    if earlier < scenario.get('fail_first', 0):
        too_many()
    for track, kind in offered.items():
        if any(re.fullmatch(pattern, track) for pattern in value('--sub-langs').split(',')):
            words = scenario.get('speech', {}).get(kind + ':' + track) or (kind + ' track ' + track + ' ') * 8
            Path(value('-o').replace('%(ext)s', track + '.vtt')).write_text(
                'WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:09.000\n' + words + '\n', encoding='utf-8')
elif '-f' in args and scenario.get('audio'):
    Path(value('-o').replace('%(ext)s', 'webm')).write_bytes(b'ses')
else:
    raise SystemExit(1)
'''

VIDEO = 'https://www.youtube.com/watch?v=Wqq0mwYmhcQ'
DUBS = ('ar', 'bn', 'de-DE', 'es-US', 'fr-FR', 'hi', 'id', 'it', 'iw', 'ja', 'ko', 'ml', 'nl-NL', 'pa', 'pl', 'pt-BR',
        'ru', 'ta', 'te', 'uk')
ENGLISH = 'So, I gave the same prompt to both models across four different builds and wrote down what every run cost.'
ARABIC = 'لذا، قمتُ بتطبيق نفس التعليمات على النموذجين في أربع تجارب مختلفة، ثم سجّلتُ تكلفة كل تجربة منها بالتفصيل.'
TURKISH = 'Aynı istemi iki modele de verdim; ölçtüğüm maliyeti şöyle yazdım: İstanbul, Iğdır, Çorum, Şırnak, Ödemiş, Ünye.'


def caption(source, target=None):
    """One automatic caption entry as yt-dlp lists it; `tlang` marks a machine translation of `source`."""
    url = 'https://www.youtube.com/api/timedtext?v=Wqq0mwYmhcQ&kind=asr&lang=' + source + ('&tlang=' + target if target else '')
    return [{'ext': ext, 'url': url + '&fmt=' + ext} for ext in ('json3', 'vtt')]


def video(language=None, audio=(), manual=(), recognised=(), translated=()):
    """The part of a `yt-dlp -J` answer the capture reads. `audio` is (language, language_preference) pairs."""
    automatic = {track: caption(track.removesuffix('-orig')) for track in recognised}
    for track in translated:  # the untranslated entry, if any, comes first; yt-dlp takes the last one
        automatic[track] = automatic.get(track, []) + caption('uk', track)
    return {'id': 'Wqq0mwYmhcQ', 'title': 'I Tested Two Models', 'language': language,
            'formats': [{'format_id': '251-' + str(index), 'acodec': 'opus', 'language': code, 'language_preference': mark}
                        for index, (code, mark) in enumerate(audio)],
            'subtitles': {track: [{'ext': 'vtt', 'url': 'https://www.youtube.com/api/timedtext?lang=' + track}] for track in manual},
            'automatic_captions': automatic}


def dubbed():
    """Like the video in #277: spoken English, 20 automatic dubs, one `-orig` track per audio track."""
    return video('en-US', audio=[('en-US', 10)] + [(code, -1) for code in DUBS],
                 recognised=['en', 'en-orig'] + [code + '-orig' for code in DUBS],
                 translated=['en', 'tr'] + [code for code in DUBS])


def defuddle(language, words=None):
    """A `defuddle parse --json` answer; without words the video has no transcript, only its description."""
    body = '## Transcript\n\n### Intro\n\n**0:00** · ' + words if words else 'Video description without a transcript. ' * 4
    return {'title': 'I Tested Two Models', 'author': 'Kanal', 'published': '2026-09-16T05:37:48-07:00', 'site': 'YouTube',
            'language': language, 'content': '![](' + VIDEO + ')\n\n' + body}


def desktop_exec_argv(text):
    """Arguments of the Exec line, read by the letter of the Desktop Entry spec (written apart from the module).

    Three layers: the string escapes of a key file, the double-quote rules of Exec, then field codes.
    A sequence the spec does not allow fails here instead of being guessed at.
    """
    line = next(item for item in text.split('\n') if item.startswith('Exec='))[5:]
    value, index = '', 0
    while index < len(line):
        if line[index] == '\\':
            value += {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}[line[index + 1]]
            index += 2
        else:
            value += line[index]
            index += 1
    args, current, quoted, open_arg, index = [], '', False, False, 0
    while index < len(value):
        char = value[index]
        if quoted:
            if char == '\\':
                assert value[index + 1] in '"`$\\', 'only " ` $ and \\ may follow a backslash: ' + value
                current += value[index + 1]
                index += 1
            elif char == '"':
                quoted = False
            else:
                assert char not in '`$', 'unescaped ' + char + ' inside quotes: ' + value
                current += char
        elif char == '"':
            assert not open_arg, 'an argument is quoted in whole: ' + value
            quoted = open_arg = True
        elif char == ' ':
            if open_arg:
                args.append(current)
            current, open_arg = '', False
        else:
            assert not re.match(r'[\s"\'\\><~|&;$*?#()`]', char), 'reserved character outside quotes: ' + value
            current += char
            open_arg = True
        index += 1
    assert not quoted, 'unterminated quote: ' + value
    if open_arg:
        args.append(current)
    for arg in args:
        assert '%' not in arg.replace('%%', ''), 'a lone % is a field code: ' + arg
    return [arg.replace('%%', '%') for arg in args]


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

    def test_clipboard_prefers_wl_paste_on_wayland(self):
        class Tk:
            def clipboard_get(self):
                raise RuntimeError('XWayland cannot see the Wayland clipboard')

        done = subprocess.CompletedProcess([], 0, stdout=b'https://example.com/x', stderr=b'')
        with patch.dict('os.environ', {'WAYLAND_DISPLAY': 'wayland-1'}), \
                patch.object(yakala.shutil, 'which', return_value='/usr/bin/wl-paste'), \
                patch.object(yakala.subprocess, 'run', return_value=done):
            self.assertEqual(yakala._clipboard(Tk()), 'https://example.com/x')
        empty = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'Nothing is copied')
        with patch.dict('os.environ', {'WAYLAND_DISPLAY': 'wayland-1'}), \
                patch.object(yakala.shutil, 'which', return_value='/usr/bin/wl-paste'), \
                patch.object(yakala.subprocess, 'run', return_value=empty):
            self.assertEqual(yakala._clipboard(Tk()), '')

    class _Tk:
        """Stands in for the Tk root: the X11 path that must still run when wl-paste gives nothing."""
        def clipboard_get(self):
            return 'tk panosu: Şifre'

    def _wayland(self, run):
        return (patch.dict('os.environ', {'WAYLAND_DISPLAY': 'wayland-1'}),
                patch.object(yakala, '_which', return_value='/usr/bin/wl-paste'),
                patch.object(yakala.subprocess, 'run', run))

    def test_clipboard_falls_back_to_tk_when_wl_paste_gives_nothing(self):
        def exits(code, out=b'', err=b''):
            return lambda command, **kwargs: subprocess.CompletedProcess(command, code, out, err)

        def raises(error):
            def run(command, **kwargs):
                raise error
            return run
        failures = {'nothing copied': exits(1, err=b'Nothing is copied\n'),
                    'image only': exits(1, err=b'Clipboard content is not available as requested type "text"\n'),
                    'empty text': exits(0),
                    'stuck owner': raises(subprocess.TimeoutExpired(['wl-paste'], 2)),
                    'cannot start': raises(PermissionError(13, 'denied'))}
        for name, run in failures.items():
            env, which, patched = self._wayland(run)
            with self.subTest(name), env, which, patched:
                self.assertEqual(yakala._clipboard(self._Tk()), 'tk panosu: Şifre')

    def test_clipboard_runs_wl_paste_only_on_wayland(self):
        with patch.dict('os.environ'), patch.object(yakala.subprocess, 'run') as run:
            os.environ.pop('WAYLAND_DISPLAY', None)
            with patch.object(yakala, '_which', return_value='/usr/bin/wl-paste') as which:
                self.assertEqual(yakala._clipboard(self._Tk()), 'tk panosu: Şifre')
            which.assert_not_called()  # macOS, Windows and X11 never look for the tool
            os.environ['WAYLAND_DISPLAY'] = 'wayland-1'
            with patch.object(yakala, '_which', return_value=None):  # wl-clipboard is not installed
                self.assertIsNone(yakala._wl_paste())
                self.assertEqual(yakala._clipboard(self._Tk()), 'tk panosu: Şifre')
        run.assert_not_called()

    def test_clipboard_wl_paste_call_and_decoding(self):
        seen = []
        out = {}

        def run(command, **kwargs):
            seen.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, out['bytes'], b'')
        env, which, patched = self._wayland(run)
        with env, which, patched:
            out['bytes'] = 'Müşteri Şifre Arşiv ığ İI\nikinci satır'.encode('utf-8')
            self.assertEqual(yakala._clipboard(self._Tk()), 'Müşteri Şifre Arşiv ığ İI\nikinci satır')
            out['bytes'] = b'caf\xe9 \xff\xfe son'  # Latin-1 text and stray bytes: replaced, never an exception
            self.assertEqual(yakala._clipboard(self._Tk()), 'caf\ufffd \ufffd\ufffd son')
            out['bytes'] = 'ş'.encode('utf-8') * yakala.CLIP_LIMIT  # twice the cap, cut in the middle of a letter
            with patch.object(yakala, 'CLIP_LIMIT', yakala.CLIP_LIMIT + 1):
                capped = yakala._wl_paste()
            self.assertEqual(capped, 'ş' * (yakala.CLIP_LIMIT // 2) + yakala.CLIP_CUT)
            out['bytes'] = b'a' * yakala.CLIP_LIMIT  # exactly at the cap: untouched
            self.assertEqual(yakala._wl_paste(), 'a' * yakala.CLIP_LIMIT)
        command, kwargs = seen[0]
        self.assertEqual(command, ['/usr/bin/wl-paste', '--no-newline', '--type', 'text'])
        self.assertEqual(kwargs.get('timeout'), yakala.CLIP_TIMEOUT)
        self.assertFalse(kwargs.get('shell'))
        self.assertFalse(kwargs.get('text') or kwargs.get('encoding') or kwargs.get('universal_newlines'))

    @unittest.skipUnless(os.name == 'posix', 'the stand-in wl-paste is a shell script')
    def test_clipboard_with_a_real_wl_paste_process(self):
        tool = Path(self.tmp.name) / 'bin/wl-paste'
        tool.parent.mkdir()
        tool.write_text('#!/bin/sh\ncase "$FAKE_WL" in\n'
                        '  args) printf "%s|" "$@" ;;\n'
                        "  latin) printf 'caf\\351 \\377' ;;\n"
                        '  empty) echo "Nothing is copied" >&2; exit 1 ;;\n'
                        '  stuck) sleep 5 & wait ;;\n'  # like wl-paste: a child (cat) holds the pipe
                        "  *) printf 'https://ornek.com/%%C5%%9F?a=1&b=2' ;;\nesac\n", encoding='utf-8', newline='\n')
        tool.chmod(0o755)

        def read(mode, timeout=60):
            path = str(tool.parent) + os.pathsep + os.environ.get('PATH', '')
            with patch.dict('os.environ', {'PATH': path, 'WAYLAND_DISPLAY': 'wayland-1', 'FAKE_WL': mode}), \
                    patch.object(yakala, 'CLIP_TIMEOUT', timeout):
                return yakala._clipboard(self._Tk())
        self.assertEqual(read('text'), 'https://ornek.com/%C5%9F?a=1&b=2')
        self.assertEqual(read('args'), '--no-newline|--type|text|')
        self.assertEqual(read('latin'), 'caf\ufffd \ufffd')
        self.assertEqual(read('empty'), 'tk panosu: Şifre')
        self.assertEqual(read('stuck', timeout=0.5), 'tk panosu: Şifre')

    def test_fit_keeps_the_longest_prefix_that_fits(self):
        # A fake font: every character is 10 px, so the answer is known without a display.
        class Font:
            def __init__(self, root=None, font=None):
                pass

            def measure(self, text):
                return 10 * len(text)
        class Canvas:  # stands in for a Tk canvas: the cache lives on it
            pass
        first, second = Canvas(), Canvas()
        tkfont = Mock(Font=Font)
        with patch.dict(sys.modules, {'tkinter': Mock(font=tkfont), 'tkinter.font': tkfont}):
            self.assertEqual(yakala._fit(first, 'kisa', ('f', 12), 100), 'kisa')
            self.assertEqual(yakala._fit(first, 'abcdefghijkl', ('f', 12), 60), 'abcde…')
            self.assertEqual(yakala._fit(first, 'ab   cdefgh', ('f', 12), 60), 'ab…')
            self.assertEqual(yakala._fit(first, 'abc', ('f', 12), 5), '…')
            self.assertEqual(list(first._fit_fonts), [('f', 12)])
            # Another window's canvas, even in the same process, builds its own font.
            yakala._fit(second, 'kisa', ('f', 12), 100)
            self.assertIsNot(second._fit_fonts[('f', 12)], first._fit_fonts[('f', 12)])

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
        with patch.object(yakala, 'tools', return_value=none), patch.object(socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]):
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
        self.assertEqual(yakala.repair_mojibake('pahalı şey'.encode('utf-8').decode('cp1252')),
                         'pahalı şey')  # same corrupted input, generated without broken source text
        for text in ('Müşteri Şifre Arşiv', 'café', 'plain'):
            self.assertEqual(yakala.repair_mojibake(text), text)

    def test_clipper_template_is_importable(self):
        template = yakala.clipper_template()
        for field in ('name', 'behavior', 'properties', 'noteContentFormat', 'noteNameFormat', 'path'):
            self.assertIn(field, template)
        self.assertEqual(template['path'], INBOX)
        self.assertEqual(template['noteNameFormat'], '{{date|date:"YYYY-MM-DD-HHmm"}}-{{title|safe_name|kebab|slice:0,48}}')
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
        self.assertEqual(yakala._windows_hotkey_vk('ctrl+alt+b'), (0x4003, ord('B')))
        self.assertEqual(yakala._windows_hotkey_vk('alt+shift+f2'), (0x4005, 0x71))
        self.assertEqual(yakala._windows_hotkey_vk('ctrl+alt+1'), (0x4003, ord('1')))
        for bad in ('cmd+b', 'shift+b', 'ctrl+"'):
            with self.assertRaises(ValueError):
                yakala.windows_hotkey(bad)
        for bad in ('cmd+b', 'ctrl+alt+f25', 'ctrl+alt+#'):
            with self.assertRaises(ValueError):
                yakala._windows_hotkey_vk(bad)

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
        """Patched Linux session: records every external call, answers the few the code reads.

        `self.linux_faults` maps a tool name to an exception to raise or an exit code; names in
        `self.linux_missing` are not on PATH. The stand-in `gsettings set` follows gsettings-tool.c:
        the value is GVariant text, and only a string that does not start with a quote may be bare.
        """
        home = Path(self.tmp.name) / 'home'
        calls = []
        self.linux_options, self.linux_faults, self.linux_missing, self.gnome_keys = [], {}, set(), {}
        self.kde_key_owner = "([(['beyne-at.desktop', '_launch'], [201326658])],)"

        def fake_run(command, **kwargs):
            command = [str(part) for part in command]
            calls.append(command)
            self.linux_options.append(kwargs)
            fault = self.linux_faults.get(Path(command[0]).name)
            if isinstance(fault, BaseException):
                raise fault
            out, code = '', fault or 0
            if code:
                pass
            elif command[:3] == ['gsettings', 'get', yakala.GNOME_KEYS]:
                out = self.gnome_list
            elif command[:2] == ['gsettings', 'set']:
                try:
                    value = ast.literal_eval(command[4])
                except (ValueError, SyntaxError):
                    value = None if command[3] == 'custom-keybindings' or command[4][:1] in ('"', "'") else command[4]
                if value is None:
                    code = 1
                elif command[3] == 'custom-keybindings':
                    self.gnome_list = repr(value) if value else '@as []'
                else:
                    self.gnome_keys[command[3]] = value
            elif command[-1] == 'org.kde.kglobalaccel.Component.isActive':
                out = '(true,)'
            elif command[-1] == '201326658':
                out = self.kde_key_owner
            return subprocess.CompletedProcess(command, code, out.encode(), b'')
        self.gnome_list = gnome_list
        patches = [patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala.subprocess, 'run', fake_run),
                   patch.object(yakala, '_which', lambda *names: next(('/usr/bin/' + name for name in names
                                                                       if name not in self.linux_missing), None)),
                   patch.dict(yakala.os.environ, {'HOME': str(home), 'XDG_DATA_HOME': str(home / 'data'),
                                                  'XDG_CURRENT_DESKTOP': desktop})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return home / 'data/applications/beyne-at.desktop', calls

    def _linux_argv(self, state):
        """The command `kur` binds on Linux: this Python, the state copy, the window, this vault."""
        return [sys.executable, str(Path(state).resolve() / 'yakala' / 'beyin_v3_yakala.py'), 'pencere',
                '--vault', str(self.vault.resolve())]

    def test_wayland_without_wl_paste_warns_even_when_hotkey_works(self):
        self._linux_env('KDE')
        self.linux_missing.add('wl-paste')
        state = Path(self.tmp.name) / 'state'
        with patch.dict(yakala.os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}):
            done = yakala.install(self.vault, state)
            health = yakala.status(self.vault, state)
            self.assertTrue(done['kisayol_calisiyor'])
            self.assertTrue(health['dinleyici_calisiyor'])
            for result, command in ((done, 'kur'), (health, 'durum')):
                self.assertIn('wl-paste', result['pano_uyarisi'])
                self.assertIn('wl-clipboard', yakala.human(result, command))
            self.assertIn('wl-paste', yakala._empty_capture_hint(self.vault, state))

    def test_wayland_clipboard_warning_is_not_a_false_capture_failure(self):
        with patch.object(yakala.sys, 'platform', 'linux'), patch.dict(yakala.os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}):
            with patch.object(yakala, '_which', return_value='/usr/bin/wl-paste'):
                self.assertIsNone(yakala._clipboard_warning())
            with patch.object(yakala, '_which', return_value=None):
                self.assertIn('wl-paste', yakala._clipboard_warning())
        with patch.object(yakala.sys, 'platform', 'linux'), patch.dict(yakala.os.environ, {'WAYLAND_DISPLAY': ''}):
            with patch.object(yakala, '_which', return_value=None):
                self.assertIsNone(yakala._clipboard_warning())  # Tk still reads the X11 clipboard

    def test_linux_install_kde_and_uninstall(self):
        entry, calls = self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'
        done = yakala.install(self.vault, state, spec='ctrl+alt+b')
        self.assertEqual((done['kisayol'], done['kisayol_calisiyor']), ('Ctrl+Alt+B', True))
        # Read back through the spec instead of searching the escaped text: holds for any path, Windows ones too.
        self.assertEqual(desktop_exec_argv(entry.read_text(encoding='utf-8')), self._linux_argv(state))
        self.assertEqual(yakala._desktop_owner(entry), str(self.vault.resolve()))
        self.assertTrue(Path(self._linux_argv(state)[1]).is_file())
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
        self.assertFalse(Path(self._linux_argv(state)[1]).exists())
        # No call can hang the command or reach a shell.
        self.assertTrue(all(options.get('timeout') and not options.get('shell') for options in self.linux_options))

    def test_linux_reinstall_refreshes_existing_state_copy(self):
        entry, _ = self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'
        runner = state / 'yakala' / 'beyin_v3_yakala.py'
        runner.parent.mkdir(parents=True)
        runner.write_text('# eski kopya: font probe yok\n', encoding='utf-8')
        yakala.install(self.vault, state)
        source = yakala.__file__
        assert source is not None
        self.assertEqual(runner.read_bytes(), Path(source).read_bytes())
        self.assertEqual(desktop_exec_argv(entry.read_text(encoding='utf-8')), self._linux_argv(state))

    def test_linux_install_gnome_keeps_existing_bindings(self):
        entry, calls = self._linux_env('ubuntu:GNOME', "['/org/other/custom0/']")
        state = Path(self.tmp.name) / 'state'
        for _ in range(2):
            self.assertEqual(yakala.install(self.vault, state)['kisayol'], 'Ctrl+Alt+B')
        self.assertEqual(yakala._gnome_list(), ['/org/other/custom0/', yakala.GNOME_PATH])
        self.assertEqual((self.gnome_keys['name'], self.gnome_keys['binding']), ('Beyne at', '<Control><Alt>b'))
        self.assertEqual(shlex.split(self.gnome_keys['command']), self._linux_argv(state))
        self.assertEqual(len([c for c in calls if c[:2] == ['gsettings', 'set'] and c[3] == 'custom-keybindings']), 1)
        yakala.uninstall(self.vault, state)
        self.assertEqual(yakala._gnome_list(), ['/org/other/custom0/'])
        self.assertFalse(entry.exists())

    def test_linux_install_other_desktop_gives_hint(self):
        entry, _ = self._linux_env('XFCE')
        done = yakala.install(self.vault, Path(self.tmp.name) / 'state')
        self.assertIsNone(done['kisayol'])
        self.assertIn('pencere --vault', done['ipucu'])
        self.assertEqual(shlex.split(done['ipucu'].split(': ', 1)[1]), self._linux_argv(Path(self.tmp.name) / 'state'))
        self.assertNotIn('kisayol_calisiyor', done)
        self.assertTrue(entry.is_file())
        self.assertIn(done['ipucu'], yakala.human(done, 'kur'))

    def test_desktop_exec_quotes_spaces(self):
        self.assertEqual(yakala._desktop_quote('/tmp/a b/x'), '"/tmp/a b/x"')
        self.assertEqual(yakala._desktop_quote('/tmp/x'), '/tmp/x')

    def test_linux_fallback_saves_clipboard_text_with_reason(self):
        with patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala, '_which', lambda *n: '/usr/bin/' + n[0]), \
                patch.object(yakala, '_try', return_value=(0, 'tek satir neden\n')) as ask:
            result = yakala._popup_fallback(self.vault, {'metin': 'kopyalanan paragraf'})
        self.assertEqual(ask.call_args[0][0][0], '/usr/bin/kdialog')
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

    def _linux_context(self, clip, code=0, wayland=True, missing=()):
        """gather_context() on Linux; `clip` is what the clipboard helper prints. -> (context, commands run)."""
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, code, clip if isinstance(clip, bytes) else clip.encode('utf-8'), b'')
        with patch.object(yakala.sys, 'platform', 'linux'), patch.dict(yakala.os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}), \
                patch.object(yakala, 'linux_browser_context', return_value={}), \
                patch.object(yakala, '_which', lambda *names: next(('/usr/bin/' + n for n in names if n not in missing), None)), \
                patch.object(yakala.subprocess, 'run', run):
            if not wayland:
                del yakala.os.environ['WAYLAND_DISPLAY']
            return yakala.gather_context(), commands

    def test_linux_context_reads_clipboard(self):
        def context(clip):
            return self._linux_context(clip)[0]
        self.assertEqual(context('https://ornek.com/a\n'), {'url': 'https://ornek.com/a'})
        # A link with more lines is text, as in the window on macOS and Windows; the second line is not dropped.
        self.assertEqual(context('https://ornek.com/a\nikinci'), {'metin': 'https://ornek.com/a\nikinci'})
        self.assertEqual(context('duz metin'), {'metin': 'duz metin'})
        self.assertEqual(context('  \n'), {})

    def test_linux_context_classifies_like_the_window(self):
        for clip in ('https://ornek.com/a', ' https://ornek.com/a?x=1&y=2#z \n', 'https://ornek.com/a b', 'http://x',
                     'https://a.com\nhttps://b.com', 'bak: https://ornek.com', 'ftp://x/y', 'Şifre: İstanbul ığ'):
            window_takes_it_as_link = bool(re.match(r'^https?://\S+$', clip.strip()))  # the rule in popup()
            context = self._linux_context(clip)[0]
            self.assertEqual('url' in context, window_takes_it_as_link, clip)
            self.assertEqual(context.get('url') or context.get('metin'), clip.strip(), clip)

    def test_linux_context_reads_through_one_helper(self):
        text = ['-o', '-selection', 'clipboard']
        self.assertEqual(self._linux_context('duz')[1], [['/usr/bin/wl-paste', '--no-newline', '--type', 'text']])
        # Only an image copied: wl-paste --type text exits 1. Nothing is taken; image bytes never become a note.
        self.assertEqual(self._linux_context(b'\x89PNG\r\n\x1a\n\x00\x00', code=1)[0], {})
        self.assertEqual(self._linux_context('duz', wayland=False)[1], [['/usr/bin/xclip'] + text])
        self.assertEqual(self._linux_context('duz', missing=('wl-paste',))[1], [['/usr/bin/xclip'] + text])
        self.assertEqual(self._linux_context('duz', wayland=False, missing=('xclip',))[1], [['/usr/bin/xsel', '-ob']])
        self.assertEqual(self._linux_context('duz', wayland=False, missing=('xclip', 'xsel')), ({}, []))
        self.assertEqual(self._linux_context(b'caf\xe9 \xff')[0], {'metin': 'caf\ufffd \ufffd'})
        capped = self._linux_context(b'a' * (yakala.CLIP_LIMIT + 10))[0]['metin']
        self.assertEqual(capped, 'a' * yakala.CLIP_LIMIT + yakala.CLIP_CUT)

    def test_file_uri_paths(self):
        path = yakala._file_uri_path
        self.assertEqual(path('file:///home/a/not.txt'), '/home/a/not.txt')
        self.assertEqual(path('file:///home/a/a%20b/%C5%9Eifre%20%C4%B1%C4%9F%20%C4%B0.pdf'), '/home/a/a b/Şifre ığ İ.pdf')
        self.assertEqual(path('file:///tmp/%2550%23x%3Fy'), '/tmp/%50#x?y')  # decoded once, not twice
        self.assertEqual(path('file://localhost/tmp/x'), '/tmp/x')
        self.assertEqual(path('file:///tmp/a#b?c'), '/tmp/a#b?c')
        for other in ('file://baska-makine/etc/hosts', 'file:/tmp/x', 'file://', 'https://ornek.com/x', '/tmp/x',
                      'bak file:///tmp/x', ''):
            self.assertIsNone(path(other), other)

    @unittest.skipUnless(os.name == 'posix', 'a Linux file manager copies POSIX paths')
    def test_linux_context_copied_files(self):
        def context(clip):
            return self._linux_context(clip)[0]
        folder = Path(self.tmp.name).resolve()
        first, second = folder / 'a b.txt', folder / 'Şifre ığ %50.pdf'
        for item in (first, second):
            item.write_text('x', encoding='utf-8')
        gone = (folder / 'yok.txt').as_uri()
        self.assertEqual(context(first.as_uri() + '\n'), {'dosyalar': [str(first)]})
        # A text/uri-list as file managers write it: CRLF lines, a comment, one file that no longer exists.
        listing = '# kopyalanan dosyalar\r\n' + '\r\n'.join((first.as_uri(), gone, second.as_uri())) + '\r\n'
        self.assertEqual(context(listing), {'dosyalar': [str(first), str(second)]})
        self.assertEqual(context(gone), {})
        self.assertEqual(context(folder.as_uri()), {})  # a folder is not a file to copy into the vault
        self.assertEqual(context('file:///tmp/a%00b'), {})
        self.assertEqual(len(context('\n'.join([first.as_uri()] * 30))['dosyalar']), 20)
        # Not a list of local files: a URI of another machine, files mixed with text, a comment alone.
        remote = 'file://baska-makine' + first.as_uri()[7:]
        self.assertEqual(context(remote), {'metin': remote})
        mixed = first.as_uri() + '\nbir not'
        self.assertEqual(context(mixed), {'metin': mixed})
        self.assertEqual(context('# yalniz yorum'), {'metin': '# yalniz yorum'})

    def _linux_fallback(self, context, answer=(0, 'neden\n'), tools=('kdialog', 'zenity')):
        """The no-tkinter window on Linux. -> (result, dialog command or None)."""
        with patch.object(yakala.sys, 'platform', 'linux'), patch.object(yakala, '_try', return_value=answer) as ask, \
                patch.object(yakala, '_which', lambda *names: next(('/usr/bin/' + n for n in names if n in tools), None)):
            result = yakala._popup_fallback(self.vault, context)
        return result, (ask.call_args[0][0] if ask.called else None)

    def test_linux_dialog_never_takes_clipboard_text_as_an_option(self):
        hostile = '--password\x00 <b>kalin</b> a_b C:\\new\\tab\n--geticon ' + 'x' * 300
        shown = ('--password  <b>kalin</b> a_b C:\\new\\tab --geticon ' + 'x' * 300)[:120].replace('\\', '\\\\')
        _, kdialog = self._linux_fallback({'metin': hostile})
        self.assertEqual(kdialog, ['/usr/bin/kdialog', '--title', 'Beyne at', '--inputbox', 'Neden kaydediyorsun?\n' + shown, ''])
        _, zenity = self._linux_fallback({'metin': hostile}, tools=('zenity',))
        self.assertEqual(zenity, ['/usr/bin/zenity', '--entry', '--title', 'Beyne at', '--text',
                                  'Neden kaydediyorsun?\n' + shown.replace('_', '__')])
        for command, ours in ((kdialog, ['--title', '--inputbox']), (zenity, ['--entry', '--title', '--text'])):
            self.assertEqual([part for part in command if part.startswith('-')], ours)
            # One fixed first line, then the clipboard on one line: it cannot add an argument, a line or markup of its own.
            self.assertEqual(command[4 if command is kdialog else 5].split('\n')[0], 'Neden kaydediyorsun?')
            self.assertEqual(sum(part.count('\n') for part in command), 1)
            self.assertFalse(any('\x00' in part for part in command))
        # The whole clipboard text is what gets saved, not the shortened line the dialog shows.
        self.assertIn('x' * 300, yakala.cards(self.vault)[0]['body'])
        self.assertEqual(self._linux_fallback({})[1][4], 'Neden kaydediyorsun?')

    def test_linux_dialog_cancel_empty_answer_and_typed_note(self):
        for answer in ((1, ''), (5, ''), (None, '')):  # Cancel, zenity timeout, the tool hung or vanished
            self.assertEqual(self._linux_fallback({'url': 'https://ornek.com/a'}, answer)[0], {'status': 'vazgecildi'})
        self.assertEqual(self._linux_fallback({}, (0, '\n'))[0], {'status': 'vazgecildi'})  # nothing copied, nothing typed
        self.assertEqual(yakala.cards(self.vault), [])
        self.assertEqual(self._linux_fallback({'url': 'https://ornek.com/a'}, (0, '\n'))[0]['status'], 'yakalandi')
        typed = self._linux_fallback({}, (0, 'yalnız yazılan not\n'))[0]
        self.assertEqual(typed['kaynak_turu'], 'metin')
        self.assertIn('yalnız yazılan not', yakala.find_card(self.vault, typed['id'])['body'])

    def test_linux_without_any_window_nothing_is_saved_unseen(self):
        with self.assertRaises(ValueError) as error:
            self._linux_fallback({'metin': 'panodaki gizli metin'}, tools=())
        self.assertEqual(str(error.exception), yakala.LINUX_NO_WINDOW)
        for word in ('tkinter', 'kdialog', 'zenity'):
            self.assertIn(word, yakala.LINUX_NO_WINDOW)
        self.assertEqual(yakala.cards(self.vault), [])
        self.assertFalse((self.vault / INBOX).exists())

    def test_fallback_on_other_systems_never_opens_a_linux_dialog(self):
        with patch.object(yakala, '_linux_ask', side_effect=AssertionError('Linux dialog')), \
                patch.object(yakala, '_osascript', return_value='mac nedeni') as mac, \
                patch.object(yakala, 'default_state', return_value=Path(self.tmp.name) / 'capture-state'):
            with patch.object(yakala.sys, 'platform', 'darwin'):
                saved = yakala._popup_fallback(self.vault, {'url': 'https://ornek.com/mac', 'baslik': 'Başlık', 'uygulama': 'Safari'})
            with patch.object(yakala.sys, 'platform', 'win32'):
                self.assertEqual(yakala._popup_fallback(self.vault, {'uygulama': 'notepad'}), {'status': 'vazgecildi'})
        mac.assert_called_once()
        card = yakala.find_card(self.vault, saved['id'])
        self.assertEqual((card['meta']['url'], card['meta']['baslik'], card['meta']['uygulama']),
                         ('https://ornek.com/mac', 'Başlık', 'Safari'))
        self.assertIn('mac nedeni', card['body'])

    def test_windows_fallback_never_saves_a_found_source_unasked(self):
        context = {'url': 'https://ornek.com/sayfa', 'baslik': 'Sayfa', 'uygulama': 'opera', 'metin': 'pano'}
        with patch.object(yakala.os, 'name', 'nt'), patch.object(yakala.sys, 'platform', 'win32'):
            self.assertEqual(yakala._popup_fallback(self.vault, context), {'status': 'vazgecildi'})
        self.assertEqual(yakala.cards(self.vault), [])

    def test_desktop_entry_round_trips_any_vault_path(self):
        vaults = ['/home/ayşe/İkinci Beyin', '/home/u/📥 Beyin', '/home/u/100% Beyin %U %%f', '/home/u/$HOME `id` $(id)',
                  '/home/u/"çift" \'tek\' tırnak', '/home/u/ters\\bölü\\', '/home/u/satır\nsonu\tsekme\rdönüş',
                  '/home/u/a\u2028b\x85c', 'C:\\Users\\Ayşe\\İkinci Beyin', '/home/u/sade',
                  '/home/u/Ş ğ ü ç ö ı İ 📥 100% $x "q" \\ `b` ~ * ? # ( ) < > | & ; =']
        entry = Path(self.tmp.name) / 'beyne-at.desktop'
        for vault in vaults:
            argv = ['/usr/bin/python3', vault + '/durum/beyin_v3_yakala.py', 'pencere', '--vault', vault]
            text = yakala._desktop_entry(argv, vault)
            self.assertEqual(desktop_exec_argv(text), argv, vault)
            lines = text.split('\n')  # a path cannot add a line, so it cannot add a key
            self.assertEqual([line.split('=')[0] for line in lines], ['[Desktop Entry]', 'Type', 'Name', 'Comment', 'Exec',
                                                                     'Terminal', 'Categories', 'X-Beyin-Vault', ''], vault)
            entry.write_text(text, encoding='utf-8', newline='\n')
            self.assertEqual(yakala._desktop_owner(entry), vault)
        self.assertEqual(yakala._desktop_quote(''), '""')
        self.assertEqual(yakala._desktop_quote('100%'), '"100%%"')
        self.assertEqual(yakala._desktop_quote('a$b\\c'), '"a\\\\$b\\\\\\\\c"')  # \$ and \\ per Exec, then each backslash doubled

    def test_desktop_owner_of_a_missing_or_foreign_file(self):
        entry = Path(self.tmp.name) / 'beyne-at.desktop'
        self.assertIsNone(yakala._desktop_owner(entry))
        entry.write_bytes(b'[Desktop Entry]\nName=caf\xe9\nX-Beyin-Vault=/x\n')  # not UTF-8: not written by kur
        self.assertIsNone(yakala._desktop_owner(entry))
        entry.write_text('[Desktop Entry]\nName=Baska\n', encoding='utf-8')
        self.assertIsNone(yakala._desktop_owner(entry))

    def test_desktop_file_ignores_a_relative_xdg_data_home(self):
        home = Path(self.tmp.name) / 'home'
        default = home / '.local/share/applications/beyne-at.desktop'
        absolute = Path(self.tmp.name).resolve() / 'veri'
        with patch.object(yakala.Path, 'home', return_value=home), patch.dict(yakala.os.environ):
            for value in ('goreli/veri', '.', ''):
                yakala.os.environ['XDG_DATA_HOME'] = value
                self.assertEqual(yakala._desktop_file(), default, value)
            del yakala.os.environ['XDG_DATA_HOME']
            self.assertEqual(yakala._desktop_file(), default)
            yakala.os.environ['XDG_DATA_HOME'] = str(absolute)
            self.assertEqual(yakala._desktop_file(), absolute / 'applications/beyne-at.desktop')

    def test_linux_kde_failures_degrade_to_the_hint(self):
        entry, calls = self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'

        def failed():
            done = yakala.install(self.vault, state)
            self.assertIs(done['kisayol_calisiyor'], False)  # never reported as working when the check did not pass
            self.assertEqual(shlex.split(done['ipucu'].split(': ', 1)[1]), self._linux_argv(state))
            self.assertIn(done['ipucu'], yakala.human(done, 'kur'))
            self.assertIn('UYARI', yakala.human(done, 'kur'))
            self.assertTrue(entry.is_file())
            return done
        for fault in (FileNotFoundError(2, 'gdbus yok'), subprocess.TimeoutExpired(['gdbus'], 5), 1):
            self.linux_faults = {'gdbus': fault}
            failed()
            self.assertIs(yakala.status(self.vault, state)['dinleyici_calisiyor'], False)
        self.linux_faults = {'kwriteconfig6': 1}
        failed()
        self.linux_faults, self.linux_missing = {}, {'kwriteconfig6', 'kwriteconfig5'}
        failed()
        self.linux_missing = {'kwriteconfig6'}  # Plasma 5 name
        self.assertIs(yakala.install(self.vault, state)['kisayol_calisiyor'], True)
        self.assertIn('/usr/bin/kwriteconfig5', [c[0] for c in calls])
        # The key answers, but for another program: registered is not the same as ours.
        self.linux_missing, self.kde_key_owner = set(), "([('kwin', 'KWin', 'kwin', 'KWin', 'x', 'X', [201326658], [0])],)"
        failed()
        self.assertTrue(all(options.get('timeout') and not options.get('shell') for options in self.linux_options))

    def test_gnome_list_parsing(self):
        cases = {'@as []': [], '[]': [], "['/a/', '/b/']": ['/a/', '/b/'], '["/it\'s/"]': ["/it's/"],
                 "['/ş/']": ['/ş/'], '': None, 'bozuk': None, "'/a/'": None, '[1, 2]': None, "['/a/', 2]": None,
                 "{'a': 1}": None, "('/a/',)": None, '[' * 300: None}
        for text, expected in cases.items():
            with patch.object(yakala, '_try', return_value=(0, text + '\n')):
                self.assertEqual(yakala._gnome_list(), expected, text[:20])
        for answer in ((1, ''), (None, '')):
            with patch.object(yakala, '_try', return_value=answer):
                self.assertIsNone(yakala._gnome_list())
        self.assertEqual(yakala._gvariant("it's a \\ path"), "'it\\'s a \\\\ path'")
        self.assertEqual(ast.literal_eval(yakala._gvariant('/opt/my py\'s/"x" \\ Ş')), '/opt/my py\'s/"x" \\ Ş')

    def test_linux_gnome_first_and_only_binding(self):
        entry, calls = self._linux_env('GNOME')  # "@as []": gsettings prints an empty list with its type
        state = Path(self.tmp.name) / 'state'
        self.assertIs(yakala.install(self.vault, state)['kisayol_calisiyor'], True)
        self.assertEqual(self.gnome_list, repr([yakala.GNOME_PATH]))
        self.assertIs(yakala.status(self.vault, state)['dinleyici_calisiyor'], True)
        yakala.uninstall(self.vault, state)
        self.assertEqual(self.gnome_list, '@as []')
        self.assertIn(['gsettings', 'reset-recursively', yakala._gnome_schema()], calls)
        self.assertIsNone(yakala.status(self.vault, state)['dinleyici_calisiyor'])
        self.assertTrue(all(options.get('timeout') and not options.get('shell') for options in self.linux_options))

    def test_linux_gnome_unreadable_list_changes_nothing(self):
        entry, calls = self._linux_env('GNOME', "['/org/other/custom0/', 7]")
        done = yakala.install(self.vault, Path(self.tmp.name) / 'state')
        self.assertIs(done['kisayol_calisiyor'], False)
        self.assertIn('pencere --vault', done['ipucu'])
        self.assertFalse([c for c in calls if c[:2] == ['gsettings', 'set']])
        self.assertEqual(self.gnome_list, "['/org/other/custom0/', 7]")

    def test_linux_gnome_command_survives_a_python_path_with_quotes(self):
        entry, calls = self._linux_env('GNOME', "['/org/other/custom0/']")
        state = Path(self.tmp.name) / 'state'
        with patch.object(yakala.sys, 'executable', "/opt/my py's/100% \"python\""):
            done = yakala.install(self.vault, state)
            argv = self._linux_argv(state)
        self.assertIs(done['kisayol_calisiyor'], True)
        self.assertEqual(shlex.split(self.gnome_keys['command']), argv)
        self.assertEqual(desktop_exec_argv(entry.read_text(encoding='utf-8')), argv)
        self.assertEqual(yakala._gnome_list(), ['/org/other/custom0/', yakala.GNOME_PATH])

    def test_linux_entry_of_another_vault_is_left_alone(self):
        entry, calls = self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'
        other = '/home/baska/Diğer Beyin'
        entry.parent.mkdir(parents=True)
        entry.write_text(yakala._desktop_entry(['python3', 'x.py', 'pencere', '--vault', other], other), encoding='utf-8', newline='\n')
        before = entry.read_bytes()
        self.assertIsNone(yakala.status(self.vault, state)['dinleyici_calisiyor'])
        self.assertNotIn('beyne-at.desktop', yakala.uninstall(self.vault, state)['kaldirilan'])
        self.assertEqual((entry.read_bytes(), calls), (before, []))
        done = yakala.install(self.vault, state)  # like macOS: the one hotkey moves to this vault and says so
        self.assertEqual(done['onceki_vault'], other)
        self.assertEqual(yakala._desktop_owner(entry), str(self.vault.resolve()))
        mine = entry.read_bytes()
        again = yakala.install(self.vault, state)
        self.assertNotIn('onceki_vault', again)
        self.assertEqual((entry.read_bytes(), again['kisayol_calisiyor']), (mine, True))
        self.assertNotIn(b'\r', mine)

    def test_linux_install_warns_when_no_window_can_open(self):
        self._linux_env('KDE')
        state = Path(self.tmp.name) / 'state'
        with patch.dict(sys.modules, {'tkinter': None}):  # import tkinter fails, as on Arch without the tk package
            self.assertNotIn('uyari', yakala.install(self.vault, state))  # kdialog is there
            self.linux_missing = {'kdialog'}
            self.assertNotIn('uyari', yakala.install(self.vault, state))  # zenity is there
            self.linux_missing = {'kdialog', 'zenity'}
            done = yakala.install(self.vault, state)
        self.assertEqual(done['uyari'], yakala.LINUX_NO_WINDOW)
        self.assertIn('UYARI: ' + yakala.LINUX_NO_WINDOW, yakala.human(done, 'kur'))

    def test_linux_state_copy_hands_over_to_the_vault_module(self):
        state = Path(self.tmp.name) / 'state'
        runner = state / 'yakala' / 'beyin_v3_yakala.py'
        current = self.vault / '.claude/scripts/beyin_v3_yakala.py'
        runner.parent.mkdir(parents=True)
        runner.write_text('# kopya\n', encoding='utf-8')
        argv = ['x', 'pencere', '--vault', str(self.vault)]
        with patch.object(yakala.os, 'execv') as execv, patch.object(yakala.sys, 'argv', argv), \
                patch.object(yakala, '_linux_tk_python', return_value=sys.executable, create=True):
            with patch.object(yakala, '__file__', str(runner)):
                yakala._hand_over(self.vault, state)  # the vault has no module of its own: the copy keeps running
                execv.assert_not_called()
                current.parent.mkdir(parents=True)
                current.write_text('# guncel\n', encoding='utf-8')
                yakala._hand_over(self.vault, state)
                execv.assert_called_once_with(sys.executable, [sys.executable, str(current)] + argv[1:])
                execv.side_effect = OSError('exec')
                yakala._hand_over(self.vault, state)  # cannot start it: the copy still opens the window
            execv.reset_mock(side_effect=True)
            with patch.object(yakala, '__file__', str(current)):
                yakala._hand_over(self.vault, state)  # already the vault module
            yakala._hand_over(self.vault, state)  # run from the package, as these tests do
            execv.assert_not_called()

    def test_linux_tk_font_fallback_hands_over_to_system_python(self):
        state = Path(self.tmp.name) / 'state'
        runner = state / 'yakala' / 'beyin_v3_yakala.py'
        current = self.vault / '.claude/scripts/beyin_v3_yakala.py'
        runner.parent.mkdir(parents=True)
        runner.write_text('# state copy\n', encoding='utf-8')
        current.parent.mkdir(parents=True)
        current.write_text('# vault copy\n', encoding='utf-8')
        argv = ['x', 'pencere', '--vault', str(self.vault)]
        with patch.object(yakala, '__file__', str(runner)), patch.object(yakala.sys, 'platform', 'linux'), \
                patch.object(yakala.sys, 'argv', argv), patch.object(yakala.os, 'execv') as execv, \
                patch.object(yakala, '_linux_tk_python', return_value='/usr/bin/python3', create=True):
            yakala._hand_over(self.vault, state)
        execv.assert_called_once_with('/usr/bin/python3', ['/usr/bin/python3', str(current)] + argv[1:])

    def test_linux_tk_python_selects_font_capable_runtime_only(self):
        from subprocess import CompletedProcess, TimeoutExpired
        with patch.object(yakala.sys, 'platform', 'linux'), \
                patch.object(yakala.sys, 'executable', '/conda/bin/python3'), \
                patch.object(yakala.Path, 'is_file', return_value=True), \
                patch.object(yakala.subprocess, 'run', side_effect=[
                    CompletedProcess([], 1), CompletedProcess([], 0),
                    CompletedProcess([], 0),
                    TimeoutExpired([], 3), CompletedProcess([], 1),
                ]) as probe:
            self.assertEqual(yakala._linux_tk_python(), '/usr/bin/python3')
            self.assertEqual(yakala._linux_tk_python(), '/conda/bin/python3')
            self.assertEqual(yakala._linux_tk_python(), '/conda/bin/python3')
            self.assertEqual(probe.call_count, 5)
        with patch.object(yakala.sys, 'platform', 'darwin'), patch.object(yakala.subprocess, 'run') as probe:
            self.assertEqual(yakala._linux_tk_python(), sys.executable)
            probe.assert_not_called()

    def test_linux_tk_python_keeps_current_when_system_tk_missing(self):
        from subprocess import CompletedProcess
        with patch.object(yakala.sys, 'platform', 'linux'), \
                patch.object(yakala.sys, 'executable', '/conda/bin/python3'), \
                patch.object(yakala.Path, 'is_file', return_value=False), \
                patch.object(yakala.subprocess, 'run', return_value=CompletedProcess([], 1)) as probe:
            self.assertEqual(yakala._linux_tk_python(), '/conda/bin/python3')
        self.assertEqual(probe.call_count, 1)  # no attempt to run an absent system Python

    def test_linux_tk_probe_is_isolated_and_rejects_an_older_python(self):
        from subprocess import CompletedProcess
        with patch.object(yakala.sys, 'platform', 'linux'), \
                patch.object(yakala.sys, 'executable', '/conda/bin/python3'), \
                patch.object(yakala.Path, 'is_file', return_value=True), \
                patch.object(yakala.subprocess, 'run', return_value=CompletedProcess([], 1)) as probe:
            yakala._linux_tk_python()
        command = probe.call_args_list[-1].args[0]
        self.assertEqual(command[:3], ['/usr/bin/python3', '-I', '-c'])
        guard = command[3].split('import tkinter')[0]  # the version check runs before any Tk import
        with patch.object(sys, 'version_info', (3, 10, 12)):
            with self.assertRaises(SystemExit) as stopped:
                exec(guard, {})
        self.assertEqual(stopped.exception.code, 1)
        exec(guard, {})  # this interpreter (3.11+) passes

    def test_hand_over_only_for_the_linux_hotkey_process(self):
        argv = ['x', 'pencere', '--vault', str(self.vault)]
        with patch.object(yakala, '_hand_over') as hand, patch.object(yakala, 'gather_context', return_value={}), \
                patch.object(yakala, 'popup', return_value={'status': 'vazgecildi'}), \
                patch.object(yakala.sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO):
            for platform in ('darwin', 'win32'):
                with patch.object(yakala.sys, 'platform', platform):
                    yakala.main()
            with patch.object(yakala.sys, 'platform', 'linux'):
                yakala.main(argv[1:])  # through beyin.py: already the vault module
                yakala.main(['liste', '--vault', str(self.vault)])
                hand.assert_not_called()
                yakala.main()
        hand.assert_called_once()
        self.assertEqual(hand.call_args[0][0], self.vault.resolve())

    def test_mac_install_plist_keepalive(self):
        state = Path(self.tmp.name) / 'mac-state'
        agent = Path(self.tmp.name) / 'agent.plist'
        with patch.object(yakala, '_launch_agent', return_value=agent), \
                patch.object(yakala.sys, 'platform', 'darwin'), \
                patch.object(yakala.os, 'getuid', return_value=501, create=True), \
                patch.object(yakala, '_listener_ok', return_value=True), \
                patch.object(yakala.subprocess, 'run'):
            yakala.install(self.vault, state)
            import plistlib
            plist = plistlib.loads(agent.read_bytes())
            self.assertIs(plist['KeepAlive'], True)

    def mac(self, agent, launchctl):
        """The macOS branches on any OS: a temp LaunchAgent path, `launchctl` answers every subprocess.run."""
        import contextlib
        stack = contextlib.ExitStack()
        for item in (patch.object(yakala, '_launch_agent', return_value=agent), patch.object(yakala.sys, 'platform', 'darwin'),
                     patch.object(yakala.os, 'getuid', return_value=501, create=True), patch('time.sleep'),
                     patch.object(yakala.subprocess, 'run', side_effect=launchctl)):
            stack.enter_context(item)
        return stack

    def test_listener_leaves_the_dock_before_the_event_loop(self):
        import ctypes
        from unittest.mock import MagicMock
        order = []

        def send(target, selector, *rest):
            order.append((target, selector, [value.value for value in rest[1:]]))
            return 'app' if selector == b'sharedApplication' else None
        objc, carbon = MagicMock(), MagicMock()
        objc.objc_getClass.side_effect = lambda name: name
        carbon.InstallEventHandler.return_value = carbon.RegisterEventHotKey.return_value = 0
        carbon.RunApplicationEventLoop.side_effect = lambda: order.append('loop')
        script = SCRIPTS / 'beyin_v3_yakala.py'
        with patch.object(yakala, '_objc', return_value=(objc, send)), patch.object(ctypes, 'CDLL', return_value=carbon):
            yakala.listen_mac(self.vault, script, 103, 0x1A00)
        # NSApplicationActivationPolicyProhibited (2) on the shared application, and only then the loop.
        self.assertEqual(order, [(b'NSApplication', b'sharedApplication', []), ('app', b'setActivationPolicy:', [2]), 'loop'])
        self.assertEqual(carbon.RegisterEventHotKey.call_args.args[:2], (103, 0x1A00))
        # AppKit that cannot be loaded costs the hidden Dock icon, never the hotkey.
        with patch.object(yakala, '_objc', side_effect=OSError('AppKit')), patch.object(ctypes, 'CDLL', return_value=carbon):
            yakala.listen_mac(self.vault, script, 103, 0x1A00)
        self.assertEqual(order[-2:], ['loop', 'loop'])

    def test_mac_install_replaces_the_running_listener(self):
        import plistlib
        state, agent, calls = Path(self.tmp.name) / 'mac-state', Path(self.tmp.name) / 'agent.plist', []
        script = SCRIPTS / 'beyin_v3_yakala.py'

        def launchctl(command, **_options):
            calls.append((command[1], agent.exists()))
            return subprocess.CompletedProcess(command, 0, b'\tstate = running\n', b'')
        with self.mac(agent, launchctl):
            self.assertIs(yakala.install(self.vault, state)['kisayol_calisiyor'], True)
        # The old job leaves launchd before the new plist is loaded; KeepAlive would restart it otherwise.
        self.assertEqual(calls, [('bootout', False), ('bootstrap', True), ('print', True)])
        arguments = plistlib.loads(agent.read_bytes())['ProgramArguments']
        runner = Path(arguments[1])
        self.assertEqual(runner, state.resolve() / 'yakala' / script.name)
        self.assertEqual(arguments[2:5], ['dinle', '--vault', str(self.vault.resolve())])
        self.assertEqual(runner.read_bytes(), script.read_bytes())
        # An update replaces only the vault script; the listener keeps its own copy until `kur` runs again.
        runner.write_text('# the release this listener was installed from\n', encoding='utf-8')
        del calls[:]
        with self.mac(agent, launchctl):
            yakala.install(self.vault, state)
        self.assertEqual(calls, [('bootout', True), ('bootstrap', True), ('print', True)])
        self.assertEqual(runner.read_bytes(), script.read_bytes())

    def test_uninstall_boots_the_listener_out_before_its_files_go(self):
        state, agent, calls = Path(self.tmp.name) / 'mac-state', Path(self.tmp.name) / 'agent.plist', []
        runner = state / 'yakala/beyin_v3_yakala.py'

        def launchctl(command, **_options):
            calls.append((command[1:], agent.exists(), runner.exists()))
            return subprocess.CompletedProcess(command, 0, b'\tstate = running\n', b'')
        with self.mac(agent, launchctl):
            yakala.install(self.vault, state)
            del calls[:]
            result = yakala.uninstall(self.vault, state)
        # A job that is still loaded would be restarted every ten seconds with its script gone.
        self.assertEqual(calls, [(['bootout', 'gui/501/' + yakala.LAUNCH_LABEL], True, True)])
        self.assertIn('LaunchAgent', result['kaldirilan'])
        self.assertFalse(agent.exists() or runner.exists() or (state / 'yakala.json').exists())

    def test_status_tells_a_loaded_but_stopped_listener(self):
        state, agent = Path(self.tmp.name) / 'mac-state', Path(self.tmp.name) / 'agent.plist'
        answer = [b'\tstate = running\n']

        def launchctl(command, **_options):
            return subprocess.CompletedProcess(command, 0, answer[0], b'')
        with self.mac(agent, launchctl):
            yakala.install(self.vault, state)
            self.assertIs(yakala.status(self.vault, state)['dinleyici_calisiyor'], True)
            # What `launchctl print` says after Dock > Quit on a 3.9.0 install: exit status 0, job still loaded.
            answer[0] = b'\tstate = not running\n\tlast exit code = 0\n'
            stopped = yakala.status(self.vault, state)
        self.assertIs(stopped['dinleyici_calisiyor'], False)
        self.assertIn('durmus (baslatmak icin: beyin.py yakala kur)', yakala.human(stopped, 'durum'))

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

    def test_emojiless_inbox_detection_and_install(self):
        vault = Path(self.tmp.name) / 'emojiless-vault'
        vault.mkdir(parents=True)
        (vault / '000-Inbox').mkdir()
        state = Path(self.tmp.name) / 'emojiless-state'
        self.assertEqual(yakala.find_inbox(vault), '000-Inbox/Yakala')
        res = yakala.install(vault, state, hotkey=False)
        self.assertEqual(res['klasor'], '000-Inbox/Yakala')
        self.assertTrue((vault / '000-Inbox/Yakala').is_dir())
        self.assertFalse((vault / '📥 000-Inbox').exists())
        st = yakala.status(vault, state)
        self.assertEqual(st['klasor'], '000-Inbox/Yakala')


class YakalaInboxTest(unittest.TestCase):
    """Which folder holds the cards: the saved choice, the one inbox already in use, the starter, one inbox by word."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-inbox-')
        self.addCleanup(self.tmp.cleanup)
        self.vault, self.state = Path(self.tmp.name) / 'Örnek Beyin', Path(self.tmp.name) / 'state'
        self.vault.mkdir()

    def top(self):
        return sorted(path.name for path in self.vault.iterdir() if not path.name.startswith('.'))

    def test_renamed_inbox_is_reused(self):
        (self.vault / '000-Inbox').mkdir()
        yakala.capture(self.vault, url='https://ornek.com/yazi')
        self.assertFalse((self.vault / '📥 000-Inbox').exists())
        self.assertEqual(len(list((self.vault / '000-Inbox/Yakala').glob('*.md'))), 1)
        self.assertEqual(yakala.clipper_template(yakala.find_inbox(self.vault))['path'], '000-Inbox/Yakala')
        self.assertIn('(000-Inbox/Yakala)', yakala.session_notice(self.vault))

    def test_inbox_choice(self):
        import shutil

        def chosen(*folders):
            for name in folders:
                (self.vault / name).mkdir(parents=True)
            try:
                return yakala.find_inbox(self.vault, self.state)
            finally:
                for path in self.vault.iterdir():
                    shutil.rmtree(path)
        self.assertEqual(chosen(), INBOX)
        self.assertEqual(chosen('00_INBOX', 'Notlar'), '00_INBOX/Yakala')
        self.assertEqual(chosen('GELEN KUTUSU'), 'GELEN KUTUSU/Yakala')
        self.assertEqual(chosen('İNBOX'), 'İNBOX/Yakala')  # str.lower() alone turns this İ into two code points
        self.assertEqual(chosen('📥 000-Inbox', '00_INBOX'), INBOX)  # nothing in use yet: the starter folder wins
        self.assertEqual(chosen('00_INBOX', 'Gelen Kutusu'), INBOX)  # two inboxes, none in use: no guess
        self.assertEqual(chosen('Gelen Belgeler', 'Inbox Arşivi', '🔐 Kasa Inbox', '.inbox', 'Inboxing'), INBOX)
        # Cards that already exist outrank a name: the one inbox that holds Yakala/ is the one in use.
        self.assertEqual(chosen('00_INBOX', 'Gelen Kutusu/Yakala'), 'Gelen Kutusu/Yakala')
        self.assertEqual(chosen('📥 000-Inbox', '00_INBOX/Yakala'), '00_INBOX/Yakala')  # an emptied starter left behind
        self.assertEqual(chosen('📥 000-Inbox/Yakala', '00_INBOX'), INBOX)
        # Yakala/ in more than one is a tie: the starter wins when the vault has it, else no guess.
        self.assertEqual(chosen('📥 000-Inbox/Yakala', '00_INBOX/Yakala'), INBOX)
        self.assertEqual(chosen('📥 000-Inbox', '00_INBOX/Yakala', 'Gelen Kutusu/Yakala'), INBOX)
        self.assertEqual(chosen('00_INBOX/Yakala', 'Gelen Kutusu/Yakala'), INBOX)
        # A Yakala/ inside a folder that is no candidate does not count.
        self.assertEqual(chosen('📥 000-Inbox', 'Inbox Arşivi/Yakala', '🔐 Kasa Inbox/Yakala', '.inbox/Yakala', 'Notlar/Yakala'), INBOX)
        self.assertEqual(chosen('00_INBOX', 'Inbox Arşivi/Yakala', 'Notlar/Yakala'), '00_INBOX/Yakala')

    def test_linked_folder_is_never_picked_or_followed_out(self):
        outside = Path(self.tmp.name) / 'Disari'
        (outside / 'Yakala').mkdir(parents=True)  # even a link whose target already holds Yakala/
        try:
            (self.vault / 'Inbox').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('this account cannot create symlinks')
        self.assertEqual(yakala.find_inbox(self.vault, self.state), INBOX)
        (self.vault / '📥 000-Inbox').mkdir()
        self.assertEqual(yakala.find_inbox(self.vault, self.state), INBOX)
        with self.assertRaises(ValueError):
            yakala.install(self.vault, self.state, hotkey=False, folder_spec='Inbox/Yakala')
        self.assertEqual((list((outside / 'Yakala').iterdir()), self.top(), self.state.exists()),
                         ([], ['Inbox', '📥 000-Inbox'], False))

    def test_inbox_words_are_the_doctors(self):
        import unicodedata
        import beyin_v3_hygiene as hygiene
        self.assertEqual(yakala.INBOX_WORDS.pattern, hygiene.INBOX_WORDS.pattern)
        self.assertEqual(yakala.SENSITIVE_WORDS.pattern, hygiene.SENSITIVE_WORDS.pattern)
        for name in ('📥 000-Inbox', '00_INBOX', 'GELEN KUTUSU', 'İNBOX', 'ınbox', 'MÜŞTERİLER',
                     unicodedata.normalize('NFD', 'Inbox Arşivi')):
            self.assertEqual(yakala._name_words(name), hygiene._name_words(name), name)
        for name in ('📥 000-Inbox', '00_INBOX', 'Gelen Kutusu', 'GelenKutum', 'Gelen Belgeler', '🔐 Kasa Inbox', 'Notlar'):
            doctor = hygiene._inbox_folder(name, None) and not hygiene.sensitive_excluded(name)
            self.assertEqual(yakala._inbox_name(name), doctor, name)
        # The doctor reports an archive that carries the word; yakala never writes into one by a guess.
        self.assertTrue(hygiene._inbox_folder('Inbox Arşivi', None))
        self.assertFalse(yakala._inbox_name(unicodedata.normalize('NFD', 'Inbox Arşivi')))

    def test_folder_outside_the_vault_is_refused_before_anything_is_written(self):
        outside = Path(self.tmp.name) / 'Disari'
        for spec in ('../Disari/Yakala', str(outside / 'Yakala'), 'Notlar/../../Disari', '', '.', '.obsidian/Yakala'):
            with self.assertRaises(ValueError, msg=spec):
                yakala.install(self.vault, self.state, hotkey=False, folder_spec=spec)
        with self.assertRaises(ValueError):
            yakala.main(['kur', '--kisayol-yok', '--klasor', '../Disari/Yakala'], vault=self.vault, state=self.state)
        self.assertEqual((outside.exists(), self.state.exists(), list(self.vault.iterdir())), (False, False, []))
        (self.vault / 'not.md').write_text('# not\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            yakala.chosen_inbox(self.vault, 'not.md')
        # Inside the vault every spelling means the same folder.
        for spec in ('Notlar/Yakala', 'Notlar\\Yakala/', 'Notlar/Gecici/../Yakala', str(self.vault / 'Notlar/Yakala')):
            self.assertEqual(yakala.chosen_inbox(self.vault, spec), 'Notlar/Yakala', spec)

    def test_chosen_folder_is_read_from_the_state_it_was_saved_in(self):
        import io
        installed = yakala.install(self.vault, self.state, hotkey=False, folder_spec='Notlar/Yakala')
        self.assertEqual(installed['klasor'], 'Notlar/Yakala')
        self.assertEqual(json.loads((self.state / 'yakala.json').read_text(encoding='utf-8'))['klasor'], 'Notlar/Yakala')
        self.assertEqual(yakala.status(self.vault, self.state)['klasor'], 'Notlar/Yakala')

        def cli(*argv):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                yakala.main([*argv, '--json'], vault=self.vault, state=self.state)
            return json.loads(out.getvalue())
        self.assertEqual(cli('durum')['klasor'], 'Notlar/Yakala')
        self.assertTrue(cli('ekle', '--metin', 'deneme')['path'].startswith('Notlar/Yakala/'))
        self.assertEqual(cli('liste')['bekleyen'], 1)
        self.assertEqual(cli('sablon')['path'], 'Notlar/Yakala')
        self.assertIn('Yakalanan 1 kaynak bekliyor (Notlar/Yakala)', yakala.session_notice(self.vault, self.state))
        self.assertEqual(self.top(), ['Notlar'])
        # Another state directory knows nothing of that choice; `kur` without --klasor keeps it.
        self.assertEqual(yakala.find_inbox(self.vault, Path(self.tmp.name) / 'other-state'), INBOX)
        again = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual(again['klasor'], 'Notlar/Yakala')
        template = json.loads((self.vault / again['web_clipper_sablonu']).read_text(encoding='utf-8'))
        skill = (self.vault / '.agents/skills/beyin-yakala/SKILL.md').read_text(encoding='utf-8')
        self.assertEqual(template['path'], 'Notlar/Yakala')
        self.assertIn('`Notlar/Yakala/`', skill)
        self.assertNotIn('000-Inbox', skill)

    def test_existing_cards_keep_their_folder_and_are_never_moved(self):
        # A vault as 3.9.0 left it: cards under the starter path, the user's own inbox beside it, no saved folder.
        (self.vault / INBOX).mkdir(parents=True)
        (self.vault / '000-Inbox').mkdir()
        waiting = yakala.capture(self.vault, url='https://ornek.com/eski', state=self.state)
        done = yakala.find_card(self.vault, yakala.capture(self.vault, text='bitti', state=self.state)['id'], self.state)
        yakala.write_card(done['path'], dict(done['meta'], durum='islendi'), done['body'])
        self.state.mkdir()
        (self.state / 'yakala.json').write_text('{"schema": 1, "session_notice": true, "kisayol": null, "tus": "ctrl+alt+b"}\n',
                                                encoding='utf-8')
        self.assertTrue(waiting['path'].startswith(INBOX + '/'))
        self.assertEqual((yakala.find_inbox(self.vault, self.state), yakala.pending(self.vault, self.state)), (INBOX, 1))
        same = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual((same['klasor'], 'onceki_klasor' in same), (INBOX, False))
        # The user picks the other folder: the queue that stays behind is reported, not moved.
        moved = yakala.install(self.vault, self.state, hotkey=False, folder_spec='000-Inbox/Yakala')
        self.assertEqual((moved['klasor'], moved['onceki_klasor'], moved['onceki_klasorde_kalan']), ('000-Inbox/Yakala', INBOX, 1))
        self.assertIn('UYARI: 1 kart eski klasorde kaldi (' + INBOX + ')', yakala.human(moved, 'kur'))
        self.assertTrue((self.vault / waiting['path']).is_file())
        self.assertEqual(yakala.pending(self.vault, self.state), 0)
        self.assertIn('000-Inbox/Yakala/', yakala.capture(self.vault, text='yeni', state=self.state)['path'])

    def test_saved_folder_that_is_gone_or_edited_is_not_trusted(self):
        (self.vault / '000-Inbox').mkdir()
        self.assertEqual(yakala.install(self.vault, self.state, hotkey=False)['klasor'], '000-Inbox/Yakala')
        yakala.capture(self.vault, text='not', state=self.state)
        (self.vault / '000-Inbox').rename(self.vault / '00_INBOX')  # the saved folder no longer exists
        self.assertEqual((yakala.find_inbox(self.vault, self.state), yakala.pending(self.vault, self.state)), ('00_INBOX/Yakala', 1))
        (Path(self.tmp.name) / 'Disari').mkdir()
        for saved in ('"../Disari"', '7', '[]'):
            (self.state / 'yakala.json').write_text('{"schema": 1, "klasor": ' + saved + '}\n', encoding='utf-8')
            self.assertEqual(yakala.find_inbox(self.vault, self.state), '00_INBOX/Yakala', saved)

    def test_raw_text_and_files_stay_out_of_git_in_any_folder(self):
        none = {name: None for name in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
        source = Path(self.tmp.name) / 'not.txt'
        source.write_text('dosyadaki ders', encoding='utf-8')
        yakala.install(self.vault, self.state, hotkey=False, folder_spec='Notlar/Kaynaklar')
        yakala.capture(self.vault, files=[source], state=self.state)
        with patch.object(yakala, 'tools', return_value=none):
            result = yakala.process(self.vault, state=self.state)
        self.assertEqual([entry['ham'].rsplit('/', 1)[0] for entry in result['kartlar']], ['Notlar/Kaynaklar/.ham'])
        for private in ('.ham', 'dosyalar'):
            self.assertEqual((self.vault / 'Notlar/Kaynaklar' / private / '.gitignore').read_text(encoding='utf-8'), '*\n')

    def test_nfd_folder_name_round_trips(self):
        import unicodedata
        nfc = 'Günlük Inbox'
        (self.vault / unicodedata.normalize('NFD', nfc)).mkdir()
        on_disk = self.top()[0]  # the file system decides which form it keeps
        installed = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual(installed['klasor'], on_disk + '/Yakala')
        self.assertTrue(yakala.capture(self.vault, text='not', state=self.state)['path'].startswith(on_disk + '/Yakala/'))
        template = json.loads((self.vault / installed['web_clipper_sablonu']).read_text(encoding='utf-8'))
        self.assertEqual((template['path'], yakala.status(self.vault, self.state)['klasor']), (on_disk + '/Yakala',) * 2)
        self.assertIn('(' + on_disk + '/Yakala)', yakala.session_notice(self.vault, self.state))
        if (self.vault / nfc).is_dir():  # APFS and HFS+ read both forms as one name; ext4 and NTFS keep them apart
            again = yakala.install(self.vault, self.state, hotkey=False, folder_spec=nfc + '/Yakala')
            self.assertEqual((again['klasor'], 'onceki_klasor' in again), (on_disk + '/Yakala', False))
            self.assertEqual((self.top(), yakala.pending(self.vault, self.state)), ([on_disk], 1))


class YakalaVideoLanguageTest(unittest.TestCase):
    """#277: the transcript is in the language spoken in the video; the card names the language it stored
    and warns only on evidence of a wrong one (a known mismatch, or a dubbed video of unknown language)."""

    def setUp(self):
        public_dns_fixture(self)
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-dil-')
        self.addCleanup(self.tmp.cleanup)
        self.vault, self.bin = Path(self.tmp.name) / 'Örnek Beyin', Path(self.tmp.name) / 'bin'
        self.vault.mkdir()
        self.bin.mkdir()
        (self.bin / 'fake_tool.py').write_text(FAKE_TOOL, encoding='utf-8')
        environ = clean_environ()
        environ.start()
        self.addCleanup(environ.stop)
        real_run = yakala._run

        def run(command, timeout, cwd=None):  # the real subprocess call, aimed at the stand-in
            self.assertGreater(timeout, 0)
            return real_run([sys.executable, str(self.bin / 'fake_tool.py'), Path(command[0]).name, *command[1:]], timeout, cwd)
        patcher = patch.object(yakala, '_run', run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def isle(self, scenario, npx=True, yt_dlp=True, whisper=False, url=VIDEO, **options):
        """Capture `url` once, process the queue against `scenario`; returns (result entry, card meta, raw text)."""
        (self.bin / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
        (self.bin / 'calls.jsonl').write_text('', encoding='utf-8')
        found = {'npx': 'npx' if npx else None, 'yt_dlp': 'yt-dlp' if yt_dlp else None,
                 'whisper': (whisper if isinstance(whisper, str) else 'whisper') if whisper else None,
                 'pdftotext': None, 'ffmpeg': None}
        card = next((card['id'] for card in yakala.cards(self.vault) if card['meta'].get('url') == url), None) \
            or yakala.capture(self.vault, url=url)['id']
        with patch.object(yakala, 'tools', return_value=found):
            entry = yakala.process(self.vault, [card], **options)['kartlar'][0]
        raw = (self.vault / entry['ham']).read_text(encoding='utf-8') if entry.get('ham') else ''
        return entry, yakala.find_card(self.vault, card)['meta'], raw

    def calls(self, tool=None):
        lines = (self.bin / 'calls.jsonl').read_text(encoding='utf-8').splitlines()
        return [call for call in map(json.loads, lines) if tool in (None, call[0])]

    def track_requests(self):
        """What each yt-dlp subtitle request asked for: (--sub-langs value, automatic?)."""
        return [(call[call.index('--sub-langs') + 1], '--write-auto-subs' in call) for call in self.calls('yt-dlp')
                if '--sub-langs' in call]

    def test_dubbed_video_asks_defuddle_for_the_spoken_language(self):
        answers = {'': defuddle('ar', ARABIC), 'en-US': defuddle('en', ENGLISH)}
        entry, meta, raw = self.isle({'video': dubbed(), 'defuddle': answers})
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        self.assertEqual((entry['durum'], entry['yontem'], entry['dil']), ('cikarildi', 'defuddle', 'en'))
        self.assertEqual(meta['dil'], 'en')
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [])  # the language matched: no subtitle download at all
        self.assertEqual(len(self.calls('yt-dlp')), 1)
        # An article never costs a yt-dlp request and its card gains no language field.
        page = {'title': 'Yazı', 'language': 'tr', 'content': 'Uzun makale metni. ' * 20}
        entry, meta, _raw = self.isle({'video': dubbed(), 'defuddle': {'': page}}, url='https://ornek.com/yazi')
        self.assertEqual(entry['yontem'], 'defuddle')
        self.assertEqual(self.calls('yt-dlp'), [])
        self.assertNotIn('--lang', self.calls('npx')[0])
        self.assertFalse({'dil', 'altyazi', 'uyari'} & set(meta))

    def test_wrong_defuddle_language_gives_way_to_the_spoken_track(self):
        # Defuddle keeps answering with the Arabic dub even when asked for en-US.
        entry, meta, raw = self.isle({'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'speech': {'auto:en-orig': ENGLISH}})
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        self.assertEqual((entry['yontem'], entry['dil'], entry['altyazi']), ('yt-dlp-altyazi', 'en', 'en-orig (otomatik)'))
        self.assertIn('![](' + VIDEO + ')\n\n## Transcript\n\n**0:01** So, I gave', raw)
        self.assertEqual((meta['dil'], meta['altyazi']), ('en', 'en-orig (otomatik)'))
        self.assertNotIn('uyari', meta)
        self.assertEqual(self.track_requests(), [('en-orig', True)])
        self.assertIn('--lang', self.calls('npx')[0])

    def test_dubbed_video_without_defuddle_requests_one_track_not_every_orig(self):
        entry, meta, raw = self.isle({'video': dubbed(), 'speech': {'auto:en-orig': ENGLISH}}, npx=False)
        self.assertEqual((entry['durum'], entry.get('yontem')), ('cikarildi', 'yt-dlp-altyazi'))
        self.assertIn(ENGLISH, raw)  # not `ar-orig`, the first `-orig` track in the alphabet
        self.assertEqual(self.track_requests(), [('en-orig', True)])  # asking for all 21 is what drew HTTP 429
        self.assertEqual((meta.get('dil'), meta.get('altyazi')), ('en', 'en-orig (otomatik)'))

    def test_http_429_on_the_first_track_request_moves_to_the_next_track(self):
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['tr'], recognised=['tr-orig', 'tr'], translated=['en']),
                    'speech': {'auto:tr-orig': TURKISH}, 'fail_first': 1}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['durum'], entry.get('altyazi')), ('cikarildi', 'tr-orig (otomatik)'))
        self.assertIn(TURKISH, raw)
        self.assertEqual(self.track_requests(), [('tr', False), ('tr-orig', True)])
        self.assertEqual(meta['dil'], 'tr')
        self.assertNotIn('uyari', meta)

    def test_wrong_language_is_kept_only_with_a_warning(self):
        # Every track request is refused: the dub transcript is all there is.
        scenario = {'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'fail_first': 9}
        entry, meta, raw = self.isle(scenario, whisper=True)
        self.assertEqual((entry['durum'], entry['yontem'], meta.get('dil')), ('cikarildi', 'defuddle', 'ar'))
        self.assertIn(ARABIC, raw)
        for part in ('konusma dilinde olmayabilir', 'video en-US', 'transkript ar'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertEqual(self.track_requests(), [('en-orig', True)])
        # Audio stays the last resort for a video with no transcript at all; nothing was downloaded here.
        self.assertEqual([call for call in self.calls('yt-dlp') if '-f' in call], [])
        # A later run that gets the track replaces the warning it wrote.
        entry, meta, raw = self.isle(dict(scenario, fail_first=0, speech={'auto:en-orig': ENGLISH}))
        self.assertEqual((meta['dil'], meta['altyazi']), ('en', 'en-orig (otomatik)'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn(ARABIC, raw)

    def test_unknown_language_of_a_single_language_video_is_recorded_without_a_warning(self):
        # Like jNQXAC9IVRw: manual tracks only, no speech recognition, so yt-dlp names no language.
        # Nothing points at a dub, so the stored language on the card is enough.
        scenario = {'video': video(None, audio=[(None, -1)], manual=['de', 'en']), 'defuddle': {'': defuddle('en', ENGLISH)},
                    'speech': {'manual:en': ENGLISH}}
        entry, meta, raw = self.isle(scenario)
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'en'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertNotIn('--lang', self.calls('npx')[0])
        self.assertEqual(self.track_requests(), [])
        # Without Defuddle a track is still taken, and the card names it.
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('altyazi')), ('yt-dlp-altyazi', 'en', 'en (elle)'))
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [('en', False)])

    def test_unknown_language_of_a_dubbed_video_warns(self):
        # Two audio languages and neither marked original: whatever is stored may be a dub.
        scenario = {'video': video('tr', audio=[('tr', 5), ('en', -1)], recognised=['en-orig', 'tr-orig'], translated=['de']),
                    'defuddle': {'': defuddle('en', ENGLISH)}}
        entry, meta, raw = self.isle(scenario)
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'en'))
        for part in ('dublajli', 'konusulan dil belirlenemedi', 'transkript dili: en'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertNotIn('--lang', self.calls('npx')[0])
        # No audio language at all, but speech recognised in two languages: the track taken is named.
        scenario = {'video': video(None, recognised=['en-orig', 'tr-orig']), 'speech': {'auto:tr-orig': TURKISH}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('altyazi')), ('yt-dlp-altyazi', 'tr', 'tr-orig (otomatik)'))
        for part in ('dublajli', 'kullanilan altyazi izi: tr-orig (otomatik)'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertEqual(self.track_requests(), [('tr-orig', True)])

    def test_audio_fallback_warns_only_for_a_dubbed_video_of_unknown_language(self):
        # No captions of any kind: the audio is transcribed. With two audio languages and no original it may be a dub.
        scenario = {'video': video(None, audio=[('tr', 5), ('en', -1)]), 'audio': TURKISH}
        entry, meta, raw = self.isle(scenario, npx=False, whisper=True)
        self.assertEqual((entry['durum'], entry.get('yontem')), ('cikarildi', 'ses-whisper'))
        self.assertIn(TURKISH, raw)
        for part in ('dublajli', 'ses asil dilde olmayabilir'):
            self.assertIn(part, meta.get('uyari', ''))
        self.assertNotIn('dil', meta)
        # One audio language: its transcript is the spoken language, nothing to warn about.
        entry, meta, raw = self.isle(dict(scenario, video=video('tr', audio=[('tr', -1)])), npx=False, whisper=True)
        self.assertEqual((entry['yontem'], meta.get('dil'), meta.get('uyari')), ('ses-whisper', 'tr', None))
        self.assertNotIn('not', entry)

    def whisper_call(self):
        return next(call for call in self.calls() if call[0].startswith('whisper'))

    def test_audio_fallback_names_the_spoken_language_and_the_chosen_model(self):
        # yt-dlp named the spoken language: Whisper is told it instead of guessing from the intro.
        scenario = {'video': video('tr', audio=[('tr', -1)]), 'audio': TURKISH}
        with patch.dict(os.environ, {'BEYIN_WHISPER_MODEL': ''}):
            entry, meta, raw = self.isle(scenario, npx=False, whisper=True)
        call = self.whisper_call()
        self.assertEqual((entry['yontem'], call[call.index('--language') + 1]), ('ses-whisper', 'tr'))
        self.assertNotIn('--model', call)  # the tool's own default unless the user picks one
        with patch.dict(os.environ, {'BEYIN_WHISPER_MODEL': 'large-v3-turbo'}):
            self.isle(scenario, npx=False, whisper=True)
        call = self.whisper_call()
        self.assertEqual(call[call.index('--model') + 1], 'large-v3-turbo')
        # Unknown or dubbed: no language flag, Whisper detects it as before.
        with patch.dict(os.environ, {'BEYIN_WHISPER_MODEL': ''}):
            self.isle(dict(scenario, video=video(None, audio=[('tr', 5), ('en', -1)])), npx=False, whisper=True)
        self.assertNotIn('--language', self.whisper_call())

    def test_cuda_failure_in_whisper_ctranslate2_is_retried_on_the_cpu(self):
        # ctranslate2 picks CUDA itself; without cuBLAS it fails yet exits 0. The CPU run is kept and the card says why.
        cublas = 'RuntimeError: Library cublas64_12.dll is not found or cannot be loaded'
        scenario = {'video': video('tr', audio=[('tr', -1)]), 'audio': TURKISH, 'whisper_error': cublas,
                    'whisper_works_on': 'cpu'}
        entry, meta, raw = self.isle(scenario, npx=False, whisper='whisper-ctranslate2')
        self.assertEqual((entry['yontem'], meta.get('dil')), ('ses-whisper', 'tr'))
        self.assertIn(TURKISH, raw)
        runs = self.calls('whisper-ctranslate2')
        self.assertEqual([call[call.index('--device') + 1] if '--device' in call else None for call in runs], [None, 'cpu'])
        self.assertIn('--language', runs[1])  # the retry keeps the language and model it was given
        for part in ('CPU ile', 'cublas64_12.dll'):
            self.assertIn(part, meta.get('uyari', ''))

    def test_empty_whisper_output_also_retries_cuda_and_reports_failure(self):
        scenario = {'video': video('tr', audio=[('tr', -1)]), 'audio': TURKISH,
                    'whisper_error': 'RuntimeError: Library cublas64_12.dll is not found',
                    'whisper_works_on': 'cpu', 'whisper_empty_output': True}
        entry, meta, raw = self.isle(scenario, npx=False, whisper='whisper-ctranslate2')
        self.assertEqual(entry['yontem'], 'ses-whisper')
        self.assertIn(TURKISH, raw)
        self.assertEqual(len(self.calls('whisper-ctranslate2')), 2)
        entry, meta, _raw = self.isle(dict(scenario, whisper_works_on='never'),
                                    npx=False, whisper='whisper-ctranslate2')
        self.assertEqual(entry['durum'], 'hata')
        self.assertIn('Whisper metin uretmedi', entry.get('not', ''))

    def test_whisper_failure_names_whisper_error_instead_of_a_missing_tool(self):
        # Both runs fail (or the tool is not ctranslate2): the card carries Whisper's own last error line.
        scenario = {'video': video('tr', audio=[('tr', -1)]), 'audio': TURKISH,
                    'whisper_error': 'RuntimeError: CUDA failed with error out of memory', 'whisper_works_on': 'never'}
        entry, meta, raw = self.isle(scenario, npx=False, whisper='whisper-ctranslate2')
        self.assertEqual(len(self.calls('whisper-ctranslate2')), 2)
        self.assertIn('Whisper metin uretmedi: RuntimeError: CUDA failed', entry.get('not', '') + meta.get('uyari', ''))
        self.assertNotIn('bulunamadi', entry.get('not', ''))
        # openai-whisper falls back to the CPU on its own: no retry, only the error is named.
        entry, meta, raw = self.isle(dict(scenario, whisper_error='Killed', whisper_exit=1), npx=False, whisper=True)
        self.assertEqual(len(self.calls('whisper')), 1)
        self.assertIn('Whisper metin uretmedi: Killed', entry.get('not', '') + meta.get('uyari', ''))

    def test_whisper_language_flag_takes_only_a_plain_language_code(self):
        self.assertEqual(yakala._whisper_options('pt-BR')[1], ['--language', 'pt'])
        for odd in (None, '', 'und&calc', 'x' * 9):
            self.assertEqual(yakala._whisper_options(odd)[1], [])

    def test_refused_metadata_request_keeps_defuddle_and_asks_for_no_track(self):
        entry, meta, raw = self.isle({'video': None, 'defuddle': {'': defuddle('ar', ARABIC)}})
        self.assertEqual((entry['yontem'], meta.get('dil')), ('defuddle', 'ar'))
        self.assertNotIn('uyari', meta)  # an unanswered request is no evidence of a dub
        self.assertNotIn('not', entry)
        self.assertEqual(self.track_requests(), [])  # no blind request for tracks it could not list
        self.assertEqual(len(self.calls('yt-dlp')), 1)

    def test_turkish_video_manual_track_beats_automatic(self):
        tracks = {'recognised': ['tr-orig', 'tr'], 'translated': ['en', 'ar']}
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['en', 'tr'], **tracks),
                    'speech': {'manual:tr': TURKISH, 'auto:tr-orig': 'otomatik ' * 20}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertIn(TURKISH, raw)  # the manual track, with Ş, ğ, ü, ç, ö, İ and ı intact
        self.assertEqual((meta.get('dil'), meta.get('altyazi')), ('tr', 'tr (elle)'))
        self.assertEqual(self.track_requests(), [('tr', False)])
        # No manual Turkish track: the recognised one, never the English translation or the manual `en`.
        scenario = {'video': video('tr', audio=[('tr', -1)], manual=['en'], **tracks), 'speech': {'auto:tr-orig': TURKISH}}
        entry, meta, raw = self.isle(scenario, npx=False)
        self.assertEqual((meta['dil'], meta['altyazi']), ('tr', 'tr-orig (otomatik)'))
        self.assertIn(TURKISH, raw)
        self.assertNotIn('uyari', meta)
        # With Defuddle the Turkish transcript is verified and no track is downloaded.
        entry, meta, raw = self.isle(dict(scenario, defuddle={'tr': defuddle('tr', TURKISH)}))
        self.assertEqual((entry['yontem'], meta['dil'], meta.get('altyazi'), meta.get('uyari')), ('defuddle', 'tr', None, None))
        self.assertEqual(self.track_requests(), [])

    def test_without_ytdlp_defuddle_stands_and_its_language_is_recorded(self):
        entry, meta, raw = self.isle({'defuddle': {'': defuddle('ar', ARABIC)}}, yt_dlp=False)
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'defuddle'))
        self.assertIn(ARABIC, raw)  # as before: there is nothing to check it against
        self.assertEqual((meta.get('dil'), entry.get('dil')), ('ar', 'ar'))  # visible on the card, which is enough
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)
        self.assertEqual(self.calls('yt-dlp'), [])
        self.assertNotIn('--lang', self.calls('npx')[0])
        # No transcript and no yt-dlp: the old note, no language claim.
        entry, meta, raw = self.isle({'defuddle': {'': defuddle('en')}}, yt_dlp=False)
        self.assertIn('Altyazi bulunamadi', entry['not'])
        self.assertFalse({'dil', 'altyazi', 'uyari'} & set(meta))

    def test_spoken_language_and_track_order_from_metadata(self):
        def tracks(data):
            with tempfile.TemporaryDirectory() as work, patch.object(yakala, '_run', return_value=(0, json.dumps(data), '')):
                return yakala.youtube_tracks(VIDEO, {'yt_dlp': 'yt-dlp'}, work)
        self.assertEqual(tracks(dubbed()), {'dil': 'en-US', 'dublaj': True, 'adaylar': [('en-orig', True)]})
        # Region codes: the exact track, then the bare language, then another region; a manual one first.
        german = video('de-DE', audio=[('de-DE', -1)], manual=['de', 'en'], recognised=['de-DE-orig', 'de-DE', 'de-orig'], translated=['de'])
        self.assertEqual(tracks(german), {'dil': 'de-DE', 'dublaj': False,
                                          'adaylar': [('de', False), ('de-DE-orig', True), ('de-orig', True)]})
        # No `language`, one recognised track: that is what is spoken.
        self.assertEqual(tracks(video(None, recognised=['ja-orig', 'ja'], translated=['en', 'tr'])),
                         {'dil': 'ja', 'dublaj': False, 'adaylar': [('ja-orig', True), ('ja', True)]})
        # Several audio languages and none marked original: nothing to go on, Turkish and English first.
        unmarked = video('tr', audio=[('tr', 5), ('en', -1), ('en-desc', -10)], recognised=['en-orig', 'tr-orig', 'ar-orig'], translated=['de'])
        self.assertEqual(tracks(unmarked), {'dil': None, 'dublaj': True,
                                            'adaylar': [('tr-orig', True), ('en-orig', True), ('ar-orig', True)]})
        # Evidence of a dub, each kind alone: two audio languages, or speech recognised in two languages.
        self.assertEqual(tracks(video(None, audio=[('tr', 5), ('en', -1)])), {'dil': None, 'dublaj': True, 'adaylar': []})
        self.assertEqual(tracks(video(None, recognised=['en-orig', 'tr-orig'])),
                         {'dil': None, 'dublaj': True, 'adaylar': [('tr-orig', True), ('en-orig', True)]})
        # No evidence: one language throughout. An audio description (`en-desc`) and a second recognised
        # track of the same language are not dubs, and neither is a video that names no language at all.
        described = video(None, audio=[('en', -1), ('en-desc', -10)], recognised=['en-orig', 'en-US-orig'])
        self.assertEqual(tracks(described), {'dil': None, 'dublaj': False, 'adaylar': [('en-US-orig', True), ('en-orig', True)]})
        self.assertEqual(tracks(video(None, audio=[(None, -1)], manual=['de', 'en'])),
                         {'dil': None, 'dublaj': False, 'adaylar': [('en', False), ('de', False)]})
        # A language that is not a language code is not passed on to a command line.
        self.assertIsNone(tracks(video('--evil', recognised=['en-orig', 'tr-orig']))['dil'])
        odd = video('en', manual=['live_chat', 'all', '-en', 'en.*', 'en'], recognised=['en-orig'])
        self.assertEqual(tracks(odd)['adaylar'], [('en', False), ('en-orig', True)])
        # A playlist address answers with its videos; the first one stands for it.
        self.assertEqual(tracks({'_type': 'playlist', 'entries': [dubbed(), german]})['dil'], 'en-US')
        for unusable in (['not', 'a', 'video'], {'_type': 'playlist', 'entries': []}):
            self.assertIsNone(tracks(unusable))
        self.assertIsNone(yakala.youtube_tracks(VIDEO, {'yt_dlp': None}, self.tmp.name))


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

    @unittest.skipUnless(os.name == 'posix', 'the stand-in tools are shell wrappers on PATH')
    def test_isle_stores_the_spoken_language_of_a_dubbed_video(self):
        tools = Path(self.tmp.name) / 'bin'
        tools.mkdir()
        (tools / 'fake_tool.py').write_text(FAKE_TOOL, encoding='utf-8')
        scenario = {'video': dubbed(), 'defuddle': {'': defuddle('ar', ARABIC)}, 'speech': {'auto:en-orig': ENGLISH}}
        (tools / 'scenario.json').write_text(json.dumps(scenario), encoding='utf-8')
        for name in ('npx', 'yt-dlp'):
            (tools / name).write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(tools / 'fake_tool.py'), name])
                                      + ' "$@"\n', encoding='utf-8')
            (tools / name).chmod(0o755)
        # The installed entry is a new interpreter, so its DNS fixture travels through a temporary
        # sitecustomize alongside the stand-in tools. Production code has no fixture/environment bypass.
        (tools / 'sitecustomize.py').write_text(
            "import socket\n"
            "socket.getaddrinfo = lambda host, port, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]\n",
            encoding='utf-8')
        self.env['PYTHONPATH'] = str(tools)
        self.env['PATH'] = isolated_path(tools)
        added = self.entry('ekle', VIDEO, '--neden', 'dublaj')
        entry = self.entry('isle', '--ses-yok')['kartlar'][0]
        self.assertEqual((entry['durum'], entry.get('yontem'), entry.get('dil')), ('cikarildi', 'yt-dlp-altyazi', 'en'))
        meta = yakala.find_card(self.vault, added['id'])['meta']
        self.assertEqual((meta.get('dil'), meta.get('altyazi'), meta.get('uyari')), ('en', 'en-orig (otomatik)', None))
        raw = (self.vault / entry['ham']).read_text(encoding='utf-8')
        self.assertIn(ENGLISH, raw)
        self.assertNotIn(ARABIC, raw)
        calls = [json.loads(line) for line in (tools / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
        asked = [call[call.index('--sub-langs') + 1] for call in calls if '--sub-langs' in call]
        self.assertEqual(asked, ['en-orig'])
        self.assertEqual([call[0] for call in calls], ['yt-dlp', 'npx', 'yt-dlp'])

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

        def stop():  # a failed assertion must not leave the listener behind
            with patch.dict(yakala.os.environ, {'APPDATA': self.env['APPDATA']}):
                yakala._windows_stop_listener()
        self.addCleanup(stop)
        installed = self.entry('kur')
        self.assertEqual(installed['kisayol'], 'Ctrl+Alt+B')
        start = appdata / 'Microsoft/Windows/Start Menu/Programs/Beyne At.lnk'
        sendto = appdata / 'Microsoft/Windows/SendTo/Beyne At.lnk'
        # One startup entry per vault; the hash keeps another vault's `kaldir` away from it.
        startup = appdata / ('Microsoft/Windows/Start Menu/Programs/Startup/Beyne At Dinleyici ' +
                             yakala._windows_digest(self.vault)[:8] + '.lnk')
        self.assertTrue(start.is_file() and sendto.is_file())
        self.assertTrue(startup.is_file())
        self.assertIsInstance(yakala.windows_context(), dict)
        st = self.entry('durum')
        self.assertTrue(st['kurulu'])
        self.assertIn(st['dinleyici_calisiyor'], (True, False))
        self.entry('kaldir')
        self.assertFalse(start.exists() or sendto.exists() or startup.exists())

    def test_windows_utf8_output_without_encoding_error(self):
        env = dict(self.env)
        env.pop('PYTHONIOENCODING', None)
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', 'durum'],
                                capture_output=True, env=env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'tamam', result.stdout)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux desktop entry and the command to bind')
    def test_linux_desktop_entry_and_hint(self):
        # A desktop that is neither KDE nor GNOME: nothing is registered, so no session is touched.
        data = Path(self.tmp.name) / 'veri'
        self.env = dict(self.env, XDG_DATA_HOME=str(data), XDG_CURRENT_DESKTOP='yakala-test')
        installed = self.entry('kur')
        self.assertIsNone(installed['kisayol'])
        entry = data / 'applications/beyne-at.desktop'
        argv = desktop_exec_argv(entry.read_text(encoding='utf-8'))
        self.assertEqual(argv[2:], ['pencere', '--vault', str(self.vault.resolve())])
        self.assertEqual(shlex.split(installed['ipucu'].split(': ', 1)[1]), argv)
        runner = Path(argv[1])
        self.assertEqual((runner.parent.name, runner.is_file()), ('yakala', True))
        self.assertIn(self.state.resolve(), runner.resolve().parents)
        # The entry names a Python and a module that start: same command, a read-only subcommand in place of the window.
        listed = subprocess.run(argv[:2] + ['liste', '--json'] + argv[3:], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=60)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIsNone(self.entry('durum')['dinleyici_calisiyor'])
        self.assertIn('beyne-at.desktop', self.entry('kaldir')['kaldirilan'])
        self.assertFalse(entry.exists() or runner.exists())

    def test_user_skill_with_same_name_is_kept(self):
        own = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        own.parent.mkdir(parents=True)
        own.write_text('kendi skill\'im\n', encoding='utf-8')
        self.entry('kur', '--kisayol-yok')
        self.entry('kaldir')
        self.assertEqual(own.read_text(encoding='utf-8'), 'kendi skill\'im\n')

    def test_renamed_inbox_through_the_installed_entry(self):
        (self.vault / '000-Inbox').mkdir()
        refused = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', 'kur', '--kisayol-yok', '--klasor',
                                  '../Disari/Yakala'], capture_output=True, text=True, encoding='utf-8', env=self.env,
                                 cwd=self.vault, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('vault\'un icinde olmali', refused.stderr)
        self.assertFalse((Path(self.tmp.name) / 'Disari').exists() or (self.state / 'yakala.json').exists())
        installed = self.entry('kur', '--kisayol-yok')
        self.assertEqual(installed['klasor'], '000-Inbox/Yakala')
        self.assertEqual(installed['web_clipper_sablonu'], '000-Inbox/Yakala/beyne-at-web-clipper.json')
        self.assertTrue(self.entry('ekle', 'https://ornek.com/yazi')['path'].startswith('000-Inbox/Yakala/'))
        self.assertIn('Yakalanan 1 kaynak bekliyor (000-Inbox/Yakala)', self.session_start())
        self.assertEqual((self.entry('durum')['klasor'], self.entry('sablon')['path']), ('000-Inbox/Yakala',) * 2)
        skill = (self.vault / '.agents/skills/beyin-yakala/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('`000-Inbox/Yakala/`', skill)
        self.assertFalse((self.vault / '📥 000-Inbox').exists())


# A captured URL (clipboard, Web Clipper card, shared link) is content the user did not write.
# On Windows a helper resolved to a .cmd/.bat shim is launched through cmd.exe, which acts on
# & | < > ^ ( ) " % in the arguments. The URL used below carries those characters plus a tail
# that, if cmd.exe ran it, would create a marker file inside the test's own temp folder (and
# nothing else). The command-building logic runs on every OS; the execution checks are Windows only.
META_URL = 'https://example.test/watch?v=1&break>MARKER.txt&a|b(c)^d%TEMP%'
MARKER = 'MARKER.txt'


class YakalaWindowsCommandUnitTest(unittest.TestCase):
    """Pure command-building rules in _windows_safe_command; no child process is spawned."""

    def setUp(self):
        public_dns_fixture(self)

    def test_unsafe_is_oserror_so_callers_fall_back(self):
        # via_defuddle/via_subtitles/via_audio already catch OSError and take the fallback path.
        self.assertTrue(issubclass(yakala._UnsafeBatchArgument, OSError))

    def test_real_executable_is_left_untouched(self):
        command = ['C:/Tools/yt-dlp.exe', '-x', META_URL, '--no-playlist']
        self.assertEqual(yakala._windows_safe_command(command), command)

    def test_npm_shim_is_rewritten_to_direct_node_call(self):
        with tempfile.TemporaryDirectory(prefix='v3-winshim-') as tmp:
            node = Path(tmp) / 'node.exe'
            script = Path(tmp) / 'node_modules' / 'npm' / 'bin' / 'npx-cli.js'
            npx = Path(tmp) / 'npx.cmd'
            script.parent.mkdir(parents=True)
            for path in (node, script, npx):
                path.write_text('', encoding='utf-8')
            command = [str(npx), '-y', yakala.DEFUDDLE, 'parse', META_URL, '--json', '--markdown']
            rewritten = yakala._windows_safe_command(command)
        # node.exe takes its arguments through CreateProcess, so cmd.exe is never involved and the
        # URL survives as a single argument next to the trailing flags.
        self.assertEqual(rewritten, [str(node), str(script), '-y', yakala.DEFUDDLE, 'parse',
                                     META_URL, '--json', '--markdown'])
        self.assertTrue(rewritten[0].lower().endswith('node.exe'))

    def test_npm_shim_without_node_layout_is_declined(self):
        with tempfile.TemporaryDirectory(prefix='v3-winshim-') as tmp:
            npx = Path(tmp) / 'npx.cmd'
            npx.write_text('', encoding='utf-8')
            with self.assertRaises(yakala._UnsafeBatchArgument):
                yakala._windows_safe_command([str(npx), '-y', yakala.DEFUDDLE, 'parse', META_URL])

    def test_other_batch_helper_with_metacharacter_is_declined(self):
        for arg in (META_URL, 'https://e.test/%41', 'a&b', 'a|b', 'a>b', 'a"b', 'a(b)', 'a^b',
                    'line\nbreak'):
            with self.assertRaises(yakala._UnsafeBatchArgument):
                yakala._windows_safe_command(['C:/tools/tool.bat', arg])

    def test_batch_helper_with_plain_arguments_is_allowed(self):
        command = ['C:/tools/tool.bat', 'parse', 'https://example.test/plain-path']
        self.assertEqual(yakala._windows_safe_command(command), command)

    def test_via_defuddle_falls_back_when_run_declines(self):
        # When _run declines a batch shim it raises an OSError, which via_defuddle already catches;
        # the page path then takes via_stdlib. (os.name is not patched here: pathlib picks its
        # flavour from it, so faking 'nt' off-Windows is unsafe.)
        with patch.object(yakala, '_run', side_effect=yakala._UnsafeBatchArgument('npx.cmd')):
            result = yakala.via_defuddle('https://example.test/x?a=1&b=2', {'npx': 'npx.cmd'})
        self.assertIsNone(result)


@unittest.skipUnless(os.name == 'nt', 'Windows cmd.exe argument handling')
class YakalaWindowsRunTest(unittest.TestCase):
    """Real child processes on Windows. On 3.9.0 these fail: the marker command runs and the URL
    is truncated at the first metacharacter; with the fix cmd.exe is never reached."""

    def setUp(self):
        public_dns_fixture(self)
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-winrun-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.marker = self.work / MARKER
        old = os.getcwd()
        os.chdir(self.work)  # so a relative MARKER.txt can only land inside the temp folder
        self.addCleanup(os.chdir, old)

    def _batch(self, name):
        path = self.work / name
        path.write_text('@echo off\r\nexit /b 0\r\n', encoding='ascii')
        return path

    def _node_layout(self):
        real = shutil.which('node')
        if not real:
            self.skipTest('node.exe not on PATH')
        node = self.work / 'node.exe'
        shutil.copy(real, node)
        recorder = self.work / 'node_modules' / 'npm' / 'bin' / 'npx-cli.js'
        recorder.parent.mkdir(parents=True)
        recorder.write_text("const fs=require('fs'),path=require('path');\n"
                            "fs.writeFileSync(path.join(__dirname,'received.json'),"
                            "JSON.stringify(process.argv.slice(2)));\n"
                            "process.stdout.write(JSON.stringify({content:'x'.repeat(120)}));\n",
                            encoding='ascii')
        self._batch('npx.cmd')  # what 3.9.0 would run instead
        return self.work / 'npx.cmd', recorder

    def test_npm_shim_runs_through_node_with_url_intact(self):
        npx, recorder = self._node_layout()
        command = [str(npx), '-y', 'parse', META_URL, '--json', '--markdown']
        yakala._run(command, timeout=60, cwd=self.work)
        self.assertFalse(self.marker.exists())  # cmd.exe never executed the injected tail
        received = json.loads((recorder.parent / 'received.json').read_text(encoding='utf-8'))
        self.assertEqual(received, ['-y', 'parse', META_URL, '--json', '--markdown'])

    def test_via_defuddle_bypasses_cmd(self):
        npx, _ = self._node_layout()
        result = yakala.via_defuddle(META_URL, {'npx': str(npx)})
        self.assertFalse(self.marker.exists())
        self.assertEqual(result, 'x' * 120)

    def test_npx_without_node_layout_is_declined(self):
        npx = self._batch('npx.cmd')  # no sibling node.exe / npx-cli.js
        with self.assertRaises(OSError):
            yakala._run([str(npx), '-y', 'parse', META_URL, '--json'], timeout=60, cwd=self.work)
        self.assertFalse(self.marker.exists())

    def test_other_batch_helper_is_declined(self):
        tool = self._batch('tool.bat')
        with self.assertRaises(OSError):
            yakala._run([str(tool), META_URL], timeout=60, cwd=self.work)
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
