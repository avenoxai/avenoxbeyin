"""Continuity gate equivalence and deterministic foreground source-read bounds."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_continuity as continuity


def full_scan_current(store, saved):
    """Original implementation retained as an independent compatibility oracle."""
    eligible = store._retrieve('', project=saved['project'], snapshot=True,
                               limit=100000, budget_chars=10000000)['records']
    by_id = {r['id']: r for r in eligible if not r.get('text_truncated')}
    records = []
    for ref in saved['refs']:
        if not isinstance(ref, dict):
            return []
        record = by_id.get(ref.get('id'))
        if record is None or record.get('project') != saved['project'] or any(record.get(k) != ref.get(k) for k in ('revision', 'source_sha256')):
            return []
        records.append(record)
    return records


class ContinuityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='v3-continuity-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name).resolve() / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(Path(temporary.name) / 'state', self.vault)

    def note(self, ident, **fields):
        record = dict(id=ident, source=ident + '.md', text='Quartz deployment rollback procedure.',
                      project='demo', updated_at='2026-10-09T00:00:00Z')
        record.update(fields)
        path = self.vault / record['source']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record['text'], encoding='utf-8')
        return self.store.ingest(record)

    def saved(self, *records, project='demo'):
        return dict(project=project, refs=[{k: row[k] for k in ('id', 'revision', 'source_sha256')}
                                         for row in records])

    def test_concrete_anchor_followup_skips_current_and_source_reads(self):
        target = self.note('target')
        self.note('unrelated')
        continuity.remember(self.store, 'claude', 'session', 'Quartz deployment rollback',
                            dict(records=[target]), now=100)
        local = runtime.pack_context([], budget_chars=5000)
        queries = ['WebSocket connection recovery strategy',
                   'check ' + ' '.join('novelword' + str(i) for i in range(17))]
        with patch.object(continuity, '_current', side_effect=AssertionError('unexpected current')) as current, \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected source read')) as read:
            for query in queries:
                with self.subTest(query=query):
                    context, inherited = continuity.resolve(self.store, 'claude', 'session', query,
                                                           local, budget_chars=5000, now=101)
                    self.assertIs(context, local)
                    self.assertFalse(inherited)
            current.assert_not_called()
            read.assert_not_called()

    def test_current_matches_full_scan_across_record_gates(self):
        rows = [self.note('normal'), self.note('public', visibility='public'),
                self.note('private', visibility='private'),
                self.note('untrusted-trust', trust='untrusted'),
                self.note('untrusted-boolean', trusted=False),
                self.note('untrusted-status', status='untrusted'),
                self.note('untrusted-kind', kind='untrusted'),
                self.note('rejected-validity', kind='inference', validity='rejected',
                          rejected_reason='Synthetic rejection', rejected_at='2026-10-09'),
                self.note('rejected-status', kind='preference', status='rejected'),
                self.note('superseded-visible'), self.note('superseded-private'),
                self.note('retired', status='Arşivlendi later'), self.note('stale'),
                self.note('other-project', project='other'), self.note('missing'),
                self.note('revision-changed'), self.note('hash-changed'),
                self.note('pretruncated', text_truncated=True),
                self.note('daily', source='daily/log.md'), self.note('untrusted-replacement-target'),
                self.note('stale-replacement-target'), self.note('invalid-path')]
        self.note('visible-replacement', supersedes=['[[superseded-visible]]'])
        self.note('private-replacement', visibility='private', project='other',
                  supersedes=['superseded-private'])
        self.note('untrusted-replacement', trust='untrusted', supersedes=['untrusted-replacement-target'])
        replacement = self.note('stale-replacement', supersedes=['stale-replacement-target'])
        (self.vault / replacement['source']).unlink()
        (self.vault / 'stale.md').write_text('Edited after indexing.', encoding='utf-8')
        (self.vault / 'missing.md').unlink()
        self.store.update_task('revision-changed', 1, {'title': 'Changed metadata'})
        (self.vault / 'hash-changed.md').write_text('New source bytes.', encoding='utf-8')
        changed = self.store.update_task('hash-changed', 1, {})
        # Isolate saved-hash equality: revision matches, but the saved hash is old.
        next(row for row in rows if row['id'] == 'hash-changed')['revision'] = changed['revision']
        # Simulate a damaged indexed source path without allowing the writer to create one.
        with self.store._connect() as db:
            row = next(row for row in rows if row['id'] == 'invalid-path')
            db.execute('UPDATE records SET payload=? WHERE id=?',
                       (runtime._json(dict(row, source='../outside.md')), row['id']))
        accepted = {'normal', 'public', 'daily', 'untrusted-replacement-target'}
        for row in rows:
            with self.subTest(record=row['id']):
                saved = self.saved(row)
                expected = full_scan_current(self.store, saved)
                self.assertEqual([r['id'] for r in expected], [row['id']] if row['id'] in accepted else [])
                self.assertEqual(continuity._current(self.store, saved), expected)
        saved = self.saved(rows[0], rows[1], rows[-4])
        self.assertEqual(continuity._current(self.store, saved), full_scan_current(self.store, saved))
        saved = self.saved(rows[0], rows[2])
        self.assertEqual(continuity._current(self.store, saved), [])

    def test_current_preserves_explicit_project_scope_and_exact_saved_project(self):
        tagged = self.note('tagged', source='Projects/tagged.md')
        unscoped = self.note('unscoped', project='')
        # Missing project is a supported indexed form.
        with self.store._connect() as db:
            unscoped.pop('project')
            db.execute('UPDATE records SET payload=? WHERE id=?', (runtime._json(unscoped), 'unscoped'))
        (self.vault / runtime.PROJECT_SCOPES_FILE).write_text(json.dumps(
            dict(folders={'Projects': 'other'}, shared_unscoped=True)), encoding='utf-8')
        for row, project, expected_ids in ((tagged, 'demo', ['tagged']),
                                          (unscoped, 'demo', []), (unscoped, None, ['unscoped'])):
            with self.subTest(record=row['id'], project=project):
                saved = self.saved(row, project=project)
                expected = full_scan_current(self.store, saved)
                self.assertEqual([r['id'] for r in expected], expected_ids)
                self.assertEqual(continuity._current(self.store, saved), expected)

    def test_continuation_reads_only_saved_sources_and_loads_records_once(self):
        refs = [self.note('ref-' + str(i)) for i in range(continuity.MAX_REFS)]
        for i in range(20):
            self.note('unrelated-' + str(i))
        saved = self.saved(*reversed(refs))
        expected = full_scan_current(self.store, saved)
        read_bytes = Path.read_bytes
        reads = []
        def counted(path):
            reads.append(path)
            return read_bytes(path)
        with patch.object(Path, 'read_bytes', counted), \
                patch.object(self.store, '_records', wraps=self.store._records) as records:
            self.assertEqual(continuity._current(self.store, saved), expected)
            records.assert_called_once_with()
        self.assertCountEqual(reads, [self.vault / row['source'] for row in refs])
        self.assertEqual(len(reads), continuity.MAX_REFS)

    def test_large_vault_anchor_no_longer_depends_on_snapshot_packing(self):
        # The previous whole-vault snapshot packed 10M characters before looking up the
        # saved id, so a large unrelated note could clip a valid anchor. Verification is
        # now per ref, with the same gates, and unrelated files no longer matter.
        target = self.note('a-target', text='.' * 6000000)
        blocker = self.note('z-blocker', text='.' * 6000000)
        saved = self.saved(target)
        self.assertEqual(full_scan_current(self.store, saved), [])
        self.assertEqual([r['id'] for r in continuity._current(self.store, saved)], ['a-target'])
        (self.vault / blocker['source']).write_text('Stale blocker.', encoding='utf-8')
        self.assertEqual(continuity._current(self.store, saved), full_scan_current(self.store, saved))

if __name__ == '__main__':
    unittest.main()
