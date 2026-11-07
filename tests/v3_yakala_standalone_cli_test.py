"""Actual single-file CLI contract; no GUI/listener or distribution-policy claim (#501)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'template/.claude/scripts/beyin_v3_yakala.py'


class StandaloneCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-standalone-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.cwd = self.root / 'neutral'
        self.home = self.root / 'home'
        for folder in (self.vault, self.state / 'yakala', self.cwd, self.home):
            folder.mkdir(parents=True)
        self.runner = self.state / 'yakala/beyin_v3_yakala.py'
        self.runner.write_bytes(SOURCE.read_bytes())
        (self.vault / '.beyin-runtime.json').write_text(
            json.dumps({'state': str(self.state)}), encoding='utf-8')
        (self.vault / 'unrelated.md').write_bytes(b'# Unrelated\r\nKeep these bytes.\r\n')
        self.original = self.snapshot(self.vault)
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                                 XDG_STATE_HOME=str(self.root / 'xdg'),
                                 LOCALAPPDATA=str(self.home / 'local'), APPDATA=str(self.home / 'roaming'),
                                 PATH='', BEYIN_UPDATES_OFF='1', BEYIN_V3_NO_SPAWN='1')
        self.env.pop('PYTHONPATH', None)
        self.env.pop('PYTHONHOME', None)

    @staticmethod
    def snapshot(root):
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob('*') if p.is_file()}

    def run_cli(self, *args):
        # -I -S excludes cwd, script directory, PYTHONPATH and installed site packages.
        result = subprocess.run([sys.executable, '-I', '-S', str(self.runner), *args],
                                cwd=self.cwd, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout

    def command(self, *args):
        return json.loads(self.run_cli('--vault', str(self.vault), '--json', *args))

    def test_copied_cli_offline_text_capture_list_process_finish(self):
        text = 'Sentetik kaynak: bağımsız kopya çevrimdışı çalışır.'
        captured = self.command('ekle', '--metin', text, '--baslik', 'Tek dosya')
        self.assertEqual(captured['status'], 'yakalandi')
        ident = captured['id']
        card = self.vault / captured['path']
        listing = self.command('liste')
        self.assertEqual([(r['id'], r['durum']) for r in listing['kartlar']], [(ident, 'bekliyor')])
        self.assertEqual(listing['bekleyen'], 1)
        processed = self.command('isle', ident, '--ses-yok')
        self.assertEqual(processed['islenen'], 1)
        row = processed['kartlar'][0]
        self.assertEqual((row['id'], row['durum']), (ident, 'cikarildi'))
        raw = self.vault / row['ham']
        self.assertIn(text, raw.read_text(encoding='utf-8'))
        self.assertIn('visibility: private', raw.read_text(encoding='utf-8'))
        finished = self.command('bitti', ident, '--ozet', 'Sentetik inceleme tamamlandı.')
        self.assertEqual((finished['status'], finished['id']), ('islendi', ident))
        self.assertEqual(self.command('liste', '--durum', 'islendi')['kartlar'][0]['id'], ident)
        self.assertEqual(self.command('liste')['bekleyen'], 0)
        self.assertIn('Sentetik inceleme tamamlandı.', card.read_text(encoding='utf-8'))
        self.assertIn('visibility: "private"', card.read_text(encoding='utf-8'))
        for name, content in self.original.items():
            self.assertEqual((self.vault / name).read_bytes(), content)
        self.assertEqual(list(self.runner.parent.glob('*.py')), [self.runner])
        self.assertEqual(self.runner.read_bytes(), SOURCE.read_bytes())

    def test_copied_cli_help_and_empty_listing_do_not_require_available_vault(self):
        missing = self.root / 'not-mounted'
        before = self.snapshot(self.root)
        self.assertIn('ekle', self.run_cli('--help'))
        self.assertIn('--metin', self.run_cli('ekle', '--help'))
        result = json.loads(self.run_cli('--vault', str(missing), '--json', 'liste'))
        self.assertEqual(result, {'status': 'tamam', 'kartlar': [], 'bekleyen': 0})
        self.assertFalse(missing.exists())
        self.assertEqual(self.snapshot(self.root), before)


if __name__ == '__main__':
    unittest.main()
