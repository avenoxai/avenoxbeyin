"""The shared installed guidance keeps external project setup outside the vault."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
GUIDANCE = 'Dış proje kurulum hedefi'


class ExternalProjectBoundaryTest(unittest.TestCase):
    def test_install_and_reinstall_distribute_guidance_preserving_user_text(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            vault, state = base / 'vault', base / 'state'
            vault.mkdir()
            user_text = '# Kullanıcının talimatı\nKendi notlarımı koru.\n'
            (vault / 'AGENTS.md').write_text(user_text, encoding='utf-8')
            env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1')
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'scripts/install_v3.py'),
                     '--vault', str(vault), '--state', str(state)],
                    capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                agents = (vault / 'AGENTS.md').read_text(encoding='utf-8')
                self.assertTrue(agents.startswith(user_text))
                self.assertEqual(agents.count(GUIDANCE), 1)
                self.assertIn('@AGENTS.md', (vault / 'CLAUDE.md').read_text(encoding='utf-8'))
                for side in ('.agents', '.claude', '.kiro'):
                    with self.subTest(side=side):
                        skill = (vault / side / 'skills/beyin/SKILL.md').read_text(encoding='utf-8')
                        self.assertIn(GUIDANCE, skill)
                        self.assertIn('sandbox garantisi değildir', skill)
                        self.assertIn('yönetilen runtime/skill', skill)


if __name__ == '__main__':
    unittest.main()
