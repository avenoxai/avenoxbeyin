#!/usr/bin/env python3
"""Optional capture ("yakala"): a one-key popup, a browser clipper template and a source queue.

Capturing never touches the network: a card is one Markdown file in the inbox folder.
`isle` is the only step that fetches, and only for cards the user captured. Nothing here
runs unless the user calls it; `kur` is the explicit opt-in for the hotkey and the
SessionStart notice (state/yakala.json, outside the preferences schema so rollback stays safe).
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True

INBOX = '📥 000-Inbox/Yakala'
RAW = '.ham'
FILES = 'dosyalar'
STATE_FILE = 'yakala.json'
DEFUDDLE = 'defuddle@0.19.4'  # pinned: npx runs exactly this release
LAUNCH_LABEL = 'com.avenoxbeyin.yakala'
MAC_HOTKEY = 'Control+Option+B'  # Option+Command+B is the bookmarks key in Chrome and Safari
WIN_HOTKEY = 'CTRL+ALT+B'
STATUSES = ('bekliyor', 'cikarildi', 'islendi', 'hata')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ---------------------------------------------------------------- cards

def inbox(vault):
    return Path(vault) / INBOX


def now():
    return datetime.now().astimezone()


def slug(text, limit=48):
    text = str(text or '').replace('ı', 'i').replace('İ', 'I')
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii').lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return (text[:limit].rstrip('-') or 'not')


def kind_of(url=None, files=(), app=None):
    if files:
        return 'dosya'
    if not url:
        return 'mail' if app and re.search(r'mail|outlook|spark|thunderbird', app, re.I) else 'metin'
    host = (urlparse(url).hostname or '').lower().removeprefix('www.').removeprefix('m.')
    path = urlparse(url).path.lower()
    if host in ('youtube.com', 'youtu.be', 'music.youtube.com'):
        return 'youtube'
    if host in ('x.com', 'twitter.com', 'fxtwitter.com', 'vxtwitter.com'):
        return 'x'
    if host in ('mail.google.com', 'outlook.live.com', 'outlook.office.com', 'mail.proton.me'):
        return 'mail'
    if host == 'github.com':
        return 'github'
    if path.endswith('.pdf'):
        return 'pdf'
    return 'makale'


def canonical(url):
    """Same video or page captured twice must land on one card."""
    if not url:
        return ''
    parts = urlparse(url.strip())
    host = (parts.hostname or '').lower().removeprefix('www.').removeprefix('m.')
    if host == 'youtu.be':
        return 'youtube:' + parts.path.strip('/')
    if host.endswith('youtube.com'):
        video = parse_qs(parts.query).get('v', [''])[0] or parts.path.rsplit('/', 1)[-1]
        return 'youtube:' + video
    if host in ('x.com', 'twitter.com'):
        match = re.search(r'/status/(\d+)', parts.path)
        if match:
            return 'x:' + match.group(1)
    return host + parts.path.rstrip('/') + ('?' + parts.query if parts.query else '')


def _value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.startswith("'") and raw.endswith("'") and len(raw) > 1:
        return raw[1:-1].replace("''", "'")
    return raw


def read_card(path):
    text = Path(path).read_text(encoding='utf-8')
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        match = re.match(r'^([A-Za-z_][\w-]*):\s?(.*)$', line)
        if match:
            meta[match.group(1)] = _value(match.group(2))
    return meta, text[end + 4:].lstrip('\n')


def _dump(meta, body):
    lines = ['---']
    for key, value in meta.items():
        if value is None or value == '':
            continue
        lines.append(key + ': ' + json.dumps(value, ensure_ascii=False))
    return '\n'.join(lines) + '\n---\n\n' + body.rstrip() + '\n'


def write_card(path, meta, body):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.yakala-', dir=path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(_dump(meta, body))
    os.replace(temp, path)


def cards(vault):
    folder = inbox(vault)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob('*.md')):
        try:
            meta, body = read_card(path)
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get('tur') == 'yakala':
            found.append({'path': path, 'id': path.stem, 'meta': meta, 'body': body})
    return found


def find_card(vault, card_id):
    for card in cards(vault):
        if card['id'] == card_id or card['path'].name == card_id:
            return card
    raise ValueError('Kart bulunamadi: ' + str(card_id))


def pending(vault):
    """Cheap for SessionStart: one directory listing, a 600-byte head read per card."""
    folder = inbox(vault)
    count = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if entry.name.endswith('.md') and entry.is_file():
            try:
                with open(entry.path, encoding='utf-8', errors='ignore') as handle:
                    head = handle.read(600)
            except OSError:
                continue
            if re.search(r'^durum:\s*"?bekliyor"?\s*$', head, re.M) and re.search(r'^tur:\s*"?yakala"?\s*$', head, re.M):
                count += 1
    return count


def _unique(path):
    path = Path(path)
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(path.stem + '-' + str(number) + path.suffix)
        if not candidate.exists():
            return candidate
    raise ValueError('Ayni adla cok fazla dosya var: ' + path.name)


def capture(vault, url=None, text=None, files=(), why='', app=None, title=None, tool='masaustu'):
    """Write one card; a repeated URL only gains the new reason. No network."""
    vault = Path(vault)
    url = (url or '').strip() or None
    text = (text or '').strip() or None
    why = (why or '').strip()
    files = [Path(f).expanduser() for f in files]
    if not (url or text or files):
        raise ValueError('Yakalanacak bir sey yok: URL, metin ya da dosya ver.')
    folder = inbox(vault)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now()
    if url:
        key = canonical(url)
        for card in cards(vault):
            if card['meta'].get('url') and canonical(card['meta']['url']) == key:
                body = card['body'].rstrip()
                addition = '\n\n## Yeniden yakalandi (' + stamp.strftime('%Y-%m-%d %H:%M') + ')\n' + (why or '_(not yok)_')
                if text:
                    addition += '\n\n' + _quote(text)
                write_card(card['path'], card['meta'], body + addition)
                return {'status': 'mevcut_karta_eklendi', 'id': card['id'], 'path': str(card['path'].relative_to(vault))}
    kind = kind_of(url, files, app)
    if not title:
        title = (files[0].name if files else None) or (url if url else _first_line(text))
    copied = []
    for source in files:
        if not source.is_file():
            raise ValueError('Dosya bulunamadi: ' + str(source))
        target = _unique(folder / FILES / source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target.relative_to(vault).as_posix())
    name = stamp.strftime('%Y-%m-%d-%H%M') + '-' + (url_slug(url) if url and title == url else slug(title))
    path = _unique(folder / (name + '.md'))
    meta = {'tur': 'yakala', 'durum': 'bekliyor', 'kaynak_turu': kind, 'baslik': _first_line(title, 160),
            'url': url, 'yakalandi': stamp.isoformat(timespec='seconds'), 'arac': tool, 'uygulama': app,
            'visibility': 'private' if kind in ('mail', 'metin', 'dosya') else 'internal'}
    body = ['# ' + _first_line(title, 160), '', '## Neden', '', why or '_(not yok)_']
    if url:
        body += ['', '## Kaynak', '', url]
    if text:
        body += ['', '## Secim', '', _quote(text)]
    if copied:
        body += ['', '## Dosyalar', ''] + ['- [[' + item + ']]' for item in copied]
    write_card(path, meta, '\n'.join(body))
    return {'status': 'yakalandi', 'id': path.stem, 'kaynak_turu': kind, 'path': path.relative_to(vault).as_posix()}


def url_slug(url):
    """Readable file name before the real title is known: youtube-<id>, x-<user>-<id>, site-last-segment."""
    key = canonical(url)
    if key.startswith('youtube:'):
        return 'youtube-' + slug(key[8:], 20)
    parts = urlparse(url)
    if key.startswith('x:'):
        return slug('x-' + parts.path.strip('/').split('/')[0] + '-' + key[2:])
    host = (parts.hostname or 'link').lower().removeprefix('www.').split('.')[0]
    tail = next((seg for seg in reversed(parts.path.split('/')) if seg), '')
    return slug(host + '-' + re.sub(r'\.[a-z0-9]{2,5}$', '', tail))


def _first_line(text, limit=80):
    line = next((part.strip() for part in str(text or '').splitlines() if part.strip()), '')
    return line[:limit] or 'Not'


def _quote(text):
    return '\n'.join('> ' + line if line else '>' for line in str(text).strip().splitlines())


# ---------------------------------------------------------------- extraction

def _which(*names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _run(command, timeout, cwd=None):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', NO_COLOR='1')
    result = subprocess.run(command, capture_output=True, timeout=timeout, cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return result.returncode, result.stdout.decode('utf-8', 'replace'), result.stderr.decode('utf-8', 'replace')


def tools():
    """Optional helpers; each one only widens what `isle` can do."""
    whisper = _which('mlx_whisper', 'whisper-ctranslate2', 'whisper')
    return {'npx': _which('npx'), 'yt_dlp': _which('yt-dlp', 'yt_dlp'), 'whisper': whisper,
            'pdftotext': _which('pdftotext'), 'ffmpeg': _which('ffmpeg')}


def via_defuddle(url, found, info=None):
    """Main text as Markdown; title, author and date go into `info` when given."""
    if not found['npx']:
        return None
    try:
        code, out, _ = _run([found['npx'], '-y', DEFUDDLE, 'parse', url, '--json', '--markdown'], timeout=180)
        data = json.loads(out) if code == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if info is not None:
        info.update({key: str(data[key]).strip() for key in ('title', 'author', 'published', 'site')
                     if data.get(key) and str(data[key]).strip()})
    text = str(data.get('content') or '').strip()
    return text if len(text) > 80 else None


def vtt_text(raw):
    """WebVTT to paragraphs that start with a timestamp roughly every 30 seconds; rolling duplicates removed."""
    paragraphs, last, mark, stamp = [], None, -999, None
    for line in raw.splitlines():
        line = line.strip()
        match = re.match(r'^(?:(\d+):)?(\d+):(\d+)[.,]\d+\s+-->', line)
        if match:
            hours, minutes, seconds = (int(part or 0) for part in match.groups())
            stamp = hours * 3600 + minutes * 60 + seconds
            continue
        if not line or line.startswith(('WEBVTT', 'Kind:', 'Language:', 'NOTE')) or line.isdigit() or '-->' in line:
            continue
        line = re.sub(r'<[^>]+>', '', line).strip()
        if not line or line == last:
            continue
        last = line
        if stamp is not None and stamp - mark >= 30 or not paragraphs:
            mark = stamp if stamp is not None else mark
            label = '%d:%02d' % divmod(stamp or 0, 60) if (stamp or 0) < 3600 else '%d:%02d:%02d' % (stamp // 3600, stamp % 3600 // 60, stamp % 60)
            paragraphs.append(['**' + label + '**', line])
        else:
            paragraphs[-1].append(line)
    return '\n\n'.join(' '.join(words) for words in paragraphs).strip()


def via_subtitles(url, found, workdir):
    if not found['yt_dlp']:
        return None
    command = [found['yt_dlp'], '--skip-download', '--write-subs', '--write-auto-subs',
               '--sub-langs', '.*-orig,tr,en', '--sub-format', 'vtt', '--no-playlist',
               '-o', str(Path(workdir) / 'sub.%(ext)s'), url]
    try:
        _run(command, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    def rank(path):  # original-language track first, then a human Turkish/English one
        name = path.name
        return (0 if name.endswith('-orig.vtt') else 1 if name.endswith('.tr.vtt') else 2 if name.endswith('.en.vtt') else 3, name)
    subs = sorted(Path(workdir).glob('sub*.vtt'), key=rank)
    for path in subs:
        text = vtt_text(path.read_text(encoding='utf-8', errors='replace'))
        if len(text) > 80:
            return text
    return None


def via_audio(url, found, workdir):
    """Last resort: download only the audio, transcribe locally, delete the audio."""
    if not (found['yt_dlp'] and found['whisper']):
        return None
    audio = Path(workdir) / 'ses'
    try:
        _run([found['yt_dlp'], '-f', 'bestaudio/best', '--no-playlist', '-o', str(audio) + '.%(ext)s', url], timeout=900)
        files = sorted(Path(workdir).glob('ses.*'))
        if not files:
            return None
        whisper = Path(found['whisper']).name.lower()
        if whisper.startswith('mlx_whisper'):
            command = [found['whisper'], str(files[0]), '--model', 'mlx-community/whisper-large-v3-turbo',
                       '-f', 'txt', '-o', str(workdir)]
        else:
            command = [found['whisper'], str(files[0]), '--output_format', 'txt', '--output_dir', str(workdir)]
        _run(command, timeout=7200)
        texts = sorted(Path(workdir).glob('ses*.txt'))
        return texts[0].read_text(encoding='utf-8', errors='replace').strip() if texts else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for path in Path(workdir).glob('ses.*'):
            if path.suffix != '.txt':
                path.unlink(missing_ok=True)


def via_stdlib(url):
    """No helper installed: plain HTTP fetch and a rough HTML-to-text."""
    from html.parser import HTMLParser
    from urllib.request import Request, urlopen

    class Text(HTMLParser):
        skip = {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg', 'form', 'aside'}

        def __init__(self):
            super().__init__()
            self.parts, self.depth, self.title, self.in_title = [], 0, '', False

        def handle_starttag(self, tag, attrs):
            if tag in self.skip:
                self.depth += 1
            if tag == 'title':
                self.in_title = True
            if tag in ('p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'tr', 'div', 'section', 'article'):
                self.parts.append('\n')

        def handle_endtag(self, tag):
            if tag in self.skip and self.depth:
                self.depth -= 1
            if tag == 'title':
                self.in_title = False

        def handle_data(self, data):
            if self.in_title:
                self.title += data
            elif not self.depth:
                self.parts.append(data)

    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (beyin-yakala)'})
        with urlopen(request, timeout=30) as response:
            if 'html' not in response.headers.get('Content-Type', 'text/html'):
                return None
            html = response.read(5_000_000).decode(response.headers.get_content_charset() or 'utf-8', 'replace')
    except Exception:
        return None
    parser = Text()
    parser.feed(html)
    text = re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(parser.parts))).strip()
    return (('# ' + parser.title.strip() + '\n\n') if parser.title.strip() else '') + text if len(text) > 200 else None


def _section(body, name):
    match = re.search(r'^## ' + name + r'\s*$(.*?)(?=^## |\Z)', body, re.M | re.S)
    return match.group(1).strip() if match else ''


def _without_section(body, name, replacement):
    return re.sub(r'^## ' + name + r'\s*$.*?(?=^## |\Z)', '## ' + name + '\n\n' + replacement + '\n\n', body, count=1, flags=re.M | re.S)


def extract(vault, card, found, allow_audio=True, info=None):
    """Return (text, method, note). Prefer what is already captured, then the cheapest fetch."""
    meta, body = card['meta'], card['body']
    url, kind = meta.get('url'), meta.get('kaynak_turu') or kind_of(meta.get('url'))
    clipped = _section(body, 'Icerik') or _section(body, 'İçerik')
    if clipped and len(clipped) > 200 and kind != 'youtube':
        return clipped, 'web-clipper', None
    if kind == 'youtube' and url:
        text = via_defuddle(url, found, info)
        if text and re.search(r'^## Transcript', text, re.M):
            return text, 'defuddle', None
        with tempfile.TemporaryDirectory(prefix='beyin-yakala-') as workdir:
            subtitles = via_subtitles(url, found, workdir)
            if subtitles:
                return ((text + '\n\n## Transcript\n\n') if text else '') + subtitles, 'yt-dlp-altyazi', None
            if allow_audio:
                spoken = via_audio(url, found, workdir)
                if spoken:
                    return ((text + '\n\n## Transcript\n\n') if text else '') + spoken, 'ses-whisper', None
        if text:
            return text, 'defuddle', 'Altyazi bulunamadi; yalniz baslik ve aciklama var. Ses yolu icin yt-dlp ve whisper gerekir.'
        return None, None, 'YouTube icerigi alinamadi: npx (Node), yt-dlp ya da whisper bulunamadi veya video erisilemez.'
    if kind == 'mail':
        selected = _section(body, 'Secim')
        if selected:
            return selected, 'secim', 'Mailin tamami icin ajan, varsa Gmail/Outlook baglantisiyla thread\'i okusun.'
        return None, None, 'Mail icerigi giris gerektiriyor; Web Clipper ile mail acikken yakala ya da metni secip yakala.'
    if kind == 'metin':
        return _section(body, 'Secim') or body, 'metin', None
    if kind == 'dosya':
        parts = []
        for link in re.findall(r'\[\[([^\]]+)\]\]', _section(body, 'Dosyalar')):
            path = Path(vault) / link
            if path.suffix.lower() in ('.md', '.txt', '.csv', '.json', '.html', '.srt', '.vtt'):
                parts.append('## ' + path.name + '\n\n' + path.read_text(encoding='utf-8', errors='replace'))
            elif path.suffix.lower() == '.pdf' and found['pdftotext']:
                try:
                    code, out, _ = _run([found['pdftotext'], '-layout', str(path), '-'], timeout=120)
                    if code == 0 and out.strip():
                        parts.append('## ' + path.name + '\n\n' + out)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if parts:
            return '\n\n'.join(parts), 'dosya', None
        return None, 'ajan-okusun', 'Dosyayi ajan dogrudan okusun (gorsel, PDF ya da ikili dosya).'
    if url:
        text = via_defuddle(url, found, info)
        if text:
            return text, 'defuddle', None
        text = via_stdlib(url)
        if text:
            return text, 'http', None
        return None, None, 'Sayfa alinamadi (giris gerekebilir); Web Clipper ile sayfa acikken yakala.'
    return _section(body, 'Secim') or None, 'metin', None


def process(vault, card_ids=None, allow_audio=True, retry=False):
    vault = Path(vault)
    found = tools()
    targets = [find_card(vault, cid) for cid in card_ids] if card_ids else \
        [c for c in cards(vault) if c['meta'].get('durum') == 'bekliyor' or (retry and c['meta'].get('durum') == 'hata')]
    results = []
    for card in targets:
        meta, body = dict(card['meta']), card['body']
        try:
            info = {}
            text, method, note = extract(vault, card, found, allow_audio, info)
        except Exception as exc:  # one broken source must not stop the queue
            text, method, note, info = None, None, type(exc).__name__ + ': ' + str(exc)[:200], {}
        entry = {'id': card['id'], 'kart': card['path'].relative_to(vault).as_posix(),
                 'kaynak_turu': meta.get('kaynak_turu'), 'baslik': meta.get('baslik'),
                 'neden': _section(body, 'Neden'), 'url': meta.get('url')}
        if info.get('title') and (not meta.get('baslik') or str(meta.get('baslik')).startswith(('http://', 'https://'))):
            body = re.sub(r'^# .*$', lambda _m: '# ' + _first_line(info['title'], 160), body, count=1, flags=re.M)
            meta['baslik'] = entry['baslik'] = _first_line(info['title'], 160)
        meta.update({target: info[source] for source, target in (('author', 'yazar'), ('published', 'yayin'), ('site', 'site'))
                     if info.get(source)})
        if text:
            raw = inbox(vault) / RAW / (card['id'] + '.md')
            raw.parent.mkdir(parents=True, exist_ok=True)
            header = ['---', 'tur: yakala-ham', 'kart: ' + json.dumps(entry['kart'], ensure_ascii=False),
                      'baslik: ' + json.dumps(meta.get('baslik') or '', ensure_ascii=False),
                      'url: ' + json.dumps(meta.get('url') or '', ensure_ascii=False),
                      'yontem: ' + json.dumps(method), 'cikarildi: ' + json.dumps(now().isoformat(timespec='seconds')),
                      'visibility: private', '---', '']
            raw.write_text('\n'.join(header) + text.strip() + '\n', encoding='utf-8', newline='\n')
            if method == 'web-clipper':
                body = _without_section(body, 'Icerik' if _section(body, 'Icerik') else 'İçerik',
                                        'Tam metin ham dosyaya tasindi: `' + raw.relative_to(vault).as_posix() + '`')
            meta.update(durum='cikarildi', yontem=method, ham=raw.relative_to(vault).as_posix())
            meta.pop('hata', None)
            entry.update(durum='cikarildi', yontem=method, ham=meta['ham'], karakter=len(text))
        elif method == 'ajan-okusun':
            meta.update(durum='cikarildi', yontem=method)
            entry.update(durum='cikarildi', yontem=method)
        else:
            meta.update(durum='hata', hata=note or 'icerik alinamadi')
            entry.update(durum='hata')
        if note:
            entry['not'] = note
        write_card(card['path'], meta, body)
        results.append(entry)
    return {'status': 'tamam', 'islenen': len(results), 'kartlar': results,
            'araclar': {name: bool(path) for name, path in found.items()}}


def finish(vault, card_id, sources=(), summary=None):
    vault = Path(vault)
    card = find_card(vault, card_id)
    meta, body = dict(card['meta']), card['body'].rstrip()
    links = []
    for source in sources:
        source = str(source).replace('\\', '/')
        if not (vault / source).is_file():
            raise ValueError('Bilgi notu bulunamadi: ' + source)
        links.append('- [[' + source.removesuffix('.md') + ']]')
    meta.update(durum='islendi', islendi=now().isoformat(timespec='seconds'))
    addition = '\n\n## Islendi\n\n' + ((summary.strip() + '\n\n') if summary else '') + '\n'.join(links or ['_(bilgi notu yok)_'])
    write_card(card['path'], meta, body + addition)
    return {'status': 'islendi', 'id': card['id'], 'bilgi': sources}


def listing(vault, status=None):
    rows = []
    for card in cards(vault):
        meta = card['meta']
        if status and meta.get('durum') != status:
            continue
        rows.append({'id': card['id'], 'durum': meta.get('durum'), 'kaynak_turu': meta.get('kaynak_turu'),
                     'baslik': meta.get('baslik'), 'url': meta.get('url'), 'ham': meta.get('ham')})
    return {'status': 'tamam', 'kartlar': rows, 'bekleyen': sum(1 for r in rows if r['durum'] == 'bekliyor')}


def session_notice(vault):
    count = pending(vault)
    if not count:
        return ''
    return ('Yakalanan ' + str(count) + ' kaynak bekliyor (' + INBOX + '). Kullanici isterse beyin-yakala skill\'iyle isle; '
            'kendiliginden baslama.\n')


# ---------------------------------------------------------------- context (which app, which page)

def _objc():
    import ctypes
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(target, selector, restype=ctypes.c_void_p, *args):
        argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
        function = ctypes.CFUNCTYPE(restype, *argtypes)(('objc_msgSend', objc))
        return function(target, objc.sel_registerName(selector), *args)
    return objc, send


def mac_frontmost():
    import ctypes
    try:
        objc, send = _objc()
        workspace = send(objc.objc_getClass(b'NSWorkspace'), b'sharedWorkspace')
        app = send(workspace, b'frontmostApplication')

        def string(value):
            raw = send(value, b'UTF8String', ctypes.c_char_p) if value else None
            return raw.decode('utf-8') if raw else None
        return {'uygulama': string(send(app, b'localizedName')), 'bundle': string(send(app, b'bundleIdentifier'))}
    except Exception:
        return {}


CHROMIUM = {'com.google.Chrome', 'com.google.Chrome.canary', 'com.brave.Browser', 'com.microsoft.edgemac',
            'company.thebrowser.Browser', 'com.vivaldi.Vivaldi', 'com.operasoftware.Opera'}


def _osascript(script, timeout=2.5):
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return result.stdout.decode('utf-8', 'replace').strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def mac_context(front=None):
    front = front or mac_frontmost()
    bundle = front.get('bundle') or ''
    context = dict(front)
    if bundle in CHROMIUM:
        out = _osascript('tell application id "' + bundle + '" to return (URL of active tab of front window) & linefeed & (title of active tab of front window)')
    elif bundle in ('com.apple.Safari', 'com.apple.SafariTechnologyPreview'):
        out = _osascript('tell application id "' + bundle + '" to return (URL of front document) & linefeed & (name of front document)')
    else:
        out = None
    if out:
        url, _, title = out.partition('\n')
        if url.startswith(('http://', 'https://')):
            context.update(url=url.strip(), baslik=title.strip())
    if bundle == 'com.apple.finder':
        out = _osascript('tell application "Finder"\nset out to ""\nrepeat with f in (selection as alias list)\n'
                         'set out to out & POSIX path of f & linefeed\nend repeat\nreturn out\nend tell')
        paths = [line for line in (out or '').splitlines() if line and Path(line).is_file()]
        if paths:
            context['dosyalar'] = paths[:20]
    return context


def windows_context():
    import ctypes
    from ctypes import wintypes
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        window = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        name = None
        if process:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                name = Path(path.value).stem
            kernel32.CloseHandle(process)
        return {'uygulama': name, 'pencere': buffer.value}
    except Exception:
        return {}


def gather_context():
    if sys.platform == 'darwin':
        return mac_context()
    if os.name == 'nt':
        return windows_context()
    return {}


# ---------------------------------------------------------------- popup

PALETTE = {'bg': '#0D1424', 'panel': '#152038', 'line': '#22304F', 'text': '#E9EEF8', 'muted': '#8C9AB5',
           'accent': '#F2C84B', 'accent_text': '#1A1405', 'ok': '#7AD9A4'}


def _font(size, weight='normal'):
    family = 'Segoe UI' if os.name == 'nt' else ('Helvetica Neue' if sys.platform == 'darwin' else 'DejaVu Sans')
    return (family, size, weight)


def _activate_mac():
    """Accessory app (no Dock icon) whose window floats over every Space, full-screen apps included."""
    try:
        import ctypes
        objc, send = _objc()
        app = send(objc.objc_getClass(b'NSApplication'), b'sharedApplication')
        send(app, b'setActivationPolicy:', ctypes.c_void_p, ctypes.c_long(1))
        windows = send(app, b'windows')
        for index in range(send(windows, b'count', ctypes.c_ulong)):
            window = send(windows, b'objectAtIndex:', ctypes.c_void_p, ctypes.c_ulong(index))
            # canJoinAllSpaces | fullScreenAuxiliary; NSStatusWindowLevel
            send(window, b'setCollectionBehavior:', ctypes.c_void_p, ctypes.c_ulong(1 | 256))
            send(window, b'setLevel:', ctypes.c_void_p, ctypes.c_long(25))
        send(app, b'activateIgnoringOtherApps:', ctypes.c_void_p, ctypes.c_bool(True))
    except Exception:
        pass


def repair_mojibake(text):
    """UTF-8 bytes that a tool decoded as Mac Roman or cp1252 ('pahalƒ±' for 'pahalı'); else unchanged."""
    if not text or text.isascii():
        return text
    for codec in ('mac_roman', 'cp1252'):
        try:
            fixed = text.encode(codec).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            return fixed
    return text


def _clipboard(root):
    try:
        value = root.clipboard_get()
    except Exception:
        return ''
    return repair_mojibake(value) if isinstance(value, str) else ''


def popup(vault, context=None):
    """Small dark window: what will be saved, one reason field, Enter saves, Esc closes."""
    context = dict(context or {})
    try:
        import tkinter as tk
    except ImportError:
        return _popup_fallback(vault, context)
    root = tk.Tk()
    root.withdraw()
    clip = _clipboard(root).strip()
    clip_is_url = bool(re.match(r'^https?://\S+$', clip))
    url = context.get('url') or (clip if clip_is_url else None)
    files = context.get('dosyalar') or []
    root.title('Beyne at')
    root.configure(bg=PALETTE['bg'])
    root.resizable(False, False)
    width = 540
    try:
        root.attributes('-topmost', True)
    except tk.TclError:
        pass

    outer = tk.Frame(root, bg=PALETTE['bg'], padx=22, pady=18)
    outer.pack(fill='both', expand=True)
    tk.Label(outer, text='Beyne at', font=_font(17, 'bold'), fg=PALETTE['text'], bg=PALETTE['bg'], anchor='w').pack(fill='x')
    origin = ' · '.join(part for part in (context.get('uygulama'), context.get('pencere')) if part)
    if origin:
        tk.Label(outer, text=origin[:80], font=_font(11), fg=PALETTE['muted'], bg=PALETTE['bg'], anchor='w').pack(fill='x', pady=(2, 0))

    card = tk.Frame(outer, bg=PALETTE['panel'], highlightthickness=1, highlightbackground=PALETTE['line'], padx=14, pady=12)
    card.pack(fill='x', pady=(14, 12))
    if files:
        kind, headline, detail = 'DOSYA', Path(files[0]).name + (' +' + str(len(files) - 1) if len(files) > 1 else ''), ''
    elif url:
        kind = {'youtube': 'VIDEO', 'x': 'TWEET', 'mail': 'MAIL', 'github': 'REPO', 'pdf': 'PDF'}.get(kind_of(url), 'SAYFA')
        headline, detail = (context.get('baslik') or url), url
    elif clip:
        kind, headline, detail = 'METİN', _first_line(clip, 90), ''
    else:
        kind, headline, detail = 'NOT', 'Yalnız not yazabilirsin', ''
    top = tk.Frame(card, bg=PALETTE['panel'])
    top.pack(fill='x')
    tk.Label(top, text=' ' + kind + ' ', font=_font(9, 'bold'), fg=PALETTE['accent_text'], bg=PALETTE['accent']).pack(side='left')
    tk.Label(top, text='  ' + headline[:70], font=_font(13, 'bold'), fg=PALETTE['text'], bg=PALETTE['panel'], anchor='w').pack(side='left', fill='x')
    if detail and detail != headline:
        tk.Label(card, text=detail[:86], font=_font(10), fg=PALETTE['muted'], bg=PALETTE['panel'], anchor='w').pack(fill='x', pady=(6, 0))

    # Clipboard text rides along by default only when there is nothing better to save.
    include_clip = tk.BooleanVar(value=bool(clip) and not clip_is_url and not url and not files)
    if clip and not clip_is_url:
        tk.Checkbutton(outer, text='Panodaki metni de ekle  (' + _first_line(clip, 46) + ')', variable=include_clip,
                       font=_font(11), fg=PALETTE['muted'], bg=PALETTE['bg'], activebackground=PALETTE['bg'],
                       activeforeground=PALETTE['text'], selectcolor=PALETTE['panel'], anchor='w',
                       highlightthickness=0, bd=0).pack(fill='x', pady=(0, 8))

    tk.Label(outer, text='Neden kaydediyorsun? (boş bırakabilirsin)', font=_font(11), fg=PALETTE['muted'], bg=PALETTE['bg'], anchor='w').pack(fill='x')
    entry = tk.Entry(outer, font=_font(14), fg=PALETTE['text'], bg=PALETTE['panel'], insertbackground=PALETTE['accent'],
                     relief='flat', highlightthickness=1, highlightbackground=PALETTE['line'], highlightcolor=PALETTE['accent'])
    entry.pack(fill='x', ipady=8, pady=(6, 14))

    footer = tk.Frame(outer, bg=PALETTE['bg'])
    footer.pack(fill='x')
    status = tk.Label(footer, text='Enter kaydeder · Esc kapatır', font=_font(10), fg=PALETTE['muted'], bg=PALETTE['bg'])
    status.pack(side='left')
    result = {}

    def save(_event=None):
        text = clip if clip and not clip_is_url and include_clip.get() else None
        why = entry.get().strip()
        if not (url or files or text or why):
            status.configure(text='Kaydedilecek bir şey yok', fg=PALETTE['accent'])
            return
        if not (url or files or text):
            text, why = why, ''  # only a typed line: the line itself is the note
        try:
            result.update(capture(vault, url=url, text=text, files=files, why=why, app=context.get('uygulama'),
                                  title=context.get('baslik') if url else None))
        except Exception as exc:
            status.configure(text='Kaydedilemedi: ' + str(exc)[:60], fg=PALETTE['accent'])
            return
        status.configure(text='Beyne atıldı  ✓', fg=PALETTE['ok'])
        root.after(650, root.destroy)

    button = tk.Label(footer, text='  Kaydet  ', font=_font(12, 'bold'), fg=PALETTE['accent_text'], bg=PALETTE['accent'], cursor='hand2', padx=6, pady=5)
    button.pack(side='right')
    button.bind('<Button-1>', save)
    root.bind('<Return>', save)
    root.bind('<KP_Enter>', save)
    root.bind('<Escape>', lambda _e: root.destroy())

    root.update_idletasks()
    height = root.winfo_reqheight()
    x = (root.winfo_screenwidth() - width) // 2
    y = max(60, root.winfo_screenheight() // 4 - height // 2)
    root.geometry('%dx%d+%d+%d' % (width, height, x, y))
    root.deiconify()
    if sys.platform == 'darwin':
        _activate_mac()
    root.lift()
    root.focus_force()
    entry.focus_set()
    snapshot = os.environ.get('BEYIN_YAKALA_SNAPSHOT')
    if snapshot and sys.platform == 'darwin':
        # Development aid: capture only this window's rectangle, then close without saving.
        def shoot():
            region = '%d,%d,%d,%d' % (root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height())
            subprocess.run(['screencapture', '-x', '-R', region, snapshot], capture_output=True)
            root.destroy()
        root.after(900, shoot)
    root.mainloop()
    return result or {'status': 'vazgecildi'}


def _popup_fallback(vault, context):
    """No tkinter (some Homebrew/Linux Pythons): the OS dialog asks only for the reason."""
    why = ''
    if sys.platform == 'darwin':
        why = _osascript('text returned of (display dialog "Neden kaydediyorsun?" default answer "" with title "Beyne at")', timeout=300) or ''
    url = context.get('url')
    if not (url or context.get('dosyalar') or why):
        return {'status': 'vazgecildi'}
    return capture(vault, url=url, text=None if url or context.get('dosyalar') else why, files=context.get('dosyalar') or [],
                   why=why if url or context.get('dosyalar') else '', app=context.get('uygulama'), title=context.get('baslik'))


# ---------------------------------------------------------------- hotkey listener (macOS)

def listen_mac(vault, script):
    """Carbon RegisterEventHotKey: global, no Accessibility permission, standard library only."""
    import ctypes
    from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int32, c_uint32, c_void_p
    carbon = ctypes.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')

    class HotKeyID(Structure):
        _fields_ = [('signature', c_uint32), ('id', c_uint32)]

    class EventTypeSpec(Structure):
        _fields_ = [('eventClass', c_uint32), ('eventKind', c_uint32)]

    handler_type = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
    carbon.GetApplicationEventTarget.restype = c_void_p
    carbon.InstallEventHandler.argtypes = [c_void_p, handler_type, c_uint32, POINTER(EventTypeSpec), c_void_p, POINTER(c_void_p)]
    carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, HotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]

    def fourcc(text):
        return int.from_bytes(text.encode('ascii'), 'big')

    def pressed(_call, _event, _data):
        try:
            context = mac_context()
            # Prefer the vault copy so an update reaches the window without reinstalling the listener.
            current = Path(vault) / '.claude/scripts' / Path(script).name
            subprocess.Popen([sys.executable, str(current if current.is_file() else script), 'pencere', '--vault', str(vault), '--baglam', json.dumps(context)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass
        return 0

    callback = handler_type(pressed)
    target = carbon.GetApplicationEventTarget()
    spec = EventTypeSpec(fourcc('keyb'), 5)  # kEventClassKeyboard / kEventHotKeyPressed
    handler_ref, hotkey_ref = c_void_p(), c_void_p()
    if carbon.InstallEventHandler(target, callback, 1, byref(spec), None, byref(handler_ref)):
        raise SystemExit('Kisayol dinleyicisi kurulamadi')
    # keycode 11 = B; controlKey 0x1000 | optionKey 0x800
    if carbon.RegisterEventHotKey(11, 0x1000 | 0x800, HotKeyID(fourcc('BYKL'), 1), target, 0, byref(hotkey_ref)):
        raise SystemExit(MAC_HOTKEY + ' baska bir uygulamada kayitli')
    carbon.RunApplicationEventLoop()


# ---------------------------------------------------------------- install / remove

TEMPLATE_NAME = 'Beyne at'
SKILL_MARK = '<!-- beyin-yakala: `beyin.py yakala kur` yazdi; kaldir komutu siler -->'
# Written by `kur`, not shipped in the package: released updaters accept only the starter skill paths.
SKILL_MD = '''---
name: beyin-yakala
description: Kullanıcının yakaladığı kaynakları (YouTube videosu, tweet, makale, mail, PDF, dosya, not) ikinci beyne işler; metni çıkarır, dersleri knowledge/ notlarına bağlar. "yakalananları işle", "beyne attıklarımı işle", "inbox'taki kaynaklar", "şu videoyu beyne at" denince ya da oturum başında "Yakalanan N kaynak bekliyor" görüp kullanıcı isteyince kullan.
---
''' + SKILL_MARK + '''

# Beyne at: yakalanan kaynakları işle

Kullanıcı kaynakları tek tuşla yakalar: kısayol (Mac'te Control+Option+B, Windows'ta Ctrl+Alt+B),
tarayıcıda Obsidian Web Clipper'ın "Beyne at" şablonu ya da Windows'ta sağ tık > Gönder > Beyne At.
Her yakalama `📥 000-Inbox/Yakala/` içinde tek bir karttır. Kartın `## Neden` bölümü kullanıcının
niyetidir; dersleri o niyete göre seç.

Windows'ta komutlardaki `python3` yerine `py -3` yaz.

## Akış

1. **Metni çıkar.** `python3 beyin.py yakala isle --json`. Ağ kullanır: önce zaten yakalanmış
   içerik, sonra Defuddle (`npx`), YouTube'da altyazı (`yt-dlp`), altyazı yoksa yalnız ses indirilip
   yerel Whisper ile yazıya dökülür ve ses silinir. Kullanıcı ses indirmeyi istemezse `--ses-yok`.
2. **Her `cikarildi` kartı oku.** Kartı ve `ham` dosyasını (`.ham/<id>.md`) oku. Uzun transkriptte
   önce bölüm başlıklarına bak, sonra nedenle ilgili kısmı oku. `ajan-okusun` yönteminde dosyayı
   kendin aç. Mail kartında Gmail/Outlook bağlantın varsa thread'in tamamını oradan oku.
3. **Dersleri bağla.** Kaynak başına 1-5 somut ders çıkar. Önce `knowledge/index.md` ve ilgili
   kavramı oku; mevcut notu geliştir, yoksa `beyin` skill'indeki kuralla `knowledge/concepts/<konu>.md`
   oluştur. Her dersin altında kaynak satırı: kart bağlantısı, URL ve videoda zaman damgası
   (`[12:40]`). Ham metni bilgi notuna kopyalama; kısa alıntı en fazla bir cümle.
4. **Kapat.** `python3 beyin.py yakala bitti <id> --bilgi knowledge/concepts/<konu>.md --ozet "<tek cümle>"`.
   Kalıcı ders yoksa `--ozet "kalıcı ders yok"` yeter; zorla not üretme.
5. **Kullanıcıya söyle.** Kaynak başına bir satır: ne öğrendik, hangi nota gitti.

## Kurallar

- Kullanıcı istemeden kuyruğu işlemeye başlama; oturum bildirimi yalnız bilgi.
- `hata` kartında karttaki `hata` notunu oku ve kullanıcıya ne gerektiğini söyle (sayfa giriş
  istiyorsa Web Clipper ile sayfa açıkken yakalamak, video için `yt-dlp`). Yeniden denemek için
  `python3 beyin.py yakala isle --tekrar`.
- `visibility: private` kartlardan (mail, pano metni, dosya) çıkan derslerde kişi adı, adres,
  numara gibi kişisel veriyi bilgi notuna taşıma.
- Kartları ve `.ham/` dosyalarını silme; kullanıcının arşividir.
- Kurulum ya da kısayol sorunu: `python3 beyin.py yakala durum`. Kurulum `python3 beyin.py yakala kur`,
  kaldırma `python3 beyin.py yakala kaldir` (notlara dokunmaz).
'''


def clipper_template():
    return {
        'schemaVersion': '0.1.0',
        'name': TEMPLATE_NAME,
        'behavior': 'create',
        'noteNameFormat': '{{date|date:"YYYY-MM-DD-HHmm"}}-{{title|safe_name|lower|slice:0,48}}',
        'path': INBOX,
        'noteContentFormat': '# {{title}}\n\n## Neden\n\n\n\n## Kaynak\n\n{{url}}\n\n## Secim\n\n{{selection}}\n\n## Icerik\n\n{{content}}\n',
        'properties': [
            {'name': 'tur', 'value': 'yakala', 'type': 'text'},
            {'name': 'durum', 'value': 'bekliyor', 'type': 'text'},
            {'name': 'kaynak_turu', 'value': '', 'type': 'text'},
            {'name': 'baslik', 'value': '{{title}}', 'type': 'text'},
            {'name': 'url', 'value': '{{url}}', 'type': 'text'},
            {'name': 'yakalandi', 'value': '{{date|date:"YYYY-MM-DDTHH:mm:ssZ"}}', 'type': 'text'},
            {'name': 'arac', 'value': 'web-clipper', 'type': 'text'},
            {'name': 'visibility', 'value': 'internal', 'type': 'text'},
        ],
        'triggers': [],
    }


def _links_skills(skills):
    try:
        return any(entry.is_symlink() for entry in Path(skills).iterdir())
    except OSError:
        return False


def _state_path(state):
    return Path(state) / STATE_FILE


def _launch_agent():
    return Path.home() / 'Library/LaunchAgents' / (LAUNCH_LABEL + '.plist')


def _windows_dirs():
    appdata = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming')))
    return appdata / 'Microsoft/Windows/Start Menu/Programs', appdata / 'Microsoft/Windows/SendTo'


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _windows_shortcut(path, target, arguments, hotkey=None):
    import base64
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lines = ['$s = (New-Object -ComObject WScript.Shell).CreateShortcut(' + _ps_quote(path) + ')',
             '$s.TargetPath = ' + _ps_quote(target), '$s.Arguments = ' + _ps_quote(arguments),
             '$s.Description = ' + _ps_quote('Beyne at: ikinci beyne kaynak yakala'), '$s.WindowStyle = 7']
    if hotkey:
        lines.append('$s.Hotkey = ' + _ps_quote(hotkey))
    lines.append('$s.Save()')
    encoded = base64.b64encode('\n'.join(lines).encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                   check=True, capture_output=True, timeout=60, creationflags=NO_WINDOW)


def _argline(values):
    return subprocess.list2cmdline([str(v) for v in values])


def install(vault, state, hotkey=True):
    vault, state = Path(vault).resolve(), Path(state).resolve()
    folder = inbox(vault)
    folder.mkdir(parents=True, exist_ok=True)
    template = folder / (TEMPLATE_NAME.replace(' ', '-').lower() + '-web-clipper.json')
    template.write_text(json.dumps(clipper_template(), ensure_ascii=False, indent='\t') + '\n', encoding='utf-8')
    script = Path(__file__).resolve()
    done = {'status': 'kuruldu', 'klasor': INBOX, 'web_clipper_sablonu': template.relative_to(vault).as_posix(),
            'kisayol': None, 'gonder_menusu': False, 'skill': []}
    # A vault without the beyin.py entry (standalone use) gets the direct module command.
    if (vault / 'beyin.py').is_file():
        prefix = 'python3 beyin.py yakala'
    elif vault in script.parents:
        prefix = 'python3 ' + script.relative_to(vault).as_posix() + ' --vault .'
    else:
        prefix = 'python3 "' + str(script) + '" --vault .'
    for root in ('.agents', '.claude'):
        skill = vault / root / 'skills/beyin-yakala/SKILL.md'
        if skill.exists() and SKILL_MARK not in skill.read_text(encoding='utf-8', errors='ignore'):
            continue  # the user's own skill with this name stays untouched
        folder = skill.parent
        if root == '.claude' and not os.path.lexists(folder) and _links_skills(folder.parent):
            try:  # vaults that keep one source under .agents and link it from .claude
                folder.symlink_to(Path('../../.agents/skills/beyin-yakala'), target_is_directory=True)
                done['skill'].append(folder.relative_to(vault).as_posix() + ' -> .agents')
                continue
            except OSError:
                pass
        folder.mkdir(parents=True, exist_ok=True)
        skill.write_text(SKILL_MD.replace('python3 beyin.py yakala', prefix), encoding='utf-8', newline='\n')
        done['skill'].append(skill.relative_to(vault).as_posix())
    if hotkey and sys.platform == 'darwin':
        # The listener runs from a copy outside the vault: a synced folder may evict the original.
        runner = state / 'yakala' / script.name
        runner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, runner)
        import plistlib
        plist = {'Label': LAUNCH_LABEL, 'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False},
                 'ProgramArguments': [sys.executable, str(runner), 'dinle', '--vault', str(vault)],
                 'ProcessType': 'Interactive', 'StandardErrorPath': str(state / 'yakala' / 'dinleyici.log'),
                 'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'),
                                          'LANG': 'en_US.UTF-8'}}
        agent = _launch_agent()
        agent.parent.mkdir(parents=True, exist_ok=True)
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/' + LAUNCH_LABEL], capture_output=True)
        agent.write_bytes(plistlib.dumps(plist))
        subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True, capture_output=True)
        done['kisayol'] = MAC_HOTKEY
    elif hotkey and os.name == 'nt':
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        target = pythonw if pythonw.is_file() else Path(sys.executable)
        programs, sendto = _windows_dirs()
        _windows_shortcut(programs / 'Beyne At.lnk', target, _argline([script, 'pencere', '--vault', vault]), WIN_HOTKEY)
        _windows_shortcut(sendto / 'Beyne At.lnk', target, _argline([script, 'ekle', '--vault', vault, '--arac', 'gonder-menusu']))
        done.update(kisayol='Ctrl+Alt+B', gonder_menusu=True)
    state.mkdir(parents=True, exist_ok=True)
    _state_path(state).write_text(json.dumps({'schema': 1, 'session_notice': True, 'kisayol': done['kisayol']}) + '\n', encoding='utf-8')
    done['araclar'] = {name: bool(path) for name, path in tools().items()}
    return done


def uninstall(vault, state):
    state = Path(state).resolve()
    removed = []
    if sys.platform == 'darwin':
        agent = _launch_agent()
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LAUNCH_LABEL], capture_output=True)
        if agent.exists():
            agent.unlink()
            removed.append('LaunchAgent')
    elif os.name == 'nt':
        for folder in _windows_dirs():
            link = folder / 'Beyne At.lnk'
            if link.exists():
                link.unlink()
                removed.append(str(link.name))
    link = Path(vault) / '.claude/skills/beyin-yakala'
    if link.is_symlink() and os.readlink(link).replace('\\', '/').endswith('.agents/skills/beyin-yakala'):
        link.unlink()
        removed.append('.claude/skills/beyin-yakala')
    for root in ('.agents', '.claude'):
        skill = Path(vault) / root / 'skills/beyin-yakala/SKILL.md'
        if skill.is_file() and SKILL_MARK in skill.read_text(encoding='utf-8', errors='ignore'):
            skill.unlink()
            try:
                skill.parent.rmdir()
            except OSError:
                pass
            removed.append(root + '/skills/beyin-yakala')
    runner = state / 'yakala' / Path(__file__).name
    if runner.exists():
        runner.unlink()
    if _state_path(state).exists():
        _state_path(state).unlink()
        removed.append(STATE_FILE)
    return {'status': 'kaldirildi', 'kaldirilan': removed, 'notlar_korundu': True}


def status(vault, state):
    installed = _state_path(state).is_file()
    running = None
    if sys.platform == 'darwin' and installed:
        probe = subprocess.run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + LAUNCH_LABEL], capture_output=True)
        running = probe.returncode == 0
    return {'status': 'tamam', 'kurulu': installed, 'dinleyici_calisiyor': running,
            'bekleyen': pending(vault), 'klasor': INBOX,
            'araclar': {name: bool(path) for name, path in tools().items()}}


# ---------------------------------------------------------------- CLI

def default_state(vault):
    import hashlib
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local')))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    return base / 'beyin-v3' / hashlib.sha256(str(vault).encode()).hexdigest()[:16]


def resolve_state(vault):
    config = Path(vault) / '.beyin-runtime.json'
    try:
        return Path(json.loads(config.read_text(encoding='utf-8'))['state'])
    except (OSError, ValueError, KeyError):
        return default_state(Path(vault).resolve())


def human(result, command):
    if result.get('status') == 'yakalandi':
        return 'Beyne atildi: ' + result['path']
    if result.get('status') == 'mevcut_karta_eklendi':
        return 'Bu kaynak zaten vardi; yeni notun karta eklendi: ' + result['path']
    if command == 'isle':
        lines = []
        for entry in result['kartlar']:
            mark = {'cikarildi': 'tamam', 'hata': 'HATA'}.get(entry['durum'], entry['durum'])
            lines.append('[' + mark + '] ' + str(entry.get('baslik') or entry['id'])[:70] +
                         ((' (' + entry['yontem'] + ')') if entry.get('yontem') else '') +
                         (('\n    ' + entry['not']) if entry.get('not') else ''))
        return '\n'.join(lines) or 'Bekleyen kaynak yok.'
    if command == 'liste':
        rows = result['kartlar']
        return '\n'.join('%-10s %-8s %s' % (r['durum'], r['kaynak_turu'], str(r['baslik'])[:70]) for r in rows) or 'Yakalanan kaynak yok.'
    if command == 'kur':
        lines = ['Yakala kuruldu.']
        if result.get('kisayol'):
            lines.append('Kisayol: ' + result['kisayol'] + ' (her uygulamada calisir).')
        if result.get('gonder_menusu'):
            lines.append('Dosyalar icin: sag tik > Gonder > Beyne At.')
        lines.append('Tarayici icin Obsidian Web Clipper eklentisini kur, Ayarlar > Sablonlar > Ice aktar ile su dosyayi sec:')
        lines.append('  ' + result['web_clipper_sablonu'])
        missing = [name for name, ok in result.get('araclar', {}).items() if not ok]
        if missing:
            lines.append('Istege bagli eksik araclar: ' + ', '.join(missing) + ' (video altyazisi ve ses icin; yoksa elde olanla devam edilir).')
        return '\n'.join(lines)
    if command == 'kaldir':
        return 'Yakala kaldirildi. Yakalanan notlar yerinde duruyor.'
    if command == 'durum':
        return ('Kurulu: ' + ('evet' if result['kurulu'] else 'hayir') +
                ('' if result['dinleyici_calisiyor'] is None else '\nKisayol dinleyicisi: ' + ('calisiyor' if result['dinleyici_calisiyor'] else 'durmus')) +
                '\nBekleyen kaynak: ' + str(result['bekleyen']) +
                '\nAraclar: ' + ', '.join(n + ('=var' if ok else '=yok') for n, ok in result['araclar'].items()))
    if result.get('status') == 'islendi':
        return 'Kart islendi olarak isaretlendi: ' + result['id']
    return json.dumps(result, ensure_ascii=False, indent=2)


def main(argv=None, vault=None, state=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--vault', type=Path, dest='vault_sub', default=None, help=argparse.SUPPRESS)
    common.add_argument('--json', action='store_true', dest='force_json', help=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog='beyin.py yakala', description='Kaynak yakala ve ikinci beyne isle', parents=[common])
    sub = parser.add_subparsers(dest='command')

    def command_parser(name, **options):
        return sub.add_parser(name, parents=[common], **options)
    add = command_parser('ekle', help='URL, metin ya da dosya yakala (ag yok)')
    add.add_argument('items', nargs='*', help='URL, dosya yolu ya da metin')
    add.add_argument('--neden', default='')
    add.add_argument('--metin')
    add.add_argument('--baslik')
    add.add_argument('--arac', default='komut')
    window = command_parser('pencere', help='Yakalama penceresini ac')
    window.add_argument('--baglam', help=argparse.SUPPRESS)
    command_parser('dinle', help=argparse.SUPPRESS)
    run = command_parser('isle', help='Bekleyen kaynaklarin metnini cikar (ag kullanir)')
    run.add_argument('ids', nargs='*')
    run.add_argument('--ses-yok', action='store_true', help='Altyazi yoksa sesi indirme')
    run.add_argument('--tekrar', action='store_true', help='Hata alan kartlari da yeniden dene')
    done = command_parser('bitti', help='Karti islendi olarak isaretle')
    done.add_argument('id')
    done.add_argument('--bilgi', action='append', default=[], help='Olusan ya da guncellenen bilgi notu (vault-goreli)')
    done.add_argument('--ozet')
    show = command_parser('liste', help='Yakalanan kaynaklar')
    show.add_argument('--durum', choices=STATUSES)
    setup = command_parser('kur', help='Kisayolu, Web Clipper sablonunu, skill\'i ve oturum bildirimini kur')
    setup.add_argument('--kisayol-yok', action='store_true', help='Yalniz sablon, skill ve bildirim; kisayol kurulmaz')
    command_parser('kaldir', help='Kisayolu ve bildirimi kaldir; notlara dokunmaz')
    command_parser('durum', help='Kurulum ve arac durumu')
    command_parser('sablon', help='Web Clipper sablonunu yazdir')
    args = parser.parse_args(argv)
    vault = vault or args.vault_sub
    if vault is None:
        parser.error('--vault gerekli')
    vault = Path(vault).expanduser().resolve()
    state = Path(state) if state else resolve_state(vault)
    command = args.command or 'pencere'
    as_json = args.force_json or not sys.stdout or not sys.stdout.isatty()
    if command == 'pencere':
        context = json.loads(args.baglam) if getattr(args, 'baglam', None) else gather_context()
        result = popup(vault, context)
    elif command == 'dinle':
        listen_mac(vault, Path(__file__).resolve())
        return 0
    elif command == 'ekle':
        url, files, texts = None, [], []
        for item in args.items:
            if re.match(r'^https?://', item) and not url:
                url = item
            elif Path(item).expanduser().is_file():
                files.append(item)
            else:
                texts.append(item)
        text = args.metin or ('\n'.join(texts) if texts else None)
        result = capture(vault, url=url, text=text, files=files, why=args.neden, title=args.baslik, tool=args.arac)
    elif command == 'isle':
        result = process(vault, args.ids or None, allow_audio=not args.ses_yok, retry=args.tekrar)
    elif command == 'bitti':
        result = finish(vault, args.id, args.bilgi, args.ozet)
    elif command == 'liste':
        result = listing(vault, args.durum)
    elif command == 'kur':
        result = install(vault, state, hotkey=not args.kisayol_yok)
    elif command == 'kaldir':
        result = uninstall(vault, state)
    elif command == 'durum':
        result = status(vault, state)
    else:
        result = clipper_template()
    if sys.stdout is None:
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2) if as_json or command == 'sablon' else human(result, command))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as exc:
        if sys.stderr:
            print(str(exc), file=sys.stderr)
        raise SystemExit(1)
