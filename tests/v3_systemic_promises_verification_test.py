"""Comprehensive systemic verification of AvenoxBeyin's core promises (RFC #368 / V3 vs V4).

This test suite subjects the six foundational promises of AvenoxBeyin to end-to-end
testing across the vault lifecycle:
1. Data Sovereignty & Markdown as Truth (Rebuild & Desync Recovery)
2. Zero-Cost & Local Operation (Zero Network Egress & Socket Resilience)
3. Anti-Hallucination & Evidence Contract (kind: fact vs inference, evidence_refs)
4. Living Companion Architecture (Core, Kurallar, Person Notes, Last-Session Archive)
5. Conversation-to-Knowledge Learning Pipeline ('Öğrenilen: yok' Bypass & Candidate Ledger)
6. Workspace Boundaries & Multi-Project Isolation (Context Leakage)
"""
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3 as runtime
import beyin_v3_companion as companion
import beyin_v3_hook as hook
import beyin_v3_sync as sync_module


class SystemicPromisesVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-promise-test-")
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.state = Path(self.tmp.name) / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.companion_dir = self.vault / companion.DEFAULT_DIRECTORY
        self.companion_dir.mkdir(parents=True)
        self.store = runtime.MemoryStore(self.state, self.vault)

    # -------------------------------------------------------------------------
    # PROMISE 1: Veri Egemenliği ve Şeffaflık (Markdown as Truth)
    # -------------------------------------------------------------------------
    def test_promise_1_markdown_as_truth_full_rebuild_from_scratch(self):
        """Markdown as Truth: If SQLite database and state are destroyed,
        sync must deterministically recreate 100% of tasks, notes, receipts,
        and search capabilities from Markdown sources alone."""
        # 1. Create source markdown files with Turkish characters and metadata
        tasks_dir = self.vault / "tasks"
        tasks_dir.mkdir(parents=True)
        task_content = (
            "---\n"
            "id: task-001\n"
            "title: Türkçe Başlık: Güvenlik İncelemesi\n"
            "kind: task\n"
            "status: in_progress\n"
            "priority: high\n"
            "project: serai\n"
            "---\n"
            "Bu görev Türkçe karakterler içerir: ğüşiöç ĞÜŞİÖÇ.\n"
        )
        (tasks_dir / "task-001.md").write_text(task_content, encoding="utf-8")

        knowledge_dir = self.vault / "knowledge" / "concepts"
        knowledge_dir.mkdir(parents=True)
        note_content = (
            "---\n"
            "id: note-001\n"
            "title: Mimari Karar: Event-Driven Queue\n"
            "kind: note\n"
            "project: serai\n"
            "---\n"
            "Kritik mimari karar detayları.\n"
        )
        (knowledge_dir / "architecture.md").write_text(note_content, encoding="utf-8")

        # 2. Perform initial sync
        engine1 = sync_module.SyncEngine(self.vault, self.state)
        res1 = engine1.sync()
        self.assertEqual(res1.get("status"), "succeeded")

        # Verify items are in store
        search_res = self.store.context_for("claude", "Güvenlik", project="serai")
        self.assertGreaterEqual(len(search_res.get("records", [])), 1)

        # 3. DESTROY the SQLite database and state entirely
        db_file = self.state / "memory.sqlite3"
        self.assertTrue(db_file.exists())
        self.store.close()
        del self.store

        # Remove db and state files
        for f in self.state.glob("*"):
            if f.is_file():
                f.unlink()

        self.assertFalse(db_file.exists())

        # 4. REBUILD from Markdown alone
        engine2 = sync_module.SyncEngine(self.vault, self.state)
        res2 = engine2.sync()
        self.assertEqual(res2.get("status"), "succeeded")

        new_store = runtime.MemoryStore(self.state, self.vault)
        rebuilt_search = new_store.context_for("claude", "Güvenlik", project="serai")
        records = rebuilt_search.get("records", [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "task-001")
        new_store.close()

    # -------------------------------------------------------------------------
    # PROMISE 2: Sıfır Maliyet ve Yerel Çalışma (No-Model & Zero Network Egress)
    # -------------------------------------------------------------------------
    def test_promise_2_zero_network_egress_and_local_only(self):
        """No-Model Guarantee: Core operations (sync, search, compact) must
        NEVER make external network connections or API calls."""
        orig_connect = socket.socket.connect

        network_attempts = []
        def guarded_connect(sock, address):
            host, port = address[0], address[1]
            if host not in ("127.0.0.1", "localhost", "::1"):
                network_attempts.append(address)
                raise ConnectionRefusedError(f"External network egress blocked: {address}")
            return orig_connect(sock, address)

        with patch.object(socket.socket, "connect", side_effect=guarded_connect):
            engine = sync_module.SyncEngine(self.vault, self.state)
            engine.sync()
            self.store.context_for("claude", "test")

        self.assertEqual(network_attempts, [], "Core operations attempted external network calls!")

    # -------------------------------------------------------------------------
    # PROMISE 3: Halüsinasyon ve Hafıza Kayması Koruması (Evidence Contract)
    # -------------------------------------------------------------------------
    def test_promise_3_task_done_without_evidence_refs_allowed_by_default(self):
        """VULNERABILITY / BROKEN PROMISE:
        The promise states: 'Görevlerde kanıt bağlantısı (evidence_refs) olmadan
        görevin tamamlanmasına izin verilmez (strict contract).'
        REALITY: If completion_contract is not explicitly set to 'strict',
        a task CAN be marked done with zero evidence_refs!"""
        task_path = self.vault / "tasks" / "lenient-task.md"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_content = (
            "---\n"
            "id: lenient-task\n"
            "title: Normal Task\n"
            "kind: task\n"
            "status: in_progress\n"
            "---\n"
            "No strict contract declared.\n"
        )
        task_path.write_text(task_content, encoding="utf-8")
        engine = sync_module.SyncEngine(self.vault, self.state)
        engine.sync()

        # Update task to 'done' without evidence_refs
        done_content = (
            "---\n"
            "id: lenient-task\n"
            "title: Normal Task\n"
            "kind: task\n"
            "status: done\n"
            "---\n"
            "Task marked done without any evidence!\n"
        )
        task_path.write_text(done_content, encoding="utf-8")
        res = engine.sync()
        # The sync succeeds without error, proving evidence_refs is NOT enforced by default!
        self.assertEqual(res.get("status"), "succeeded")

    def test_promise_3_knowledge_notes_have_no_evidence_requirement(self):
        """VULNERABILITY / BROKEN PROMISE:
        Knowledge notes in knowledge/ have NO evidence_refs requirement at all.
        An agent can create hallucinated 'facts' with zero citations, and sync accepts it."""
        note_path = self.vault / "knowledge" / "concepts" / "hallucinated_fact.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_content = (
            "---\n"
            "id: hallucination-001\n"
            "title: The earth is hollow and flat\n"
            "kind: note\n"
            "---\n"
            "Zero evidence provided.\n"
        )
        note_path.write_text(note_content, encoding="utf-8")
        engine = sync_module.SyncEngine(self.vault, self.state)
        res = engine.sync()
        self.assertEqual(res.get("status"), "succeeded")
        # Ingested into database without any warnings or rejections!
        records = self.store.context_for("claude", "hollow").get("records", [])
        self.assertEqual(len(records), 1)

    # -------------------------------------------------------------------------
    # PROMISE 4: Gerçek Bir Düşünme Ortağı (Companion Context & notes/kisiler/)
    # -------------------------------------------------------------------------
    def test_promise_4_notes_kisiler_never_part_of_companion_names(self):
        """VULNERABILITY / BROKEN PROMISE:
        The companion claims to keep 'hayatınızdaki kişileri (notes/kisiler/)'.
        REALITY: companion.NAMES only defines:
        ('Core.md', 'Soul.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md').
        notes/kisiler/ is completely excluded from companion bootstrap sources!"""
        self.assertNotIn("notes/kisiler", companion.NAMES)
        self.assertNotIn("kisiler", [Path(p).stem for p in companion.NAMES])

    def test_promise_4_archived_last_session_lost_from_context(self):
        """VULNERABILITY / BROKEN PROMISE:
        When Last-Session.md references archived sessions under '## Önceki oturumlar',
        companion.excerpt('Last-Session.md') intentionally cuts off at '## Önceki' / '## Previous'.
        The pointer to the archive and all historical session pointers are stripped from the context!"""
        full_last_session = (
            "## 2026-10-18 10:00 · latest-session · 87654321\n"
            "- **Ne yapıldı:** En son çalışma yapıldı.\n\n"
            "## Önceki oturumlar\n"
            "Arşivlenen metin: 🔮 850-Companion/Arşiv/Last-Session-2026-10.md\n"
        )
        excerpted = companion.excerpt("Last-Session.md", full_last_session)
        # PROOF: The previous sessions pointer is now preserved!
        self.assertIn("Önceki oturumlar", excerpted)
        self.assertIn("Arşivlenen metin", excerpted)
        self.assertIn("latest-session", excerpted)

    # -------------------------------------------------------------------------
    # PROMISE 5: Kalıcı Öğrenim & 'Öğrenilen: Yok' Bypass (RFC #368 P0-A)
    # -------------------------------------------------------------------------
    def test_promise_5_ogrenilen_yok_bypass_in_receipt(self):
        """VULNERABILITY / BROKEN PROMISE:
        The hook checks if receipt declared learning. But if the receipt says
        'Öğrenilen: yok', the hook treats it as NO DECLARED LEARNING, allowing
        the session to close without recording ANY knowledge!"""
        receipt_with_no_learning = (
            "## 2026-10-18 11:00 · feature · abcdef12\n"
            "- **Ne yapıldı:** Büyük bir mimari refactor yapıldı.\n"
            "- **Öğrenilen:** yok (rutin kontrol)\n"
        )
        has_learning = hook._has_declared_learning(receipt_with_no_learning)
        # PROOF: Returns False! So hook skips reminder and allows session to exit without saving!
        self.assertFalse(has_learning, "Hook should treat 'Öğrenilen: yok' as False (bypassing reminder)")

        # Built-in bypass regex matches these:
        self.assertFalse(hook._has_declared_learning("- **Ders:** yok"))
        self.assertFalse(hook._has_declared_learning("- **Öğrenilenler:** hiçbiri"))
        self.assertFalse(hook._has_declared_learning("- **Learned:** none"))
        self.assertFalse(hook._has_declared_learning("- **Öğrenilen:** -"))

    # -------------------------------------------------------------------------
    # PROMISE 6: Çalışma Alanları İzolasyonu (Workspace Boundaries & Leakage)
    # -------------------------------------------------------------------------
    def test_promise_6_unscoped_search_cross_project_leakage(self):
        """VULNERABILITY / BROKEN PROMISE:
        In a multi-project vault, a search without an explicit --project filter
        leaks sensitive notes across isolated projects."""
        proj_a = self.vault / "projects" / "serai"
        proj_b = self.vault / "projects" / "sentinel"
        proj_a.mkdir(parents=True, exist_ok=True)
        proj_b.mkdir(parents=True, exist_ok=True)

        (proj_a / "secret.md").write_text("---\nid: serai-sec\nproject: serai\nkind: note\n---\nSerAI JWT Token\n", encoding="utf-8")
        (proj_b / "secret.md").write_text("---\nid: sentinel-sec\nproject: sentinel\nkind: note\n---\nSentinel JWT Token\n", encoding="utf-8")

        engine = sync_module.SyncEngine(self.vault, self.state)
        engine.sync()

        # Searching for 'JWT' without project returns BOTH projects
        leak = self.store.context_for("claude", "JWT")
        projects_found = {r.get("project") for r in leak.get("records", [])}
        self.assertEqual(projects_found, {"serai", "sentinel"}, "Unscoped search leaked cross-project context!")


if __name__ == "__main__":
    unittest.main()
