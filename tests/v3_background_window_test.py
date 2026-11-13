"""Background spawn contract, with native Windows descendant-window verification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, install, isolated_env, run_python

# Exercise the real entry points, not a copied options expression. Only the process
# boundary is intercepted; queue/cadence and installed module imports remain real.
CAPTURE = r'''
import io, json, os, sys, subprocess
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
scripts, vault, state, kind, platform = sys.argv[1:]
sys.path.insert(0, scripts)
import beyin_v3_hook as hook
import beyin_v3_releases as releases
module = hook if kind == 'hook' else releases
fake_os = SimpleNamespace(**dict(vars(os), name=platform))
calls = []
def capture(command, **options):
    calls.append({'command': command, 'options': options})
    return SimpleNamespace(returncode=0)
with patch.object(module, 'os', fake_os), \
     patch.object(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True), \
     patch.object(subprocess, 'DETACHED_PROCESS', 0x8, create=True), \
     patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
     patch.object(subprocess, 'Popen', capture), patch('sys.stdout', new_callable=io.StringIO):
    if kind == 'hook':
        sys.argv = ['hook', '--vault', vault, '--state', state, '--harness', 'claude', '--metadata-only']
        with patch('sys.stdin', io.StringIO(json.dumps({'hook_event_name':'SessionEnd','session_id':'window-test'}))):
            hook.main()
    else:
        releases.session_start(Path(vault), Path(state))
print(json.dumps(calls))
'''


class BackgroundWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = isolated_env(self.root / 'home')
        self.env['BEYIN_V3_NO_SPAWN'] = '0'
        self.wrapper = self.root / 'capture.py'
        self.wrapper.write_text(CAPTURE, encoding='utf-8')

    def capture(self, installed, kind, platform):
        vault = self.root / ('vault-' + str(installed) + kind + platform)
        vault.mkdir()
        state = self.root / ('state-' + str(installed) + kind + platform)
        scripts = ROOT / 'template/.claude/scripts'
        if installed:
            install(vault, state, self.env)
            scripts = vault / '.claude/scripts'
        else:
            (vault / '.beyin-version').write_text('3.0.0', encoding='utf-8')
        result = run_python(self.wrapper, [str(scripts), str(vault), str(state), kind, platform], self.root, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        self.assertEqual(len(calls), 1, result.stdout)
        return calls[0]

    def test_windows_workers_request_no_window_in_source_and_install(self):
        for installed in (False, True):
            for kind in ('hook', 'release'):
                with self.subTest(installed=installed, kind=kind):
                    call = self.capture(installed, kind, 'nt')
                    options = call['options']
                    self.assertEqual(options['creationflags'], 0x08000200)
                    self.assertNotIn('start_new_session', options)
                    self.assertTrue(options['close_fds'])
                    for stream in ('stdin', 'stdout', 'stderr'):
                        self.assertEqual(options[stream], subprocess.DEVNULL)
                    self.assertIn('--worker', call['command'])

    def test_posix_workers_keep_new_session(self):
        for kind in ('hook', 'release'):
            with self.subTest(kind=kind):
                options = self.capture(False, kind, 'posix')['options']
                self.assertTrue(options['start_new_session'])
                self.assertNotIn('creationflags', options)

    @unittest.skipUnless(os.name == 'nt', 'native Windows console API required')
    def test_windows_worker_and_descendant_have_no_visible_console(self):
        # Use production entry-point options, then run a synthetic worker and its
        # ordinary (no extra flags) child. No real vault or network operation.
        console_api = ('from ctypes import wintypes\n'
                       'ctypes.windll.kernel32.GetConsoleWindow.restype = wintypes.HWND\n'
                       'ctypes.windll.user32.IsWindowVisible.argtypes = [wintypes.HWND]\n'
                       'ctypes.windll.user32.IsWindowVisible.restype = wintypes.BOOL\n')
        probe = self.root / 'probe.py'
        probe.write_text('import ctypes, json, sys\n' + console_api +
                         'hwnd = ctypes.windll.kernel32.GetConsoleWindow()\n'
                         'visible = bool(hwnd and ctypes.windll.user32.IsWindowVisible(hwnd))\n'
                         'open(sys.argv[1], "w").write(json.dumps(visible))\n', encoding='utf-8')
        worker = self.root / 'worker.py'
        worker.write_text('import subprocess, sys, ctypes, json\n' + console_api +
                          'hwnd = ctypes.windll.kernel32.GetConsoleWindow()\n'
                          'open(sys.argv[2] + ".worker", "w").write(json.dumps(bool(hwnd and ctypes.windll.user32.IsWindowVisible(hwnd))))\n'
                          'subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], check=True)\n', encoding='utf-8')
        for kind in ('hook', 'release'):
            with self.subTest(kind=kind):
                options = self.capture(True, kind, 'nt')['options']
                output = self.root / (kind + '.json')
                process = subprocess.Popen([sys.executable, str(worker), str(probe), str(output)], env=self.env, **options)
                self.assertEqual(process.wait(timeout=20), 0)
                self.assertFalse(json.loads(output.read_text()))
                self.assertFalse(json.loads(Path(str(output) + '.worker').read_text()))


if __name__ == '__main__':
    unittest.main()
