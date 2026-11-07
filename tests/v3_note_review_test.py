"""PR #412 regressions: competing writers, committed writes, aliases and usage."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_notes as notes
import beyin_v3_sync as sync

SPEC = importlib.util.spec_from_file_location('note_review_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class NoteReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'vault'
        self.vault.mkdir()
        # /var vs /private/var and Windows 8.3 temp aliases are one location.
        self.vault = self.vault.resolve()
        self.state = (Path(self.temp.name) / 'state').resolve()
        self.engine = sync.SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)
        self.env = dict(os.environ, BEYIN_V3_NO_SPAWN='1')
        self.env.pop('CLAUDE_CODE_SESSION_ID', None)

    def write(self, source='Project/Threads.md', raw=b'alpha\nbeta\n'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def request(self, source='Project/Threads.md'):
        return {'source': source, 'expected_sha256': hashlib.sha256(b'alpha\nbeta\n').hexdigest(),
                'edits': [{'old': 'alpha', 'new': 'ALPHA'}]}

    def command(self, *args):
        return [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                '--state', str(self.state), *args]

    def test_note_create_respects_another_process_note_writer_lock(self):
        # Different state directories must still share the same vault-local lock.
        # A short child timeout makes this deterministic without scheduler sleeps.
        code = '''import json, sys
from beyin_v3_sync import SyncEngine
import beyin_v3_notes as notes
engine = SyncEngine(sys.argv[1], sys.argv[2])
original = notes.locked
notes.locked = lambda engine: original(engine, timeout=0.1)
try:
    result = engine.note_create('notes/new.md', 'competing writer')
except Exception as exc:
    result = {'error': type(exc).__name__}
print(json.dumps(result))
'''
        env = dict(self.env, PYTHONPATH=str(ROOT / 'template/.claude/scripts'))
        with notes.locked(self.engine):
            child = subprocess.run([sys.executable, '-c', code, str(self.vault),
                                    str(Path(self.temp.name) / 'other-state')],
                                   text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), {'error': 'NoteConflict'})
        self.assertFalse((self.vault / 'notes/new.md').exists())
        self.engine.note_edit({'source': 'notes/new.md', 'expected_sha256': None,
                               'content': 'note-edit writer'})
        with self.assertRaisesRegex(ValueError, 'source exists'):
            self.engine.note_create('notes/new.md', 'competing writer')
        self.assertEqual((self.vault / 'notes/new.md').read_bytes(), b'note-edit writer')

    def test_empty_lock_waits_without_writing_before_acquisition(self):
        # A byte-range lock may extend beyond EOF. Its owner need not write a
        # sentinel: Windows rejects another handle's write to the locked byte.
        folder = self.vault / notes.LOCAL_DIRECTORY
        folder.mkdir(exist_ok=True)
        lock = folder / 'writer.lock'
        code = '''import os, sys
with open(sys.argv[1], 'a+b') as handle:
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('locked-empty', flush=True)
    sys.stdin.read()
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked-empty')
            self.assertEqual(lock.stat().st_size, 0)
            with self.assertRaisesRegex(notes.NoteConflict, 'writer busy'):
                with notes.locked(self.engine, timeout=0.1):
                    self.fail('the empty lock was held by another process')
            self.assertEqual(lock.stat().st_size, 0)
        finally:
            output, errors = child.communicate('', timeout=10)
        self.assertEqual(child.returncode, 0, output + errors)
        # Closing the owner's descriptor releases the same persistent lock.
        with notes.locked(self.engine, timeout=0.1):
            self.assertEqual(lock.stat().st_size, 0)

    def pending_creation(self):
        path = self.vault / 'notes/new.md'
        original = sync._safe_replace
        def fail(src, dst):
            if Path(dst) == path:
                raise OSError('synthetic failure before source creation')
            return original(src, dst)
        with patch.object(sync, '_safe_replace', side_effect=fail):
            with self.assertRaises(OSError):
                self.engine.note_create('notes/new.md', 'pending creation')
        self.assertEqual(len(list(self.engine.journal.glob('*.json'))), 1)
        self.assertFalse(path.exists())
        return path

    def test_pending_note_recovery_serializes_with_real_note_edit_process(self):
        path = self.pending_creation()
        code = '''import json, sys
from beyin_v3_sync import SyncEngine
import beyin_v3_notes as notes
engine = SyncEngine(sys.argv[1], sys.argv[2])
original = notes.locked
notes.locked = lambda engine: original(engine, timeout=0.1)
try:
    result = engine.note_edit({'source':'notes/new.md', 'expected_sha256':None, 'content':'racing edit'})
except Exception as exc:
    result = {'error': type(exc).__name__}
print(json.dumps(result))
'''
        original = sync._safe_replace
        child_results = []
        def replace(src, dst):
            if Path(dst) == path:
                child = subprocess.run([sys.executable, '-c', code, str(self.vault),
                                        str(Path(self.temp.name) / 'other-state')],
                                       text=True, capture_output=True, timeout=20,
                                       env=dict(self.env, PYTHONPATH=str(ROOT / 'template/.claude/scripts')))
                self.assertEqual(child.returncode, 0, child.stderr)
                child_results.append(json.loads(child.stdout))
            return original(src, dst)
        with patch.object(sync, '_safe_replace', side_effect=replace):
            self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(child_results, [{'error': 'NoteConflict'}])
        self.assertIn(b'pending creation', path.read_bytes())
        snapshot = self.engine.note_read('notes/new.md')
        self.engine.note_edit({'source': snapshot['source'], 'expected_sha256': snapshot['sha256'],
                               'edits': [{'old': 'pending creation', 'new': 'deliberate update'}]})
        self.assertIn(b'deliberate update', path.read_bytes())

    def test_busy_note_recovery_defers_without_waiting_for_the_writer(self):
        path = self.pending_creation()
        with notes.locked(self.engine):
            # The child holds its DB transaction when it tries the note lock.
            # Waiting for the normal 15-second timeout would invert lock order.
            result = subprocess.run(self.command('sync'), text=True, capture_output=True,
                                    env=self.env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        # Deferring a live writer's journal is not a conflict: a hook sync running beside
        # note-create would otherwise drop SessionStart context and record hook-error.json.
        self.assertEqual(report['status'], 'succeeded')
        self.assertEqual(report['conflicts'], [])
        self.assertFalse(path.exists())
        self.assertEqual(len(list(self.engine.journal.glob('*.json'))), 1)
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertIn(b'pending creation', path.read_bytes())

    def test_recovered_note_stays_reserved_until_committed_journal_cleanup(self):
        path = self.pending_creation()
        entry = next(self.engine.journal.glob('*.json'))
        code = '''import json, sys
from beyin_v3_sync import SyncEngine
import beyin_v3_notes as notes
engine = SyncEngine(sys.argv[1], sys.argv[2])
original = notes.locked
notes.locked = lambda engine, timeout=0.1: original(engine, timeout=timeout)
try:
    # Reading takes the same reservation, so a busy writer may stop here already.
    snapshot = engine.note_read('notes/new.md')
    result = engine.note_edit({'source': snapshot['source'], 'expected_sha256': snapshot['sha256'],
                              'edits': [{'old': 'pending creation', 'new': 'deliberate update'}]})
except Exception as exc:
    result = {'error': type(exc).__name__, 'write_occurred': getattr(exc, 'write_occurred', False)}
print(json.dumps(result))
'''
        original_unlink = Path.unlink
        child_results = []
        def unlink(file, *args, **kwargs):
            if file == entry:
                # sync has committed, but has not removed the completed intent.
                # Let a real writer finish its entire attempt in this exact gap.
                self.assertTrue(entry.exists())
                with self.engine.store._connect() as db:
                    row = db.execute('SELECT r.payload FROM records r JOIN markdown_sources m ON r.id=m.id '
                                     'WHERE m.source=?', ('notes/new.md',)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(json.loads(row[0])['source_sha256'], sync._hash(path.read_bytes()))
                child = subprocess.run([sys.executable, '-c', code, str(self.vault), str(self.state)],
                                       text=True, capture_output=True, timeout=20,
                                       env=dict(self.env, PYTHONPATH=str(ROOT / 'template/.claude/scripts')))
                self.assertEqual(child.returncode, 0, child.stderr)
                child_results.append(json.loads(child.stdout))
            return original_unlink(file, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(child_results, [{'error': 'NoteConflict', 'write_occurred': False}])
        self.assertIn(b'pending creation', path.read_bytes())
        self.assertFalse(entry.exists())
        # Once cleanup releases the reservation, the same edit succeeds normally.
        snapshot = self.engine.note_read('notes/new.md')
        result = self.engine.note_edit({'source': snapshot['source'], 'expected_sha256': snapshot['sha256'],
                                        'edits': [{'old': 'pending creation', 'new': 'deliberate update'}]})
        self.assertEqual(result['status'], 'written')
        self.assertIn(b'deliberate update', path.read_bytes())

    def test_replayed_note_survives_sync_rollback_and_releases_reservation(self):
        path = self.pending_creation()
        entry = next(self.engine.journal.glob('*.json'))
        with patch.object(self.engine, '_scan_receipts', side_effect=OSError('synthetic scan failure')):
            with self.assertRaisesRegex(OSError, 'synthetic scan failure'):
                self.engine.sync()
        self.assertIn(b'pending creation', path.read_bytes())
        self.assertTrue(entry.exists())
        with self.engine.store._connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='last_sync_time'").fetchone())
        def other_writer():
            with notes.locked(self.engine, timeout=0):
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertTrue(pool.submit(other_writer).result(timeout=10))
        # The durable source already matches the intent; replay remains idempotent.
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertFalse(entry.exists())

    def test_nested_lock_is_reentrant_only_for_the_holding_thread(self):
        def other_thread():
            with self.assertRaises(notes.NoteConflict):
                with notes.locked(self.engine, timeout=0.1):
                    self.fail('a different thread reused the lock')
        with notes.locked(self.engine) as first:
            with notes.locked(self.engine, timeout=0) as second:
                self.assertEqual(first, second)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(other_thread).result(timeout=10)

    def test_sync_exception_after_replace_has_explicit_partial_write_json(self):
        path = self.write()
        args = self.command('note-edit', '--file', '-')[2:]
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(self.request()))), \
                patch.object(sync.SyncEngine, 'sync', side_effect=sqlite3.OperationalError('database locked')), \
                patch.object(cli, 'mark_session_edit') as mark:
            result, code = cli.main(args, return_result=True)
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')
        self.assertEqual(code, 2, result)
        self.assertEqual(result['status'], 'conflict')
        self.assertIs(result['write_occurred'], True)
        self.assertEqual(result['source'], 'Project/Threads.md')
        self.assertEqual(result['written_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue((self.vault / result['backup']).is_file())
        self.assertIn('reread', result['message'])
        mark.assert_not_called()

    def test_database_exception_after_replace_is_not_a_generic_error(self):
        path = self.write()
        indexing = []
        original_connect = self.engine.store._connect
        def refresh():
            indexing.append(True)
            return {'status': 'succeeded'}
        def connect():
            if indexing:
                raise sqlite3.OperationalError('disk full')
            return original_connect()
        with patch.object(self.engine, 'sync', side_effect=refresh), \
                patch.object(self.engine.store, '_connect', side_effect=connect):
            with self.assertRaises(notes.NoteConflict) as caught:
                self.engine.note_edit(self.request())
        self.assertIs(caught.exception.write_occurred, True)
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')

    def track_view(self, source):
        with self.engine.store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipt_views(path TEXT PRIMARY KEY, hash TEXT NOT NULL)')
            # Ownership persists even when the current bytes/header were edited.
            db.execute('INSERT OR REPLACE INTO receipt_views VALUES (?,?)', (source, '0' * 64))

    def test_tracked_view_without_header_is_rejected_using_canonical_source(self):
        source = 'daily/2026-10-19.md'
        path = self.write(source)
        self.track_view(source)
        with self.alias_lookup({'DAILY': 'daily'}), \
                patch.object(self.engine, 'sync') as refresh, \
                patch.object(sync, '_safe_replace', wraps=sync._safe_replace) as replace:
            for requested in (source, 'DAILY/2026-10-19.md'):
                with self.subTest(source=requested):
                    with self.assertRaisesRegex(ValueError, 'managed source'):
                        self.engine.note_read(requested)
                    with self.assertRaisesRegex(ValueError, 'managed source'):
                        self.engine.note_edit(self.request(requested))
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        self.assertFalse((self.vault / notes.LOCAL_DIRECTORY / 'backups').exists())
        refresh.assert_not_called()
        replace.assert_not_called()

    def test_flat_view_in_rollback_safe_table_is_protected_without_header(self):
        source = 'daily/2026-10-19.md'
        path = self.write(source)
        with self.engine.store._connect() as db:
            db.execute('CREATE TABLE receipt_flat_views(path TEXT PRIMARY KEY, hash TEXT NOT NULL)')
            db.execute('INSERT INTO receipt_flat_views VALUES (?,?)', (source, '0' * 64))
        with self.assertRaisesRegex(ValueError, 'managed source'):
            self.engine.note_read(source)
        with self.assertRaisesRegex(ValueError, 'managed source'):
            self.engine.note_edit(self.request(source))
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        self.assertFalse((self.vault / notes.LOCAL_DIRECTORY / 'backups').exists())

    def test_secret_filter_rejects_new_and_edited_secrets_before_backup(self):
        from beyin_v3_preferences import save
        save(self.vault, {'secret_filter': True})
        source = 'notes/secret-test.md'
        token = 'ghp_' + 'x' * 30  # synthetic provider-format fixture
        request = {'source': source, 'expected_sha256': None, 'content': token}
        with self.assertRaisesRegex(ValueError, 'secret_filter'):
            self.engine.note_edit(request)
        self.assertFalse((self.vault / source).exists())
        path = self.write(source)
        request = self.request(source)
        request['edits'][0]['new'] = token
        with self.assertRaisesRegex(ValueError, 'secret_filter'):
            self.engine.note_edit(request)
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        self.assertFalse((self.vault / notes.LOCAL_DIRECTORY / 'backups').exists())
        # Removing a pre-existing secret is allowed; byte preservation still applies.
        path.write_bytes((token + '\r\n').encode())
        request = {'source': source, 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'edits': [{'old': token, 'new': '[removed]'}]}
        self.assertEqual(self.engine.note_edit(request)['status'], 'written')
        self.assertEqual(path.read_bytes(), b'[removed]\r\n')
        save(self.vault, {'secret_filter': False})
        self.assertEqual(self.engine.note_edit({'source': 'notes/opt-out.md',
                         'expected_sha256': None, 'content': token})['status'], 'written')

    def test_human_daily_without_current_view_tracking_remains_editable(self):
        source = 'daily/2026-10-19.md'
        for tracking in ('absent', 'retired'):
            with self.subTest(tracking=tracking):
                path = self.write(source)
                if tracking == 'retired':
                    self.track_view(source)
                    with self.engine.store._connect() as db:
                        db.execute('DELETE FROM receipt_views WHERE path=?', (source,))
                self.assertEqual(self.engine.note_read(source)['content'], 'alpha\nbeta\n')
                self.assertEqual(self.engine.note_edit(self.request(source))['status'], 'written')
                self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')

    def test_view_claim_while_preparing_write_is_rechecked_before_replace(self):
        source = 'daily/2026-10-19.md'
        path = self.write(source)
        original = notes.tempfile.mkstemp
        def prepare(*args, **kwargs):
            self.track_view(source)
            return original(*args, **kwargs)
        with patch.object(notes.tempfile, 'mkstemp', side_effect=prepare), \
                patch.object(self.engine, 'sync') as refresh, \
                patch.object(sync, '_safe_replace', wraps=sync._safe_replace) as replace:
            with self.assertRaisesRegex(ValueError, 'managed source'):
                self.engine.note_edit(self.request(source))
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        self.assertEqual(list(path.parent.glob('.beyin-note-*')), [])
        refresh.assert_not_called()
        replace.assert_not_called()

    def test_view_tracking_database_error_fails_before_writing(self):
        path = self.write()
        with patch.object(self.engine.store, '_connect', side_effect=sqlite3.OperationalError('disk full')), \
                patch.object(self.engine, 'sync') as refresh, \
                patch.object(sync, '_safe_replace', wraps=sync._safe_replace) as replace:
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.note_edit(self.request())
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        refresh.assert_not_called()
        replace.assert_not_called()

    def test_path_and_cleanup_exceptions_after_replace_report_write(self):
        for stage in ('source-path', 'temporary-cleanup', 'diagnostics'):
            with self.subTest(stage=stage):
                path = self.write()
                replaced = []
                original_replace, original_path = sync._safe_replace, notes.source_path
                original_exists = notes.os.path.exists
                def replace(src, dst):
                    original_replace(src, dst)
                    if Path(dst) == path:
                        replaced.append(True)
                def source_path(*args):
                    if replaced and stage == 'source-path':
                        raise ValueError('path changed after replace')
                    return original_path(*args)
                def exists(value):
                    if replaced and stage == 'temporary-cleanup' and '.beyin-note-' in str(value):
                        raise OSError('temporary directory unreadable')
                    return original_exists(value)
                with patch.object(sync, '_safe_replace', side_effect=replace), \
                        patch.object(notes, 'source_path', side_effect=source_path), \
                        patch.object(notes.os.path, 'exists', side_effect=exists), \
                        patch.object(self.engine, '_write_diagnostics', side_effect=OSError('diagnostics failed')):
                    with self.assertRaises(notes.NoteConflict) as caught:
                        self.engine.note_edit(self.request())
                self.assertIs(caught.exception.write_occurred, True)
                self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')

    @unittest.skipIf(os.name == 'nt', 'POSIX directory fsync')
    def test_parent_fsync_happens_between_replace_and_indexing(self):
        path = self.write()
        events = []
        original_fsync, original_replace, original_sync = os.fsync, sync._safe_replace, self.engine.sync
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append('directory')
            return original_fsync(fd)
        def replace(src, dst):
            original_replace(src, dst)
            if Path(dst) == path:
                events.append('replace')
        def refresh():
            events.append('sync')
            return original_sync()
        with patch.object(os, 'fsync', side_effect=fsync), \
                patch.object(sync, '_safe_replace', side_effect=replace), \
                patch.object(self.engine, 'sync', side_effect=refresh):
            self.engine.note_edit(self.request())
        self.assertIn('directory', events)
        self.assertLess(events.index('replace'), events.index('directory'))
        self.assertLess(events.index('directory'), events.index('sync'))

    @unittest.skipIf(os.name == 'nt', 'POSIX directory fsync')
    def test_parent_fsync_failure_reports_written_but_unverified(self):
        path = self.write()
        original = os.fsync
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory durability failure')
            return original(fd)
        with patch.object(os, 'fsync', side_effect=fsync), \
                patch.object(self.engine, 'sync') as refresh:
            with self.assertRaises(notes.NoteConflict) as caught:
                self.engine.note_edit(self.request())
        self.assertIs(caught.exception.write_occurred, True)
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')
        refresh.assert_not_called()

    def alias_lookup(self, aliases):
        # Emulate only the filesystem's lstat alias lookup, not source_path or SQL.
        # This runs the canonicalization/security checks on Linux as well as Windows.
        original = Path.lstat
        vault = self.engine.root
        def lstat(path, *args, **kwargs):
            try:
                parts = path.relative_to(vault).parts
            except ValueError:
                return original(path, *args, **kwargs)
            actual = vault.joinpath(*(aliases.get(part, part) for part in parts))
            return original(actual, *args, **kwargs)
        return patch.object(Path, 'lstat', lstat)

    def test_filesystem_alias_uses_stored_source_for_read_write_and_index(self):
        path = self.write()
        with self.alias_lookup({'project': 'Project', 'threads.md': 'Threads.md'}):
            read = self.engine.note_read('project/threads.md')
            self.assertEqual(read['source'], 'Project/Threads.md')
            self.assertEqual(read['content'], 'alpha\nbeta\n')
            result = self.engine.note_edit(self.request('project/threads.md'))
        self.assertEqual(result['status'], 'written')
        self.assertEqual(result['source'], 'Project/Threads.md')
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')
        backup = json.loads((self.vault / result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(backup['source'], 'Project/Threads.md')

    def test_new_note_canonicalizes_existing_ancestor(self):
        (self.vault / 'Project').mkdir()
        with self.alias_lookup({'project': 'Project'}):
            result = self.engine.note_edit({'source': 'project/new.md', 'expected_sha256': None,
                                            'content': 'new note'})
        self.assertEqual(result['status'], 'written')
        self.assertEqual(result['source'], 'Project/new.md')
        self.assertEqual((self.vault / 'Project/new.md').read_bytes(), b'new note')

    def test_short_name_alias_cannot_bypass_hidden_or_managed_paths(self):
        for source, aliases in (
                ('HIDDEN~1/note.md', {'HIDDEN~1': '.hidden'}),
                ('CREDEN~1/note.md', {'CREDEN~1': 'credentials'}),
                ('KNOWLE~1/v3/outcomes.md', {'KNOWLE~1': 'knowledge'}),
                ('SETUP~1.MD', {'SETUP~1.MD': 'setup-client.md'})):
            actual = '/'.join(aliases.get(part, part) for part in source.split('/'))
            path = self.write(actual)
            with self.subTest(source=source), self.alias_lookup(aliases):
                with self.assertRaises(ValueError):
                    self.engine.note_read(source)
                with self.assertRaises(ValueError):
                    self.engine.note_edit(self.request(source))
            self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')

    def test_case_sensitive_distinct_sources_are_not_conflated(self):
        first = self.write()
        alias = self.vault / 'project/threads.md'
        if alias.exists():
            self.skipTest('case-insensitive filesystem; live alias regression runs separately')
        second = self.write('project/threads.md')
        result = self.engine.note_edit(self.request('project/threads.md'))
        self.assertEqual(result['source'], 'project/threads.md')
        self.assertEqual(first.read_bytes(), b'alpha\nbeta\n')
        self.assertEqual(second.read_bytes(), b'ALPHA\nbeta\n')

    def test_case_insensitive_filesystem_live_readback(self):
        path = self.write()
        alias = self.vault / 'project/threads.md'
        if not alias.exists():
            self.skipTest('case-sensitive filesystem; alias lookup regression runs separately')
        self.assertEqual(self.engine.note_read('project/threads.md')['source'], 'Project/Threads.md')
        result = self.engine.note_edit(self.request('project/threads.md'))
        self.assertEqual(result['status'], 'written')
        self.assertEqual(result['source'], 'Project/Threads.md')
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')

    def test_note_usage_errors_are_json_exit_64(self):
        for args in (('note-read',), ('note-read', 'Project/Threads.md', '--unknown'),
                     ('note-edit', '--file'), ('note-edit', 'unexpected')):
            with self.subTest(args=args):
                result = subprocess.run(self.command(*args), text=True, capture_output=True,
                                        env=self.env, timeout=20)
                self.assertEqual(result.returncode, 64, result.stderr + result.stdout)
                self.assertEqual(json.loads(result.stdout)['status'], 'usage_error')
                self.assertEqual(result.stderr, '')

    def test_note_usage_missing_global_argument_and_return_result(self):
        result, code = cli.main(['--state', str(self.state), 'note-read', 'Threads.md'], return_result=True)
        self.assertEqual(code, 64)
        self.assertEqual(result['status'], 'usage_error')
        self.assertIn('--vault', result['message'])

    def test_installed_note_usage_keeps_json_in_human_mode(self):
        installed = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'),
                                    '--vault', str(self.vault), '--state', str(self.state)],
                                   text=True, capture_output=True, env=self.env, timeout=40)
        self.assertEqual(installed.returncode, 0, installed.stderr + installed.stdout)
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'note-read', '--human'],
                                text=True, capture_output=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 64, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['status'], 'usage_error')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
