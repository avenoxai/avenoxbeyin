"""An excluded directory withholds Markdown refs only.

A script or data file has no visibility record. Treating every path under the
directory as private hides receipts that do not describe a note. Real install,
real SQLite rows, synthetic fixtures.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

from beyin_v3_index import dir_key
from beyin_v3_projections import EXCLUDED_DIRS_ROW, _hidden_ref_sources


class ExcludedMarkdownGateTest(unittest.TestCase):
    def test_only_markdown_paths_under_an_excluded_directory_are_hidden(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE records(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE markdown_sources(id TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        ''')
        db.execute('INSERT INTO metadata VALUES (?,?)', (EXCLUDED_DIRS_ROW, json.dumps([dir_key('araclar')])))
        hidden = _hidden_ref_sources(db, ['araclar/arac.py', 'araclar/not.md', 'araclar/NOT.MD', 'araclar/veri.json'])
        self.assertEqual(hidden, {'araclar/not.md', 'araclar/NOT.MD'})


class InstalledExcludedMarkdownTest(unittest.TestCase):
    SCRIPT = 'Zirkonbetik kaydi.'
    NOTE = 'Zirkonarac notu kaydi.'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-excluded-md-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state, home = root / 'vault', root / 'state', root / 'home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                 PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        (self.vault / 'araclar').mkdir()
        (self.vault / 'araclar/arac.py').write_bytes(b'print(1)\n')
        (self.vault / 'araclar/not.md').write_bytes(b'Arac notu.\n')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def beyin(self, *arguments):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def test_a_script_ref_stays_visible_and_a_note_ref_stays_withheld(self):
        self.beyin('preferences', '--index-exclude-dir', 'araclar')
        self.beyin('sync')
        self.beyin('receipt', '--event-id', 'synthetic-script', '--summary', self.SCRIPT, '--ref', 'araclar/arac.py')
        self.beyin('receipt', '--event-id', 'synthetic-tree-note', '--summary', self.NOTE, '--ref', 'araclar/not.md')
        self.beyin('sync')
        items = {item['summary']: item for item in self.beyin('recap')['items']}
        self.assertEqual(items[self.SCRIPT]['refs'], ['araclar/arac.py'])
        self.assertIsNone(items[self.SCRIPT].get('refs_withheld'))
        self.assertEqual(items[self.NOTE]['refs'], [])
        self.assertEqual(items[self.NOTE]['refs_withheld'], {'private': 1, 'missing': 0})


if __name__ == '__main__':
    unittest.main()
