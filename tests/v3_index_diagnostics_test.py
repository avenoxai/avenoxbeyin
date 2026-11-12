"""Doctor must distinguish an unreadable index report from an empty index (#587)."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python


class IndexDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-index-diagnostics-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = dict(isolated_env(self.root / 'home'), BEYIN_UPDATES_OFF='1')
        self.source = 'project/3rdparty/README.md'
        self.note = self.vault / self.source
        self.note.parent.mkdir(parents=True)
        self.note.write_bytes(b'Synthetic upstream note.\n')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def cli(self, *args):
        done = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b'')
        return done.stdout.decode('utf-8')

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db, db:
            return db.execute(query, parameters).fetchall()

    def test_corrupt_record_is_unavailable_without_leaking_content_or_rewriting_data(self):
        self.cli('sync')
        healthy = json.loads(self.cli('doctor'))['index_exclusions']
        self.assertEqual(healthy['vendored_hint']['notes'], 1)
        # A valid settings file must not be blamed for an unreadable database payload.
        settings = self.state / 'index.json'
        settings.write_bytes(b'{"excluded_dirs": ["target"]}\n')
        canary = '{SYNTHETIC_PRIVATE_RECORD_CANARY'
        self.sql('UPDATE records SET payload=? WHERE id=(SELECT id FROM markdown_sources WHERE source=?)',
                 (canary, self.source))
        notes_before, settings_before = self.note.read_bytes(), settings.read_bytes()
        for flags in ((), ('--json',)):
            with self.subTest(flags=flags):
                text = self.cli('doctor', *flags)
                result = json.loads(text)
                self.assertEqual(result['index_exclusions'],
                                 {'status': 'unavailable', 'error': 'JSONDecodeError'})
                self.assertIn('state_storage', result, 'the rest of doctor still runs')
                self.assertNotIn(canary, text)
        human = self.cli('doctor', '--human')
        self.assertIn('Indeks kapsami denetlenemedi (JSONDecodeError)', human)
        self.assertNotIn('index.json gecersiz', human)
        self.assertNotIn('her sey indeksleniyor', human)
        self.assertNotIn(canary, human)
        self.assertEqual(self.note.read_bytes(), notes_before)
        self.assertEqual(settings.read_bytes(), settings_before)
        self.assertEqual(self.sql('SELECT payload FROM records WHERE id='
                                 '(SELECT id FROM markdown_sources WHERE source=?)', (self.source,)), [(canary,)])

    def test_broken_index_schema_is_not_treated_as_missing_table(self):
        self.cli('sync')
        self.sql('ALTER TABLE markdown_sources RENAME COLUMN source TO broken_source')
        result = json.loads(self.cli('doctor'))
        self.assertEqual(result['index_exclusions'], {'status': 'unavailable', 'error': 'OperationalError'})
        self.assertIn('state_storage', result)
        self.assertIn('Indeks kapsami denetlenemedi (OperationalError)', self.cli('doctor', '--human'))
        columns = [row[1] for row in self.sql('PRAGMA table_info(markdown_sources)')]
        self.assertIn('broken_source', columns, 'doctor must not attempt to repair the index')
        self.assertNotIn('source', columns)

    def test_missing_markdown_table_keeps_the_empty_index_report(self):
        # A pre-sync database legitimately has no Markdown ownership table. The installer
        # may already have synced, so remove this derived table in the synthetic fixture.
        self.sql('DROP TABLE IF EXISTS markdown_sources')
        result = json.loads(self.cli('doctor'))['index_exclusions']
        self.assertEqual(result, {'file': 'index.json', 'excluded_dirs': [], 'status': 'none',
                                  'always_excluded': ['__pycache__', 'node_modules', 'receipts']})
        self.assertNotIn('Indeks kapsami denetlenemedi', self.cli('doctor', '--human'))
        self.assertEqual(self.sql("SELECT name FROM sqlite_master WHERE name='markdown_sources'"), [])

    def test_invalid_settings_keep_their_existing_diagnostic(self):
        self.cli('sync')
        settings = self.state / 'index.json'
        settings.write_bytes(b'{not valid JSON')
        result = json.loads(self.cli('doctor'))['index_exclusions']
        self.assertEqual((result['status'], result['error']), ('invalid', 'not valid JSON'))
        self.assertEqual(result['vendored_hint']['notes'], 1)
        human = self.cli('doctor', '--human')
        self.assertIn('index.json gecersiz (not valid JSON)', human)
        self.assertNotIn('Indeks kapsami denetlenemedi', human)
        self.assertEqual(settings.read_bytes(), b'{not valid JSON')


if __name__ == '__main__':
    unittest.main()
