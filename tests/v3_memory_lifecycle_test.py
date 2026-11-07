"""V3 Memory Lifecycle & P0-A Definition of Done (DoD) Verification.

Tests the end-to-end memory lifecycle across 5 consecutive sessions and verifies
100% accurate recall with proper scoping and zero leaks in a fresh 6th session
(RFC #368 P0-A Definition of Done).
Zero external dependencies; stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_learning as learning
import beyin_v3_hook as hook
from beyin_v3_sync import SyncEngine
from v3_package_helpers import inherited_env


class MemoryLifecycleDoDTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        (self.vault / 'knowledge').mkdir(parents=True, exist_ok=True)
        (self.vault / 'notes').mkdir(parents=True, exist_ok=True)
        (self.vault / 'tasks').mkdir(parents=True, exist_ok=True)
        (self.vault / '🔮 850-Companion').mkdir(parents=True, exist_ok=True)
        (self.vault / 'projects' / 'sentinel').mkdir(parents=True, exist_ok=True)

        self.state = self.root / 'state'
        self.state.mkdir()
        self.engine = SyncEngine(self.vault, self.state)
        self.env = inherited_env(PYTHONIOENCODING='utf-8')

    def test_receipt_auto_resolves_staged_candidates(self):
        session_a = "session-dod-a-12345"
        session_b = "session-dod-b-67890"

        # Stage candidates for session A and session B
        learning.stage_candidate(self.state, session_a, "Candidate A1", source="notes/a1.md")
        learning.stage_candidate(self.state, session_a, "Candidate A2", source="notes/a1.md")
        learning.stage_candidate(self.state, session_b, "Candidate B1", source="notes/b1.md")

        self.assertEqual(len(learning.list_candidates(self.state, session=session_a)), 2)
        self.assertEqual(len(learning.list_candidates(self.state, session=session_b)), 1)

        # Write file referenced in receipt
        (self.vault / 'notes' / 'a1.md').write_text('# Note A1\n', encoding='utf-8')

        # Submit receipt for session A
        res = self.engine.receipt(
            'receipt-a-1',
            'Implemented note A1 with learned patterns.\nÖğrenilen: Candidate A1, Candidate A2',
            ['notes/a1.md'],
            'claude',
            session=session_a
        )
        self.assertEqual(res['status'], 'succeeded')
        self.assertEqual(res.get('candidates_resolved'), 2)

        # Candidates for session A must now be distilled
        self.assertFalse(learning.has_candidates(self.state, session=session_a))
        distilled_a = learning.list_candidates(self.state, session=session_a, status="distilled")
        self.assertEqual(len(distilled_a), 2)
        self.assertTrue(all("resolved_at" in item for item in distilled_a))

        # Candidate for session B must remain active as candidate
        self.assertTrue(learning.has_candidates(self.state, session=session_b))
        active_b = learning.list_candidates(self.state, session=session_b, status="candidate")
        self.assertEqual(len(active_b), 1)

    def test_receipt_leaves_unrelated_and_later_candidates_pending(self):
        (self.vault / 'notes/a.md').write_text('# A\n', encoding='utf-8')
        learning.stage_candidate(self.state, 'cli', 'Unscoped candidate', source='notes/a.md')
        learning.stage_candidate(self.state, 'session-a', 'Unreferenced candidate', source='notes/b.md')
        learning.stage_candidate(self.state, 'session-a', 'Referenced candidate', source='notes/a.md')
        result = self.engine.receipt('scoped-event', 'Saved A', ['notes/a.md'], 'claude', session='session-a')
        self.assertEqual(result.get('candidates_resolved'), 1)
        pending = learning.list_candidates(self.state)
        self.assertEqual({p['candidate'] for p in pending}, {'Unscoped candidate', 'Unreferenced candidate'})
        learning.stage_candidate(self.state, 'session-a', 'Later candidate', source='notes/a.md')
        self.engine.receipt('scoped-event', 'Saved A', ['notes/a.md'], 'claude', session='session-a')
        self.assertIn('Later candidate', {p['candidate'] for p in learning.list_candidates(self.state)})

    def test_cli_learn_resolve_command(self):
        session = "session-cli-resolve"
        # Stage via CLI
        subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--candidate', 'Candidate via CLI', '--session', session
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)

        # Verify candidate exists
        list_res = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--list', '--session', session
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        cands = json.loads(list_res.stdout).get("candidates", [])
        self.assertEqual(len(cands), 1)

        # Resolve via CLI
        resolve_res = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--resolve', 'distilled', '--session', session
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        res_data = json.loads(resolve_res.stdout)
        self.assertEqual(res_data.get("status"), "succeeded")
        self.assertEqual(res_data.get("resolved_count"), 1)

        # Verify active list is now empty
        list_after = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--list', '--session', session
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        self.assertEqual(len(json.loads(list_after.stdout).get("candidates", [])), 0)

    def test_cli_learn_resolve_discarded_and_all(self):
        sess1 = "sess-disc-1"
        sess2 = "sess-disc-2"
        learning.stage_candidate(self.state, sess1, "Discard candidate 1")
        learning.stage_candidate(self.state, sess2, "Discard candidate 2")
        self.assertEqual(len(learning.list_candidates(self.state, status="candidate")), 2)

        # Resolve all as discarded via CLI
        resolve_res = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'learn', '--resolve', 'discarded', '--all'
        ], capture_output=True, text=True, check=True, env=self.env, timeout=30)
        res_data = json.loads(resolve_res.stdout)
        self.assertEqual(res_data.get("status"), "succeeded")
        self.assertEqual(res_data.get("resolved_count"), 2)

        # Active candidates should now be 0, discarded should be 2
        self.assertEqual(len(learning.list_candidates(self.state, status="candidate")), 0)
        discarded = learning.list_candidates(self.state, status="discarded")
        self.assertEqual(len(discarded), 2)

    def test_receipt_resolves_candidates_from_environment_session(self):
        env_sess = "123e4567-e89b-12d3-a456-426614174000"
        learning.stage_candidate(self.state, env_sess, "Env session candidate", source="notes/env-ref.md")
        self.assertEqual(len(learning.list_candidates(self.state, session=env_sess)), 1)

        (self.vault / 'notes' / 'env-ref.md').write_text('# Env test\n', encoding='utf-8')

        # Run receipt CLI with CLAUDE_CODE_SESSION_ID in environment
        env_with_sess = dict(self.env, CLAUDE_CODE_SESSION_ID=env_sess)
        res_cli = subprocess.run([
            sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'receipt', '--event-id', 'rcpt-env-auto', '--summary', 'Auto resolved via env sess.\nÖğrenilen: Env session candidate',
            '--ref', 'notes/env-ref.md'
        ], capture_output=True, text=True, check=True, env=env_with_sess, timeout=30)
        res_json = json.loads(res_cli.stdout)
        self.assertEqual(res_json.get("status"), "succeeded")
        self.assertEqual(res_json.get("candidates_resolved"), 1)

        receipt_text = next((self.vault / 'receipts').glob('*.md')).read_text(encoding='utf-8')
        self.assertIn(hashlib.sha256(env_sess.encode()).hexdigest()[:24], receipt_text)
        self.assertNotIn(env_sess, receipt_text)

        # Candidate must now be distilled
        self.assertFalse(learning.has_candidates(self.state, session=env_sess))
        self.assertEqual(len(learning.list_candidates(self.state, session=env_sess, status="distilled")), 1)

    def test_five_session_lifecycle_and_sixth_fresh_session_recall_dod(self):
        """RFC #368 P0-A Definition of Done: 5 consecutive sessions + fresh 6th session recall."""
        # -------------------------------------------------------------
        # Session 1: Architectural Decision
        # -------------------------------------------------------------
        sess1_id = "sess-01-architectural-decision"
        sess1_hash = hashlib.sha256(sess1_id.encode('utf-8')).hexdigest()[:24]

        arch_file = self.vault / 'knowledge' / 'architecture.md'
        arch_file.write_text(
            '---\nkind: knowledge\ntitle: Architecture Decisions\n---\n\n'
            '# Architecture Decisions\n\n'
            '- Storage: SQLite WAL mode with synchronous=NORMAL is mandated for high concurrency.\n'
            '- Initial Timeout: 5 seconds timeout for lock acquisition.\n',
            encoding='utf-8'
        )
        learning.stage_candidate(
            self.state, sess1_hash,
            "SQLite WAL mode with synchronous=NORMAL is mandated for high concurrency",
            source="knowledge/architecture.md", scope="global"
        )
        self.engine.sync()
        r1 = self.engine.receipt(
            'rcpt-01',
            'Finalized storage architecture.\nÖğrenilen: SQLite WAL mode with synchronous=NORMAL',
            ['knowledge/architecture.md'], 'claude', session=sess1_hash
        )
        self.assertEqual(r1['status'], 'succeeded')
        self.assertEqual(r1.get('candidates_resolved'), 1)

        # -------------------------------------------------------------
        # Session 2: User Coding Preference
        # -------------------------------------------------------------
        sess2_id = "sess-02-user-preference"
        sess2_hash = hashlib.sha256(sess2_id.encode('utf-8')).hexdigest()[:24]

        rules_file = self.vault / '🔮 850-Companion' / 'Kurallar.md'
        rules_file.write_text(
            '# Kurallar ve Çalışma Prensipleri\n\n'
            '- **kural:** Public API fonksiyonlarında daima açık keyword arguments ve tip doğrulaması kullan.\n',
            encoding='utf-8'
        )
        learning.stage_candidate(
            self.state, sess2_hash,
            "Public API fonksiyonlarında daima açık keyword arguments ve tip doğrulaması kullan",
            source="🔮 850-Companion/Kurallar.md", scope="global"
        )
        self.engine.sync()
        r2 = self.engine.receipt(
            'rcpt-02',
            'Recorded user coding preference into Kurallar.md.\nÖğrenilen: Explicit keyword arguments and typing',
            ['🔮 850-Companion/Kurallar.md'], 'claude', session=sess2_hash
        )
        self.assertEqual(r2['status'], 'succeeded')
        self.assertEqual(r2.get('candidates_resolved'), 1)

        # -------------------------------------------------------------
        # Session 3: User Correction
        # -------------------------------------------------------------
        sess3_id = "sess-03-timeout-correction"
        sess3_hash = hashlib.sha256(sess3_id.encode('utf-8')).hexdigest()[:24]

        # Update architecture.md with corrected timeout
        arch_file.write_text(
            '---\nkind: knowledge\ntitle: Architecture Decisions\n---\n\n'
            '# Architecture Decisions\n\n'
            '- Storage: SQLite WAL mode with synchronous=NORMAL is mandated for high concurrency.\n'
            '- Corrected Timeout: Lock acquisition timeout increased to 15 seconds on Windows network shares (supersedes 5s).\n',
            encoding='utf-8'
        )
        learning.stage_candidate(
            self.state, sess3_hash,
            "Lock acquisition timeout increased to 15 seconds on Windows network shares (supersedes 5s)",
            source="knowledge/architecture.md", scope="global"
        )
        self.engine.sync()
        r3 = self.engine.receipt(
            'rcpt-03',
            'Corrected lock acquisition timeout based on latency evidence.\nÖğrenilen: 15 seconds timeout on Windows network shares',
            ['knowledge/architecture.md'], 'claude', session=sess3_hash
        )
        self.assertEqual(r3['status'], 'succeeded')
        self.assertEqual(r3.get('candidates_resolved'), 1)

        # -------------------------------------------------------------
        # Session 4: Workspace Scoped Rule
        # -------------------------------------------------------------
        sess4_id = "sess-04-workspace-sentinel-rule"
        sess4_hash = hashlib.sha256(sess4_id.encode('utf-8')).hexdigest()[:24]

        sentinel_rules = self.vault / 'projects' / 'sentinel' / 'Kurallar.md'
        sentinel_rules.write_text(
            '# Sentinel Proje Kuralları\n\n'
            '- [scope: sentinel] Tüm telemetri uyarıları 60 saniye içinde tekilleştirilmelidir (deduplicated).\n',
            encoding='utf-8'
        )
        learning.stage_candidate(
            self.state, sess4_hash,
            "Sentinel projesinde tüm telemetri uyarıları 60 saniye içinde tekilleştirilmelidir",
            source="projects/sentinel/Kurallar.md", scope="sentinel"
        )
        self.engine.sync()
        r4 = self.engine.receipt(
            'rcpt-04',
            'Established scoped rule for sentinel project.\nÖğrenilen: 60s telemetry alert deduplication',
            ['projects/sentinel/Kurallar.md'], 'claude', session=sess4_hash
        )
        self.assertEqual(r4['status'], 'succeeded')
        self.assertEqual(r4.get('candidates_resolved'), 1)

        # -------------------------------------------------------------
        # Session 5: Task Lifecycle Completion
        # -------------------------------------------------------------
        sess5_id = "sess-05-task-migration-done"
        sess5_hash = hashlib.sha256(sess5_id.encode('utf-8')).hexdigest()[:24]

        task_file = self.vault / 'tasks' / 'db-migration.md'
        task_file.write_text(
            '---\nid: task-db-migration\nstatus: done\nrevision: 2\n---\n\n'
            '# Database Connection Pool Migration\n\n'
            'Status: done. Zero downtime verified on test cluster.\n',
            encoding='utf-8'
        )
        learning.stage_candidate(
            self.state, sess5_hash,
            "Database connection pool migration completed with zero downtime",
            source="tasks/db-migration.md", scope="global"
        )
        self.engine.sync()
        r5 = self.engine.receipt(
            'rcpt-05',
            'Completed db-migration task.\nÖğrenilen: Zero downtime pool migration completed',
            ['tasks/db-migration.md'], 'claude', session=sess5_hash
        )
        self.assertEqual(r5['status'], 'succeeded')
        self.assertEqual(r5.get('candidates_resolved'), 1)

        # -------------------------------------------------------------
        # Session 6: Fresh 6th Session DoD Verification
        # -------------------------------------------------------------
        # Simulate a completely fresh, uninitialized session with zero cached state in memory
        sess6_id = "sess-06-fresh-start-dod-query"
        sess6_hash = hashlib.sha256(sess6_id.encode('utf-8')).hexdigest()[:24]

        (self.vault / '.beyin-projects.json').write_text(json.dumps({
            'folders': {'projects/sentinel': 'sentinel'}, 'shared_unscoped': True}), encoding='utf-8')
        (self.vault / 'notes/private-canary.md').write_text(
            '---\nvisibility: private\n---\n# PRIVATE_LIFECYCLE_CANARY SQLite WAL telemetri', encoding='utf-8')
        fresh_engine = SyncEngine(self.vault, self.state)
        fresh_engine.sync()

        # Every query runs in a new interpreter; no parent memory/store object survives.
        def retrieve(query, project=None):
            command = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                       '--vault', str(self.vault), '--state', str(self.state),
                       'context', query, '--harness', 'codex']
            if project is not None:
                command.extend(['--project', project])
            result = subprocess.run(command, capture_output=True, text=True,
                                    check=True, env=self.env, timeout=30)
            self.assertNotIn('PRIVATE_LIFECYCLE_CANARY', result.stdout)
            return json.loads(result.stdout)

        # 1. Architectural Decision Recall:
        arch_results = retrieve("SQLite WAL mode synchronous").get("records", [])
        self.assertTrue(len(arch_results) > 0, "Architecture decision must be retrieved")
        arch_doc = next(r for r in arch_results if "architecture.md" in r.get("source", ""))
        self.assertIn("SQLite WAL mode with synchronous=NORMAL", arch_doc.get("text", ""))

        # 2. User Coding Preference Recall:
        pref_results = retrieve("keyword arguments tip doğrulaması").get("records", [])
        self.assertTrue(len(pref_results) > 0, "Coding preference must be retrieved")
        pref_doc = next(r for r in pref_results if "Kurallar.md" in r.get("source", ""))
        self.assertIn("keyword arguments", pref_doc.get("text", ""))

        # 3. User Correction Recall (Superseded value is NOT the current value):
        corr_results = retrieve("timeout Windows network shares").get("records", [])
        self.assertTrue(len(corr_results) > 0, "Corrected timeout must be retrieved")
        corr_doc = next(r for r in corr_results if "architecture.md" in r.get("source", ""))
        self.assertIn("15 seconds on Windows network shares", corr_doc.get("text", ""))
        self.assertNotIn("Initial Timeout: 5 seconds", corr_doc.get("text", ""))

        # 4. Workspace Scoped Rule Recall:
        scope_results = retrieve("telemetri uyarıları tekilleştirilmelidir", project="sentinel").get("records", [])
        self.assertTrue(len(scope_results) > 0, "Sentinel rule must be retrieved")
        scope_doc = next(r for r in scope_results if "sentinel/Kurallar.md" in r.get("source", ""))
        self.assertIn("60 saniye", scope_doc.get("text", ""))
        unrelated = retrieve('telemetri uyarıları tekilleştirilmelidir', project='unrelated')
        self.assertFalse(any('sentinel/Kurallar.md' in record.get('source', '')
                             for record in unrelated.get('records', [])))

        # 5. Task Completion Recall:
        task_results = retrieve("task-db-migration Connection Pool").get("records", [])
        self.assertTrue(len(task_results) > 0, "Task record must be retrieved")
        task_doc = next(r for r in task_results if "db-migration.md" in r.get("source", ""))
        self.assertIn("done", task_doc.get("text", "").lower())

        # 6. Candidate Ledger Integrity:
        all_distilled = learning.list_candidates(self.state, status="distilled")
        self.assertEqual(len(all_distilled), 5, "All 5 candidates from sessions 1-5 must be distilled")
        all_active = learning.list_candidates(self.state, status="candidate")
        self.assertEqual(len(all_active), 0, "Zero active candidates must be left dangling")

        # 7. 'Öğrenilen: yok' Shield Verification in Session 6:
        # Simulate PostToolUse edit in session 6 without staging a candidate
        payload_edit = {'session_id': sess6_id, 'hook_event_name': 'PostToolUse'}
        hook.receipt_reminder(payload_edit, self.state, 'claude', 'PostToolUse', vault=self.vault)
        (self.vault / 'notes' / 'scratch.md').write_text('# Scratch edit\n', encoding='utf-8')

        # Agent writes receipt with bare 'Öğrenilen: yok'
        fresh_engine.receipt(
            'rcpt-06-bare',
            'Routine scratch edit.\nÖğrenilen: yok',
            ['notes/scratch.md'], 'claude', session=sess6_hash
        )

        # Stop hook MUST challenge the bare 'Öğrenilen: yok'
        payload_stop = {'session_id': sess6_id, 'hook_event_name': 'Stop'}
        stop_res = hook.receipt_reminder(payload_stop, self.state, 'claude', 'Stop', vault=self.vault)
        self.assertIsNotNone(stop_res)
        self.assertEqual(stop_res.get('decision'), 'block')
        self.assertIn("Öğrenilen: yok", stop_res.get('reason', ''))


if __name__ == '__main__':
    unittest.main()
