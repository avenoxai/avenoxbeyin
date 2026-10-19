#!/usr/bin/env python3
"""Search benchmark runner and regression contracts (#94). Offline, no model.

Usage:
    python tests/v3_search_benchmark.py
    python -m unittest tests/v3_search_benchmark.py
"""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "template/.claude/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("evaluate_v3_search", ROOT / "scripts/evaluate_v3_search.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class SearchBenchmarkTest(unittest.TestCase):
    def test_frozen_fixture_identity_and_integrity(self):
        """Ensure benchmark fixture matches frozen SHA-256 digest and structure."""
        fixture, digest = evaluator.read_fixture()
        self.assertEqual(digest, "cf175a93ffcc19e44c79c00187f504e6992797c9b92a16160adbc19cf8513627")
        self.assertGreaterEqual(len(fixture["records"]), 35)
        self.assertGreaterEqual(len(fixture["cases"]), 30)

        record_ids = {r["id"] for r in fixture["records"]}
        for case in fixture["cases"]:
            self.assertIn("target_id", case)
            self.assertIn(case["target_id"], record_ids, f"Target {case['target_id']} missing in records")
            self.assertIn("category", case)
            self.assertIn("query", case)

    def test_benchmark_execution_and_baseline_contract(self):
        """Execute benchmark and assert operational performance boundaries."""
        report = evaluator.evaluate(modes=["non_strict", "strict"])
        self.assertLess(report["cold_index_ms"], 10000.0, "Cold index should complete under 10 seconds")

        # Non-strict search baseline boundaries
        non_strict = report["modes"]["non_strict"]
        self.assertGreaterEqual(non_strict["recall_at_1"], 0.65, "Baseline non-strict Recall@1 drop")
        self.assertGreaterEqual(non_strict["recall_at_5"], 0.95, "Baseline non-strict Recall@5 drop")
        self.assertLessEqual(non_strict["median_latency_ms"], 150.0, "Latency ceiling exceeded")

        # Strict search: only floors, never ceilings. Single-term queries are now
        # eligible under a separate weight floor; benchmark recall must not regress.
        strict = report["modes"]["strict"]
        self.assertGreaterEqual(strict["recall_at_1"], 0.75, "Baseline strict Recall@1 drop")

        # Baseline control queries must pass at 100%
        self.assertEqual(strict["categories"]["clean_baseline"]["recall_at_1"], 1.0)
        self.assertEqual(non_strict["categories"]["clean_baseline"]["recall_at_1"], 1.0)

    def test_percentile_calculations(self):
        """Test percentile helper function across edge cases, sorting, and rounding."""
        # Empty input
        self.assertIsNone(evaluator.percentile([], 0.5))
        self.assertIsNone(evaluator.percentile([], 0.95))

        # Single element
        self.assertEqual(evaluator.percentile([42.1234], 0.5), 42.12)
        self.assertEqual(evaluator.percentile([42.1234], 0.0), 42.12)
        self.assertEqual(evaluator.percentile([42.1234], 1.0), 42.12)

        # Unsorted list
        vals = [10.0, 1.0, 5.0, 8.0, 3.0]
        # Sorted: [1.0, 3.0, 5.0, 8.0, 10.0], len = 5
        self.assertEqual(evaluator.percentile(vals, 0.0), 1.0)
        self.assertEqual(evaluator.percentile(vals, 0.5), 5.0)  # ceil(5*0.5)-1 = 2 -> 5.0
        self.assertEqual(evaluator.percentile(vals, 0.95), 10.0)  # ceil(5*0.95)-1 = 4 -> 10.0
        self.assertEqual(evaluator.percentile(vals, 1.0), 10.0)

        # Precision rounding
        self.assertEqual(evaluator.percentile([1.111, 2.229, 3.333], 0.5), 2.23)

    def test_reranked_search_improves_salience_and_recall(self):
        """Field-weighted reranking floors: Recall@1 >= 0.90 and MRR >= 0.90."""
        report = evaluator.evaluate(modes=["reranked"])
        reranked = report["modes"]["reranked"]
        self.assertGreaterEqual(reranked["recall_at_1"], 0.90, "Reranked Recall@1 drop below 90%")
        # Floors with slack, like the baseline asserts: 0.964 today, and a single case moving
        # from rank 1 to 2 costs 0.016, so a 0.95 floor would fail on any one-rank change.
        self.assertGreaterEqual(reranked["mrr"], 0.90, "Reranked MRR drop below 0.90")
        self.assertLessEqual(reranked["median_latency_ms"], 150.0, "Reranked latency ceiling exceeded")


def main():
    report = evaluator.evaluate()
    print(evaluator.format_markdown(report))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--unittest":
        unittest.main(argv=[sys.argv[0]])
    elif len(sys.argv) > 1 and sys.argv[1] not in ("--report", "-r"):
        unittest.main()
    else:
        raise SystemExit(main())
