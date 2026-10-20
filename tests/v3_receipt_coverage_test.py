"""Receipt coverage ratio tests (Issue #78)."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

cli_spec = importlib.util.spec_from_file_location('cli_beyin_v3', ROOT / 'scripts/beyin_v3.py')
cli_beyin_v3 = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli_beyin_v3)
mark_session_edit = cli_beyin_v3.mark_session_edit
import beyin_entry
import beyin_v3_hook as hook
from beyin_v3_projections import receipt_coverage, _checkpoint_schema
from beyin_v3_sync import SyncEngine


class ReceiptCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()

    def _enqueue(self, event, session_id, at=None, harness='claude'):
        queue_dir = self.state / 'hook-queue'
        queue_dir.mkdir(parents=True, exist_ok=True)
        t = time.time() if at is None else at
        payload = {
            'harness': harness,
            'event': event,
            'session': session_id,
            'at': t,
        }
        item_path = queue_dir / f"{int(t * 1000)}-{os.urandom(4).hex()}.json"
        item_path.write_text(json.dumps(payload), encoding='utf-8')

    def test_coverage_counts_sessions_with_user_prompt_submit(self):
        """Only sessions with at least one UserPromptSubmit are counted in coverage (Issue #78)."""
        engine = SyncEngine(self.vault, self.state)

        # 1. Interactive Session 1: Has UserPromptSubmit + has a receipt
        now = time.time()
        self._enqueue('SessionStart', 'sess_interactive_1', at=now - 100)
        self._enqueue('UserPromptSubmit', 'sess_interactive_1', at=now - 90)
        self._enqueue('Stop', 'sess_interactive_1', at=now - 80)

        # Drain to create checkpoint
        hook.drain_queue(self.vault, self.state)
        gaps_data = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        sess1_hashed = gaps_data['checkpoints'][0]['session']

        # Add matching receipt for Session 1
        engine.note_create('notes/work.md', 'Work completed.', {'id': 'work-1'})
        engine.receipt('evt_1', 'Completed work', ['notes/work.md'], 'claude', session=sess1_hashed)
        engine.sync()

        # 2. Interactive Session 2: Has UserPromptSubmit but NO receipt
        self._enqueue('SessionStart', 'sess_interactive_2', at=now - 50)
        self._enqueue('UserPromptSubmit', 'sess_interactive_2', at=now - 40)
        self._enqueue('Stop', 'sess_interactive_2', at=now - 30)

        # 3. Non-interactive Session 3: SessionStart + Stop only (NO UserPromptSubmit)
        self._enqueue('SessionStart', 'sess_empty_3', at=now - 20)
        self._enqueue('Stop', 'sess_empty_3', at=now - 10)

        hook.drain_queue(self.vault, self.state)

        # Verify receipt-gaps.json
        gaps_file = self.state / 'receipt-gaps.json'
        self.assertTrue(gaps_file.exists())
        data = json.loads(gaps_file.read_text(encoding='utf-8'))

        coverage = data.get('receipt_coverage')
        self.assertIsNotNone(coverage)
        # Empty session 3 without UserPromptSubmit must NOT be in coverage!
        self.assertEqual(coverage['total'], 2, "Total should count only sessions with UserPromptSubmit")
        self.assertEqual(coverage['covered'], 1, "Only sess_interactive_1 has a receipt")
        self.assertEqual(coverage['missing'], 1, "sess_interactive_2 is missing a receipt")
        self.assertEqual(coverage['ratio'], 0.5)
        self.assertEqual(data['potential_missing_receipts'], coverage['missing'])
        self.assertEqual(len(data['checkpoints']), 1,
                         'promptless sessions must not inflate the gap count (#212)')

    def test_coverage_zero_sessions_returns_none_ratio(self):
        """When there are zero sessions with user prompts, ratio is None."""
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=time.time())
        self.assertEqual(cov['total'], 0)
        self.assertEqual(cov['covered'], 0)
        self.assertEqual(cov['missing'], 0)
        self.assertIsNone(cov['ratio'])
        self.assertIsNone(cov['last_7d']['ratio'])
        self.assertIsNone(cov['last_30d']['ratio'])

    def test_coverage_time_windows_measured_from_reference_now(self):
        """7-day and 30-day windows must be measured relative to the reference timestamp."""
        engine = SyncEngine(self.vault, self.state)
        now = 1800000000.0  # Fixed reference point

        # Session A: 3 days ago (within 7d, within 30d, all_time)
        t_a = now - 3 * 86400
        self._enqueue('SessionStart', 'sess_a', at=t_a)
        self._enqueue('UserPromptSubmit', 'sess_a', at=t_a + 1)
        self._enqueue('Stop', 'sess_a', at=t_a + 2)

        # Session B: 15 days ago (outside 7d, within 30d, all_time)
        t_b = now - 15 * 86400
        self._enqueue('SessionStart', 'sess_b', at=t_b)
        self._enqueue('UserPromptSubmit', 'sess_b', at=t_b + 1)
        self._enqueue('Stop', 'sess_b', at=t_b + 2)

        # Session C: 45 days ago (outside 7d, outside 30d, all_time)
        t_c = now - 45 * 86400
        self._enqueue('SessionStart', 'sess_c', at=t_c)
        self._enqueue('UserPromptSubmit', 'sess_c', at=t_c + 1)
        self._enqueue('Stop', 'sess_c', at=t_c + 2)

        hook.drain_queue(self.vault, self.state)

        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=now)

        self.assertEqual(cov['total'], 3)
        self.assertEqual(cov['last_30d']['total'], 2)  # A and B
        self.assertEqual(cov['last_7d']['total'], 1)   # A only

    def test_doctor_reports_none_for_missing_gaps_file(self):
        """Review Point 6: doctor must report potential_missing_receipts=None when receipt-gaps.json does not exist."""
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout)
        self.assertIsNone(doc.get('potential_missing_receipts'))
        self.assertIsNone(doc.get('receipt_coverage'))
        # Doctor reads through its own store; it must not build a SyncEngine and its journal.
        self.assertFalse((self.state / 'markdown-journal').exists())

    def test_doctor_dynamic_coverage_and_human_output(self):
        """Review Point 5 & Human Rendering: doctor computes coverage dynamically at invocation time."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()

        self._enqueue('SessionStart', 'doc_sess_1', at=now - 50)
        self._enqueue('UserPromptSubmit', 'doc_sess_1', at=now - 40)
        self._enqueue('Stop', 'doc_sess_1', at=now - 30)

        hook.drain_queue(self.vault, self.state)
        gaps_data = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        sess1_hashed = gaps_data['checkpoints'][0]['session']

        engine.note_create('notes/doc.md', 'Doctor test note.', {'id': 'doc-1'})
        engine.receipt('evt_doc', 'Doctor receipt', ['notes/doc.md'], 'claude', session=sess1_hashed)
        engine.sync()

        # Add second session without receipt
        self._enqueue('SessionStart', 'doc_sess_2', at=now - 20)
        self._enqueue('UserPromptSubmit', 'doc_sess_2', at=now - 10)
        self._enqueue('Stop', 'doc_sess_2', at=now)
        hook.drain_queue(self.vault, self.state)

        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout)

        self.assertEqual(doc.get('potential_missing_receipts'), 1)
        cov = doc.get('receipt_coverage')
        self.assertIsNotNone(cov)
        self.assertEqual(cov['total'], 2)
        self.assertEqual(cov['covered'], 1)
        self.assertEqual(cov['ratio'], 0.5)

        # Human rendering test
        human_text = beyin_entry.human_result(doc, 'doctor')
        self.assertIn('Makbuz kapsami: %50 (1/2 oturum, son 7 gun: %50)', human_text)

    def test_first_prompt_delivered_as_session_start_counts(self):
        """Hermes and OpenCode send the first user prompt as SessionStart; a one-prompt session still counts."""
        flows = {'opencode': ('SessionStart', 'Stop'), 'hermes': ('SessionStart', 'SessionEnd'),
                 'antigravity': ('SessionStart', 'SessionEnd')}
        for harness, (start, end) in flows.items():
            payload = {'hook_event_name': start, 'session_id': 'one-' + harness, 'event_id': harness + '-start'}
            if harness != 'antigravity':
                payload['prompt'] = 'tek istem'
            hook.enqueue_event(self.vault, self.state, payload, harness)
            hook.enqueue_event(self.vault, self.state, {'hook_event_name': end, 'session_id': 'one-' + harness,
                                                        'event_id': harness + '-end'}, harness)
        queued = [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-queue').glob('*.json')]
        self.assertFalse(any('prompt' in item for item in queued))
        hook.drain_queue(self.vault, self.state)
        with SyncEngine(self.vault, self.state).store._connect() as db:
            cov = receipt_coverage(db, now=time.time())
        self.assertEqual(cov['total'], 2)
        self.assertEqual(cov['missing'], 2)

    def test_unreadable_receipt_created_at_is_skipped_not_fatal(self):
        """A receipt row with a missing, null or malformed created_at never covers a session and never breaks sync or doctor."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        for session in ('sess_ok', 'sess_bad'):
            self._enqueue('UserPromptSubmit', session, at=now - 60)
            self._enqueue('Stop', session, at=now - 50)
        hook.drain_queue(self.vault, self.state)
        later = datetime.fromtimestamp(now - 10, timezone.utc).isoformat().replace('+00:00', 'Z')
        rows = {'ok': {'session': 'sess_ok', 'created_at': later},
                'null': {'session': 'sess_bad', 'created_at': None},
                'missing': {'session': 'sess_bad'},
                'malformed': {'session': 'sess_bad', 'created_at': '2026-13-45T00:00:00+00:00'}}
        with engine.store._connect() as db:
            for event_id, fields in rows.items():
                receipt = dict(fields, event_id=event_id, summary='s', refs=['notes/x.md'], harness='claude')
                db.execute('INSERT INTO receipts VALUES (?,?)', (event_id, json.dumps(receipt)))
        self.assertNotEqual(engine.sync()['status'], 'conflict')
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual([item['session'] for item in gaps['checkpoints']], ['sess_bad'])
        self.assertEqual((gaps['receipt_coverage']['covered'], gaps['receipt_coverage']['total']), (1, 2))
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        doc = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(doc['potential_missing_receipts'], 1)
        self.assertEqual((doc['receipt_coverage']['covered'], doc['receipt_coverage']['missing']), (1, 1))

    def test_schema_migration_adds_prompt_at_cleanly(self):
        """Legacy receipt_checkpoints table without prompt_at column is migrated without errors."""
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
            db.execute("INSERT INTO receipt_checkpoints VALUES ('claude', 'legacy_sess', 100.0, 50.0)")

        # Calling _checkpoint_schema or sync should migrate schema
        with engine.store._connect() as db:
            _checkpoint_schema(db)
            cols = {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}
            self.assertIn('prompt_at', cols)
            self.assertIn('edit_at', cols)

    def _edit_session(self, session, now, receipt_turns, turns):
        """One session: each turn is a prompt, optional edit and optional receipt, drained after its Stop."""
        engine = SyncEngine(self.vault, self.state)
        if not (self.vault / 'notes/task.md').exists():
            engine.note_create('notes/task.md', 'Work.', {'id': 'task'})
        for index, edits in enumerate(turns):
            start = now + index * 100
            self._enqueue('UserPromptSubmit', session, at=start)
            if edits:
                self._enqueue('PostToolUse', session, at=start + 10)
            hook.drain_queue(self.vault, self.state)
            if index in receipt_turns:
                event_id = f'{session}-{index}'
                engine.receipt(event_id, 'Bitti.', ['notes/task.md'], 'claude', session=session)
                stamp = datetime.fromtimestamp(start + 30, timezone.utc).isoformat()
                with engine.store._connect() as db:
                    db.execute("UPDATE receipts SET payload=json_set(payload,'$.created_at',?) WHERE id=?", (stamp, event_id))
            self._enqueue('Stop', session, at=start + 50)
            hook.drain_queue(self.vault, self.state)
        return json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))

    def test_question_after_the_receipt_keeps_the_session_covered(self):
        """A "thanks" or status turn after the receipt does not reopen finished work (#212)."""
        now = time.time() - 1000
        # Edit + receipt, then a question; also two edit turns closed by one receipt, then a question.
        data = self._edit_session('late_thanks', now, {0}, [True, False])
        data = self._edit_session('two_edit_turns', now, {1}, [True, True, False])
        self.assertEqual(data['potential_missing_receipts'], 0)
        self.assertEqual((data['receipt_coverage']['covered'], data['receipt_coverage']['total']), (2, 2))

    def test_later_edit_turn_still_needs_its_own_receipt(self):
        """A receipt from an earlier turn never covers a later turn that edited again (#212)."""
        data = self._edit_session('next_edit', time.time() - 1000, {0}, [True, True])
        self.assertEqual([item['session'] for item in data['checkpoints']], ['next_edit'])
        self.assertEqual(data['receipt_coverage']['missing'], 1)

    def test_backfill_edit_at_from_hook_done(self):
        """Historical checkpoints without edit_at are backfilled from hook-done events (#212)."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time() - 1000
        engine.note_create('notes/task.md', 'Work.', {'id': 'task'})

        # Insert legacy checkpoint without edit_at (edit_at=0), turn_at=now+100 (second turn: thanks)
        with engine.store._connect() as db:
            _checkpoint_schema(db)
            db.execute("INSERT INTO receipt_checkpoints(harness,session,at,turn_at,prompt_at,edit_at) VALUES ('claude','legacy_sess',?,?,?,0)",
                       (now + 150, now + 100, now + 100))
            # Receipt written at now + 50 (after turn 1 edit, before turn 2 thanks)
            event_id = 'legacy-receipt'
            receipt = {
                'event_id': event_id,
                'harness': 'claude',
                'session': 'legacy_sess',
                'summary': 'Done work.',
                'refs': ['notes/task.md'],
                'created_at': datetime.fromtimestamp(now + 50, timezone.utc).isoformat()
            }
            db.execute("INSERT INTO receipts VALUES (?,?)", (event_id, json.dumps(receipt)))

        # Before backfill: receipt at now+50 does NOT cover threshold turn_at=now+100
        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=now + 200)
            self.assertEqual(cov['covered'], 0)
            self.assertEqual(cov['missing'], 1)

        # Place historical events in state/hook-done
        done_dir = self.state / 'hook-done'
        done_dir.mkdir(parents=True, exist_ok=True)
        t1_prompt = {'harness': 'claude', 'session': 'legacy_sess', 'event': 'UserPromptSubmit', 'at': now}
        t1_edit = {'harness': 'claude', 'session': 'legacy_sess', 'event': 'PostToolUse', 'at': now + 10}
        t2_prompt = {'harness': 'claude', 'session': 'legacy_sess', 'event': 'UserPromptSubmit', 'at': now + 100}
        (done_dir / 'ev1.json').write_text(json.dumps(t1_prompt), encoding='utf-8')
        (done_dir / 'ev2.json').write_text(json.dumps(t1_edit), encoding='utf-8')
        (done_dir / 'ev3.json').write_text(json.dumps(t2_prompt), encoding='utf-8')

        # Run receipt_coverage with state provided (triggers backfill)
        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=now + 200, state=self.state)
            self.assertEqual(cov['covered'], 1)
            self.assertEqual(cov['missing'], 0)
            row = db.execute("SELECT edit_at FROM receipt_checkpoints WHERE session='legacy_sess'").fetchone()
            self.assertEqual(row[0], now)

        # Marker exists preventing repeated disk scan
        with engine.store._connect() as db:
            self.assertTrue(db.execute("SELECT value FROM metadata WHERE key='edit_at_backfilled'").fetchone())

    def test_backfill_preserves_harness_identity_and_rejects_invalid_events(self):
        engine = SyncEngine(self.vault, self.state)
        done = self.state / 'hook-done'
        done.mkdir()
        with engine.store._connect() as db:
            _checkpoint_schema(db)
            for harness in ('claude', 'codex'):
                db.execute("INSERT INTO receipt_checkpoints(harness,session,at,turn_at,prompt_at,edit_at) VALUES (?, 'shared', 300, 200, 200, 0)", (harness,))
            events = [[], {'harness': 'claude', 'session': 'shared', 'event': 'PostToolUse', 'at': 'bad'},
                      {'harness': 'claude', 'session': 'shared', 'event': 'UserPromptSubmit', 'at': 100},
                      {'harness': 'claude', 'session': 'shared', 'event': 'PostToolUse', 'at': 110},
                      {'harness': 'codex', 'session': 'shared', 'event': 'PostToolUse', 'at': 120, 'no_memory': True}]
            for index, event in enumerate(events):
                (done / f'{index}.json').write_text(json.dumps(event))
            receipt_coverage(db, now=400, state=self.state)
            values = dict(db.execute("SELECT harness,edit_at FROM receipt_checkpoints"))
            self.assertEqual(values, {'claude': 100, 'codex': 0})

    def test_coverage_reports_no_observed_edit(self):
        """Doctor coverage reports sessions without observed edits (#212, #462)."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        # Session 1: edit session
        self._enqueue('SessionStart', 'sess_edit', at=now - 50)
        self._enqueue('UserPromptSubmit', 'sess_edit', at=now - 40)
        self._enqueue('PostToolUse', 'sess_edit', at=now - 30)
        self._enqueue('Stop', 'sess_edit', at=now - 20)
        # Session 2: question-only session (no edit)
        self._enqueue('SessionStart', 'sess_no_edit', at=now - 45)
        self._enqueue('UserPromptSubmit', 'sess_no_edit', at=now - 35)
        self._enqueue('Stop', 'sess_no_edit', at=now - 25)

        hook.drain_queue(self.vault, self.state)
        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=now, state=self.state)
            self.assertEqual(cov['total'], 2)
            self.assertEqual(cov['no_observed_edit'], 1)
            self.assertEqual(cov['missing_no_observed_edit'], 1)
            self.assertEqual(cov['last_7d']['no_observed_edit'], 1)
            self.assertEqual(cov['last_7d']['missing_no_observed_edit'], 1)

    def test_opening_warning_and_gaps_report_no_observed_edit_separately(self):
        """Checkpoints without observed edits are tagged in gaps and reported separately in opening warning (#462)."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        # 1. Edit session without receipt (Claude)
        self._enqueue('SessionStart', 'sess_edit_only', at=now - 60)
        self._enqueue('UserPromptSubmit', 'sess_edit_only', at=now - 55)
        self._enqueue('PostToolUse', 'sess_edit_only', at=now - 50)
        self._enqueue('Stop', 'sess_edit_only', at=now - 45)

        # 2. Read-only session without receipt (Claude: no PostToolUse)
        self._enqueue('SessionStart', 'sess_read_only', at=now - 40)
        self._enqueue('UserPromptSubmit', 'sess_read_only', at=now - 35)
        self._enqueue('Stop', 'sess_read_only', at=now - 30)

        # 3. Harness without edit observation (Antigravity: scope limitation preserved)
        self._enqueue('SessionStart', 'sess_ag', at=now - 25, harness='antigravity')
        self._enqueue('Stop', 'sess_ag', at=now - 20, harness='antigravity')

        hook.drain_queue(self.vault, self.state)

        # Check receipt-gaps.json
        gaps_path = self.state / 'receipt-gaps.json'
        self.assertTrue(gaps_path.exists())
        gaps_data = json.loads(gaps_path.read_text(encoding='utf-8'))
        self.assertEqual(gaps_data['potential_missing_receipts'], 2)
        self.assertEqual(gaps_data['no_observed_edit'], 1)

        # Checkpoints in gaps: read-only has no_observed_edit=True
        gap_items = {g['session']: g for g in gaps_data['checkpoints']}
        self.assertTrue(gap_items['sess_read_only'].get('no_observed_edit'))
        self.assertNotIn('no_observed_edit', gap_items['sess_edit_only'])

        # Check hook-health.json
        health_data = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        sync_result = health_data.get('sync', {})
        self.assertEqual(sync_result.get('recent_missing_receipts'), 2)
        self.assertEqual(sync_result.get('recent_no_observed_edit'), 1)

        # Verify hook warning output
        hook_path = ROOT / 'template/.claude/scripts/beyin_v3_hook.py'
        cmd = [sys.executable, str(hook_path), '--vault', str(self.vault),
               '--state', str(self.state), '--harness', 'claude']
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'sess_check_warning'}
        proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)
        context = out.get('hookSpecificOutput', {}).get('additionalContext', '')
        self.assertIn('Prior checkpoints may lack structured receipts (2 in the last 7 days (1 without observed edits))', context)

    def test_mark_session_edit_records_session_marker(self):
        """mark_session_edit creates a receipt reminder marker for the active Claude session (#212)."""
        session_id = "test-session-uuid-1234"
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session_id}, clear=False):
            mark_session_edit(self.state)

        reminders_dir = self.state / "receipt-reminders"
        self.assertTrue(reminders_dir.is_dir())
        expected_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        marker_file = reminders_dir / f"{expected_hash}.edited"
        self.assertTrue(marker_file.is_file())
        data = json.loads(marker_file.read_text(encoding="utf-8"))
        self.assertIn("at", data)
        original_mtime = marker_file.stat().st_mtime_ns

        # Second call is idempotent and returns early
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session_id}, clear=False):
            mark_session_edit(self.state)
        self.assertEqual(marker_file.stat().st_mtime_ns, original_mtime)

    def test_mark_session_edit_skips_when_env_unset_or_suppressed(self):
        """mark_session_edit no-ops if session id is missing or reminder is disabled."""
        reminders_dir = self.state / "receipt-reminders"

        # Missing CLAUDE_CODE_SESSION_ID
        env_without_session = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with patch.dict(os.environ, env_without_session, clear=True):
            mark_session_edit(self.state)
        self.assertFalse(reminders_dir.exists())

        # BEYIN_V3_NO_RECEIPT_REMINDER is set
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-abc", "BEYIN_V3_NO_RECEIPT_REMINDER": "1"}, clear=False):
            mark_session_edit(self.state)
        self.assertFalse(reminders_dir.exists())

    def test_mark_session_edit_handles_oserror_gracefully(self):
        """mark_session_edit never raises on filesystem errors."""
        invalid_state = self.state / "nonexistent" / "file.txt"
        invalid_state.parent.mkdir(parents=True, exist_ok=True)
        invalid_state.write_text("not a directory", encoding="utf-8")
        # Passing a file as state causes folder.mkdir to fail with NotADirectoryError / OSError
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-xyz"}, clear=False):
            # Must not raise
            mark_session_edit(invalid_state)


if __name__ == '__main__':
    unittest.main()

