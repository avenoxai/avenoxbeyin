"""Pi receipts stay readable by released 3.12.0 after a rollback or on another device (#385).

Released 3.12.0 rebuilds the receipts index from `receipts/*.md` and rejects any `harness`
outside its own client list plus "manual" (`invalid harness: pi`, sync `degraded`, doctor
`needs_attention`). The same vault is read by every installed version: a rollback keeps the
receipt files, and another device may run an older release. Pi receipts are therefore
written as `harness: manual` plus `client: pi`, while the runtime index keeps `pi`, so the
receipt still closes the Pi checkpoint and Stop reminder.
"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

from beyin_v3_hook import drain_queue
from beyin_v3_projections import receipt_coverage
from beyin_v3_sync import SyncEngine, _hash, parse, render

# The receipt `harness` values released 3.12.0 accepts (beyin_v3_sync._receipt_event there).
RELEASED_3_12_RECEIPT_HARNESSES = ('codex', 'claude', 'antigravity', 'hermes', 'opencode', 'omp', 'kiro', 'manual')


def released_3_12_accepts(metadata):
    """The 3.12.0 receipt harness rule, verbatim in effect: absent means manual, others fail."""
    return metadata.get('harness', 'manual') in RELEASED_3_12_RECEIPT_HARNESSES


class PiReceiptCompatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-pi-receipt-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.vault = self.base / 'vault'
        (self.vault / 'notes').mkdir(parents=True)
        (self.vault / 'notes/sonuç.md').write_text('Pi sonucu için kaynak not.\n', encoding='utf-8')
        self.state = self.base / 'state'
        self.engine = SyncEngine(self.vault, self.state)

    def source(self, event_id):
        return self.vault / 'receipts' / (_hash(event_id) + '.md')

    def payloads(self, state):
        db = sqlite3.connect(state / 'memory.sqlite3')
        try:
            return {row[0]: json.loads(row[1]) for row in db.execute('SELECT id, payload FROM receipts')}
        finally:
            db.close()

    def test_pi_receipt_source_uses_a_harness_released_3_12_accepts(self):
        result = self.engine.receipt('pi-ev', 'Pi işi bitti.', ['notes/sonuç.md'], 'pi', session='pisess01')
        self.assertEqual(result['status'], 'succeeded')
        metadata, body = parse(self.source('pi-ev').read_text(encoding='utf-8'))
        self.assertTrue(released_3_12_accepts(metadata), metadata)
        self.assertEqual((metadata['harness'], metadata['client']), ('manual', 'pi'))
        self.assertEqual(body, 'Pi işi bitti.\n')
        # The runtime index keeps the client: Stop reminders and coverage match (pi, session).
        payload = self.payloads(self.state)['pi-ev']
        self.assertEqual((payload['harness'], payload['session']), ('pi', 'pisess01'))
        self.assertNotIn('client', payload)
        for harness in ('claude', 'codex', 'kiro'):
            self.engine.receipt('ev-' + harness, 'Sonuç.', ['notes/sonuç.md'], harness)
            metadata, _ = parse(self.source('ev-' + harness).read_text(encoding='utf-8'))
            self.assertEqual(metadata['harness'], harness)
            self.assertNotIn('client', metadata, 'Known clients keep the released format byte for byte')

    def test_fresh_state_rebuild_keeps_pi_attribution_and_replay_is_idempotent(self):
        self.engine.receipt('pi-ev', 'Pi işi bitti.', ['notes/sonuç.md'], 'pi', session='pisess01')
        written = self.source('pi-ev').read_bytes()
        fresh_state = self.base / 'fresh-state'
        fresh = SyncEngine(self.vault, fresh_state)
        result = fresh.sync()
        self.assertNotEqual(result.get('status'), 'degraded', result)
        self.assertEqual(result.get('warnings', []), [])
        payload = self.payloads(fresh_state)['pi-ev']
        self.assertEqual((payload['harness'], payload['session']), ('pi', 'pisess01'))
        for engine in (self.engine, fresh, SyncEngine(self.vault, self.base / 'adopting-state')):
            again = engine.receipt('pi-ev', 'Pi işi bitti.', ['notes/sonuç.md'], 'pi', session='pisess01')
            self.assertEqual(again['status'], 'succeeded')
        self.assertEqual(self.source('pi-ev').read_bytes(), written)

    def write_source(self, event_id, metadata):
        path = self.source(event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {'kind': 'receipt', 'event_id': event_id, **metadata, 'refs': ['notes/sonuç.md'],
                'visibility': 'internal', 'created_at': '2026-11-07T10:00:00+00:00', 'session': 'abc'}
        path.write_bytes(render(data, 'Elle yazılmış receipt.\n').encode('utf-8'))
        return path

    def test_receipt_written_with_harness_pi_still_reads(self):
        # Sources of unreleased contribution builds name the client directly.
        self.write_source('pi-direct', {'harness': 'pi'})
        result = self.engine.sync()
        self.assertEqual(result.get('warnings', []), [])
        self.assertEqual(self.payloads(self.state)['pi-direct']['harness'], 'pi')

    def test_unknown_client_from_a_newer_writer_stays_manual_and_replays(self):
        path = self.write_source('future', {'harness': 'manual', 'client': 'yeni-istemci'})
        written = path.read_bytes()
        result = self.engine.sync()
        self.assertEqual(result.get('warnings', []), [])
        payload = self.payloads(self.state)['future']
        self.assertEqual((payload['harness'], payload['client']), ('manual', 'yeni-istemci'))
        fresh = SyncEngine(self.vault, self.base / 'adopting-state')
        again = fresh.receipt('future', 'Elle yazılmış receipt.', ['notes/sonuç.md'], 'manual')
        self.assertEqual(again['status'], 'succeeded')
        self.assertEqual(path.read_bytes(), written)

    def test_invalid_client_is_reported_not_indexed(self):
        for event_id, client in (('bad-1', 'Pi Agent'), ('bad-2', 3), ('bad-3', '')):
            self.write_source(event_id, {'harness': 'manual', 'client': client})
        result = self.engine.sync()
        self.assertEqual(result.get('status'), 'degraded')
        reasons = {item['source']: item['reason'] for item in result['warnings']}
        self.assertEqual(set(reasons.values()), {'invalid receipt client'})
        self.assertEqual(self.payloads(self.state), {})

    def enqueue(self, harness, session, event, at):
        queue = self.state / 'hook-queue'
        queue.mkdir(parents=True, exist_ok=True)
        (queue / f'{int(at * 1000)}-{os.urandom(4).hex()}.json').write_text(
            json.dumps({'harness': harness, 'event': event, 'session': session, 'at': at}), encoding='utf-8')

    def test_pi_receipt_closes_pi_checkpoint(self):
        now = time.time()
        for session, events in (('pi-edit', ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop')),
                                ('pi-read', ('SessionStart', 'UserPromptSubmit', 'Stop'))):
            for offset, event in enumerate(events):
                self.enqueue('pi', session, event, now - 60 + offset)
        drain_queue(self.vault, self.state)
        self.engine.receipt('pi-close', 'Pi düzenlemesi kaydedildi.', ['notes/sonuç.md'], 'pi', session='pi-edit')
        with self.engine.store._connect() as db:
            coverage = receipt_coverage(db, now=time.time(), state=self.state)
        self.assertEqual((coverage['total'], coverage['covered'], coverage['missing']), (2, 1, 1))


if __name__ == '__main__':
    unittest.main()
