"""Fast, offline measurement-contract smoke test; deliberately no timing gates."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    from tests.v3_package_helpers import ROOT, inherited_env
except ModuleNotFoundError:
    from v3_package_helpers import ROOT, inherited_env

HARNESS = ROOT / 'scripts/perf_v3.py'


class PerfHarnessTest(unittest.TestCase):
    def invoke(self, *arguments):
        result = subprocess.run([sys.executable, str(HARNESS), *map(str, arguments)],
                                cwd=ROOT, env=inherited_env(), capture_output=True,
                                text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def test_generate_run_and_report(self):
        with tempfile.TemporaryDirectory(prefix='v3-perf-smoke-') as temporary:
            root = Path(temporary)
            digests = []
            for name in ('fixture-a', 'fixture-b'):
                output = self.invoke('generate', '--profile', 'day1', '--scale', '0.05',
                                     '--seed', 169, '--out', root / name)
                fixture = json.loads((root / name / 'fixture.json').read_text(encoding='utf-8'))
                digests.append(fixture['fixture_sha256'])
                self.assertIn(fixture['fixture_sha256'], output)
                self.assertEqual(len(fixture['fixture_sha256']), 64)
                self.assertEqual(fixture['notes'], 2)
                vault = root / name / 'vault'
                self.assertTrue((root / name / 'state/memory.sqlite3').is_file())
                self.assertTrue((vault / 'beyin.py').is_file())
                receipt = vault / 'receipts' / (hashlib.sha256(b'perf-000000').hexdigest() + '.md')
                self.assertIn('Synthetic calibration outcome', receipt.read_text(encoding='utf-8'))
            self.assertEqual(digests[0], digests[1], 'same seed must produce identical source fixtures')
            results = root / 'results.json'
            self.invoke('run', '--profile', 'day1', '--scale', '0.05', '--seed', 169,
                        '--repeat', 1, '--scenario', 'cold_process', '--scenario', 'sync_cold',
                        '--scenario', 'hook_session_start', '--out', results)
            payload = json.loads(results.read_text(encoding='utf-8'))
            self.assertEqual(payload['schema_version'], 1)
            self.assertEqual(payload['mode'], 'timing')
            self.assertFalse(payload['profiler_enabled'])
            self.assertEqual(set(payload['scenarios']), {'cold_process', 'sync_cold', 'hook_session_start'})
            for row in payload['scenarios'].values():
                self.assertEqual(row['n'], 1)
                self.assertEqual(len(row['samples_ms']), 1)
                self.assertEqual(len(row['details']), 1)
                for key in ('median_ms', 'p95_ms', 'min_ms', 'max_ms'):
                    self.assertIsInstance(row[key], (float, int))
                    self.assertEqual(row[key], row['samples_ms'][0])
            env = payload['environment']
            for key in ('platform', 'machine', 'cpu_count', 'python_version', 'python_implementation',
                        'filesystem_type', 'under_icloud', 'under_onedrive', 'git_sha',
                        'fixture_sha256', 'repeat_count', 'timestamp', 'updates_off'):
                self.assertIn(key, env)
            self.assertTrue(env['updates_off'])
            self.assertEqual(env['repeat_count'], 1)
            self.assertEqual(env['fixture_sha256'], digests[0])
            hook = payload['scenarios']['hook_session_start']['details'][0]
            self.assertTrue(hook['worker_spawn_enabled'])
            self.assertLessEqual(hook['additional_context_utf16_units'], 10000)
            self.assertIsInstance(hook['sync_pending'], bool)
            self.assertIn('| sync_cold | 1 |', self.invoke('report', results))


if __name__ == '__main__':
    unittest.main()
