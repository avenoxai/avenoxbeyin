"""#233 foreground/scaling budgets: count real operations, never elapsed time."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from v3_package_helpers import clean_environ, inherited_env, install, isolated_env

N = 60
CONSTANT_SLACK = 10  # Fixed companion sources and hook bookkeeping, not per-note work.
SCALING_SLACK = 4
ANCHOR = 'What are quartzanchor rollbackanchor calibration sources?'
CONCRETE = 'How should quartzanchor rollbackanchor calibration sources be verified?'
CONTINUATION = 'bunu biraz daha detaylandır'


@contextmanager
def installed_runtime(vault):
    """Use the installed checkout bytes; restore other tests' imported runtimes."""
    saved = {k: v for k, v in sys.modules.items()
             if k == 'beyin_v3' or k.startswith('beyin_v3_')}
    for name in saved:
        del sys.modules[name]
    directory = str(vault / '.claude/scripts')
    sys.path.insert(0, directory)
    old_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield tuple(importlib.import_module(name) for name in (
            'beyin_v3', 'beyin_v3_sync', 'beyin_v3_hook',
            'beyin_v3_continuity', 'beyin_v3_passage'))
    finally:
        sys.dont_write_bytecode = old_bytecode
        sys.path.remove(directory)
        for name in list(sys.modules):
            if name == 'beyin_v3' or name.startswith('beyin_v3_'):
                del sys.modules[name]
        sys.modules.update(saved)


@contextmanager
def operations(vault, runtime, continuity, passage):
    """Wrap, never stub, retrieval and I/O; attribute reads to the actual caller."""
    reads = dict(total=0, strict=0, continuity=0)
    phase = ['other']

    def tagged(function, name):
        def call(*args, **kwargs):
            previous = phase[0]
            # A regressed _current may call _retrieve internally. Those reads
            # still belong to continuity, not the hook's strict retrieval.
            phase[0] = 'continuity' if previous == 'continuity' else name
            try:
                return function(*args, **kwargs)
            finally:
                phase[0] = previous
        return Mock(wraps=call)

    original_read = Path.read_bytes

    def read(path):
        if path.is_relative_to(vault):
            reads['total'] += 1
            if phase[0] in reads:
                reads[phase[0]] += 1
        return original_read(path)

    byte_reads = Mock(wraps=read)
    walk = Mock(wraps=os.walk)
    rglob = Mock(wraps=Path.rglob)
    glob = Mock(wraps=Path.glob)
    loads = Mock(wraps=json.loads)
    current = tagged(continuity._current, 'continuity')
    strict = tagged(passage.context_for, 'strict')
    retrieve = tagged(runtime.MemoryStore._retrieve, 'strict')
    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, 'read_bytes', lambda path: byte_reads(path)))
        stack.enter_context(patch.object(os, 'walk', walk))
        stack.enter_context(patch.object(Path, 'rglob', lambda path, *a, **kw: rglob(path, *a, **kw)))
        stack.enter_context(patch.object(Path, 'glob', lambda path, *a, **kw: glob(path, *a, **kw)))
        stack.enter_context(patch.object(json, 'loads', loads))
        stack.enter_context(patch.object(continuity, '_current', current))
        stack.enter_context(patch.object(passage, 'context_for', strict))
        stack.enter_context(patch.object(runtime.MemoryStore, '_retrieve',
                                        lambda store, *a, **kw: retrieve(store, *a, **kw)))
        counts = dict(reads=reads, current=current, passage=strict, retrieve=retrieve)
        yield counts
    counts.update(json=loads.call_count,
                  walk=sum(Path(c.args[0]).is_relative_to(vault) for c in walk.call_args_list),
                  rglob=sum(c.args[0].is_relative_to(vault) for c in rglob.call_args_list),
                  glob=sum(c.args[0].is_relative_to(vault) and '**' in str(c.args[1])
                           for c in glob.call_args_list))


class PerfBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='v3-perf-budget-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixtures = []
        for count in (N, 2 * N):
            root = Path(temporary.name).resolve() / str(count)
            vault, state = root / 'Synthetic vault', root / 'external state'
            vault.mkdir(parents=True)
            env = isolated_env(root / 'home')
            env.update(BEYIN_UPDATES_OFF='1', BEYIN_JEV_DISABLE='1')
            result = install(vault, state, inherited_env(**env))
            if result.returncode:
                raise AssertionError(f'fixture install budget setup: notes={count}, '
                                     f'rc={result.returncode}, stderr={result.stderr!r}')
            (vault / '.beyin-preferences.json').write_text(json.dumps(dict(
                auto_sync=True, interval_minutes=0, context_mode='turn', context_chars=5000)),
                encoding='utf-8')
            for index in range(count):
                path = vault / 'notes' / f'note-{index:04d}.md'
                path.parent.mkdir(exist_ok=True)
                body = ('Quartzanchor rollbackanchor calibration sources. Synthetic owner. 🧠\n'
                        if index == 0 else f'Unrelated filler{index} experiment.\n')
                metadata = dict(id=f'perf-note-{index}', kind='note', project='nebula',
                                revision=1, visibility='internal')
                path.write_text('---\n' + json.dumps(metadata) + '\n---\n' + body, encoding='utf-8')
            # A fixed, realistically clipped opening avoids an ambient snapshot. Its
            # astral characters also exercise the client's UTF-16 delivery budget.
            companion = vault / '🔮 850-Companion'
            companion.mkdir(exist_ok=True)
            (companion / 'Last-Session.md').write_text('SYNTHETIC_HANDOFF 🧠\n' * 200, encoding='utf-8')
            (companion / 'Threads.md').write_text('SYNTHETIC_THREADS 🧠\n' * 400, encoding='utf-8')
            with clean_environ(**env), installed_runtime(vault) as modules:
                engine = modules[1].SyncEngine(vault, state)
                result = engine.sync()
                # Prepare a complete real passage cache without a deadline so a
                # scheduler pause in cold setup cannot trigger retrieval fallback.
                eligible, _ = engine.store._eligible('internal', 'nebula')
                config = modules[4].settings(state)
                candidates = modules[4].pool(engine.store, eligible, exclude=config['exclude'])
                modules[4].build(state, candidates, deadline=None)
            if result['status'] != 'succeeded':
                raise AssertionError(f'fixture sync budget setup: notes={count}, result={result}')
            cls.fixtures.append((count, vault, state, env))

    def invoke(self, fixture, hook, event, session, prompt=''):
        count, vault, state, _ = fixture
        payload = json.dumps(dict(hook_event_name=event, session_id=session,
                                  event_id=f'{session}-{event}-{prompt}', prompt=prompt,
                                  cwd=str(vault), project='nebula'))
        output = io.StringIO()
        argv = [hook.__file__, '--vault', str(vault), '--state', str(state), '--harness', 'claude']
        # main() sets a process-wide umask; keep it from leaking to other tests.
        old_umask = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(payload)), \
                    redirect_stdout(output):
                hook.main()
        finally:
            os.umask(old_umask)
        response = json.loads(output.getvalue())
        self.assertFalse((state / 'hook-error.json').exists(),
                         f'{event} foreground budget: notes={count}, response={response}')
        return response.get('hookSpecificOutput', {}).get('additionalContext', '')

    def assert_no_scan(self, label, count, stats):
        scans = {key: stats[key] for key in ('walk', 'rglob', 'glob')}
        self.assertEqual(scans, dict(walk=0, rglob=0, glob=0),
                         f'{label} vault traversal budget: notes={count}, counts={scans}')

    def assert_delivery(self, label, count, text, marker):
        units = len(text.encode('utf-16-le')) // 2
        self.assertIn(marker, text,
                      f'{label} delivery budget: notes={count}, units={units}, marker={marker!r}')
        self.assertLessEqual(units, 10000,
                             f'{label} UTF-16 budget: notes={count}, units={units}, max=10000')

    def anchor(self, fixture, modules, session):
        text = self.invoke(fixture, modules[2], 'UserPromptSubmit', session, ANCHOR)
        self.assert_delivery('anchor', fixture[0], text, 'notes/note-0000.md')
        store = modules[1].SyncEngine(fixture[1], fixture[2]).store
        saved = modules[3]._read(modules[3]._path(store, 'claude', session), modules[3].time.time())
        self.assertIsNotNone(saved, f'anchor setup budget: notes={fixture[0]}, saved={saved}')
        self.assertGreater(len(saved['refs']), 0,
                           f'anchor setup budget: notes={fixture[0]}, refs={saved["refs"]}')

    def test_session_start_constant_json_and_no_vault_traversal(self):
        decodes = []
        for fixture in self.fixtures:
            count, vault, _, env = fixture
            with clean_environ(**env), installed_runtime(vault) as modules:
                with operations(vault, modules[0], modules[3], modules[4]) as stats:
                    text = self.invoke(fixture, modules[2], 'SessionStart', 'opening')
                self.assert_no_scan('SessionStart', count, stats)
                self.assert_delivery('SessionStart', count, text, 'SYNTHETIC_HANDOFF')
                decodes.append(stats['json'])
        self.assertLessEqual(decodes[1] - decodes[0], SCALING_SLACK,
                             f'SessionStart JSON scaling budget: N={N}, loads(N,2N)={decodes}, '
                             f'slack={SCALING_SLACK}')

    def test_concrete_followup_reads_each_source_once_without_continuity(self):
        reads = []
        for fixture in self.fixtures:
            count, vault, _, env = fixture
            with clean_environ(**env), installed_runtime(vault) as modules:
                self.anchor(fixture, modules, 'concrete')
                with operations(vault, modules[0], modules[3], modules[4]) as stats:
                    text = self.invoke(fixture, modules[2], 'UserPromptSubmit', 'concrete', CONCRETE)
                self.assert_no_scan('concrete followup', count, stats)
                self.assert_delivery('concrete followup', count, text, 'notes/note-0000.md')
                measured = stats['reads']
                self.assertLessEqual(measured['total'], count + CONSTANT_SLACK,
                                     f'concrete source-read budget: notes={count}, reads={measured}, '
                                     f'max={count + CONSTANT_SLACK}')
                self.assertEqual((stats['current'].call_count, measured['continuity']), (0, 0),
                                 f'concrete continuity budget: notes={count}, '
                                 f'current={stats["current"].call_count}, reads={measured}')
                self.assertEqual(stats['passage'].call_count, 1,
                                 f'concrete strict retrieval budget: notes={count}, '
                                 f'passage={stats["passage"].call_count}, reads={measured}')
                reads.append(measured['total'])
        self.assertLessEqual(reads[1] - reads[0], N + SCALING_SLACK,
                             f'concrete source-read scaling budget: N={N}, reads(N,2N)={reads}')

    def test_vague_continuation_bounds_only_continuity_reads(self):
        measurements = []
        for fixture in self.fixtures:
            count, vault, _, env = fixture
            with clean_environ(**env), installed_runtime(vault) as modules:
                self.anchor(fixture, modules, 'vague')
                with operations(vault, modules[0], modules[3], modules[4]) as stats:
                    text = self.invoke(fixture, modules[2], 'UserPromptSubmit', 'vague', CONTINUATION)
                self.assert_no_scan('vague continuation', count, stats)
                self.assert_delivery('vague continuation', count, text, 'notes/note-0000.md')
                measured = stats['reads']
                self.assertEqual(stats['current'].call_count, 1,
                                 f'vague continuity execution budget: notes={count}, '
                                 f'current={stats["current"].call_count}, reads={measured}')
                self.assertGreater(measured['continuity'], 0,
                                   f'vague continuity verification budget: notes={count}, reads={measured}')
                self.assertLessEqual(measured['continuity'], modules[3].MAX_REFS,
                                     f'vague continuity source-read budget: notes={count}, '
                                     f'reads={measured}, MAX_REFS={modules[3].MAX_REFS}')
                self.assertLessEqual(measured['strict'], count + CONSTANT_SLACK,
                                     f'vague strict source-read budget: notes={count}, reads={measured}')
                self.assertLessEqual(measured['total'] - measured['strict'],
                                     modules[3].MAX_REFS + CONSTANT_SLACK,
                                     f'vague non-strict source-read budget: notes={count}, reads={measured}')
                measurements.append(measured.copy())
        growth = measurements[1]['total'] - measurements[0]['total']
        strict_growth = measurements[1]['strict'] - measurements[0]['strict']
        self.assertLessEqual(growth - strict_growth, SCALING_SLACK,
                             f'vague source-read scaling budget: N={N}, reads(N,2N)={measurements}, '
                             f'growth={growth}, strict_growth={strict_growth}, slack={SCALING_SLACK}')

    def test_unchanged_warm_sync_has_constant_sql(self):
        totals = []
        for fixture in self.fixtures:
            count, vault, state, env = fixture
            with clean_environ(**env), installed_runtime(vault) as modules:
                engine = modules[1].SyncEngine(vault, state)
                engine.sync()  # Untimed warm-up also rebases receipt-directory signatures.
                statements = []
                connect = engine.store._connect

                @contextmanager
                def traced():
                    with connect() as db:
                        db.set_trace_callback(statements.append)
                        yield db

                with patch.object(engine.store, '_connect', wraps=traced):
                    result = engine.sync()
                self.assertEqual(result['status'], 'succeeded',
                                 f'warm sync correctness budget: notes={count}, result={result}')
                sql = [' '.join(s.upper().split()) for s in statements]
                selects = sum(s.startswith('SELECT PAYLOAD FROM RECORDS WHERE ID=') for s in sql)
                writes = sum(s.startswith('INSERT OR REPLACE INTO MARKDOWN_SOURCES') for s in sql)
                self.assertEqual((selects, writes), (0, 0),
                                 f'warm sync per-note SQL budget: notes={count}, '
                                 f'id_selects={selects}, ownership_writes={writes}, total={len(sql)}')
                totals.append(len(sql))
        self.assertLessEqual(totals[1] - totals[0], SCALING_SLACK,
                             f'warm sync SQL scaling budget: N={N}, statements(N,2N)={totals}, '
                             f'slack={SCALING_SLACK}')

    def test_metadata_events_never_retrieve(self):
        for fixture in self.fixtures:
            count, vault, _, env = fixture
            with clean_environ(**env), installed_runtime(vault) as modules:
                for event in ('PostToolUse', 'Stop', 'SessionEnd'):
                    with self.subTest(notes=count, event=event):
                        with operations(vault, modules[0], modules[3], modules[4]) as stats:
                            self.invoke(fixture, modules[2], event, 'metadata', CONCRETE)
                        calls = dict(retrieve=stats['retrieve'].call_count,
                                     passage=stats['passage'].call_count)
                        self.assertEqual(calls, dict(retrieve=0, passage=0),
                                         f'{event} no-retrieval budget: notes={count}, calls={calls}, '
                                         f'reads={stats["reads"]}')
                        self.assert_no_scan(event, count, stats)


if __name__ == '__main__':
    unittest.main()
