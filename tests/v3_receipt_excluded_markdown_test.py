"""An excluded directory withholds every ref below it, Markdown or not.

A path under an excluded directory has no record, so nothing says whether it is
private. A script or data file there can describe the same private work as a note,
so the gate covers every file type, in any case or Unicode spelling of the folder.
Real install, real SQLite rows, synthetic fixtures.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

from beyin_v3_index import dir_key
from beyin_v3_projections import EXCLUDED_DIRS_ROW, WITHHELD_SUMMARY, _hidden_ref_sources


class ExcludedTreeGateTest(unittest.TestCase):
    def test_every_path_under_an_excluded_directory_is_hidden(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE records(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE markdown_sources(id TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
        ''')
        db.execute('INSERT INTO metadata VALUES (?,?)', (EXCLUDED_DIRS_ROW, json.dumps([dir_key('araclar')])))
        below = ['araclar/arac.py', 'araclar/not.md', 'araclar/NOT.MD', 'araclar/veri.json', 'ARACLAR/alt/arac.sh']
        # Outside the tree, or a file merely named like the folder, is not below it.
        beside = ['notlar/arac.py', 'notlar/not.md', 'araclar.md', 'notlar/araclar']
        self.assertEqual(_hidden_ref_sources(db, below + beside), set(below))

    def test_unicode_aliases_under_excluded_directory_hide_every_file_type(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO metadata VALUES (?,?)',
                   (EXCLUDED_DIRS_ROW, json.dumps([dir_key('Arçlar-İ-I')])))
        alias = unicodedata.normalize('NFD', 'ARÇLAR-i-ı')
        refs = {alias + '/NOT.MD', alias + '/ARAC.PY', alias + '\\veri.json'}
        self.assertEqual(_hidden_ref_sources(db, sorted(refs)), refs)


class InstalledExcludedTreeTest(unittest.TestCase):
    SCRIPT = 'Zirkonbetik kaydi.'
    NOTE = 'Zirkonarac notu kaydi.'
    OUTSIDE = 'Zirkondis betik kaydi.'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-excluded-tree-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state, home = root / 'vault', root / 'state', root / 'home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                 BEYIN_UPDATES_OFF='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative, data in (('araclar/arac.py', b'print(1)\n'), ('araclar/not.md', b'Arac notu.\n'),
                               ('notlar/arac.py', b'print(2)\n')):
            (self.vault / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / relative).write_bytes(data)
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def beyin(self, *arguments):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def test_script_and_note_refs_under_an_excluded_directory_stay_withheld(self):
        self.beyin('preferences', '--index-exclude-dir', 'araclar')
        self.beyin('sync')
        self.beyin('receipt', '--event-id', 'synthetic-script', '--summary', self.SCRIPT, '--ref', 'araclar/arac.py')
        self.beyin('receipt', '--event-id', 'synthetic-tree-note', '--summary', self.NOTE, '--ref', 'araclar/not.md')
        self.beyin('receipt', '--event-id', 'synthetic-outside', '--summary', self.OUTSIDE, '--ref', 'notlar/arac.py')
        self.beyin('sync')
        items = self.beyin('recap')['items']

        def item_for(event_id):
            # The receipt file name, not the summary: a private ref replaces the summary text.
            source = 'receipts/' + hashlib.sha256(event_id.encode()).hexdigest() + '.md'
            return next(item for item in items if item['source'] == source)

        withheld = {'private': 1, 'missing': 0}
        for event_id in ('synthetic-script', 'synthetic-tree-note'):
            with self.subTest(event_id=event_id):
                item = item_for(event_id)
                self.assertEqual((item['refs'], item.get('refs_withheld'), item['summary']), ([], withheld, WITHHELD_SUMMARY))
        outside = item_for('synthetic-outside')
        self.assertEqual((outside['refs'], outside.get('refs_withheld'), outside['summary']),
                         (['notlar/arac.py'], None, self.OUTSIDE))

        hook = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', 'claude'], cwd=self.vault, env=self.env,
                              input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'synthetic-tree',
                                                'prompt': 'Zirkon kayitlari'}).encode('utf-8'),
                              capture_output=True, timeout=120)
        self.assertEqual(hook.returncode, 0, hook.stderr.decode('utf-8', errors='replace'))
        opening = hook.stdout.decode('utf-8')
        self.assertIn(self.OUTSIDE, opening)
        self.assertNotIn(self.SCRIPT, opening, 'a receipt about an excluded script must not open a session')
        self.assertNotIn(self.NOTE, opening)
        self.assertNotIn('araclar/arac.py', opening)


if __name__ == '__main__':
    unittest.main()
