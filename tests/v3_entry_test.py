"""Human entrypoint smoke: a novice sees an honest short status, tools retain JSON."""
import json
from pathlib import Path
import tempfile
import unittest
from v3_package_helpers import install, isolated_env, run_python


class EntryPresentationTest(unittest.TestCase):
    def test_human_doctor_and_machine_doctor_report_unverified_install(self):
        with tempfile.TemporaryDirectory(prefix='v3-entry-test-') as t:
            root = Path(t);vault=root/'vault';vault.mkdir();state=root/'state';env=isolated_env(root/'home')
            installed=install(vault,state,env)
            self.assertEqual(installed.returncode,0,installed.stderr)
            machine=run_python(vault/'beyin.py',['doctor'],vault,env)
            self.assertEqual(machine.returncode,0,machine.stderr)
            self.assertEqual(json.loads(machine.stdout)['status'],'never_seen')
            human=run_python(vault/'beyin.py',['doctor','--human'],vault,env)
            self.assertEqual(human.returncode,0,human.stderr)
            text=human.stdout.decode('utf-8')
            self.assertIn('3.0.0',text)
            self.assertIn('oturum',text.lower())
            self.assertNotIn('"hook-health.json"',text)
            self.assertNotIn(str(state),text)

    def test_heterogeneous_os_state_pin_falls_back_to_default_state(self):
        with tempfile.TemporaryDirectory(prefix='v3-cross-test-') as t:
            root = Path(t); vault = root / 'vault'; vault.mkdir(); state = root / 'state'; env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            import sys
            foreign = '/home/user/.local/state/beyin-v3/ab12' if sys.platform == 'win32' else 'C:\\\\Users\\\\user\\\\AppData\\\\Local\\\\beyin-v3\\\\ab12'
            (vault / '.beyin-runtime.json').write_text(json.dumps({'state': foreign, 'schema': 1}), encoding='utf-8')
            machine = run_python(vault / 'beyin.py', ['doctor'], vault, env)
            self.assertEqual(machine.returncode, 0, machine.stderr)
            payload = json.loads(machine.stdout)
            self.assertIn('status', payload)

    def test_semantic_unchanged_accepts_cross_platform_beyin_entry_fallback(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('install_v3', root / 'scripts/install_v3.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        baseline = b"        config = json.loads(config_path.read_text(encoding='utf-8'))\n        state = Path(config['state'])\n        directory = vault / '.claude/scripts'\n        sys.path.insert(0, str(directory))"
        fallback = b"        config = json.loads(config_path.read_text(encoding='utf-8'))\n        directory = vault / '.claude/scripts'\n        sys.path.insert(0, str(directory))\n        raw_state = config.get('state', '') if isinstance(config, dict) else ''\n        if not isinstance(raw_state, str) or not raw_state:\n            from beyin_v3_cli import default_state\n            state = default_state(vault)\n        elif sys.platform != 'win32' and ('\\\\' in raw_state or ':' in raw_state):\n            from beyin_v3_cli import default_state\n            state = default_state(vault)\n        elif sys.platform == 'win32' and raw_state.startswith('/'):\n            from beyin_v3_cli import default_state\n            state = default_state(vault)\n        else:\n            state = Path(raw_state)"
        arbitrary = b"        config = json.loads(config_path.read_text(encoding='utf-8'))\n        state = Path('/custom/modified/state')"
        self.assertTrue(installer.semantic_unchanged('beyin.py', baseline, fallback, []))
        self.assertTrue(installer.semantic_unchanged('beyin.py', fallback, baseline, []))
        self.assertFalse(installer.semantic_unchanged('beyin.py', baseline, arbitrary, []))


if __name__ == '__main__':
    unittest.main()
