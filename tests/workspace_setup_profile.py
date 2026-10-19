#!/usr/bin/env python3
"""Offline workspace setup profiler using the real onboarding test fixture.

Run from the repository root:
    python tests/workspace_setup_profile.py --sizes 0,100,1000
    python tests/workspace_setup_profile.py --sizes 0,100 --jsonl workspace-profile.jsonl

This is a manual diagnostic, not a timing gate. It uses only temporary synthetic
vaults; no real user files, network, native clients, or production code changes.
Each JSONL row is flushed as a phase finishes so a cancelled run retains evidence.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter
from unittest.mock import patch

from v3_workspace_onboarding_test import OnboardingTest, artifacts, render, SyncEngine
from beyin_v3_workspaces import Registry, LocalAuthority
from beyin_v3_sources import SourceResolver
from beyin_v3_workspace_context import WorkspaceRuntime
from beyin_v3_provision import Provisioner
import beyin_v3_workspace_views as views
from beyin_v3_workspace_setup import execute as setup_execute


class Probe:
    """Inclusive method timings and counts; individual counters may overlap."""

    def __init__(self):
        self.samples = defaultdict(lambda: [0, 0.0, 0])

    def instrument(self, label, original, *, byte_result=False, byte_argument=False):
        def measured(*args, **kwargs):
            start = perf_counter()
            size = 0
            try:
                result = original(*args, **kwargs)
                if byte_result:
                    size = len(result)
                elif byte_argument:
                    size = len(args[1] if len(args) > 1 else kwargs['data'])
                return result
            finally:
                row = self.samples[label]
                row[0] += 1
                row[1] += perf_counter() - start
                row[2] += size
        return measured

    def install(self, stack):
        targets = [
            (Registry, 'snapshot', 'registry.snapshot'),
            (Registry, 'bindings', 'registry.bindings'),
            (Registry, 'capture_files', 'registry.capture_files'),
            (LocalAuthority, '_load', 'authority.load'),
            (SourceResolver, 'files', 'resolver.files'),
            (SourceResolver, 'resolve', 'resolver.resolve'),
            (SourceResolver, 'extract', 'resolver.extract'),
            (SourceResolver, 'read', 'resolver.read'),
            (WorkspaceRuntime, 'context', 'runtime.context'),
            (SyncEngine, 'sync', 'sync'),
            (Provisioner, 'apply', 'provision.apply'),
            (views, 'initialize', 'views.initialize'),
            (os, 'fsync', 'os.fsync'),
            (Path, 'read_bytes', 'path.read_bytes'),
            (Path, 'write_bytes', 'path.write_bytes'),
        ]
        for owner, name, label in targets:
            original = getattr(owner, name)
            stack.enter_context(patch.object(
                owner, name, self.instrument(
                    label, original, byte_result=label == 'path.read_bytes',
                    byte_argument=label == 'path.write_bytes')))

    def snapshot(self):
        return {name: tuple(values) for name, values in self.samples.items()}

    def changes(self, before):
        changed = {}
        for name, current in self.samples.items():
            old = before.get(name, (0, 0.0, 0))
            calls, seconds, size = [current[i] - old[i] for i in range(3)]
            if calls:
                changed[name] = {'calls': calls, 'seconds': round(seconds, 4)}
                if name in ('path.read_bytes', 'path.write_bytes'):
                    changed[name]['bytes'] = size
        return changed


class Reporter:
    def __init__(self, jsonl):
        self.destination = Path(jsonl) if jsonl else None
        if self.destination:
            self.destination.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event):
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        print(line, flush=True)
        if self.destination:
            with self.destination.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
                stream.flush()


def seed(case, count):
    """Put an equal number of indexed vault notes and external text files in the fixture."""
    if not count:
        return
    notes = case.vault / 'notes' / 'benchmark'
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        identity = f'bench-note-{index:05d}'
        (notes / (identity + '.md')).write_bytes(render({
            'id': identity, 'kind': 'note', 'revision': 1,
            'workspace_id': 'fitness', 'visibility': 'internal',
            'title': 'Benchmark ' + str(index),
        }, 'Synthetic indexed note ' + str(index) + '\n').encode('utf-8'))
        (case.external / (identity + '.txt')).write_text(
            'Synthetic external source ' + str(index) + '\n', encoding='utf-8')


def run_case(count, iteration, reporter, max_items):
    case = OnboardingTest('test_stage_is_compact_durable_and_never_approves_or_initializes')
    case.setUp()
    probe = Probe()
    label = {'notes': count, 'external_files': count, 'iteration': iteration}
    try:
        seed(case, count)
        with ExitStack() as stack:
            probe.install(stack)

            def phase(name, operation, *, expected_code=None):
                before = probe.snapshot()
                start = perf_counter()
                try:
                    value = operation()
                    if expected_code is not None and value[1] != expected_code:
                        raise RuntimeError(f'{name}: unexpected exit code {value[1]}')
                    return value
                finally:
                    reporter.emit({
                        **label, 'phase': name,
                        'wall_seconds': round(perf_counter() - start, 4),
                        'operations': probe.changes(before),
                    })

            if count:
                phase('fixture.seed_sync', lambda: SyncEngine(case.vault, case.state).sync())

            discovery = dict(
                sources=case.payload['sources'], goal=case.payload['purpose'],
                workspace_id=case.payload['workspace_id'], name=case.payload['name'],
                max_depth=2, max_items=max_items)
            phase('discovery', lambda: setup_execute(case.vault, case.state, 'discover', discovery),
                  expected_code=0)
            phase('prepare', lambda: setup_execute(case.vault, case.state, 'prepare', case.payload),
                  expected_code=0)
            # Stage intentionally calls prepare again: report it as an inclusive step.
            staged, saved = phase('stage_including_prepare', case.stage)
            phase('explicit_test_approval', lambda: case.approve(saved))
            result, code = phase('complete_including_provision_and_homepage',
                                 lambda: case.complete(staged))
            if code != 5 or not artifacts(result)['homepage_verified']:
                raise RuntimeError('workspace homepage was not verified: ' + repr(result))
            result, code = phase('idempotent_resume', lambda: case.complete(staged))
            if code != 5 or not artifacts(result)['homepage_verified']:
                raise RuntimeError('workspace resume was not verified: ' + repr(result))
    finally:
        case.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', default='0,100,1000',
                        help='Comma-separated synthetic note/file counts (default: 0,100,1000)')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--max-items', type=int, default=500,
                        help='Bounded discovery catalog limit (1..500)')
    parser.add_argument('--jsonl', help='Append flushed per-phase JSONL measurements here')
    args = parser.parse_args()
    try:
        sizes = [int(value) for value in args.sizes.split(',')]
    except ValueError:
        parser.error('--sizes must be comma-separated nonnegative integers')
    if not sizes or any(size < 0 or size > 1000 for size in sizes):
        parser.error('--sizes must contain values between 0 and 1000')
    if args.repeat < 1 or args.repeat > 10:
        parser.error('--repeat must be between 1 and 10')
    if not 1 <= args.max_items <= 500:
        parser.error('--max-items must be between 1 and 500')

    reporter = Reporter(args.jsonl)
    reporter.emit({
        'event': 'environment', 'python': sys.version.split()[0],
        'platform': platform.platform(), 'sizes': sizes, 'repeat': args.repeat,
        'note': 'Nested timings overlap; stage includes prepare; fixture seeding is excluded.',
    })
    for count in sizes:
        for iteration in range(1, args.repeat + 1):
            run_case(count, iteration, reporter, args.max_items)


if __name__ == '__main__':
    main()
