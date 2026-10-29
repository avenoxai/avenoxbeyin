"""Unchanged needs_rewrite snapshots remain recoverable across monthly rotation.

Only synthetic files are used. These tests call the production command's compact
implementation with real files; no transcript or model call is involved.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3_compact as compact_module  # noqa: E402

OCTOBER = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
NOVEMBER = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
BODY = '# Threads\n\n## Active Threads\n### Synthetic topic\n' + 'Unresolved synthetic detail.\n' * 320


class ArchiveReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='compact-month-reuse-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.target = self.vault / '850-Companion'
        self.target.mkdir(parents=True)
        self.state = Path(self.tmp.name) / 'state'
        self.live = self.target / 'Threads.md'
        self.live.write_bytes(BODY.encode('utf-8'))
        self.archives = self.target / 'Arşiv'
        self.october = self.archives / 'Threads-2026-10.md'
        self.november = self.archives / 'Threads-2026-11.md'

    def compact(self, now=OCTOBER, **kwargs):
        result = compact_module.compact(self.vault, self.state, now=now, **kwargs)
        self.assertEqual(result['status'], 'needs_rewrite' if not kwargs.get('dry_run') else 'dry_run')
        return result['files']['Threads.md']

    def test_unchanged_snapshot_is_reused_across_months_without_a_new_archive(self):
        for newline, bom in (('\n', ''), ('\r\n', ''), ('\n', '\ufeff'), ('\r\n', '\ufeff')):
            with self.subTest(newline=repr(newline), bom=bool(bom)):
                for old in self.archives.glob('*.md'):
                    old.unlink()
                raw = (bom + BODY.replace('\n', newline)).encode('utf-8')
                self.live.write_bytes(raw)
                first = self.compact()
                self.assertEqual(first['backup'], 'written')
                stored = self.october.read_bytes()
                for _ in range(20):
                    repeated = self.compact(NOVEMBER)
                    self.assertEqual(repeated['backup'], 'exists')
                    self.assertEqual(repeated['archive'], '850-Companion/Arşiv/Threads-2026-10.md')
                self.assertEqual(self.october.read_bytes(), stored)
                self.assertEqual(self.live.read_bytes(), raw)
                self.assertFalse(self.november.exists())

    def test_changed_text_of_equal_length_gets_a_new_snapshot(self):
        self.compact()
        original = self.october.read_bytes()
        changed = BODY.replace('Unresolved', 'New detail', 1)
        self.assertEqual(len(changed), len(BODY))
        self.live.write_bytes(changed.encode('utf-8'))
        self.assertEqual(self.compact(NOVEMBER)['backup'], 'written')
        self.assertIn(changed, compact_module._archive_history(self.target, 'Threads.md', self.vault)[1].values())
        self.assertEqual(self.october.read_bytes(), original)
        stored = self.november.read_bytes()
        self.assertEqual(self.compact(NOVEMBER)['backup'], 'exists')
        self.assertEqual(self.november.read_bytes(), stored)

    def test_current_month_archive_is_unchanged_when_an_older_backup_is_reused(self):
        self.compact()
        existing = b'# Unrelated synthetic moved history\n'
        self.november.write_bytes(existing)
        report = self.compact(NOVEMBER)
        self.assertEqual(report['backup'], 'exists')
        self.assertEqual(report['archive'], '850-Companion/Arşiv/Threads-2026-10.md')
        self.assertEqual(self.november.read_bytes(), existing)

    def test_edit_during_prior_archive_scan_is_a_conflict_not_a_reused_backup(self):
        self.compact()
        stored = self.october.read_bytes()
        original = compact_module._previous_backup

        def scan_then_edit(*args):
            found = original(*args)
            with self.live.open('ab') as handle:
                handle.write(b'CONCURRENT_EDIT\n')
            return found

        with mock.patch.object(compact_module, '_previous_backup', scan_then_edit):
            result = compact_module.compact(self.vault, self.state, now=NOVEMBER)
        report = result['files']['Threads.md']
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(report['status'], 'conflict')
        self.assertNotIn('backup', report)
        edited = self.live.read_bytes()
        self.assertTrue(edited.endswith(b'CONCURRENT_EDIT\n'))
        self.assertEqual(self.october.read_bytes(), stored)
        self.assertFalse(self.november.exists())
        retry = self.compact(NOVEMBER)
        self.assertEqual(retry['backup'], 'written')
        self.assertIn(edited.decode('utf-8'),
                      compact_module._archive_history(self.target, 'Threads.md', self.vault)[1].values())

    def test_missing_corrupt_or_changed_prior_archive_never_suppresses_a_backup(self):
        self.compact()
        saved = self.october.read_bytes()
        for broken in (None, b'\xff', saved.replace(b'Unresolved', b'New detail', 1)):
            with self.subTest(broken=None if broken is None else len(broken)):
                self.november.unlink(missing_ok=True)
                if broken is None:
                    self.october.unlink()
                else:
                    self.october.write_bytes(broken)
                report = self.compact(NOVEMBER)
                self.assertEqual(report['backup'], 'written')
                self.assertIn(BODY.encode('utf-8'), self.november.read_bytes())

    def test_symlinked_prior_archive_is_not_reused(self):
        self.compact()
        external = Path(self.tmp.name) / 'external.md'
        saved = self.october.read_bytes()
        external.write_bytes(saved)
        self.october.unlink()
        try:
            self.october.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        self.assertEqual(self.compact(NOVEMBER)['backup'], 'written')
        self.assertEqual(external.read_bytes(), saved)
        self.assertIn(BODY.encode('utf-8'), self.november.read_bytes())

    def test_lookback_and_byte_budget_fall_back_to_a_complete_new_backup(self):
        self.compact()
        previous = self.october.read_bytes()
        with mock.patch.object(compact_module, 'BACKUP_LOOKBACK_BYTES', len(previous) - 1):
            self.assertEqual(self.compact(NOVEMBER)['backup'], 'written')
        self.november.unlink()
        outside_window = datetime(2027, 11, 20, tzinfo=timezone.utc)
        self.assertEqual(self.compact(outside_window)['backup'], 'written')
        self.assertIn(BODY.encode('utf-8'), (self.archives / 'Threads-2027-11.md').read_bytes())

    def test_dry_run_across_months_does_not_change_live_or_archive_files(self):
        self.compact()
        before = {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        self.assertEqual(self.compact(NOVEMBER, dry_run=True)['backup'], 'planned')
        after = {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
