#!/usr/bin/env python3
"""Warm-sync operation counts and byte equivalence against the pre-optimization loops."""
from contextlib import contextmanager
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from v3_sync_test import load_module

subject = load_module()
_json = subject._json
resolve_supersedes = subject.resolve_supersedes
project_receipts = subject.project_receipts
_hash = subject._hash
parse = subject.parse
_has_conflict_markers = subject._has_conflict_markers
EXCLUDED_DIRS = subject.EXCLUDED_DIRS
EXCLUDED_FILES = subject.EXCLUDED_FILES


class LegacySyncEngine(subject.SyncEngine):
    # Frozen source from the original engine: deliberately keep per-note SQL and
    # independent path validation here so this oracle cannot follow the new loops.
    def _path(self, relative, existing=False):
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('relative source required')
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('source outside vault')
        cursor = path
        while cursor != self.root:
            if cursor.is_symlink():
                raise ValueError('symlink source rejected')
            cursor = cursor.parent
        if existing and not path.is_file():
            raise ValueError('source missing')
        return path

    def _scan(self):
        records, warnings, conflicts = {}, [], []
        duplicate = set()
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.casefold() not in EXCLUDED_DIRS and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                if name.startswith('.') or not name.lower().endswith('.md') or name.casefold() in EXCLUDED_FILES or name.casefold().startswith('setup-'):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                try:
                    self._path(relative, existing=True)
                    raw = path.read_bytes()
                    text = raw.decode('utf-8')
                    if _has_conflict_markers(text):
                        raise ValueError('unresolved git conflict markers; resolve the merge before sync')
                    metadata, body = parse(text)
                    if metadata.get('kind') == 'task' and body.lstrip().startswith('---'):
                        raise ValueError('task has embedded frontmatter; reconcile metadata and body explicitly')
                    if metadata.get('kind') == 'receipt' or metadata.get('generated') is True:
                        continue
                    record = dict(metadata, source=relative, text=body)
                    record.setdefault('id', 'md-' + _hash(relative)[:24])
                    record.setdefault('kind', 'note')
                    record = self.store._validate(record)
                    if record['source_sha256'] != _hash(raw):
                        raise ValueError('source changed while scanning')
                    if record['id'] in records:
                        duplicate.add(record['id'])
                    records[record['id']] = record
                except (ValueError, OSError, UnicodeError) as exc:
                    warnings.append({'source': relative, 'reason': str(exc)})
        for id in sorted(duplicate):
            records.pop(id, None)
            conflicts.append({'id': id, 'reason': 'duplicate source id; all copies quarantined'})
        return records, warnings, conflicts

    def sync(self):
        # Serialize recovery, source scan and projection across local processes.
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            completed, recovery_conflicts = self._recover(db)
            receipt_warnings, receipt_conflicts = self._scan_receipts(db)
            records, warnings, conflicts = self._scan()
            warnings.extend(receipt_warnings)
            conflicts.extend(recovery_conflicts + receipt_conflicts)
            old_owned = {row[0] for row in db.execute('SELECT id FROM markdown_sources')}
            deleted = 0
            for id in old_owned - records.keys():
                row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
                if row:
                    old = json.loads(row[0])
                    db.execute("INSERT INTO events(event_type,record_id,revision,record) VALUES ('delete',?,?,?)", (id, old['revision'], row[0]))
                    db.execute('DELETE FROM records WHERE id=?', (id,))
                    deleted += 1
                db.execute('DELETE FROM markdown_sources WHERE id=?', (id,))
            for id, record in records.items():
                row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
                if row and id in old_owned:
                    previous = json.loads(row[0])
                    if previous['source_sha256'] != record['source_sha256']:
                        record['revision'] = max(record['revision'], previous['revision'] + 1)
                    else:
                        record['revision'] = max(record['revision'], previous['revision'])
                payload = _json(record)
                if row and id not in old_owned and row[0] != payload:
                    conflicts.append({'id': id, 'reason': 'id already owned by another record'})
                    continue
                if not row or row[0] != payload:
                    event_type = 'update' if row else 'ingest'
                    db.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (id, payload))
                    db.execute('INSERT INTO events(event_type,record_id,revision,record) VALUES (?,?,?,?)', (event_type, id, record['revision'], payload))
                db.execute('INSERT OR REPLACE INTO markdown_sources VALUES (?,?)', (id, record['source']))
            conflicts.extend(project_receipts(self, db, warnings))
            # A supersedes value that retires nothing is reported, not a degraded scan: no
            # source was excluded and the hook must not warn on every turn about it.
            dead_supersedes = resolve_supersedes([json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY id')])[1]
            if conflicts:
                # A receipt hidden by a directory mtime that did not move surfaces as a view
                # conflict; the next sync then rescans receipts/ in full.
                db.execute("DELETE FROM metadata WHERE key='receipt_scan_signature'")
        for entry in completed:
            entry.unlink(missing_ok=True)
        result = {'status': 'conflict' if conflicts else 'degraded' if warnings else 'succeeded', 'indexed': len(records), 'deleted': deleted, 'warnings': warnings, 'conflicts': conflicts}
        if dead_supersedes:
            result['supersedes_issues'] = dead_supersedes[:20]
            result['supersedes_issue_count'] = len(dead_supersedes)
        return result


class WarmSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-sync-warm-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.engine = subject.SyncEngine(self.vault, self.root / 'state')
        self.legacy = LegacySyncEngine(self.vault, self.root / 'old-state')

    def write(self, name='notes/note.md', id='note', text='Original calibration.\n', **fields):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(subject.render(dict(id=id, kind='note', revision=1, **fields), text), encoding='utf-8')
        return path

    def snapshot(self, engine):
        with engine.store._connect() as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('events', 'records', 'markdown_sources', 'receipts')}

    def assert_equivalent(self):
        self.assertEqual(self.engine.sync(), self.legacy.sync())
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.legacy))

    def test_events_records_and_conflicts_match_original(self):
        # Each case starts with the same already indexed source and full event history.
        for case in ('unchanged', 'edited', 'deleted', 'renamed', 'collision', 'duplicate'):
            with self.subTest(case=case):
                path = self.write(id=case)
                self.assert_equivalent()
                if case == 'edited':
                    self.write(id=case, text='Edited calibration.\n')
                elif case == 'deleted':
                    path.unlink()
                elif case == 'renamed':
                    path.rename(path.with_name('renamed.md'))
                elif case == 'duplicate':
                    self.write('notes/duplicate.md', id=case)
                elif case == 'collision':
                    # A non-Markdown writer owns this ID. Its record must survive.
                    payload = subject._json(dict(id='foreign', revision=7, source='notes/note.md',
                                                 text='Other writer', kind='note'))
                    for engine in (self.engine, self.legacy):
                        with engine.store._connect() as db:
                            db.execute('INSERT INTO records VALUES (?,?)', ('foreign', payload))
                    self.write('notes/collision.md', id='foreign')
                self.assert_equivalent()
                # Clear files through both engines so following cases share identical histories.
                for file in (self.vault / 'notes').iterdir():
                    file.unlink()
                self.assert_equivalent()

    def test_multiple_deletions_preserve_event_sequence(self):
        paths = [self.write(f'notes/note-{index}.md', id=f'note-{index}') for index in range(40)]
        self.assert_equivalent()
        for path in paths:
            path.unlink()
        self.assert_equivalent()

    def test_unchanged_warm_sync_has_constant_record_sql(self):
        for count in (4, 12):
            with self.subTest(count=count):
                for index in range(count):
                    self.write(f'notes/note-{index}.md', id=f'note-{index}')
                self.engine.sync()
                statements = []
                connect = self.engine.store._connect

                @contextmanager
                def traced():
                    with connect() as db:
                        db.set_trace_callback(statements.append)
                        yield db

                with patch.object(self.engine.store, '_connect', traced):
                    self.engine.sync()
                sql = [' '.join(s.upper().split()) for s in statements]
                self.assertFalse(any(s.startswith('SELECT PAYLOAD FROM RECORDS WHERE') for s in sql), sql)
                self.assertFalse(any(s.startswith('INSERT OR REPLACE INTO MARKDOWN_SOURCES') for s in sql), sql)
                self.assertEqual(sql.count('SELECT ID, PAYLOAD FROM RECORDS'), 1)
                self.assertEqual(sql.count('SELECT ID, SOURCE FROM MARKDOWN_SOURCES ORDER BY ID'), 1)
                self.assertLess(sql.index('BEGIN IMMEDIATE'), sql.index('SELECT ID, PAYLOAD FROM RECORDS'))

    def test_scan_shares_resolution_but_reads_each_source_twice(self):
        for index in range(6):
            self.write(f'notes/note-{index}.md', id=f'note-{index}')
        counts = []
        original_resolve, original_read, original_lstat = Path.resolve, Path.read_bytes, os.lstat
        for engine in (self.legacy, self.engine):
            calls = dict(resolve=0, read=0, lstat=0)

            def resolve(path, *args, **kwargs):
                calls['resolve'] += 1
                return original_resolve(path, *args, **kwargs)

            def read(path):
                calls['read'] += 1
                return original_read(path)

            def lstat(path, *args, **kwargs):
                calls['lstat'] += 1
                return original_lstat(path, *args, **kwargs)

            with patch.object(Path, 'resolve', resolve), patch.object(Path, 'read_bytes', read), \
                    patch.object(os, 'lstat', lstat):
                result = engine._scan()
            self.assertEqual(result[1:], ([], []))
            counts.append(calls)
        self.assertEqual(counts[0]['read'], 12)
        self.assertEqual(counts[1]['read'], 12)
        self.assertEqual(counts[1]['resolve'], 0)
        self.assertEqual(counts[0]['resolve'], 12)
        if os.name != 'nt':
            # Windows resolves through GetFinalPathNameByHandle, not lstat; there the
            # comparison is the resolve count above.
            self.assertLess(counts[1]['lstat'], counts[0]['lstat'])

    def test_windows_reparse_points_require_fresh_resolution(self):
        # Junctions can leave the vault without being reported as Unix symlinks.
        with patch.object(os, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR,
                                                                  st_reparse_tag=0xA0000003)):
            self.assertTrue(subject._path_redirected('synthetic-junction'))
        path = self.write()
        resolve = Path.resolve
        resolutions = []

        def counted(file, *args, **kwargs):
            resolutions.append(file)
            return resolve(file, *args, **kwargs)

        # Force the conservative path in both checks; the original source stays valid.
        with patch.object(subject, '_path_redirected', return_value=True), \
                patch('beyin_v3._path_redirected', return_value=True), \
                patch.object(Path, 'resolve', counted):
            records, warnings, conflicts = self.engine._scan()
        self.assertEqual(list(records), ['note'])
        self.assertEqual((warnings, conflicts), ([], []))
        self.assertGreaterEqual(resolutions.count(path), 2)

    def test_second_read_detects_source_change(self):
        path = self.write()
        original_read = Path.read_bytes
        calls = []

        def read(file):
            raw = original_read(file)
            if file == path:
                calls.append(file)
                if len(calls) == 1:
                    path.write_bytes(raw + b'Changed during scan.\n')
            return raw

        with patch.object(Path, 'read_bytes', read):
            records, warnings, conflicts = self.engine._scan()
        self.assertEqual(len(calls), 2)
        self.assertEqual(records, {})
        self.assertEqual(warnings, [{'source': 'notes/note.md', 'reason': 'source changed while scanning'}])
        self.assertEqual(conflicts, [])

    def test_second_validation_rejects_removed_source(self):
        path = self.write()
        original_read = Path.read_bytes

        def read(file):
            raw = original_read(file)
            if file == path:
                path.unlink()
            return raw

        with patch.object(Path, 'read_bytes', read):
            records, warnings, _ = self.engine._scan()
        self.assertEqual(records, {})
        self.assertEqual(warnings, [{'source': 'notes/note.md', 'reason': 'source missing or outside vault'}])

    def test_symlink_swap_between_reads_matches_original(self):
        original_read = Path.read_bytes
        outside = self.root / 'outside'
        outside.mkdir()
        for directory_swap, inside in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(directory_swap=directory_swap, inside=inside):
                target = self.vault / '.inside' if inside else outside
                target.mkdir(exist_ok=True)
                results = []
                for engine in (self.legacy, self.engine):
                    path = self.write()
                    (target / 'note.md').write_bytes(original_read(path))
                    swapped = []

                    def read(file):
                        raw = original_read(file)
                        if file == path and not swapped:
                            try:
                                if directory_swap:
                                    path.parent.rename(self.vault / 'saved-notes')
                                    path.parent.symlink_to(target, target_is_directory=True)
                                else:
                                    path.unlink()
                                    path.symlink_to(target / 'note.md')
                            except (OSError, NotImplementedError):
                                self.skipTest('symlinks unavailable on this platform')
                            swapped.append(True)
                        return raw

                    try:
                        with patch.object(Path, 'read_bytes', read):
                            results.append(engine._scan())
                    finally:
                        if swapped:
                            if directory_swap:
                                path.parent.unlink()
                                (self.vault / 'saved-notes').rename(path.parent)
                            else:
                                path.unlink()
                self.assertEqual(results[0], results[1])
                if inside:
                    self.assertEqual(list(results[1][0]), ['note'])
                    self.assertEqual(results[1][1:], ([], []))
                else:
                    self.assertEqual(results[1][0], {})
                    self.assertEqual(results[1][1], [{'source': 'notes/note.md',
                                                   'reason': 'source missing or outside vault'}])

    def test_path_safety_matches_original(self):
        path = self.write()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'outside.md').write_text('Outside source.\n', encoding='utf-8')
        try:
            (self.vault / 'inside-link.md').symlink_to(path)
            (self.vault / 'outside-link.md').symlink_to(outside / 'outside.md')
            (self.vault / 'broken-link.md').symlink_to(outside / 'missing.md')
            (self.vault / 'directory-link').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        self.assertEqual(self.engine._scan(), self.legacy._scan())
        self.assert_equivalent()
        for relative in ('../outside/outside.md', str(path.resolve()), 'notes', 'missing.md',
                         'inside-link.md', 'outside-link.md', 'broken-link.md',
                         'directory-link/outside.md'):
            errors = []
            for engine in (self.legacy, self.engine):
                with self.assertRaises(ValueError) as caught:
                    engine._path(relative, existing=True)
                errors.append(str(caught.exception))
            self.assertEqual(errors[0], errors[1])


if __name__ == '__main__':
    unittest.main()
