"""#299: Defuddle and yt-dlp reach only public addresses, through a loopback guard proxy."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402

NO_TOOLS = {key: None for key in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
PUBLIC = '8.8.8.8'  # the address the fake resolver gives a "public" test name


def without_proxies():
    """os.environ for a helper run: the suite's environment with no proxy or bypass list of its own."""
    env = {key: value for key, value in inherited_env().items()
           if key.upper() not in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}
    return patch.dict(os.environ, env, clear=True)


class Pages:
    """Two loopback HTTP servers: `public` stands in for a site on the internet, `secret` is the
    local service a redirect or a rebound name must never reach."""

    def __init__(self, test):
        self.hits = {'public': [], 'secret': []}
        self.public = self._serve(test, 'public')
        self.secret = self._serve(test, 'secret')

    def _serve(self, test, name):
        pages = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                pages.hits[name].append(self.path)
                if name == 'public' and self.path.startswith('/yonlendir'):
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:%d/gizli' % pages.secret)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                text = ('GIZLI-YEREL-ICERIK ' if name == 'secret' else 'Acik makale metni. ') * 20
                body = ('<html><head><title>Sayfa</title></head><body><p>' + text + '</p></body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        test.addCleanup(worker.join, 5)
        test.addCleanup(server.server_close)
        test.addCleanup(server.shutdown)
        return server.server_port

    def network(self, private=(), rebound=()):
        """Resolver and dialer: `*.example` names are public (8.8.8.8, dialled to the public test page),
        names in `private` answer 10.0.0.5, names in `rebound` answer public once and loopback after."""
        dns, connect, asked = socket.getaddrinfo, socket.create_connection, {}

        def answer(host, port, *args, **kwargs):
            if host in rebound:
                asked[host] = asked.get(host, 0) + 1
                address = PUBLIC if asked[host] == 1 else '127.0.0.1'
            elif host in private:
                address = '10.0.0.5'
            elif str(host).endswith('.example'):
                address = PUBLIC
            else:
                return dns(host, port, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port))]

        def dial(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
            if address[0] == PUBLIC:
                return connect(('127.0.0.1', self.public), timeout, source_address)
            return connect(address, timeout, source_address)
        return patch.object(socket, 'getaddrinfo', side_effect=answer), patch.object(socket, 'create_connection', side_effect=dial)


def ask(proxy_port, request, follow=b''):
    """Send one raw proxy request and return the whole answer (the connection is closed after it)."""
    with socket.create_connection(('127.0.0.1', proxy_port), 10) as client:
        client.sendall(request)
        data = b''
        if follow:
            while b'\r\n\r\n' not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return data
                data += chunk
            if not data.startswith(b'HTTP/1.1 200'):
                return data
            client.sendall(follow)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return data
            data += chunk


class EgressGuardTest(unittest.TestCase):
    def setUp(self):
        self.pages = Pages(self)
        self.guard = yakala._EgressGuard()
        self.addCleanup(self.guard.close)
        self.port = int(self.guard.url.rsplit(':', 1)[1])

    def test_guard_tunnels_public_targets_and_refuses_local_ones(self):
        dns, dial = self.pages.network(private=('ic-ag.example',))
        with dns, dial:
            tunnel = ask(self.port, b'CONNECT ornek.example:443 HTTP/1.1\r\nHost: ornek.example:443\r\n\r\n',
                         b'GET /acik HTTP/1.1\r\nHost: ornek.example\r\nConnection: close\r\n\r\n')
            forward = ask(self.port, b'GET http://ornek.example/duz?a=1 HTTP/1.1\r\nHost: ornek.example\r\n'
                                     b'Proxy-Connection: keep-alive\r\n\r\n')
            refused = [ask(self.port, request) for request in (
                b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.pages.secret,
                b'GET http://127.0.0.1:%d/gizli HTTP/1.1\r\n\r\n' % self.pages.secret,
                b'CONNECT localhost:443 HTTP/1.1\r\n\r\n',
                b'CONNECT [::1]:443 HTTP/1.1\r\n\r\n',
                b'CONNECT ic-ag.example:443 HTTP/1.1\r\n\r\n',
                b'GET file:///etc/passwd HTTP/1.1\r\n\r\n',
                b'POST http://ornek.example/ HTTP/1.1\r\nContent-Length: 0\r\n\r\n',
                b'CONNECT ornek.example@127.0.0.1:443 HTTP/1.1\r\n\r\n')]
        self.assertIn(b'HTTP/1.1 200 Connection established', tunnel)
        self.assertIn(b'Acik makale metni.', tunnel)
        self.assertIn(b'Acik makale metni.', forward)
        self.assertEqual(self.pages.hits['public'], ['/acik', '/duz?a=1'])
        for reply in refused:
            self.assertTrue(reply.startswith(b'HTTP/1.1 403'), reply[:80])
        self.assertEqual(self.pages.hits['secret'], [])
        self.assertIn('https://ic-ag.example:443/', self.guard.blocked)
        self.assertIn('http://127.0.0.1:%d/gizli' % self.pages.secret, self.guard.blocked)

    def test_a_name_rebound_after_the_check_never_reaches_a_local_target(self):
        """The first answer passes _safe_network_url; the guard asks again and refuses the loopback one."""
        dns, dial = self.pages.network(rebound=('rebind.example',))
        with dns, dial:
            yakala._safe_network_url('https://rebind.example/')
            reply = ask(self.port, b'CONNECT rebind.example:%d HTTP/1.1\r\n\r\n' % self.pages.secret,
                        b'GET /gizli HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        self.assertTrue(reply.startswith(b'HTTP/1.1 403'), reply[:80])
        self.assertEqual(self.pages.hits, {'public': [], 'secret': []})

    def test_closing_the_guard_ends_open_tunnels(self):
        dns, dial = self.pages.network()
        with dns, dial:
            client = socket.create_connection(('127.0.0.1', self.port), 10)
            self.addCleanup(client.close)
            client.sendall(b'CONNECT ornek.example:443 HTTP/1.1\r\n\r\n')
            self.assertTrue(client.recv(4096).startswith(b'HTTP/1.1 200'))
            self.guard.close()
            client.settimeout(5)
            self.assertEqual(client.recv(4096), b'')  # the idle tunnel was ended, not left behind


# Stands in for `npx defuddle` as a real child process. Like Defuddle 0.19.4 (src/fetch.ts) it picks
# its proxy per request from HTTP(S)_PROXY/ALL_PROXY unless NO_PROXY names the host, and follows each
# redirect itself; without a proxy it connects directly.
HELPER = textwrap.dedent('''
    import json, os, sys, urllib.error, urllib.request
    from urllib.parse import urljoin, urlparse

    class Manual(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def proxy_for(url):
        host = urlparse(url).hostname or ''
        for pattern in (os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or '').split(','):
            pattern = pattern.strip().lstrip('.')
            if pattern == '*' or (pattern and (host == pattern or host.endswith('.' + pattern))):
                return None
        return (os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or os.environ.get('ALL_PROXY')
                or os.environ.get('all_proxy'))

    url = sys.argv[sys.argv.index('--') + 1]
    for _hop in range(5):
        proxy = proxy_for(url)
        handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {})
        try:
            body = urllib.request.build_opener(handler, Manual()).open(url, timeout=20).read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get('Location'):
                url = urljoin(url, exc.headers['Location'])
                continue
            print(json.dumps({'error': exc.code}))
            sys.exit(1)
        except Exception as exc:
            print(json.dumps({'error': str(exc)}))
            sys.exit(1)
        print(json.dumps({'content': body, 'title': 'Sayfa'}))
        sys.exit(0)
    sys.exit(1)
''')


class GuardedHelperTest(unittest.TestCase):
    """A real helper process through `_run`: what Defuddle does with ALL_PROXY, end to end."""

    def setUp(self):
        self.pages = Pages(self)
        tmp = tempfile.TemporaryDirectory(prefix='yakala-egress-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'defuddle_helper.py').write_text(HELPER, encoding='utf-8')
        real_run = yakala._run

        def run(command, timeout, cwd=None):  # the real _run (env, guard), aimed at the stand-in
            return real_run([sys.executable, str(self.root / 'defuddle_helper.py'), *command[1:]], timeout, cwd)
        for patcher in (patch.object(yakala, '_run', run), without_proxies(),
                        patch('urllib.request.getproxies', return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_redirect_to_a_local_service_is_refused_and_named_on_the_card(self):
        dns, dial = self.pages.network()
        vault = self.root / 'vault'
        vault.mkdir()
        card = yakala.find_card(vault, yakala.capture(vault, url='http://ornek.example/yonlendir')['id'])
        with dns, dial:
            text, method, note = yakala.extract(vault, card, dict(NO_TOOLS, npx='npx'))
        self.assertEqual((text, method), (None, None))
        self.assertIn('yerel veya ozel bir ag adresine yonlendirdi', note)
        self.assertEqual(self.pages.hits['secret'], [])
        self.assertEqual(self.pages.hits['public'], ['/yonlendir', '/yonlendir'])  # Defuddle, then the stdlib fallback

    def test_public_page_still_comes_through_the_guard(self):
        dns, dial = self.pages.network()
        info = {}
        with dns, dial:
            text = yakala.via_defuddle('http://ornek.example/acik', dict(NO_TOOLS, npx='npx'), info)
        self.assertIn('Acik makale metni.', text)
        self.assertEqual((self.pages.hits['public'], info.get('ag_engeli')), (['/acik'], None))

    def test_no_proxy_list_cannot_send_the_helper_around_the_guard(self):
        # Kept, NO_PROXY would send the redirect hop to 127.0.0.1 straight past the guard.
        dns, dial = self.pages.network()
        info = {}
        with dns, dial, patch.dict(os.environ, {'NO_PROXY': 'localhost,127.0.0.1'}):
            self.assertIsNone(yakala.via_defuddle('http://ornek.example/yonlendir', dict(NO_TOOLS, npx='npx'), info))
        self.assertEqual((self.pages.hits, info.get('ag_engeli')), ({'public': ['/yonlendir'], 'secret': []}, True))


class HelperEnvironmentTest(unittest.TestCase):
    def helper_envs(self, extra=None):
        """The environment each network helper is started with, without running one."""
        work = tempfile.TemporaryDirectory(prefix='yakala-env-')
        self.addCleanup(work.cleanup)
        video = 'https://www.youtube.com/watch?v=x'
        calls = ((yakala.via_defuddle, ('https://example.com/x', {'npx': 'npx'})),
                 (yakala.youtube_tracks, (video, {'yt_dlp': 'yt-dlp'}, work.name)),
                 (yakala.via_audio, (video, {'yt_dlp': 'yt-dlp', 'whisper': 'whisper'}, work.name)))
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC, 443))]
        envs = {}
        with without_proxies(), patch.dict(os.environ, extra or {}), \
                patch('urllib.request.getproxies', return_value={}), \
                patch.object(socket, 'getaddrinfo', return_value=public):
            for function, args in calls:
                if os.name == 'nt' and sys.platform == 'win32':
                    with patch.object(yakala, '_windows_run', return_value=(1, '', '')) as run:
                        function(*args)
                    envs[function.__name__] = run.call_args_list[0].args[3]
                else:
                    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='cmd', timeout=1)) as run:
                        function(*args)
                    envs[function.__name__] = run.call_args_list[0].kwargs['env']
        return envs

    def test_every_network_helper_gets_the_guard_as_its_only_proxy(self):
        for name, env in self.helper_envs({'NO_PROXY': 'localhost,127.0.0.1,.internal'}).items():
            with self.subTest(helper=name):
                upper = {key.upper(): value for key, value in env.items()}
                self.assertRegex(upper['ALL_PROXY'], r'^http://127\.0\.0\.1:\d+$')
                self.assertNotIn('NO_PROXY', upper)
                self.assertNotIn('HTTPS_PROXY', upper)
                # npm reads its own bypass list for its registry; Defuddle and yt-dlp do not.
                self.assertEqual(upper['NPM_CONFIG_NOPROXY'], 'localhost,127.0.0.1,.internal')

    def test_os_proxy_settings_stay_with_yt_dlp(self):
        """yt-dlp honours the OS proxy (urllib.getproxies); Defuddle reads only the environment."""
        with without_proxies():
            with patch('urllib.request.getproxies', return_value={'https': 'http://kurum-proxy.example:8080'}):
                self.assertTrue(yakala._user_proxy(system=True))
                self.assertFalse(yakala._user_proxy(system=False))
            with patch.dict(os.environ, {'https_proxy': 'http://kurum-proxy.example:8080'}):
                self.assertTrue(yakala._user_proxy(system=False))
            with patch.dict(os.environ, {'https_proxy': 'http://kurum-proxy.example:8080'}), \
                    yakala._HelperNetwork() as guard:
                self.assertIsNone(guard)  # the user's proxy resolves targets, as on the stdlib path

    def test_only_helpers_inside_the_block_are_routed(self):
        seen = []
        with without_proxies(), patch('urllib.request.getproxies', return_value={}):
            with yakala._HelperNetwork(system=True) as guard:
                self.assertIsNotNone(guard)
                seen.append(getattr(yakala._HELPER_NETWORK, 'guard', None))
            seen.append(getattr(yakala._HELPER_NETWORK, 'guard', None))
        self.assertIs(seen[0], guard)
        self.assertIsNone(seen[1])  # nothing outside the block (Whisper, pdftotext) is routed


if __name__ == '__main__':
    unittest.main()
