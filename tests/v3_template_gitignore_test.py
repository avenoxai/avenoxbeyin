"""Ensure template/.gitignore ignores every path checked by doctor's tracked_machine_files."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('template_gitignore_cli', ROOT / 'scripts/beyin_v3.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@unittest.skipUnless(shutil.which('git'), 'Git optional; executable unavailable')
class TemplateGitignoreTest(unittest.TestCase):
    def test_template_gitignore_ignores_all_machine_local_files(self):
        with tempfile.TemporaryDirectory(prefix='v3-template-gitignore-') as tmp:
            repo = Path(tmp) / 'vault'
            repo.mkdir()
            (repo / '.gitignore').write_bytes((ROOT / 'template/.gitignore').read_bytes())
            env = {k: v for k, v in inherited_env(PYTHONDONTWRITEBYTECODE='1').items()
                   if not k.startswith('GIT_')}
            init = subprocess.run(['git', '-C', str(repo), 'init'], env=env,
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(init.returncode, 0, init.stderr)
            missing = []
            for path in CLI.MACHINE_LOCAL_FILES:
                check = subprocess.run(
                    ['git', '-C', str(repo), 'check-ignore', '--no-index', '-q', '--', path],
                    env=env, capture_output=True, text=True, timeout=10,
                )
                if check.returncode != 0:
                    missing.append(path)
            self.assertEqual(missing, [], f'template/.gitignore does not ignore: {missing}')


if __name__ == '__main__':
    unittest.main()
