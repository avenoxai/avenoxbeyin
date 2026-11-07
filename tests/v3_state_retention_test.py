#!/usr/bin/env python3
"""#585: bounded event retention, stale temp files and a safe VACUUM policy for local state."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3 as runtime  # noqa: E402
import beyin_v3_hook as hook  # noqa: E402
import beyin_v3_retention as retention  # noqa: E402

DAY = 86400
T0 = 1_900_000_000.0
HEX32 = 'a' * 32
HEX64 = 'b' * 64


def load_entry():
    spec = importlib.util.spec_from_file_location('retention_entry_cli', ROOT / 'scripts/beyin_entry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v3-retention-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / 'Synthetic Arşiv Kasası'
        self.vault.mkdir()
        self.state = self.root / 'state outside vault'
        self.store = runtime.MemoryStore(self.state, self.vault)

    def db(self):
        return closing(sqlite3.connect(self.store.database))

    def note(self, name, revisions=1):
        """One record with `revisions` events: an ingest, then task updates."""
        (self.vault / (name + '.md')).write_text('Müşteri notu ' + name + '\n', encoding='utf-8')
        self.store.ingest({'id': name, 'text': 'Şifre değil, örnek ' + name, 'source': name + '.md'})
        for revision in range(1, revisions):
            self.store.update_task(name, revision, {'status': 'adım-' + str(revision)})

    def delete(self, name):
        """What sync writes when a source disappears: a final delete snapshot."""
        with self.db() as db, db:
            payload = db.execute('SELECT payload FROM records WHERE id=?', (name,)).fetchone()[0]
            db.execute("INSERT INTO events(event_type,record_id,revision,record) VALUES ('delete',?,?,?)",
                       (name, json.loads(payload)['revision'], payload))
            db.execute('DELETE FROM records WHERE id=?', (name,))
        (self.vault / (name + '.md')).unlink()

    def sequences(self, record_id=None):
        with self.db() as db:
            query, values = 'SELECT sequence FROM events', ()
            if record_id is not None:
                query, values = query + ' WHERE record_id=?', (record_id,)
            return [row[0] for row in db.execute(query + ' ORDER BY sequence', values)]

    def metadata(self, key):
        with self.db() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def test_per_record_cap_keeps_newest_snapshots_and_history(self):
        self.note('hot', revisions=15)
        self.note('calm', revisions=3)
        before = self.sequences('hot')
        result = retention.maintain(self.store, now=T0)
        self.assertEqual(result['events_removed'], 5)
        self.assertEqual(self.sequences('hot'), before[-retention.EVENTS_PER_RECORD:])
        self.assertEqual(len(self.sequences('calm')), 3)
        history = self.store.history('hot')
        self.assertEqual([entry['revision'] for entry in history], list(range(6, 16)))
        self.assertEqual(history[-1]['record']['status'], 'adım-14')

    def test_age_window_from_sequence_marks_keeps_current_records_and_recent_trails(self):
        self.note('kept', revisions=4)
        self.note('gone')
        self.delete('gone')
        latest_kept = self.sequences('kept')[-1]
        self.assertEqual(retention.maintain(self.store, now=T0)['events_removed'], 0)
        self.assertEqual(json.loads(self.metadata(retention.MARKS_KEY)), [[int(T0), self.sequences()[-1]]])
        self.note('fresh', revisions=2)
        # Day 29: nothing is old enough yet; the deleted record keeps its whole trail.
        self.assertEqual(retention.maintain(self.store, now=T0 + 29 * DAY)['events_removed'], 0)
        self.assertEqual(self.store.history('gone')[-1]['event_type'], 'delete')
        # Day 31: everything at or below the first mark is old, except each current record's newest.
        result = retention.maintain(self.store, now=T0 + 31 * DAY)
        self.assertEqual(result['events_removed'], 5)
        self.assertEqual(self.sequences('kept'), [latest_kept])
        self.assertEqual(self.sequences('gone'), [])
        self.assertEqual(len(self.sequences('fresh')), 2)
        self.assertEqual([entry['revision'] for entry in self.store.history('kept')], [4])
        self.assertEqual(self.store.history('gone'), [])
        self.assertEqual(len(self.store.history('fresh')), 2)
        # Only the marks that can still decide an age remain.
        marks = json.loads(self.metadata(retention.MARKS_KEY))
        self.assertEqual([mark[0] for mark in marks], [int(T0), int(T0 + 29 * DAY), int(T0 + 31 * DAY)])

    def test_newest_row_and_sequence_numbers_survive_pruning_and_vacuum(self):
        self.note('a')
        self.note('b')
        self.delete('b')
        top = self.sequences()[-1]
        retention.maintain(self.store, now=T0)
        retention.compact(self.store, now=T0 + 40 * DAY)
        # The deleted record's final event is the table's newest row: MAX(sequence) stays.
        self.assertEqual(self.sequences()[-1], top)
        self.store.update_task('a', 1, {'status': 'açık'})
        self.assertGreater(self.sequences('a')[-1], top)

    def test_batches_continue_backlog_then_throttle_daily(self):
        self.note('hot', revisions=15)
        counts = [retention.maintain(self.store, now=T0, batch=2)['events_removed'] for _ in range(3)]
        self.assertEqual(counts, [2, 2, 1])
        self.assertEqual(retention.maintain(self.store, now=T0 + 60)['status'], 'throttled')
        self.assertEqual(retention.maintain(self.store, now=T0 + DAY + 1)['status'], 'done')
        # A check stamped by a clock that ran ahead does not suppress maintenance.
        with self.db() as db, db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (retention.CHECK_KEY, repr(T0 + 400 * DAY)))
        self.assertEqual(retention.maintain(self.store, now=T0 + 2 * DAY)['status'], 'done')

    def test_future_and_malformed_marks_never_age_events(self):
        self.note('hot', revisions=3)
        with self.db() as db, db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                       (retention.MARKS_KEY, json.dumps([[int(T0 + 500 * DAY), 99], 'bad', [-1, 3]])))
        self.assertEqual(retention.maintain(self.store, now=T0)['events_removed'], 0)
        self.assertEqual(json.loads(self.metadata(retention.MARKS_KEY)), [[int(T0), self.sequences()[-1]]])
        with self.db() as db, db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (retention.MARKS_KEY, '{not json'))
        self.assertEqual(retention.maintain(self.store, now=T0 + 2 * DAY)['events_removed'], 0)

    def test_busy_index_skips_without_error(self):
        self.note('hot', revisions=15)
        busy = runtime.MemoryStore(self.state, self.vault, busy_timeout=0.05)
        holder = sqlite3.connect(self.store.database, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('BEGIN IMMEDIATE')
        self.assertEqual(retention.maintain(busy, now=T0)['status'], 'busy')
        holder.execute('ROLLBACK')
        self.assertEqual(len(self.sequences('hot')), 15)

    def stale(self, path, age=2 * DAY):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_temp_sweep_removes_only_our_old_temp_names(self):
        removed = [self.stale(self.state / ('hook-health.json.' + HEX32 + '.tmp')),
                   self.stale(self.state / ('hook-error.json.' + HEX32 + '.tmp')),
                   self.stale(self.state / '.index-ab_9xy12'),
                   self.stale(self.state / '.passages-abcdefgh.tmp'),
                   self.stale(self.state / 'hook-queue' / (HEX64 + '.json.' + HEX32 + '.tmp')),
                   self.stale(self.state / 'receipt-reminders' / (HEX64 + '.edited.' + HEX32 + '.tmp'))]
        kept = [self.stale(self.state / ('hook-health.json.' + 'c' * 32 + '.tmp'), age=60),
                self.stale(self.state / 'notlar.tmp'),
                self.stale(self.state / '.index-short'),
                self.stale(self.state / ('hook-health.json.' + 'F' * 32 + '.tmp')),
                self.stale(self.state / ('other.json.' + HEX32 + '.tmp')),
                self.stale(self.state / 'hook-queue' / (HEX64 + '.json')),
                self.stale(self.state / 'hook-done' / (HEX64 + '.json.' + HEX32 + '.tmp')),
                self.stale(self.vault / ('hook-health.json.' + HEX32 + '.tmp'))]
        (self.state / ('hook-error.json.' + 'd' * 32 + '.tmp')).mkdir()
        if os.name != 'nt':
            os.symlink(self.vault / 'outside', self.state / ('hook-error.json.' + 'e' * 32 + '.tmp'))
        self.assertEqual(retention.stale_temp_files(self.state), sorted(removed))
        self.assertEqual(retention.sweep_temp_files(self.state), len(removed))
        self.assertFalse(any(path.exists() for path in removed))
        self.assertTrue(all(path.exists() for path in kept))

    def test_hook_atomic_removes_its_temp_when_the_write_fails(self):
        target = self.state / 'hook-health.json'
        with patch.object(hook, '_safe_replace', side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(PermissionError):
                hook.atomic(target, {'at': 1})
        with self.assertRaises(TypeError):
            hook.atomic(target, {'at': object()})
        self.assertEqual(sorted(path.name for path in self.state.glob('*.tmp')), [])
        self.assertFalse(target.exists())
        hook.atomic(target, {'at': 2})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'at': 2})

    def auto_vacuum(self):
        with self.db() as db:
            return db.execute('PRAGMA auto_vacuum').fetchone()[0]

    def test_new_index_uses_incremental_auto_vacuum_and_worker_frees_pages(self):
        self.assertEqual(self.auto_vacuum(), 2)
        (self.vault / 'big.md').write_text('büyük\n', encoding='utf-8')
        self.store.ingest({'id': 'big', 'text': 'x' * 200_000, 'source': 'big.md'})
        for revision in range(1, 13):
            self.store.update_task('big', revision, {'status': 'açık-' + str(revision)})
        size = self.store.database.stat().st_size
        with patch.object(retention, 'INCREMENTAL_VACUUM_PAGES', 40), self.db() as db:
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            result = retention.maintain(self.store, now=T0)
            free = db.execute('PRAGMA freelist_count').fetchone()[0]
        self.assertEqual((result['events_removed'], result['pages_freed']), (3, 40))
        # Every counted page really left the file (3.11 freed one page per PRAGMA statement).
        self.assertEqual(self.store.database.stat().st_size, size - 40 * page_size)
        self.assertGreater(free, 0)
        self.assertEqual(retention.maintain(self.store, now=T0 + DAY + 1)['pages_freed'], min(free, 1024))
        with self.db() as db:
            self.assertEqual(db.execute('PRAGMA freelist_count').fetchone()[0], max(0, free - 1024))

    def legacy_store(self):
        """An index created before #585: auto_vacuum none, as 3.12.0 leaves it."""
        state = self.root / 'legacy state'
        state.mkdir()
        with closing(sqlite3.connect(state / 'memory.sqlite3')) as db:
            db.execute('CREATE TABLE records(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        store = runtime.MemoryStore(state, self.vault)
        with closing(sqlite3.connect(store.database)) as db:
            self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 0)
        return store

    def test_compact_dry_run_writes_nothing_then_vacuum_switches_legacy_index(self):
        self.store = self.legacy_store()
        (self.vault / 'big.md').write_text('büyük\n', encoding='utf-8')
        self.store.ingest({'id': 'big', 'text': 'x' * 100_000, 'source': 'big.md'})
        for revision in range(1, 15):
            self.store.update_task('big', revision, {'status': 'açık-' + str(revision)})
        temp = self.stale(self.store.state_dir / ('hook-health.json.' + HEX32 + '.tmp'))
        raw = self.store.database.read_bytes()
        plan = retention.compact(self.store, dry_run=True, now=T0)
        self.assertEqual((plan['status'], plan['events_prunable'], plan['stale_temp_files']), ('planned', 5, 1))
        self.assertGreater(plan['event_bytes_prunable'], 5 * 100_000)
        self.assertEqual(self.store.database.read_bytes(), raw)
        self.assertTrue(temp.exists())
        result = retention.compact(self.store, now=T0)
        self.assertEqual((result['status'], result['vacuum'], result['events_removed'], result['temp_files_removed']),
                         ('compacted', 'done', 5, 1))
        self.assertLess(result['database_bytes_after'], result['database_bytes_before'])
        self.assertEqual((result['auto_vacuum'], result['free_bytes']), ('incremental', 0))
        self.assertFalse(temp.exists())
        # The release before this one validates exactly these columns and the integrity check.
        with closing(sqlite3.connect(self.store.database)) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
        self.assertEqual(columns, {'sequence', 'event_type', 'record_id', 'revision', 'record'})
        self.assertEqual(len(self.store.history('big')), retention.EVENTS_PER_RECORD)

    def test_compact_reports_a_busy_index_and_keeps_it(self):
        self.note('hot', revisions=12)
        store = runtime.MemoryStore(self.state, self.vault, busy_timeout=0.05)
        reader = sqlite3.connect(self.store.database, isolation_level=None)
        self.addCleanup(reader.close)

        def read_during_vacuum(*args):
            # A hook reading the index while VACUUM wants its exclusive lock.
            reader.execute('BEGIN')
            reader.execute('SELECT COUNT(*) FROM events').fetchone()
            return 0
        with patch.object(retention, 'sweep_temp_files', side_effect=read_during_vacuum):
            result = retention.compact(store, now=T0)
        self.assertEqual((result['status'], result['vacuum'], result['events_removed']), ('busy', 'busy', 2))
        reader.execute('COMMIT')
        self.assertEqual(len(self.sequences('hot')), 10)
        with self.db() as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        # The pruning step itself can also meet a busy writer; nothing is half done.
        holder = sqlite3.connect(self.store.database, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('BEGIN IMMEDIATE')
        result = retention.compact(store, now=T0)
        holder.execute('ROLLBACK')
        self.assertEqual((result['status'], result['vacuum'], result['events_removed']), ('busy', 'skipped', 0))
        self.assertEqual(len(self.sequences('hot')), 10)

    def test_storage_report_reads_only(self):
        self.note('hot', revisions=12)
        retention.maintain(self.store, now=T0)
        self.stale(self.state / ('hook-error.json.' + HEX32 + '.tmp'))
        before = (self.store.database.read_bytes(), self.store.database.stat().st_mtime_ns)
        with patch.object(retention, 'COMPACT_HINT_BYTES', 1):
            report = retention.storage_report(self.state)
        self.assertEqual((before[0], before[1]), (self.store.database.read_bytes(), self.store.database.stat().st_mtime_ns))
        self.assertEqual((report['status'], report['events'], report['stale_temp_files'], report['auto_vacuum']),
                         ('ok', 10, 1, 'incremental'))
        self.assertIn('compact_suggested', report)
        self.assertEqual(retention.storage_report(self.root / 'missing')['status'], 'absent')
        self.assertFalse((self.root / 'missing').exists())

    def test_entry_lines_for_state_compact_and_doctor_hint(self):
        entry = load_entry()
        text = entry.human_result({'status': 'compacted', 'vacuum': 'done', 'events_removed': 3, 'temp_files_removed': 1,
                                   'database_bytes_before': 219 * 1048576, 'database_bytes_after': 34 * 1048576},
                                  'state-compact')
        self.assertIn('219.0 MB -> 34.0 MB', text)
        self.assertIn('Vault dosyalarina dokunulmadi.', text)
        plan = entry.human_result({'status': 'planned', 'events_prunable': 7, 'event_bytes_prunable': 1048576,
                                   'stale_temp_files': 2}, 'state-compact')
        self.assertIn('Hicbir sey degismedi', plan)
        doctor = entry.human_result({'status': 'observed_metadata', 'state_storage': {
            'compact_suggested': True, 'database_bytes': 219 * 1048576, 'free_bytes': 185 * 1048576}}, 'doctor', '3.12.0')
        self.assertIn('beyin.py state-compact', doctor)


class WorkerMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v3-retention-worker-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        home = self.root / 'home'
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), BEYIN_UPDATES_OFF='1',
                                 PYTHONDONTWRITEBYTECODE='1')

    def check_stamp(self):
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (retention.CHECK_KEY,)).fetchone()
        return row

    def test_drain_runs_maintenance_only_when_asked(self):
        with patch.dict(os.environ, {'BEYIN_V3_INTERNAL': '1'}):
            hook.enqueue_event(self.vault, self.state, {'hook_event_name': 'SessionStart', 'session_id': 's',
                                                        'event_id': 'retention-1'}, 'claude')
            self.assertEqual(hook.drain_queue(self.vault, self.state)['processed'], 1)
            self.assertIsNone(self.check_stamp())
            hook.enqueue_event(self.vault, self.state, {'hook_event_name': 'Stop', 'session_id': 's',
                                                        'event_id': 'retention-2'}, 'claude')
            self.assertEqual(hook.drain_queue(self.vault, self.state, maintenance=True)['processed'], 1)
        self.assertIsNotNone(self.check_stamp())

    def test_waited_hooks_never_ask_their_worker_for_maintenance(self):
        log = self.root / 'spawned.jsonl'
        wrapper = self.root / 'recording_hook.py'
        wrapper.write_text(f'''import json, runpy, subprocess, sys
sys.path.insert(0, {str(SCRIPTS)!r})
class Recorder:
    returncode = 0
    def __init__(self, command, **kwargs):
        with open({str(log)!r}, 'a', encoding='utf-8') as out:
            out.write(json.dumps(command) + '\\n')
    def wait(self, timeout=None):
        return 0
subprocess.Popen = Recorder
sys.argv[0] = {str(HOOK)!r}
runpy.run_path({str(HOOK)!r}, run_name='__main__')
''', encoding='utf-8')
        events = ('SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd')
        for event in events:
            payload = {'hook_event_name': event, 'session_id': 'retention-session', 'event_id': 'worker-' + event,
                       'cwd': str(self.vault), 'prompt': 'Arşiv notu nerede?'}
            result = subprocess.run([sys.executable, str(wrapper), '--vault', str(self.vault), '--state', str(self.state),
                                     '--harness', 'claude'], input=json.dumps(payload), text=True, encoding='utf-8',
                                    capture_output=True, env=self.env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        workers = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        workers = [command for command in workers if '--harness' in command]
        self.assertEqual(['--maintenance' in command for command in workers], [False, False, True, True])


if __name__ == '__main__':
    unittest.main()
