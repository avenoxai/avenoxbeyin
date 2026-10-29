"""Fast, offline measurement-contract smoke test; deliberately no timing gates."""
import importlib.util
from unittest.mock import Mock, patch
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

    def test_windows_worker_wait_closes_handle_after_exit(self):
        spec = importlib.util.spec_from_file_location('perf_worker_wait', HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = 0
        module.wait_windows_worker(42, kernel=kernel)
        kernel.OpenProcess.assert_called_once_with(0x00100000, False, 42)
        kernel.WaitForSingleObject.assert_called_once_with(123, 30000)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_worker_timeout_is_not_suppressed(self):
        spec = importlib.util.spec_from_file_location('perf_worker_timeout', HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = 258
        with self.assertRaises(TimeoutError):
            module.wait_windows_worker(42, kernel=kernel)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_worker_already_exited_is_safe(self):
        import ctypes
        spec = importlib.util.spec_from_file_location('perf_worker_exited', HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch.object(ctypes, 'get_last_error', return_value=87, create=True):
            module.wait_windows_worker(42, kernel=kernel)
        kernel.WaitForSingleObject.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_windows_worker_access_denied_is_not_suppressed(self):
        import ctypes
        spec = importlib.util.spec_from_file_location('perf_worker_denied', HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch.object(ctypes, 'get_last_error', return_value=5, create=True):
            with self.assertRaises(OSError) as error:
                module.wait_windows_worker(42, kernel=kernel)
        self.assertEqual(error.exception.errno, 5)
        kernel.CloseHandle.assert_not_called()

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


    def test_summary_empty_samples(self):
        spec = importlib.util.spec_from_file_location('perf_summary', HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.summary([], [])
        self.assertEqual(result['n'], 0)
        self.assertIsNone(result['median_ms'])
        self.assertIsNone(result['p95_ms'])
        self.assertIsNone(result['min_ms'])
        self.assertIsNone(result['max_ms'])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'empty-results.json'
            path.write_text(json.dumps({'scenarios': {'empty': result}}), encoding='utf-8')
            self.assertIn('| empty | 0 | n/a | n/a | n/a | n/a |', self.invoke('report', path))


    def test_evaluate_v3_decisions_percentile_empty(self):
        spec = importlib.util.spec_from_file_location('evaluate_v3_decisions', ROOT / 'scripts/evaluate_v3_decisions.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.percentile([], 0.95)
        self.assertIsNone(result)

    def test_evaluate_v3_aggregate_empty(self):
        spec = importlib.util.spec_from_file_location('evaluate_v3', ROOT / 'scripts/evaluate_v3.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.aggregate([])
        self.assertEqual(result['cases'], 0)
        self.assertIsNone(result['recall_at_5'])
        self.assertIsNone(result['precision_at_5_diagnostic'])
        self.assertIsNone(result['abstention_accuracy'])
        self.assertIsNone(result['structured_fact_retention'])
        self.assertFalse(result['passed'])

    def test_evaluate_v3_decisions_summarize_empty(self):
        spec = importlib.util.spec_from_file_location('evaluate_v3_decisions', ROOT / 'scripts/evaluate_v3_decisions.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.summarize([])
        self.assertEqual(result['samples'], 0)
        self.assertIsNone(result['recall'])
        self.assertIsNone(result['precision'])
        self.assertIsNone(result['candidate_exposure_recall'])
        self.assertIsNone(result['abstention_accuracy'])
        self.assertIsNone(result['p50_e2e_ms'])
        self.assertIsNone(result['p95_e2e_ms'])

if __name__ == '__main__':
    unittest.main()
