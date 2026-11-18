# -*- coding: utf-8 -*-
import unittest
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "template" / ".claude" / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import beyin_v3_usage
import beyin_v3_hook

class TestBeyinV3UsageFeedback(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_path = Path(self.test_dir) / "state"
        self.state_path.mkdir(parents=True, exist_ok=True)
        self.vault_path = Path(self.test_dir) / "vault"
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_injected_privacy_and_sensitivity(self):
        records = [
            {"id": "note1", "source": "knowledge/note1.md", "visibility": "public"},
            {"id": "note2", "source": "knowledge/secret.env", "visibility": "public"}, # sensitive source
            {"id": "note3", "source": "knowledge/note3.md", "visibility": "private"}, # private visibility
            {"id": "note4", "source": "knowledge/note4.md", "trusted": False}, # untrusted
        ]
        beyin_v3_usage.record_injected(self.state_path, records)

        data = beyin_v3_usage._load_usage_data(self.state_path)
        rec = data["records"]
        self.assertIn("note1", rec)
        self.assertNotIn("note2", rec)
        self.assertNotIn("note3", rec)
        self.assertNotIn("note4", rec)

    def test_dead_weight_count_summarize_window(self):
        # Inject note1 10 times with 0 citations -> dead weight
        for _ in range(10):
            beyin_v3_usage.record_injected(self.state_path, [{"id": "note1", "source": "note1.md"}])

        summary = beyin_v3_usage.summarize_usage(self.state_path, threshold=10, window_days=30)
        self.assertEqual(summary["dead_weight_count"], 1)

        # Test old injection outside 30-day window
        old_time = "2020-01-01T00:00:00+00:00"
        data = beyin_v3_usage._load_usage_data(self.state_path)
        data["records"]["note1"]["last_injected"] = old_time
        beyin_v3_usage._save_usage_data(self.state_path, data)

        summary_old = beyin_v3_usage.summarize_usage(self.state_path, threshold=10, window_days=30)
        self.assertEqual(summary_old["dead_weight_count"], 0)

    def test_feedback_hint_word_boundaries(self):
        beyin_v3_usage.record_injected(self.state_path, [{"id": "note1", "source": "knowledge/note1.md"}])

        # "doktor" contains "ok", but should NOT trigger hint because it's not whole word cue
        hint_doktor = beyin_v3_usage.feedback_hint(self.state_path, "doktor bey")
        self.assertFalse(hint_doktor)

        # Exact whole word cue "ok" should trigger
        hint_ok = beyin_v3_usage.feedback_hint(self.state_path, "ok")
        self.assertTrue(hint_ok)
        self.assertIn("knowledge/note1.md", hint_ok)

    def test_hook_feedback_hint_integration(self):
        beyin_v3_usage.record_injected(self.state_path, [{"id": "note1", "source": "knowledge/note1.md"}])

        # Enable chat-trace so hook hints check passes
        (self.state_path / "chat-trace.json").write_text("{}", encoding="utf-8")

        payload = {"prompt": "yanlış"}
        # We can test that feedback_hint is invoked correctly via helper or hook function
        hint = beyin_v3_usage.feedback_hint(self.state_path, beyin_v3_hook.prompt_text(payload))
        self.assertTrue(hint)
        self.assertIn("knowledge/note1.md", hint)

    def test_cli_feedback_integration(self):
        script_path = Path(__file__).parent.parent / "scripts" / "beyin_v3.py"
        cmd = [sys.executable, str(script_path), "--vault", str(self.vault_path), "--state", str(self.state_path), "feedback", "note1", "stale"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("stale", res.stdout)

if __name__ == "__main__":
    unittest.main()
