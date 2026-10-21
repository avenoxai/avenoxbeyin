#!/usr/bin/env python3
"""Comprehensive test suite for Beyin Web Portal (Issue #371).
Tests server lifecycle, security contracts, cookie authentication,
read-only mode, and scope invariants.
"""
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys_path = [str(ROOT / "scripts"), str(ROOT / "template" / ".claude" / "scripts")]
for p in sys_path:
    if p not in sys.path:
        sys.path.insert(0, p)

import beyin_v3_web as web


class TestBeyinWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-web-test-"))
        self.vault = self.tmp / "vault"
        self.state = self.tmp / "state"
        self.vault.mkdir()
        self.state.mkdir()

        # Seed mock vault structure
        (self.vault / "AGENTS.md").write_text("# Test Vault", encoding="utf-8")
        (self.vault / "Kurallar.md").write_text("# Kurallar\n- **kural (test):** test kuralı\n", encoding="utf-8")
        (self.vault / "🔮 850-Companion").mkdir()
        (self.vault / "🔮 850-Companion" / "Threads.md").write_text("# Threads\n## Active Threads\n### Aktif Proje (2026-10-21)\n- [ ] Alt görev 1 (P0)\n", encoding="utf-8")
        (self.vault / "🔮 850-Companion" / "Last-Session.md").write_text("# Son oturum\n## 2026-10-21 01:00 · test-etiket · a1b2c3d4\n- Oturum özeti\n", encoding="utf-8")
        (self.vault / "🔮 850-Companion" / "Core.md").write_text("# Düşünme ortağı\n", encoding="utf-8")
        (self.vault / "🔮 850-Companion" / "Journal.md").write_text("# Journal\n", encoding="utf-8")

        # Projects
        proj_dir = self.vault / "projects" / "test-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "tasks.md").write_text("- [ ] Task 1 (P0)\n- [x] Task 2\n", encoding="utf-8")
        (proj_dir / "Kurallar.md").write_text("- **kural (p-test):** proje kurali\n", encoding="utf-8")

        # Goals
        goals_dir = self.vault / "goals"
        goals_dir.mkdir()
        (goals_dir / "hedef1.md").write_text("---\ntitle: 'Test Goal'\nprogress: 75\n---\nGoal content", encoding="utf-8")

        # Receipts
        receipts_dir = self.vault / "receipts"
        receipts_dir.mkdir()
        (receipts_dir / "receipt-1.md").write_text('{"summary": "Test receipt", "project": "test-proj"}\n# Receipt Body', encoding="utf-8")

        # Copy web assets
        web_src = ROOT / "template" / ".claude" / "web"
        self.web_dir = self.vault / ".claude" / "web"
        self.web_dir.mkdir(parents=True)
        if (web_src / "index.html").is_file():
            shutil.copy(web_src / "index.html", self.web_dir / "index.html")
        else:
            (self.web_dir / "index.html").write_text("<!DOCTYPE html><html><body>Test</body></html>", encoding="utf-8")
        if (web_src / "avenox_logo.png").is_file():
            shutil.copy(web_src / "avenox_logo.png", self.web_dir / "avenox_logo.png")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_free_port(self):
        port = web.find_free_port()
        self.assertTrue(isinstance(port, int))
        self.assertGreater(port, 1024)

    def test_status_when_stopped(self):
        st = web.status_server(self.vault, self.state)
        self.assertEqual(st["status"], "calismiyor")

    def test_server_lifecycle_and_security(self):
        port = web.find_free_port()
        server = web.BeyinWebServer("127.0.0.1", port, self.vault, self.state, self.web_dir, read_only=False)
        web.write_lock_file(server)

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)

        try:
            # 1. Check lock file
            lock_path = self.state / "web" / "running" / f"{port}.json"
            self.assertTrue(lock_path.is_file())
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock_data["port"], port)
            self.assertEqual(lock_data["vault"], str(self.vault.resolve()))

            # 2. Status command detects running server
            st = web.status_server(self.vault, self.state)
            self.assertEqual(st["status"], "calisiyor")
            self.assertEqual(st["port"], port)

            # 3. Security: Host header attack rejection
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/pulse", headers={"Host": "malicious.com"})
            res = conn.getresponse()
            self.assertEqual(res.status, 401)
            conn.close()

            # 3b. Security: Portless Host rejection (prevent port bypass)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/pulse", headers={"Host": "127.0.0.1"})
            res = conn.getresponse()
            self.assertEqual(res.status, 401)
            conn.close()

            # 4. Security: Unauthenticated request rejection
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/pulse", headers={"Host": f"127.0.0.1:{port}"})
            res = conn.getresponse()
            self.assertEqual(res.status, 401)
            conn.close()

            # 5. Initial auth via token query param sets cookie
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", f"/?token={server.session_token}", headers={"Host": f"127.0.0.1:{port}"})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            set_cookie = res.getheader("Set-Cookie")
            self.assertIn(f"bw_session_{port}={server.session_token}", set_cookie)
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=Strict", set_cookie)
            # Check CSP headers
            csp = res.getheader("Content-Security-Policy")
            self.assertIn("default-src 'self'", csp)
            self.assertIn("connect-src 'self'", csp)
            conn.close()

            # 6. Authenticated requests with cookie
            headers = {
                "Host": f"127.0.0.1:{port}",
                "Cookie": f"bw_session_{port}={server.session_token}"
            }
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/pulse", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode())
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["vault_name"], "vault")
            conn.close()

            # 7. Live stats endpoint
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/live-stats", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode())
            self.assertEqual(data["doctor"]["status"], "ok")
            self.assertGreaterEqual(data["counts"]["projects"], 1)
            self.assertEqual(data["counts"]["goals"], 1)
            self.assertEqual(data["counts"]["receipts"], 1)
            self.assertEqual(len(data["recent_receipts"]), 1)
            conn.close()

            # 8. Projects endpoint
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/projects", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode())
            self.assertGreaterEqual(len(data["projects"]), 1)
            p_names = [p["name"] for p in data["projects"]]
            self.assertIn("test-proj", p_names)
            t_proj = next(p for p in data["projects"] if p["name"] == "test-proj")
            self.assertEqual(t_proj["task_count"], 2)
            self.assertEqual(t_proj["done_count"], 1)
            conn.close()

            # 9. Auditor endpoint
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/auditor", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode())
            self.assertIn("Threads.md", data["stats"])
            self.assertIn("Last-Session.md", data["stats"])
            self.assertGreaterEqual(len(data["sessions"]), 1)
            conn.close()

            # 10. Goals endpoint (read)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/v1/goals", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read().decode())
            self.assertEqual(len(data["goals"]), 1)
            self.assertEqual(data["goals"][0]["progress"], 75)
            self.assertGreaterEqual(len(data["habits"]), 1)
            conn.close()

            # 11. Security: POST without Origin / X-Beyin-Web header rejected with 403
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            body = json.dumps({"filename": "yeni_hedef.md", "content": "# Yeni Hedef\nprogress: 50\n"})
            conn.request("POST", "/api/v1/goals", body=body, headers={**headers, "Content-Type": "application/json"})
            res = conn.getresponse()
            self.assertEqual(res.status, 403)
            conn.close()

            # 11b. Valid POST with valid write headers succeeds
            valid_write_headers = {
                **headers,
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "X-Beyin-Web": "1"
            }
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("POST", "/api/v1/goals", body=body, headers=valid_write_headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            conn.close()
            self.assertTrue((self.vault / "goals" / "yeni_hedef.md").is_file())

            # 12. Security: Path traversal write rejection
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            body = json.dumps({"filename": "../malicious.md", "content": "malicious"})
            conn.request("POST", "/api/v1/goals", body=body, headers=valid_write_headers)
            res = conn.getresponse()
            self.assertIn(res.status, (400, 403))
            conn.close()
            self.assertFalse((self.vault / "malicious.md").is_file())

            # 13. Task toggle endpoint
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            toggle_body = json.dumps({"source_file": "projects/test-proj/tasks.md", "line_index": 0, "done": True})
            conn.request("POST", "/api/v1/tasks/toggle", body=toggle_body, headers=valid_write_headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            conn.close()
            tasks_content = (self.vault / "projects" / "test-proj" / "tasks.md").read_text(encoding="utf-8")
            self.assertIn("- [x] Task 1 (P0)", tasks_content)

            # 14. Heartbeat resets idle timer
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("POST", "/api/v1/heartbeat", headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            conn.close()

            # 15. Unauthenticated logo access succeeds
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/avenox_logo.png", headers={"Host": f"127.0.0.1:{port}"})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            conn.close()

        finally:
            server.shutdown()
            server.server_close()
            web.remove_lock_file(server)

    def test_read_only_mode(self):
        port = web.find_free_port()
        server = web.BeyinWebServer("127.0.0.1", port, self.vault, self.state, self.web_dir, read_only=True)
        web.write_lock_file(server)

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)

        try:
            headers = {
                "Host": f"127.0.0.1:{port}",
                "Cookie": f"bw_session_{port}={server.session_token}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
                "X-Beyin-Web": "1"
            }
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            body = json.dumps({"filename": "ro_test.md", "content": "test"})
            conn.request("POST", "/api/v1/goals", body=body, headers=headers)
            res = conn.getresponse()
            self.assertEqual(res.status, 403)
            conn.close()
            self.assertFalse((self.vault / "goals" / "ro_test.md").is_file())
        finally:
            server.shutdown()
            server.server_close()
            web.remove_lock_file(server)


if __name__ == "__main__":
    unittest.main()
