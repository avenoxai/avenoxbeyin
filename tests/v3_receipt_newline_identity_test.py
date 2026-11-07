"""Installed receipt identities ignore line endings without losing edit protection (#602)."""
from contextlib import closing
import hashlib
import json
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

    def assert_racing_view_preserved(self, stale):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
        from beyin_v3_sync import SyncEngine
        from beyin_v3_projections import _receipt_view_contents, project_receipts
        from beyin_v3_preferences import save_daily_layout
        self.seed_legacy()
        path = self.views()[0] if stale else self.vault / 'knowledge/v3/outcomes.md'
        before = path.read_bytes().replace(b'\r\n', b'\n')
        path.write_bytes(before)
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
                    candidate.write_bytes(raw + b'Concurrent human edit\n')
                    injected.append(candidate)
            return raw

        with engine.store._connect() as db:
            desired = _receipt_view_contents(engine, db)
            with patch.object(Path, 'read_bytes', raced):
                issues = project_receipts(engine, db, desired=desired)
        self.assertEqual(len(injected), 1, 'the concurrent edit must actually be injected once')
        self.assertGreaterEqual(len(reads), 2, 'projection must reread the target before changing it')
        self.assertEqual(path.read_bytes(), before + b'Concurrent human edit\n')
        self.assertTrue(any(item['reason'] == 'receipt view changed during projection' for item in issues), issues)

    def test_newline_repair_rechecks_raw_bytes_before_replacement(self):
        self.assert_racing_view_preserved(stale=False)

    def test_newline_retirement_rechecks_raw_bytes_before_unlink(self):
        self.assert_racing_view_preserved(stale=True)

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

    def test_released_reader_trailing_cr_replays_as_same_receipt(self):
        # V3.12 read a CRLF checkout (core.autocrlf) by dropping only the final LF.
        first = self.receipt(summary='İlk satır\nİkinci satır')
        source = self.vault / first['source']
        source.write_bytes(source.read_bytes().replace(b'\n', b'\r\n'))
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            old = self.saved()
            old['summary'] = 'İlk satır\r\nİkinci satır\r'
            db.execute('UPDATE receipts SET payload=? WHERE id=?', (json.dumps(old), 'r1'))
            db.commit()
        indexed, before = self.saved(), source.read_bytes()
        self.receipt(summary='İlk satır\nİkinci satır', harness='codex')
        self.receipt(summary='İlk satır\r\nİkinci satır\r\n')
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.saved(), indexed)
        for summary in ('İlk satır\nİkinci satır.', 'İlk satır\nİkinci satır\n\n', 'İlk satır\nİkinci satır '):
            with self.subTest(summary=summary):
                result = self.cli('receipt', payload={'event_id': 'r1', 'summary': summary, 'refs': ['notes/a.md']}, ok=False)
                self.assertIn(b'event id collision', result.stderr)
        self.assert_clean(self.cli('sync', '--full'))

    WITHHELD = b'[summary withheld: the receipt refers to a private or untrusted source]'

    def seed_released(self, ref='notes/a.md'):
        """Recreate a V3.12.0 view: hash-named links, an unwithheld CRLF summary, LF wrapper."""
        legacy = b'First line\r\nSecond line'
        first = self.cli('receipt', '--harness', 'claude', payload={
            'event_id': 'r1', 'summary': 'First line\nSecond line', 'refs': [ref]})
        source = self.vault / first['source']
        link = first['source'].encode()
        source.write_bytes(source.read_bytes().replace(b'First line\nSecond line', legacy))
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            old = self.saved()
            old['summary'] = legacy.decode()
            db.execute('UPDATE receipts SET payload=? WHERE id=?', (json.dumps(old), 'r1'))
            for path in self.views():
                raw = path.read_bytes().replace(b'[[' + link + b'|r1]]', b'[[' + link + b']]')
                raw = raw.replace(self.WITHHELD, legacy).replace(b'First line\nSecond line', legacy)
                self.assertNotIn(b'|r1]]', raw)
                self.assertIn(b'[[' + link + b']]', raw)
                self.assertIn(legacy, raw)
                path.write_bytes(raw)
                relative = path.relative_to(self.vault).as_posix()
                table = 'receipt_flat_views' if relative.startswith('daily/') and not relative.startswith('daily/v3/') else 'receipt_views'
                db.execute(f'UPDATE {table} SET hash=? WHERE path=?', (hashlib.sha256(raw).hexdigest(), relative))
            db.commit()

    def test_released_engine_view_recovers_after_newline_drift(self):
        # A view stuck under V3.12.0 is proven in that release's format, then rewritten.
        self.seed_released()
        for view in self.views():
            view.write_bytes(view.read_bytes().replace(b'\r\n', b'\n'))
        second = self.receipt('r2', 'New recorded result')
        self.assertNotIn('source_sync', second)
        for view in self.views():
            raw = view.read_bytes()
            self.assertIn(b'New recorded result', raw)
            self.assertIn(b'|r1]]', raw)
        self.assert_clean(self.cli('sync', '--full'))

    def test_released_engine_view_with_real_edit_stays_preserved(self):
        self.seed_released()
        before = {}
        for view in self.views():
            raw = view.read_bytes().replace(b'\r\n', b'\n').replace(b'Second line', b'Edited line')
            view.write_bytes(raw)
            before[view] = raw
        second = self.receipt('r2', 'New recorded result')
        self.assertEqual(len(second['source_sync']['warnings']), 2)
        for view, raw in before.items():
            self.assertEqual(view.read_bytes(), raw)

    def test_released_engine_view_drops_private_summary_on_recovery(self):
        (self.vault / 'notes/gizli.md').write_bytes(b'---\nvisibility: private\n---\nGizli.\n')
        self.cli('sync')
        self.seed_released('notes/gizli.md')
        for view in self.views():
            view.write_bytes(view.read_bytes().replace(b'\r\n', b'\n'))
        self.receipt('r2', 'New recorded result')
        for view in self.views():
            raw = view.read_bytes()
            self.assertIn(b'New recorded result', raw)
            self.assertIn(self.WITHHELD, raw)
            self.assertNotIn(b'Second line', raw)


if __name__ == '__main__':
    unittest.main()
