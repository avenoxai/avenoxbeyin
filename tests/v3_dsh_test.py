"""DeepSeek Harness istemci destegi: harness listesi, makbuz uyumu ve CLI kabulu.

DSH'nin shell hook'u yoktur; bu testler hook teslimi degil, istemcinin adinin
taninmasini ve eski surumlerle okunabilir makbuz yazimini dogrular.
"""
import json
from pathlib import Path
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python


class DshClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-dsh-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Beyin Test'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8'))
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes' / 'kaynak.md').write_text('# Kaynak\n\nKarar metni.\n', encoding='utf-8')

    def cli(self, *args):
        return run_python(self.vault / 'beyin.py', list(args), self.vault, self.env)

    def test_harness_is_listed_in_the_installed_engine(self):
        engine = (self.vault / '.claude' / 'scripts' / 'beyin_v3.py').read_text(encoding='utf-8')
        self.assertIn('"dsh"', engine, 'installed engine must accept the dsh harness')

    def test_receipt_with_dsh_is_written_forward_readable(self):
        result = self.cli(
            'receipt', '--harness', 'dsh', '--event-id', 'dsh-1',
            '--summary', 'DSH makbuz testi', '--ref', 'notes/kaynak.md',
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        sources = sorted((self.vault / 'receipts').glob('*.md'))
        self.assertEqual(len(sources), 1)
        frontmatter = sources[0].read_text(encoding='utf-8').split('---', 2)[1]
        metadata = json.loads(frontmatter)
        self.assertEqual(metadata['harness'], 'manual')
        self.assertEqual(metadata['client'], 'dsh')

    def test_context_accepts_dsh(self):
        result = self.cli('context', '--harness', 'dsh', 'kaynak')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
