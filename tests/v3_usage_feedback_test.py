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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "template" / ".claude" / "scripts"))
import beyin_v3_usage
import beyin_v3_hook
from beyin_v3_checkpoint import run_checkpoint
from beyin_v3_sync import SyncEngine

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
            {"id": "note1_int", "source": "knowledge/note1_internal.md", "visibility": "internal", "text": "SYNTHETIC_INTERNAL_BODY"},
            {"id": "note2", "source": "knowledge/secret.env", "visibility": "public"},
            {"id": "note3", "source": "knowledge/note3.md", "visibility": "private"},
            {"id": "note4", "source": "knowledge/note4.md", "trusted": False},
            {"id": "note5", "source": "knowledge/Şifre/Müşteri.md"},
            {"id": "note6", "source": "knowledge/note6.md", "visibility": "secret"},
            {"id": "note7", "source": "knowledge/note7.md", "visibility": "untrusted"},
        ]
        for turn in ("one", "two", "one"):
            beyin_v3_usage.record_injected(self.state_path, records, session="session", turn=turn)
        self.assertEqual(self.rows(), {"note1": (2, 0, 0, 0, 0), "note1_int": (2, 0, 0, 0, 0)})
        raw_db = (self.state_path / beyin_v3_usage.DATABASE).read_bytes()
        self.assertNotIn(b"SYNTHETIC_NOTE_BODY", raw_db)
        self.assertNotIn(b"SYNTHETIC_INTERNAL_BODY", raw_db)

    def test_unknown_or_malformed_visibility_stays_out_of_counters(self):
        records = [
            {"id": "implicit", "source": "knowledge/implicit.md"},
            {"id": "shared", "source": "knowledge/shared.md", "visibility": "shared"},
            {"id": "upper", "source": "knowledge/upper.md", "visibility": "Private"},
            {"id": "upper_internal", "source": "knowledge/upper_internal.md", "visibility": "INTERNAL"},
            {"id": "empty", "source": "knowledge/empty.md", "visibility": None},
            {"id": "listed", "source": "knowledge/listed.md", "visibility": ["internal"]},
        ]
        beyin_v3_usage.record_injected(self.state_path, records, session="session", turn="one")
        self.assertEqual(self.rows(), {"implicit": (1, 0, 0, 0, 0)})

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

    def hook(self, session, event_id, prompt, event="UserPromptSubmit"):
        script = Path(beyin_v3_hook.__file__)
        payload = {"hook_event_name": event, "session_id": session,
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

    def checkpoint_receipt(self, receipt, *flags):
        request = Path(self.test_dir) / "checkpoint.json"
        request.write_text(json.dumps({"receipt": receipt}), encoding="utf-8")
        result = self.cli("checkpoint", "--file", str(request), "--harness", "claude", *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["steps"]["receipt"]

    def test_checkpoint_cites_eligible_notes_once_across_receipt_paths(self):
        public, private = "knowledge/Artemis.md", "knowledge/Veiled.md"
        self.note(public, "artemis", "Synthetic public text")
        self.note(private, "veiled", "Synthetic private text", "private")
        for turn in range(10):
            beyin_v3_usage.record_injected(self.state_path, [{"id": "artemis", "source": public}],
                                           session="citation-session", turn=str(turn))
        unused = {"artemis": (10, 0, 0, 0, 0)}
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path)["dead_weight_count"], 1)
        payload = {"event_id": "checkpoint-citation", "summary": "Synthetic result",
                   "refs": [public, public, private]}
        receipts = self.vault_path / "receipts"

        self.assertEqual(self.checkpoint_receipt(payload, "--dry-run"), "planned")
        self.assertEqual(self.checkpoint_receipt(dict(payload, summary=""))["status"], "error")
        self.assertFalse(list(receipts.glob("*.md")))
        self.assertEqual(self.rows(), unused)

        self.assertEqual(self.checkpoint_receipt(payload), "succeeded")
        used = {"artemis": (10, 1, 0, 0, 0)}
        self.assertEqual(self.rows(), used)
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path)["dead_weight_count"], 0)
        receipt_file, = receipts.glob("*.md")
        original = receipt_file.read_bytes()
        self.assertEqual(self.checkpoint_receipt(payload), "succeeded")
        standalone = self.cli("receipt", "--harness", "codex", "--event-id", payload["event_id"],
                              "--summary", payload["summary"],
                              "--ref", public, "--ref", public, "--ref", private)
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertEqual(json.loads(standalone.stdout)["status"], "succeeded")
        self.assertEqual(list(receipts.glob("*.md")), [receipt_file])
        self.assertEqual(receipt_file.read_bytes(), original)
        self.assertEqual(self.rows(), used)
        self.assertEqual(self.checkpoint_receipt(dict(payload, summary="conflicting content"))["status"], "error")
        self.assertEqual(self.rows(), used)

    def test_locked_usage_keeps_checkpoint_receipt_durable_and_replay_recovers(self):
        source = "knowledge/Artemis.md"
        self.note(source, "artemis", "Synthetic public text")
        beyin_v3_usage.record_injected(self.state_path, [{"id": "artemis", "source": source}],
                                       session="citation-session", turn="1")
        payload = {"event_id": "locked-citation", "summary": "Synthetic result", "refs": [source]}
        with closing(sqlite3.connect(self.state_path / beyin_v3_usage.DATABASE)) as db:
            db.execute("BEGIN EXCLUSIVE")
            try:
                self.assertEqual(self.checkpoint_receipt(payload), "succeeded")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM citations").fetchone()[0], 0)
            finally:
                db.rollback()

        receipt_file, = (self.vault_path / "receipts").glob("*.md")
        original = receipt_file.read_bytes()
        engine = SyncEngine(self.vault_path, self.state_path)
        with engine.store._connect() as db:
            row = db.execute("SELECT payload FROM receipts WHERE id=?", (payload["event_id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["summary"], payload["summary"])
        self.assertEqual(self.rows(), {"artemis": (1, 0, 0, 0, 0)})
        for _ in range(2):
            self.assertEqual(self.checkpoint_receipt(payload), "succeeded")
            self.assertEqual(self.rows(), {"artemis": (1, 1, 0, 0, 0)})
            self.assertEqual(receipt_file.read_bytes(), original)

    def test_failed_receipt_submission_does_not_count_a_citation(self):
        source = "knowledge/Artemis.md"
        self.note(source, "artemis", "Synthetic public text")
        engine = SyncEngine(self.vault_path, self.state_path)
        req = {"receipt": {"event_id": "failed-submission", "summary": "Synthetic result", "refs": [source]}}
        with patch.object(engine.store, "submit_receipt", side_effect=OSError("fixture submission failed")) as submit:
            result = run_checkpoint(engine, self.vault_path, self.state_path, req, harness="claude")
        submit.assert_called_once()
        self.assertEqual(result["steps"]["receipt"]["status"], "error")
        usage_db = self.state_path / beyin_v3_usage.DATABASE
        self.assertFalse(usage_db.exists())
        self.assertEqual(self.checkpoint_receipt(req["receipt"]), "succeeded")
        self.assertTrue(usage_db.is_file(), "successful replay should record the citation")
        self.assertEqual(self.rows(), {"artemis": (0, 1, 0, 0, 0)})

    def test_receipt_case_alias_counts_the_indexed_note(self):
        source = "knowledge/İstanbul Guide.md"
        alias = "knowledge/İstanbul guide.md"
        self.note(source, "artemis", "Artemis calibration owner: synthetic note body")
        if not (self.vault_path / alias).is_file():
            self.skipTest("requires a case-insensitive filesystem")
        result = self.cli("receipt", "--harness", "claude", "--event-id", "alias", "--summary", "Fixture",
                          "--ref", alias)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path)["tracked"], 1)
        self.assertEqual(self.rows(), {"artemis": (0, 1, 0, 0, 0)})

    def test_opening_counts_the_related_note_it_delivers(self):
        source = "knowledge/Artemis.md"
        self.note(source, "artemis", "Artemis calibration owner: synthetic note body")
        self.assertEqual(self.cli("sync").returncode, 0)
        self.assertIn(source, self.hook("session-a", "a1", "Artemis calibration owner", event="SessionStart"))
        self.assertEqual(beyin_v3_usage.summarize_usage(self.state_path)["tracked"], 1)
        self.assertIn(source, self.hook("session-a", "a2", "yanlış"))

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

    def test_default_internal_visibility_tracks_injection_citation_and_feedback(self):
        internal_explicit = "knowledge/InternalGuide.md"
        plain_default = "knowledge/PlainDefault.md"
        private_note = "knowledge/PrivateSecret.md"
        self.note(internal_explicit, "int-explicit", "Helios calibration protocol explicit internal note", visibility="internal")
        (self.vault_path / plain_default).parent.mkdir(parents=True, exist_ok=True)
        (self.vault_path / plain_default).write_text("# Plain Default\nVega telemetry calibration without frontmatter.\n",
                                                    encoding="utf-8", newline="\n")
        self.note(private_note, "priv-note", "Helios private calibration body", visibility="private")
        self.assertEqual(self.cli("sync").returncode, 0)

        self.assertIn("Helios calibration protocol", self.hook("sess-int", "t1", "Helios calibration protocol"))
        hint = self.hook("sess-int", "t2", "yanlış")
        self.assertIn(internal_explicit, hint)
        self.assertNotIn(private_note, hint)

        fb = self.cli("feedback", internal_explicit, "ok", CLAUDE_CODE_SESSION_ID="sess-int")
        self.assertEqual(fb.returncode, 0, fb.stderr)
        self.assertEqual(json.loads(fb.stdout)["target"], "int-explicit")

        self.assertIn("Vega telemetry calibration", self.hook("sess-plain", "p1", "Vega telemetry calibration"))
        rc = self.cli("receipt", "--harness", "claude", "--event-id", "int-cite-1",
                      "--summary", "Used internal and plain default notes",
                      "--ref", internal_explicit, "--ref", plain_default, "--ref", private_note)
        self.assertEqual(rc.returncode, 0, rc.stderr)

        doctor = json.loads(self.cli("doctor").stdout)["note_usage"]
        self.assertEqual(doctor["tracked"], 2)
        self.assertEqual(doctor["feedback_totals"]["ok"], 1)
        rows = self.rows()
        self.assertEqual(rows["int-explicit"], (1, 1, 1, 0, 0))
        self.assertNotIn("priv-note", rows)
        plain_rows = [vals for nid, vals in rows.items() if nid != "int-explicit"]
        self.assertEqual(plain_rows, [(1, 1, 0, 0, 0)])


if __name__ == "__main__":
    unittest.main()
