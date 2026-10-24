#!/usr/bin/env python3
"""Measure the original, unsharded V3 unittest suite without changing test behavior.

Equivalent test discovery/order to:
    python -m unittest discover -s tests -p "v3_*test.py"

Writes one flushed JSONL event per test. Timings include individual test
setup/teardown; the gap before each test separately captures class/module
fixture overhead omitted by unittest startTest/stopTest hooks.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter
import unittest


class MeasuredResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity, emit):
        super().__init__(stream, descriptions, verbosity)
        self.emit = emit
        self._start = {}
        self._status = {}
        self._last_stop = perf_counter()
        self._count = 0

    def startTest(self, test):
        now = perf_counter()
        self._start[id(test)] = (now, now - self._last_stop)
        self._status[id(test)] = "passed"
        super().startTest(test)

    def addFailure(self, test, err):
        self._status[id(test)] = "failed"
        super().addFailure(test, err)

    def addError(self, test, err):
        self._status[id(test)] = "error"
        super().addError(test, err)

    def addSkip(self, test, reason):
        self._status[id(test)] = "skipped"
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._status[id(test)] = "expected_failure"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._status[id(test)] = "unexpected_success"
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._status[id(test)] = "failed_subtest"
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        now = perf_counter()
        start, fixture_gap = self._start.pop(id(test))
        elapsed = now - start
        self._count += 1
        event = {
            "event": "test",
            "number": self._count,
            "test_id": test.id(),
            "seconds": round(elapsed, 6),
            "before_test_seconds": round(fixture_gap, 6),
            "status": self._status.pop(id(test), "unknown"),
        }
        self.emit(event)
        if elapsed >= 3.0 or fixture_gap >= 3.0 or self._count % 100 == 0:
            print(
                "TEST_TIMING " + json.dumps(event, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        self._last_stop = perf_counter()
        super().stopTest(test)


class MeasuredRunner(unittest.TextTestRunner):
    def __init__(self, emit):
        super().__init__(verbosity=1, failfast=False, buffer=False)
        self._emit = emit

    def _makeResult(self):
        return MeasuredResult(self.stream, self.descriptions, self.verbosity, self._emit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="v3-test-timings.jsonl")
    args = parser.parse_args()

    tests_dir = Path(__file__).resolve().parent
    # A script executed from tests/ lacks the repository root on sys.path.
    # unittest -m discovery does have it; preserve the original imports.
    root = str(tests_dir.parent)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8", newline="\n") as handle:
        def emit(event):
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()

        emit({
            "event": "environment",
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "runner_os": os.environ.get("RUNNER_OS"),
            "runner_arch": os.environ.get("RUNNER_ARCH"),
            "test_pattern": "v3_*test.py",
            "mode": "original-full-suite",
        })
        suite = unittest.TestLoader().discover(
            start_dir=str(tests_dir),
            pattern="v3_*test.py",
            top_level_dir=str(tests_dir),
        )
        started = perf_counter()
        result = MeasuredRunner(emit).run(suite)
        emit({
            "event": "summary",
            "seconds": round(perf_counter() - started, 6),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "successful": result.wasSuccessful(),
        })
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
