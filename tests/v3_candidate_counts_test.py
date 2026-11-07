"""Whole-note count reuse across hook processes. Synthetic sources only."""
import json
import itertools
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime


class CandidateCountsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.store = runtime.MemoryStore(root / 'state', self.vault)
        self.add('one', 'Quartz mirror calibration calibration. Kitapların metni.',
                 aliases=['observatory'], facts={'lens': 'blue'})
        self.add('two', 'Quartz calibration mirror mirror mirror.', source='telescope-guide.md')

    def add(self, ident, text, **extra):
        source = extra.pop('source', ident + '.md')
        (self.vault / source).write_text(text, encoding='utf-8')
        return self.store.ingest(dict(id=ident, source=source, text=text, title=ident,
                                      project='alpha', **extra))

    def reader(self):
        return runtime.MemoryStore(self.store.state_dir, self.vault, read_only=True)

    def cached_notes(self, call):
        real, seen = runtime._token_counts, []
        prefixes = tuple(record['text'] + ' ' for record in self.store._records())

        def spy(text):
            if text.startswith(prefixes):
                seen.append(text)
            return real(text)
        with patch.object(runtime, '_token_counts', side_effect=spy):
            result = call()
        return result, len(seen)

    def test_new_read_only_store_reuses_counts_without_tokenising_notes(self):
        expected, cold = self.cached_notes(lambda: self.store.candidates('quartz mirror'))
        self.assertGreater(cold, 0)
        actual, warm = self.cached_notes(lambda: self.reader().candidates('quartz mirror'))
        self.assertEqual(actual, expected)
        self.assertEqual(warm, 0)

    def test_query_mutations_and_filename_salience_do_not_pollute_counts(self):
        queries = ['quartz mirror', 'telescope guide', 'kitabım metin', 'observatory lens',
                   'what is the status', 'quartz mirror']
        for query in queries:
            with self.subTest(query=query):
                # Disable reuse only; the original count computation is the oracle.
                with patch.object(runtime._CountCache, 'counts', lambda _, text: runtime._token_counts(text)):
                    expected = self.reader().candidates(query)
                self.assertEqual(self.store.candidates(query), expected)
                self.assertEqual(self.reader().candidates(query), expected)

    def test_cached_counts_keep_first_seen_key_order(self):
        text = 'zeta alpha zeta beta calibration mirror'
        expected = runtime._token_counts(text)
        cache = runtime._CountCache(self.store.state_dir)
        self.assertEqual(list(cache.counts(text).items()), list(expected.items()))
        cache.save()
        actual = runtime._CountCache(self.store.state_dir).counts(text)
        self.assertEqual(list(actual.items()), list(expected.items()))

    def test_index_changes_invalidate_even_with_the_same_revision(self):
        self.store.candidates('quartz mirror')
        for field, value in [('text', 'Changed calibration observatory.'),
                             ('facts', {'lens': 'green'}), ('aliases', ['dome']),
                             ('title', 'New calibration'), ('source', 'quartz-mirror.md')]:
            with self.subTest(field=field):
                with self.store._connect() as db:
                    record = json.loads(db.execute('SELECT payload FROM records WHERE id=?', ('one',)).fetchone()[0])
                    record[field] = value
                    (self.vault / record['source']).write_text(record['text'], encoding='utf-8')
                    record['source_sha256'] = runtime.hashlib.sha256((self.vault / record['source']).read_bytes()).hexdigest()
                    db.execute('UPDATE records SET payload=? WHERE id=?', (runtime._json(record), 'one'))
                _, missed = self.cached_notes(lambda: self.store.candidates('quartz mirror'))
                self.assertGreater(missed, 0)

    def test_warm_cache_reapplies_freshness_visibility_trust_scope_and_supersession(self):
        self.store.candidates('quartz mirror')
        (self.vault / 'one.md').write_text('External edit.', encoding='utf-8')
        self.assertEqual([r['id'] for r in self.reader().candidates('quartz mirror')], ['two'])
        for field, value in [('visibility', 'private'), ('trust', 'untrusted'), ('project', 'beta')]:
            with self.subTest(field=field):
                with self.store._connect() as db:
                    record = json.loads(db.execute('SELECT payload FROM records WHERE id=?', ('two',)).fetchone()[0])
                    changed = dict(record, **{field: value})
                    db.execute('UPDATE records SET payload=? WHERE id=?', (runtime._json(changed), 'two'))
                self.assertEqual(self.reader().candidates('quartz mirror', project='alpha'), [])
                with self.store._connect() as db:
                    db.execute('UPDATE records SET payload=? WHERE id=?', (runtime._json(record), 'two'))
        self.add('new', 'Quartz mirror calibration replacement.', supersedes=['two'])
        self.assertNotIn('two', [r['id'] for r in self.reader().candidates('quartz mirror')])

    def test_read_only_miss_does_not_create_or_update_cache(self):
        path = self.store.state_dir / 'token-counts.json'
        self.reader().candidates('quartz mirror')
        self.assertFalse(path.exists())
        self.store.candidates('quartz mirror')
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.reader().candidates('telescope guide')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_cached_and_uncached_topical_retrieval_keep_all_gate_combinations(self):
        self.add('public', 'Quartz mirror calibration public guide.', visibility='public')
        self.add('private', 'Quartz mirror calibration personal guide.', visibility='private')
        self.add('waiting', 'Quartz mirror calibration supplier.', status='waiting')
        self.add('archived', 'Quartz mirror calibration legacy.', status='archived')
        self.add('untrusted', 'Quartz mirror calibration imported.', trust='untrusted')
        for project, audience, statuses, strict in itertools.product(
                (None, 'alpha', 'beta'), ('public', 'internal', 'private'),
                (None, ('active',), ('waiting',)), (False, True)):
            params = dict(project=project, audience=audience, statuses=statuses, strict=strict)
            with self.subTest(**params):
                with patch.object(runtime._CountCache, 'counts', lambda _, text: runtime._token_counts(text)):
                    expected = self.reader().candidates('quartz mirror', **params)
                self.assertEqual(self.store.candidates('quartz mirror', **params), expected)
                self.assertEqual(self.reader().candidates('quartz mirror', **params), expected)

    def test_invalid_missing_oversized_and_old_caches_fall_back(self):
        expected = self.store.candidates('quartz mirror')
        path = self.store.state_dir / 'token-counts.json'
        original = path.read_text(encoding='utf-8')
        invalid_entry = json.loads(original)
        invalid_entry['records'] = {key: {'quartz': True} for key in invalid_entry['records']}
        for blob in ['{', '[]', '{}', original.replace(runtime._counts_version(), 'old'),
                     json.dumps(invalid_entry)]:
            with self.subTest(blob_type=blob[:1]):
                path.write_text(blob, encoding='utf-8')
                actual, misses = self.cached_notes(lambda: self.reader().candidates('quartz mirror'))
                self.assertEqual(actual, expected)
                self.assertGreater(misses, 0)
        path.write_text(original, encoding='utf-8')
        with patch.object(runtime._CountCache, 'MAX_BYTES', 1):
            actual, misses = self.cached_notes(lambda: self.reader().candidates('quartz mirror'))
        self.assertEqual(actual, expected)
        self.assertGreater(misses, 0)

    def test_write_failure_only_costs_performance_and_cleans_temporary_file(self):
        expected = self.reader().candidates('quartz mirror')
        with patch.object(runtime.os, 'replace', side_effect=OSError('synthetic lock')):
            self.assertEqual(self.store.candidates('quartz mirror'), expected)
        self.assertFalse(list(self.store.state_dir.glob('.token-counts-*')))

    def test_deep_json_cache_is_a_miss_and_rebuilds_correctly(self):
        path = self.store.state_dir / 'token-counts.json'
        text = 'Quartz mirror calibration calibration.'
        expected_counts = runtime._token_counts(text)
        expected = self.reader().candidates('quartz mirror')
        for blob in ('', '{', '[' * 10000 + '0' + ']' * 10000):
            with self.subTest(depth=blob.count('[')):
                path.write_text(blob, encoding='utf-8')
                cache = runtime._CountCache(self.store.state_dir)
                self.assertEqual(cache.counts(text), expected_counts)
                actual, misses = self.cached_notes(lambda: self.reader().candidates('quartz mirror'))
                self.assertEqual(actual, expected)
                self.assertGreater(misses, 0)
                self.assertEqual(path.read_text(encoding='utf-8'), blob)
                self.assertEqual(self.store.candidates('quartz mirror'), expected)
                self.assertIsInstance(json.loads(path.read_text(encoding='utf-8')), dict)


if __name__ == '__main__':
    unittest.main()
