#!/usr/bin/env python3
"""Missing receipt source files on disk avoid broken wikilinks in generated daily/outcomes views (#PR2)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

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
        self.assertIn(receipt_rel, views_missing)
        self.assertIn('Acik makbuz ozeti.', views_missing)
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)

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
        self.assertIn(receipt_rel, views_missing)
        self.assertEqual(self.beyin('lint', allow_nonzero=True)['broken_links_count'], 0)


if __name__ == '__main__':
    unittest.main()
