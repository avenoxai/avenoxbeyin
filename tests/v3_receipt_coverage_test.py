"""Receipt coverage ratio tests (Issue #78)."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

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


if __name__ == '__main__':
    unittest.main()
