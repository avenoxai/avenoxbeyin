"""Archive retry/delta regressions; synthetic vaults and independent state only."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_compact as compact
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def oversized_threads():
    return ('# Threads\n\n## Active Threads\n'
            '### Thread: DEGİSMEYEN_KONU\n' + 'Sentetik sabit açıklama. ' * 200 + '\n\n'
            '### Thread: DEGİSEN_KONU\n' + 'Sentetik başka açıklama. ' * 200 + '\n')


def movable_handoff():
    return ('# Handoff\n\n## 2026-10-01 09:00\nOLD_PAYLOAD_UNIQUE\n'
            '**Karar:** Yalnız yerel çalış.\n' + 'Sentetik eski açıklama. ' * 180 + '\n\n'
            '## 2026-10-26 09:00\nNEW_PAYLOAD_UNIQUE\n')


NEXT = datetime(2026, 11, 1, tzinfo=timezone.utc)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='compact-retention-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.target = self.vault / '850-Companion'
        self.target.mkdir(parents=True)

    def write(self, name, text):
        p = self.target / name
        p.write_bytes(text.encode('utf-8'))
        return p

    def run_compact(self, now=NOW):
        return compact.compact(self.vault, self.state, now=now)

    def archive(self, name='Threads', month='2026-10'):
        return self.target / 'Arşiv' / f'{name}-{month}.md'

    def history(self, name='Threads.md'):
        return compact._archive_history(self.target, name, self.vault)

    def test_crash_retry_across_months_and_state_loss(self):
        live = self.write('Last-Session.md', movable_handoff())
        raw = live.read_bytes()
        original = compact._write
        def fail(path, text):
            if Path(path) == live:
                raise OSError('interruption')
            original(path, text)
        with patch.object(compact, '_write', fail), self.assertRaises(OSError):
            self.run_compact()
        self.assertEqual(live.read_bytes(), raw)
        before = self.archive('Last-Session').read_bytes()
        self.assertEqual(before.count(b'OLD_PAYLOAD_UNIQUE'), 1)
        self.state = self.root / 'fresh-state'
        self.state.mkdir()
        self.assertEqual(self.run_compact(NEXT)['status'], 'compacted')
        self.assertEqual(self.archive('Last-Session').read_bytes(), before)
        self.assertFalse(self.archive('Last-Session', '2026-11').exists())
        self.assertIn('Last-Session-2026-10.md', live.read_text(encoding='utf-8'))
        self.assertNotIn('Last-Session-2026-11.md', live.read_text(encoding='utf-8'))
        self.assertNotIn(b'OLD_PAYLOAD_UNIQUE', live.read_bytes())
        self.assertEqual((self.target / 'Kararlar.md').read_text(encoding='utf-8').count('**Karar:** Yalnız yerel çalış.'), 1)

    def test_process_exit_after_archive_write(self):
        self.write('Last-Session.md', movable_handoff())
        code = '''import sys, os
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import beyin_v3_compact as c
write = c._write
def stop(path, text):
    write(path, text)
    if Path(path).parent.name == 'Arşiv': os._exit(23)
c._write = stop
c.compact(Path(sys.argv[2]), Path(sys.argv[3]), now=datetime(2026,10,26,tzinfo=timezone.utc))
'''
        p = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(SCRIPTS), str(self.vault), str(self.state)], env=inherited_env(), timeout=30)
        self.assertEqual(p.returncode, 23)
        self.assertEqual(self.run_compact(NEXT)['status'], 'compacted')
        self.assertEqual(self.archive('Last-Session').read_bytes().count(b'OLD_PAYLOAD_UNIQUE'), 1)
        self.assertTrue((self.vault / '.beyin-note-write/writer.lock').exists())
        ledger = (self.target / 'Kararlar.md').read_text(encoding='utf-8')
        self.assertIn('Last-Session-2026-10.md', ledger)
        self.assertNotIn('Last-Session-2026-11.md', ledger)

    def test_retry_after_unrelated_live_edit_does_not_duplicate_move(self):
        live = self.write('Last-Session.md', movable_handoff())
        original = compact._write
        def fail(path, text):
            if Path(path) == live: raise OSError('stop')
            original(path, text)
        with patch.object(compact, '_write', fail), self.assertRaises(OSError):
            self.run_compact()
        live.write_bytes(live.read_bytes() + b'Unrelated new live status\n')
        self.run_compact()
        self.assertEqual(self.archive('Last-Session').read_bytes().count(b'OLD_PAYLOAD_UNIQUE'), 1)
        self.assertIn(b'Unrelated new live status', live.read_bytes())

    def test_decision_ledger_failure_retry(self):
        live = self.write('Last-Session.md', movable_handoff())
        raw = live.read_bytes()
        with patch.object(compact, '_record_decisions', side_effect=OSError('ledger crash')), self.assertRaises(OSError):
            self.run_compact()
        self.assertEqual(live.read_bytes(), raw)
        self.run_compact()
        self.assertEqual(self.archive('Last-Session').read_bytes().count(b'OLD_PAYLOAD_UNIQUE'), 1)

    def test_same_card_heading_with_changed_moved_content_is_new_data(self):
        text = movable_handoff()
        for version in [text, text.replace('OLD_PAYLOAD_UNIQUE', 'CHANGED_PAYLOAD_UNIQUE')]:
            self.write('Last-Session.md', version)
            self.run_compact()
        stored = self.archive('Last-Session').read_bytes()
        self.assertEqual(stored.count(b'OLD_PAYLOAD_UNIQUE'), 1)
        self.assertEqual(stored.count(b'CHANGED_PAYLOAD_UNIQUE'), 1)

    def test_retry_reusing_archive_preserves_external_live_edit(self):
        live = self.write('Last-Session.md', movable_handoff())
        original_write = compact._write
        def fail(path, text):
            if Path(path) == live: raise OSError('stop')
            original_write(path, text)
        with patch.object(compact, '_write', fail), self.assertRaises(OSError):
            self.run_compact()
        before = self.archive('Last-Session').read_bytes()
        original_history = compact._archive_history
        def change(*args):
            result = original_history(*args)
            live.write_bytes(live.read_bytes() + b'EXTERNAL_EDIT\n')
            return result
        with patch.object(compact, '_archive_history', change):
            self.assertEqual(self.run_compact()['status'], 'conflict')
        self.assertIn(b'EXTERNAL_EDIT', live.read_bytes())
        self.assertIn(b'OLD_PAYLOAD_UNIQUE', live.read_bytes())
        self.assertEqual(self.archive('Last-Session').read_bytes(), before)

    def test_recovery_utility_restores_delta_bytes(self):
        base = oversized_threads()
        version = base + 'Kurtarılacak 🧠 değişiklik\r\n'
        for text in [base, version]:
            self.write('Threads.md', text)
            self.run_compact()
        # The synthetic vault uses precisely the same engine as the copy.
        (self.vault / '.claude/scripts').mkdir(parents=True)
        import shutil
        for source in SCRIPTS.glob('*.py'):
            shutil.copy2(source, self.vault / '.claude/scripts' / source.name)
        recovered = self.root / 'recovered.md'
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/restore_v3_compact_backup.py'),
                                 '--vault', str(self.vault), '--name', 'Threads.md', '--sha256', compact._hash(version),
                                 '--output', str(recovered)], capture_output=True, env=inherited_env(), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(recovered.read_bytes(), version.encode('utf-8'))

    def test_small_changes_keep_every_version_and_one_unchanged_heading(self):
        base = oversized_threads()
        versions = []
        for i in range(4):
            version = base + f'Ek sentetik durum: {i}\n'
            versions.append(version)
            self.write('Threads.md', version)
            self.assertEqual(self.run_compact()['status'], 'needs_rewrite')
        archived = self.archive().read_bytes()
        self.assertEqual(archived.count('### Thread: DEGİSMEYEN_KONU'.encode()), 1)
        snapshots = self.history()[1]
        self.assertEqual(set(snapshots.values()), set(versions))
        self.assertLess(len(archived), 16000)

    def test_unchanged_across_months_writes_nothing(self):
        self.write('Threads.md', oversized_threads())
        self.run_compact()
        before = self.archive().read_bytes()
        for now in [NOW, NOW, NEXT]:
            self.assertEqual(self.run_compact(now)['files']['Threads.md']['backup'], 'exists')
        self.assertEqual(self.archive().read_bytes(), before)
        self.assertFalse(self.archive(month='2026-11').exists())

    def test_changed_across_months_and_clock_rollback(self):
        text = oversized_threads()
        versions = [text, text + 'Kasım değişikliği\n', text + 'Ekim saat geri alma\n']
        for now, version in zip([NOW, NEXT, NOW], versions):
            self.write('Threads.md', version)
            self.run_compact(now)
        self.assertEqual(set(self.history()[1].values()), set(versions))
        before = {p.name: p.read_bytes() for p in self.archive().parent.iterdir()}
        self.assertEqual(self.run_compact(NEXT)['files']['Threads.md']['backup'], 'exists')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.archive().parent.iterdir()})

    def test_same_heading_changed_body_is_preserved(self):
        text = oversized_threads()
        changed = text.replace('Sentetik başka açıklama.', 'Yeni gerçek içerik.', 1)
        for version in [text, changed]:
            self.write('Threads.md', version)
            self.run_compact()
        self.assertEqual(set(self.history()[1].values()), {text, changed})

    def test_unicode_crlf_no_final_newline_duplicate_lines_and_deletion(self):
        text = '# Threads\r\n' + ('aynı 🧠 satır\r\n' * 700) + 'son'
        versions = [text, text.replace('aynı 🧠 satır\r\n', '', 1) + ' yeni', text + '\r\nEK']
        for version in versions:
            live = self.write('Threads.md', version)
            self.run_compact()
            self.assertEqual(live.read_bytes(), version.encode())
        self.assertEqual(set(self.history()[1].values()), set(versions))

    def test_legacy_archive_retained_byte_for_byte_prefix(self):
        self.archive().parent.mkdir()
        legacy = b'# Old legacy archive\nDO_NOT_REMOVE\n'
        self.archive().write_bytes(legacy)
        self.write('Threads.md', oversized_threads())
        self.run_compact()
        self.assertTrue(self.archive().read_bytes().startswith(legacy))
        before = self.archive().read_bytes()
        self.run_compact()
        self.assertEqual(self.archive().read_bytes(), before)

    def test_corrupt_backup_not_used_for_deduplication(self):
        text = oversized_threads()
        self.write('Threads.md', text)
        self.run_compact()
        # Simulated outside corruption; compactor must never erase this archive.
        broken = self.archive().read_bytes().replace(b'Sentetik', b'BROKENXX', 1)
        self.archive().write_bytes(broken)
        self.assertEqual(self.run_compact()['files']['Threads.md']['backup'], 'written')
        self.assertTrue(self.archive().read_bytes().startswith(broken))
        self.assertIn(text, self.history()[1].values())

    def test_corrupt_move_marker_does_not_authorize_live_removal(self):
        live = self.write('Last-Session.md', movable_handoff())
        original = compact._write
        def fail(path, text):
            if Path(path) == live: raise OSError('stop')
            original(path, text)
        with patch.object(compact, '_write', fail), self.assertRaises(OSError): self.run_compact()
        archive = self.archive('Last-Session')
        broken = archive.read_bytes().replace(b'OLD_PAYLOAD_UNIQUE', b'BAD_PAYLOAD_UNIQUE')
        archive.write_bytes(broken)
        self.run_compact()
        self.assertTrue(archive.read_bytes().startswith(broken))
        self.assertEqual(archive.read_bytes().count(b'OLD_PAYLOAD_UNIQUE'), 1)

    def test_marker_text_inside_payload_is_not_a_commit(self):
        source = '850-Companion/Threads.md'
        fake = compact._record('full', source, compact._hash('fake'), 'fake', '\n')
        text = oversized_threads() + fake
        self.write('Threads.md', text)
        self.run_compact()
        self.assertEqual(list(self.history()[1].values()), [text])

    def test_dry_run_no_archive_and_no_state_writes(self):
        self.write('Threads.md', oversized_threads())
        result = compact.compact(self.vault, self.state, dry_run=True, now=NOW)
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(self.archive().exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_legacy_previous_month_is_reused_but_never_a_delta_base(self):
        text = oversized_threads()
        self.write('Threads.md', text)
        self.archive().parent.mkdir()
        legacy = ('# Legacy archive\n\n## 2026-10-26 12:00 UTC, Threads.md, '
                  + compact.BACKUP.format(len(text)) + '\n\n' + text).encode('utf-8')
        self.archive().write_bytes(legacy)
        report = self.run_compact(NEXT)['files']['Threads.md']
        self.assertEqual(report['backup'], 'exists')
        self.assertEqual(report['archive'], '850-Companion/Arşiv/Threads-2026-10.md')
        self.assertFalse(self.archive(month='2026-11').exists())
        changed = text + 'New synthetic update\n'
        self.write('Threads.md', changed)
        report = self.run_compact(NEXT)['files']['Threads.md']
        self.assertEqual(report['backup_format'], 'full')
        self.assertEqual(self.archive().read_bytes(), legacy)
        self.assertEqual(set(self.history()[1].values()), {changed})

    def test_move_retry_is_not_limited_by_snapshot_cache_window_or_budget(self):
        live = self.write('Last-Session.md', movable_handoff())
        original = compact._write

        def fail(path, text):
            if Path(path) == live:
                raise OSError('interruption')
            original(path, text)

        with patch.object(compact, '_write', fail), self.assertRaises(OSError):
            self.run_compact()
        stored = self.archive('Last-Session').read_bytes()
        with patch.object(compact, 'BACKUP_LOOKBACK_BYTES', 1):
            self.assertEqual(self.run_compact(datetime(2028, 1, 1, tzinfo=timezone.utc))['status'], 'compacted')
        self.assertEqual(self.archive('Last-Session').read_bytes(), stored)
        self.assertFalse(self.archive('Last-Session', '2028-01').exists())
        self.assertIn(b'Last-Session-2026-10.md', live.read_bytes())

    def test_delta_with_missing_base_falls_back_to_complete_backup(self):
        text = oversized_threads()
        self.write('Threads.md', text)
        self.run_compact()
        version = text + 'New synthetic update\n'
        self.write('Threads.md', version)
        self.assertEqual(self.run_compact(NEXT)['files']['Threads.md']['backup_format'], 'delta')
        # Simulate external base damage, without changing the dependent delta.
        damaged = self.archive().read_bytes().replace(b'Sentetik', b'BROKENXX', 1)
        self.archive().write_bytes(damaged)
        delta = self.archive(month='2026-11').read_bytes()
        self.assertNotIn(version, self.history()[1].values())
        self.assertEqual(self.run_compact(NEXT)['files']['Threads.md']['backup_format'], 'full')
        self.assertTrue(self.archive(month='2026-11').read_bytes().startswith(delta))
        self.assertEqual(self.archive().read_bytes(), damaged)
        self.assertIn(version, self.history()[1].values())

    def test_bad_delta_ranges_hash_and_metadata_never_suppress_backup(self):
        text = oversized_threads()
        self.write('Threads.md', text)
        self.run_compact()
        source = '850-Companion/Threads.md'
        for number, parts in enumerate(([[0, len(text) + 1]], [[True, 1]], [text + 'wrong'])):
            version = text + f'New update {number}\n'
            self.write('Threads.md', version)
            bad = compact._record('delta', source, compact._hash(version),
                                  json.dumps(dict(base=compact._hash(text), parts=parts)), '\n')
            with self.archive().open('ab') as archive:
                archive.write(bad.encode('utf-8'))
            prefix = self.archive().read_bytes()
            self.assertNotIn(version, self.history()[1].values())
            self.assertEqual(self.run_compact()['files']['Threads.md']['backup'], 'written')
            self.assertTrue(self.archive().read_bytes().startswith(prefix))
            self.assertIn(version, self.history()[1].values())

    def test_delta_dependency_resolves_independently_of_month_order(self):
        text = oversized_threads()
        version = text + 'New synthetic update\n'
        source = '850-Companion/Threads.md'
        self.archive().parent.mkdir()
        base = compact._record('full', source, compact._hash(text), text, '\n')
        kind, delta = compact._backup_record(version, source, {compact._hash(text): text}, '\n')
        self.assertEqual(kind, 'delta')
        self.archive().write_bytes(delta.encode('utf-8'))
        self.archive(month='2026-11').write_bytes(base.encode('utf-8'))
        self.assertEqual(set(self.history()[1].values()), {text, version})

    def test_unreadable_old_archive_is_cache_miss_not_a_compaction_failure(self):
        text = oversized_threads()
        self.write('Threads.md', text)
        self.archive().parent.mkdir()
        self.archive().write_bytes(b'\xff')
        report = self.run_compact(NEXT)['files']['Threads.md']
        self.assertEqual(report['status'], 'needs_rewrite')
        self.assertEqual(report['backup_format'], 'full')
        self.assertEqual(self.archive().read_bytes(), b'\xff')
        self.assertIn(text, self.history()[1].values())

    def test_legacy_backup_heading_inside_record_payload_is_not_a_snapshot(self):
        text = oversized_threads()
        source = '850-Companion/Threads.md'
        payload = compact.BACKUP.format(len(text)) + '\n\n' + text
        self.archive().parent.mkdir()
        # The outer move is verified; its content is not a standalone full backup.
        self.archive().write_bytes(compact._record('move', source, 'synthetic', payload, '\n').encode())
        self.write('Threads.md', text)
        self.assertEqual(self.run_compact(NEXT)['files']['Threads.md']['backup_format'], 'full')


if __name__ == '__main__':
    unittest.main()
