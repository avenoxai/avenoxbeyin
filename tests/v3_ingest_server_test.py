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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_ingest_server as ingest_server


class IngestServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-ingest-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
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
        self.assertTrue(target.is_relative_to(self.vault / "000-Inbox"))
        self.assertIn("sqlite-kilitleri-ve-cozumleri", target.name)

        content = target.read_text(encoding="utf-8")
        self.assertIn("source: mobile-ingest", content)
        self.assertIn("url: https://example.com/sqlite-locks", content)
        self.assertIn("Windows üzerinde dosya kilitleme", content)

    def test_http_server_lifecycle_and_endpoints(self):
        # Bind on ephemeral port (port=0)
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
            self.assertIn("İzinsiz fırlatma", file_path.read_text(encoding="utf-8"))


if __name__ == '__main__':
    unittest.main()
