#!/usr/bin/env python3
"""Commands that never sync must not import the sync engine (#233).

Each command runs in its own interpreter, so sys.modules shows exactly what that command
imported. The sync engine is still loaded where a command really syncs or reads sync health.
"""
import json
from pathlib import Path
import tempfile
import unittest

from v3_package_helpers import ROOT, inherited_env, install, run_python

CLI = ROOT / 'scripts/beyin_v3.py'
HOOK = ROOT / 'template/.claude/scripts/beyin_v3_hook.py'

CLI_WRAPPER = '''import contextlib, importlib.util, io, json, sys
cli, vault, state = sys.argv[1:4]
spec = importlib.util.spec_from_file_location('cli_under_test', cli)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    code = module.main(['--vault', vault, '--state', state, *sys.argv[4:]])
sys.stdout.write('REPORT:' + json.dumps({'code': code, 'result': json.loads(buffer.getvalue()),
                                         'sync_imported': 'beyin_v3_sync' in sys.modules}))
'''

HOOK_WRAPPER = '''import contextlib, importlib.util, io, json, sys
hook, vault, state = sys.argv[1:4]
sys.path.insert(0, str(__import__('pathlib').Path(hook).parent))
spec = importlib.util.spec_from_file_location('hook_under_test', hook)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
sys.argv = ['hook', '--vault', vault, '--state', state, '--harness', 'claude']
sys.stdin = io.StringIO(json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'lazy-sync',
                                    'prompt': 'quartzlazyanchor karari neydi'}))
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    module.main()
sys.stdout.write('REPORT:' + json.dumps({'output': buffer.getvalue(),
                                         'sync_imported': 'beyin_v3_sync' in sys.modules}))
'''


class LazySyncImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='v3-lazy-sync-')
        root = Path(cls.tmp.name)
        cls.root, cls.vault, cls.state = root, root / 'vault', root / 'state'
        cls.vault.mkdir()
        home = root / 'home'
        home.mkdir()
        cls.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr
        (cls.vault / 'notes').mkdir()
        (cls.vault / 'notes/lazy.md').write_text(
            '---\nid: lazy-note\nkind: fact\n---\nQuartzlazyanchor karari kesinlesti.\n', encoding='utf-8')
        synced = run_python(CLI, ['--vault', cls.vault, '--state', cls.state, 'sync'], ROOT, cls.env)
        assert synced.returncode == 0, synced.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def wrapper(self, name, body, arguments):
        path = self.root / name
        path.write_text(body, encoding='utf-8')
        result = run_python(path, arguments, ROOT, self.env)
        text = result.stdout.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn('REPORT:', text, text)
        return json.loads(text.split('REPORT:', 1)[1])

    def command(self, *arguments):
        return self.wrapper('cli_wrapper.py', CLI_WRAPPER, [CLI, self.vault, self.state, *arguments])

    def test_commands_without_sync_skip_the_sync_engine(self):
        for arguments in (('preferences',), ('skill-sync',), ('companion-compact', '--dry-run'), ('jev', 'status')):
            with self.subTest(command=arguments):
                report = self.command(*arguments)
                self.assertEqual(report['code'], 0, report)
                self.assertFalse(report['sync_imported'], arguments)
        self.assertIn('context_mode', self.command('preferences')['result']['preferences'])
        self.assertIn('synced', self.command('skill-sync')['result'])
        # A write still finds its lazily imported helpers through the scripts path.
        written = self.command('preferences', '--threads-chars', '7000')
        self.assertEqual(written['code'], 0, written)
        self.assertEqual(written['result']['companion_limits']['Threads.md'], 7000)

    def test_commands_that_sync_still_load_the_engine(self):
        report = self.command('sync')
        self.assertEqual(report['code'], 0, report)
        self.assertTrue(report['sync_imported'])
        doctor = self.command('doctor')
        self.assertEqual(doctor['code'], 0, doctor)
        self.assertNotIn('error', doctor['result']['task_completion'])

    def test_prompt_context_reads_the_store_without_the_sync_engine(self):
        report = self.wrapper('hook_wrapper.py', HOOK_WRAPPER, [HOOK, self.vault, self.state])
        self.assertIn('lazy-note', report['output'])
        self.assertFalse(report['sync_imported'])


if __name__ == '__main__':
    unittest.main()
