#!/usr/bin/env python3
"""Missing receipt source files on disk avoid broken wikilinks in generated daily/outcomes views (#899)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from unittest.mock import patch

from v3_package_helpers import inherited_env, install
from v3_sync_test import load_module

ROOT = Path(__file__).resolve().parents[1]

WITHHELD_SUMMARY = '[summary withheld: the receipt refers to a private or untrusted source]'


class MissingReceiptSourceViewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-missing-receipt-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state, home = root / 'vault', root / 'state', root / 'home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                 PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative, text in (('notlar/genel.md', 'Genel karar notu.\n'),
                               ('notlar/gizli.md', '---\nvisibility: private\n---\nGizli kaynak.\n')):
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def beyin(self, *arguments, allow_nonzero=False):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        if not allow_nonzero:
            self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def _views_text(self):
        files = sorted((self.vault / 'daily/v3').glob('*.md')) + [self.vault / 'knowledge/v3/outcomes.md']
        self.assertTrue(all(path.is_file() for path in files) and len(files) > 1)
        return ''.join(path.read_text(encoding='utf-8') for path in files)

    def test_missing_receipt_file_avoids_broken_lint_links_and_recovers_when_restored(self):
        self.beyin('sync')
        r1 = self.beyin('receipt', '--event-id', 'synthetic-open-receipt',
                        '--summary', 'Acik makbuz ozeti.', '--ref', 'notlar/genel.md')
        receipt_rel = r1['source']
        receipt_path = self.vault / receipt_rel
        self.assertTrue(receipt_path.is_file())
        saved_bytes = receipt_path.read_bytes()

        self.beyin('sync')
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)
        self.assertIn(f'[[{receipt_rel}|synthetic-open-receipt]]', self._views_text())

        receipt_path.unlink()
        sync_missing = self.beyin('sync')
        self.assertEqual(sync_missing['status'], 'succeeded')
        warnings = [w for w in sync_missing.get('warnings', []) if w.get('kind') == 'missing_receipt_source']
        self.assertEqual(len(warnings), 1, sync_missing)
        self.assertEqual(warnings[0]['source'], receipt_rel)

        views_missing = self._views_text()
        self.assertNotIn(f'[[{receipt_rel}', views_missing)
        self.assertIn(f'`{receipt_rel}` (synthetic-open-receipt, file missing on disk)', views_missing)
        self.assertIn('Acik makbuz ozeti.', views_missing)
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)
        # A second sync with the file still missing leaves the generated views byte for byte.
        self.assertEqual(self.beyin('sync')['status'], 'succeeded')
        self.assertEqual(self._views_text(), views_missing)

        # Restoring the receipt file on disk automatically restores the wikilink and clears the warning.
        receipt_path.write_bytes(saved_bytes)
        sync_restored = self.beyin('sync')
        self.assertEqual(sync_restored['status'], 'succeeded')
        self.assertEqual([w for w in sync_restored.get('warnings', []) if w.get('kind') == 'missing_receipt_source'], [])
        self.assertIn(f'[[{receipt_rel}|synthetic-open-receipt]]', self._views_text())
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)

    def test_missing_private_ref_receipt_keeps_summary_and_event_id_withheld(self):
        self.beyin('sync')
        r_priv = self.beyin('receipt', '--event-id', 'synthetic-secret-event-canary',
                            '--summary', 'Gizli ozet sizmamali.', '--ref', 'notlar/gizli.md')
        receipt_rel = r_priv['source']
        (self.vault / receipt_rel).unlink()

        sync_missing = self.beyin('sync')
        self.assertEqual(sync_missing['status'], 'succeeded')
        views_missing = self._views_text()
        self.assertNotIn(f'[[{receipt_rel}', views_missing)
        self.assertNotIn('Gizli ozet sizmamali.', views_missing)
        self.assertNotIn('synthetic-secret-event-canary', views_missing)
        self.assertIn(WITHHELD_SUMMARY, views_missing)
        self.assertIn(f'`{receipt_rel}` (file missing on disk)', views_missing)
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)


class UnlistableReceiptsTest(unittest.TestCase):
    """A receipts/ listing error must not rewrite every view as missing and back on the next sync."""

    def setUp(self):
        scripts = str(ROOT / 'template/.claude/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            self.addCleanup(lambda: sys.path.remove(scripts))
        self.subject = load_module()
        tmp = tempfile.TemporaryDirectory(prefix='beyin-unlistable-receipts-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state = root / 'vault', root / 'state'
        note = self.vault / 'notes/task.md'
        note.parent.mkdir(parents=True)
        note.write_bytes(self.subject.render({'id': 'listing-task', 'kind': 'task', 'revision': 1, 'status': 'active'},
                                             'Synthetic listing task.\n').encode('utf-8'))
        self.engine = self.subject.SyncEngine(self.vault, self.state)
        self.engine.sync()
        self.engine.receipt('listing-event', 'Synthetic listing outcome.', ['notes/task.md'], 'codex')

    def test_listing_error_keeps_links_and_adds_no_missing_warning(self):
        import beyin_v3_projections as projections
        source = 'receipts/' + hashlib.sha256(b'listing-event').hexdigest() + '.md'
        real_listdir = projections.os.listdir

        def failing_listdir(path='.'):
            if Path(path).name == 'receipts':
                raise PermissionError('synthetic listing failure')
            return real_listdir(path)

        warnings = []
        with self.engine.store._connect() as db, patch.object(projections.os, 'listdir', failing_listdir):
            desired = projections._receipt_view_contents(self.engine, db, warnings)
        outcomes = desired['knowledge/v3/outcomes.md']
        self.assertIn(f'[[{source}|listing-event]]', outcomes)
        self.assertNotIn('file missing on disk', outcomes)
        self.assertEqual([w for w in warnings if w.get('kind') == 'missing_receipt_source'], [])


if __name__ == '__main__':
    unittest.main()
