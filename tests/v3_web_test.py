"""Optional local web view (#371, #423): loopback-only guards, sessions, CSRF and core paths.

Every read is checked against real index rows (a private note, a private task, an untrusted
note and a kasa-class folder), and every write against the files and records the core CLI
produced, so a gate that only filtered fake rows would fail here.
"""
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import beyin_v3_web as web  # noqa: E402
import beyin_v3_web_ui as ui  # noqa: E402
from v3_package_helpers import clean_environ, inherited_env, install, isolated_env  # noqa: E402

CLI = web.load_cli()
PRIVATE_NOTE = 'PRIVNOTE7731'
PRIVATE_TASK = 'PRIVTASK7731'
UNTRUSTED = 'UNTRUSTED7731'
SENSITIVE = 'SENSITIVE7731'
PUBLIC = 'ACIKNOT7731'


class WebFixture(unittest.TestCase):
    read_only = False

    def setUp(self):
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name) / 'vault'
        self.state = Path(temp.name) / 'state'
        (self.vault / 'notes').mkdir(parents=True)
        self.write('notes/acik.md', f'---\ntitle: Açık not\n---\nHerkese açık {PUBLIC} içerik.\n')
        self.write('notes/gizli.md', f'---\nvisibility: private\n---\nGizli {PRIVATE_NOTE} banka notu.\n')
        self.write('notes/disaridan.md', f'---\ntrust: untrusted\n---\nDışarıdan {UNTRUSTED} metin.\n')
        # A kasa-class folder in decomposed form: sync must never index it, whatever the normal form.
        self.write(unicodedata.normalize('NFD', 'Müşteri Kasası') + '/m.md', f'Müşteri {SENSITIVE} notu.\n')
        self.core = web.Core(self.vault, self.state, CLI)
        self.core.run('sync')
        self.public_task = self.core.run_json('task-create', {
            'source': 'tasks/acik-gorev.md', 'text': 'Açık görev gövdesi.',
            'metadata': {'id': 'acik-gorev', 'title': 'Açık görev', 'status': 'active', 'owner': 'ben', 'project': 'deneme'}})
        self.private_task = self.core.run_json('task-create', {
            'source': 'tasks/gizli-gorev.md', 'text': f'{PRIVATE_TASK} gövdesi.',
            'metadata': {'id': 'gizli-gorev', 'title': f'Gizli {PRIVATE_TASK}', 'status': 'active', 'owner': 'ben',
                         'visibility': 'private'}})
        self.ids = self.record_ids()
        self.server = web.WebServer(0, self.core, read_only=self.read_only)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.host = f'127.0.0.1:{self.server.port}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def write(self, relative, text):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def record_ids(self):
        import sqlite3
        db = sqlite3.connect(self.state / 'memory.sqlite3')
        try:
            rows = db.execute('SELECT id, payload FROM records').fetchall()
        finally:
            db.close()
        return {json.loads(payload)['source']: ident for ident, payload in rows}

    def request(self, method, path, headers=None, body=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=10)
        try:
            sent = {'Host': self.host}
            sent.update(headers or {})
            data = raw if raw is not None else (json.dumps(body).encode('utf-8') if body is not None else None)
            if method == 'POST' and 'Content-Length' not in sent:
                sent['Content-Length'] = str(len(data or b''))
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def login(self):
        response, _ = self.request('GET', '/?token=' + self.server.token)
        self.assertEqual(response.status, 303)
        cookie = response.getheader('Set-Cookie').split(';')[0]
        response, page = self.request('GET', '/', {'Cookie': cookie})
        self.assertEqual(response.status, 200)
        csrf = re.search(rb'name="beyin-csrf" content="([^"]+)"', page).group(1).decode()
        return cookie, csrf

    def write_headers(self, cookie, csrf, **extra):
        headers = {'Cookie': cookie, 'Origin': 'http://' + self.host, 'Content-Type': 'application/json',
                   'X-Beyin-CSRF': csrf}
        headers.update(extra)
        return {key: value for key, value in headers.items() if value is not None}

    def get_json(self, path, cookie):
        response, body = self.request('GET', path, {'Cookie': cookie})
        return response.status, json.loads(body)

    def task_files(self):
        return sorted(path.name for path in (self.vault / 'tasks').glob('*.md'))


class WebGuardTest(WebFixture):
    def test_foreign_host_origin_and_site_are_rejected_before_anything_is_served(self):
        for headers in ({'Host': f'evil.example:{self.server.port}'}, {'Host': '127.0.0.1:1'},
                        {'Origin': 'http://evil.example'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Sec-Fetch-Site': 'same-site'}):
            for path in ('/app.css', '/', '/?token=' + self.server.token, '/api/durum'):
                response, body = self.request('GET', path, headers)
                self.assertEqual(response.status, 403, (headers, path))
                self.assertNotIn(b'Set-Cookie', body)
                self.assertIsNone(response.getheader('Set-Cookie'))
        # localhost is the same listener; the Host check is case-insensitive.
        response, _ = self.request('GET', '/app.css', {'Host': f'LOCALHOST:{self.server.port}'})
        self.assertEqual(response.status, 200)

    def test_two_host_headers_are_rejected(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.port, timeout=5)
        try:
            connection.putrequest('GET', '/app.css', skip_host=True)
            connection.putheader('Host', self.host)
            connection.putheader('Host', 'evil.example')
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()

    def test_token_opens_a_separate_http_only_session_and_leaves_the_address(self):
        response, _ = self.request('GET', '/?token=wrong')
        self.assertEqual(response.status, 401)
        self.assertIsNone(response.getheader('Set-Cookie'))
        response, _ = self.request('GET', '/?token=' + self.server.token)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader('Location'), '/')
        cookie = response.getheader('Set-Cookie')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn(self.server.token, cookie)
        self.assertNotIn('Max-Age', cookie)
        # Without the cookie the page is the unlock form, never the app or its CSRF token.
        response, page = self.request('GET', '/')
        self.assertEqual(response.status, 401)
        self.assertNotIn(b'beyin-csrf', page)

    def test_api_needs_a_session_and_the_launch_token_is_not_a_query_credential(self):
        for path in ('/api/durum', '/api/gorevler', '/api/durum?token=' + self.server.token):
            response, _ = self.request('GET', path)
            self.assertEqual(response.status, 401, path)
        response, _ = self.request('GET', '/api/durum', {'Cookie': f'beyin_web_{self.server.port}=forged'})
        self.assertEqual(response.status, 401)

    def test_every_response_is_strict_and_sends_no_cors(self):
        cookie, _ = self.login()
        for path in ('/', '/app.js', '/app.css', '/api/durum', '/nope'):
            response, _ = self.request('GET', path, {'Cookie': cookie})
            csp = response.getheader('Content-Security-Policy')
            self.assertIn("default-src 'none'", csp, path)
            self.assertIn("script-src 'self';", csp)
            self.assertNotIn('unsafe', csp)
            self.assertNotIn('http', csp)
            self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')
            self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
            self.assertFalse([name for name, _ in response.getheaders() if name.lower().startswith('access-control-')])
        response, _ = self.request('OPTIONS', '/api/gorev', {'Origin': 'http://evil.example',
                                                             'Access-Control-Request-Method': 'POST'})
        self.assertNotEqual(response.status, 200)
        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))

    def test_writes_need_session_origin_json_and_the_session_csrf_token(self):
        cookie, csrf = self.login()
        other_cookie, other_csrf = self.login()
        before = self.task_files()
        body = {'baslik': 'Yazılmamalı', 'sorumlu': 'ben'}
        for headers, status in (
                (self.write_headers(None, csrf), 401),
                (self.write_headers(cookie, None), 403),
                (self.write_headers(cookie, 'yanlis'), 403),
                (self.write_headers(cookie, other_csrf), 403),
                (self.write_headers(cookie, csrf, Origin=None), 403),
                (self.write_headers(cookie, csrf, Origin='http://evil.example'), 403),
                (self.write_headers(cookie, csrf, **{'Content-Type': 'text/plain'}), 415)):
            response, _ = self.request('POST', '/api/gorev', headers, body)
            self.assertEqual(response.status, status, headers)
        self.assertEqual(self.task_files(), before)
        self.assertNotEqual(cookie, other_cookie)

    def test_request_bodies_are_bounded_and_the_server_survives(self):
        cookie, csrf = self.login()
        headers = self.write_headers(cookie, csrf)
        for length, status in (('x', 400), ('-1', 400), (str(web.MAX_BODY + 1), 413)):
            response, _ = self.request('POST', '/api/gorev', dict(headers, **{'Content-Length': length}), raw=b'')
            self.assertEqual(response.status, status, length)
        response, _ = self.request('POST', '/api/gorev', headers, raw=b'[1, 2]')
        self.assertEqual(response.status, 400)
        response, _ = self.request('GET', '/api/durum', {'Cookie': cookie})
        self.assertEqual(response.status, 200)

    def test_idle_clock_moves_on_use_not_on_static_files(self):
        cookie, _ = self.login()
        self.server.last_activity = 0.0
        self.request('GET', '/app.js')
        self.request('GET', '/app.css')
        self.assertTrue(web.alive({'port': self.server.port, 'token': self.server.token}))
        self.assertEqual(self.server.last_activity, 0.0)
        self.assertTrue(self.server.idle_expired(now=web.IDLE_SECONDS + 1))
        self.request('GET', '/api/durum', {'Cookie': cookie})
        self.assertFalse(self.server.idle_expired())


class WebPrivacyTest(WebFixture):
    def test_private_untrusted_and_kasa_class_content_is_never_served(self):
        cookie, csrf = self.login()
        self.assertIn('notes/gizli.md', self.ids)  # the private rows really are in the index
        self.assertIn('notes/disaridan.md', self.ids)
        self.assertIn('tasks/gizli-gorev.md', self.ids)
        self.assertFalse([source for source in self.ids if 'Kasas' in unicodedata.normalize('NFC', source)])
        status, found = self.get_json('/api/ara?q=' + PUBLIC, cookie)
        self.assertEqual((status, [r['kaynak'] for r in found['sonuclar']]), (200, ['notes/acik.md']))
        for secret in (PRIVATE_NOTE, PRIVATE_TASK, UNTRUSTED, SENSITIVE, 'Gizli', 'banka'):
            status, found = self.get_json('/api/ara?q=' + secret, cookie)
            self.assertEqual((status, found['sonuclar']), (200, []), secret)
        status, tasks = self.get_json('/api/gorevler', cookie)
        self.assertEqual([t['id'] for t in tasks['gorevler']], ['acik-gorev'])
        for source in ('notes/gizli.md', 'notes/disaridan.md', 'tasks/gizli-gorev.md'):
            status, body = self.get_json('/api/kayit?id=' + self.ids[source], cookie)
            self.assertEqual(status, 404, source)
            self.assertNotIn('metin', body)
        status, record = self.get_json('/api/kayit?id=' + self.ids['notes/acik.md'], cookie)
        self.assertEqual(status, 200)
        self.assertIn(PUBLIC, record['metin'])
        # A private task cannot be changed blind either, even with its id and revision.
        response, _ = self.request('POST', '/api/gorev/durum', self.write_headers(cookie, csrf),
                                   {'id': 'gizli-gorev', 'revizyon': 1, 'durum': 'done'})
        self.assertEqual(response.status, 404)
        self.assertIn('status": "active"', (self.vault / 'tasks/gizli-gorev.md').read_text(encoding='utf-8'))

    def test_a_note_made_private_after_indexing_is_withheld_until_and_after_sync(self):
        cookie, _ = self.login()
        self.write('notes/acik.md', f'---\ntitle: Açık not\nvisibility: private\n---\nHerkese açık {PUBLIC} içerik.\n')
        status, record = self.get_json('/api/kayit?id=' + self.ids['notes/acik.md'], cookie)
        self.assertEqual(status, 404)  # stale index row: source hash no longer matches
        self.core.run('sync')
        status, found = self.get_json('/api/ara?q=' + PUBLIC, cookie)
        self.assertEqual(found['sonuclar'], [])


class WebWriteTest(WebFixture):
    def test_task_create_and_status_change_use_the_core_task_commands(self):
        cookie, csrf = self.login()
        headers = self.write_headers(cookie, csrf)
        response, body = self.request('POST', '/api/gorev', headers, {
            'baslik': 'Şifre değil, İstanbul planı', 'sorumlu': 'ben', 'proje': 'deneme',
            'aciklama': unicodedata.normalize('NFD', 'Müşteri ziyareti için Çarşamba'), 'durum': 'active'})
        self.assertEqual(response.status, 200, body)
        task = json.loads(body)['gorev']
        self.assertRegex(task['id'], r'^web-\d{8}-[0-9a-f]{6}$')
        self.assertRegex(task['kaynak'], r'^tasks/sifre-degil-istanbul-plani-[0-9a-f]{6}\.md$')
        text = (self.vault / task['kaynak']).read_text(encoding='utf-8')
        self.assertIn('"kind": "task"', text)
        self.assertIn('"owner": "ben"', text)
        self.assertIn('"revision": 1', text)
        self.assertIn('Şifre değil, İstanbul planı', text)
        self.assertIn('Müşteri ziyareti için Çarşamba', text)  # NFC, Turkish letters kept
        store = self.core.store()
        row = self.core._row(store, task['id'])
        self.assertEqual((row['kind'], row['revision'], row['status']), ('task', 1, 'active'))
        self.assertEqual(row['source_sha256'], hashlib.sha256((self.vault / task['kaynak']).read_bytes()).hexdigest())
        response, body = self.request('POST', '/api/gorev/durum', headers,
                                      {'id': task['id'], 'revizyon': 1, 'durum': 'done'})
        self.assertEqual(response.status, 200, body)
        self.assertEqual(self.core._row(self.core.store(), task['id'])['revision'], 2)
        response, body = self.request('POST', '/api/gorev/durum', headers,
                                      {'id': task['id'], 'revizyon': 1, 'durum': 'active'})
        self.assertEqual(response.status, 409, body)
        for bad in ({'baslik': '', 'sorumlu': 'ben'}, {'baslik': 'x', 'sorumlu': ''},
                    {'baslik': 'x', 'sorumlu': 'ben', 'durum': 'bitti'}, {'baslik': 'x', 'sorumlu': 'ben', 'aciklama': '---\nid: y'},
                    {'baslik': ['x'], 'sorumlu': 'ben'}):
            response, _ = self.request('POST', '/api/gorev', headers, bad)
            self.assertEqual(response.status, 400, bad)
        self.assertFalse(list((self.state / 'web').glob('istek-*')))  # request files never linger

    def test_sync_and_compact_are_the_cli_commands(self):
        cookie, csrf = self.login()
        headers = self.write_headers(cookie, csrf)
        response, body = self.request('POST', '/api/esitle', headers, {})
        self.assertEqual((response.status, json.loads(body)['durum']), (200, 'succeeded'))
        response, body = self.request('POST', '/api/hafiza/sikistir', headers, {})
        self.assertEqual(response.status, 200, body)
        status, memory = self.get_json('/api/hafiza', cookie)
        self.assertEqual(status, 200)


class WebReadOnlyTest(WebFixture):
    read_only = True

    def test_read_only_mode_refuses_every_write_and_keeps_reads(self):
        cookie, csrf = self.login()
        headers = self.write_headers(cookie, csrf)
        before = self.task_files()
        for path, body in (('/api/gorev', {'baslik': 'x', 'sorumlu': 'ben'}),
                           ('/api/gorev/durum', {'id': 'acik-gorev', 'revizyon': 1, 'durum': 'done'}),
                           ('/api/esitle', {}), ('/api/hafiza/sikistir', {})):
            response, _ = self.request('POST', path, headers, body)
            self.assertEqual(response.status, 403, path)
        self.assertEqual(self.task_files(), before)
        status, tasks = self.get_json('/api/gorevler', cookie)
        self.assertEqual((status, [t['id'] for t in tasks['gorevler']]), (200, ['acik-gorev']))
        response, page = self.request('GET', '/', {'Cookie': cookie})
        self.assertIn(b'name="beyin-salt-okunur" content="1"', page)


class WebAssetTest(unittest.TestCase):
    def test_page_script_and_style_load_nothing_from_the_network(self):
        assets = ui.index_page('csrf', False) + ui.unlock_page(True) + ui.APP_JS + ui.APP_CSS
        self.assertNotRegex(assets, r'(?i)https?://|//[a-z0-9.-]+\.[a-z]{2,}/|@import|url\(')
        self.assertNotIn('innerHTML', ui.APP_JS)
        self.assertNotIn('insertAdjacentHTML', ui.APP_JS)
        self.assertNotRegex(ui.APP_JS, r'\beval\(|new Function|document\.write')
        for page in (ui.index_page('csrf', False), ui.unlock_page(False)):
            self.assertNotRegex(page, r'<script>|<style|\sstyle=|\son[a-z]+=')  # CSP has no unsafe-inline

    def test_csrf_value_is_escaped_into_the_page(self):
        self.assertIn('content="a&quot;b"', ui.index_page('a"b', False))

    def test_turkish_interface_text(self):
        page = ui.index_page('x', False)
        for text in ('Görevler', 'Hafıza', 'Eşitle', 'Sunucuyu kapat', 'Başlık', 'Açıklama', 'Sıkıştır', 'Önizleme'):
            self.assertIn(text, page)
        for text in ("'İptal'", "'Engellendi'", "'Bekliyor'", 'Görev oluşturuldu'):
            self.assertIn(text, ui.APP_JS)
        self.assertNotRegex(page + ui.APP_JS, '[–—â]')


class WebLifecycleTest(unittest.TestCase):
    def setUp(self):
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name) / 'vault'
        self.state = Path(temp.name) / 'state'
        self.vault.mkdir()

    def call(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = web.main(['--json', *argv], vault=self.vault, state=self.state, cli=CLI)
        return code, [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]

    def test_start_status_stop_and_stale_record(self):
        self.assertEqual(self.call('durum'), (0, [{'durum': 'calismiyor', 'eski_kayit': False}]))
        output = io.StringIO()
        with patch.object(web.webbrowser, 'open') as opened, patch('sys.stdout', output):
            thread = threading.Thread(target=web.main, args=(['--json', 'ac', '--no-browser'],),
                                      kwargs={'vault': self.vault, 'state': self.state, 'cli': CLI}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 30
            while web.read_running(self.state) is None and time.monotonic() < deadline:
                time.sleep(0.05)
            running = web.read_running(self.state)
            self.assertIsNotNone(running)
            self.assertTrue(web.alive(running))
            if os.name != 'nt':
                self.assertEqual((self.state / 'web' / web.RUNNING).stat().st_mode & 0o777, 0o600)
            self.assertEqual(web.status(self.state)['durum'], 'calisiyor')
            self.assertNotIn('token', json.dumps(web.status(self.state)))
            self.assertEqual(web.stop(self.state)['durum'], 'durduruldu')
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            opened.assert_not_called()
        started = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(started['durum'], 'basladi')
        self.assertTrue(started['adres'].startswith('http://127.0.0.1:'))
        self.assertIsNone(web.read_running(self.state))
        # A record left by a killed server is stale: status says so, stop clears it.
        (self.state / 'web').mkdir(parents=True, exist_ok=True)
        (self.state / 'web' / web.RUNNING).write_text(json.dumps({'port': 1, 'token': 'x'}), encoding='utf-8')
        self.assertEqual(web.status(self.state), {'durum': 'calismiyor', 'eski_kayit': True})
        self.assertEqual(web.stop(self.state), {'durum': 'calismiyor', 'eski_kayit_temizlendi': True})
        self.assertIsNone(web.read_running(self.state))

    def test_rejects_bad_port_and_state_inside_vault(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            web.main(['ac', '--port', '80'], vault=self.vault, state=self.state, cli=CLI)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = web.main(['--json', 'ac', '--no-browser'], vault=self.vault, state=self.vault / 'state', cli=CLI)
        self.assertEqual((code, json.loads(output.getvalue())['durum']), (1, 'hata'))
        self.assertFalse((self.vault / 'state').exists())

    def tearDown(self):
        running = web.read_running(self.state)
        if running:
            web.stop(self.state)


class WebInstalledEntryTest(unittest.TestCase):
    def test_installed_entry_runs_web_and_install_never_starts_or_wires_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, state = root / 'vault', root / 'state'
            vault.mkdir()
            env = isolated_env(root / 'home')
            env['BEYIN_UPDATES_OFF'] = '1'
            result = install(vault, state, env)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            for name in ('beyin_v3_web.py', 'beyin_v3_web_ui.py'):
                self.assertEqual((vault / '.claude/scripts' / name).read_bytes(), (SCRIPTS / name).read_bytes())
            self.assertFalse((state / 'web').exists())  # install started nothing
            for hooks in ('.claude/settings.local.json', '.codex/hooks.json', '.kiro/hooks/beyin-v3.json'):
                self.assertNotIn('beyin_v3_web', (vault / hooks).read_text(encoding='utf-8'))
            for launcher in vault.iterdir():
                if launcher.is_file() and launcher.suffix in ('.command', '.cmd', '.bat', '.sh', '.desktop', '.lnk'):
                    self.assertNotIn(b' web', launcher.read_bytes())
            run = subprocess.run([sys.executable, str(vault / 'beyin.py'), 'web', 'durum', '--json'], cwd=vault,
                                 env=env, capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', 'replace'))
            self.assertEqual(json.loads(run.stdout), {'durum': 'calismiyor', 'eski_kayit': False})

    def test_release_package_carries_the_web_files_through_the_updater_allowlist(self):
        spec = importlib.util.spec_from_file_location('beyin_web_release_builder', ROOT / 'scripts/build_v3_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        spec = importlib.util.spec_from_file_location('beyin_web_release_updater', SCRIPTS / 'beyin_v3_update.py')
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / 'beyin-v3.zip'
            builder.build(package, '9.9.9')
            manifest, files = updater.validate_package(package)
            for name in ('template/.claude/scripts/beyin_v3_web.py', 'template/.claude/scripts/beyin_v3_web_ui.py'):
                self.assertIn(name, files)
            with zipfile.ZipFile(package) as archive:
                self.assertFalse([n for n in archive.namelist() if '/web/' in n or n.endswith(('.html', '.css', '.png'))])

    def test_no_runtime_path_imports_the_web_view(self):
        offenders = []
        for path in [ROOT / 'scripts/install_v3.py', ROOT / 'scripts/beyin_v3.py'] + sorted(SCRIPTS.glob('beyin_v3*.py')):
            if path.name in ('beyin_v3_web.py', 'beyin_v3_web_ui.py'):
                continue
            if re.search(r'beyin_v3_web\b', path.read_text(encoding='utf-8')):
                offenders.append(path.name)
        self.assertEqual(offenders, [])
        entry = (ROOT / 'scripts/beyin_entry.py').read_text(encoding='utf-8')
        self.assertEqual(entry.count('import beyin_v3_web'), 1)
        self.assertIn("argv[0] == 'web'", entry)


if __name__ == '__main__':
    unittest.main()
