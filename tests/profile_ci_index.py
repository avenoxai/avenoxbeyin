#!/usr/bin/env python3
"""Trace the unchanged frozen index alone or in original full-suite order.

Only seed_store is instrumented. SQL includes lock wait; __exit__ includes
commit/rollback. Counters overlap and must not be summed.
"""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import platform
import sys
import sqlite3
import threading
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import evaluate_v3_search as evaluator


def measured_seed(original, emit, ambient=None):
    def seed(*args, **kwargs):
        from profile_windows_io import IOProbe
        from unittest.mock import patch
        probe = IOProbe()
        start = perf_counter()
        before_env = dict(os.environ)
        before_cwd = os.getcwd()
        emit(dict(event='seed_start', runtime_module=getattr(args[0], '__file__', None),
                  sqlite_version=sqlite3.sqlite_version,
                  cwd_changed_since_start=bool(ambient and before_cwd != ambient['cwd']),
                  env_changed_keys_since_start=sorted(
                      k for k in ambient['env'].keys() | before_env.keys()
                      if ambient['env'].get(k) != before_env.get(k)) if ambient else [],
                  threads=[dict(name=t.name,daemon=t.daemon) for t in threading.enumerate()]))
        initialized = None
        initialize = args[0].MemoryStore.__init__
        def measured_initialize(store, *a, **kw):
            nonlocal initialized
            before = probe.snapshot()
            started = perf_counter()
            try:
                return initialize(store, *a, **kw)
            finally:
                emit(dict(event='initialize', wall_seconds=perf_counter()-started,
                          operations=probe.changes(before)))
                initialized = probe.snapshot()
        try:
            with ExitStack() as stack:
                probe.install(stack)
                stack.enter_context(patch.object(args[0].MemoryStore, '__init__', measured_initialize))
                result = original(*args, **kwargs)
            emit(dict(event='cold_index', cold_index_ms=result[1],
                      seed_wall_seconds=perf_counter()-start,
                      operations=probe.changes(initialized or {}),
                      operations_scope='after constructor; frozen clock unchanged',
                      cwd_changed=os.getcwd()!=before_cwd,
                      env_changed_keys=sorted(k for k in before_env.keys() | os.environ.keys()
                                             if before_env.get(k)!=os.environ.get(k))))
            return result
        except BaseException as exc:
            emit(dict(event='cold_index_error', error=type(exc).__name__,
                      operations=probe.changes({})))
            raise
    return seed


@contextmanager
def instrument_evaluators(emit, ambient=None):
    # The frozen test loads its evaluator with module_from_spec, so patching
    # only the normal import would miss the exact failing benchmark.
    from unittest.mock import patch
    modules = [evaluator]
    for module in list(sys.modules.values()):
        candidate = getattr(module, 'evaluator', None)
        if (candidate is not None and candidate not in modules and
                getattr(candidate, '__file__', None) == str(ROOT / 'scripts/evaluate_v3_search.py')):
            modules.append(candidate)
    with ExitStack() as stack:
        for module in modules:
            stack.enter_context(patch.object(module, 'seed_store', measured_seed(module.seed_store, emit, ambient)))
        yield


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--output', default='ci-index.jsonl')
    args = parser.parse_args()
    ambient = dict(cwd=os.getcwd(), env=dict(os.environ))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as output:
        def emit(event):
            output.write(json.dumps(event, ensure_ascii=False, sort_keys=True)+'\n')
            output.flush()
        emit(dict(event='environment', python=sys.version, platform=platform.platform(),
                  fixture_sha256=evaluator.read_fixture()[1], full=args.full,
                  semantics='nested inclusive counters; cold excludes constructor; SQL includes lock wait'))
        if args.full:
            import unittest
            from profile_v3_unittests import MeasuredRunner
            sys.path.insert(0,str(ROOT))
            suite=unittest.TestLoader().discover(str(ROOT/'tests'),pattern='v3_*test.py',top_level_dir=str(ROOT/'tests'))
            with instrument_evaluators(emit, ambient):
                result=MeasuredRunner(emit).run(suite)
            emit(dict(event='summary',successful=result.wasSuccessful(),tests_run=result.testsRun,
                      failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped)))
            return 0 if result.wasSuccessful() else 1
        import tempfile
        import beyin_v3
        with instrument_evaluators(emit, ambient):
            for _ in range(3):
                with tempfile.TemporaryDirectory() as directory:
                    evaluator.seed_store(beyin_v3,Path(directory),evaluator.read_fixture()[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
