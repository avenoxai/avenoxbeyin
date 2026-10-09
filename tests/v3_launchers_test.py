import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'template/.claude/scripts/beyin_v3_launchers.py'


class LauncherTest(unittest.TestCase):
    def test_launcher_uses_vault_path_and_preserves_exit_status(self):
        self.assertTrue(MODULE.exists(), 'Portable launcher planner must be implemented')
        spec = importlib.util.spec_from_file_location('test_v3_launchers_module', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='beyin-launcher-') as temp:
            vault = Path(temp) / 'Beyin Ölçüm & Space'
            vault.mkdir()
            entry = vault / 'beyin.py'
            entry.write_text("from pathlib import Path\nimport json,sys\nPath(__file__).with_name('invoked.json').write_text(json.dumps(sys.argv[1:]))\nsys.exit(23)\n", encoding='utf-8')
            plans = module.plan_launchers(vault, Path(temp) / 'runtime')
            for name, body in plans.items():
                target = vault / name
                target.write_bytes(body)
            suffix = '.cmd' if os.name == 'nt' else ('.command' if sys.platform == 'darwin' else '.sh')
            launcher = next(vault / name for name in plans if name.endswith(suffix))
            cmd = subprocess.list2cmdline([str(launcher)]) if os.name == 'nt' else ['/bin/sh', str(launcher)]
            result = subprocess.run(cmd, shell=os.name == 'nt', cwd=temp, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 23, 'Launcher must not mask updater failures')
            self.assertEqual(json.loads((vault / 'invoked.json').read_text()), ['update'])
            if sys.platform.startswith('linux'):
                desktop = next(body.decode('utf-8') for name, body in plans.items() if name.endswith('.desktop'))
                self.assertIn('Terminal=true', desktop)
                self.assertIn(str(vault), desktop)


if __name__ == '__main__':
    unittest.main()
