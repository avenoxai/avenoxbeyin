#!/usr/bin/env python3
"""Batch ingest must not keep records written before a later invalid item.

Each list item is its own transaction. An invalid late item used to return
exit 1 with an empty stdout while the earlier items stayed in the store.
The CLI validates the whole list before the first write and names the failing
index. A write-time error still reports which ids were already stored.
Synthetic vault, temporary state, no network.
"""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env, isolated_env


class IngestBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-ingest-batch-test-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        source = self.vault / 'knowledge/kaynak.md'
        source.parent.mkdir(parents=True)
        source.write_text('Nebula batch ingest source.\n', encoding='utf-8')
        self.cli = ROOT / 'scripts/beyin_v3.py'
        self.env = inherited_env(**isolated_env(root / 'home'), BEYIN_UPDATES_OFF='1')

    def ingest(self, payload):
        return subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault),
             '--state', str(self.state), '--json', 'ingest'],
            input=json.dumps(payload).encode('utf-8'),
            capture_output=True, env=self.env, timeout=120)

    def stored_ids(self):
        database = self.state / 'memory.sqlite3'
        if not database.is_file():
            return []
        db = sqlite3.connect(str(database))
        try:
            return sorted(row[0] for row in db.execute('SELECT id FROM records'))
        finally:
            db.close()

    def error_of(self, result):
        self.assertEqual(result.stdout.strip(), b'')
        self.assertNotEqual(result.returncode, 0)
        return json.loads(result.stderr.decode('utf-8').strip())

    def test_invalid_item_writes_nothing_and_reports_its_index(self):
        result = self.ingest([
            {'id': 'batch-first', 'source': 'knowledge/kaynak.md', 'text': 'X'},
            {'id': 'invalid'},
        ])
        error = self.error_of(result)
        self.assertEqual(self.stored_ids(), [])
        self.assertEqual(error['error'], 'ValueError')
        self.assertEqual(error['message'], 'record text required')
        self.assertEqual(error['failed_index'], 1)
        self.assertEqual(error['written_ids'], [])

    def test_duplicate_id_conflict_is_rejected_before_writes(self):
        result = self.ingest([
            {'id': 'batch-dup', 'source': 'knowledge/kaynak.md', 'text': 'A'},
            {'id': 'batch-dup', 'source': 'knowledge/kaynak.md', 'text': 'B'},
        ])
        error = self.error_of(result)
        self.assertEqual(error['error'], 'RevisionConflict')
        self.assertEqual(error['failed_index'], 1)
        self.assertEqual(error['written_ids'], [])
        self.assertEqual(self.stored_ids(), [])

    def test_source_removed_after_preflight_reports_committed_prefix(self):
        original_path = sys.path[:]
        module_name = 'beyin_v3_shared_engine'
        had_engine = module_name in sys.modules
        original_engine = sys.modules.get(module_name)

        def restore_imports():
            sys.path[:] = original_path
            if had_engine:
                sys.modules[module_name] = original_engine
            else:
                sys.modules.pop(module_name, None)

        self.addCleanup(restore_imports)
        spec = importlib.util.spec_from_file_location('beyin_batch_cli_subject', self.cli)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        engine = cli.load_engine()
        first_source = self.vault / 'knowledge/kaynak.md'
        original_source = first_source.read_bytes()
        second_source = self.vault / 'knowledge/second.md'
        second_source.write_bytes(b'Synthetic second source.\n')
        payload = [
            {'id': 'batch-first', 'source': 'knowledge/kaynak.md', 'text': 'First'},
            {'id': 'batch-second', 'source': 'knowledge/second.md', 'text': 'Second'},
        ]
        input_file = self.vault.parent / 'batch.json'
        input_file.write_text(json.dumps(payload), encoding='utf-8')
        original_input = input_file.read_bytes()
        real_ingest, real_validate = engine.MemoryStore.ingest, engine.MemoryStore._validate
        validated = []

        def validate(store, record, **kwargs):
            result = real_validate(store, record, **kwargs)
            validated.append(record['id'])
            return result

        def ingest(store, record):
            if record['id'] == 'batch-second':
                # Both inputs passed preflight, then the first committed. A real
                # filesystem change must still be rejected by ingest's own validation.
                self.assertEqual(validated, ['batch-first', 'batch-second', 'batch-first'])
                second_source.unlink()
            return real_ingest(store, record)

        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(sys, 'stdin', io.StringIO()), \
                patch.object(cli, 'load_engine', return_value=engine), \
                patch.object(engine.MemoryStore, '_validate', validate), \
                patch.object(engine.MemoryStore, 'ingest', ingest):
            error, code = cli.main(['--vault', str(self.vault), '--state', str(self.state),
                                    '--json', 'ingest', '--file', str(input_file)], return_result=True)
        self.assertEqual(code, 1)
        self.assertEqual(error['error'], 'ValueError')
        self.assertEqual(error['message'], 'source missing or outside vault')
        self.assertEqual(error['failed_index'], 1)
        self.assertEqual(error['written_ids'], ['batch-first'])
        self.assertEqual(self.stored_ids(), ['batch-first'])
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            self.assertEqual(list(db.execute('SELECT event_type,record_id FROM events ORDER BY sequence')),
                             [('ingest', 'batch-first')])
        self.assertEqual(first_source.read_bytes(), original_source)
        self.assertEqual(input_file.read_bytes(), original_input)
        self.assertFalse(second_source.exists())
        self.assertEqual(list((self.state / 'journal').glob('*')), [])

    def test_existing_id_conflict_writes_nothing(self):
        first = self.ingest({'id': 'already', 'source': 'knowledge/kaynak.md', 'text': 'A'})
        self.assertEqual(first.returncode, 0, first.stderr)
        result = self.ingest([
            {'id': 'batch-new', 'source': 'knowledge/kaynak.md', 'text': 'Y'},
            {'id': 'already', 'source': 'knowledge/kaynak.md', 'text': 'B'},
        ])
        error = self.error_of(result)
        self.assertEqual(error['error'], 'RevisionConflict')
        self.assertEqual(error['failed_index'], 1)
        self.assertEqual(error['written_ids'], [])
        self.assertEqual(self.stored_ids(), ['already'])

    def test_single_record_ingest_is_unchanged(self):
        result = self.ingest({'id': 'solo', 'source': 'knowledge/kaynak.md', 'text': 'X'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.decode('utf-8'))['id'], 'solo')
        self.assertEqual(self.stored_ids(), ['solo'])

    def test_valid_batch_still_writes_every_item(self):
        result = self.ingest([
            {'id': 'batch-one', 'source': 'knowledge/kaynak.md', 'text': 'X'},
            {'id': 'batch-two', 'source': 'knowledge/kaynak.md', 'text': 'Y'},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item['id'] for item in json.loads(result.stdout.decode('utf-8'))],
                         ['batch-one', 'batch-two'])
        self.assertEqual(self.stored_ids(), ['batch-one', 'batch-two'])


if __name__ == '__main__':
    unittest.main()
