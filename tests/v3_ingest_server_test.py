"""Unit tests for mobile / external ingest server (Issue #288 Item 4)."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_ingest_server as ingest_server
from v3_package_helpers import inherited_env, install, run_python


class IngestServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-ingest-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = (Path(self.tmp.name) / 'vault').resolve()
        self.vault.mkdir()

    def test_save_capture_creates_inbox_markdown_with_frontmatter(self):
        payload = {
            "title": "SQLite Kilitleri ve Çözümleri",
            "url": "https://example.com/sqlite-locks",
            "text": "Windows üzerinde dosya kilitleme ve closing() kullanımı.",
            "tags": ["sqlite", "windows", "mimari"]
        }
        target, nbytes = ingest_server.save_capture(self.vault, payload)
        self.assertTrue(target.is_file())
        self.assertTrue(target.resolve().is_relative_to((self.vault / "000-Inbox").resolve()))
        self.assertIn("sqlite-kilitleri-ve-cozumleri", target.name)

        content = target.read_text(encoding="utf-8")
        self.assertIn('source: "mobile-ingest"', content)
        self.assertIn("untrusted: true", content)
        self.assertIn('url: "https://example.com/sqlite-locks"', content)
        self.assertIn("Windows üzerinde dosya kilitleme", content)

    def test_save_capture_resolves_existing_custom_inbox(self):
        custom_inbox = (self.vault / "📥 000-Inbox").resolve()
        custom_inbox.mkdir()
        target, _ = ingest_server.save_capture(self.vault, "Özel gelen kutusu testi")
        self.assertTrue(target.resolve().is_relative_to(custom_inbox))

    def test_yaml_frontmatter_sanitization_prevents_injection(self):
        malicious = {
            "title": "Başlık\ninjected_field: true\n---\nMalicious content",
            "url": "https://evil.com/a\nb: c",
            "source": "mobile\nevil: 1",
            "text": "Normal içerik"
        }
        target, _ = ingest_server.save_capture(self.vault, malicious)
        content = target.read_text(encoding="utf-8")
        # Ensure frontmatter only has one closing ---
        parts = content.split("---\n")
        self.assertEqual(len(parts), 3, "Frontmatter should not have injected separators")
        self.assertNotIn("\ninjected_field:", parts[1])

    def test_http_server_lifecycle_and_endpoints(self):
        server = ingest_server.create_server(self.vault, host="127.0.0.1", port=0, token="test-secret-token")
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(cleanup_server)

        # 1. GET /health -> 200 OK
        req = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode('utf-8'))
            self.assertEqual(data.get("service"), "beyin-ingest-server")
            self.assertEqual(data.get("status"), "ok")

        # 2. GET /vault-file -> 404 (strictly rejected, zero data leakage)
        try:
            with urllib.request.urlopen(f"{base_url}/000-Inbox/something.md"):
                self.fail("Expected 404")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 404)
            err.close()

        # 3. POST /ingest without auth -> 401 Unauthorized
        post_data = json.dumps({"text": "İzinsiz fırlatma"}).encode("utf-8")
        req_unauth = urllib.request.Request(f"{base_url}/ingest", data=post_data,
                                           headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req_unauth):
                self.fail("Expected 401")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 401)
            err.close()

        # 4. POST /ingest with wrong token -> 401 Unauthorized
        req_wrong = urllib.request.Request(f"{base_url}/ingest", data=post_data,
                                          headers={"Content-Type": "application/json",
                                                   "Authorization": "Bearer wrong-token"})
        try:
            with urllib.request.urlopen(req_wrong):
                self.fail("Expected 401")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 401)
            err.close()

        # 5. POST /ingest with valid Bearer token -> 201 Created
        req_valid = urllib.request.Request(f"{base_url}/ingest", data=post_data,
                                          headers={"Content-Type": "application/json",
                                                   "Authorization": "Bearer test-secret-token"})
        with urllib.request.urlopen(req_valid) as resp:
            self.assertEqual(resp.status, 201)
            result = json.loads(resp.read().decode('utf-8'))
            self.assertEqual(result.get("status"), "accepted")
            file_path = self.vault / result["file"]
            self.assertTrue(file_path.is_file())
            content = file_path.read_text(encoding="utf-8")
            self.assertIn("İzinsiz fırlatma", content)
            self.assertIn("untrusted: true", content)

        # 6. Query strings cannot carry credentials
        req_query = urllib.request.Request(f"{base_url}/ingest?token=test-secret-token", data=post_data,
                                           headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req_query)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        # 7. OPTIONS /ingest -> 204 No Content with CORS
        req_options = urllib.request.Request(f"{base_url}/ingest", method="OPTIONS")
        with urllib.request.urlopen(req_options) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

        # 8. Payload too large -> 413
        oversized = b"x" * (ingest_server.MAX_PAYLOAD_BYTES + 10)
        req_oversized = urllib.request.Request(f"{base_url}/ingest", data=oversized,
                                               headers={"Content-Type": "text/plain",
                                                        "Authorization": "Bearer test-secret-token"})
        try:
            with urllib.request.urlopen(req_oversized):
                self.fail("Expected 413 Payload Too Large")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 413)
            err.close()
        except (urllib.error.URLError, ConnectionError) as err:
            # If underlying TCP socket was reset by kernel before response body was read
            reason = getattr(err, "reason", err)
            self.assertIsInstance(reason, ConnectionError)

    def test_json_response_tolerates_disconnects_at_each_write_stage(self):
        for stage in ('headers', 'body', 'flush'):
            for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                with self.subTest(stage=stage, error=error.__name__):
                    handler = object.__new__(ingest_server.IngestHandler)
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock()
                    handler.wfile = Mock()
                    write = {'headers': handler.end_headers, 'body': handler.wfile.write,
                             'flush': handler.wfile.flush}[stage]
                    write.side_effect = error('client disconnected')
                    handler._send_json(413, {'error': 'payload_too_large'})
                    write.assert_called_once()

    def test_json_response_does_not_hide_unrelated_io_errors(self):
        handler = object.__new__(ingest_server.IngestHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = OSError('unrelated I/O failure')
        with self.assertRaisesRegex(OSError, 'unrelated I/O failure'):
            handler._send_json(200, {'status': 'ok'})

    def test_symlink_default_inbox_cannot_create_external_file(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        try:
            (self.vault / '000-Inbox').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on host')
        with self.assertRaises(ValueError):
            ingest_server.save_capture(self.vault, 'capture')
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_captures_are_append_only(self):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda _: ingest_server.save_capture(self.vault, 'same', now)[0], range(12)))
        self.assertEqual(len(set(paths)), 12)

    def test_non_loopback_requires_auth_and_scalars_are_strings(self):
        with self.assertRaises(ValueError):
            ingest_server.create_server(self.vault, host='0.0.0.0', port=0)
        from beyin_v3_sync import parse
        target, _ = ingest_server.save_capture(self.vault, {'text': 'capture', 'source': r'\evil: true', 'url': 'false'})
        fields, _ = parse(target.read_text())
        self.assertEqual(fields['source'], r'\evil: true')
        self.assertEqual(fields['url'], 'false')
        self.assertTrue(fields['untrusted'])
        from beyin_v3_sync import SyncEngine
        engine = SyncEngine(self.vault, Path(self.tmp.name) / 'state')
        engine.sync()
        with engine.store._connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['trust'], 'untrusted')

    def test_installed_capture_trust_is_enforced_by_real_index(self):
        state = Path(self.tmp.name) / 'installed-state'
        env = inherited_env(BEYIN_UPDATES_OFF='1', BEYIN_V3_NO_SPAWN='1')
        result = install(self.vault, state, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        script = Path(self.tmp.name) / 'capture.py'
        script.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]) / '.claude/scripts'))\nfrom beyin_v3_ingest_server import save_capture\nsave_capture(Path(sys.argv[1]), {'text': 'INSTALLED_INGEST_CANARY'})\n")
        result = run_python(script, [self.vault], self.vault, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_python(self.vault / 'beyin.py', ['sync', '--json'], self.vault, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(state / 'memory.sqlite3')) as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records')]
        captured = [row for row in rows if 'INSTALLED_INGEST_CANARY' in json.dumps(row)]
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['trust'], 'untrusted')
        result = run_python(self.vault / 'beyin.py', ['context', 'INSTALLED_INGEST_CANARY', '--json'], self.vault, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['records'], [])

    def test_tokenless_loopback_refuses_browser_writes_but_accepts_native(self):
        server = ingest_server.create_server(self.vault, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        url = f'http://127.0.0.1:{server.server_address[1]}/ingest'
        for headers in ({'Origin': 'https://attacker.example'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            request = urllib.request.Request(url, data=b'capture', headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            self.assertIsNone(caught.exception.headers.get('Access-Control-Allow-Origin'))
            caught.exception.close()
        self.assertFalse((self.vault / '000-Inbox').exists())
        with urllib.request.urlopen(urllib.request.Request(url, data=b'native capture')) as response:
            self.assertEqual(response.status, 201)
            self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))

    def test_cli_ingest_server_help(self):
        import subprocess
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                               'ingest-server', '--help'], capture_output=True, text=True, check=True, env=inherited_env(), timeout=60)
        self.assertIn("--host", proc.stdout)
        self.assertIn("--port", proc.stdout)
        self.assertIn("--token", proc.stdout)

    def test_stress_http_socket_resilience(self):
        import socket
        import struct
        import threading
        import time
        server = ingest_server.create_server(self.vault, port=0)
        port = server.server_address[1]
        # The server already survived a dropped client before #393; what changed is that the
        # drop no longer reaches handle_error, which prints a traceback for every request.
        errors, accepted, closed = [], [], []
        server.handle_error = lambda request, address: errors.append(address)
        process_request = server.process_request
        server.process_request = lambda request, address: (accepted.append(1), process_request(request, address))
        shutdown_request = server.shutdown_request
        server.shutdown_request = lambda request: (closed.append(1), shutdown_request(request))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), t.join(2)))

        # Winsock linger uses two shorts; POSIX uses two native ints.
        linger = struct.pack('hh' if sys.platform == 'win32' else 'ii', 1, 0)

        # 1. Connection reset during read
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))
        s.sendall(b"POST /ingest HTTP/1.1\r\nContent-Length: 100\r\n\r\n")
        s.sendall(b"half data")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        s.close()
        time.sleep(0.05)

        # 2. Connection closed immediately after connect
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))
        s.close()
        time.sleep(0.05)

        # 3. Connection reset during OPTIONS
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))
        s.sendall(b"OPTIONS /ingest HTTP/1.1\r\n\r\n")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        s.close()
        time.sleep(0.05)

        # 4. Connection reset during GET
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))
        s.sendall(b"GET /health HTTP/1.1\r\n\r\n")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        s.close()
        time.sleep(0.05)

        # 5. Server remains healthy and accepts new normal requests
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
            self.assertEqual(resp.status, 200)
        # A reset that lands before accept() may never reach the server (ECONNABORTED), so wait
        # for every accepted request, the /health one included, instead of a fixed count.
        deadline = time.monotonic() + 10
        while len(closed) < len(accepted) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(closed), len(accepted))
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
