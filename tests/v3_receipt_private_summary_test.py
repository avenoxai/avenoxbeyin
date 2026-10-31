#!/usr/bin/env python3
"""A receipt that refers to a private source keeps its summary out of recap and the generated views.

Withholding the ref alone does not anonymize the summary: it usually describes that source. Real
install, real database rows and real daily/v3 and outcomes files; synthetic local fixtures only.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

WITHHELD_SUMMARY = '[summary withheld: the receipt refers to a private or untrusted source]'


class PrivateSummaryTest(unittest.TestCase):
    GENERAL, PRIVATE, LATER = 'Zirkongenel karar kaydi.', 'Zirkongizli ayrinti kaydi.', 'Zirkonsonradan gizli kayit.'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-private-summary-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.vault, self.state, home = root / 'vault', root / 'state', root / 'home'
        self.vault.mkdir()
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                 LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                 PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        for relative, text in (('notlar/genel.md', 'Genel not.\n'), ('notlar/gizli.md', '---\nvisibility: private\n---\nGizli.\n'),
                               ('notlar/sonra.md', 'Henuz acik not.\n')):
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def beyin(self, *arguments):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), *arguments, '--json'], cwd=self.vault,
                              env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def views(self):
        files = sorted((self.vault / 'daily/v3').glob('*.md')) + [self.vault / 'knowledge/v3/outcomes.md']
        self.assertTrue(all(path.is_file() for path in files) and len(files) > 1)
        return ''.join(path.read_text(encoding='utf-8') for path in files)

    def test_private_ref_withholds_the_summary_in_recap_and_views(self):
        self.beyin('sync')
        for event, summary, ref in (('synthetic-general', self.GENERAL, 'notlar/genel.md'),
                                    ('synthetic-private', self.PRIVATE, 'notlar/gizli.md'),
                                    ('synthetic-later', self.LATER, 'notlar/sonra.md')):
            self.beyin('receipt', '--event-id', event, '--summary', summary, '--ref', ref)
        self.beyin('sync')
        recap = self.beyin('recap')
        summaries = [item['summary'] for item in recap['items']]
        self.assertIn(self.GENERAL, summaries)
        self.assertIn(WITHHELD_SUMMARY, summaries)
        self.assertNotIn(self.PRIVATE, json.dumps(recap, ensure_ascii=False))
        text = self.views()
        self.assertIn(self.GENERAL, text)
        self.assertIn(WITHHELD_SUMMARY, text)
        self.assertNotIn(self.PRIVATE, text)
        self.assertIn(self.LATER, text)

        # The note becomes private after its receipt: the same sync that indexes the change
        # rewrites the generated views, not the next one.
        (self.vault / 'notlar/sonra.md').write_bytes('---\nvisibility: private\n---\nArtik gizli.\n'.encode('utf-8'))
        self.assertEqual(self.beyin('sync')['status'], 'succeeded')
        text = self.views()
        self.assertNotIn(self.LATER, text)
        self.assertNotIn(self.PRIVATE, text)
        self.assertIn(self.GENERAL, text)
        self.assertNotIn(self.LATER, json.dumps(self.beyin('recap'), ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
