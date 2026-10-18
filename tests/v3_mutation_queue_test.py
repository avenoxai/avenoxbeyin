import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3_mutation as mutation
from beyin_v3_sync import SyncEngine, _hash
CLI = ROOT / "scripts/beyin_v3.py"


class MutationQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-mqueue-test-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.sync = SyncEngine(self.vault, self.state)
        self.sync.sync()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_machine_id_generation_and_persistence(self):
        mid1 = mutation.get_machine_id(self.state)
        self.assertRegex(mid1, r"^[0-9a-f]{32}$")

        mid2 = mutation.get_machine_id(self.state)
        self.assertEqual(mid1, mid2)

        # Reuses parallel presence device if present
        other_state = self.tmp / "other_state"
        other_state.mkdir()
        presence_file = other_state / "parallel-presence-device.json"
        presence_file.write_text(json.dumps({"schema": 1, "device": "abcdef0123456789abcdef0123456789"}), encoding="utf-8")
        mid3 = mutation.get_machine_id(other_state)
        self.assertEqual(mid3, "abcdef0123456789abcdef0123456789")

    def test_enqueue_and_list_mutations(self):
        m1 = mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/first.md",
            {"text": "First note body", "metadata": {"title": "First"}},
        )
        self.assertEqual(m1["status"], "pending")
        self.assertEqual(m1["action"], "note_create")
        self.assertEqual(m1["source"], "notes/first.md")

        m2 = mutation.enqueue_mutation(
            self.state,
            "receipt",
            "receipts/rec1.md",
            {"event_id": "rec1", "summary": "Did work", "refs": ["notes/first.md"]},
        )
        self.assertEqual(m2["status"], "pending")

        items = mutation.list_mutations(self.state)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["mutation_id"], m1["mutation_id"])
        self.assertEqual(items[1]["mutation_id"], m2["mutation_id"])

        pending_items = mutation.list_mutations(self.state, status="pending")
        self.assertEqual(len(pending_items), 2)

        applied_items = mutation.list_mutations(self.state, status="applied")
        self.assertEqual(len(applied_items), 0)

    def test_replay_mutations_applies_note_and_receipt(self):
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/test-note.md",
            {"text": "Hello offline world", "metadata": {"title": "Offline Note"}},
        )
        mutation.enqueue_mutation(
            self.state,
            "receipt",
            "receipts/rec-test.md",
            {"event_id": "rec-test-1", "summary": "Offline receipt", "refs": ["notes/test-note.md"]},
        )

        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["applied"], 2)
        self.assertEqual(res["conflicts"], 0)
        self.assertEqual(res["failed"], 0)

        # Verify files on disk
        note_file = self.vault / "notes/test-note.md"
        self.assertTrue(note_file.is_file())
        self.assertIn("Hello offline world", note_file.read_text(encoding="utf-8"))

        receipt_file = self.vault / "receipts" / f"{_hash('rec-test-1')}.md"
        self.assertTrue(receipt_file.is_file())

        # Verify queue status updated
        applied = mutation.list_mutations(self.state, status="applied")
        self.assertEqual(len(applied), 2)

        # Idempotent replay: enqueuing exact same note_create
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/test-note.md",
            {"text": "Hello offline world", "metadata": {"title": "Offline Note"}},
        )
        res2 = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res2["applied"], 1)
        self.assertEqual(res2["conflicts"], 0)
        applied2 = mutation.list_mutations(self.state, status="applied")
        self.assertTrue(any(m.get("idempotent") for m in applied2))

    def test_replay_task_create_and_update(self):
        task_meta = {
            "id": "offline-task-1",
            "title": "Offline Task",
            "status": "inbox",
            "owner": "test-agent",
            "project": "core",
            "visibility": "internal",
        }
        mutation.enqueue_mutation(
            self.state,
            "task_create",
            "tasks/offline-task.md",
            {"text": "Task description", "metadata": task_meta},
        )
        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["applied"], 1)
        self.assertTrue((self.vault / "tasks/offline-task.md").is_file())

        # Update task status to active
        mutation.enqueue_mutation(
            self.state,
            "task_update",
            "tasks/offline-task.md",
            {"id": "offline-task-1", "expected_revision": 1, "changes": {"status": "active"}},
        )
        res2 = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res2["applied"], 1)
        content = (self.vault / "tasks/offline-task.md").read_text(encoding="utf-8")
        self.assertIn('"status": "active"', content)
        self.assertIn('"revision": 2', content)

    def test_conflict_isolation_preserves_original_and_creates_stamped_copy(self):
        # Create an existing note in the vault
        note_path = self.vault / "notes/conflict.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "---\n{\n  \"title\": \"Original\"\n}\n---\nOriginal content that must not be lost\n"
        note_path.write_text(original_text, encoding="utf-8")
        self.sync.sync()

        # Enqueue a conflicting note_create with different content
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/conflict.md",
            {"text": "Conflicting incoming content", "metadata": {"title": "Incoming"}},
        )

        res = mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["conflicts"], 1)
        self.assertEqual(res["applied"], 0)

        # Original note is UNTOUCHED
        self.assertEqual(note_path.read_text(encoding="utf-8"), original_text)

        # Conflicted copy is created
        mut = mutation.list_mutations(self.state, status="conflict")[0]
        conflict_rel = mut.get("conflict_path")
        self.assertIsNotNone(conflict_rel)
        conflict_file = self.vault / conflict_rel
        self.assertTrue(conflict_file.is_file())
        self.assertIn("Conflicting incoming content", conflict_file.read_text(encoding="utf-8"))

        # Regex check: adheres to _STAMPED_COPY format
        self.assertRegex(conflict_file.name, r"^conflict\.sync-conflict-\d{8}-\d{6}-[0-9a-f]{8}\.md$")

        # Sync recognizes the conflict copy and warns without overwriting
        sync_res = self.sync.sync()
        self.assertEqual(sync_res["status"], "degraded")
        self.assertTrue(any("file sync conflict copy" in w.get("reason", "") for w in sync_res["warnings"]))

    def test_clear_applied_prunes_queue(self):
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n1.md",
            {"text": "Note 1"},
        )
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n2.md",
            {"text": "Note 2"},
        )
        mutation.replay_mutations(self.vault, self.state, sync_engine=self.sync)

        # Add one more pending
        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/n3.md",
            {"text": "Note 3"},
        )

        all_muts = mutation.list_mutations(self.state)
        self.assertEqual(len(all_muts), 3)

        pruned_info = mutation.clear_applied(self.state)
        self.assertEqual(pruned_info["pruned"], 2)
        self.assertEqual(pruned_info["remaining"], 1)

        remaining = mutation.list_mutations(self.state)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source"], "notes/n3.md")

    def test_doctor_diagnostics(self):
        diag1 = mutation.doctor(self.state)
        self.assertEqual(diag1["status"], "ok")
        self.assertEqual(diag1["queue_exists"], False)

        mutation.enqueue_mutation(
            self.state,
            "note_create",
            "notes/pending.md",
            {"text": "Pending note"},
        )
        diag2 = mutation.doctor(self.state)
        self.assertEqual(diag2["status"], "pending")
        self.assertEqual(diag2["pending_count"], 1)
        self.assertEqual(diag2["queue_exists"], True)

    def test_cli_mutation_queue_commands(self):
        def run_cli(*args):
            cmd = [sys.executable, str(CLI), "--vault", str(self.vault), "--state", str(self.state)] + list(args)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            try:
                data = json.loads(proc.stdout)
            except Exception:
                data = proc.stdout
            return data, proc.returncode

        # 1. doctor status via CLI
        res, code = run_cli("mutation-queue")
        self.assertEqual(code, 0)
        self.assertIn("machine_id", res)
        self.assertEqual(res["pending_count"], 0)

        # 2. enqueue via CLI
        payload_file = self.tmp / "enqueue.json"
        payload_file.write_text(json.dumps({
            "action": "note_create",
            "source": "notes/cli-note.md",
            "payload": {"text": "From CLI", "metadata": {"title": "CLI Note"}}
        }), encoding="utf-8")

        res_enq, code_enq = run_cli("mutation-queue", "--enqueue", "--file", str(payload_file))
        self.assertEqual(code_enq, 0)
        self.assertEqual(res_enq["status"], "pending")
        self.assertEqual(res_enq["source"], "notes/cli-note.md")

        # 3. list via CLI
        res_list, code_list = run_cli("mutation-queue", "--list")
        self.assertEqual(code_list, 0)
        self.assertEqual(len(res_list), 1)

        # 4. replay via CLI
        res_rep, code_rep = run_cli("mutation-queue", "--replay")
        self.assertEqual(code_rep, 0)
        self.assertEqual(res_rep["applied"], 1)
        self.assertTrue((self.vault / "notes/cli-note.md").is_file())

        # 5. clear-applied via CLI
        res_clr, code_clr = run_cli("mutation-queue", "--clear-applied")
        self.assertEqual(code_clr, 0)
        self.assertEqual(res_clr["pruned"], 1)


if __name__ == "__main__":
    unittest.main()
