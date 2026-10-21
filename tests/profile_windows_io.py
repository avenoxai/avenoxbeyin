#!/usr/bin/env python3
"""Bounded offline IO diagnosis using unchanged real operations and three tests.

Timings are inclusive and overlap. Subprocess metrics include the whole child;
Python file/SQLite metrics cover only this process. No durability setting,
acceptance threshold, source bytes, or tested implementation is changed.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from workspace_setup_profile import Probe
import evaluate_v3_search as evaluator
import beyin_v3 as runtime
from beyin_v3_sync import atomic

TESTS = (
    'v3_runtime_test.RuntimeContractTest.test_history_duplicate_ingest_and_rejected_writes_add_no_event',
    'v3_workspace_workflow_test.WorkspaceWorkflowTest.test_self_trigger_recovery_rejects_external_changes_and_forged_ownership',
    'v3_update_test.OfflineUpdateTest.test_check_is_read_only_with_available_version',
)


class IOProbe(Probe):
    def install(self, stack):
        super().install(stack)
        for owner, name, label in (
            (os, 'replace', 'os.replace'),
            (Path, 'resolve', 'path.resolve'),
            (Path, 'stat', 'path.stat'),
            (subprocess, 'run', 'subprocess.run'),
        ):
            stack.enter_context(patch.object(owner, name,
                self.instrument(label, getattr(owner, name))))
        class MeasuredConnection(sqlite3.Connection):
            pass
        for name in ('execute', 'executemany', 'executescript', '__exit__', 'commit', 'rollback', 'close'):
            setattr(MeasuredConnection, name, self.instrument('sqlite.' + name,
                getattr(sqlite3.Connection, name)))
        execute = MeasuredConnection.execute
        def execute_by_kind(connection, sql, *args, **kwargs):
            kind = ' '.join(sql.upper().split()[:2])
            label = ('begin_immediate' if kind == 'BEGIN IMMEDIATE' else
                     'select' if kind.startswith('SELECT') else
                     'pragma' if kind.startswith('PRAGMA') else 'other')
            return self.instrument('sqlite.sql.' + label, execute)(connection, sql, *args, **kwargs)
        MeasuredConnection.execute = execute_by_kind
        original = sqlite3.connect
        def connect(*args, **kwargs):
            if 'factory' not in kwargs and len(args) < 6:
                kwargs['factory'] = MeasuredConnection
            return original(*args, **kwargs)
        stack.enter_context(patch.object(sqlite3, 'connect', self.instrument('sqlite.connect', connect)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--output', default='windows-io-profile.jsonl')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3:
        parser.error('--repeats must be 1..3')
    fixture, digest = evaluator.read_fixture()
    # Load the exact test implementations before measurement, as full discovery does.
    suites = [unittest.defaultTestLoader.loadTestsFromName(name) for name in TESTS]
    if any(s.countTestCases() != 1 for s in suites):
        raise RuntimeError('unexpected test selection')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8', newline='\n') as handle:
        def emit(event):
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            print(line, flush=True)
            handle.write(line + '\n'); handle.flush()
        emit(dict(event='environment', python=sys.version, platform=platform.platform(),
            processor=platform.processor(), cpus=os.cpu_count(), sqlite=sqlite3.sqlite_version,
            fixture_sha256=digest, tests=TESTS, repeats=args.repeats,
            semantics='inclusive parent-process measurements; subprocess.run includes child wall time'))
        for iteration in range(args.repeats):
            probe = IOProbe()
            with ExitStack() as stack:
                probe.install(stack)
                def phase(name, operation, **extra):
                    before = probe.snapshot(); start = perf_counter()
                    try:
                        return operation()
                    finally:
                        emit(dict(event='phase', iteration=iteration, phase=name,
                            wall_seconds=round(perf_counter()-start, 6), operations=probe.changes(before), **extra))
                with tempfile.TemporaryDirectory(prefix='beyin-io-controls-') as directory:
                    base = Path(directory)
                    def atomic_control():
                        for index in range(32):
                            atomic(base / ('atomic-%02d.txt' % index), 'Synthetic bytes\n' * 128)
                    phase('control.atomic32', atomic_control)
                    _, cold = phase('control.frozen_seed35', lambda: evaluator.seed_store(runtime, base / 'seed', fixture))
                    emit(dict(event='cold_index', iteration=iteration, cold_index_ms=cold))
                    def startup():
                        for _ in range(5):
                            subprocess.run([sys.executable, '-c', 'pass'], check=True, capture_output=True)
                    phase('control.python_startup5', startup)
                for name in TESTS:
                    case = next(iter(unittest.defaultTestLoader.loadTestsFromName(name)))
                    for method, label in (('setUp', 'setup'), (case._testMethodName, 'body'),
                                          ('tearDown', 'teardown'), ('doCleanups', 'cleanup')):
                        original = getattr(case, method)
                        def measured(*a, _work=original, _label=label, **kw):
                            return phase('test.' + _label, lambda: _work(*a, **kw), test_id=name)
                        setattr(case, method, measured)
                    result = phase('test.total', lambda: unittest.TextTestRunner(verbosity=1).run(case), test_id=name)
                    if not result.wasSuccessful() or result.testsRun != 1 or result.skipped:
                        raise RuntimeError('diagnostic test did not pass: ' + name)
        emit(dict(event='summary', successful=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
