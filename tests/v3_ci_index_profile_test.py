"""Measurement seams must see the evaluator loaded by the frozen test."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import profile_ci_index as profile
import v3_search_benchmark as benchmark


class IndexProfileTest(unittest.TestCase):
    def test_anonymous_evaluator_is_measured_and_restored_on_failure(self):
        # The frozen test uses module_from_spec without sys.modules registration.
        self.assertIsNot(benchmark.evaluator, profile.evaluator)
        originals = (benchmark.evaluator.seed_store, profile.evaluator.seed_store)
        rows = []
        with profile.instrument_evaluators(rows.append):
            self.assertIsNot(benchmark.evaluator.seed_store, originals[0])
            import beyin_v3
            with tempfile.TemporaryDirectory() as directory:
                _, cold = benchmark.evaluator.seed_store(beyin_v3, Path(directory),
                    benchmark.evaluator.read_fixture()[0])
            measured = [r for r in rows if r['event'] == 'cold_index']
            self.assertEqual(len(measured),1)
            self.assertEqual(measured[0]['cold_index_ms'],cold)
            self.assertEqual(measured[0]['operations']['sqlite.connect']['calls'],35)
            initialization = [r for r in rows if r['event'] == 'initialize']
            self.assertEqual(len(initialization),1)
            self.assertEqual(initialization[0]['operations']['sqlite.connect']['calls'],1)
        self.assertIs(benchmark.evaluator.seed_store, originals[0])
        self.assertIs(profile.evaluator.seed_store, originals[1])
        failure = RuntimeError('synthetic seed failure')
        def fail(*args):raise failure
        with self.assertRaisesRegex(RuntimeError,'synthetic seed failure'):
            profile.measured_seed(fail, rows.append)(beyin_v3)
        self.assertEqual(rows[-1]['event'],'cold_index_error')


if __name__ == '__main__':
    unittest.main()
