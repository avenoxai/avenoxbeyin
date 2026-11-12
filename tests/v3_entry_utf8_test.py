"""The installed human entry preserves source text in redirected output."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python


class InstalledHumanUtf8Test(unittest.TestCase):
    def test_recap_preserves_turkish_emoji_and_source_in_cp1252_redirects(self):
        with tempfile.TemporaryDirectory(prefix='v3-entry-utf8-') as tmp:
            root = Path(tmp)
            vault, state = root / 'vault', root / 'state'
            vault.mkdir()
            env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            source = 'notes/Çığ 😀.md'
            summary = 'İş tamamlandı; çığ ve şüphe 😀'
            path = vault / source
            path.parent.mkdir()
            path.write_text('# Kaynak\n', encoding='utf-8')
            receipt = run_python(vault / 'beyin.py',
                                 ['receipt', '--event-id', 'utf8-recap', '--summary', summary,
                                  '--ref', source], vault, env)
            self.assertEqual(receipt.returncode, 0, receipt.stderr)

            env = dict(env, PYTHONIOENCODING='cp1252:replace')
            command = [sys.executable, str(vault / 'beyin.py'), 'recap', '--human']
            for mode in ('pipe', 'file'):
                with self.subTest(mode=mode):
                    if mode == 'pipe':
                        done = subprocess.run(command, cwd=vault, env=env,
                                              capture_output=True, timeout=60)
                        output = done.stdout
                    else:
                        target_path = root / 'recap.txt'
                        with target_path.open('wb') as target:
                            done = subprocess.run(command, cwd=vault, env=env, stdout=target,
                                                  stderr=subprocess.PIPE, timeout=60)
                        output = target_path.read_bytes()
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn(summary.encode('utf-8'), output)
                    self.assertIn(source.encode('utf-8'), output)


if __name__ == '__main__':
    unittest.main()
