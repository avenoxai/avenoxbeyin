"""TEMPORARY measurement probe for #233, never merged: prints Windows hook launch timings to the CI log."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    from tests.v3_package_helpers import ROOT, inherited_env
except ModuleNotFoundError:
    from v3_package_helpers import ROOT, inherited_env


@unittest.skipUnless(os.name == 'nt', 'Windows launch cost probe')
class WindowsHookLaunchProbe(unittest.TestCase):
    def test_print_launch_costs(self):
        with tempfile.TemporaryDirectory(prefix='v3-perf-win-') as temporary:
            results = Path(temporary) / 'results.json'
            for profile, scale in (('day1', 1.0), ('active', 0.2)):
                command = [sys.executable, str(ROOT / 'scripts/perf_v3.py'), 'run', '--profile', profile,
                           '--scale', str(scale), '--repeat', '5', '--out', str(results)]
                for scenario in ('cold_process', 'hook_session_start', 'hook_session_start_direct',
                                 'hook_session_start_nospawn', 'hook_user_prompt', 'hook_user_prompt_direct',
                                 'hook_user_prompt_nospawn', 'hook_post_tool_use', 'hook_post_tool_use_direct',
                                 'hook_stop', 'hook_stop_direct', 'sync_warm'):
                    command += ['--scenario', scenario]
                run = subprocess.run(command, cwd=ROOT, env=inherited_env(), capture_output=True,
                                     text=True, encoding='utf-8', timeout=900)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                report = subprocess.run([sys.executable, str(ROOT / 'scripts/perf_v3.py'), 'report', str(results)],
                                        capture_output=True, text=True, encoding='utf-8', check=True).stdout
                env = json.loads(results.read_text(encoding='utf-8'))['environment']
                print(f'\n#233 WINDOWS PROBE profile={profile} scale={scale} env={json.dumps(env)}\n{report}',
                      file=sys.stderr, flush=True)
