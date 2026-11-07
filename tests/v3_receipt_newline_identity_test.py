"""Installed receipt identities ignore line endings without losing edit protection (#602)."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python


class ReceiptNewlineIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-receipt-newlines-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.env = isolated_env(self.root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/a.md').write_text('# Synthetic source\n', encoding='utf-8')
        (self.vault / 'notes/b.md').write_text('# Other source\n', encoding='utf-8')

    def cli(self, *args, payload=None, ok=True):
        result = run_python(self.vault / 'beyin.py', args, self.vault, self.env, payload)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        return result

    def receipt(self, event='r1', summary='First line\nSecond line', harness='claude'):
        return self.cli('receipt', '--harness', harness, payload={
            'event_id': event, 'summary': summary, 'refs': ['notes/a.md']})

    def saved(self, event='r1'):
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            return json.loads(db.execute('SELECT payload FROM receipts WHERE id=?', (event,)).fetchone()[0])

    def views(self):
        return sorted((self.vault / 'daily').rglob('*.md')) + [self.vault / 'knowledge/v3/outcomes.md']

    def seed_legacy(self):
        """Recreate the pre-fix writer's mixed LF wrapper and CRLF summary exactly."""
        first = self.receipt()
        source = self.vault / first['source']
        paths = [source, *self.views()]
        for path in paths:
            path.write_bytes(path.read_bytes().replace(b'First line\nSecond line', b'First line\r\nSecond line'))
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            old = self.saved()
            old['summary'] = 'First line\r\nSecond line'
            db.execute('UPDATE receipts SET payload=? WHERE id=?', (json.dumps(old), 'r1'))
            for path in self.views():
                relative = path.relative_to(self.vault).as_posix()
                table = 'receipt_flat_views' if relative.startswith('daily/') and not relative.startswith('daily/v3/') else 'receipt_views'
                db.execute(f'UPDATE {table} SET hash=? WHERE path=?', (hashlib.sha256(path.read_bytes()).hexdigest(), relative))
            db.commit()
        return source

    def assert_clean(self, report):
        self.assertEqual(report['status'], 'succeeded', report)
        self.assertEqual(report.get('conflicts', []), [], report)
        self.assertEqual(report.get('warnings', []), [], report)

    def view_snapshot(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
                for path in self.views()}

    def assert_legacy_lf_migration(self, flat=False):
        if flat:
            self.cli('preferences', '--daily-layout', 'flat')
        source = self.seed_legacy()
        source_bytes = source.read_bytes()
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            payload = db.execute('SELECT payload FROM receipts WHERE id=?', ('r1',)).fetchone()[0]
        expected = {path: path.read_bytes().replace(b'\r\n', b'\n') for path in self.views()}
        self.assert_clean(self.cli('sync', '--full'))
        for path, raw in expected.items():
            self.assertEqual(path.read_bytes(), raw)
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            for path in self.views():
                relative = path.relative_to(self.vault).as_posix()
                table = 'receipt_flat_views' if flat and relative.startswith('daily/') else 'receipt_views'
                tracked = db.execute(f'SELECT hash FROM {table} WHERE path=?', (relative,)).fetchone()[0]
                self.assertEqual(tracked, hashlib.sha256(path.read_bytes()).hexdigest())
        for save in (False, True, True):
            if save:
                for path in self.views():
                    path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
            before = self.view_snapshot()
            self.assert_clean(self.cli('sync', '--full'))
            self.assertEqual(self.view_snapshot(), before, 'LF saves must not cause another replacement')
        self.receipt(harness='codex')
        self.assertEqual(self.view_snapshot(), before)
        self.assertEqual(source.read_bytes(), source_bytes)
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT payload FROM receipts WHERE id=?', ('r1',)).fetchone()[0], payload)

    def test_legacy_views_migrate_to_lf_once_and_lf_saves_are_stable(self):
        self.assert_legacy_lf_migration()

    def test_flat_legacy_views_migrate_to_lf_once_and_lf_saves_are_stable(self):
        self.assert_legacy_lf_migration(flat=True)

    def assert_fresh_state_retires_legacy_daily(self, normalized, edited=False, unproved_crlf=False):
        from v3_sync_test import load_module
        subject = load_module()
        from beyin_v3_preferences import save_daily_layout
        source = self.seed_legacy()
        source_bytes = source.read_bytes()
        daily = self.views()[0]
        if normalized:
            for path in self.views():
                path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        if unproved_crlf:
            daily.write_bytes(daily.read_bytes().replace(b'\n', b'\r\n'))
        if edited:
            daily.write_bytes(daily.read_bytes() + b'Human annotation\n')
        before = daily.read_bytes()
        save_daily_layout(self.vault, 'flat')
        engine = subject.SyncEngine(self.vault, self.root / 'fresh-state')
        report = engine.sync(full=True)
        if edited or unproved_crlf:
            self.assertEqual(report['status'], 'degraded', report)
            self.assertEqual(daily.read_bytes(), before)
            self.assertTrue(any(w['source'] == daily.relative_to(self.vault).as_posix()
                                and w.get('kind') == 'receipt_view' for w in report['warnings']), report)
        else:
            self.assert_clean(report)
            self.assertFalse(daily.exists())
            self.assertNotIn(b'\r', (self.vault / 'daily' / daily.name).read_bytes())
        self.assertEqual(source.read_bytes(), source_bytes)
        with engine.store._connect() as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM receipts WHERE id=?', ('r1',)).fetchone()[0])['summary'],
                             'First line\r\nSecond line')

    def test_fresh_state_retires_exact_raw_legacy_daily(self):
        self.assert_fresh_state_retires_legacy_daily(normalized=False)

    def test_fresh_state_retires_exact_lf_legacy_daily(self):
        self.assert_fresh_state_retires_legacy_daily(normalized=True)

    def test_fresh_state_preserves_edited_lf_stale_daily(self):
        self.assert_fresh_state_retires_legacy_daily(normalized=True, edited=True)

    def test_stale_adoption_requires_exact_observed_hash_not_normalized_bytes(self):
        self.assert_fresh_state_retires_legacy_daily(normalized=True, unproved_crlf=True)

    def test_legacy_lf_migration_keeps_hardlink_alias_excluded(self):
        self.seed_legacy()
        view = self.vault / 'knowledge/v3/outcomes.md'
        alias = self.vault / 'notes/view-alias.md'
        try:
            os.link(view, alias)
        except OSError as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        original = alias.read_bytes()
        self.assert_clean(self.cli('sync', '--full'))
        self.assertNotIn(b'\r', view.read_bytes())
        self.assertEqual(alias.read_bytes(), original)
        self.assertFalse(alias.samefile(view))
        self.assert_clean(self.cli('sync', '--full'))
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            self.assertNotIn('notes/view-alias.md', [row[0] for row in db.execute('SELECT source FROM markdown_sources')])

    def test_legacy_lf_migration_keeps_current_privacy_filter(self):
        source = self.seed_legacy()
        before, indexed = source.read_bytes(), self.saved()
        self.assert_clean(self.cli('sync', '--full'))
        (self.vault / 'notes/a.md').write_text('---\nvisibility: private\n---\nSynthetic private source\n', encoding='utf-8')
        self.assert_clean(self.cli('sync', '--full'))
        for path in self.views():
            self.assertNotIn(b'First line', path.read_bytes())
            self.assertIn(b'[summary withheld:', path.read_bytes())
            self.assertNotIn(b'\r', path.read_bytes())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.saved(), indexed)

    def test_changed_privacy_cannot_prove_drifted_legacy_view(self):
        self.seed_legacy()
        for path in self.views():
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        before = self.view_snapshot()
        (self.vault / 'notes/a.md').write_text('---\nvisibility: private\n---\nSynthetic private source\n', encoding='utf-8')
        report = self.cli('sync', '--full')
        self.assertEqual(report['status'], 'degraded', report)
        self.assertEqual(self.view_snapshot(), before)
        self.assertEqual(len([w for w in report['warnings'] if w.get('kind') == 'receipt_view']), 2)

    def test_new_summary_file_and_json_replays_use_lf(self):
        summary = 'İlk satır\r\nİkinci satır\rSon satır\n'
        path = self.root / 'summary.txt'
        path.write_bytes(summary.encode())
        first = self.cli('receipt', '--event-id', 'r1', '--summary-file', path, '--ref', 'notes/a.md')
        self.assertEqual(self.saved()['summary'], 'İlk satır\nİkinci satır\nSon satır\n')
        for output in [self.vault / first['source'], *self.views()]:
            self.assertNotIn(b'\r', output.read_bytes())
        self.receipt(summary=summary, harness='codex')
        self.receipt(summary=summary.replace('\r\n', '\n').replace('\r', '\n'))

    def test_legacy_replay_preserves_source_and_index(self):
        source = self.seed_legacy()
        before, indexed = source.read_bytes(), self.saved()
        self.receipt(summary='First line\nSecond line', harness='codex')
        self.receipt(summary='First line\rSecond line')
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.saved(), indexed)

    def test_legacy_normalized_views_accept_new_receipt_without_intermediate_sync(self):
        self.seed_legacy()
        for view in self.views():
            view.write_bytes(view.read_bytes().replace(b'\r\n', b'\n'))
        second = self.receipt('r2', 'New recorded result')
        self.assertNotIn('source_sync', second)
        for view in self.views():
            self.assertIn(b'New recorded result', view.read_bytes())
            self.assertNotIn(b'\r', view.read_bytes())
        self.assert_clean(self.cli('sync', '--full'))

    def test_legacy_sources_and_views_normalize_without_divergence(self):
        source = self.seed_legacy()
        for path in [source, *self.views()]:
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        before = source.read_bytes()
        self.assert_clean(self.cli('sync', '--full'))
        self.receipt(harness='codex')
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.saved()['summary'], 'First line\r\nSecond line')

    def test_full_crlf_checkout_and_state_reset_replay(self):
        first = self.receipt(summary='First line\nSecond line\n')
        source = self.vault / first['source']
        for path in [source, *self.views()]:
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        before = source.read_bytes()
        self.assert_clean(self.cli('sync', '--full'))
        self.receipt(summary='First line\nSecond line\n')
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            db.execute('DELETE FROM receipts')
            db.execute("DELETE FROM metadata WHERE key='receipt_scan_signature'")
            db.commit()
        self.receipt(summary='First line\nSecond line\n', harness='codex')
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.saved()['harness'], 'claude')

    def test_normalized_view_with_real_edit_stays_preserved(self):
        self.seed_legacy()
        before = {}
        for view in self.views():
            raw = view.read_bytes().replace(b'\r\n', b'\n') + b'Human annotation\n'
            view.write_bytes(raw)
            before[view] = raw
        second = self.receipt('r2', 'New recorded result')
        self.assertEqual(len(second['source_sync']['warnings']), 2)
        for view, raw in before.items():
            self.assertEqual(view.read_bytes(), raw)
        self.assertEqual(self.cli('sync', '--full')['status'], 'degraded')

    def test_actual_summary_refs_links_and_source_edits_still_conflict(self):
        source = self.seed_legacy()
        for changes in ({'summary': 'Changed line\nSecond line'}, {'refs': ['notes/b.md']},
                        {'links': ['https://example.com/changed']}):
            with self.subTest(changes=changes):
                payload = {'event_id': 'r1', 'summary': 'First line\nSecond line', 'refs': ['notes/a.md']}
                payload.update(changes)
                result = self.cli('receipt', payload=payload, ok=False)
                self.assertIn(b'event id collision', result.stderr)
        source.write_bytes(source.read_bytes().replace(b'First line', b'Changed line'))
        report = self.cli('sync', '--full')
        self.assertEqual(report['status'], 'conflict')
        self.assertEqual(report['conflicts'][0]['kind'], 'receipt_divergence')

    def test_legacy_trailing_cr_keeps_indexed_summary(self):
        first = self.receipt(summary='Trailing line')
        source = self.vault / first['source']
        source.write_bytes(source.read_bytes().replace(b'Trailing line\n', b'Trailing line\r\n'))
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            old = self.saved()
            old['summary'] = 'Trailing line\r'
            db.execute('UPDATE receipts SET payload=? WHERE id=?', (json.dumps(old), 'r1'))
            db.commit()
        for spelling in (b'\r\n', b'\n'):
            source.write_bytes(source.read_bytes().replace(b'\r\n', spelling))
            self.assert_clean(self.cli('sync', '--full'))
            self.receipt(summary='Trailing line\n')
        self.assertEqual(self.saved()['summary'], 'Trailing line\r')

    def test_original_legacy_trailing_cr_replays_after_state_reset(self):
        first = self.receipt(summary='Trailing line')
        source = self.vault / first['source']
        source.write_bytes(source.read_bytes().replace(b'Trailing line\n', b'Trailing line\r\n'))
        before = source.read_bytes()
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            db.execute('DELETE FROM receipts')
            db.execute("DELETE FROM metadata WHERE key='receipt_scan_signature'")
            db.commit()
        self.receipt(summary='Trailing line\r')
        self.assertEqual(self.saved()['summary'], 'Trailing line\r')
        self.assertEqual(source.read_bytes(), before)

    def test_newline_only_stale_daily_is_retired_on_layout_change(self):
        self.seed_legacy()
        old_daily = self.views()[0]
        old_daily.write_bytes(old_daily.read_bytes().replace(b'\r\n', b'\n'))
        self.cli('preferences', '--daily-layout', 'flat')
        self.assert_clean(self.cli('sync', '--full'))
        self.assertFalse(old_daily.exists())
        self.assertTrue((self.vault / 'daily' / old_daily.name).exists())

    def test_tracked_hash_not_links_decides_legacy_ownership(self):
        self.seed_legacy()
        before = {}
        for view in self.views():
            raw = view.read_bytes().replace(b'\r\n', b'\n').replace(b'First line', b'Altered line')
            view.write_bytes(raw)
            before[view] = raw
        self.receipt('r2', 'Second result')
        self.receipt('r3', 'Third result')
        for view, raw in before.items():
            self.assertEqual(view.read_bytes(), raw)

    def assert_racing_view_preserved(self, stale, newline_only=False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import _receipt_view_contents, project_receipts
        from beyin_v3_preferences import save_daily_layout
        self.seed_legacy()
        path = self.views()[0] if stale else self.vault / 'knowledge/v3/outcomes.md'
        before = path.read_bytes()
        if not newline_only:
            before = before.replace(b'\r\n', b'\n')
        path.write_bytes(before)
        inode = path.stat().st_ino
        concurrent = before.replace(b'\r\n', b'\n') if newline_only else before + b'Concurrent human edit\n'
        if stale:
            save_daily_layout(self.vault, 'flat')
        engine = SyncEngine(self.vault, self.state)
        # The engine canonicalizes its root; temp paths may still use /var
        # aliases on macOS or short directory names on Windows.
        target = path.resolve()
        read_bytes = Path.read_bytes
        reads = []
        injected = []

        def raced(candidate):
            raw = read_bytes(candidate)
            if candidate.resolve() == target:
                reads.append(raw)
                if len(reads) == 1:
                    candidate.write_bytes(concurrent)
                    injected.append(candidate)
            return raw

        with engine.store._connect() as db:
            if not stale and not newline_only:
                # An indexed new receipt requires a real replacement, even when
                # the old view already uses the canonical LF output spelling.
                event = self.saved()
                event.update(event_id='pending', summary='Pending result')
                db.execute('INSERT INTO receipts VALUES (?,?)', ('pending', json.dumps(event)))
            desired = _receipt_view_contents(engine, db)
            if not stale and not newline_only:
                self.assertIn('Pending result', desired[target.relative_to(engine.root).as_posix()])
            with patch.object(Path, 'read_bytes', raced):
                issues = project_receipts(engine, db, desired=desired)
            if newline_only:
                tracked = db.execute('SELECT hash FROM receipt_views WHERE path=?',
                                     ('knowledge/v3/outcomes.md',)).fetchone()[0]
                self.assertEqual(tracked, hashlib.sha256(before).hexdigest())
        self.assertEqual(len(injected), 1, 'the concurrent edit must actually be injected once')
        self.assertGreaterEqual(len(reads), 2, 'projection must reread the target before changing it')
        self.assertEqual(path.read_bytes(), concurrent)
        if newline_only:
            self.assertEqual(path.stat().st_ino, inode, 'a concurrent LF save must not be replaced')
        self.assertTrue(any(item['reason'] == 'receipt view changed during projection' for item in issues), issues)

    def test_newline_repair_rechecks_raw_bytes_before_replacement(self):
        self.assert_racing_view_preserved(stale=False)

    def test_newline_retirement_rechecks_raw_bytes_before_unlink(self):
        self.assert_racing_view_preserved(stale=True)

    def test_lf_migration_rechecks_even_concurrent_newline_only_save(self):
        self.assert_racing_view_preserved(stale=False, newline_only=True)

    def test_newline_repair_race_with_aliased_vault_path(self):
        # Exercise a spelling resolve() changes on every OS, without needing
        # symlink privileges (macOS /var and Windows short temp paths do too).
        self.vault = self.vault / '..' / self.vault.name
        self.assertNotEqual(self.vault, self.vault.resolve())
        self.assert_racing_view_preserved(stale=False)

    def test_newline_retirement_race_with_aliased_vault_path(self):
        self.vault = self.vault / '..' / self.vault.name
        self.assertNotEqual(self.vault, self.vault.resolve())
        self.assert_racing_view_preserved(stale=True)

    def test_flat_views_recover_newline_only_changes(self):
        self.cli('preferences', '--daily-layout', 'flat')
        self.seed_legacy()
        for view in self.views():
            view.write_bytes(view.read_bytes().replace(b'\r\n', b'\n'))
        self.receipt('r2', 'Flat result')
        for view in self.views():
            self.assertIn(b'Flat result', view.read_bytes())
        self.assert_clean(self.cli('sync', '--full'))


if __name__ == '__main__':
    unittest.main()
