# -*- coding: utf-8 -*-
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "template" / ".claude" / "scripts"))
import beyin_v3_usage
import beyin_v3_hook

from tests.v3_package_helpers import inherited_env

class TestBeyinV3UsageFeedback(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_path = Path(self.test_dir) / "state"
        self.state_path.mkdir(parents=True, exist_ok=True)
        self.vault_path = Path(self.test_dir) / "vault"
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def rows(self):
        with closing(sqlite3.connect(self.state_path / beyin_v3_usage.DATABASE)) as db:
            return {row[0]: row[1:] for row in db.execute("SELECT id, injected, cited, ok, wrong, stale FROM notes")}

    def test_record_injected_privacy_and_sensitivity(self):
        records = [
            {"id": "note1", "source": "knowledge/note1.md", "visibility": "public", "text": "SYNTHETIC_NOTE_BODY"},
            {"id": "note2", "source": "knowledge/secret.env", "visibility": "public"},
            {"id": "note3", "source": "knowledge/note3.md", "visibility": "private"},
            {"id": "note4", "source": "knowledge/note4.md", "trusted": False},
            {"id": "note5", "source": "knowledge/Şifre/Müşteri.md"},
        ]
        for turn in ("one", "two", "one"):
            beyin_v3_usage.record_injected(self.state_path, records, session="session", turn=turn)
        self.assertEqual(self.rows(), {"note1": (2, 0, 0, 0, 0)})
        self.assertNotIn(b"SYNTHETIC_NOTE_BODY", (self.state_path / beyin_v3_usage.DATABASE).read_bytes())

    def test_dead_weight_count_summarize_window(self):
        now = datetime.now(timezone.utc)
        note = [{"id": "note1", "source": "note1.md"}]
        for turn in range(9):
            beyin_v3_usage.record_injected(self.state_path, note, session="session", turn=str(turn),
                                           now=now - timedelta(days=31))
        beyin_v3_usage.record_injected(self.state_path, note, session="session", turn="latest", now=now)
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path, now=now)["dead_weight_count"], 0)
        for turn in range(9):
            beyin_v3_usage.record_injected(self.state_path, note, session="session", turn=f"new-{turn}", now=now)
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path, now=now)["dead_weight_count"], 1)
        self.assertEqual(beyin_v3_usage.summarize_usage(
            self.state_path, now=now + timedelta(days=31))["dead_weight_count"], 0)

    def test_feedback_hint_word_boundaries(self):
        beyin_v3_usage.record_injected(self.state_path, [{"id": "note1", "source": "knowledge/note1.md"}],
                                       session="session", turn="one")
        for query in ("doktor bey", "", "   ", "yanlışlık"):
            self.assertFalse(beyin_v3_usage.feedback_hint(self.state_path, query, session="session"))
        for query in ("ok", "YANLIŞ", "ESKİ", "DOĞRU"):
            self.assertIn("knowledge/note1.md", beyin_v3_usage.feedback_hint(self.state_path, query, session="session"))

    def cli(self, *args, **env):
        script = Path(__file__).resolve().parents[1] / "scripts/beyin_v3.py"
        return subprocess.run(
            [sys.executable, str(script), "--vault", str(self.vault_path), "--state", str(self.state_path), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            env=inherited_env(BEYIN_UPDATES_OFF="1", PYTHONIOENCODING="utf-8", **{"CODEX_THREAD_ID": "", **env}),
        )

    def hook(self, session, event_id, prompt):
        script = Path(beyin_v3_hook.__file__)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": session,
                   "event_id": event_id, "prompt": prompt}
        result = subprocess.run(
            [sys.executable, str(script), "--vault", str(self.vault_path), "--state", str(self.state_path),
             "--harness", "claude"],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=60,
            env=inherited_env(BEYIN_V3_NO_SPAWN="1", BEYIN_UPDATES_OFF="1", PYTHONIOENCODING="utf-8"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")

    def note(self, source, note_id, text, visibility="public"):
        path = self.vault_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nid: {note_id}\nvisibility: {visibility}\n---\n{text}\n",
                        encoding="utf-8", newline="\n")

    def snapshot(self):
        return {p.relative_to(self.state_path): (p.stat().st_mtime_ns, p.read_bytes())
                for p in self.state_path.rglob("*") if p.is_file()}

    def test_interleaved_sessions(self):
        artemis = "knowledge/Arşiv/İstanbul Rehberi.md"
        zephyr = "knowledge/Zephyr Guide.md"
        self.note(artemis, "artemis", "Artemis calibration owner: synthetic note body A")
        self.note(zephyr, "zephyr", "Zephyr propulsion engineer: synthetic note body B")
        self.assertEqual(self.cli("sync").returncode, 0)
        self.assertIn("artemis", self.hook("session-a", "a1", "Artemis calibration owner"))
        self.assertIn("zephyr", self.hook("session-b", "b1", "Zephyr propulsion engineer"))

        a_hint = self.hook("session-a", "a2", "yanlış")
        b_hint = self.hook("session-b", "b2", "ESKİ")
        self.assertIn(artemis, a_hint)
        self.assertNotIn(zephyr, a_hint)
        self.assertIn(zephyr, b_hint)
        self.assertNotIn(artemis, b_hint)

        before = self.snapshot()
        for target, options, env in (
            (zephyr, (), {"CLAUDE_CODE_SESSION_ID": "session-a"}),
            ("arbitrary prompt", (), {}),
            ("", (), {"CLAUDE_CODE_SESSION_ID": "session-a"}),
            (artemis, ("--session", ""), {"CLAUDE_CODE_SESSION_ID": "session-a"}),
            (artemis, ("--session", "missing"), {"CLAUDE_CODE_SESSION_ID": "session-a"}),
            (artemis, (), {"CLAUDE_CODE_SESSION_ID": "session-a", "PI_SESSION_ID": "session-b"}),
        ):
            with self.subTest(target=target, options=options, env=env):
                wrong_session = self.cli("feedback", target, "stale", *options, **env)
                self.assertNotEqual(wrong_session.returncode, 0)
                self.assertEqual(self.snapshot(), before)
        a = self.cli("feedback", artemis.replace("/", "\\"), "wrong", CLAUDE_CODE_SESSION_ID="session-a")
        b_key = b_hint.split("--session ")[1].split()[0]
        b = self.cli("feedback", zephyr, "stale", "--session", b_key, CLAUDE_CODE_SESSION_ID="session-a")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(json.loads(a.stdout)["target"], "artemis")
        self.assertEqual(json.loads(b.stdout)["target"], "zephyr")
        self.assertEqual(self.rows(), {"artemis": (1, 0, 0, 1, 0), "zephyr": (1, 0, 0, 0, 1)})
        self.assertNotIn("[Hafıza geri bildirimi]", self.hook("session-a", "a3", ""))
        self.assertNotIn("[Hafıza geri bildirimi]", self.hook("session-a", "a4", "yanlış"))

    def test_unresolved_session_fails_without_writing(self):
        self.state_path.rmdir()
        result = self.cli("feedback", "SYNTHETIC_PROMPT_TEXT_NOT_A_NOTE", "wrong")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--session", result.stderr)
        self.assertFalse(self.state_path.exists())
        result = self.cli("feedback", "note1", "wrong", "--session", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oturum", result.stderr)
        self.assertFalse(self.state_path.exists())

    def test_citation_privacy_and_replay(self):
        normal = "knowledge/Arşiv/İstanbul Rehberi.md"
        private = "knowledge/Özel Not.md"
        sensitive = "knowledge/Şifre/Deneme.md"
        self.note(normal, "artemis", "Artemis calibration owner: synthetic note body")
        self.note(private, "private", "Synthetic private body", "private")
        self.note(sensitive, "sensitive", "Synthetic sensitive body")
        receipt = ("receipt", "--harness", "claude", "--event-id", "repeat", "--summary", "Fixture",
                   "--ref", normal, "--ref", normal, "--ref", private, "--ref", sensitive)
        for _ in range(2):
            result = self.cli(*receipt)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path)["tracked"], 1)
        self.assertEqual(self.rows(), {"artemis": (0, 1, 0, 0, 0)})

    def test_usage_doctor_reads_without_writing_and_hook_fails_open_under_lock(self):
        self.note("knowledge/Artemis.md", "artemis", "Artemis calibration owner: synthetic note body")
        self.assertEqual(self.cli("sync").returncode, 0)
        doctor = self.cli("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)["note_usage"]["tracked"], 0)
        path = self.state_path / beyin_v3_usage.DATABASE
        self.assertFalse(path.exists())
        self.assertIn("artemis", self.hook("session-a", "a1", "Artemis calibration owner"))
        before = self.snapshot()
        for _ in range(2):
            doctor = self.cli("doctor")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertEqual(json.loads(doctor.stdout)["note_usage"]["tracked"], 1)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN EXCLUSIVE")
            self.assertIn("artemis", self.hook("session-a", "a2", "Artemis calibration owner"))
        self.assertEqual(self.rows(), {"artemis": (1, 0, 0, 0, 0)})
        self.assertFalse((self.state_path / "hook-error.json").exists())


if __name__ == "__main__":
    unittest.main()
