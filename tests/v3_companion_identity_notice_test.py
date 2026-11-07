"""Core.md and Soul.md are alternative identity files: one alone is not a missing source."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_companion as companion

DIRECTORY = 'Companion'
NOTICE = 'Missing/unavailable companion sources:'


class IdentityNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='identity-notice-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        (self.vault / DIRECTORY).mkdir(parents=True)
        self.store = runtime.MemoryStore(root / 'state', self.vault)

    def write(self, name, text):
        source = f'{DIRECTORY}/{name}'
        (self.vault / source).write_text(text, encoding='utf-8')
        self.store.ingest(dict(id=source, source=source, text=text, kind='note', status='active',
                               updated_at='2026-10-01T00:00:00Z'))

    def seed(self, identity):
        for name in (identity, 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            self.write(name, f'# {name}\nSynthetic {name} body.\n')

    def notice(self):
        text = companion.context(self.store, 9000, '0' * 24, 'claude')
        return next((line for line in text.splitlines() if line.startswith(NOTICE)), '')

    def test_core_alone_reports_no_missing_soul(self):
        # A fresh install creates Core.md and never Soul.md.
        self.seed('Core.md')
        self.assertEqual(self.notice(), '')

    def test_soul_alone_reports_no_missing_core(self):
        # A V2 vault with Soul.md never gets a competing Core.md.
        self.seed('Soul.md')
        self.assertEqual(self.notice(), '')

    def test_both_identity_files_missing_are_still_reported(self):
        for name in ('Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            self.write(name, f'# {name}\nSynthetic {name} body.\n')
        self.assertEqual(self.notice(), NOTICE + ' Core.md, Soul.md.')

    def test_other_missing_files_are_still_reported(self):
        self.write('Core.md', '# Core\nSynthetic body.\n')
        self.assertEqual(self.notice(), NOTICE + ' Kurallar.md, Last-Session.md, Threads.md, Journal.md.')

    def test_changed_core_is_listed_without_soul(self):
        # A Core.md edited after the last sync is excluded as stale; Soul.md was never expected.
        self.seed('Core.md')
        (self.vault / DIRECTORY / 'Core.md').write_text('# Core.md\nEdited after sync.\n', encoding='utf-8')
        self.assertEqual(self.notice(), NOTICE + ' Core.md.')


if __name__ == '__main__':
    unittest.main()
