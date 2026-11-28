"""Unit tests for unified session checkpoint handoff (Issue #767)."""
import unittest
import tempfile
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "template" / ".claude" / "scripts"))

from beyin_v3_checkpoint import run_checkpoint
from beyin_v3_learning import stage_candidate
from beyin_v3_secrets import health
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
        self.assertIn("Eski kart gövdesi", last_session_text)

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

    def test_card_updates_only_its_session_with_shared_prefix(self):
        card = self.comp / "Last-Session.md"
        card.write_text("# Son oturum\n", encoding="utf-8", newline="\n")
        for session, label, body in (
            ("abcdabcd1111111111111111", "İstanbul", "birinci gövde"),
            ("abcdabcd2222222222222222", "ılık", "ikinci gövde"),
            ("abcdabcd1111111111111111", "Arşiv", "güncel gövde"),
        ):
            request = {"card": {"label": label, "body": body}}
            result = run_checkpoint(self.sync, self.vault, self.state, request, session=session)
            self.assertEqual(result["steps"]["card"], "written")
        content = card.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# Son oturum\n"))
        self.assertIn("ikinci gövde", content)
        self.assertIn("güncel gövde", content)
        self.assertNotIn("birinci gövde", content)
        result = run_checkpoint(self.sync, self.vault, self.state, dict(request, now="2026-11-23T10:12:00"),
                                session=session)
        self.assertEqual(result["steps"]["card"], "unchanged")
        self.assertEqual(card.read_text(encoding="utf-8"), content)

    def test_card_heading_uses_local_wall_time(self):
        # Hand-written cards and the hook's now= use local time; a card stamped with an
        # aware time in another offset (or the UTC default) must not read hours behind them.
        local = datetime(2026, 11, 23, 12, 0).astimezone().utcoffset()
        other = timezone(local + timedelta(hours=-3 if local >= timedelta(0) else 3))
        stamp = datetime(2026, 11, 23, 12, 0, tzinfo=other)
        request = {"card": {"label": "saat", "body": "gövde"}, "now": stamp.isoformat()}
        result = run_checkpoint(self.sync, self.vault, self.state, request, session="saat1234")
        self.assertEqual(result["steps"]["card"], "written")
        content = (self.comp / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn(f"## {stamp.astimezone():%Y-%m-%d %H:%M} · saat · ", content)
        self.assertNotIn("## 2026-11-23 12:00 · saat", content)

        utc_now = datetime(2026, 11, 23, 15, 35, tzinfo=timezone.utc)
        with patch("beyin_v3_checkpoint.datetime", wraps=datetime) as clock:
            clock.now.return_value = utc_now
            run_checkpoint(self.sync, self.vault, self.state,
                           {"card": {"label": "varsayılan", "body": "gövde"}}, session="vars1234")
        content = (self.comp / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn(f"## {utc_now.astimezone():%Y-%m-%d %H:%M} · varsayılan · ", content)

    def test_automatic_compaction(self):
        card = self.comp / "Last-Session.md"
        card.write_text("# Last-Session\n\n## 2026-11-02 10:00 · yeni\n" + "Güncel kart.\n" +
                        "\n## 2026-11-01 10:00 · eski\n" + "Eski kayıt.\n" * 320,
                        encoding="utf-8", newline="\n")
        before = card.read_bytes()

        req = {
            "card": {"label": "compaction test", "body": "Test kompaksiyon."},
        }
        req["now"] = "2026-11-21T10:00:00"
        preview = run_checkpoint(None, self.vault, self.state, req, dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "planned")
        self.assertEqual(card.read_bytes(), before)
        res = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session="session-compaction")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["steps"]["compact"], "compacted")
        self.assertIn("Eski kayıt.", (self.comp / "Arşiv" / "Last-Session-2026-11.md").read_text(encoding="utf-8"))

    def test_card_retry_recognizes_a_compacted_card(self):
        limits = self.state / "companion-limits.json"
        limits.write_text('{"Last-Session.md": 0}', encoding="utf-8")
        old = {"card": {"body": "Eski kartın sonucu.\n" * 190}, "now": "2026-11-18T12:00:00"}
        run_checkpoint(self.sync, self.vault, self.state, old, session="older")
        run_checkpoint(self.sync, self.vault, self.state,
                       {"card": {"body": "Güncel kart."}, "now": "2026-11-19T12:00:00"}, session="newer")
        limits.unlink()

        retry = run_checkpoint(self.sync, self.vault, self.state, dict(old, now="2026-11-21T12:00:00"),
                               session="older")
        self.assertEqual(retry["steps"]["compact"], "compacted")
        self.assertEqual(retry["steps"]["card"], "unchanged")
        content = (self.comp / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn("Güncel kart.", content)
        self.assertNotIn("Eski kartın sonucu.", content)

    def test_disabled_compaction_limit_uses_characters_and_stays_off(self):
        limits = self.state / "companion-limits.json"
        limits.write_text(json.dumps({"Last-Session.md": 0, "Threads.md": 1000}),
                          encoding="utf-8", newline="\n")
        (self.comp / "Last-Session.md").write_text("a" * 5000, encoding="utf-8")
        (self.comp / "Threads.md").write_text("Ş" * 900, encoding="utf-8")
        result = run_checkpoint(self.sync, self.vault, self.state, {})
        self.assertEqual(result["steps"]["compact"], "skipped_within_limit")
        self.assertFalse((self.comp / ".beyin-compact.lock").exists())

    def test_invalid_limits_report_partial_without_archiving(self):
        (self.state / "companion-limits.json").write_text('{"Last-Session.md": "yanlış"}', encoding="utf-8")
        result = run_checkpoint(self.sync, self.vault, self.state, {"card": {"body": "İş tamamlandı"}})
        self.assertEqual(result["steps"]["compact"], "needs_attention")
        self.assertEqual(result["steps"]["card"], "written")
        self.assertEqual(result["status"], "partial")
        self.assertFalse((self.comp / "Arşiv").exists())

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

    def test_retry_after_interrupted_candidate_does_not_duplicate_writes(self):
        request = {
            "threads": [{"heading": "Aktif İşler", "next_step": "Şifreyi yenile"}],
            "learn": [{"candidate": "İstanbul ılık", "source": "knowledge/test.md"}],
        }

        def write_then_fail(*args, **kwargs):
            stage_candidate(*args, **kwargs)
            raise OSError("injected interruption after candidate append")

        with patch("beyin_v3_checkpoint.stage_candidate", side_effect=write_then_fail):
            result = run_checkpoint(self.sync, self.vault, self.state, request, session="checkpoint-retry")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["steps"]["threads"], "written")
        self.assertEqual(result["steps"]["learn"]["status"], "error")
        request["threads"][0]["next_step"] = " Şifreyi yenile "
        request["learn"][0]["candidate"] = " İstanbul ılık "
        retry = run_checkpoint(self.sync, self.vault, self.state, request, session="checkpoint-retry")
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["steps"]["learn"], {"status": "unchanged", "count": 0})
        threads = (self.comp / "Threads.md").read_text(encoding="utf-8")
        records = [json.loads(line) for line in
                   (self.state / "learning-state.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((threads.count("Şifreyi yenile"), len(records)), (1, 1))
        self.assertEqual(records[0]["candidate"], "İstanbul ılık")

    def test_redacted_candidate_retry_counts_only_the_stored_entry(self):
        request = {"learn": [{"candidate": "OPENAI_API_KEY=sk-" + "x" * 32}]}
        first = run_checkpoint(self.sync, self.vault, self.state, request, session="redaction")
        self.assertEqual(first["steps"]["learn"], {"status": "recorded", "count": 1})
        self.assertEqual(health(self.state)["total"], 1)
        retry = run_checkpoint(self.sync, self.vault, self.state, request, session="redaction")
        self.assertEqual(health(self.state)["total"], 1)
        self.assertEqual(retry["steps"]["learn"], {"status": "unchanged", "count": 0})

    def test_retry_repairs_threads_projection_after_note_replacement(self):
        request = {"threads": [{"heading": "Aktif İşler", "next_step": "İndeks onarılsın"}]}
        with patch.object(self.sync, "sync", side_effect=OSError("injected failure during readback")):
            interrupted = run_checkpoint(self.sync, self.vault, self.state, request, session="repair")
        self.assertEqual(interrupted["steps"]["threads"]["status"], "error")
        note = self.comp / "Threads.md"
        committed = note.read_bytes()
        self.assertIn("İndeks onarılsın".encode("utf-8"), committed)
        retry = run_checkpoint(self.sync, self.vault, self.state, request, session="repair")
        self.assertEqual(retry["steps"]["threads"], "unchanged")
        self.assertEqual(note.read_bytes(), committed)
        with self.sync.store._connect() as db:
            payload, = db.execute(
                "SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id WHERE m.source=?",
                (note.relative_to(self.vault).as_posix(),),
            ).fetchone()
        self.assertEqual(json.loads(payload)["source_sha256"], hashlib.sha256(committed).hexdigest())

    def test_threads_matches_exact_headings_and_applies_a_batch(self):
        threads = self.comp / "Threads.md"
        initial = "# Threads\r\n\r\n### Müşteri Arşivi Eski\r\n- Eski\r\n"
        threads.write_bytes(initial.encode("utf-8"))
        request = {"threads": [
            {"heading": "Müşteri Arşivi", "next_step": "İlk iş"},
            {"heading": "Müşteri Arşivi", "next_step": "Ilık"},
            {"old": "- Ilık", "new": "- ılık"},
        ]}
        first = run_checkpoint(self.sync, self.vault, self.state, request, session="threads")
        self.assertEqual(first["steps"]["threads"], "written")
        written = threads.read_bytes()
        visible = b"".join(line for line in written.splitlines(keepends=True)
                           if not line.startswith(b"<!-- beyin-checkpoint-thread:"))
        self.assertTrue(visible.startswith(initial.encode("utf-8")))
        self.assertIn("### Müşteri Arşivi\r\n- İlk iş\r\n".encode("utf-8"), written)
        self.assertEqual(written.count("- ılık\r\n".encode("utf-8")), 1)
        self.assertNotIn(b"\n", written.replace(b"\r\n", b""))
        second = run_checkpoint(self.sync, self.vault, self.state, request, session="threads")
        self.assertEqual(second["steps"]["threads"], "unchanged")
        self.assertEqual(threads.read_bytes(), written)

    def test_threads_can_create_missing_note(self):
        (self.comp / "Threads.md").unlink()
        result = run_checkpoint(self.sync, self.vault, self.state, {
            "threads": [{"heading": "Müşteri Arşivi", "next_step": "İlk iş"}],
        }, session="threads")
        self.assertEqual(result["steps"]["threads"], "written")
        self.assertIn("### Müşteri Arşivi\n- İlk iş\n",
                      (self.comp / "Threads.md").read_text(encoding="utf-8"))

    def test_checkpoint_preserves_fenced_heading_examples(self):
        card = self.comp / "Last-Session.md"
        threads = self.comp / "Threads.md"
        example = "```md\n## Örnek kart\n### Aktif İşler\n- örnek\n```\n"
        card.write_text("# Son oturum\n\n" + example, encoding="utf-8", newline="\n")
        threads.write_text("# Threads\n\n" + example + "\n### Aktif İşler\n- gerçek\n",
                           encoding="utf-8", newline="\n")
        result = run_checkpoint(self.sync, self.vault, self.state, {
            "card": {"body": "Güncel kart."},
            "threads": [{"heading": "Aktif İşler", "next_step": "Güncel iş."}],
        }, session="example-session")
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(example, card.read_text(encoding="utf-8"))
        self.assertIn(example + "\n### Aktif İşler\n- gerçek\n- Güncel iş.\n",
                      threads.read_text(encoding="utf-8"))

    def test_threads_retry_keeps_replacement_key_after_closed_threads_are_compacted(self):
        limits = self.state / "companion-limits.json"
        limits.write_text('{"Threads.md": 0}', encoding="utf-8")
        threads = self.comp / "Threads.md"
        threads.write_text("# Threads\n\n## Active Threads\n### Açık iş\n- önceki adım\n"
                           "\n## Closed Threads\n" + "- eski not\n" * 1000,
                           encoding="utf-8", newline="\n")
        request = {"threads": [{"old": "- önceki adım", "new": "- güncel adım"}]}
        first = run_checkpoint(self.sync, self.vault, self.state, request, session="archive")
        self.assertEqual(first["steps"]["threads"], "written")
        limits.unlink()
        retry = run_checkpoint(self.sync, self.vault, self.state, request, session="archive")
        self.assertEqual(retry["steps"]["compact"], "compacted")
        self.assertEqual(retry["steps"]["threads"], "unchanged")

    def test_trace_retry_across_month_boundary(self):
        beyin_v3_chat_trace.configure(self.state, True)
        request = {"chat_trace": "Sohbet özeti"}
        for day in (datetime(2026, 10, 31, 12), datetime(2026, 11, 1, 12)):
            with patch("beyin_v3_chat_trace.datetime") as clock:
                clock.now.return_value = day
                result = run_checkpoint(self.sync, self.vault, self.state, request,
                                        session="123456789012345678901234")
            self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"]["chat_trace"], "unchanged")
        traces = sorted((self.vault / "knowledge" / "conversations").glob("*.md"))
        self.assertEqual([path.name for path in traces], ["2026-10.md"])
        self.assertEqual(traces[0].read_text(encoding="utf-8").count("Sohbet özeti"), 1)

    def test_learning_rejects_private_and_sensitive_sources_before_sync(self):
        sources = {
            "Müşteri Kayıtları/not.md": "# Dizin adı korunmalı",
            "knowledge/özel not.md": "---\nvisibility: private\n---\n# Not korunmalı",
        }
        for source, body in sources.items():
            with self.subTest(source=source):
                path = self.vault / source
                path.parent.mkdir(exist_ok=True)
                path.write_text(body, encoding="utf-8", newline="\n")
                result = run_checkpoint(self.sync, self.vault, self.state, {
                    "learn": [{"candidate": "KORUNAN_İÇERİK", "source": source}],
                })
                self.assertEqual(result["steps"]["learn"]["status"], "error")
                self.assertNotIn(source, json.dumps(result, ensure_ascii=False))
                self.assertNotIn("KORUNAN_İÇERİK", json.dumps(result, ensure_ascii=False))
                self.assertFalse((self.state / "learning-state.jsonl").exists())

    def test_quickstart_checkpoint_example(self):
        quickstart = Path(__file__).resolve().parents[1] / "docs" / "v3" / "QUICKSTART.md"
        section = quickstart.read_text(encoding="utf-8").split("### Oturum kontrol noktası", 1)[1]
        payload = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
        (self.vault / payload["receipt"]["refs"][0]).write_text("# Kaynak\n", encoding="utf-8")
        result = run_checkpoint(self.sync, self.vault, self.state, payload,
                                harness="claude", session="123456789012345678901234")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"]["receipt"], "succeeded")

    def test_cli_checkpoint_and_human_result(self):
        req_file = Path(self.test_dir) / "checkpoint_req.json"
        req_file.write_text(json.dumps({"card": {"label": "cli", "body": "CLI testi."}}),
                            encoding="utf-8", newline="\n")

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "beyin_v3.py"
        proc = subprocess.run(
            [sys.executable, str(script_path), "--vault", str(self.vault), "--state", str(self.state),
             "checkpoint", "--file", str(req_file), "--harness", "claude"],
            capture_output=True, text=True, encoding="utf-8",
            env=inherited_env(CLAUDE_CODE_SESSION_ID="checkpoint-test-session"), timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["session"], hashlib.sha256(b"checkpoint-test-session").hexdigest()[:24])

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

    def test_post_write_compaction_when_new_card_pushes_over_limit(self):
        # Issue #952: file starts below 3,000 chars (~2,312 chars), new ~1,500-char card pushes it
        # past 3,000; checkpoint must compact older cards after writing rather than reporting
        # skipped_within_limit while leaving Last-Session.md over limit.
        card = self.comp / "Last-Session.md"
        older_a = "## 2026-11-01 10:00 · eski a · aaaaaaaa\nESKI_KART_A " + ("detay satırı. " * 78) + "\n"
        older_b = "## 2026-11-02 10:00 · eski b · bbbbbbbb\nESKI_KART_B " + ("not satırı. " * 90) + "\n"
        initial = "# Last-Session\n\n" + older_b + "\n" + older_a
        self.assertGreater(len(initial), 2200)
        self.assertLess(len(initial), 2500)
        card.write_text(initial, encoding="utf-8", newline="\n")
        before = card.read_bytes()

        new_body = "YENI_KART_952 " + ("güncel çalışma özeti. " * 68)
        self.assertGreater(len(new_body), 1400)
        req = {"card": {"label": "güncel", "body": new_body}, "now": "2026-11-21T10:00:00"}

        preview = run_checkpoint(None, self.vault, self.state, req, session="post-write-952", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "planned")
        self.assertEqual(card.read_bytes(), before)
        self.assertFalse((self.comp / "Arşiv").exists())

        res = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session="post-write-952")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "compacted")

        live = card.read_text(encoding="utf-8")
        self.assertLessEqual(len(live), 3000)
        self.assertIn("YENI_KART_952", live)
        self.assertNotIn("ESKI_KART_A", live)
        archive = (self.comp / "Arşiv" / "Last-Session-2026-11.md").read_text(encoding="utf-8")
        self.assertIn("ESKI_KART_A", archive)

        with self.sync.store._connect() as db:
            payload, = db.execute(
                "SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id WHERE m.source=?",
                (card.relative_to(self.vault).as_posix(),),
            ).fetchone()
        self.assertEqual(json.loads(payload)["source_sha256"], hashlib.sha256(card.read_bytes()).hexdigest())

        compacted_bytes = card.read_bytes()
        retry = run_checkpoint(self.sync, self.vault, self.state, req, harness="claude", session="post-write-952")
        self.assertEqual(retry["status"], "succeeded")
        self.assertEqual(retry["steps"]["card"], "unchanged")
        self.assertEqual(retry["steps"]["compact"], "skipped_within_limit")
        self.assertEqual(card.read_bytes(), compacted_bytes)

    def test_post_write_compaction_reports_needs_rewrite_when_single_card_exceeds_limit(self):
        card = self.comp / "Last-Session.md"
        card.write_text("# Last-Session\n", encoding="utf-8", newline="\n")
        before = card.read_bytes()

        req = {
            "card": {"label": "tek büyük kart", "body": "TEK_BUYUK_KART " + ("uzun içerik satırı. " * 165)},
            "now": "2026-11-21T10:00:00",
        }
        preview = run_checkpoint(None, self.vault, self.state, req, session="oversized-single", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "needs_rewrite")
        self.assertEqual(preview["status"], "partial")
        self.assertEqual(card.read_bytes(), before)
        self.assertFalse((self.comp / "Arşiv").exists())

        res = run_checkpoint(self.sync, self.vault, self.state, req, session="oversized-single")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "needs_rewrite")
        self.assertEqual(res["status"], "partial")
        self.assertIn("TEK_BUYUK_KART", card.read_text(encoding="utf-8"))
        archive = (self.comp / "Arşiv" / "Last-Session-2026-11.md").read_text(encoding="utf-8")
        self.assertIn("TEK_BUYUK_KART", archive)

    def test_post_write_compaction_reports_needs_rewrite_when_parallel_sessions_protect_all_cards(self):
        from beyin_v3_parallel import receipt_session, touch

        card = self.comp / "Last-Session.md"
        peer_session = "parallel-peer-session"
        peer_short = receipt_session(peer_session)[:8]
        now_dt = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
        touch(self.state, "claude", peer_session, now=now_dt.timestamp(), vault=self.vault)

        peer_card = (
            f"# Last-Session\n\n## 2026-11-21 09:50 · paralel · {peer_short}\n"
            "PARALEL_KART " + ("aktif oturum ayrıntısı. " * 85) + "\n"
        )
        self.assertLess(len(peer_card), 3000)
        card.write_text(peer_card, encoding="utf-8", newline="\n")

        req = {
            "card": {"label": "ikinci oturum", "body": "BENIM_KARTIM " + ("yeni oturum ayrıntısı. " * 60)},
            "now": now_dt.isoformat(),
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req, session="second-parallel-session")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "needs_rewrite")
        self.assertEqual(res["status"], "partial")
        live = card.read_text(encoding="utf-8")
        self.assertIn("PARALEL_KART", live)
        self.assertIn("BENIM_KARTIM", live)
        self.assertTrue((self.comp / "Arşiv" / "Last-Session-2026-11.md").is_file())

    def test_post_write_compaction_never_archives_freshly_written_card_with_older_timestamp(self):
        card = self.comp / "Last-Session.md"
        future_card = "## 2026-11-25 12:00 · ileride · ffffffff\nILERI_KART " + ("ileri saat notu. " * 75) + "\n"
        old_card = "## 2026-11-01 10:00 · eski · 11111111\nESKI_KART " + ("eski kayıt. " * 120) + "\n"
        initial = "# Last-Session\n\n" + future_card + "\n" + old_card
        self.assertLess(len(initial), 3000)
        card.write_text(initial, encoding="utf-8", newline="\n")

        req = {
            "card": {"label": "taze kart", "body": "TAZE_KART " + ("yeni içerik. " * 70)},
            "now": "2026-11-21T10:00:00",
        }
        res = run_checkpoint(self.sync, self.vault, self.state, req, session="fresh-older-clock")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "compacted")
        live = card.read_text(encoding="utf-8")
        self.assertLessEqual(len(live), 3000)
        self.assertIn("TAZE_KART", live)
        self.assertIn("ILERI_KART", live)
        self.assertNotIn("ESKI_KART", live)

        # When only the ahead-clock card and the freshly written card exist and together exceed
        # the limit, compaction must protect both rather than archiving the card just written.
        card.write_text("# Last-Session\n\n" + future_card, encoding="utf-8", newline="\n")
        req2 = {
            "card": {"label": "korunan taze kart", "body": "KORUNAN_TAZE_KART " + ("yeni detay. " * 165)},
            "now": "2026-11-21T10:05:00",
        }
        res2 = run_checkpoint(self.sync, self.vault, self.state, req2, session="fresh-protected-only")
        self.assertEqual(res2["steps"]["card"], "written")
        self.assertEqual(res2["steps"]["compact"], "needs_rewrite")
        self.assertEqual(res2["status"], "partial")
        live2 = card.read_text(encoding="utf-8")
        self.assertIn("KORUNAN_TAZE_KART", live2)
        self.assertIn("ILERI_KART", live2)

    def test_post_write_stays_skipped_within_limit_when_file_remains_under_limit(self):
        req = {
            "card": {"label": "kısa", "body": "Kısa oturum özeti."},
            "threads": [{"heading": "Aktif İşler", "next_step": "kısa adım"}],
            "now": "2026-11-21T10:00:00",
        }
        preview = run_checkpoint(None, self.vault, self.state, req, session="within-limit", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "skipped_within_limit")
        self.assertEqual(preview["status"], "succeeded")

        res = run_checkpoint(self.sync, self.vault, self.state, req, session="within-limit")
        self.assertEqual(res["steps"]["compact"], "skipped_within_limit")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["threads"], "written")
        self.assertEqual(res["status"], "succeeded")
        self.assertFalse((self.comp / "Arşiv").exists())

    def test_post_write_compaction_when_threads_update_pushes_over_limit(self):
        from beyin_v3_companion import companion_chars

        threads = self.comp / "Threads.md"
        active_block = "# Threads\n\n## Active Threads\n### Aktif İşler\n" + ("- aktif ayrıntı satırı burada yer alır.\n" * 115)
        closed_block = "\n## Closed Threads\n" + ("- kapanan eski iş kaydı ayrıntısı.\n" * 80)
        initial = active_block + closed_block
        self.assertGreater(len(initial), 7000)
        self.assertLess(len(initial), 7800)
        threads.write_text(initial, encoding="utf-8", newline="\n")
        before = threads.read_bytes()

        new_step = "YENI_THREAD_ADIMI " + ("uzun yeni görev açıklaması " * 40).strip()
        req = {"threads": [{"heading": "Aktif İşler", "next_step": new_step}], "now": "2026-11-21T10:00:00"}

        preview = run_checkpoint(None, self.vault, self.state, req, session="threads-952", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "planned")
        self.assertEqual(threads.read_bytes(), before)
        self.assertFalse((self.comp / "Arşiv").exists())

        res = run_checkpoint(self.sync, self.vault, self.state, req, session="threads-952")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["steps"]["threads"], "written")
        self.assertEqual(res["steps"]["compact"], "compacted")
        live = threads.read_text(encoding="utf-8")
        self.assertLessEqual(companion_chars("Threads.md", live), 8000)
        self.assertIn("YENI_THREAD_ADIMI", live)
        self.assertNotIn("kapanan eski iş kaydı", live)
        archive = (self.comp / "Arşiv" / "Threads-2026-11.md").read_text(encoding="utf-8")
        self.assertIn("kapanan eski iş kaydı", archive)

        # If Threads.md has no movable history/dated entries and the update pushes over limit,
        # checkpoint must honestly report needs_rewrite instead of skipped_within_limit.
        threads.write_text(
            "# Threads\n\n## Active Threads\n### Aktif İşler\n" + ("- aktif sabit madde satırı burada.\n" * 215),
            encoding="utf-8", newline="\n",
        )
        self.assertLess(companion_chars("Threads.md", threads.read_text(encoding="utf-8")), 8000)
        req_over = {
            "threads": [{"heading": "Aktif İşler", "next_step": "TASAN_ADIM " + ("uzun ekleme " * 80).strip()}],
            "now": "2026-11-21T10:05:00",
        }
        res_over = run_checkpoint(self.sync, self.vault, self.state, req_over, session="threads-952-over")
        self.assertEqual(res_over["steps"]["threads"], "written")
        self.assertEqual(res_over["steps"]["compact"], "needs_rewrite")
        self.assertEqual(res_over["status"], "partial")

    def test_already_oversized_single_old_card_compacts_cleanly_after_new_card(self):
        card = self.comp / "Last-Session.md"
        card.write_text(
            "# Last-Session\n\n## 2026-11-01 10:00 · eski büyük · 11111111\n"
            "ESKI_BUYUK_KART " + ("eski ayrıntı satırı. " * 165) + "\n",
            encoding="utf-8", newline="\n",
        )
        self.assertGreater(len(card.read_text(encoding="utf-8")), 3000)
        req = {"card": {"label": "yeni kısa", "body": "YENI_KISA_KART özeti."}, "now": "2026-11-21T10:00:00"}

        res = run_checkpoint(self.sync, self.vault, self.state, req, session="replace-oversized-old")
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "compacted")
        live = card.read_text(encoding="utf-8")
        self.assertLessEqual(len(live), 3000)
        self.assertIn("YENI_KISA_KART", live)
        self.assertNotIn("ESKI_BUYUK_KART", live)
        archive = (self.comp / "Arşiv" / "Last-Session-2026-11.md").read_text(encoding="utf-8")
        self.assertIn("ESKI_BUYUK_KART", archive)
        self.assertNotIn("needs_rewrite", archive)

    def test_post_write_partial_compaction_still_over_limit_reports_needs_rewrite(self):
        from beyin_v3_compact import compact as companion_compact

        with self.assertRaises(ValueError):
            companion_compact(self.vault, self.state, dry_run=False, overrides={"Last-Session.md": "x"})

        card = self.comp / "Last-Session.md"
        card.write_text(
            "# Last-Session\n\n## 2026-11-01 10:00 · eski · 11111111\nESKI_KART_MOVE " + ("eski not. " * 100) + "\n",
            encoding="utf-8", newline="\n",
        )
        self.assertLess(len(card.read_text(encoding="utf-8")), 3000)
        before = card.read_bytes()

        req = {
            "card": {"label": "dev kart", "body": "DEV_KART " + ("çok uzun kart içeriği. " * 145)},
            "now": "2026-11-21T10:00:00",
        }
        preview = run_checkpoint(None, self.vault, self.state, req, session="still-over-after-compact", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "needs_rewrite")
        self.assertEqual(preview["status"], "partial")
        self.assertEqual(card.read_bytes(), before)

        res = run_checkpoint(self.sync, self.vault, self.state, req, session="still-over-after-compact")
        self.assertEqual(res["steps"]["card"], "written")
        self.assertEqual(res["steps"]["compact"], "needs_rewrite")
        self.assertEqual(res["status"], "partial")
        live = card.read_text(encoding="utf-8")
        self.assertIn("DEV_KART", live)
        self.assertNotIn("ESKI_KART_MOVE", live)
        archive = (self.comp / "Arşiv" / "Last-Session-2026-11.md").read_text(encoding="utf-8")
        self.assertIn("ESKI_KART_MOVE", archive)
        self.assertIn("DEV_KART", archive)

    def test_dry_run_recognizes_already_archived_card_replay(self):
        limits = self.state / "companion-limits.json"
        limits.write_text('{"Last-Session.md": 0}', encoding="utf-8")
        old = {"card": {"body": "Eski arşivlik kart.\n" * 190}, "now": "2026-11-18T12:00:00"}
        run_checkpoint(self.sync, self.vault, self.state, old, session="archived-replay")
        run_checkpoint(self.sync, self.vault, self.state,
                       {"card": {"body": "Güncel kısa kart."}, "now": "2026-11-19T12:00:00"}, session="newer-short")
        limits.unlink()

        # First live retry compacts the old oversized card into Arşiv.
        first = run_checkpoint(self.sync, self.vault, self.state, dict(old, now="2026-11-21T12:00:00"),
                               session="archived-replay")
        self.assertEqual(first["steps"]["compact"], "compacted")
        self.assertEqual(first["steps"]["card"], "unchanged")

        # Subsequent dry-run and live replay recognize the archived card without over-limit false alarms.
        replay_req = dict(old, now="2026-11-21T13:00:00")
        preview = run_checkpoint(None, self.vault, self.state, replay_req, session="archived-replay", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "skipped_within_limit")
        self.assertEqual(preview["status"], "succeeded")
        second = run_checkpoint(self.sync, self.vault, self.state, replay_req, session="archived-replay")
        self.assertEqual(second["steps"]["card"], "unchanged")
        self.assertEqual(second["steps"]["compact"], "skipped_within_limit")
        self.assertEqual(second["status"], "succeeded")

    def _own_card_marks(self, req, session):
        from beyin_v3_checkpoint import _prepare_card
        _, owner, marker, end, _ = _prepare_card("", req["card"], session, datetime.fromisoformat(req["now"]))
        return owner, marker, end

    def _archive_moves(self, name="Last-Session.md", vault=None):
        from beyin_v3_compact import _records
        vault = vault or self.vault
        comp = vault / self.comp.name
        source = f"{comp.relative_to(vault).as_posix()}/{name}"
        payloads = []
        archive_dir = comp / "Arşiv"
        if archive_dir.is_dir():
            for path in sorted(archive_dir.glob(f"{Path(name).stem}-*.md")):
                moves, _, _, _ = _records(path.read_text(encoding="utf-8"), source)
                payloads.extend(moves.values())  # verbatim moved text, one entry per move record
        return payloads

    def test_retry_after_needs_rewrite_keeps_own_card_live(self):
        # A retry of the same request finds its card live and still leading the file. Compaction
        # must protect it again; otherwise the ahead-clock card stays and the handoff leaves.
        card = self.comp / "Last-Session.md"
        future_card = "## 2026-11-25 12:00 · ileride · ffffffff\nILERI_KART " + ("ileri saat notu. " * 75) + "\n"
        card.write_text("# Last-Session\n\n" + future_card, encoding="utf-8", newline="\n")
        req = {"card": {"label": "taze", "body": "TAZE_KART " + ("yeni detay. " * 165)}, "now": "2026-11-21T10:05:00"}
        _, marker, end = self._own_card_marks(req, "retry-own-card")

        first = run_checkpoint(self.sync, self.vault, self.state, req, session="retry-own-card")
        self.assertEqual(first["steps"]["compact"], "needs_rewrite")
        preview = run_checkpoint(None, self.vault, self.state, req, session="retry-own-card", dry_run=True)
        self.assertEqual(preview["steps"]["compact"], "needs_rewrite")
        retry = run_checkpoint(self.sync, self.vault, self.state, req, session="retry-own-card")
        self.assertEqual(retry["steps"]["card"], "unchanged")
        self.assertEqual(retry["steps"]["compact"], "needs_rewrite")
        live = card.read_text(encoding="utf-8")
        self.assertIn(marker, live)
        self.assertIn(end, live)
        self.assertFalse(any(marker in moved for moved in self._archive_moves()))

    def test_retry_after_interrupted_compaction_reuses_the_committed_move(self):
        import beyin_v3_compact

        card = self.comp / "Last-Session.md"
        future_card = "## 2026-11-25 12:00 · ileride · ffffffff\nILERI_KART " + ("ileri saat notu. " * 60) + "\n"
        old_card = "## 2026-11-01 10:00 · eski · 11111111\nESKI_KART " + ("eski kayıt. " * 40) + "\n"
        card.write_text("# Last-Session\n\n" + future_card + "\n" + old_card, encoding="utf-8", newline="\n")
        req = {"card": {"label": "taze", "body": "TAZE_KART " + ("yeni detay. " * 170)}, "now": "2026-11-21T10:05:00"}
        _, marker, end = self._own_card_marks(req, "interrupted")
        real_write = beyin_v3_compact._write

        def crash_before_live_replace(path, text):
            if Path(path).name == "Last-Session.md":
                raise OSError("simulated interruption after the archive append")
            return real_write(path, text)

        with patch.object(beyin_v3_compact, "_write", crash_before_live_replace):
            first = run_checkpoint(self.sync, self.vault, self.state, req, session="interrupted")
        self.assertEqual(first["steps"]["card"], "written")
        self.assertEqual(first["steps"]["compact"]["status"], "error")
        self.assertEqual(len(self._archive_moves()), 1)

        retry = run_checkpoint(self.sync, self.vault, self.state, req, session="interrupted")
        self.assertEqual(retry["steps"]["card"], "unchanged")
        moves = self._archive_moves()
        self.assertEqual(len(moves), 1, "the retry must reuse the interrupted run's archive move")
        self.assertIn("ESKI_KART", moves[0])
        self.assertNotIn(marker, moves[0])
        live = card.read_text(encoding="utf-8")
        self.assertIn(marker, live)
        self.assertIn(end, live)
        self.assertNotIn("ESKI_KART", live)

    def test_post_write_threads_compaction_keeps_replay_markers_live(self):
        # #877: compaction now runs after the Threads write. The keys it just committed must stay
        # in the live preamble, so a retry is recognized even after the edited thread moved.
        threads = self.comp / "Threads.md"
        closed = "\n## Closed Threads\n" + "".join(
            f"### Eski iş {i}\n- 2026-10-{i + 1:02d} kapanan eski iş kaydı {i}.\n" for i in range(20))
        threads.write_text("# Threads\n\n## Active Threads\n### Aktif İşler\n"
                           + ("- aktif ayrıntı satırı burada yer alır.\n" * 110) + closed
                           + ("- kapanan dolgu satırı.\n" * 60), encoding="utf-8", newline="\n")
        step = "YENI_ADIM " + ("uzun yeni görev açıklaması " * 40).strip()
        req = {"threads": [{"heading": "Aktif İşler", "next_step": step},
                           {"old": "### Eski iş 3\n", "new": "### Eski iş 3 (taşındı)\n"}],
               "now": "2026-11-21T10:00:00"}
        first = run_checkpoint(self.sync, self.vault, self.state, req, session="threads-877")
        self.assertEqual(first["steps"], {"threads": "written", "compact": "compacted"})
        live = threads.read_text(encoding="utf-8")
        self.assertNotIn("Eski iş 3 (taşındı)", live)
        self.assertEqual(len(re.findall(r"^<!-- beyin-checkpoint-thread:[a-f0-9]{64} -->$", live, re.MULTILINE)), 2)
        for _ in range(2):
            retry = run_checkpoint(self.sync, self.vault, self.state, req, session="threads-877")
            self.assertEqual(retry["steps"], {"threads": "unchanged", "compact": "skipped_within_limit"})
            self.assertEqual(threads.read_text(encoding="utf-8"), live)

    def test_own_card_never_leaves_live_file_and_nothing_is_lost_grid(self):
        # Invariant over a fixed grid: after a checkpoint and its retry (no newer card written
        # above it in between), the request's own card is whole in the live file, never in an
        # archive move, and every other line of the file before the call is either still live
        # or in an archive move. A retry superseded by a newer card may still archive its own
        # card (test_card_retry_recognizes_a_compacted_card).
        from itertools import product

        futures = ([], [("2026-11-25 12:00", 300)], [("2026-11-26 09:00", 1400), ("2026-11-25 12:00", 300)])
        olds = ([], [("2026-11-02 10:00", 600)], [("2026-11-03 10:00", 600), ("2026-11-02 10:00", 1200)])

        def card_text(stamp, size, tag):
            return f"## {stamp} · {tag} · {tag[:8]:0<8}\n{tag.upper()}_GOVDE " + ("x" * size) + "\n"

        for index, (future, old, history, prior, size) in enumerate(
                product(futures, olds, (False, True), (False, True), (80, 1200, 3300))):
            with self.subTest(future=len(future), old=len(old), history=history, prior=prior, size=size):
                vault = Path(self.test_dir) / f"grid-{index}" / "vault"
                state = vault.parent / "state"
                comp = vault / self.comp.name
                comp.mkdir(parents=True)
                state.mkdir()
                sync = SyncEngine(vault, state)
                session = f"grid-{index}"
                parts = ["# Last-Session\n\n"]
                parts += [card_text(stamp, n, f"ileri{i}") + "\n" for i, (stamp, n) in enumerate(future)]
                parts += [card_text(stamp, n, f"eski{i}") + "\n" for i, (stamp, n) in enumerate(old)]
                if history:
                    parts.append("## Önceki oturumlar\n- 2026-10-01 GECMIS_SATIRI " + ("y" * 200) + "\n")
                live_path = comp / "Last-Session.md"
                live_path.write_text("".join(parts), encoding="utf-8", newline="\n")
                limits = state / "companion-limits.json"
                if prior:
                    limits.write_text('{"Last-Session.md": 0}', encoding="utf-8")
                    run_checkpoint(sync, vault, state,
                                   {"card": {"label": "önceki", "body": "ONCEKI_SURUM " + ("z" * 400)},
                                    "now": "2026-11-20T09:00:00"}, session=session)
                    limits.unlink()
                before = live_path.read_text(encoding="utf-8")
                req = {"card": {"label": "taze", "body": "TAZE_GOVDE " + ("t" * size)}, "now": "2026-11-21T10:00:00"}
                owner, marker, end = self._own_card_marks(req, session)
                for attempt in ("call", "retry"):
                    res = run_checkpoint(sync, vault, state, req, session=session)
                    self.assertIn(res["steps"]["compact"], ("compacted", "needs_rewrite", "skipped_within_limit"),
                                  (attempt, res))
                    live = live_path.read_text(encoding="utf-8")
                    self.assertIn(marker, live, attempt)
                    self.assertIn(end, live, attempt)
                    self.assertIn("TAZE_GOVDE " + ("t" * size), live, attempt)
                    moved = self._archive_moves(vault=vault)
                    self.assertFalse(any(marker in payload for payload in moved), attempt)
                    kept = set(live.splitlines()) | {line for payload in moved for line in payload.splitlines()}
                    lost = [line for line in before.splitlines()
                            if line.strip() and "ONCEKI_SURUM" not in line and owner not in line
                            and line not in kept]
                    self.assertEqual(lost, [], attempt)

if __name__ == "__main__":
    unittest.main()
