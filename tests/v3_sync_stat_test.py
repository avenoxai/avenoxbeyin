#!/usr/bin/env python3
"""Stat signature cache of warm syncs (#233): no lost edit, every conflict stays visible.

Every case is checked against LegacySyncEngine, the frozen engine that reads every source,
on its own state directory over the same vault. A cache hit must give the same sync report
and the same events, records and ownership rows as reading the bytes.
"""
from contextlib import contextmanager
import errno
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from v3_sync_test import load_module
from v3_sync_warm_test import LegacySyncEngine

subject = load_module()
SCRIPTS = Path(subject.__file__).resolve().parent
_real_time_ns = time.time_ns


def coarse_signature(st):
    """A filesystem whose stat cannot see an in-place rewrite: 2 s mtime ticks, no ctime or
    file id (FAT; on Windows st_ctime is the creation time)."""
    return (st.st_size, st.st_mtime_ns // 2_000_000_000 * 2_000_000_000, 0, '0')


def weak_signature(st):
    """The signature first proposed for the cache: size and mtime only."""
    return (st.st_size, st.st_mtime_ns, 0, '0')


@contextmanager
def clock(seconds):
    """Run a sync as if it happened `seconds` from now (no real sleeping)."""
    offset = int(seconds * 1_000_000_000)
    with patch.object(subject.time, 'time_ns', side_effect=lambda: _real_time_ns() + offset):
        yield


class StatSignatureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-sync-stat-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.oracle = LegacySyncEngine(self.vault, self.root / 'oracle-state')
        self.minutes = 0

    def write(self, name='notes/note.md', id='note', text='Alpha calibration.\n', **fields):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(subject.render(dict(id=id, kind='note', revision=1, **fields), text), encoding='utf-8', newline='')
        return path

    def snapshot(self, engine):
        with engine.store._connect() as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('events', 'records', 'markdown_sources')}

    def later(self):
        """Each settled sync happens a minute after the previous one, past the racy window."""
        self.minutes += 1
        return clock(60 * self.minutes)

    @contextmanager
    def reads(self):
        calls = []
        original = Path.read_bytes

        def counted(path):
            calls.append(Path(path).name)
            return original(path)

        with patch.object(Path, 'read_bytes', counted):
            yield calls

    def sync_both(self):
        """One sync of each engine over the same vault; reports and tables must be equal."""
        with self.later():
            result = self.engine.sync()
            expected = self.oracle.sync()
        self.assertEqual(result, expected)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        return result

    def settle(self):
        """Index the vault and make every signature trusted (read after the racy window)."""
        self.sync_both()
        self.sync_both()

    def record(self, id):
        with self.engine.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def rewrite_same_size(self, path, old, new):
        raw = path.read_bytes()
        self.assertEqual(len(old), len(new))
        path.write_bytes(raw.replace(old, new))

    def test_settled_unchanged_sources_are_not_read(self):
        for index in range(5):
            self.write(f'notes/note-{index}.md', id=f'note-{index}')
        self.settle()
        with self.reads() as calls:
            result = self.sync_both()
        self.assertEqual(result['status'], 'succeeded')
        # Only the oracle read the five notes (twice each: scan and validation).
        self.assertEqual(len(calls), 10)
        with self.reads() as calls, self.later():
            self.assertEqual(self.engine.sync()['indexed'], 5)
        self.assertEqual(calls, [])

    def test_a_same_size_edit_is_indexed(self):
        path = self.write()
        other = self.write('notes/other.md', id='other')
        self.settle()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        with self.reads() as calls, self.later():
            result = self.engine.sync()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
        self.assertEqual(self.record('note')['revision'], 2)
        self.assertIn('note.md', calls)
        self.assertNotIn(other.name, calls)
        self.assertEqual(result['status'], 'succeeded')
        with self.later():
            self.assertEqual(self.oracle.sync(), result)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))

    @unittest.skipIf(os.name == 'nt', 'Windows os.stat has no change time: st_ctime is the creation time')
    def test_an_edit_with_its_mtime_put_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_second_write_in_the_same_timestamp_tick_is_indexed(self):
        # Coarse timestamps: both writes carry one mtime. The first was read within the
        # racy window, so its signature is not trusted and the second write is read.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            with clock(0.5):
                self.engine.sync()
                self.oracle.sync()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with clock(1):
                result = self.engine.sync()
                self.assertEqual(result, self.oracle.sync())
            self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            # Read after the window, the signature is trusted from then on.
            with clock(10):
                self.engine.sync()
            with self.reads() as calls, clock(20):
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_a_replaced_file_is_indexed(self):
        # Atomic save: a new file with the same size and mtime takes the name.
        path = self.write()
        self.settle()
        before = path.stat()
        replacement = path.with_name('note.md.tmp')
        replacement.write_bytes(path.read_bytes().replace(b'Alpha', b'Omega'))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_clock_set_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        path.write_text(path.read_text(encoding='utf-8').replace('Alpha', 'Earlier clock'), encoding='utf-8')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns - 3600 * 1_000_000_000))
        with clock(-3600):
            result = self.engine.sync()
            self.assertEqual(result, self.oracle.sync())
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.assertEqual(self.record('note')['text'], 'Earlier clock calibration.\n')
        self.sync_both()

    def test_an_unreadable_placeholder_keeps_its_record_without_a_read(self):
        path = self.write()
        self.settle()
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted), self.later():
            result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='delete'").fetchone()[0], 0)

    def test_a_changed_unreadable_placeholder_matches_a_full_read(self):
        # Same as reading every source: the warning, and what happens to the record.
        path = self.write()
        self.settle()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted):
            result = self.sync_both()
        self.assertEqual(result['warnings'], [{'source': 'notes/note.md', 'reason': str(OSError(errno.EDEADLK, 'Resource deadlock avoided'))}])
        # Readable again: indexed again, as in a full read.
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_duplicate_ids_stay_quarantined(self):
        self.write()
        self.settle()
        self.write('notes/copy.md')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'note', 'reason': 'duplicate source id; all copies quarantined'}])
            self.assertIsNone(self.record('note'))
        (self.vault / 'notes/copy.md').unlink()
        self.assertEqual(self.sync_both()['status'], 'succeeded')
        self.assertIsNotNone(self.record('note'))

    def test_conflict_markers_stay_visible(self):
        path = self.write()
        self.settle()
        path.write_text(path.read_text(encoding='utf-8') + '<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n', encoding='utf-8')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['warnings'], [{'source': 'notes/note.md',
                                                   'reason': 'unresolved git conflict markers; resolve the merge before sync'}])

    def test_id_owned_by_another_writer_stays_a_conflict(self):
        payload = subject._json(dict(id='foreign', revision=7, source='notes/note.md', text='Other writer', kind='note'))
        for engine in (self.engine, self.oracle):
            with engine.store._connect() as db:
                db.execute('INSERT INTO records VALUES (?,?)', ('foreign', payload))
        self.write()
        self.write('notes/collision.md', id='foreign')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'foreign', 'reason': 'id already owned by another record'}])

    def test_a_manual_receipt_view_edit_stays_a_conflict(self):
        self.write()
        self.settle()
        self.engine.receipt('event-1', 'Calibrated alpha.', ['notes/note.md'], 'codex')
        views = [path for path in self.vault.rglob('*.md') if 'v3' in path.parts]
        self.assertTrue(views)
        view = views[0]
        view.write_text(view.read_text(encoding='utf-8') + '\nEdited by hand.\n', encoding='utf-8')
        relative = view.relative_to(self.vault).as_posix()
        for _ in range(3):
            with self.later():
                result = self.engine.sync()
            self.assertIn({'source': relative, 'reason': 'manual receipt view edit preserved'}, result['conflicts'])
            self.assertTrue(view.read_text(encoding='utf-8').endswith('Edited by hand.\n'))

    def test_outside_vault_symlink_is_rejected_on_every_sync(self):
        self.write()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'outside.md').write_text('Outside source.\n', encoding='utf-8', newline='')
        try:
            (self.vault / 'outside-link.md').symlink_to(outside / 'outside.md')
            (self.vault / 'broken-link.md').symlink_to(outside / 'missing.md')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        expected = [{'source': 'broken-link.md', 'reason': 'source outside vault'},
                    {'source': 'outside-link.md', 'reason': 'source outside vault'}]
        for _ in range(3):
            self.assertEqual(self.sync_both()['warnings'], expected)

    def test_an_index_written_by_a_version_without_signatures_is_not_trusted(self):
        # Rollback to a version without the table: it re-indexes an edit, the edit is then
        # reverted with its old mtime, and the update comes back. Stat cannot see the revert
        # here; the indexed payload is no longer the one the signature was recorded for.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            original = path.read_bytes()
            before = path.stat()
            rolled_back = LegacySyncEngine(self.vault, self.state)
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.later():
                rolled_back.sync()
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            path.write_bytes(original)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertIn('note.md', calls)
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_a_payload_rewritten_by_another_writer_is_rebuilt(self):
        # The store API can rewrite an indexed row without touching its source; a full read
        # rebuilds the row from the source, so a hit must not keep the rewritten payload.
        self.write()
        self.settle()
        for engine in (self.engine, self.oracle):
            engine.store.update_task('note', 1, {'status': 'rewritten elsewhere'})
        self.sync_both()
        self.assertNotIn('status', self.record('note'))

    def test_rollback_edits_are_indexed_after_update(self):
        path = self.write()
        self.settle()
        rolled_back = LegacySyncEngine(self.vault, self.state)
        path.write_text(path.read_text(encoding='utf-8').replace('Alpha', 'Rolled back'), encoding='utf-8')
        with self.later():
            rolled_back.sync()
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Rolled back calibration.\n')
        self.sync_both()

    def test_new_code_trusts_no_stored_signature(self):
        self.write()
        self.settle()
        with self.reads() as calls, self.later(), patch.object(subject, '_signature_epoch', return_value='other code'):
            self.engine.sync()
        self.assertIn('note.md', calls)
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='source_signature_epoch'").fetchone()[0], 'other code')

    def test_signature_includes_ctime_and_file_id(self):
        st = os.stat(self.write())
        self.assertEqual(subject._stat_signature(st), (st.st_size, st.st_mtime_ns, st.st_ctime_ns, str(st.st_ino)))


def _load(name):
    spec = importlib.util.spec_from_file_location('v3_sync_stat_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafeReplaceTest(unittest.TestCase):
    modules = ('beyin_v3_sync', 'beyin_v3_hook', 'beyin_v3_update')

    def test_posix_permission_error_is_raised_at_once(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Permission denied', str(destination))

                with patch.object(module.os, 'name', 'posix'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep') as sleep:
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_windows_sharing_violation_is_retried(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def busy(source, destination):
                    calls.append(destination)
                    if len(calls) < 3:
                        error = PermissionError(errno.EACCES, 'The process cannot access the file', str(destination))
                        error.winerror = 32
                        raise error

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', busy), \
                        patch.object(module.time, 'sleep') as sleep:
                    module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleep.call_count, 2)
                # Not a sharing violation (e.g. a read-only target emulated without winerror): no retry.
                calls.clear()

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Access is denied', str(destination))

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep'):
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
