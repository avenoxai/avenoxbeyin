#!/usr/bin/env python3
"""Offline developer measurement contract for #233; never packaged in a release.

Examples: generate --profile day1 --out /tmp/fixture --seed 233
          run --profile active --repeat 5 --out /tmp/results.json
          profile --profile active --scenario sync_warm --out /tmp/profile
          report /tmp/results.json

Timing excludes fixture creation, restoration and scenario preparation. A divergent
sample covers THREE syncs; lock samples measure acquisition wait inside process two.
Hook timing includes the installed shell command and its real detached worker. The
worker is drained before restoration, outside the timed interval. Profiles use only
in-process equivalents and are deliberately separate from wall-clock measurements.
"""
from __future__ import annotations

import argparse
import cProfile
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import platform
import pstats
import random
import re
import runpy
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('day1', 'active', 'mega', 'edge')
EVENTS = dict(session_start='SessionStart', user_prompt='UserPromptSubmit',
              post_tool_use='PostToolUse', stop='Stop', session_end='SessionEnd')
SYNC_SCENARIOS = ('sync_cold', 'sync_warm', 'sync_one_note', 'sync_one_receipt',
                  'sync_divergent_repeat', 'sync_after_resolution')
SCENARIOS = ('cold_process', *SYNC_SCENARIOS, 'lock_two_process',
             *('hook_' + name + suffix for suffix in ('', '_nospawn', '_direct')
               for name in EVENTS))
FIXED_DATE = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
# Synthetic vocabulary only; never sampled from a real vault.
WORDS = ('kalibrasyon', 'kaynak', 'deney', 'sonuç', 'karar', 'toplantı', 'müşteri', 'arşiv', 'şifre',
         'görev', 'öğrenim', 'bütçe', 'takvim', 'sürüm', 'ölçüm', 'nebula', 'calibration', 'source',
         'experiment', 'owner', 'decision', 'budget', 'release', 'measure', 'latency', 'index',
         've', 'ile', 'için', 'bir', 'the', 'and', 'of', 'next', 'step', 'notları', 'kitabı',
         'örneği', 'temizliği', 'performans', 'kanca', 'oturum', 'bağlam', 'receipt', 'sync')
sys.dont_write_bytecode = True


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def environment(home, tmp=None, nospawn=False):
    """Isolated synthetic home, preserving Windows loader prerequisites only."""
    home = Path(home).resolve()
    tmp = Path(tmp or home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
               LOCALAPPDATA=str(home / 'localappdata'), TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp),
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.defpath,
               PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               BEYIN_UPDATES_OFF='1', BEYIN_JEV_DISABLE='1', TZ='UTC')
    if nospawn:
        env['BEYIN_V3_NO_SPAWN'] = '1'
    return env


def checked(command, vault, env, payload=None, shell=False):
    result = subprocess.run(command, cwd=vault, env=env, shell=shell,
                            input=json.dumps(payload) if payload is not None else None,
                            text=True, encoding='utf-8', capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {result.stderr}\n{result.stdout}')
    return result


def modules(vault):
    """Load the installed copy, never a globally installed or user runtime."""
    directory = str(Path(vault) / '.claude/scripts')
    for name in list(sys.modules):
        if name == 'beyin_v3' or name.startswith('beyin_v3_'):
            del sys.modules[name]
    sys.path.insert(0, directory)
    try:
        return importlib.import_module('beyin_v3_sync'), importlib.import_module('beyin_v3_hook')
    finally:
        sys.path.remove(directory)


def authored_receipt(sync, engine, event_id, summary, refs, at=FIXED_DATE, session=None):
    """Use the real writer with a synthetic clock, so repeated setup is identical."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    with patch.object(sync, 'datetime', Clock):
        return engine.receipt(event_id, summary, refs, 'claude', session=session)


def fixture_hash(vault):
    """Hash synthetic inputs, excluding installed path-dependent launchers/state.

    Framing: sorted POSIX relative paths, NUL, 8-byte big-endian size, file bytes.
    Receipt projections are derived outputs, so only their canonical sources count.
    """
    paths = [p for directory in ('notes', 'receipts', '🔮 850-Companion')
             for p in (vault / directory).rglob('*') if p.is_file()]
    paths.append(vault / '.beyin-preferences.json')
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.relative_to(vault).as_posix()):
        data = path.read_bytes()
        digest.update(path.relative_to(vault).as_posix().encode('utf-8') + b'\0')
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def generate(directory, profile, seed=233, scale=1.0):
    directory = Path(directory).resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('--out fixture directory must be empty (existing data is never removed)')
    vault, state = directory / 'vault', directory / 'state'
    vault.mkdir(parents=True, exist_ok=True)
    env = environment(directory / 'home', nospawn=True)
    checked([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(vault),
             '--state', str(state)], vault, env)
    sync, _ = modules(vault)
    rng = random.Random(seed)
    counts = dict(day1=(40, 2), active=(1500, 300), mega=(10000, 1000), edge=(100, 10))
    notes, receipts = (max(1, round(n * scale)) for n in counts[profile])
    note = vault / 'notes/000/000/note-000000.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text('# Synthetic nebula calibration\n', encoding='utf-8')
    # receipt() writes, validates, journals, projects and syncs every event. Generate
    # them before bulk notes to avoid paying 300 full 1500-note scans during setup.
    engine = sync.SyncEngine(vault, state)
    for index in range(receipts):
        authored_receipt(sync, engine, f'perf-{index:06d}', f'Synthetic calibration outcome {index:06d}.',
                         ['notes/000/000/note-000000.md'], FIXED_DATE + timedelta(seconds=index),
                         session='perf-fixture')
        if index % 25 == 0:
            print(f'Generating {profile}: receipt {index + 1}/{receipts}', flush=True)
    for index in range(notes):
        path = vault / f'notes/{index % 20:03d}/{index % 7:03d}/note-{index:06d}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        # Block YAML like a real vault, and bodies of realistic, varied length (about
        # 0.4-4 KB of mixed Turkish/English prose under two headings).
        frontmatter = (f'---\nid: perf-note-{index:06d}\nkind: note\nrevision: 1\nproject: nebula\n'
                       f'visibility: internal\ntitle: Calibration {index}\n---\n')
        paragraphs = []
        for _ in range(rng.randrange(2, 12)):
            paragraphs.append(' '.join(rng.choice(WORDS) for _ in range(rng.randrange(20, 60))).capitalize() + '.')
        half = len(paragraphs) // 2
        body = (f'# Nebula calibration {index}\n\nSynthetic owner {rng.randrange(1000)}. '
                'Verify calibration sources before the next experiment. 🧠\n\n' +
                '\n\n'.join(paragraphs[:half]) + '\n\n## Ayrıntılar\n\n' + '\n\n'.join(paragraphs[half:]) +
                '\n\n' + ' '.join(f'#tag{index * 6 + j}' for j in range(6)) + '\n')
        text = frontmatter + body
        if profile == 'edge':
            cases = ('---\nid: edge\nodd:\n  nested:\n    bad: true\n---\n',
                     '---\ntags: [one, "two"]\naliases: {{placeholder}}\n---\n',
                     '---\nunterminated: [\n---\n', '')
            text = cases[index % len(cases)] + body + 'long-line ' * 10000 + '\n'
            text += 'SYNTHETIC ONLY: sk-' + 'Z' * 48 + '\npassword = "FAKE_ONLY_233"\n'
            text += 'ghp_' + 'A' * 36 + '\n'
        path.write_text(text, encoding='utf-8')
    if profile == 'mega':
        for index in range(3):
            size = max(1024, round((1 + index / 2) * 1024 * 1024 * scale))
            line = 'Synthetic archive paragraph with calibration data. #archive\n'
            (vault / f'notes/archive-{index}.md').write_text((line * (size // len(line) + 1))[:size],
                                                                        encoding='utf-8')
    companion = vault / '🔮 850-Companion'
    companion.mkdir(exist_ok=True)
    for name, size in (('Last-Session.md', 3000), ('Threads.md', 8000), ('Kurallar.md', 200)):
        if profile == 'day1':
            size = 240
        line = f'Synthetic {name}: calibration next step, verify source. 🧠\n'
        (companion / name).write_text((line * (size // len(line) + 1))[:size], encoding='utf-8')
    write_json(vault / '.beyin-preferences.json', dict(auto_sync=True, interval_minutes=0,
               context_mode='turn', context_chars=5000, secret_filter=profile == 'edge'))
    # Keep source mtimes deterministic as well as content; cold scans cannot hit a
    # receipt-directory cache from the generator's partial index.
    for path in sorted(vault.rglob('*'), reverse=True):
        if path.is_file() or path.is_dir():
            os.utime(path, (FIXED_DATE.timestamp(), FIXED_DATE.timestamp()))
    info = dict(profile=profile, seed=seed, scale=scale, notes=notes, receipts=receipts,
                fixture_sha256=fixture_hash(vault),
                fixture_hash_scope='notes/, receipts/, companion/, .beyin-preferences.json; sorted path+NUL+size+bytes',
                updates_off=True, jev_disabled=True, state_outside_vault=True)
    write_json(directory / 'fixture.json', info)
    print('Fixture sha256: ' + info['fixture_sha256'], flush=True)
    return info


def filesystem_type(path):
    path = str(Path(path).resolve())
    candidates = []
    try:
        if sys.platform == 'darwin':
            output = subprocess.run(['mount'], capture_output=True, text=True, timeout=10, check=True).stdout
            for line in output.splitlines():
                match = re.search(r' on (.+) \(([^, )]+)', line)
                if match:
                    candidates.append(match.groups())
        elif sys.platform.startswith('linux'):
            for line in Path('/proc/mounts').read_text(encoding='utf-8').splitlines():
                fields = line.split()
                mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[1])
                candidates.append((mount, fields[2]))
        elif os.name == 'nt':
            import ctypes
            volume = ctypes.create_unicode_buffer(261)
            kind = ctypes.create_unicode_buffer(261)
            if ctypes.windll.kernel32.GetVolumePathNameW(path, volume, len(volume)):
                if ctypes.windll.kernel32.GetVolumeInformationW(volume.value, None, 0, None, None, None, kind, len(kind)):
                    return kind.value
        matches = [(mount, kind) for mount, kind in candidates
                   if path == mount or path.startswith(mount.rstrip('/') + '/')]
        return max(matches, key=lambda pair: len(pair[0]))[1] if matches else 'unknown'
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return 'unknown'


def env_info(vault, fixture, repeat):
    try:
        sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                             text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = None
    path = str(vault).casefold()
    return dict(platform=platform.platform(), machine=platform.machine(), cpu_count=os.cpu_count(),
                python_version=platform.python_version(), python_implementation=platform.python_implementation(),
                filesystem_type=filesystem_type(vault), vault_path=str(vault),
                under_icloud='icloud' in path or 'mobile documents' in path,
                under_onedrive='onedrive' in path, git_sha=sha,
                fixture_sha256=fixture['fixture_sha256'], repeat_count=repeat,
                timestamp=datetime.now(timezone.utc).isoformat(), updates_off=True,
                network_policy='BEYIN_UPDATES_OFF=1; BEYIN_JEV_DISABLE=1; no network commands')


def replace_tree(source, destination):
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def wait_file(path, timeout=60):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Timed out waiting for {path.name}')
        time.sleep(0.01)


def lock_worker(vault, state, coordination, role):
    """Execute installed companion-compact, observing its actual advisory lock.

    A deterministic 250ms overlap after process two reaches the lock removes the
    scheduler race of two fast compactions. Startup is excluded from lock wait.
    """
    modules(vault)
    compact = importlib.import_module('beyin_v3_compact')
    original = compact._compact_lock
    coordination = Path(coordination)

    @contextmanager
    def observed(target, **kwargs):
        if role == 'second':
            (coordination / 'second-ready').touch()
        started = time.perf_counter()
        with original(target, **kwargs):
            waited = (time.perf_counter() - started) * 1000
            if role == 'first':
                (coordination / 'first-held').touch()
                wait_file(coordination / 'second-ready')
                time.sleep(0.25)
            yield
        if role == 'second':
            write_json(coordination / 'wait.json', dict(wait_ms=waited))

    with patch.object(compact, '_compact_lock', observed), patch.object(sys, 'argv',
            ['beyin.py', '--vault', str(vault), '--state', str(state), 'companion-compact']):
        runpy.run_path(str(vault / '.claude/scripts/beyin_v3_cli.py'), run_name='__main__')


class Benchmark:
    def __init__(self, root, profile, seed, scale):
        self.root = Path(root)
        self.fixture = generate(self.root / 'work', profile, seed, scale)
        self.vault, self.state = self.root / 'work/vault', self.root / 'work/state'
        self.home = self.root / 'work/home'
        self.env = environment(self.home)
        self.sync, self.hook = modules(self.vault)
        with patch.dict(os.environ, self.env, clear=True):
            report = self.engine().sync()
        if report['conflicts']:
            raise AssertionError(f'Pristine fixture conflicts: {report}')
        self.baseline_status = report['status']
        # Warm receipt scan cache is materialized without a 2s sleep. This also
        # exercises remote replacement of a previously cached receipt directory.
        receipts = self.vault / 'receipts'
        os.utime(receipts, (FIXED_DATE.timestamp(), FIXED_DATE.timestamp()))
        with patch.dict(os.environ, self.env, clear=True):
            self.engine().sync()
        self.pristine = self.root / 'pristine'
        shutil.copytree(self.root / 'work', self.pristine)
        self.commands = json.loads((self.vault / '.claude/settings.local.json').read_text(encoding='utf-8'))['hooks']

    def engine(self):
        return self.sync.SyncEngine(self.vault, self.state)

    def reset(self):
        replace_tree(self.pristine, self.root / 'work')

    def remote_receipt(self, event_id, summary, at=FIXED_DATE):
        # Another device's valid receipt, authored through the same engine API.
        other = self.root / 'other-device'
        if other.exists():
            shutil.rmtree(other)
        ref = other / 'vault/notes/000/000/note-000000.md'
        ref.parent.mkdir(parents=True)
        ref.write_text('# Synthetic remote calibration\n', encoding='utf-8')
        remote = self.sync.SyncEngine(other / 'vault', other / 'state')
        result = authored_receipt(self.sync, remote, event_id, summary,
                                  ['notes/000/000/note-000000.md'], at)
        return result['source'], (other / 'vault' / result['source']).read_bytes()

    def divergence(self):
        relative, rewritten = self.remote_receipt('perf-000000',
                                                   'Synthetic remote device rewrote the outcome.')
        source = self.vault / relative
        original = source.read_bytes()
        source.unlink()  # atomic replacement, as a sync client would do
        source.write_bytes(rewritten)
        return source, original

    @staticmethod
    def assert_divergence(result, source):
        assert any(c.get('kind') == 'receipt_divergence' and c.get('source') == source
                   for c in result['conflicts']), f'Receipt divergence disappeared: {result}'

    def prepare(self, scenario):
        self.reset()
        if scenario not in ('cold_process', 'sync_cold'):
            # copytree changes the receipt directory's inode, an input to the
            # runtime cache key. Rewarm outside timing after each restoration.
            refreshed = self.engine().sync()
            assert not refreshed['conflicts'], refreshed
        if scenario == 'sync_cold':
            shutil.rmtree(self.state)
        elif scenario == 'sync_one_note':
            path = self.vault / 'notes/000/000/note-000000.md'
            path.write_text(path.read_text(encoding='utf-8') + '\nSynthetic new calibration step.\n', encoding='utf-8')
        elif scenario == 'sync_one_receipt':
            relative, content = self.remote_receipt('perf-new-event',
                                                    'Synthetic newly verified calibration.',
                                                    FIXED_DATE + timedelta(days=1))
            (self.vault / relative).write_bytes(content)
        elif scenario in ('sync_divergent_repeat', 'sync_after_resolution'):
            source, original = self.divergence()
            if scenario == 'sync_after_resolution':
                self.assert_divergence(self.engine().sync(), source.relative_to(self.vault).as_posix())
                source.unlink()
                source.write_bytes(original)
                resolved = self.engine().sync()
                assert not resolved['conflicts'], resolved
                self.engine().sync()  # establish warm state after resolution
        if scenario.startswith('hook_'):
            name = scenario.removeprefix('hook_').removesuffix('_nospawn').removesuffix('_direct')
            self.payload = dict(hook_event_name=EVENTS[name], session_id='perf-session',
                                event_id='perf-hook-event', cwd=str(self.vault))
            if name == 'user_prompt':
                self.payload['prompt'] = 'What is the next nebula calibration step?'
            if name == 'post_tool_use':
                self.payload.update(tool_name='Write', tool_input=dict(file_path=str(
                    self.vault / 'notes/000/000/note-000000.md')))

    def hook_response(self, response):
        assert isinstance(response, dict), 'Hook response must be a JSON object'
        text = response.get('hookSpecificOutput', {}).get('additionalContext', '')
        units = len(text.encode('utf-16-le')) // 2
        assert units <= 10000, f'additionalContext exceeded issue #175 budget: {units}'
        return dict(additional_context_utf16_units=units, sync_pending='sync is pending' in text)

    def run_lock(self):
        coordination = self.root / 'lock-coordination'
        if coordination.exists():
            shutil.rmtree(coordination)
        coordination.mkdir()
        processes = []
        try:
            for role in ('first', 'second'):
                env = environment(self.home, self.root / ('tmp-' + role))
                command = [sys.executable, str(Path(__file__).resolve()), '_lock_worker',
                           str(self.vault), str(self.root / ('lock-state-' + role)), str(coordination), role]
                processes.append(subprocess.Popen(command, cwd=self.vault, env=env,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                if role == 'first':
                    wait_file(coordination / 'first-held')
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                if process.returncode:
                    raise RuntimeError(stderr.decode('utf-8', errors='replace'))
                result = json.loads(stdout)
                assert result['status'] not in ('conflict', 'needs_attention'), result
            wait = json.loads((coordination / 'wait.json').read_text(encoding='utf-8'))['wait_ms']
            return wait, dict(controlled_overlap_ms=250, different_state_dirs=True, different_tmpdirs=True)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            for role in ('first', 'second'):
                path = self.root / ('lock-state-' + role)
                if path.exists():
                    shutil.rmtree(path)

    def action(self, scenario, in_process=False):
        details = {}
        if scenario == 'lock_two_process':
            if in_process:
                raise ValueError('lock_two_process has no single-process profiler equivalent')
            return self.run_lock()
        if scenario == 'cold_process':
            if in_process:
                raise ValueError('cold_process has no in-process cold-import equivalent; profile a sync or hook')
            command = [sys.executable, '-c',
                       'import sys; sys.path.insert(0, sys.argv[1]); import beyin_v3_hook',
                       str(self.vault / '.claude/scripts')]
            started = time.perf_counter()
            checked(command, self.vault, self.env)
        elif scenario in SYNC_SCENARIOS:
            engine = self.engine()
            started = time.perf_counter()
            reports, timings = [], []
            for _ in range(3 if scenario == 'sync_divergent_repeat' else 1):
                step = time.perf_counter()
                reports.append(engine.sync())
                timings.append((time.perf_counter() - step) * 1000)
            elapsed = (time.perf_counter() - started) * 1000
            for result in reports:
                if scenario == 'sync_divergent_repeat':
                    source = 'receipts/' + hashlib.sha256(b'perf-000000').hexdigest() + '.md'
                    self.assert_divergence(result, source)
                else:
                    assert not result['conflicts'], result
            details.update(statuses=[r['status'] for r in reports], sync_samples_ms=timings,
                           conflict_counts=[len(r['conflicts']) for r in reports],
                           warning_counts=[len(r['warnings']) for r in reports])
            return elapsed, details
        else:
            nospawn = scenario.endswith('_nospawn') or in_process
            env = environment(self.home, nospawn=nospawn)
            direct = [sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'),
                      '--vault', str(self.vault), '--state', str(self.state), '--harness', 'claude']
            installed = self.commands[self.payload['hook_event_name']][0]['hooks'][0]['command']
            started = time.perf_counter()
            if in_process:
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', direct[1:]), \
                        patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload))), redirect_stdout(output):
                    self.hook.main()
                raw = output.getvalue()
            else:
                use_direct = scenario.endswith('_direct')
                raw = checked(direct if use_direct else installed, self.vault, env,
                              self.payload, shell=not use_direct).stdout
            elapsed = (time.perf_counter() - started) * 1000
            details.update(self.hook_response(json.loads(raw)))
            details.update(worker_spawn_enabled=not nospawn,
                           launcher='direct_python' if scenario.endswith('_direct') or in_process else 'installed_command')
            if not nospawn:
                done = self.state / 'hook-done' / (hashlib.sha256(b'perf-hook-event').hexdigest() + '.json')
                wait_file(done)
                # done is written at the end of drain_queue; wait until the queue is empty.
                assert not list((self.state / 'hook-queue').glob('*.json'))
                health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
                details['worker_sync_status'] = health['sync']['status']
            return elapsed, details
        return (time.perf_counter() - started) * 1000, details


def summary(samples, details):
    ordered = sorted(samples)
    # Nearest rank p95: reproducible for small N, no interpolated pseudo-samples.
    return dict(n=len(samples), median_ms=statistics.median(samples),
                p95_ms=ordered[math.ceil(0.95 * len(ordered)) - 1], min_ms=min(samples),
                max_ms=max(samples), samples_ms=samples, details=details)


def selected(args):
    return args.scenario or list(SCENARIOS)


def run(args):
    with tempfile.TemporaryDirectory(prefix='beyin-perf-', dir=args.work_dir) as root:
        bench = Benchmark(root, args.profile, args.seed, args.scale)
        result = dict(schema_version=1, mode='timing', profiler_enabled=False,
                      fixture=bench.fixture, environment=env_info(bench.vault, bench.fixture, args.repeat),
                      baseline_status=bench.baseline_status, scenarios={}, skipped_scenarios={},
                      measurement_notes=dict(reset='pristine vault, state and home copied before every repeat; untimed sync rebases directory identity caches for warm scenarios',
                          sync_one_receipt='real API authors receipt on synthetic second device; source copied before timing, measured index has not seen the new event',
                          sync_divergent_repeat='each sample is the total wall time of three sync calls; each must expose receipt_divergence',
                          lock_two_process='actual second-process lock acquisition wait; controlled 250ms holder overlap',
                          hooks='end-to-end command time; detached worker completion is awaited outside timing',
                          p95='nearest rank'))
        with patch.dict(os.environ, bench.env, clear=True):
            for scenario in selected(args):
                if scenario.endswith('_direct') and os.name != 'nt':
                    result['skipped_scenarios'][scenario] = 'POSIX installed command already launches Python directly'
                    continue
                samples, details = [], []
                for index in range(args.repeat):
                    bench.prepare(scenario)
                    elapsed, observation = bench.action(scenario)
                    samples.append(elapsed)
                    details.append(observation)
                    print(f'{scenario} {index + 1}/{args.repeat}: {elapsed:.2f} ms', flush=True)
                result['scenarios'][scenario] = summary(samples, details)
                write_json(args.out, result)  # a completed block survives an interrupted run
        write_json(args.out, result)


def profile_run(args):
    if args.scenario in ('cold_process', 'lock_two_process'):
        raise ValueError('Choose a sync or hook scenario for an in-process profile')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='beyin-profile-', dir=args.work_dir) as root:
        bench = Benchmark(root, args.profile, args.seed, args.scale)
        with patch.dict(os.environ, bench.env, clear=True):
            bench.prepare(args.scenario)
            profiler = cProfile.Profile()
            tracemalloc.start()
            try:
                profiler.enable()
                _, details = bench.action(args.scenario, in_process=True)
                profiler.disable()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                profiler.disable()
                tracemalloc.stop()
        profiler.dump_stats(str(out / (args.scenario + '.pstats')))
        text = io.StringIO()
        pstats.Stats(profiler, stream=text).sort_stats('cumulative').print_stats(40)
        (out / (args.scenario + '.txt')).write_text(text.getvalue(), encoding='utf-8')
        write_json(out / (args.scenario + '.json'), dict(schema_version=1, mode='profile',
                   scenario=args.scenario, tracemalloc_peak_bytes=peak, details=details,
                   environment=env_info(bench.vault, bench.fixture, 1),
                   hook_equivalent='in-process main, patched argv/stdin, worker spawning disabled'))
        print(f'Profile saved to {out}; tracemalloc peak: {peak} bytes', flush=True)


def report(path):
    result = json.loads(Path(path).read_text(encoding='utf-8'))
    print('| scenario | n | median ms | p95 ms | min | max |')
    print('| --- | ---: | ---: | ---: | ---: | ---: |')
    for name, row in result['scenarios'].items():
        print(f"| {name} | {row['n']} | {row['median_ms']:.2f} | {row['p95_ms']:.2f} | {row['min_ms']:.2f} | {row['max_ms']:.2f} |")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def positive_scale(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('generate', 'run', 'profile'):
        child = sub.add_parser(command)
        child.add_argument('--profile', choices=PROFILES, required=True)
        child.add_argument('--out', required=True, type=Path)
        child.add_argument('--seed', type=int, default=233)
        child.add_argument('--scale', type=positive_scale, default=1.0)
        if command != 'generate':
            child.add_argument('--work-dir', type=Path, help='parent of temporary vault (choose filesystem to measure)')
            child.add_argument('--scenario', choices=SCENARIOS, action='append' if command == 'run' else 'store',
                               required=command == 'profile', help='repeat flag to select timing blocks')
        if command == 'run':
            child.add_argument('--repeat', type=positive_int, default=5)
    sub.add_parser('report').add_argument('results', type=Path)
    internal = sub.add_parser('_lock_worker', help=argparse.SUPPRESS)
    internal.add_argument('vault', type=Path)
    internal.add_argument('state', type=Path)
    internal.add_argument('coordination', type=Path)
    internal.add_argument('role', choices=('first', 'second'))
    args = parser.parse_args()
    if args.command == 'generate':
        generate(args.out, args.profile, args.seed, args.scale)
    elif args.command == 'run':
        run(args)
    elif args.command == 'profile':
        profile_run(args)
    elif args.command == 'report':
        report(args.results)
    else:
        lock_worker(args.vault, args.state, args.coordination, args.role)


if __name__ == '__main__':
    main()
