#!/usr/bin/env python3
"""Comprehensive edge-case, boundary, concurrency torture, and disaster recovery test suite for V3."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import beyin_v3 as runtime
from beyin_v3_sync import SyncEngine, parse as parse_frontmatter
from beyin_v3 import pack_context, MemoryStore, RevisionConflict, ReceiptConflict
from beyin_v3_projections import recent_receipts, _receipt_instant, receipt_day
from beyin_v3_compact import compact
from v3_package_helpers import inherited_env


class TestV3EdgeCasesAndResilience(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()

        # In-memory DB for lightweight isolated unit tests
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE receipts(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_01_sync_frontmatter_edge_cases(self):
        """Malformed and adversarial frontmatter must cleanly raise ValueError."""
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle:\n\t- value\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\nmetadata:\n\tkey: value\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle:\n\tvalue\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\nnested:\n  deep:\n    level: 3\n---")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\ntitle: \"unclosed\n---")

        parsed, _ = parse_frontmatter("---\ntitle: \"A\\tB\"\n---")
        self.assertEqual(parsed['title'], "A\tB")

    def test_02_pack_context_extreme_budgets(self):
        """pack_context handles negative, zero, and minimal character budgets safely."""
        records = [
            {'id': 'a1', 'source': 'notes/a.md', 'text': 'This is a normal sized sentence for testing.'},
            {'id': 'b1', 'source': 'notes/b.md', 'text': 'Short.'},
        ]

        with self.assertRaises(ValueError):
            pack_context(records, limit=2, budget_chars=-1)

        result_zero = pack_context(records, limit=2, budget_chars=0)
        self.assertEqual(len(result_zero['records']), 0)
        self.assertTrue(result_zero['truncated'])

        result_one = pack_context(records, limit=2, budget_chars=1)
        self.assertEqual(len(result_one['records']), 0)
        self.assertTrue(result_one['truncated'])

        result_min = pack_context(records, limit=2, budget_chars=50)
        self.assertTrue(isinstance(result_min, dict))

    def test_03_turkish_and_unicode_surrogate_robustness(self):
        """Localized Turkish typography, surrogate emojis, and cross-platform NFC/NFD matching."""
        records = [
            {'id': 'turk1', 'source': 'notes/İstanbul.md', 'text': 'İstanbul ı İ ğ ş'},
        ]
        result = pack_context(records, limit=1, budget_chars=8000)
        self.assertEqual(len(result['records']), 1)

        records_surrogate = [
            {'id': 'surrogate1', 'source': 'notes/a.md', 'text': 'Emoji: \U0001F600'},
        ]
        result_surrogate = pack_context(records_surrogate, limit=1, budget_chars=8000)
        self.assertEqual(len(result_surrogate['records']), 1)

        text_with_i = "İstanbul"
        parsed, _ = parse_frontmatter(f"---\ntitle: {text_with_i}\n---")
        self.assertEqual(parsed['title'], text_with_i)

        # Cross-platform NFD vs NFC search query matching
        note_path = self.vault / 'İstanbul_projesi.md'
        note_path.write_text("---\ntitle: İstanbul NFC\n---\nÖzel Türkçe içerik ve projesi.\n", encoding='utf-8')
        engine = SyncEngine(self.vault, self.state)
        engine.sync()

        nfd_query = unicodedata.normalize('NFD', 'İstanbul')
        search_res = engine.store.retrieve(nfd_query, limit=5)
        self.assertGreaterEqual(len(search_res['records']), 1)

    def test_04_date_and_calendar_boundary_transitions(self):
        """Leap year validity, month overflows, and ISO format validation in receipts."""
        instant = _receipt_instant("2024-02-29T23:59:59Z")
        self.assertIsNotNone(instant)
        instant_invalid = _receipt_instant("2024-02-30T10:00:00Z")
        self.assertIsNone(instant_invalid)
        instant_overflow = _receipt_instant("2024-12-32T10:00:00Z")
        self.assertIsNone(instant_overflow)

    def test_05_recent_receipts_corrupted_payload_tolerance(self):
        """Corrupted or missing JSON payloads in receipts table do not break query iteration."""
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r1', '{invalid}'))
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r2', '{"event_id": "r2", "summary": "test"'))
        self.db.execute('INSERT INTO receipts VALUES (?,?)', ('r3', '{"event_id": "r3", "summary": "no date"}'))

        result = recent_receipts(self.db, days=7, limit=20)
        self.assertEqual(result['undated_omitted'], 3)
        self.assertEqual(len(result['items']), 0)

    def test_06_malformed_tasks_and_trailing_whitespace(self):
        """Tasks created via CLI with trailing whitespace and missing dates sync cleanly."""
        (self.vault / 'tasks').mkdir(exist_ok=True)
        task_data = {
            "source": "tasks/t1.md",
            "text": "This is a task body.",
            "metadata": {
                "id": "t1",
                "status": "inbox",
                "owner": "jules",
                "project": "test"
            }
        }
        task_file = self.root / 'task.json'
        task_file.write_text(json.dumps(task_data), encoding='utf-8')

        res = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
             '--state', str(self.state), 'task-create', '--file', str(task_file)],
            capture_output=True, text=True, env=inherited_env()
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        task_path = self.vault / 'tasks/t1.md'
        self.assertTrue(task_path.exists())

        content = task_path.read_text(encoding='utf-8')
        content += "\n- [ ] Missing date task with trailing whitespace   \n"
        content += "\n- [x] Done missing date task\n"
        task_path.write_text(content, encoding='utf-8')

        engine = SyncEngine(self.vault, self.state)
        sync_res = engine.sync()
        self.assertGreaterEqual(sync_res['indexed'], 1)

    def test_07_cross_platform_mixed_path_separators(self):
        """Mixed backslash and forward slash path separators in records pack without error."""
        records = [
            {'id': 'a1', 'source': 'notes\\a.md', 'text': 'Mixed path separators.'},
            {'id': 'b1', 'source': 'notes/b\\c.md', 'text': 'Short.'},
        ]
        result = pack_context(records, limit=2, budget_chars=8000)
        self.assertEqual(len(result['records']), 2)

    def test_08_concurrency_torture_on_compaction(self):
        """10 threads concurrently attempting compaction under file lock without corruption or loss."""
        companion = self.vault / '🔮 850-Companion'
        companion.mkdir(exist_ok=True)
        last_session = companion / 'Last-Session.md'

        # Set custom limits via companion-limits.json in state dir
        limits_file = self.state / 'companion-limits.json'
        limits_file.write_text(json.dumps({'schema': 1, 'Last-Session.md': 2000, 'Threads.md': 8000}), encoding='utf-8')

        initial_content = "# Son oturum\n\n" + "\n\n".join(
            f"## 2026-10-01 10:{i:02d} · session-{i} · {i:08x}\n- Long summary description for session {i}."
            for i in range(40)
        )
        last_session.write_text(initial_content, encoding='utf-8')

        results, errors = [], []

        def worker(idx):
            try:
                res = compact(self.vault, self.state, dry_run=False)
                results.append((idx, res.get('status')))
            except Exception as e:
                errors.append((idx, type(e).__name__, str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent compact produced unhandled errors: {errors}")
        final_len = len(last_session.read_text(encoding='utf-8'))
        self.assertLessEqual(final_len, 2500)
        archive = companion / 'Arşiv'
        self.assertTrue(archive.exists())
        archived_files = list(archive.glob('*.md'))
        self.assertGreaterEqual(len(archived_files), 1)

    def test_09_disaster_recovery_rebuild_from_markdown(self):
        """Markdown source-of-truth rebuild: wiping memory.sqlite3 is 100% recovered on next sync."""
        engine = SyncEngine(self.vault, self.state)
        (self.vault / 'notes').mkdir(exist_ok=True)
        (self.vault / 'tasks').mkdir(exist_ok=True)
        for i in range(15):
            note = self.vault / f'notes/note_{i}.md'
            note.write_text(f"---\ntitle: Note {i}\ntags: [research, test]\n---\nBody content for note {i}\n", encoding='utf-8')
        for i in range(5):
            task = self.vault / f'tasks/task_{i}.md'
            task.write_text(f"---\nkind: task\nid: task-{i}\ntitle: Task {i}\nstatus: active\n---\nTask text {i}\n", encoding='utf-8')

        engine.sync()
        db_path = self.state / 'memory.sqlite3'
        self.assertTrue(db_path.exists())

        db = sqlite3.connect(db_path)
        count_before = db.execute("SELECT count(*) FROM records").fetchone()[0]
        db.close()
        self.assertEqual(count_before, 20)

        # Simulate total database wipe / loss
        db_path.unlink()

        fresh_engine = SyncEngine(self.vault, self.state)
        fresh_engine.sync()

        db2 = sqlite3.connect(db_path)
        count_after = db2.execute("SELECT count(*) FROM records").fetchone()[0]
        db2.close()
        self.assertEqual(count_after, 20, "SyncEngine must fully restore all records from Markdown")

    def test_10_multi_agent_concurrency_revision_and_receipt_conflicts(self):
        """Optimistic concurrency control: RevisionConflict on stale tasks and ReceiptConflict on replay."""
        store = MemoryStore(self.state, self.vault)
        (self.vault / 'tasks').mkdir(exist_ok=True)
        (self.vault / 'notes').mkdir(exist_ok=True)

        task_path = self.vault / 'tasks/shared_task.md'
        task_path.write_text("Task body content", encoding='utf-8')
        task = store.ingest({
            "id": "shared-task", "source": "tasks/shared_task.md", "kind": "task",
            "text": "Task body content", "title": "Shared Task", "status": "active", "revision": 1
        })
        self.assertEqual(task['revision'], 1)

        # Agent A successfully updates task to revision 2
        updated = store.update_task("shared-task", 1, {"status": "done"})
        self.assertEqual(updated['revision'], 2)

        # Agent B attempts update with stale revision 1 -> must raise RevisionConflict
        with self.assertRaises(RevisionConflict):
            store.update_task("shared-task", 1, {"status": "inbox"})

        # Multi-harness receipts referencing common note
        note_path = self.vault / 'notes/reference_note.md'
        note_path.write_text("Common reference note content", encoding='utf-8')
        store.ingest({"id": "ref-note", "source": "notes/reference_note.md", "kind": "note", "text": "Common reference note content"})

        for harness in runtime.HARNESSES:
            rc = store.submit_receipt(f"evt-{harness}", f"Summary by {harness}", ["notes/reference_note.md"], harness)
            self.assertEqual(rc.get('status'), 'succeeded')

        # Tampered duplicate receipt replay must raise ReceiptConflict
        with self.assertRaises(ReceiptConflict):
            store.submit_receipt("evt-claude", "TAMPERED DIFFERENT SUMMARY", ["notes/reference_note.md"], "claude")

    def test_11_project_scopes_strict_cross_project_isolation(self):
        """Project scope configuration (.beyin-projects.json) strictly prevents cross-project leakage."""
        (self.vault / 'alpha').mkdir(exist_ok=True)
        (self.vault / 'beta').mkdir(exist_ok=True)

        (self.vault / 'alpha/plan.md').write_text("Alpha secret roadmap and strategy.", encoding='utf-8')
        (self.vault / 'beta/plan.md').write_text("Beta unrelated documentation.", encoding='utf-8')

        scopes_config = {
            "folders": {
                "alpha": "project-alpha",
                "beta": "project-beta"
            },
            "shared_unscoped": False
        }
        (self.vault / runtime.PROJECT_SCOPES_FILE).write_text(json.dumps(scopes_config), encoding='utf-8')

        engine = SyncEngine(self.vault, self.state)
        engine.sync()

        # Query project-alpha: must only see alpha records
        alpha_res = engine.store.retrieve("roadmap documentation", project="project-alpha", limit=5)
        sources_found = [r['source'] for r in alpha_res['records']]
        self.assertIn("alpha/plan.md", sources_found)
        self.assertNotIn("beta/plan.md", sources_found, "Cross-project leakage! Beta records must not appear in project-alpha query")

    def test_12_adversarial_frontmatter_redos_resilience(self):
        """Adversarial YAML with 50,000 whitespace characters is rejected within milliseconds."""
        malicious_input = "---\nkey" + (" " * 50000) + ": value\n---\nBody"
        t0 = time.perf_counter()
        with self.assertRaises(ValueError):
            parse_frontmatter(malicious_input)
        duration = time.perf_counter() - t0
        self.assertLess(duration, 0.5, f"Possible ReDoS detected! Took {duration:.2f}s on whitespace parsing")


if __name__ == '__main__':
    unittest.main()
