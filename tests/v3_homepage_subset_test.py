"""Homepage subset reads resolve dependencies without widening returned records (#788)."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_homepages import create
from beyin_v3_sync import SyncEngine
from beyin_v3_web import Core, WebError
from v3_package_helpers import clean_environ


class HomepageSubsetTest(unittest.TestCase):
    def setUp(self):
        env = clean_environ()
        env.start()
        self.addCleanup(env.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(temp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)
        self.engine.note_create('notes/source.md', 'Synthetic observatory appointment.', {
            'id': 'appointment', 'project': 'observatory',
            'facts': {'date': '2026-12-01'}})
        create(self.engine, {'source': 'notes/Home.md', 'expected_sha256': None,
            'title': 'Observatory dashboard', 'project': 'observatory',
            'selections': [{'source': 'notes/source.md', 'fields': [
                {'field': 'facts.date', 'label': 'Date', 'editable': False}]}]})
        self.core = Core(self.vault, self.state)
        self.page = next(r for r in self.engine.store._records() if r['source'] == 'notes/Home.md')

    def test_search_hit_can_be_opened_and_subset_stays_narrow(self):
        hits = self.core.search('Observatory')['sonuclar']
        self.assertIn(self.page['id'], [r['id'] for r in hits])
        result = self.core.record(self.page['id'])
        self.assertIn('2026-12-01', result['metin'])
        self.assertEqual(result['kaynak'], 'notes/Home.md')
        eligible, stale = self.engine.store._eligible('internal', records=[self.page])
        self.assertEqual([r['id'] for r in eligible], [self.page['id']])
        self.assertEqual(stale, 0)
        self.assertEqual(self.core.record('appointment')['kaynak'], 'notes/source.md')

    def test_changed_or_missing_dependency_is_still_withheld(self):
        path = self.vault / 'notes/source.md'
        path.write_bytes(path.read_bytes() + b'\nchanged\n')
        for missing in (False, True):
            if missing:
                path.unlink()
            with self.subTest(missing=missing), self.assertRaises(WebError) as caught:
                self.core.record(self.page['id'])
            self.assertEqual(caught.exception.status, 404)

    def test_private_and_untrusted_dependency_is_still_withheld_after_sync(self):
        path = self.vault / 'notes/source.md'
        original = path.read_text(encoding='utf-8')
        from beyin_v3_sync import parse, render
        metadata, body = parse(original)
        for restriction in ({'visibility': 'private'}, {'trust': 'untrusted'}):
            with self.subTest(restriction=restriction):
                path.write_text(render({**metadata, **restriction}, body), encoding='utf-8')
                self.engine.sync()
                with self.assertRaises(WebError) as caught:
                    self.core.record(self.page['id'])
                self.assertEqual(caught.exception.status, 404)

    def test_superseded_dependency_is_still_withheld(self):
        self.engine.note_create('notes/replacement.md', 'New appointment.', {
            'id': 'replacement', 'project': 'observatory',
            'supersedes': ['appointment'], 'facts': {'date': '2026-12-02'}})
        with self.assertRaises(WebError) as caught:
            self.core.record(self.page['id'])
        self.assertEqual(caught.exception.status, 404)

    def test_plain_subset_does_not_fetch_full_table(self):
        record = next(r for r in self.engine.store._records() if r['id'] == 'appointment')
        with patch.object(self.engine.store, '_records', side_effect=AssertionError('unnecessary scan')):
            eligible, _ = self.engine.store._eligible('internal', records=[record])
        self.assertEqual([r['id'] for r in eligible], ['appointment'])


if __name__ == '__main__':
    unittest.main()
