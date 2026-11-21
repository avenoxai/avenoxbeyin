"""#670: stdlib fallback pins destinations through the user's proxy, too."""
import socket
import importlib.util
import sys
import zipfile
import ssl
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from v3_yakala_egress_test import Pages, Upstream, PUBLIC, without_proxies, yakala


class StdlibProxyPinningTest(unittest.TestCase):
    def setUp(self):
        self.pages = Pages(self)
        self.upstream = Upstream(self)

    def fetch(self, path='/page', scheme='http', proxy_scheme='http', host='ornek.example'):
        proxy = '%s://user:secret@127.0.0.1:%d' % (proxy_scheme, self.upstream.port)
        with without_proxies(), patch('urllib.request.getproxies', return_value={scheme: proxy}):
            return yakala.via_stdlib('%s://%s%s' % (scheme, host, path))

    def test_http_proxy_receives_only_checked_ip_and_original_host_reaches_origin(self):
        dns, dial = self.pages.network()
        with dns, dial:
            text = self.fetch('/page?q=1')
        self.assertIn('Acik makale', text)
        self.assertEqual(self.upstream.asked[0][:3], ('http', 'CONNECT', PUBLIC + ':80'))
        self.assertEqual(self.pages.hits['public'], ['/page?q=1'])

    def test_tls_keeps_original_verification_context_and_hostname(self):
        # Use real tunnel sockets and an instrumented TLS boundary (no external network/cert).
        # Verification settings stay enabled; only the final encryption is stubbed to let the
        # synthetic HTTP endpoint observe the origin Host and absence of proxy credentials.
        context = ssl.create_default_context()
        observed = []
        parse = BaseHTTPRequestHandler.parse_request
        def record(handler):
            result = parse(handler)
            observed.append((handler.headers.get('Host'), handler.headers.get('Proxy-Authorization')))
            return result
        dns, dial = self.pages.network()
        with dns, dial, patch.object(ssl, '_create_default_https_context', return_value=context), \
                patch.object(context, 'wrap_socket', side_effect=lambda sock, **kwargs: sock) as tls, \
                patch.object(BaseHTTPRequestHandler, 'parse_request', record):
            self.assertIn('Acik makale', self.fetch('/tls', scheme='https'))
        self.assertEqual(tls.call_args.kwargs, {'server_hostname': 'ornek.example'})
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(observed, [('ornek.example', None)])
        self.assertEqual(self.upstream.asked[0][:3], ('http', 'CONNECT', PUBLIC + ':443'))
        self.assertIsNotNone(self.upstream.asked[0][3])

    def test_no_proxy_stays_direct_and_still_pins_checked_ip(self):
        dns, dial = self.pages.network()
        proxy = 'http://127.0.0.1:%d' % self.upstream.port
        with without_proxies(), dns, dial, patch('urllib.request.getproxies',
                return_value={'http': proxy, 'no': 'ornek.example'}), \
                patch('urllib.request.proxy_bypass', return_value=True):
            self.assertIn('Acik makale', yakala.via_stdlib('http://ornek.example/direct'))
        self.assertEqual(self.upstream.asked, [])
        self.assertEqual(self.pages.hits['secret'], [])

    def test_unsupported_tls_proxy_fails_closed(self):
        dns, dial = self.pages.network()
        with dns, dial:
            self.assertIsNone(self.fetch(proxy_scheme='https'))
        self.assertEqual(self.upstream.asked, [])
        self.assertEqual(self.pages.hits, {'public': [], 'secret': []})

    def test_rebinding_at_proxy_cannot_reach_private_service(self):
        # The local guard sees a public DNS answer. A separate upstream resolver gets loopback.
        # Real sockets still carry the traffic: the old hostname proxy path reaches the secret.
        original_dial = Upstream._dial
        connect = socket.create_connection
        def proxy_dns(host, port):
            if host == 'ornek.example':
                return connect(('127.0.0.1', self.pages.secret), 10)
            return original_dial(host, port)
        dns, dial = self.pages.network()
        with dns, dial, patch.object(Upstream, '_dial', staticmethod(proxy_dns)):
            text = self.fetch()
        self.assertEqual(self.pages.hits['secret'], [])
        self.assertIn('Acik makale', text)
        self.assertEqual(self.upstream.asked[0][:3], ('http', 'CONNECT', PUBLIC + ':80'))

    @unittest.skipUnless(shutil.which('openssl'), 'openssl required only to generate ephemeral TLS fixture')
    def test_real_tls_verification_and_sni_survive_numeric_proxy_tunnel(self):
        with tempfile.TemporaryDirectory() as folder:
            key, cert = Path(folder) / 'key.pem', Path(folder) / 'cert.pem'
            subprocess.run([shutil.which('openssl'), 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-days', '1', '-subj', '/CN=ornek.example', '-addext',
                            'subjectAltName=DNS:ornek.example', '-keyout', str(key), '-out', str(cert)],
                           capture_output=True, check=True, timeout=30)
            seen, names = [], []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append((self.headers.get('Host'), self.headers.get('Proxy-Authorization')))
                    body = b'<html><p>' + b'Verified TLS origin content. ' * 30 + b'</p></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *_):
                    pass
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            context.set_servername_callback(lambda sock, name, ctx: names.append(name))
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            trusted = ssl.create_default_context(cafile=str(cert))
            real_connect = socket.create_connection
            def dial(address, timeout=30, source_address=None):
                if address[0] == PUBLIC:
                    address = ('127.0.0.1', server.server_port)
                return real_connect(address, timeout, source_address)
            try:
                for proxy_scheme in ('http', 'socks5h', 'socks4a'):
                    with self.subTest(proxy=proxy_scheme):
                        dns, _ = self.pages.network()
                        with dns, patch.object(socket, 'create_connection', side_effect=dial), \
                                patch.object(ssl, '_create_default_https_context', return_value=trusted):
                            self.assertIn('Verified TLS origin content', self.fetch(scheme='https', proxy_scheme=proxy_scheme))
                dns, _ = self.pages.network()
                with dns, patch.object(socket, 'create_connection', side_effect=dial), \
                        patch.object(ssl, '_create_default_https_context', return_value=trusted):
                    self.assertIsNone(self.fetch(scheme='https', host='wrong.example'))
                self.assertEqual(names, ['ornek.example'] * 3 + ['wrong.example'])
                self.assertEqual(seen, [('ornek.example', None)] * 3)
                self.assertTrue(trusted.check_hostname)
                self.assertEqual(trusted.verify_mode, ssl.CERT_REQUIRED)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_redirect_to_private_target_is_blocked_before_proxy_request(self):
        dns, dial = self.pages.network()
        with dns, dial:
            text = self.fetch('/yonlendir')
        self.assertIsNone(text)
        self.assertEqual(len(self.upstream.asked), 1)
        self.assertEqual(self.pages.hits['secret'], [])

    def test_refused_proxy_tunnel_never_falls_back_direct(self):
        self.upstream.refuse = True
        dns, dial = self.pages.network()
        with dns, dial:
            self.assertIsNone(self.fetch())
        self.assertEqual(self.pages.hits, {'public': [], 'secret': []})
        self.assertEqual(self.upstream.asked[0][:3], ('http', 'CONNECT', PUBLIC + ':80'))

    def test_socks_remote_dns_setting_also_receives_numeric_target(self):
        for scheme in ('socks4a', 'socks5h'):
            with self.subTest(scheme=scheme):
                self.upstream.asked.clear()
                dns, dial = self.pages.network()
                with dns, dial:
                    self.assertIn('Acik makale', self.fetch(proxy_scheme=scheme))
                self.assertIn(PUBLIC, self.upstream.asked[0])
                self.assertNotIn('ornek.example', self.upstream.asked[0])


class InstalledStdlibProxyPinningTest(StdlibProxyPinningTest):
    @classmethod
    def setUpClass(cls):
        from v3_package_helpers import build_package, inherited_env, isolated_env
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        root = Path(cls.folder.name)
        env = inherited_env(**isolated_env(root / 'home'))
        env['XDG_STATE_HOME'] = str(root / 'state-home')
        package = build_package(root / 'release.zip', '3.13.0', env)
        extracted = root / 'package'
        with zipfile.ZipFile(package) as archive:
            archive.extractall(extracted)
        vault = root / 'vault'
        vault.mkdir()
        result = subprocess.run([sys.executable, str(extracted / 'scripts/install_v3.py'),
                                 '--vault', str(vault), '--state', str(root / 'state')],
                                capture_output=True, env=env, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr.decode('utf-8', 'replace'))
        module_path = vault / '.claude/scripts/beyin_v3_yakala.py'
        spec = importlib.util.spec_from_file_location('beyin_yakala_installed_proxy_test', module_path)
        installed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installed)
        replacement = patch(__name__ + '.yakala', installed)
        replacement.start()
        cls.addClassCleanup(replacement.stop)

    def test_installed_helper_proxy_contract(self):
        import v3_yakala_egress_test as egress
        suite = unittest.TestSuite()
        for case in (egress.PinnedProxyWireTest, egress.UserProxyChainTest):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        with patch.object(egress, 'yakala', yakala):
            outcome = unittest.TestResult()
            suite.run(outcome)
        self.assertEqual(outcome.errors + outcome.failures, [])
        self.assertEqual(outcome.testsRun, 9)


if __name__ == '__main__':
    unittest.main()
