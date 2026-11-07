#!/usr/bin/env python3
"""Opt-in local web view for one Beyin vault (#371, #423).

Only `beyin.py web` starts it; install, update and hooks never do. It listens on 127.0.0.1
alone and answers only when the Host names this listener (DNS rebinding) and any Origin or
Sec-Fetch-Site is this listener. A launch token opens one browser session: the session
cookie is HttpOnly and SameSite=Strict, and every write also carries that session's CSRF
token. No CORS header is ever sent.

Reads come from the core index through MemoryStore's own gates (visibility, trust, sensitive
folders, index exclusions, source freshness, supersession), so a private or excluded note is
never served. Writes are the CLI's own commands run in-process (task-create, task-update,
companion-compact, sync) under the CLI's locks; this module never writes a vault file itself.
The page is served from this package and fetches nothing from the network.
"""
import argparse
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import webbrowser

import beyin_v3_web_ui as ui

HOST = '127.0.0.1'
PORTS = tuple(range(7373, 7394))
IDLE_SECONDS = 30 * 60
MAX_BODY = 64 * 1024
MAX_SESSIONS = 16
MAX_TASKS = 500
MAX_RECORD_CHARS = 20000
SEARCH_LIMIT = 20
RECEIPT_DAYS = 14
RECEIPT_LIMIT = 30
RUNNING = 'running.json'

SECURITY_HEADERS = (
    ('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'no-referrer'),
    ('Cross-Origin-Resource-Policy', 'same-origin'),
    ('Cross-Origin-Opener-Policy', 'same-origin'),
    ('Cache-Control', 'no-store'),
)
STATIC = {
    '/app.css': ('text/css; charset=utf-8', ui.APP_CSS),
    '/app.js': ('text/javascript; charset=utf-8', ui.APP_JS),
}


class WebError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def load_cli():
    """The CLI this runtime ships: beyin_v3_cli.py when installed, scripts/beyin_v3.py in a checkout."""
    here = Path(__file__).resolve().parent
    for path in (here / 'beyin_v3_cli.py', here.parents[2] / 'scripts' / 'beyin_v3.py'):
        if path.is_file():
            spec = importlib.util.spec_from_file_location('beyin_web_cli', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('Beyin CLI bulunamadı')


def _line(value, limit):
    """One trimmed line of user text; NFC so a decomposed Turkish letter stays one character."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise WebError(400, 'Metin alanı bekleniyordu')
    return unicodedata.normalize('NFC', ' '.join(value.split()))[:limit]


def _slug(text):
    folded = unicodedata.normalize('NFKD', text.replace('ı', 'i').replace('İ', 'I'))
    folded = ''.join(char for char in folded if not unicodedata.combining(char)).lower()
    return re.sub(r'[^a-z0-9]+', '-', folded).strip('-')[:48].strip('-') or 'gorev'


def _title(record):
    title = record.get('title')
    if isinstance(title, str) and title.strip():
        return title.strip()[:200]
    for line in str(record.get('text') or '').splitlines():
        line = line.strip().lstrip('#').strip()
        if line:
            return line[:200]
    return Path(str(record.get('source') or '')).stem[:200]


def _snippet(text, query, width=240):
    text = ' '.join(str(text or '').split())
    words = [word for word in re.findall(r'\w+', query) if len(word) >= 2]
    match = None
    for word in sorted(words, key=len, reverse=True):
        # re.IGNORECASE folds ı/i/I/İ without changing the text's length (str.lower() would).
        match = re.search(re.escape(word), text, re.IGNORECASE)
        if match:
            break
    start = max(0, match.start() - width // 3) if match else 0
    piece = text[start:start + width]
    return ('…' if start else '') + piece + ('…' if start + width < len(text) else '')


def _text(value):
    return value if isinstance(value, str) else None


def task_view(record):
    priority = record.get('priority')
    return {
        'id': record['id'], 'baslik': _title(record), 'durum': _text(record.get('status')),
        'proje': _text(record.get('project')), 'sorumlu': _text(record.get('owner')),
        'oncelik': None if priority is None or isinstance(priority, (dict, list)) else str(priority)[:40],
        'bitis': _text(record.get('due_at')), 'revizyon': record.get('revision'),
        'kaynak': record.get('source'), 'guncelleme': _text(record.get('updated_at')),
        'kati': record.get('completion_contract') == 'strict',
        'ozet': ' '.join(str(record.get('text') or '').split())[:280],
    }


class Core:
    """The vault's own CLI and engine. Every read and write in this module goes through them."""

    def __init__(self, vault, state, cli=None):
        self.vault = Path(vault).resolve()
        self.state = Path(state).resolve()
        self.cli = cli or load_cli()
        self.cli._ensure_scripts_path()
        self.engine = self.cli.load_engine()
        import beyin_v3_sync
        self.statuses = tuple(beyin_v3_sync.TASK_STATUSES)

    def version(self):
        try:
            return (self.vault / '.beyin-version').read_text(encoding='utf-8').strip()[:40] or None
        except OSError:
            return None

    def run(self, *arguments):
        """One CLI command in this process, exactly as `beyin.py <command>` would run it."""
        result, code = self.cli.main(['--vault', str(self.vault), '--state', str(self.state), *arguments],
                                     return_result=True)
        if code:
            error = result if isinstance(result, dict) else {}
            name, message = str(error.get('error') or ''), str(error.get('message') or 'işlem tamamlanamadı')
            status = 409 if 'Conflict' in name else 400 if name in ('ValueError', 'KeyError', 'SystemExit') else 500
            raise WebError(status, message[:400])
        return result

    def run_json(self, command, payload):
        """A JSON-input CLI command; the request file stays in the private state folder."""
        directory = self.state / 'web'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, name = tempfile.mkstemp(prefix='istek-', suffix='.json', dir=directory)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
            return self.run(command, '--file', name)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    def store(self):
        try:
            return self.engine.MemoryStore(self.state, self.vault, read_only=True)
        except ValueError:
            raise WebError(503, 'Dizin henüz hazır değil; önce Eşitle düğmesini kullan ya da beyin.py sync çalıştır.')

    @staticmethod
    def _row(store, ident):
        with store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def visible_task(self, ident):
        """A task only when retrieval itself would deliver it: never a private or stale one."""
        store = self.store()
        record = self._row(store, ident) if isinstance(ident, str) else None
        if not record or record.get('kind') != 'task':
            return None
        eligible, _ = store._eligible('internal', records=[record])
        return eligible[0] if eligible and record['id'] not in store._superseded_ids() else None

    def tasks(self):
        store = self.store()
        records = store._records()
        superseded = store._superseded_ids(records)
        tasks = [record for record in records if record.get('kind') == 'task' and record['id'] not in superseded]
        eligible, stale = store._eligible('internal', records=tasks)
        order = {status: index for index, status in enumerate(self.statuses)}
        eligible.sort(key=lambda r: (order.get(r.get('status'), len(order)), str(r.get('due_at') or '9999'),
                                     _title(r).casefold()))
        return {'gorevler': [task_view(r) for r in eligible[:MAX_TASKS]], 'toplam': len(eligible),
                'kisaltildi': len(eligible) > MAX_TASKS, 'eski': stale, 'durumlar': list(self.statuses)}

    def search(self, query):
        query = _line(query, 200)
        if len(query) < 2:
            return {'sonuclar': []}
        found = self.store().candidates(query, audience='internal', limit=SEARCH_LIMIT)
        return {'sonuclar': [{'id': r['id'], 'baslik': _title(r), 'kaynak': r.get('source'),
                              'tur': _text(r.get('kind')), 'durum': _text(r.get('status')),
                              'ozet': _snippet(r.get('text'), query)} for r in found]}

    def record(self, ident):
        store = self.store()
        record = self._row(store, ident) if isinstance(ident, str) and ident else None
        eligible, _ = store._eligible('internal', records=[record]) if record else ([], 0)
        if not eligible:
            raise WebError(404, 'Kayıt bulunamadı ya da bu görünümde gösterilmiyor')
        text = str(eligible[0].get('text') or '')
        return {'id': record['id'], 'baslik': _title(record), 'kaynak': record.get('source'),
                'tur': _text(record.get('kind')), 'metin': text[:MAX_RECORD_CHARS],
                'kisaltildi': len(text) > MAX_RECORD_CHARS}

    def receipts(self):
        store = self.store()
        from beyin_v3_projections import recent_receipts  # withholds private and missing refs itself
        with store._connect() as db:
            result = recent_receipts(db, days=RECEIPT_DAYS, limit=RECEIPT_LIMIT, vault=self.vault)
        items = [{'zaman': item.get('created_at'), 'ozet': str(item.get('summary') or '')[:2000],
                  'ozet_gizli': bool(item.get('summary_withheld')), 'kaynaklar': list(item.get('refs') or [])[:20],
                  'gizlenen': item.get('refs_withheld') or {}} for item in result.get('items', [])]
        return {'makbuzlar': items, 'toplam': result.get('total', 0), 'gun': RECEIPT_DAYS}

    def doctor(self):
        result = self.run('doctor')
        hygiene = result.get('companion_hygiene') if isinstance(result.get('companion_hygiene'), dict) else {}
        updates = result.get('updates') if isinstance(result.get('updates'), dict) else {}
        completion = result.get('task_completion') if isinstance(result.get('task_completion'), dict) else {}
        return {'durum': _text(result.get('status')), 'bekleyen_olay': result.get('pending_events'),
                'ayiklanan_gizli': result.get('secrets_redacted'), 'guncelleme': _text(updates.get('status')),
                'otomatik_model': bool(result.get('automatic_model_calls')),
                'kati_gorev_sorunu': completion.get('strict_issue_count', 0),
                'hafiza': {'durum': _text(hygiene.get('status')), 'asan': list(hygiene.get('over_limit') or [])}}

    def memory(self):
        result = self.run('companion-compact', '--dry-run')
        files = []
        for name, entry in sorted((result.get('files') or {}).items()):
            if isinstance(entry, dict):
                files.append({'ad': name, 'karakter': entry.get('chars'), 'sinir': entry.get('limit'),
                              'durum': _text(entry.get('status')), 'tasinacak': entry.get('moved_chars')})
        return {'durum': _text(result.get('status')), 'dizin': _text(result.get('directory')), 'dosyalar': files,
                'neden': _text(result.get('reason'))}

    def create_task(self, data):
        title = _line(data.get('baslik'), 160)
        owner = _line(data.get('sorumlu'), 80)
        project = _line(data.get('proje'), 80)
        body = data.get('aciklama')
        if body is not None and not isinstance(body, str):
            raise WebError(400, 'Açıklama metin olmalı')
        body = unicodedata.normalize('NFC', (body or '').strip())[:4000] or title
        status = data.get('durum') or 'inbox'
        if not title or not owner:
            raise WebError(400, 'Başlık ve sorumlu gerekli')
        if status not in self.statuses:
            raise WebError(400, 'Geçersiz durum')
        if body.lstrip().startswith('---'):
            raise WebError(400, 'Açıklama --- ile başlayamaz')
        stamp = time.strftime('%Y%m%d')
        suffix = secrets.token_hex(3)
        metadata = {'id': f'web-{stamp}-{suffix}', 'title': title, 'status': status, 'owner': owner}
        if project:
            metadata['project'] = project
        source = f'tasks/{_slug(title)}-{suffix}.md'
        record = self.run_json('task-create', {'source': source, 'text': body, 'metadata': metadata})
        return {'durum': 'olusturuldu', 'gorev': task_view(record)}

    def set_status(self, data):
        ident, revision, status = data.get('id'), data.get('revizyon'), data.get('durum')
        if type(revision) is not int or status not in self.statuses:
            raise WebError(400, 'Görev, revizyon ve geçerli durum gerekli')
        if not self.visible_task(ident):
            raise WebError(404, 'Görev bulunamadı ya da bu görünümde gösterilmiyor')
        record = self.run_json('task-update', {'id': ident, 'expected_revision': revision, 'changes': {'status': status}})
        return {'durum': 'guncellendi', 'gorev': task_view(record)}

    def sync(self):
        result = self.run('sync')
        return {'durum': _text(result.get('status')), 'dizinlenen': result.get('indexed'),
                'uyari': len(result.get('warnings') or []), 'cakisma': len(result.get('conflicts') or [])}

    def compact(self):
        result = self.run('companion-compact')
        moved = {name: entry.get('status') for name, entry in (result.get('files') or {}).items() if isinstance(entry, dict)}
        return {'durum': _text(result.get('status')), 'dosyalar': moved, 'neden': _text(result.get('reason'))}


class WebServer(HTTPServer):
    # SO_REUSEADDR lets a second Windows listener bind a port that is already in use.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, port, core, read_only=False, token=None):
        self.core = core
        self.read_only = bool(read_only)
        self.token = token or secrets.token_urlsafe(32)
        self.sessions = {}
        self.last_activity = time.monotonic()
        super().__init__((HOST, port), Handler)
        self.port = self.server_address[1]

    def server_bind(self):
        # HTTPServer.server_bind asks getfqdn(), a DNS lookup that can stall startup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def touch(self):
        self.last_activity = time.monotonic()

    def idle_expired(self, now=None):
        return ((time.monotonic() if now is None else now) - self.last_activity) > IDLE_SECONDS

    def new_session(self):
        while len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)))
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[cookie] = csrf
        return cookie

    def stop_soon(self):
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = 'BeyinWeb'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError, socket.timeout):
            pass

    def log_message(self, format, *args):
        pass  # a request line can carry the launch token

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    # --- responses -------------------------------------------------------
    def _send(self, status, body, content_type='application/json; charset=utf-8', headers=()):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _fail(self, status, message):
        self._json(status, {'hata': status, 'mesaj': message})

    # --- request checks --------------------------------------------------
    def _authorities(self):
        return {f'127.0.0.1:{self.server.port}', f'localhost:{self.server.port}'}

    def _guard(self):
        """Host, Origin and Sec-Fetch-Site must all name this listener; checked before anything else."""
        hosts = self.headers.get_all('Host') or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self._authorities():
            self._fail(403, 'Bu sunucu yalnız kendi yerel adresinden kullanılabilir')
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin.strip().lower() not in {'http://' + a for a in self._authorities()}:
            self._fail(403, 'Başka bir siteden gelen istek reddedildi')
            return False
        site = self.headers.get('Sec-Fetch-Site')
        if site is not None and site.strip().lower() not in ('same-origin', 'none'):
            self._fail(403, 'Başka bir siteden gelen istek reddedildi')
            return False
        return True

    def _session(self):
        name = f'beyin_web_{self.server.port}'
        for header in self.headers.get_all('Cookie') or []:
            for part in header.split(';'):
                key, _, value = part.strip().partition('=')
                if key == name and value:
                    for known in list(self.server.sessions):
                        if hmac.compare_digest(known.encode(), value.encode()):
                            return known
        return None

    def _bearer(self):
        value = self.headers.get('Authorization', '')
        return value.startswith('Bearer ') and hmac.compare_digest(value[7:].encode(), self.server.token.encode())

    def _read_body(self):
        """A bounded body, read before any rejection: a Windows socket closed with unread bytes resets."""
        length = self.headers.get('Content-Length')
        if self.headers.get('Transfer-Encoding') or length is None or not length.isdecimal():
            self.close_connection = True
            self._fail(411 if length is None else 400, 'Geçersiz istek gövdesi')
            return None
        if int(length) > MAX_BODY:
            self.close_connection = True
            self._fail(413, 'İstek gövdesi çok büyük')
            return None
        body = self.rfile.read(int(length)) if int(length) else b''
        if len(body) != int(length):
            self.close_connection = True
            self._fail(400, 'Eksik istek gövdesi')
            return None
        return body

    # --- GET -------------------------------------------------------------
    def do_GET(self):
        if not self._guard():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path in STATIC:
            content_type, text = STATIC[url.path]
            self._send(200, text.encode('utf-8'), content_type)
            return
        if url.path == '/':
            self._page(urllib.parse.parse_qs(url.query).get('token', [''])[0])
            return
        if not url.path.startswith('/api/'):
            self._fail(404, 'Bulunamadı')
            return
        if not (self._session() or self._bearer()):
            self._fail(401, 'Oturum yok; beyin.py web ac ile yeniden aç')
            return
        self.server.touch()
        core, query = self.server.core, urllib.parse.parse_qs(url.query)
        routes = {
            '/api/durum': lambda: {'surum': core.version(), 'kasa': core.vault.name, 'salt_okunur': self.server.read_only,
                                   'bosta_kapanma_dk': IDLE_SECONDS // 60},
            '/api/doktor': core.doctor,
            '/api/gorevler': core.tasks,
            '/api/ara': lambda: core.search(query.get('q', [''])[0]),
            '/api/kayit': lambda: core.record(query.get('id', [''])[0]),
            '/api/makbuzlar': core.receipts,
            '/api/hafiza': core.memory,
        }
        self._route(routes.get(url.path))

    def _page(self, token):
        if token:
            if not hmac.compare_digest(token.encode(), self.server.token.encode()):
                self._send(401, ui.unlock_page(True).encode('utf-8'), 'text/html; charset=utf-8')
                return
            cookie = self.server.new_session()
            self.server.touch()
            # The token leaves the address bar at once; the session lives only in the HttpOnly cookie.
            self._send(303, b'', 'text/plain; charset=utf-8', (
                ('Set-Cookie', f'beyin_web_{self.server.port}={cookie}; HttpOnly; SameSite=Strict; Path=/'),
                ('Location', '/')))
            return
        session = self._session()
        if not session:
            self._send(401, ui.unlock_page(False).encode('utf-8'), 'text/html; charset=utf-8')
            return
        self.server.touch()
        page = ui.index_page(self.server.sessions[session], self.server.read_only)
        self._send(200, page.encode('utf-8'), 'text/html; charset=utf-8')

    def _route(self, handler, *arguments):
        if handler is None:
            self._fail(404, 'Bulunamadı')
            return
        try:
            self._json(200, handler(*arguments))
        except WebError as exc:
            self._fail(exc.status, exc.message)
        except Exception as exc:  # the page shows a short reason; nothing else leaves the process
            self._fail(500, (type(exc).__name__ + ': ' + str(exc))[:300])

    # --- POST ------------------------------------------------------------
    def do_POST(self):
        body = self._read_body()
        if body is None or not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/kapat' and self._bearer() and self.headers.get('Origin') is None:
            self._json(200, {'durum': 'kapaniyor'})
            self.server.stop_soon()
            return
        session = self._session()
        if not session:
            self._fail(401, 'Oturum yok; beyin.py web ac ile yeniden aç')
            return
        if self.headers.get('Origin') is None:
            self._fail(403, 'Yazma isteği kaynak (Origin) başlığı taşımalı')
            return
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            self._fail(415, 'Yazma isteği JSON olmalı')
            return
        if not hmac.compare_digest(self.headers.get('X-Beyin-CSRF', '').encode(), self.server.sessions[session].encode()):
            self._fail(403, 'CSRF belirteci geçersiz; sayfayı yenile')
            return
        try:
            data = json.loads(body.decode('utf-8') or '{}')
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            self._fail(400, 'İstek gövdesi bir JSON nesnesi olmalı')
            return
        self.server.touch()
        if path == '/api/etkinlik':
            self._json(200, {'durum': 'tamam'})
            return
        if path == '/api/kapat':
            self._json(200, {'durum': 'kapaniyor'})
            self.server.stop_soon()
            return
        core = self.server.core
        routes = {'/api/gorev': core.create_task, '/api/gorev/durum': core.set_status,
                  '/api/esitle': lambda _: core.sync(), '/api/hafiza/sikistir': lambda _: core.compact()}
        if path in routes and self.server.read_only:
            self._fail(403, 'Sunucu salt okunur modda; yazma kapalı')
            return
        self._route(routes.get(path), data)


# --- lifecycle --------------------------------------------------------------
def _running_path(state):
    return Path(state) / 'web' / RUNNING


def read_running(state):
    try:
        data = json.loads(_running_path(state).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or type(data.get('port')) is not int or not isinstance(data.get('token'), str)
            or not 0 < data['port'] < 65536):
        return None
    return data


def write_running(state, server):
    path = _running_path(state)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'schema': 1, 'port': server.port, 'pid': os.getpid(), 'token': server.token,
            'read_only': server.read_only, 'started_at': int(time.time())}
    handle, temporary = tempfile.mkstemp(prefix='.running-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_running(state, token=None):
    """Remove the record; with a token, only when it still names that server."""
    current = read_running(state)
    if token is not None and (current is None or not hmac.compare_digest(current['token'].encode(), token.encode())):
        return
    try:
        _running_path(state).unlink()
    except OSError:
        pass


def _request(running, method, path, timeout=2.0):
    connection = http.client.HTTPConnection(HOST, running['port'], timeout=timeout)
    try:
        headers = {'Host': f"127.0.0.1:{running['port']}", 'Authorization': 'Bearer ' + running['token']}
        if method == 'POST':
            headers.update({'Content-Type': 'application/json', 'Content-Length': '2'})
        connection.request(method, path, body=b'{}' if method == 'POST' else None, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def alive(running):
    try:
        status, _ = _request(running, 'GET', '/api/durum')
        return status == 200
    except (OSError, http.client.HTTPException):
        return False


def bind(core, port=None, read_only=False):
    candidates = (port,) if port else PORTS + (0,)
    last = None
    for candidate in candidates:
        try:
            return WebServer(candidate, core, read_only=read_only)
        except OSError as exc:
            last = exc
    raise WebError(409, f'Port kullanılamıyor: {port}' if port else 'Boş port bulunamadı') from last


def serve(core, port=None, read_only=False, open_browser=True, as_json=False):
    running = read_running(core.state)
    if running and alive(running):
        url = f"http://127.0.0.1:{running['port']}/?token={running['token']}"
        _say(as_json, {'durum': 'zaten_calisiyor', 'adres': url, 'salt_okunur': bool(running.get('read_only'))},
             'Web görünümü zaten çalışıyor: ' + url)
        if open_browser:
            webbrowser.open(url)
        return 0
    server = bind(core, port, read_only)
    write_running(core.state, server)
    url = f'http://127.0.0.1:{server.port}/?token={server.token}'
    _say(as_json, {'durum': 'basladi', 'adres': url, 'salt_okunur': server.read_only},
         '\n'.join(('Beyin web görünümü açıldı (yalnız bu bilgisayar).', 'Adres: ' + url,
                    'Mod: ' + ('salt okunur' if server.read_only else 'yazma açık (görev, eşitleme, sıkıştırma)'),
                    f'{IDLE_SECONDS // 60} dakika kullanılmazsa kapanır. Kapatmak için Ctrl+C ya da beyin.py web durdur.')))
    if open_browser:
        webbrowser.open(url)

    def watchdog():
        while not stopped.wait(15):
            if server.idle_expired():
                server.shutdown()
                return
    stopped = threading.Event()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.server_close()
        clear_running(core.state, server.token)
    if not as_json:
        print('Web görünümü kapandı.')
    return 0


def status(state):
    running = read_running(state)
    if running and alive(running):
        return {'durum': 'calisiyor', 'adres': f"http://127.0.0.1:{running['port']}/", 'port': running['port'],
                'salt_okunur': bool(running.get('read_only'))}
    return {'durum': 'calismiyor', 'eski_kayit': running is not None}


def stop(state):
    running = read_running(state)
    if not running:
        return {'durum': 'calismiyor'}
    if alive(running):
        try:
            _request(running, 'POST', '/api/kapat')
        except (OSError, http.client.HTTPException):
            pass
        deadline = time.monotonic() + 5
        while alive(running) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(running):
            return {'durum': 'durdurulamadi', 'port': running['port']}
        clear_running(state, running['token'])
        return {'durum': 'durduruldu', 'port': running['port']}
    clear_running(state, running['token'])
    return {'durum': 'calismiyor', 'eski_kayit_temizlendi': True}


def _human(result):
    state = result['durum']
    if state == 'calisiyor':
        return 'Web görünümü çalışıyor: ' + result['adres'] + (' (salt okunur)' if result.get('salt_okunur') else '')
    if state == 'durduruldu':
        return 'Web görünümü durduruldu.'
    if state == 'durdurulamadi':
        return 'Web görünümü durdurulamadı (port ' + str(result['port']) + '); terminalindeki sunucuyu Ctrl+C ile kapat.'
    return 'Web görünümü çalışmıyor.' + (' Eski çalışma kaydı temizlendi.' if result.get('eski_kayit_temizlendi') else '')


def _say(as_json, data, text):
    print(json.dumps(data, ensure_ascii=False) if as_json else text, flush=True)


def _state_for(vault, cli):
    """The state root beyin.py itself would use for this vault."""
    try:
        raw = json.loads((vault / '.beyin-runtime.json').read_text(encoding='utf-8')).get('state')
    except (OSError, ValueError, AttributeError):
        raw = None
    if isinstance(raw, str) and raw and Path(raw).expanduser().is_absolute():
        return cli.resolve_state(vault, Path(raw))
    return cli.default_state(vault)


def main(argv=None, vault=None, state=None, cli=None):
    parser = argparse.ArgumentParser(prog='beyin.py web', description='İsteğe bağlı yerel web görünümü (yalnız 127.0.0.1)')
    parser.add_argument('--json', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--vault', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--state', type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='komut')
    start = sub.add_parser('ac', help='Sunucuyu başlat ve tarayıcıda aç')
    start.add_argument('--port', type=int, help='Tercih edilen port (varsayılan 7373-7393 arası ilk boş port)')
    start.add_argument('--read-only', action='store_true', help='Salt okunur: görev, eşitleme ve sıkıştırma kapalı')
    start.add_argument('--no-browser', action='store_true', help='Tarayıcıyı açma; adresi yalnız yazdır')
    sub.add_parser('durum', help='Çalışıyor mu?')
    sub.add_parser('durdur', help='Çalışan sunucuyu kapat')
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    port = getattr(args, 'port', None)
    if port is not None and not 1024 <= port <= 65535:
        parser.error('--port 1024 ile 65535 arasında olmalı')
    vault = vault or args.vault
    if vault is None or not Path(vault).expanduser().is_dir():
        parser.error('--vault mevcut bir klasör olmalı')
    vault = Path(vault).expanduser().resolve()
    try:
        if (args.komut or 'ac') != 'ac':
            if state is None and args.state is None:
                cli = cli or load_cli()
            state = Path(state or args.state or _state_for(vault, cli)).resolve()
            result = status(state) if args.komut == 'durum' else stop(state)
            _say(args.json, result, _human(result))
            return 0 if result['durum'] != 'durdurulamadi' else 1
        cli = cli or load_cli()
        state = Path(state or args.state or _state_for(vault, cli)).resolve()
        if state == vault or vault in state.parents:
            raise WebError(400, 'state klasörü vault dışında olmalı')
        core = Core(vault, state, cli)
        return serve(core, port=port, read_only=getattr(args, 'read_only', False),
                     open_browser=not getattr(args, 'no_browser', False), as_json=args.json)
    except WebError as exc:
        _say(args.json, {'durum': 'hata', 'mesaj': exc.message}, 'Hata: ' + exc.message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
