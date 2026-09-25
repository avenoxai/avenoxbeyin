"""A local, bounded activity view over already recorded outcomes."""
from datetime import date
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'scripts'))

from beyin_v3_projections import recent_receipts
from beyin_entry import human_result


class RecentReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE receipts(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def add(self, ident, stamp, summary):
        self.db.execute('INSERT INTO receipts VALUES (?,?)', (ident, json.dumps({
            'event_id': ident, 'created_at': stamp, 'summary': summary,
            'refs': ['notes/source.md'],
        })))

    def test_utc_window_order_limit_and_source(self):
        self.add('old', '2026-09-18T23:59:59+00:00', 'Older outcome')
        self.add('first', '2026-09-19T10:00:00+00:00', 'First outcome')
        self.add('latest', '2026-09-25T00:10:00+02:00', 'Previous UTC day')
        self.add('newest', '2026-09-25T12:00:00Z', 'Newest outcome')
        result = recent_receipts(self.db, days=7, limit=2, today=date(2026, 9, 25))
        self.assertEqual((result['from'], result['through']), ('2026-09-19', '2026-09-25'))
        self.assertEqual(result['total'], 3)
        self.assertEqual([item['summary'] for item in result['items']],
                         ['Newest outcome', 'Previous UTC day'])
        self.assertTrue(result['truncated'])
        self.assertTrue(result['items'][0]['source'].startswith('receipts/'))
        self.assertEqual(result['items'][0]['refs'], ['notes/source.md'])

    def test_legacy_missing_timestamp_is_explicitly_omitted(self):
        self.db.execute('INSERT INTO receipts VALUES (?,?)',
                        ('legacy', json.dumps({'event_id': 'legacy', 'summary': 'Old format'})))
        result = recent_receipts(self.db, today=date(2026, 9, 25))
        self.assertEqual(result['items'], [])
        self.assertEqual(result['undated_omitted'], 1)
        self.assertIn('not independently verified', result['meaning'])
        self.assertIn('tarihsiz', human_result(result, 'recap'))

    def test_invalid_bounds_rejected(self):
        for days, limit in ((0, 20), (367, 20), (7, 0), (7, 101)):
            with self.subTest(days=days, limit=limit):
                with self.assertRaises(ValueError):
                    recent_receipts(self.db, days=days, limit=limit)

    def test_cli_returns_recorded_receipt_without_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            vault, state = Path(temp) / 'vault', Path(temp) / 'state'
            (vault / 'notes').mkdir(parents=True)
            (vault / 'notes/source.md').write_text('Source evidence.\n', encoding='utf-8')
            command = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                       '--vault', str(vault), '--state', str(state)]
            saved = subprocess.run(command + ['receipt', '--harness', 'codex'], input=json.dumps({
                'event_id': 'recap-smoke', 'summary': 'Prepared a source-linked demo.',
                'refs': ['notes/source.md'],
            }), capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(saved.returncode, 0, saved.stderr)
            read = subprocess.run(command + ['recap', '--days', '1'],
                                  capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(read.returncode, 0, read.stderr)
            result = json.loads(read.stdout)
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['items'][0]['summary'], 'Prepared a source-linked demo.')
            self.assertEqual(result['items'][0]['refs'], ['notes/source.md'])
            self.assertFalse(result['truncated'])


if __name__ == '__main__':
    unittest.main()
