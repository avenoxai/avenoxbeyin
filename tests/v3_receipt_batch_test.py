"""First-visible receipt batching must not cache privacy across generator yields."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_projections import latest_receipts


class ReceiptBatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='receipt-batch-')
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)
        (self.vault / 'receipts').mkdir()
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE records(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE markdown_sources(id TEXT PRIMARY KEY, source TEXT);
            CREATE INDEX source_lookup ON markdown_sources(source);
            CREATE TABLE receipts(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.scans = []
        self.db.set_trace_callback(lambda sql: self.scans.append(sql)
                                  if sql == 'SELECT source FROM markdown_sources' else None)

    def record(self, source, visibility='internal'):
        self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)',
                        (source, json.dumps({'source': source, 'visibility': visibility})))
        self.db.execute('INSERT OR REPLACE INTO markdown_sources VALUES (?,?)', (source, source))

    def receipt(self, number, ref='notes/public.md', session='selected'):
        ident = f'event-{number:03d}'
        event = {'event_id': ident, 'summary': ident, 'refs': [ref],
                 'created_at': '2026-10-31T12:00:00Z', 'session': session}
        self.db.execute('INSERT INTO receipts VALUES (?,?)', (ident, json.dumps(event)))
        path = self.vault / 'receipts' / (hashlib.sha256(ident.encode()).hexdigest() + '.md')
        path.write_text('Synthetic receipt.\n', encoding='utf-8')
        return path

    def test_newest_public_needs_only_one_alias_scan(self):
        self.record('notes/public.md')
        for number in range(200):
            self.receipt(number)
        source, event = next(latest_receipts(self.db, self.vault))
        self.assertEqual(event['event_id'], 'event-199')
        self.assertEqual(len(self.scans), 1)

    def test_many_hidden_predecessors_use_bounded_scans(self):
        for number in range(10000):
            self.record(f'notes/{number}.md', 'private' if number else 'public')
        for spelling in ('notes/1.md', 'NOTES/1.MD'):
            with self.subTest(spelling=spelling):
                self.db.execute('DELETE FROM receipts')
                for number in range(200):
                    self.receipt(number, spelling if number else 'notes/0.md')
                self.scans.clear()
                _, event = next(latest_receipts(self.db, self.vault))
                self.assertEqual(event['event_id'], 'event-000')
                self.assertLessEqual(len(self.scans), 5)

    def test_privacy_and_sources_are_rechecked_after_each_yield(self):
        changes = ('visibility', 'alias-index', 'unreadable', 'provenance',
                   'excluded-directory', 'excluded-script', 'missing-source', 'symlink-source')
        for change in changes:
            with self.subTest(change=change):
                for table in ('records', 'markdown_sources', 'receipts', 'metadata'):
                    self.db.execute('DELETE FROM ' + table)
                self.record('notes/public.md', 'public')
                if change not in ('alias-index', 'excluded-script'):
                    self.record('vendor/target.md', 'public')
                # First skip a private receipt so the first yielded public receipt
                # comes from an expanded batch that also inspected the next target.
                self.record('notes/hidden.md', 'private')
                self.receipt(4, 'notes/hidden.md')
                self.receipt(3)
                target = self.receipt(2, 'VENDOR/TARGET.MD' if change == 'alias-index'
                                      else 'vendor/target.py' if change == 'excluded-script'
                                      else 'vendor/target.md')
                self.receipt(1)
                reader = latest_receipts(self.db, self.vault)
                self.assertEqual(next(reader)[1]['event_id'], 'event-003')
                if change in ('visibility', 'alias-index'):
                    self.record('vendor/target.md', 'private')
                elif change == 'missing-source':
                    target.unlink()
                elif change == 'symlink-source':
                    replacement = self.vault / 'replacement.md'
                    replacement.write_text('Synthetic replacement.\n', encoding='utf-8')
                    target.unlink()
                    try:
                        target.symlink_to(replacement)
                    except OSError as exc:
                        self.skipTest('Symlinks unavailable: ' + str(exc))
                else:
                    keys = {'unreadable': 'index_unreadable_sources',
                            'provenance': 'receipt_view_sources',
                            'excluded-directory': 'index_excluded_dirs',
                            'excluded-script': 'index_excluded_dirs'}
                    values = ['vendor'] if change.startswith('excluded-') else ['vendor/target.md']
                    self.db.execute('INSERT INTO metadata VALUES (?,?)',
                                    (keys[change], json.dumps(values)))
                self.assertEqual([event['event_id'] for _, event in reader], ['event-001'])

    def test_sessions_order_and_missing_sources_stay_filtered(self):
        self.record('notes/public.md', 'public')
        self.receipt(5, session='other')
        self.receipt(4).unlink()
        self.receipt(3)
        self.receipt(2, session='other')
        self.receipt(1)
        self.assertEqual([event['event_id'] for _, event in latest_receipts(
            self.db, self.vault, {'selected'})], ['event-003', 'event-001'])


if __name__ == '__main__':
    unittest.main()
