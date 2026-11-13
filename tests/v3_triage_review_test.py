"""Due-review triage is informational, source-verified and read-only (#658)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]


class ReviewTriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        (self.vault / 'notes').mkdir(parents=True)
        self.env = inherited_env(PYTHONDONTWRITEBYTECODE='1', BEYIN_V3_NO_SPAWN='1')
        self.entry = ROOT / 'scripts/beyin_v3.py'

    def note(self, name, **metadata):
        p = self.vault / 'notes' / (name + '.md')
        p.write_text('---\n' + json.dumps(dict(id=name, review_at='2020-01-01', **metadata))
                     + '\n---\nSynthetic idea.\n', encoding='utf-8')
        return p

    def cli(self, *args):
        prefix = [sys.executable, str(self.entry)]
        if self.entry == ROOT / 'scripts/beyin_v3.py':
            prefix += ['--vault', str(self.vault), '--state', str(self.state)]
        p = subprocess.run(prefix + list(args), cwd=self.vault, env=self.env,
                           capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_due_review_visible_and_fresh_privacy_gates_apply(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                for p in (self.vault / 'notes').glob('*.md'):
                    p.unlink()
                self.note('old')
                self.note('private-canary', visibility='private')
                self.note('untrusted-canary', trust='untrusted')
                self.note('retired-canary', status='archived')
                self.note('superseded-canary')
                self.note('replacement', supersedes=['[[superseded-canary]]'])
                stale = self.note('stale-canary')
                self.cli('sync')
                if installed:
                    done = install(self.vault, self.state, self.env)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.entry = self.vault / 'beyin.py'
                stale.write_text(stale.read_text().replace('Synthetic idea.', 'Changed private detail.'), encoding='utf-8')
                before = {p: p.read_bytes() for p in (self.vault / 'notes').glob('*.md')}
                output = self.cli('triage')
                report = json.loads(output)
                self.assertEqual([r['source'] for r in report['review']['due']], ['notes/old.md', 'notes/replacement.md'])
                self.assertEqual(report['review']['due_count'], 2)
                self.assertEqual(report['review']['stale_count'], 1)
                self.assertTrue(any('review_at' in r for r in report['recommendations']))
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                # Check only the new review section; pre-existing digest has separate semantics.
                for marker in ('private-canary', 'untrusted-canary', 'retired-canary', 'superseded-canary', 'stale-canary'):
                    self.assertNotIn(marker, json.dumps(report['review']))
                if installed:
                    human = self.cli('triage', '--human')
                    self.assertIn('[Yeniden Bakilacak Notlar]', human)
                    self.assertIn('notes/old.md', human)

    def test_review_bounds_and_invalid_dates_match_doctor(self):
        for n in range(25):
            self.note(f'due-{n:02}')
        for name, value in (('future', '2999-01-01'), ('invalid', 'not-a-date')):
            p = self.note(name)
            p.write_text(p.read_text().replace('2020-01-01', value), encoding='utf-8')
        self.cli('sync')
        review = json.loads(self.cli('triage'))['review']
        self.assertEqual(review['due_count'], 25)
        self.assertEqual(len(review['due']), 20)
        self.assertTrue(review['truncated'])
        self.assertEqual(review['invalid_count'], 1)
        self.assertEqual(review['invalid'][0]['source'], 'notes/invalid.md')
        self.assertEqual(review['stale_count'], 0)

    def test_no_due_review_has_no_review_recommendation(self):
        self.cli('sync')
        report = json.loads(self.cli('triage'))
        self.assertEqual(report['review']['due_count'], 0)
        self.assertFalse(any('review_at' in r for r in report['recommendations']))


if __name__ == '__main__':
    unittest.main()
