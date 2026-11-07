"""Offline daily layout ownership, migration and cross-machine contracts."""
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_preferences as preferences
import beyin_v3_projections as projections
from beyin_v3_sync import SyncEngine, render


class DailyLayoutTest(unittest.TestCase):
    day = '2026-09-28'
    stamp = '2026-09-28T12:00:00+00:00'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-daily-layout-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'vault'
        (self.vault / 'notes').mkdir(parents=True)
        (self.vault / 'notes/source.md').write_text('# Source\nPublic note.\n', encoding='utf-8')
        self.engine = SyncEngine(self.vault, self.root / 'state')
        zone = patch.object(projections, '_local_zone', return_value=timezone.utc)
        zone.start()
        self.addCleanup(zone.stop)

    def daily(self, layout):
        return self.vault / ('daily/' if layout == 'flat' else 'daily/v3/') / (self.day + '.md')

    def receipt(self, ident='seed', refs=None):
        directory = self.vault / 'receipts'
        directory.mkdir(exist_ok=True)
        source = 'receipts/' + hashlib.sha256(ident.encode()).hexdigest() + '.md'
        (self.vault / source).write_bytes(render(
            {'kind': 'receipt', 'event_id': ident, 'harness': 'codex',
             'refs': refs if refs is not None else ['notes/source.md'],
             'visibility': 'internal', 'created_at': self.stamp}, 'Recorded result.\n').encode())
        return source

    def tracked(self):
        with self.engine.store._connect() as db:
            return dict(projections.receipt_view_rows(db))

    def visible(self):
        with self.engine.store._connect() as db:
            return [event['event_id'] for _, event in projections.latest_receipts(db, self.vault)]

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                                 '--vault', str(self.vault), '--state', str(self.engine.state), *args],
                                capture_output=True, text=True, encoding='utf-8',
                                env=inherited_env(), timeout=60)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def test_default_v3_bytes_are_unchanged(self):
        source = self.receipt()
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        header = '---\n{\n  "generated": true,\n  "kind": "receipt-index"\n}\n---\n'
        expected = (header + '# Recorded outcomes\n\nAgent-authored claims, not independently verified facts.\n\n'
                    + '## ' + self.stamp + '\n\nRecorded result.\n\nSource: [[' + source + '|seed]]\n')
        outcomes = (header + '# Outcome source index\n\nThis index links semantic receipts. '
                    'It is not an automatic knowledge compiler.\n\n- 2026-09-28: Recorded result.\n'
                    '  Source: [[' + source + '|seed]]\n')
        self.assertEqual(self.daily('v3').read_bytes(), expected.encode())
        self.assertEqual((self.vault / 'knowledge/v3/outcomes.md').read_bytes(), outcomes.encode())
        self.assertFalse(self.daily('flat').exists())
        before = self.daily('v3').read_bytes()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine.sync()
        self.assertEqual(self.daily('v3').read_bytes(), before)

    def test_receipt_link_alias_cannot_break_the_wikilink(self):
        source = 'receipts/' + 'a' * 64 + '.md'
        self.assertEqual(projections._receipt_link(source, 'update-check-2026-10-16'),
                         '[[' + source + '|update-check-2026-10-16]]')
        self.assertEqual(projections._receipt_link(source, 'a|b]]c#d^e\n[[x'),
                         '[[' + source + '|a b c d e x]]')
        self.assertEqual(projections._receipt_link(source, ' ||[] '), '[[' + source + ']]')
        self.assertEqual(len(projections._receipt_link(source, 'x' * 200)), len('[[' + source + '|]]') + 80)

    def test_views_written_by_older_engines_take_the_alias_without_conflict(self):
        source = self.receipt()
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        relative = self.daily('v3').relative_to(self.vault).as_posix()
        # Bytes an engine before #514 wrote and tracked: bare hash link.
        old = self.daily('v3').read_bytes().replace(('|seed]]').encode(), b']]')
        self.daily('v3').write_bytes(old)
        with self.engine.store._connect() as db:
            db.execute('UPDATE receipt_views SET hash=? WHERE path=?', (hashlib.sha256(old).hexdigest(), relative))
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result.get('conflicts', []), [])
        self.assertIn(('[[' + source + '|seed]]').encode(), self.daily('v3').read_bytes())

    def test_flat_tracking_is_invisible_to_older_engines(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        source = self.daily('flat').relative_to(self.vault).as_posix()
        raw = self.daily('flat').read_bytes()
        with self.engine.store._connect() as db:
            legacy = dict(db.execute('SELECT path,hash FROM receipt_views'))
            self.assertNotIn(source, legacy)
            self.assertEqual(dict(db.execute('SELECT path,hash FROM receipt_flat_views')),
                             {source: hashlib.sha256(raw).hexdigest()})
            self.assertTrue(all(projections.is_receipt_view_source(path) for path in legacy))
        # An older engine ignores the separate table. Its default v3 layout can
        # coexist with the flat file; upgrading again must retire only its view.
        preferences.save_daily_layout(self.vault, 'v3')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertFalse(self.daily('flat').exists())
        self.assertTrue(self.daily('v3').exists())

    def test_original_flat_tracking_migrates_without_losing_edited_view(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        digest = self.tracked()[source]
        # Simulate the first PR version's database, retaining its original hash.
        with self.engine.store._connect() as db:
            db.execute('DELETE FROM receipt_flat_views WHERE path=?', (source,))
            db.execute('INSERT INTO receipt_views VALUES (?,?)', (source, digest))
        edited = self.daily('flat').read_bytes() + b'Human addition.\n'
        self.daily('flat').write_bytes(edited)
        preferences.save_daily_layout(self.vault, 'v3')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded', result)
        self.assertEqual(self.daily('flat').read_bytes(), edited)
        with self.engine.store._connect() as db:
            self.assertIsNone(db.execute('SELECT hash FROM receipt_views WHERE path=?', (source,)).fetchone())
            self.assertEqual(db.execute('SELECT hash FROM receipt_flat_views WHERE path=?', (source,)).fetchone()[0], digest)
        self.assertEqual(self.engine.sync(recover_views=True)['status'], 'succeeded')
        self.assertFalse(self.daily('flat').exists())

    def test_flat_and_both_untouched_transitions_preserve_bytes(self):
        self.receipt()
        self.engine.sync()
        original = self.daily('v3').read_bytes()
        for layout, old in (('flat', 'v3'), ('v3', 'flat')):
            preferences.save_daily_layout(self.vault, layout)
            self.assertEqual(self.engine.sync()['status'], 'succeeded')
            self.assertEqual(self.daily(layout).read_bytes(), original)
            self.assertFalse(self.daily(old).exists())
            self.assertNotIn(self.daily(old).relative_to(self.vault).as_posix(), self.tracked())

    def test_human_daily_is_indexed_and_receipt_remains_visible_even_with_recovery(self):
        preferences.save_daily_layout(self.vault, 'flat')
        note = self.daily('flat')
        note.parent.mkdir()
        original = b'# My day\r\nHuman journal.\r\n'
        note.write_bytes(original)
        source = note.relative_to(self.vault).as_posix()
        self.receipt(refs=[source])
        for fresh in (False, True):
            if fresh:
                self.engine = SyncEngine(self.vault, self.root / 'fresh-state')
            for recover in (False, True):
                result = self.engine.sync(recover_views=recover)
                self.assertEqual(result['status'], 'succeeded', result)
                self.assertEqual(result['conflicts'], [])
                self.assertEqual(note.read_bytes(), original)
                self.assertNotIn(source, self.tracked())
                self.assertIn('seed', self.visible())
                with self.engine.store._connect() as db:
                    self.assertIsNotNone(db.execute('SELECT id FROM markdown_sources WHERE source=?', (source,)).fetchone())
                    self.assertEqual(db.execute('SELECT id FROM receipt_view_receipts').fetchall(), [])
                    row = db.execute('SELECT value FROM metadata WHERE key=?', (projections.RECEIPT_VIEW_SOURCES_ROW,)).fetchone()
                    self.assertNotIn(source, json.loads(row[0]))
        self.assertIn('Recorded result.', (self.vault / 'knowledge/v3/outcomes.md').read_text())
        self.assertFalse((note.parent / '.archive').exists())

    def test_transition_to_flat_with_human_daily_retires_only_old_view(self):
        self.receipt()
        self.engine.sync()
        self.daily('flat').write_bytes(b'# Human day\n')
        preferences.save_daily_layout(self.vault, 'flat')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.daily('flat').read_bytes(), b'# Human day\n')
        self.assertFalse(self.daily('v3').exists())

    def test_other_generated_daily_is_not_claimed_as_engine_receipt_view(self):
        preferences.save_daily_layout(self.vault, 'flat')
        note = self.daily('flat')
        note.parent.mkdir()
        raw = render({'generated': True, 'kind': 'journal'}, '# Another generator\n').encode()
        note.write_bytes(raw)
        source = note.relative_to(self.vault).as_posix()
        self.receipt(refs=[source])
        self.assertEqual(self.engine.sync(recover_views=True)['status'], 'succeeded')
        self.assertEqual(note.read_bytes(), raw)
        self.assertNotIn(source, self.tracked())
        self.assertIn('seed', self.visible())

    def test_edited_views_in_both_transitions_stay_visible_until_recovered(self):
        self.receipt()
        for old, new in (('v3', 'flat'), ('flat', 'v3')):
            preferences.save_daily_layout(self.vault, old)
            self.engine.sync()
            original = self.daily(old).read_bytes() + b'\nUser edit\r\n'
            self.daily(old).write_bytes(original)
            preferences.save_daily_layout(self.vault, new)
            result = self.engine.sync()
            self.assertEqual(result['status'], 'degraded', result)
            self.assertTrue(any(w['source'] == self.daily(old).relative_to(self.vault).as_posix()
                                and w.get('kind') == 'receipt_view' for w in result['warnings']))
            self.assertEqual(self.daily(old).read_bytes(), original)
            self.assertTrue(self.daily(new).exists())
            from beyin_v3_hook import drain_queue
            self.assertEqual(drain_queue(self.vault, self.engine.state)['failed'], 0)
            doctor = self.cli('doctor')
            self.assertTrue(any(w['source'] == self.daily(old).relative_to(self.vault).as_posix()
                                for w in doctor['receipt_view_conflicts']))
            recovered = self.engine.sync(recover_views=True)
            self.assertEqual(recovered['status'], 'succeeded', recovered)
            self.assertFalse(self.daily(old).exists())
            self.assertEqual(len(recovered['recovered_views']), 1)
            archive = self.vault / recovered['recovered_views'][0]['archive']
            self.assertEqual(archive.read_bytes(), original)

    def test_generated_flat_receipts_are_withheld_across_state_rebuild_and_layout_change(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('derived', [source])
        self.engine.sync()
        self.assertNotIn('derived', self.visible())
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine = SyncEngine(self.vault, self.root / 'fresh-state')
        self.engine.sync()
        self.assertNotIn('derived', self.visible())

    def test_edited_tracked_flat_view_stays_quarantined_without_generated_header(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        note = self.daily('flat')
        note.write_bytes(b'# Edited generated day\n')
        self.receipt('derived', [note.relative_to(self.vault).as_posix()])
        result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded')
        self.assertNotIn('derived', self.visible())
        self.assertEqual(note.read_bytes(), b'# Edited generated day\n')

    def test_human_note_reusing_retired_flat_path_does_not_hide_new_receipts(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('view-derived', [source])
        self.engine.sync()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine.sync()
        self.daily('flat').write_bytes(b'# New human journal\n')
        self.receipt('human-derived', [source])
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertIn('human-derived', self.visible())
        self.assertNotIn('view-derived', self.visible())

    def test_retired_flat_receipt_provenance_survives_state_reset_and_human_reuse(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        receipt_source = self.receipt('view-derived', [source])
        immutable = (self.vault / receipt_source).read_bytes()
        self.engine.sync()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine.sync()
        self.assertFalse(self.daily('flat').exists())
        self.daily('flat').write_bytes(b'# New human journal\n')
        self.receipt('human-derived', [source])
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertIn('human-derived', self.visible())
        self.assertNotIn('view-derived', self.visible())
        with self.engine.store._connect() as db:
            from datetime import date
            recap = projections.recent_receipts(db, vault=self.vault, today=date.fromisoformat(self.day))
        self.assertNotIn(receipt_source, [item['source'] for item in recap['items']])
        self.assertEqual((self.vault / receipt_source).read_bytes(), immutable)
        other = self.root / 'copied-vault'
        shutil.copytree(self.vault, other)
        copied = SyncEngine(other, self.root / 'copied-state')
        self.assertEqual(copied.sync()['status'], 'succeeded')
        with copied.store._connect() as db:
            visible = [event['event_id'] for _, event in projections.latest_receipts(db, other)]
        self.assertIn('human-derived', visible)
        self.assertNotIn('view-derived', visible)

    def test_state_reset_retires_unchanged_orphan_flat_view(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        original = self.daily('flat').read_bytes()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(self.daily('flat').exists())
        self.assertEqual(self.daily('v3').read_bytes(), original)

    def test_state_reset_with_human_flat_daily_retires_unchanged_orphan_v3_view(self):
        self.receipt()
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        human = b'# My day\r\nHuman journal.\r\n'
        self.daily('flat').write_bytes(human)
        preferences.save_daily_layout(self.vault, 'flat')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        for _ in range(2):
            result = self.engine.sync()
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(self.daily('flat').read_bytes(), human)
            self.assertFalse(self.daily('v3').exists())
            for layout in ('flat', 'v3'):
                self.assertNotIn(self.daily(layout).relative_to(self.vault).as_posix(), self.tracked())
        self.assertFalse((self.vault / 'daily/.archive').exists())
        self.assertFalse((self.vault / 'daily/v3/.archive').exists())

    def test_state_reset_with_human_flat_daily_preserves_edited_orphan_v3_view(self):
        self.receipt()
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        original = self.daily('v3').read_bytes() + b'\nHuman addition\r\n'
        self.daily('v3').write_bytes(original)
        human = b'# My day\r\nHuman journal.\r\n'
        self.daily('flat').write_bytes(human)
        preferences.save_daily_layout(self.vault, 'flat')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        for _ in range(2):
            result = self.engine.sync()
            self.assertEqual(result['status'], 'degraded', result)
            self.assertTrue(any(w.get('kind') == 'receipt_view'
                                and w['source'] == self.daily('v3').relative_to(self.vault).as_posix()
                                for w in result['warnings']))
            self.assertEqual(self.daily('v3').read_bytes(), original)
            self.assertEqual(self.daily('flat').read_bytes(), human)
            self.assertNotIn(self.daily('flat').relative_to(self.vault).as_posix(), self.tracked())
        result = self.engine.sync(recover_views=True)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(self.daily('v3').exists())
        self.assertEqual(self.daily('flat').read_bytes(), human)
        self.assertEqual(len(result['recovered_views']), 1)
        self.assertEqual((self.vault / result['recovered_views'][0]['archive']).read_bytes(), original)
        self.assertFalse((self.vault / 'daily/.archive').exists())

    def test_legacy_orphan_recorded_as_alias_can_still_be_retired(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        note = self.daily('flat')
        source = note.relative_to(self.vault).as_posix()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        # The original implementation kept an orphan only in its alias table
        # after one sync. Upgrading that already-built state must repair it too.
        with self.engine.store._connect() as db:
            db.execute('CREATE TABLE receipt_view_aliases(source TEXT PRIMARY KEY, identity TEXT NOT NULL)')
            db.execute('INSERT INTO receipt_view_aliases VALUES (?,?)',
                       (source, json.dumps({'identity': self.engine._source_identity(note),
                                            'sha256': hashlib.sha256(note.read_bytes()).hexdigest()})))
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(note.exists())

    def test_state_reset_orphan_flat_edit_is_preserved_until_explicit_recovery(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        original = self.daily('flat').read_bytes() + b'\nHuman addition\r\n'
        self.daily('flat').write_bytes(original)
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded', result)
        self.assertTrue(any(w.get('kind') == 'receipt_view' for w in result['warnings']))
        self.assertEqual(self.daily('flat').read_bytes(), original)
        result = self.engine.sync(recover_views=True)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(self.daily('flat').exists())
        self.assertEqual(len(result['recovered_views']), 1)
        self.assertEqual((self.vault / result['recovered_views'][0]['archive']).read_bytes(), original)

    def test_flat_symlink_is_warned_and_skipped_without_failing_sync(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        note = self.daily('flat')
        note.parent.mkdir()
        target = self.root / 'outside.md'
        target.write_bytes(b'# Outside journal\n')
        try:
            note.symlink_to(target)
        except OSError as exc:
            self.skipTest('Symlinks unavailable: ' + str(exc))
        for recover in (False, True):
            result = self.engine.sync(recover_views=recover)
            self.assertEqual(result['status'], 'degraded', result)
            self.assertEqual(result['conflicts'], [])
            self.assertTrue(any(w['source'] == 'daily/' + self.day + '.md'
                                and ('symlink' in w['reason'] or 'outside vault' in w['reason'])
                                for w in result['warnings']))
            self.assertTrue(note.is_symlink())
            self.assertEqual(target.read_bytes(), b'# Outside journal\n')
            self.assertTrue((self.vault / 'knowledge/v3/outcomes.md').is_file())
            self.assertIn('seed', self.visible())

    def test_tracked_flat_replaced_by_symlink_is_preserved_with_warning(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        note = self.daily('flat')
        note.unlink()
        target = self.vault / 'notes/source.md'
        original = target.read_bytes()
        try:
            note.symlink_to(target)
        except OSError as exc:
            self.skipTest('Symlinks unavailable: ' + str(exc))
        result = self.engine.sync(recover_views=True)
        self.assertEqual(result['status'], 'degraded', result)
        self.assertEqual(result['conflicts'], [])
        self.assertTrue(note.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_quarantine_marker_write_failure_preserves_view_and_retries(self):
        import beyin_v3_sync as sync
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('view-derived', [source])
        original = self.daily('flat').read_bytes()
        preferences.save_daily_layout(self.vault, 'v3')
        atomic = sync.atomic
        def fail_marker(path, content):
            if str(path).endswith(projections.RECEIPT_VIEW_RECEIPT_SUFFIX):
                raise OSError('injected provenance write failure')
            return atomic(path, content)
        with patch.object(sync, 'atomic', fail_marker):
            result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded', result)
        self.assertTrue(any('receipt quarantine provenance unavailable' in w['reason'] for w in result['warnings']))
        self.assertEqual(self.daily('flat').read_bytes(), original)
        self.assertNotIn('view-derived', self.visible())
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertFalse(self.daily('flat').exists())

    def test_pre_upgrade_quarantine_rows_are_backfilled_after_view_retirement(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('view-derived', [source])
        self.engine.sync()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine.sync()
        marker = self.vault / 'receipts' / (hashlib.sha256(b'view-derived').hexdigest() +
                                          projections.RECEIPT_VIEW_RECEIPT_SUFFIX)
        marker.unlink()  # the old version had only the existing SQLite quarantine row
        self.daily('flat').write_bytes(b'# New human journal\n')
        self.receipt('human-derived', [source])
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertTrue(marker.is_file())
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertNotIn('view-derived', self.visible())
        self.assertIn('human-derived', self.visible())

    @unittest.skipUnless(os.name == 'posix', 'Directory fsync barriers require POSIX')
    def test_quarantine_directory_fsync_failure_keeps_view_until_durable(self):
        import stat
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('view-derived', [source])
        original = self.daily('flat').read_bytes()
        preferences.save_daily_layout(self.vault, 'v3')
        receipts_inode = (self.vault / 'receipts').stat().st_ino
        fsync = os.fsync
        def fail_directory(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == receipts_inode:
                raise OSError('injected receipts directory durability failure')
            return fsync(fd)
        with patch('os.fsync', fail_directory):
            result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded', result)
        self.assertEqual(self.daily('flat').read_bytes(), original)
        self.assertTrue(any('provenance not durable' in w['reason'] for w in result['warnings']))
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertFalse(self.daily('flat').exists())
        self.engine = SyncEngine(self.vault, self.root / 'rebuilt-state')
        self.engine.sync()
        self.assertNotIn('view-derived', self.visible())

    def test_damaged_quarantine_marker_stays_fail_closed_without_hiding_new_human_receipts(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        source = self.daily('flat').relative_to(self.vault).as_posix()
        self.receipt('view-derived', [source])
        self.engine.sync()
        preferences.save_daily_layout(self.vault, 'v3')
        self.engine.sync()
        self.daily('flat').write_bytes(b'# New human journal\n')
        self.receipt('human-derived', [source])
        self.engine.sync()
        marker = self.vault / 'receipts' / (hashlib.sha256(b'view-derived').hexdigest() +
                                          projections.RECEIPT_VIEW_RECEIPT_SUFFIX)
        invalid_schema = json.dumps({'schema': True, 'kind': 'receipt-view-quarantine',
                                     'event_id_sha256': hashlib.sha256(b'view-derived').hexdigest()})
        for malformed in ('{broken', '[]', invalid_schema, 'x' * 5000):
            marker.write_text(malformed, encoding='utf-8')
            self.engine = SyncEngine(self.vault, self.root / ('rebuilt-' + str(len(malformed))))
            result = self.engine.sync()
            self.assertEqual(result['status'], 'degraded', result)
            self.assertEqual(marker.read_text(encoding='utf-8'), malformed)
            self.assertNotIn('view-derived', self.visible())
            self.assertIn('human-derived', self.visible())

    def test_flat_archive_alias_sidecar_survives_state_rebuild(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        note = self.daily('flat')
        raw = b'# View with removed header\n'
        note.write_bytes(raw)
        alias = self.vault / 'notes/alias.md'
        try:
            os.link(note, alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        self.receipt('derived', ['notes/alias.md'])
        result = self.engine.sync(recover_views=True)
        self.assertEqual(result['status'], 'succeeded', result)
        archive = self.vault / result['recovered_views'][0]['archive']
        self.assertEqual(archive.parent, self.vault / 'daily/.archive')
        self.assertEqual(archive.read_bytes(), raw)
        sidecar = Path(str(archive) + '.provenance.json')
        self.assertTrue(sidecar.exists())
        self.engine = SyncEngine(self.vault, self.root / 'fresh-state')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertNotIn('derived', self.visible())
        data = json.loads(sidecar.read_text())
        data['view'] = 'daily/v3/' + self.day + '.md'
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        warnings = []
        self.assertEqual(projections.archived_receipt_view_aliases(self.engine, warnings), set())
        self.assertTrue(warnings, 'sidecar must belong to the adjacent view directory')

    def test_two_machines_share_preference_and_produce_identical_views(self):
        preferences.save_daily_layout(self.vault, 'flat')
        self.receipt()
        self.engine.sync()
        other = self.root / 'second-vault'
        shutil.copytree(self.vault, other)
        second = SyncEngine(other, self.root / 'second-state')
        self.assertEqual(preferences.read_daily_layout(other), 'flat')
        self.assertEqual(second.sync()['status'], 'succeeded')
        for path in ('daily/' + self.day + '.md', 'knowledge/v3/outcomes.md'):
            self.assertEqual((other / path).read_bytes(), (self.vault / path).read_bytes())
        # Both independent states also converge on the reverse transition.
        preferences.save_daily_layout(self.vault, 'v3')
        shutil.copyfile(self.vault / '.beyin-daily-layout.json', other / '.beyin-daily-layout.json')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(second.sync()['status'], 'succeeded')
        self.assertFalse((other / 'daily' / (self.day + '.md')).exists())
        self.assertEqual((other / 'daily/v3' / (self.day + '.md')).read_bytes(), self.daily('v3').read_bytes())

    def test_installed_entry_projects_flat_with_shipped_modules(self):
        preferences.save_daily_layout(self.vault, 'flat')
        installed = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'),
                                    '--vault', str(self.vault), '--state', str(self.engine.state)],
                                   capture_output=True, env=inherited_env(), timeout=60)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.receipt()  # a new receipt, after the installer's historical migration snapshot
        synced = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'sync', '--json'],
                                capture_output=True, env=inherited_env(), timeout=60)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        # The subprocess uses the real local zone, not the in-process UTC patch.
        local_name = projections.receipt_day(self.stamp) + '.md'
        self.assertTrue((self.vault / 'daily' / local_name).is_file())
        self.assertFalse((self.vault / 'daily/v3' / local_name).exists())
        self.assertEqual(preferences.read_daily_layout(self.vault), 'flat')
        human = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'preferences', '--human'],
                               capture_output=True, text=True, encoding='utf-8', env=inherited_env(), timeout=60)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('Daily duzeni: flat (daily/YYYY-MM-DD.md)', human.stdout)

    def test_malformed_shared_layout_keeps_sync_and_freezes_views(self):
        # .beyin-daily-layout.json travels through git; a merge conflict must not stop
        # indexing, receipts or hooks. Views stay as they are until the file is valid.
        self.receipt()
        preferences.save_daily_layout(self.vault, 'flat')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        before = self.daily('flat').read_bytes()
        layout = self.vault / '.beyin-daily-layout.json'
        layout.write_text('<<<<<<< HEAD\n{"schema": 1, "daily_layout": "flat"}\n=======\n'
                          '{"schema": 1, "daily_layout": "v3"}\n>>>>>>> other\n', encoding='utf-8')
        second = self.receipt('second')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('.beyin-daily-layout.json', [issue.get('source') for issue in result['warnings']])
        self.assertEqual(self.daily('flat').read_bytes(), before)
        self.assertFalse(self.daily('v3').exists())
        self.assertIn(self.daily('flat').relative_to(self.vault).as_posix(), self.tracked())
        written = self.cli('receipt', '--harness', 'codex', '--event-id', 'during-conflict',
                           '--summary', 'Kayıt sürüyor.', '--ref', 'notes/source.md')
        self.assertEqual(written['status'], 'succeeded')
        layout.write_text('{"schema": 1, "daily_layout": "flat"}\n', encoding='utf-8')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertIn(second.encode(), self.daily('flat').read_bytes())

    def test_flat_name_alone_is_not_view_provenance(self):
        source = 'daily/' + self.day + '.md'
        self.assertFalse(projections.is_receipt_view_source(source))
        self.assertTrue(projections.is_receipt_view_source(source, proven=True))
        self.assertTrue(projections.is_receipt_view_source('daily/v3/' + self.day + '.md'))


if __name__ == '__main__':
    unittest.main()
