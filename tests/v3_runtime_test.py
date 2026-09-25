#!/usr/bin/env python3
"""Reliability gates with real temp directories/SQLite and synthetic records."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v3_runtime_evaluator', ROOT / 'scripts/evaluate_v3.py')
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class RuntimeContractTest(unittest.TestCase):
    def setUp(self):
        self.module = evaluator.load_runtime()
        self.assertIsNotNone(self.module, 'Runtime not implemented')
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-runtime-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'runtime'
        self.store = self.module.MemoryStore(self.state, self.vault)
        self.addCleanup(lambda: evaluator.close_store(self.store))

    def record(self, id='record', **changes):
        record = {'id': id, 'project': 'synthetic', 'kind': 'task', 'status': 'active',
                  'visibility': 'internal', 'text': 'Quartz observatory calibration awaits review.',
                  'facts': {'owner': 'Synthetic Reviewer'}, 'source': f'notes/{id}.md',
                  'updated_at': '2026-09-15T10:00:00Z'}
        record.update(changes)
        source = self.vault / record['source']
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(record['text'], encoding='utf-8')
        return record

    def retrieve(self, query='Quartz observatory calibration', **kwargs):
        return self.store.retrieve(query, project='synthetic', **kwargs)

    def test_persists_through_sqlite_reopen(self):
        self.store.ingest(self.record())
        evaluator.close_store(self.store)
        self.store = self.module.MemoryStore(self.state, self.vault)
        result = self.retrieve()
        self.assertEqual([r['id'] for r in result['records']], ['record'])
        self.assertEqual(result['records'][0]['facts']['owner'], 'Synthetic Reviewer')
        databases = [p for p in self.state.rglob('*') if p.is_file() and p.read_bytes()[:16] == b'SQLite format 3\x00']
        self.assertTrue(databases, 'Real SQLite persistence is required')
        for database in databases:
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                connection.close()

    def test_read_only_reopen_retrieves_and_rejects_writes(self):
        record = self.store.ingest(self.record())
        evaluator.close_store(self.store)
        read_only = self.module.MemoryStore(self.state, self.vault, read_only=True)
        self.addCleanup(lambda: evaluator.close_store(read_only))
        self.assertEqual([r['id'] for r in read_only.retrieve('Quartz observatory calibration')['records']], ['record'])
        with self.assertRaisesRegex(ValueError, 'read-only runtime'):
            read_only.ingest(record)
        with self.assertRaisesRegex(ValueError, 'read-only runtime'):
            read_only.update_task('record', 1, {'status': 'done'})
        with self.assertRaisesRegex(ValueError, 'read-only runtime'):
            read_only.submit_receipt('read-only-event', 'Synthetic result.', [record['source']], 'codex')

    def test_read_only_reopen_requires_existing_initialized_runtime(self):
        missing = self.root / 'missing-runtime'
        with self.assertRaisesRegex(ValueError, 'not initialized'):
            self.module.MemoryStore(missing, self.vault, read_only=True)
        self.assertFalse(missing.exists())

    def test_read_only_reopen_rejects_wrong_and_unbound_vault(self):
        other_vault = self.root / 'another-vault'
        other_vault.mkdir()
        with self.assertRaisesRegex(ValueError, 'another vault'):
            self.module.MemoryStore(self.state, other_vault, read_only=True)
        with self.store._connect() as db:
            db.execute("DELETE FROM metadata WHERE key='vault_root'")
        before = self.store.database.read_bytes()
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.module.MemoryStore(self.state, self.vault, read_only=True)
        self.assertEqual(self.store.database.read_bytes(), before)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission contract')
    def test_read_only_retrieval_preserves_runtime_permissions(self):
        self.store.ingest(self.record())
        self.store.database.chmod(0o400)
        self.state.chmod(0o500)
        try:
            read_only = self.module.MemoryStore(self.state, self.vault, read_only=True)
            self.assertEqual(len(read_only.retrieve('Quartz observatory calibration')['records']), 1)
            self.assertEqual(self.store.database.stat().st_mode & 0o777, 0o400)
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o500)
        finally:
            self.state.chmod(0o700)
            self.store.database.chmod(0o600)

    def test_receipt_idempotency_and_collision(self):
        rec = self.record()
        first = self.store.submit_receipt('synthetic-event', 'Calibration reviewed.', [rec['source']], 'codex')
        second = self.store.submit_receipt('synthetic-event', 'Calibration reviewed.', [rec['source']], 'claude')
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.store.submit_receipt('synthetic-event', 'Different content.', [rec['source']], 'codex')
        evaluator.close_store(self.store)
        self.store = self.module.MemoryStore(self.state, self.vault)
        self.assertEqual(first, self.store.submit_receipt('synthetic-event', 'Calibration reviewed.', [rec['source']], 'codex'))

    def test_task_revision_conflict_preserves_winner(self):
        self.store.ingest(self.record())
        updated = self.store.update_task('record', 1, {'status': 'waiting', 'facts': {'owner': 'New Reviewer'}})
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(ValueError):
            self.store.update_task('record', 1, {'status': 'done'})
        current = self.retrieve()['records'][0]
        self.assertEqual(current['status'], 'waiting')
        self.assertEqual(current['facts']['owner'], 'New Reviewer')
        self.assertEqual(current['revision'], 2)

    def test_concurrent_task_updates_have_one_winner(self):
        self.store.ingest(self.record())
        def update(status):
            try:
                return self.store.update_task('record', 1, {'status': status})
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ['waiting', 'done']))
        self.assertEqual(sum(r is not None for r in outcomes), 1)
        self.assertEqual(self.retrieve()['records'][0]['revision'], 2)

    def test_invalid_source_and_receipt_refs_rejected(self):
        record = self.record()
        outside = self.root / 'outside.md'
        outside.write_text('Synthetic outside source')
        (self.vault / 'outside-link.md').symlink_to(outside)
        for source in ['missing.md', '../outside.md', str(outside), 'outside-link.md']:
            with self.subTest(source=Path(source).name):
                with self.assertRaises(ValueError):
                    self.store.ingest(dict(record, source=source))
                with self.assertRaises(ValueError):
                    self.store.submit_receipt('invalid-' + Path(source).name, 'Synthetic summary', [source], 'codex')
        self.assertEqual(self.retrieve()['records'], [])

    def test_state_cannot_be_inside_vault_or_symlink_inside(self):
        for state in [self.vault, self.vault / '.runtime']:
            with self.assertRaises(ValueError):
                self.module.MemoryStore(state, self.vault)
        (self.vault / 'nested').mkdir()
        (self.root / 'alias').symlink_to(self.vault / 'nested', target_is_directory=True)
        with self.assertRaises(ValueError):
            self.module.MemoryStore(self.root / 'alias', self.vault)

    def test_private_untrusted_public_boundaries(self):
        for id, metadata in [('private', {'visibility': 'private'}), ('untrusted', {'kind': 'untrusted'}),
                             ('internal', {}), ('public', {'visibility': 'public'})]:
            self.store.ingest(self.record(id, text='Quartz observatory ' + ('SYNTHETIC_PRIVATE_CANARY' if id == 'private' else id), **metadata))
        for audience in ['internal', 'public']:
            response = self.retrieve(audience=audience)
            self.assertNotIn('SYNTHETIC_PRIVATE_CANARY', json.dumps(response))
            ids = [r['id'] for r in response['records']]
            self.assertNotIn('private', ids)
            self.assertNotIn('untrusted', ids)
            if audience == 'public':
                self.assertEqual(ids, ['public'])

    def test_truncation_is_bounded_and_source_cited(self):
        self.store.ingest(self.record(text='Quartz observatory calibration. ' * 150))
        response = self.retrieve(budget_chars=1000)
        self.assertTrue(response['truncated'])
        self.assertTrue(response['records'], 'Large source should be clipped usefully, not silently omitted')
        record = response['records'][0]
        self.assertLessEqual(sum(len(r['text']) for r in response['records']), 1000)
        self.assertTrue(any(marker in record['text'].lower() for marker in ['truncat', 'kırp', 'kirp', '…', '[...']),'Explicit clipping marker required')
        self.assertEqual(record['source'], 'notes/record.md')
        self.assertIn('notes/record.md', json.dumps(response['citations']))
        self.assertEqual(record['facts'], {'owner': 'Synthetic Reviewer'})
        serialized_size = sum(len(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':'))) for r in response['records'] + response['citations'])
        self.assertLessEqual(serialized_size, 1000)
        self.assertEqual(response['used_chars'], serialized_size)

    def test_limits_and_omission_are_visible(self):
        for i in range(8):
            self.store.ingest(self.record(str(i)))
        response = self.retrieve(limit=3)
        self.assertEqual(len(response['records']), 3)
        self.assertGreaterEqual(response['omitted_count'], 5)
        self.assertTrue(response['truncated'])

    def test_supersession_out_of_order(self):
        newest = self.record('new', supersedes=['old'], facts={'owner': 'Current Reviewer'})
        self.store.ingest(newest)
        self.store.ingest(self.record('old', facts={'owner': 'Former Reviewer'}, updated_at='2026-09-01T10:00:00Z'))
        ids = [r['id'] for r in self.retrieve()['records']]
        self.assertIn('new', ids)
        self.assertNotIn('old', ids)

    def test_rejected_inferences_are_history_only_across_context_paths(self):
        rejected = self.record('rejected', kind='inference', validity='rejected',
                               rejected_reason='User corrected this inference.',
                               rejected_at='2026-09-24', text='Synthetic user prefers amber diagrams.')
        legacy = self.record('legacy', kind='preference', status='rejected',
                             text='Synthetic user prefers amber diagrams.')
        current = self.record('current', kind='preference', validity='current',
                              text='Synthetic user prefers amber diagrams.')
        for record in (rejected, legacy, current):
            self.store.ingest(record)
        query = 'prefers amber diagrams'
        for result in (self.store.retrieve(query), self.store.retrieve(query, strict=True),
                        self.store.retrieve(query, statuses=['rejected'])):
            self.assertNotIn('rejected', [row['id'] for row in result['records']])
            self.assertNotIn('legacy', [row['id'] for row in result['records']])
        for result in (self.store.retrieve(query), self.store.retrieve(query, strict=True)):
            self.assertIn('current', [row['id'] for row in result['records']])
        self.assertEqual([row['id'] for row in self.store.candidates(query)], ['current'])
        self.assertNotIn('rejected', [row['id'] for row in self.store.snapshot_context()['records']])
        self.assertNotIn('legacy', [row['id'] for row in self.store.snapshot_context()['records']])
        self.assertIn('current', [row['id'] for row in self.store.snapshot_context()['records']])
        history = self.store.history('rejected')
        self.assertEqual(history[-1]['record']['rejected_reason'], 'User corrected this inference.')
        self.assertEqual(history[-1]['record']['rejected_at'], '2026-09-24')
        self.assertEqual(history[-1]['record']['validity'], 'rejected')

    def test_rejection_does_not_change_task_lifecycle_or_invalid_metadata(self):
        self.store.ingest(self.record('cancelled', status='cancelled'))
        self.assertEqual([row['id'] for row in self.retrieve()['records']], ['cancelled'])
        with self.assertRaisesRegex(ValueError, 'inference validity'):
            self.store.ingest(self.record('typo', kind='inference', validity='rejecetd'))
        with self.assertRaisesRegex(ValueError, 'rejected_reason'):
            self.store.ingest(self.record('missing-reason', kind='inference', validity='rejected',
                                          rejected_at='2026-09-24'))
        for invalid in ('2026-02-30', '2026-09-24T25:00:00Z', '2026-09-24T24:00:00', '2026-09-24T10',
                        '2026-09-24T10:00:00UTC', '2026-W39-4', '20260924', 'yesterday'):
            with self.assertRaisesRegex(ValueError, 'rejected_at'):
                self.store.ingest(self.record('invalid-date', kind='inference', validity='rejected',
                                              rejected_reason='User corrected it.', rejected_at=invalid))

    def test_rejected_at_accepts_timestamps_like_updated_at(self):
        for index, stamp in enumerate(('2026-09-24T10:00:00Z', '2026-09-24T13:00:00+03:00', '2026-09-24 10:00')):
            record_id = 'stamped-' + str(index)
            self.store.ingest(self.record(record_id, kind='inference', validity='rejected',
                                          rejected_reason='User corrected it.', rejected_at=stamp,
                                          text='Synthetic user prefers amber diagrams.'))
            self.assertNotIn(record_id, [row['id'] for row in self.store.retrieve('prefers amber diagrams')['records']])
            self.assertEqual(self.store.history(record_id)[-1]['record']['rejected_at'], stamp)

    def test_history_keeps_visibility_and_current_source_hash_boundary(self):
        self.store.ingest(self.record('private', kind='inference', validity='rejected',
                                      rejected_reason='User corrected it.', rejected_at='2026-09-24',
                                      visibility='private', text='Synthetic private preference canary.'))
        self.assertEqual(self.store.history('private'), [])
        self.assertEqual(len(self.store.history('private', audience='private')), 1)
        self.store.update_task('private', 1, {'visibility': 'internal'})
        self.assertEqual([event['revision'] for event in self.store.history('private')], [2])
        self.store.ingest(self.record('stale', kind='inference', validity='rejected',
                                      rejected_reason='User corrected it.', rejected_at='2026-09-24',
                                      text='Synthetic stale preference canary.'))
        self.assertEqual(len(self.store.history('stale')), 1)
        (self.vault / 'notes/stale.md').write_text('Source changed after indexing.', encoding='utf-8')
        self.assertEqual(self.store.history('stale'), [])

    def test_history_with_missing_current_source_fails_closed(self):
        self.store.ingest(self.record('missing-source', kind='inference', validity='rejected',
                                      rejected_reason='User corrected it.', rejected_at='2026-09-24'))
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', ('missing-source',)).fetchone()
            record = json.loads(row[0])
            record.pop('source')
            db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(record), 'missing-source'))
        self.assertEqual(self.store.history('missing-source'), [])

    def test_history_without_current_row_or_delete_event_fails_closed(self):
        self.store.ingest(self.record('orphan'))
        with self.store._connect() as db:
            db.execute('DELETE FROM records WHERE id=?', ('orphan',))
        self.assertEqual(self.store.history('orphan'), [])

    def test_deleted_source_not_retrieved_as_verified_fact(self):
        self.store.ingest(self.record())
        (self.vault / 'notes/record.md').unlink()
        response = self.retrieve()
        self.assertEqual(response['records'], [])
        self.assertTrue(response['abstained'])

    def test_runtime_state_is_bound_to_original_vault(self):
        self.store.ingest(self.record())
        other = self.root / 'different-vault'
        other.mkdir()
        evaluator.close_store(self.store)
        with self.assertRaises(ValueError):
            self.module.MemoryStore(self.state, other)
        self.store = self.module.MemoryStore(self.state, self.vault)
        self.assertTrue(self.retrieve()['records'])

    def test_changed_source_hash_is_stale_not_verified(self):
        self.store.ingest(self.record())
        (self.vault / 'notes/record.md').write_text('Quartz calibration cancelled. Synthetic correction.', encoding='utf-8')
        response = self.retrieve()
        self.assertEqual(response['records'], [])
        self.assertTrue(response['abstained'])
        self.assertEqual(response['stale_count'], 1)
        self.assertNotIn('Synthetic Reviewer', json.dumps(response))

    def test_history_ordered_ingest_and_update_snapshots(self):
        initial = self.store.ingest(self.record())
        updated = self.store.update_task('record', 1, {'status': 'waiting', 'facts': {'owner': 'Updated Reviewer'}})
        completed = self.store.update_task('record', 2, {'status': 'done'})
        history = self.store.history('record')
        self.assertEqual(len(history), 3)
        self.assertEqual([e['event_type'] for e in history], ['ingest', 'update', 'update'])
        self.assertEqual([e['revision'] for e in history], [1, 2, 3])
        self.assertEqual([e['record_id'] for e in history], ['record'] * 3)
        sequences = [e['sequence'] for e in history]
        self.assertTrue(all(type(n) is int and n > 0 for n in sequences))
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual([e['record'] for e in history], [initial, updated, completed])
        self.assertEqual(history[0]['record']['facts']['owner'], 'Synthetic Reviewer')
        self.assertEqual(history[0]['record']['status'], 'active')
        self.assertEqual(self.store.history('never-ingested'), [])

    def test_history_snapshots_immutable_to_caller(self):
        self.store.ingest(self.record())
        original = self.store.history('record')
        supplied = self.store.history('record')
        supplied[0]['record']['facts']['owner'] = 'Forged Caller Value'
        supplied[0]['event_type'] = 'forged'
        supplied.clear()
        self.assertEqual(self.store.history('record'), original)
        self.assertEqual(self.retrieve()['records'][0], original[0]['record'])

    def test_history_duplicate_ingest_and_rejected_writes_add_no_event(self):
        record = self.record()
        self.store.ingest(record)
        self.store.ingest(dict(record))
        self.assertEqual(len(self.store.history('record')), 1)
        self.store.update_task('record', 1, {'status': 'waiting'})
        before = self.store.history('record')
        with self.assertRaises(ValueError):
            self.store.update_task('record', 1, {'status': 'done'})
        with self.assertRaises(ValueError):
            self.store.ingest(dict(record, text='Conflicting same ID'))
        with self.assertRaises(ValueError):
            self.store.update_task('record', 2, {'source': 'missing-source.md'})
        self.assertEqual(self.store.history('record'), before)
        self.assertEqual(self.retrieve()['records'][0], before[-1]['record'])

    def test_history_persists_through_reopen_and_is_record_scoped(self):
        self.store.ingest(self.record())
        self.store.ingest(self.record('other'))
        self.store.update_task('record', 1, {'status': 'waiting'})
        before = self.store.history('record')
        self.assertEqual(len(before), 2)
        self.assertEqual(len(self.store.history('other')), 1)
        evaluator.close_store(self.store)
        self.store = self.module.MemoryStore(self.state, self.vault)
        self.assertEqual(self.store.history('record'), before)
        current = {r['id']: r for r in self.retrieve()['records']}['record']
        self.assertEqual(current, before[-1]['record'])

    def test_history_event_write_failure_rolls_back_projection(self):
        self.store.ingest(self.record())
        before = self.store.history('record')
        # Inject a real SQLite failure at the append boundary. Projection must
        # not commit if its matching durable event cannot be appended.
        database = next(p for p in self.state.rglob('*') if p.is_file() and p.read_bytes()[:16] == b'SQLite format 3\x00')
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TRIGGER synthetic_event_failure BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'synthetic event write failure'); END")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.update_task('record', 1, {'status': 'done'})
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.ingest(self.record('failed-new'))
        self.assertEqual(self.store.history('record'), before)
        self.assertEqual(self.store.history('failed-new'), [])
        response = self.retrieve()
        self.assertEqual([r['id'] for r in response['records']], ['record'])
        self.assertEqual(response['records'][0], before[-1]['record'])

    def test_history_concurrent_revision_conflict_adds_one_update(self):
        self.store.ingest(self.record())
        def update(status):
            try:
                return self.store.update_task('record', 1, {'status': status})
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ['waiting', 'done']))
        self.assertEqual(sum(r is not None for r in outcomes), 1)
        history = self.store.history('record')
        self.assertEqual(len(history), 2)
        self.assertEqual([e['revision'] for e in history], [1, 2])
        self.assertEqual(history[-1]['record'], next(r for r in outcomes if r is not None))
        self.assertEqual(self.retrieve()['records'][0], history[-1]['record'])

    def test_disabled_optional_provider_does_not_execute_factory(self):
        def forbidden():
            self.fail('Disabled provider factory executed')
        if hasattr(self.module, 'optional_provider'):
            self.assertIsNone(self.module.optional_provider(enabled=False, factory=forbidden))
        self.store.ingest(self.record())
        self.assertTrue(self.retrieve()['records'])


if __name__ == '__main__':
    unittest.main()
