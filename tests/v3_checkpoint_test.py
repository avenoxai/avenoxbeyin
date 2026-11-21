"""Unit tests for unified session checkpoint handoff (Issue #767)."""
import unittest
import tempfile
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "template" / ".claude" / "scripts"))

from beyin_v3_checkpoint import run_checkpoint
from beyin_v3_sync import SyncEngine
import beyin_v3_chat_trace
from tests.v3_package_helpers import inherited_env

class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.vault = Path(self.test_dir) / "vault"
        self.vault.mkdir()
        self.state = Path(self.test_dir) / "state"
        self.state.mkdir()

        # Initialize companion folder & files
        self.comp = self.vault / "🔮 850-Companion"
        self.comp.mkdir()
        (self.comp / "Last-Session.md").write_text("# Last-Session\n\n## 2026-11-01 10:00 · eski oturum · 12345678\nEski kart gövdesi\n", encoding="utf-8")
        (self.comp / "Threads.md").write_text("# Threads\n\n### Aktif İşler\n- eski görev\n", encoding="utf-8")

        # Initialize test source for receipt
        (self.vault / "knowledge").mkdir()
        (self.vault / "knowledge" / "test.md").write_text("Test knowledge content", encoding="utf-8")

        self.sync = SyncEngine(self.vault, self.state)
        self.sync.sync()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_single_checkpoint_creates_and_updates(self):
        beyin_v3_chat_trace.configure(self.state, True)
        session_id = "123456789012345678901234"

        req = {
            "card": {"label": "yeni oturum", "body": "Harika ilerleme kaydedildi."},
            "threads": [{"heading": "Aktif İşler", "next_step": "yeni adım"}],
            "learn": [{"candidate": "Önemli ders", "source": "knowledge/test.md", "scope": "global"}],
            "receipt": {
                "event_id": "ev-001",
                "summary": "checkpoint testi",
                "refs": ["knowledge/test.md"]
            },
            "chat_trace": "Bugün checkpoint tamamlandı."
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session=session_id)
        self.assertEqual(res["status"], "succeeded")
        self.assertIn("card", res["steps"])
        self.assertIn("threads", res["steps"])
        self.assertIn("learn", res["steps"])
        self.assertIn("receipt", res["steps"])
        self.assertIn("chat_trace", res["steps"])

        last_session_text = (self.comp / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn("12345678", last_session_text)
        self.assertIn("Harika ilerleme kaydedildi.", last_session_text)

        threads_text = (self.comp / "Threads.md").read_text(encoding="utf-8")
        self.assertIn("yeni adım", threads_text)

    def test_second_checkpoint_replaces_sibling_card(self):
        session_id = "123456789012345678901234"
        req1 = {
            "card": {"label": "ilk durum", "body": "İlk gövde."},
        }
        run_checkpoint(self.sync, self.vault, self.state, req1, harness="claude", session=session_id)

        req2 = {
            "card": {"label": "ikinci durum", "body": "Güncellenmiş gövde."},
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req2, harness="claude", session=session_id)
        self.assertEqual(res["status"], "succeeded")

        last_session_text = (self.comp / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn("Güncellenmiş gövde.", last_session_text)
        self.assertNotIn("İlk gövde.", last_session_text)
        self.assertIn("12345678", last_session_text)

    def test_automatic_compaction(self):
        limits_file = self.state / "companion-limits.json"
        limits_file.write_text(json.dumps({"schema": 1, "Last-Session.md": 20, "Threads.md": 20000}), encoding="utf-8")

        (self.comp / "Last-Session.md").write_text("# Long Last-Session\n\n## 2026-11-01 10:00 · uzun oturum · 12345678\nÇok uzun içerik metni burada yer alıyor ve limiti aşıyor.\n", encoding="utf-8")

        req = {
            "card": {"label": "compaction test", "body": "Test kompaksiyon."},
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session="session-compaction")
        self.assertEqual(res["status"], "succeeded")
        self.assertIn("compact", res["steps"])

    def test_partial_error_handling(self):
        req = {
            "card": {"label": "hata testi", "body": "Test hata."},
            "receipt": {
                "event_id": "ev-err",
                "summary": "bozuk ref",
                "refs": ["olmayan-dosya.md"]
            }
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session="session-error")
        self.assertEqual(res["status"], "partial")
        self.assertIsInstance(res["steps"]["receipt"], dict)
        self.assertEqual(res["steps"]["receipt"]["status"], "error")

    def test_cli_checkpoint_and_human_result(self):
        import subprocess
        import sys
        req_file = Path(self.test_dir) / "checkpoint_req.json"
        req_file.write_text(json.dumps({"card": {"label": "cli", "body": "CLI testi."}}), encoding="utf-8")

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "beyin_v3.py"
        proc = subprocess.run(
            [sys.executable, str(script_path), "--vault", str(self.vault), "--state", str(self.state), "checkpoint", "--file", str(req_file), "--harness", "claude", "--session", "abcd1234abcd1234"],
            capture_output=True, text=True, encoding="utf-8", check=False
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "succeeded")

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import beyin_entry
        human_text = beyin_entry.human_result(data, "checkpoint")
        self.assertIn("Oturum kontrol noktasi (succeeded):", human_text)

    def test_dry_run_leaves_entire_tree_unchanged(self):
        root = Path(self.test_dir)
        source = Path(__file__).resolve().parents[1]
        installed_scripts = self.vault / ".claude" / "scripts"
        shutil.copytree(source / "template" / ".claude" / "scripts", installed_scripts,
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(source / "scripts" / "beyin_v3.py", installed_scripts / "beyin_v3_cli.py")
        entry = self.vault / "beyin.py"
        shutil.copyfile(source / "scripts" / "beyin_entry.py", entry)
        scratch = root / "temp"
        scratch.mkdir()
        payload = {
            "card": {"body": "Müşteri notu"},
            "threads": [{"heading": "Aktif İşler", "next_step": "Şifreyi yenile"}],
            "learn": [{"candidate": "İstanbul ılık"}],
            "receipt": {"event_id": "dry", "summary": "deneme", "refs": ["knowledge/test.md"]},
            "chat_trace": "Sohbet özeti",
        }

        def snapshot():
            entries = {}
            for path in [root, *root.rglob("*")]:
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                entries[path.relative_to(root)] = (stat.st_size, stat.st_mtime_ns, digest)
            return entries

        empty_state = root / "Boş durum"
        empty_state.mkdir()
        for state in (root / "missing", empty_state, self.state):
            with self.subTest(state=state.name):
                (self.vault / ".beyin-runtime.json").write_text(
                    json.dumps({"state": str(state)}), encoding="utf-8", newline="\n")
                for command in ([str(entry), "--json"], [
                    str(installed_scripts / "beyin_v3_cli.py"), "--vault", str(self.vault), "--state", str(state)
                ]):
                    with self.subTest(entry=command[0]):
                        before = snapshot()
                        proc = subprocess.run(
                            [sys.executable, *command, "checkpoint", "--dry-run"], cwd=self.vault,
                            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                            env=inherited_env(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch)),
                            timeout=30,
                        )
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        steps = json.loads(proc.stdout)["steps"]
                        self.assertTrue(all(steps[key] == "planned" for key in payload))
                        after = snapshot()
                        changed = {str(path) for path in before.keys() | after.keys()
                                   if before.get(path) != after.get(path)}
                        self.assertEqual(changed, set())
                        self.assertEqual(after, before)

if __name__ == "__main__":
    unittest.main()
