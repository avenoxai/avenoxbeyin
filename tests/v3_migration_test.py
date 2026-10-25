"""Synthetic migration/continuity acceptance tests, fixed before implementation."""
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from v3_package_helpers import ROOT, inherited_env

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))


def flush_record(status='inflight', age=7200, **extra):
    """A sentinel shaped like the one stock V2 flush writes; age is seconds since its recorded start."""
    return json.dumps(dict({'session_id': 'example', 'ts': int(time.time()) - age, 'status': status}, **extra))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'vault'
        self.root.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.m = importlib.import_module('beyin_v3_migrate')

    def source(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('utf-8'))
        return p

    def lock_directory(self, name='prompt_count.' + 'a' * 64 + '.lock', days=0):
        """The directory V2's prompt counter makes with mkdir and normally removes again."""
        path = self.root / '.claude/scripts/.state' / name
        path.mkdir(parents=True)
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_sources_and_v2_state_preserved_idempotent_cutover(self):
        paths = [self.source('daily/2025-01-01.md', 'Human day\r\n'),
                 self.source('knowledge/idea.md', 'Existing knowledge ölçüm\n'),
                 self.source('Companion/Core.md', 'Existing persona\n'),
                 self.source('.claude/scripts/.state/compile-state.json', '{"last":"2025-01-01"}')]
        before = {p: p.read_bytes() for p in paths}
        first = self.m.migrate_v2(self.root, self.state)
        second = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(first['cutover_at'], second['cutover_at'])
        self.assertTrue(all(p.read_bytes() == value for p, value in before.items()))
        self.assertTrue((self.state / 'v2-migration.json').exists())

    def test_inflight_writer_blocks_without_success_receipt(self):
        self.source('.claude/scripts/.state/flush-example.json', '{"status":"inflight"}')
        with self.assertRaisesRegex(RuntimeError, r'flush-example\.json.*reconcile'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_orphaned_inflight_is_recorded_when_its_writer_lock_is_free(self):
        # V2 flush holds flush-<key>.lock from before it writes "inflight" until its terminal
        # state. A guard that holds that lock has proof the writer died mid-summary.
        record = flush_record()
        sentinel = self.source('.claude/scripts/.state/flush-example.json', record)
        self.source('.claude/scripts/.state/flush-example.lock', '')
        result = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(result['orphaned_legacy_state'], ['.claude/scripts/.state/flush-example.json'])
        self.assertEqual(sentinel.read_bytes(), record.encode('utf-8'))
        self.assertTrue((self.state / 'v2-preserved-state/.claude/scripts/.state/flush-example.json').exists())

    def test_free_lock_does_not_prove_a_recent_or_undated_sentinel(self):
        # The state directory travels with a synced vault and a lock does not: a flush that is
        # still running on another machine leaves this same picture, a fresh sentinel beside a
        # lock that is free here. Only a start time an hour old rules that out.
        recent = self.m.ORPHANED_FLUSH_SECONDS - 120
        for label, record in (('just started', flush_record(age=0)),
                              ('under the hour', flush_record(age=recent)),
                              ('clock ahead', flush_record(age=-86400)),
                              ('no ts', '{"session_id":"example","status":"inflight"}'),
                              ('null ts', '{"session_id":"example","ts":null,"status":"inflight"}'),
                              ('text ts', '{"session_id":"example","ts":"1789936860","status":"inflight"}'),
                              ('boolean ts', '{"session_id":"example","ts":true,"status":"inflight"}'),
                              ('negative ts', '{"session_id":"example","ts":-1,"status":"inflight"}'),
                              ('not a number', '{"session_id":"example","ts":NaN,"status":"inflight"}')):
            with self.subTest(label=label):
                sentinel = self.source('.claude/scripts/.state/flush-example.json', record)
                self.source('.claude/scripts/.state/flush-example.lock', '')
                with self.assertRaisesRegex(RuntimeError, r'flush-example\.json; its lock is free here.*60 minutes old.*'
                                                          r'Rerun once it is older.*terminal value such as "fail"'):
                    self.m.migrate_v2(self.root, self.state)
                self.assertFalse((self.state / 'v2-migration.json').exists())
                self.assertEqual(sentinel.read_bytes(), record.encode('utf-8'))

    def test_sentinel_at_the_hour_is_old_enough(self):
        self.source('.claude/scripts/.state/flush-example.json', flush_record(age=self.m.ORPHANED_FLUSH_SECONDS))
        self.source('.claude/scripts/.state/flush-example.lock', '')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['orphaned_legacy_state'], ['.claude/scripts/.state/flush-example.json'])

    @unittest.skipUnless(os.name == 'posix', 'drives stock V2 flush with a shell-script model and a process-group kill')
    def test_stock_v2_flush_holds_its_lock_for_as_long_as_it_says_inflight(self):
        # The claim the orphan rule rests on, checked against the V2 writer as shipped: while the
        # real flush is mid-summary its sentinel says "inflight" and its lock cannot be taken;
        # once that process is killed the same sentinel stays behind and the lock is free.
        scripts = self.root / '.claude/scripts'
        legacy = scripts / '.state'
        legacy.mkdir(parents=True)
        for name in ('flush.py', '_portalock.py'):
            shutil.copyfile(ROOT / 'template/.claude/scripts' / name, scripts / name)
        tools = Path(self.temp.name) / 'bin'
        tools.mkdir()
        model = tools / 'claude'
        model.write_bytes(b'#!/bin/sh\ncat >/dev/null\nsleep 300\n')
        model.chmod(0o755)
        transcript = Path(self.temp.name) / 'transcript.jsonl'
        transcript.write_bytes((json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'Synthetic question.'}}) + '\n' +
                                json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Synthetic answer.'}}) + '\n').encode('utf-8'))
        hook_input = legacy / 'hookin-1.json'
        hook_input.write_bytes(json.dumps({'session_id': 'synthetic-session', 'transcript_path': str(transcript)}).encode('utf-8'))
        # flush records the event time it is given; a writer that died two hours ago is the case.
        started = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        env = inherited_env(PATH=str(tools) + os.pathsep + os.environ.get('PATH', os.defpath), HOME=self.temp.name,
                            TMPDIR=self.temp.name, PYTHONDONTWRITEBYTECODE='1', BEYIN_FAKE_NOW=started)
        env.pop('BEYIN_INVOKED_BY', None)
        env.pop('BEYIN_MODEL_RUNNER', None)
        writer = subprocess.Popen([sys.executable, str(scripts / 'flush.py'), '--hook-input', str(hook_input)],
                                  cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)

        stopped = False

        def stop():
            nonlocal stopped
            # Cleanup runs again after the explicit stop; do not reuse the reaped writer's PGID.
            if stopped:
                return
            try:
                os.killpg(writer.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            writer.wait(timeout=60)
            stopped = True
        self.addCleanup(stop)
        sentinels = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and writer.poll() is None:
            sentinels = [path for path in legacy.glob('flush-*.json') if b'"inflight"' in path.read_bytes()]
            if sentinels:
                break
            time.sleep(0.05)
        self.assertEqual(len(sentinels), 1, 'stock flush never reached its model call')
        sentinel = sentinels[0]
        self.assertTrue(sentinel.with_suffix('.lock').is_file())
        with self.assertRaisesRegex(RuntimeError, 'legacy writer lock is active'):
            with self.m.migration_guard(self.root, self.state):
                pass
        stop()
        left = sentinel.read_bytes()
        self.assertEqual(json.loads(left)['status'], 'inflight')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['orphaned_legacy_state'], [sentinel.relative_to(self.root).as_posix()])
        self.assertEqual(sentinel.read_bytes(), left)

    def test_inflight_with_an_active_writer_lock_still_blocks(self):
        self.source('.claude/scripts/.state/flush-example.json', flush_record())
        lock = self.source('.claude/scripts/.state/flush-example.lock', '0')
        with self.m._legacy_lock(lock):
            with self.assertRaisesRegex(RuntimeError, 'legacy writer lock'):
                self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_inflight_under_another_writers_lock_still_blocks(self):
        # Only the sentinel's own lock is proof: compile.lock says nothing about a claim
        # in compile-state.json.
        self.source('.claude/scripts/.state/compile-state.json', '{"state":"running"}')
        self.source('.claude/scripts/.state/compile.lock', '')
        with self.assertRaisesRegex(RuntimeError, r'compile-state\.json.*reconcile'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_only_a_flush_inflight_claim_is_proven_by_its_lock(self):
        # Stock V2 also leaves antigravity-<key>.json next to antigravity-<key>.lock, and it
        # never writes "pending" or "running" at all: a free lock and an old start time prove
        # nothing about those.
        state_dir = self.root / '.claude/scripts/.state'
        for name, record in (('antigravity-example', flush_record(state='pending', status='ok')),
                             ('antigravity-example', flush_record()),
                             ('flush-example', flush_record(status='pending')),
                             ('flush-example', flush_record(status='running')),
                             ('flush-example', flush_record(state='pending'))):
            with self.subTest(name=name, record=record):
                sentinel = self.source('.claude/scripts/.state/' + name + '.json', record)
                lock = self.source('.claude/scripts/.state/' + name + '.lock', '')
                self.addCleanup(sentinel.unlink, missing_ok=True)
                self.addCleanup(lock.unlink, missing_ok=True)
                with self.assertRaisesRegex(RuntimeError, name + r'\.json; verify the prior write completed.*reconcile'):
                    self.m.migrate_v2(self.root, self.state)
                self.assertFalse((self.state / 'v2-migration.json').exists())
                sentinel.unlink()
                lock.unlink()
        self.assertEqual(list(state_dir.iterdir()), [])

    def test_blocking_sentinel_error_says_how_to_reconcile(self):
        # No lock of the same name: nothing is proven, however old the record is.
        self.source('.claude/scripts/.state/flush-example.json', flush_record())
        with self.assertRaisesRegex(RuntimeError, r'verify the prior write completed.*no V2 writer running.*terminal value such as "fail"'):
            self.m.migrate_v2(self.root, self.state)

    def test_orphaned_inflight_left_after_cutover_does_not_block_a_later_guard(self):
        # update and recover enter the same guard and take no flags, and a kept V2 flush can
        # die mid-summary long after the cutover.
        first = self.m.migrate_v2(self.root, self.state)
        self.source('.claude/scripts/.state/flush-later.json', flush_record())
        self.source('.claude/scripts/.state/flush-later.lock', '')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['orphaned_legacy_state'], ['.claude/scripts/.state/flush-later.json'])
            self.assertEqual(self.m.finalize_migration(self.root, self.state, plan), first)

    def test_prompt_counter_lock_directory_v2_itself_would_sweep_does_not_block(self):
        # A prompt-counter hook killed between its mkdir and its rmdir leaves this directory. No
        # V2 hook steals it; V2's own session-start sweep removes it once it is empty and eight
        # days old. The guard reads that rule and leaves the directory alone.
        lock = self.lock_directory(days=9)
        stamp = lock.lstat().st_mtime_ns
        relative = lock.relative_to(self.root).as_posix()
        result = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(result['stale_legacy_locks'], [relative])
        self.assertEqual(json.loads((self.state / 'v2-migration.json').read_text(encoding='utf-8'))['stale_legacy_locks'], [relative])
        self.assertTrue(lock.is_dir())
        self.assertEqual(list(lock.iterdir()), [])
        self.assertEqual(lock.lstat().st_mtime_ns, stamp)
        self.assertNotIn(relative, result['legacy_state'])

    def test_lock_directory_at_eight_days_is_stale(self):
        lock = self.lock_directory(days=self.m.STALE_LOCK_DIRECTORY_SECONDS / 86400)
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['stale_legacy_locks'], [lock.relative_to(self.root).as_posix()])

    def test_younger_prompt_counter_lock_directory_blocks_and_says_what_to_do(self):
        # Seconds old may be a live hook; 7.5 days old is one V2's own sweep would still keep.
        for days in (0, 7.5, -1):
            with self.subTest(days=days):
                lock = self.lock_directory(days=days)
                with self.assertRaisesRegex(RuntimeError, r'legacy writer lock directory in \.claude/scripts/\.state/prompt_count\.a{64}\.lock'
                                                          r' is less than 8 days old.*so rerun\..*no V2 session is running.*'
                                                          r'remove that empty directory and rerun') as raised:
                    self.m.migrate_v2(self.root, self.state)
                self.assertNotIn(self.temp.name, str(raised.exception))
                self.assertFalse((self.state / 'v2-migration.json').exists())
                self.assertTrue(lock.is_dir())
                lock.rmdir()

    def test_lock_directory_v2_would_never_sweep_blocks_however_old(self):
        # rmdir refuses a directory with something in it, and the sweep only knows the prompt
        # counter's name: neither is a lock the guard can read as released.
        for name, inside in (('prompt_count.' + 'a' * 64 + '.lock', 'note.txt'), ('compile.lock', None), ('flush-example.lock', None)):
            with self.subTest(name=name):
                lock = self.lock_directory(name)
                if inside:
                    (lock / inside).write_bytes(b'synthetic')
                stamp = time.time() - 30 * 86400
                os.utime(lock, (stamp, stamp))
                with self.assertRaisesRegex(RuntimeError, r'legacy writer lock in \.claude/scripts/\.state/' + re.escape(name) +
                                                          r' is a directory that is not an empty V2 prompt-counter lock;'
                                                          r' review it locally and move it out of the V2 state directory'):
                    self.m.migrate_v2(self.root, self.state)
                self.assertFalse((self.state / 'v2-migration.json').exists())
                self.assertTrue(lock.is_dir())
                if inside:
                    (lock / inside).unlink()
                lock.rmdir()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs a FIFO')
    def test_lock_that_is_not_a_regular_file_blocks_without_being_opened(self):
        fifo = self.root / '.claude/scripts/.state/flush-example.lock'
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        with self.assertRaisesRegex(RuntimeError, r'legacy writer lock in \.claude/scripts/\.state/flush-example\.lock'
                                                  r' is not a regular file; review it locally and move it out'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_lock_symlink_is_refused_with_its_path(self):
        target = self.source('.claude/scripts/.state/elsewhere.txt', '')
        try:
            target.with_name('flush-example.lock').symlink_to(target)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(RuntimeError, r'legacy writer lock in \.claude/scripts/\.state/flush-example\.lock is a symlink; review it locally'):
            self.m.migrate_v2(self.root, self.state)

    def low_open_file_limit(self, locks):
        """locks V2 flush locks under a soft open-file limit of 256, the macOS default; restored after."""
        import resource
        for index in range(locks):
            self.source('.claude/scripts/.state/flush-%04d.lock' % index, '')
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 2 * locks:
            self.skipTest('hard open-file limit too low to hold the locks at all')
        # Only the soft limit: an unprivileged process cannot raise a lowered hard limit again.
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        return resource, (256, hard)

    @unittest.skipUnless(os.name == 'posix', 'the soft open-file limit is a POSIX rlimit')
    def test_hundreds_of_v2_locks_fit_under_a_low_open_file_limit(self):
        # V2 flush leaves one flush-<key>.lock per summarized session and never removes it, so a
        # long-used vault holds hundreds, and the guard holds every one open until the cutover ends.
        resource, limit = self.low_open_file_limit(400)
        with self.m.migration_guard(self.root, self.state):
            self.assertGreater(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 400)
            # Every lock is really held: another open description cannot take one.
            with self.assertRaisesRegex(RuntimeError, 'legacy writer lock is active'):
                with self.m._legacy_lock(self.root / '.claude/scripts/.state/flush-0399.lock'):
                    pass
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), limit)
        with self.m._legacy_lock(self.root / '.claude/scripts/.state/flush-0399.lock'):
            pass

    @unittest.skipUnless(os.name == 'posix', 'the soft open-file limit is a POSIX rlimit')
    def test_an_open_file_limit_it_cannot_raise_is_named_in_the_error(self):
        from unittest import mock
        resource, limit = self.low_open_file_limit(400)
        with mock.patch.object(resource, 'setrlimit', side_effect=ValueError('not allowed to raise maximum limit')):
            with self.assertRaisesRegex(RuntimeError, r'legacy writer lock in \.claude/scripts/\.state/flush-\d{4}\.lock'
                                                      r' cannot be opened: the open-file limit is too low for the 400 legacy'
                                                      r' writer locks .*ulimit -n 656\) and rerun') as raised:
                self.m.migrate_v2(self.root, self.state)
        self.assertNotIn('readable and writable', str(raised.exception))
        self.assertNotIn(self.temp.name, str(raised.exception))
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), limit)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_lock_that_cannot_be_opened_blocks_with_its_path(self):
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('root opens a file whatever its mode')
        lock = self.source('.claude/scripts/.state/flush-example.lock', '')
        lock.chmod(0o444)  # no write bit on POSIX, the read-only attribute on Windows: r+b fails on both
        self.addCleanup(lock.chmod, 0o644)
        with self.assertRaisesRegex(RuntimeError, r'legacy writer lock in \.claude/scripts/\.state/flush-example\.lock cannot be opened'
                                                  r' \(.+\); make it readable and writable for this user, then rerun') as raised:
            self.m.migrate_v2(self.root, self.state)
        self.assertNotIn(self.temp.name, str(raised.exception))
        self.assertFalse((self.state / 'v2-migration.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'directory permission bits')
    def test_state_directory_that_cannot_be_read_blocks_instead_of_passing_unseen(self):
        if os.geteuid() == 0:
            self.skipTest('root reads a directory whatever its mode')
        # A listing that fails used to look like an empty directory: the writer inside went
        # unseen and the cutover recorded no legacy state at all.
        sentinel = self.source('.claude/scripts/.state/flush-example.json', flush_record(age=0))
        self.source('.claude/scripts/.state/flush-example.lock', '')
        legacy = sentinel.parent
        self.addCleanup(legacy.chmod, 0o755)
        for mode in (0o000, 0o311, 0o444):
            with self.subTest(mode=oct(mode)):
                legacy.chmod(mode)
                with self.assertRaisesRegex(RuntimeError, r'legacy writer state directory \.claude/scripts/\.state cannot be read;'
                                                          r' make it readable for this user, then rerun'):
                    self.m.migrate_v2(self.root, self.state)
                self.assertFalse((self.state / 'v2-migration.json').exists())
        # Reading is all the guard needs: a read-only state directory is not an obstacle.
        legacy.chmod(0o755)
        sentinel.write_bytes(flush_record().encode('utf-8'))
        legacy.chmod(0o555)
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['orphaned_legacy_state'], ['.claude/scripts/.state/flush-example.json'])

    def test_writer_record_that_cannot_be_read_blocks_with_its_path(self):
        record = self.root / '.claude/scripts/.state/flush-example.json'
        record.mkdir(parents=True)  # reading it fails on every platform
        with self.assertRaisesRegex(RuntimeError, r'legacy writer state in \.claude/scripts/\.state/flush-example\.json cannot be read'
                                                  r' \(.+\); make it a readable file for this user') as raised:
            self.m.migrate_v2(self.root, self.state)
        self.assertNotIn(self.temp.name, str(raised.exception))
        record.rmdir()
        record.write_bytes(b'{"status": "infl')
        with self.assertRaisesRegex(RuntimeError, r'legacy writer state unreadable in \.claude/scripts/\.state/flush-example\.json; review locally'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_legacy_lock_is_guarded_but_excluded_from_inventory(self):
        lock = self.source('.claude/scripts/.state/compile.lock', '')
        state_file = self.source('.claude/scripts/.state/compile-state.json', '{"status":"ok"}')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertNotIn(lock.relative_to(self.root).as_posix(), plan['legacy_state'])
            self.assertIn(state_file.relative_to(self.root).as_posix(), plan['legacy_state'])

    def test_legacy_state_symlink_escape_rejected_without_copy(self):
        external = Path(self.temp.name)/'external'
        external.mkdir()
        (external/'flush-private.json').write_text('{}')
        parent = self.root/'.claude/scripts'
        parent.mkdir(parents=True)
        try:
            (parent/'.state').symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state/'v2-preserved-state').exists())

    def test_existing_active_legacy_lock_rejected(self):
        lock = self.source('.claude/scripts/.state/compile.lock', '0')
        with self.m._legacy_lock(lock):
            with self.assertRaisesRegex(RuntimeError, 'legacy writer lock'):
                self.m.migrate_v2(self.root, self.state)

    def test_uncut_legacy_runner_prevents_success_watermark(self):
        self.source('.claude/scripts/flush.py', 'print("old writer")\n')
        with self.assertRaisesRegex(RuntimeError, 'runner'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state/'v2-migration.json').exists())

    def test_concurrent_content_edit_blocks_finalize(self):
        p = self.source('Companion/Core.md', 'old')
        with self.m.migration_guard(self.root, self.state) as plan:
            p.write_text('new', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.m.finalize_migration(self.root, self.state, plan)
        self.assertEqual(p.read_text(), 'new')

    def test_finalize_with_an_earlier_success_receipt_ignores_later_source_edits(self):
        # An earlier cutover already succeeded, so finalizing again writes nothing and
        # must not fail because a note changed since the plan was taken (interrupted
        # update, edit, recover).
        p = self.source('Companion/Core.md', 'old')
        first = self.m.migrate_v2(self.root, self.state)
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['previous'], first)
            p.write_text('new', encoding='utf-8')
            self.assertEqual(self.m.finalize_migration(self.root, self.state, plan), first)
        self.assertEqual(p.read_text(), 'new')
        self.assertEqual(json.loads((self.state / 'v2-migration.json').read_text(encoding='utf-8')), first)

    def test_receipts_project_new_views_without_old_day_recapture(self):
        from beyin_v3_sync import SyncEngine
        day = self.source('daily/2025-01-01.md', 'Human daily stays exact\n')
        old_receipt = self.source('receipts/old.md', 'Historical receipt must not be recaptured')
        self.m.migrate_v2(self.root, self.state)
        source = self.source('notes/example.md', 'Synthetic completed artifact')
        engine = SyncEngine(self.root, self.state)
        engine.receipt('new-event', 'Verified synthetic outcome.', ['notes/example.md'], 'codex')
        engine.sync()
        daily = list((self.root / 'daily/v3').glob('*.md'))
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0].read_text().count('Verified synthetic outcome.'), 1)
        self.assertNotIn('Historical receipt', daily[0].read_text())
        self.assertEqual(day.read_bytes(), b'Human daily stays exact\n')
        self.assertEqual(old_receipt.read_text(), 'Historical receipt must not be recaptured')
        self.assertIn('Verified synthetic outcome.', (self.root / 'knowledge/v3/outcomes.md').read_text())

    def test_manual_generated_view_edit_preserved_and_reported(self):
        from beyin_v3_sync import SyncEngine
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        engine.receipt('one', 'One outcome.', ['notes/example.md'], 'codex')
        target = self.root / 'knowledge/v3/outcomes.md'
        target.write_text('Manual edit', encoding='utf-8')
        result = engine.sync()
        self.assertEqual(result['status'], 'degraded')
        self.assertTrue(any(w.get('kind') == 'receipt_view' for w in result['warnings']))
        self.assertEqual(target.read_text(), 'Manual edit')

    def test_companion_private_metadata_excluded(self):
        from beyin_v3_sync import SyncEngine
        self.source('Companion/Core.md', '---\n{"visibility":"private"}\n---\nPRIVATE_PERSONA_CANARY')
        engine = SyncEngine(self.root, self.state)
        engine.sync()
        self.assertTrue(engine.store.retrieve('PRIVATE_PERSONA_CANARY')['abstained'])

    def test_semantic_note_create_preserves_existing_source(self):
        from beyin_v3_sync import SyncEngine
        engine = SyncEngine(self.root, self.state)
        engine.note_create('knowledge/lesson.md', 'Learned synthetic lesson.', {'project': 'demo'})
        original = (self.root/'knowledge/lesson.md').read_bytes()
        with self.assertRaises(ValueError):
            engine.note_create('knowledge/lesson.md', 'Overwrite', {})
        self.assertEqual((self.root/'knowledge/lesson.md').read_bytes(), original)

    def test_receipt_gap_is_only_a_signal_and_closes_on_matching_receipt(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import record_checkpoints
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'codex', 'session': 'synthetic', 'at': .5},
                                    {'event': 'Stop', 'harness': 'codex', 'session': 'synthetic', 'at': 1}])
        engine.sync()
        gaps = json.loads((self.state/'receipt-gaps.json').read_text())
        self.assertEqual(gaps['potential_missing_receipts'], 1)
        self.assertFalse((self.root/'daily/v3').exists())
        engine.receipt('session-outcome', 'Explicit synthetic outcome.', ['notes/example.md'], 'codex', session='synthetic')
        engine.sync()
        self.assertEqual(json.loads((self.state/'receipt-gaps.json').read_text())['potential_missing_receipts'], 0)

    def test_prior_turn_receipt_does_not_cover_next_turn(self):
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import record_checkpoints
        import time
        self.source('notes/example.md', 'Synthetic source')
        engine = SyncEngine(self.root, self.state)
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'codex', 'session': 'same', 'at': time.time()-10}])
        engine.receipt('turn-one', 'First outcome.', ['notes/example.md'], 'codex', session='same')
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'codex', 'session': 'same', 'at': time.time()+1}, {'event': 'Stop', 'harness': 'codex', 'session': 'same', 'at': time.time()+2}])
        engine.sync()
        self.assertEqual(json.loads((self.state/'receipt-gaps.json').read_text())['potential_missing_receipts'], 1)

    def install_manifest(self, *kept):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'v3-install.json').write_text(
            json.dumps({'files': {}, 'kept_legacy': list(kept)}), encoding='utf-8')

    def test_guard_plan_reports_kept_legacy_runners_from_the_install_manifest(self):
        self.install_manifest('.claude/scripts/flush.py')
        self.source('.claude/scripts/flush.py', 'print("user owned flush")\n')
        with self.m.migration_guard(self.root, self.state) as plan:
            self.assertEqual(plan['kept_legacy'], ['.claude/scripts/flush.py'])

    def test_kept_legacy_runner_finalizes_unretired_and_unchanged(self):
        self.install_manifest('.claude/scripts/flush.py')
        runner = self.source('.claude/scripts/flush.py', 'print("user owned flush")\n')
        before = runner.read_bytes()
        result = self.m.migrate_v2(self.root, self.state)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(runner.read_bytes(), before)
        self.assertNotIn(b'BEYIN_V3_LEGACY_RETIRED', runner.read_bytes())
        self.assertTrue((self.state / 'v2-migration.json').exists())

    def test_kept_legacy_runner_symlink_is_still_refused(self):
        self.install_manifest('.claude/scripts/flush.py')
        external = Path(self.temp.name) / 'external-flush.py'
        external.write_text('print("external writer")\n')
        parent = self.root / '.claude/scripts'
        parent.mkdir(parents=True)
        try:
            (parent / 'flush.py').symlink_to(external)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.m.migrate_v2(self.root, self.state)
        self.assertFalse((self.state / 'v2-migration.json').exists())

    def test_legacy_tags_converted_to_list(self):
        from beyin_v3_sync import parse
        content = "---\ntags: a, b, c\n---\nbody"
        meta, _ = parse(content)
        self.assertEqual(meta.get('tags'), ['a', 'b', 'c'])

    def test_legacy_tags_yaml_list_converted_to_list(self):
        from beyin_v3_sync import parse
        content = "---\ntags: [a, b]\n---\nbody"
        meta, _ = parse(content)
        self.assertEqual(meta.get('tags'), ['a', 'b'])

    def test_date_conversion(self):
        from beyin_v3 import MemoryStore
        store = MemoryStore(self.state, self.root, read_only=False)
        self.source("t.md", "body")
        r1 = store._validate({"id": "1", "text": "t", "source": "t.md", "updated": "2026/09/20"})
        self.assertEqual(r1.get("updated_at"), "2026-09-20")

        r2 = store._validate({"id": "2", "text": "t", "source": "t.md", "updated": "20-09-2026"})
        self.assertEqual(r2.get("updated_at"), "2026-09-20")
        r5 = store._validate({"id": "5", "text": "t", "source": "t.md", "updated": "20.09.2026"})
        self.assertEqual(r5.get("updated_at"), "2026-09-20")
        r6 = store._validate({"id": "6", "text": "t", "source": "t.md", "updated": "20.09-2026"})
        self.assertNotIn("updated_at", r6)
        # Month-first text is not a day-first date; it stays unset instead of 2026-31-12.
        r3 = store._validate({"id": "3", "text": "t", "source": "t.md", "updated": "12/31/2026"})
        self.assertNotIn("updated_at", r3)
        r4 = store._validate({"id": "4", "text": "t", "source": "t.md", "updated": "2026/02/30", "modified": "2026-02-01"})
        self.assertEqual(r4.get("updated_at"), "2026-02-01")

    def test_comma_lists_only_for_tags_and_aliases(self):
        from beyin_v3_sync import parse
        meta, _ = parse("---\ntitle: Merhaba, dunya\nproject: Acme, Inc\naliases: Alfa, Beta\ntags: \"a, b\"\n---\nbody")
        self.assertEqual(meta['title'], 'Merhaba, dunya')
        self.assertEqual(meta['project'], 'Acme, Inc')
        self.assertEqual(meta['aliases'], ['Alfa', 'Beta'])
        self.assertEqual(meta['tags'], 'a, b')

    def test_migration_rollback_on_failure(self):
        self.source('.claude/scripts/.state/flush-state.json', '{"status":"ok"}')
        self.source('.claude/scripts/.state/compile-state.json', '{"status":"ok"}')

        import shutil
        original_copyfile = shutil.copyfile

        def failing_copyfile(src, dst):
            original_copyfile(src, dst)
            if 'compile-state.json' in str(src):
                raise RuntimeError("Simulated disk full")

        shutil.copyfile = failing_copyfile
        try:
            with self.assertRaisesRegex(RuntimeError, "Simulated disk full"):
                self.m.migrate_v2(self.root, self.state)

            preserved = self.state / 'v2-preserved-state' / '.claude/scripts/.state'
            self.assertFalse((preserved / 'flush-state.json').exists())
            self.assertFalse((preserved / 'compile-state.json').exists())
            self.assertFalse((self.state / 'v2-migration.json').exists())
        finally:
            shutil.copyfile = original_copyfile


if __name__ == '__main__':
    unittest.main()
