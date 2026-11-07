#!/usr/bin/env python3
"""A private note whose metadata breaks must keep its receipts withheld; synthetic local fixtures only.

The receipt gate decides from the indexed record. A sync that cannot parse a source drops its
record, so without a stored trace the gate would read "no record" as "not private" and open the
receipt's summary to recap, the SessionStart hook and the global bridge. Real install, real
database rows.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

# Reads the real database the way the SessionStart hook does: read-only, without a sync first.
RECEIPT_READER = '''import json, sqlite3, sys
from pathlib import Path
vault, state = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(vault / '.claude/scripts'))
from beyin_v3_projections import latest_receipts, recent_receipts
db = sqlite3.connect((state / 'memory.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
try:
    latest = next((event['summary'] for _, event in latest_receipts(db, vault)), None)
    recap = {item['source']: [item['summary'], item['refs'], item.get('refs_withheld')] for item in recent_receipts(db, vault=vault)['items']}
finally:
    db.close()
print(json.dumps({'latest': latest, 'recap': recap}))
'''

PRIVATE_NOTE = '---\nvisibility: private\n---\nGizli not.\n'
# visibility stays in place; only an unrelated field is malformed.
BROKEN_NOTE = '---\nvisibility: private\ninvalid: [\n---\nGizli not.\n'


class UnreadablePrivateRefTest(unittest.TestCase):
    GENERAL, PRIVATE = 'Zirkongenel karar kaydi.', 'Zirkongizli ayrinti kaydi.'
    SOURCE = 'notlar/gizli.md'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='beyin-unreadable-refs-')
        root = Path(cls.tmp.name).resolve()
        cls.vault, cls.state, home = root / 'vault', root / 'state', root / 'home'
        cls.vault.mkdir()
        home.mkdir()
        cls.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative, text in (('notlar/genel.md', 'Genel not.\n'), (cls.SOURCE, PRIVATE_NOTE)):
            path = cls.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def beyin(self, *arguments):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def rows(self, query):
        db = sqlite3.connect(str(self.state / 'memory.sqlite3'))
        try:
            return [row[0] for row in db.execute(query)]
        finally:
            db.close()

    def unsynced(self):
        done = subprocess.run([sys.executable, '-c', RECEIPT_READER, str(self.vault), str(self.state)],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def session_start(self):
        done = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault', str(self.vault),
                               '--state', str(self.state), '--harness', 'claude'], cwd=self.vault, env=self.env,
                              input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'synthetic-unreadable',
                                                'prompt': 'Zirkon kayitlari'}).encode('utf-8'),
                              capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8')

    def assert_withheld(self, reason):
        reader = self.unsynced()
        self.assertEqual(reader['latest'], self.GENERAL, reason)
        source = 'receipts/' + hashlib.sha256(b'synthetic-private').hexdigest() + '.md'
        summary, refs, withheld = reader['recap'][source]
        self.assertEqual(summary, '[summary withheld: the receipt refers to a private or untrusted source]', reason)
        self.assertNotIn(self.PRIVATE, json.dumps(reader), reason)
        self.assertEqual([refs, withheld], [[], {'private': 1, 'missing': 0}], reason)
        opening = self.session_start()
        self.assertNotIn(self.PRIVATE, opening, reason)
        self.assertNotIn('gizli.md', opening, reason)

    def test_a_private_note_with_broken_metadata_keeps_its_receipt_withheld(self):
        self.beyin('sync')
        # The private receipt is the newest one, so only the gate keeps it out of the session.
        self.beyin('receipt', '--event-id', 'synthetic-general', '--summary', self.GENERAL, '--ref', 'notlar/genel.md')
        self.beyin('receipt', '--event-id', 'synthetic-private', '--summary', self.PRIVATE, '--ref', self.SOURCE)
        self.assertEqual(self.rows(f"SELECT source FROM markdown_sources WHERE source='{self.SOURCE}'"), [self.SOURCE])
        self.assert_withheld('the indexed private record withholds its receipt')

        # Broken metadata: sync drops the record but stores the source with the same transaction.
        (self.vault / self.SOURCE).write_bytes(BROKEN_NOTE.encode('utf-8'))
        sync = self.beyin('sync')
        self.assertEqual(sync['status'], 'degraded')
        self.assertIn(self.SOURCE, [warning['source'] for warning in sync['warnings']])
        self.assertEqual(self.rows(f"SELECT source FROM markdown_sources WHERE source='{self.SOURCE}'"), [])
        self.assert_withheld('a source the sync could not parse has unknown visibility: withheld')
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_unreadable_sources'"),
                         [json.dumps([self.SOURCE])])

        # Repaired: the record is back and decides for itself; the stored list is cleared.
        (self.vault / self.SOURCE).write_bytes(PRIVATE_NOTE.encode('utf-8'))
        self.assertEqual(self.beyin('sync')['status'], 'succeeded')
        self.assertEqual(self.rows(f"SELECT source FROM markdown_sources WHERE source='{self.SOURCE}'"), [self.SOURCE])
        self.assertEqual(self.rows("SELECT value FROM metadata WHERE key='index_unreadable_sources'"), [])
        self.assert_withheld('the repaired private record withholds its receipt again')


if __name__ == '__main__':
    unittest.main()
