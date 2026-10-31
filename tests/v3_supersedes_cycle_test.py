#!/usr/bin/env python3
"""A supersedes cycle retires nothing; every cyclic edge is reported and members stay searchable."""
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_passage_test import Vault, runtime  # noqa: E402

CYCLE_REASON = 'supersedes reference is part of a cycle'


class CyclePerformanceTest(unittest.TestCase):
    def test_6000_note_acyclic_chain_resolves_under_one_second(self):
        records = [{'id': 'n%d' % i, 'source': 'knowledge/n%d.md' % i,
                    'supersedes': ['n%d' % (i + 1)] if i < 5999 else []}
                   for i in range(6000)]
        started = perf_counter()
        retired, dead = runtime.resolve_supersedes(records)
        elapsed = perf_counter() - started
        self.assertEqual(retired, {'n%d' % i for i in range(1, 6000)})
        self.assertEqual(dead, [])
        self.assertLess(elapsed, 1.0, '6000-note chain took %.6fs' % elapsed)


class CycleResolutionTest(Vault):
    def test_two_note_cycle_retires_nothing_and_is_reported(self):
        self.ingest('a', 'Zirkon kovası ilk karar.', 'knowledge/a.md', supersedes=['b'])
        self.ingest('b', 'Zirkon kovası ikinci karar.', 'knowledge/b.md', supersedes=['a'])
        retired, dead = runtime.resolve_supersedes(self.store._records())
        self.assertEqual(retired, set())
        self.assertEqual({(item['id'], item['value'], item['reason']) for item in dead},
                         {('a', 'b', CYCLE_REASON), ('b', 'a', CYCLE_REASON)})
        ids = {record['id'] for record in self.store.retrieve('zirkon kovası karar')['records']}
        self.assertEqual(ids, {'a', 'b'})

    def test_three_note_cycle_retires_nothing_and_is_reported(self):
        self.ingest('a', 'Zirkon kovası ilk karar.', 'knowledge/a.md', supersedes=['b'])
        self.ingest('b', 'Zirkon kovası ikinci karar.', 'knowledge/b.md', supersedes=['c'])
        self.ingest('c', 'Zirkon kovası üçüncü karar.', 'knowledge/c.md', supersedes=['a'])
        retired, dead = runtime.resolve_supersedes(self.store._records())
        self.assertEqual(retired, set())
        self.assertEqual({(item['id'], item['value'], item['reason']) for item in dead},
                         {('a', 'b', CYCLE_REASON), ('b', 'c', CYCLE_REASON), ('c', 'a', CYCLE_REASON)})
        ids = {record['id'] for record in self.store.retrieve('zirkon kovası karar')['records']}
        self.assertEqual(ids, {'a', 'b', 'c'})

    def test_acyclic_chain_still_retires_unchanged(self):
        self.ingest('old', 'Zirkon kovası eski karar.', 'knowledge/old.md')
        self.ingest('new', 'Zirkon kovası yeni karar.', 'knowledge/new.md', supersedes=['old'])
        retired, dead = runtime.resolve_supersedes(self.store._records())
        self.assertEqual(retired, {'old'})
        self.assertEqual(dead, [])
        ids = {record['id'] for record in self.store.retrieve('zirkon kovası karar')['records']}
        self.assertEqual(ids, {'new'})

    def test_edge_into_a_cycle_from_outside_still_applies(self):
        self.ingest('a', 'Zirkon kovası ilk karar.', 'knowledge/a.md', supersedes=['b'])
        self.ingest('b', 'Zirkon kovası ikinci karar.', 'knowledge/b.md', supersedes=['a'])
        self.ingest('c', 'Zirkon kovası üçüncü karar.', 'knowledge/c.md', supersedes=['a'])
        retired, dead = runtime.resolve_supersedes(self.store._records())
        # c -> a is not on the cycle, so it still retires a; the a <-> b edges do nothing.
        self.assertEqual(retired, {'a'})
        self.assertEqual({(item['id'], item['reason']) for item in dead},
                         {('a', CYCLE_REASON), ('b', CYCLE_REASON)})
        ids = {record['id'] for record in self.store.retrieve('zirkon kovası karar')['records']}
        self.assertEqual(ids, {'b', 'c'})


class CycleSyncReportTest(unittest.TestCase):
    """The cycle reaches the sync report like other supersedes issues, members stay searchable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-supersedes-cycle-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()

    def note(self, rel, meta, body):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(meta, ensure_ascii=False) + '\n---\n' + body + '\n', encoding='utf-8')

    def test_sync_reports_the_cycle_and_context_finds_its_members(self):
        from beyin_v3_sync import SyncEngine
        self.note('knowledge/a.md', {'id': 'a', 'supersedes': ['b']}, 'Zirkon kovası ilk karar.')
        self.note('knowledge/b.md', {'id': 'b', 'supersedes': ['a']}, 'Zirkon kovası ikinci karar.')
        engine = SyncEngine(self.vault, self.root / 'state')
        result = engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['indexed'], 2)
        self.assertEqual({(item['id'], item['reason']) for item in result['supersedes_issues']},
                         {('a', CYCLE_REASON), ('b', CYCLE_REASON)})
        self.assertEqual(result['supersedes_issue_count'], 2)
        ids = {record['id'] for record in engine.store.context_for('claude', 'zirkon kovası karar')['records']}
        self.assertEqual(ids, {'a', 'b'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
