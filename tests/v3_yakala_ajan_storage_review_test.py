"""Maintainer storage regressions: atomic publication, real stores, bounded memory lookup.

Native no-replace rename and cross-process POSIX locks run on the host filesystem.
Forcing os.link to ENOTSUP simulates a hard-link-less volume; it is not an exFAT mount.
Windows/macOS API stand-ins only verify dispatch/flags on other operating systems.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import uuid


SCRIPTS = Path(__file__).resolve().parents[1] / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala


class StorageFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-storage-review-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.vault = self.base / 'Ornek Beyin'
        self.vault.mkdir()
        self.state, self.local_state = self.base / 'selected-state', self.base / 'default-state'
        default = patch.object(yakala, 'default_state', return_value=self.local_state)
        default.start()
        self.addCleanup(default.stop)

    def note(self, relative, body='Ordinary text'):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8', newline='')
        return path

    def job(self, url='https://example.com/source'):
        result = yakala.capture(self.vault, url=url, state=self.state)
        return {'id': str(uuid.uuid4()), 'card_id': result['id'], 'card_path': result['path'],
                'state': 'running', 'client': 'claude', 'url': url,
                'config': dict(yakala._agent_defaults(self.vault), client='claude',
                               downloads_dir=str(self.base / 'Downloads'))}


class PublicationFallbackTest(StorageFiles):
    def no_links(self):
        return patch.object(yakala.os, 'link', side_effect=OSError(errno.ENOTSUP, 'No hard links'))

    def test_native_fallback_publishes_exact_durable_bytes_without_temporary_alias(self):
        target = self.vault / 'tam-sonuc.json'
        data = ('{"sonuc":"\u00fc\u011f\u0131"}\n' * 1000).encode('utf-8')
        original = yakala.os.fsync
        visible = []

        def observe(fd):
            visible.append(target.exists())
            return original(fd)

        with self.no_links(), patch.object(yakala.os, 'fsync', side_effect=observe):
            yakala._agent_create_bytes(target, data)
        self.assertFalse(visible[0])
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(list(self.vault.glob('.ajan-new-*')), [])

    def test_existing_file_is_not_replaced_when_hard_links_are_unsupported(self):
        target = self.note('existing.md', 'USER BYTES')
        with self.no_links(), self.assertRaises(FileExistsError):
            yakala._agent_create_bytes(target, b'agent bytes')
        self.assertEqual(target.read_bytes(), b'USER BYTES')
        self.assertEqual(list(self.vault.glob('.ajan-new-*')), [])

    def test_destination_created_during_fallback_is_not_replaced(self):
        target = self.vault / 'racing.md'
        rename = yakala._agent_rename_noreplace

        def competing_writer(source, destination):
            with destination.open('xb') as output:
                output.write(b'OTHER WRITER')
            return rename(source, destination)

        with self.no_links(), patch.object(yakala, '_agent_rename_noreplace', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                yakala._agent_create_bytes(target, b'agent result')
        self.assertEqual(target.read_bytes(), b'OTHER WRITER')

    def test_existing_directory_is_not_replaced(self):
        target = self.vault / 'existing'
        target.mkdir()
        (target / 'keep.txt').write_bytes(b'KEEP')
        with self.no_links(), self.assertRaises(OSError):
            yakala._agent_create_bytes(target, b'agent result')
        self.assertEqual((target / 'keep.txt').read_bytes(), b'KEEP')

    def test_symlink_target_and_linked_parent_are_refused(self):
        outside = self.base / 'outside'
        outside.mkdir()
        target = outside / 'keep.md'
        target.write_bytes(b'OUTSIDE')
        try:
            (self.vault / 'link.md').symlink_to(target)
            (self.vault / 'linked-dir').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        with self.no_links():
            for path in (self.vault / 'link.md', self.vault / 'linked-dir/new.md'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    yakala._agent_create_bytes(path, b'agent result')
        self.assertEqual(target.read_bytes(), b'OUTSIDE')
        self.assertFalse((outside / 'new.md').exists())

    def test_failed_content_fsync_does_not_publish_or_leave_plaintext_stage(self):
        target = self.vault / 'result.json'
        with patch.object(yakala.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'Disk full')):
            with self.assertRaises(OSError):
                yakala._agent_create_bytes(target, b'partial result')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.vault.glob('.ajan-new-*')), [])

    def test_both_publication_primitives_unsupported_fail_without_partial_destination(self):
        target = self.vault / 'result.json'
        with self.no_links(), patch.object(yakala, '_agent_rename_noreplace',
                                           side_effect=OSError(errno.ENOTSUP, 'No exclusive rename')):
            with self.assertRaises(OSError):
                yakala._agent_create_bytes(target, b'complete stage')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.vault.glob('.ajan-new-*')), [])

    def test_permission_failure_is_not_treated_as_filesystem_capability_failure(self):
        with patch.object(yakala.os, 'link', side_effect=PermissionError(errno.EACCES, 'Denied')), \
                patch.object(yakala, '_agent_rename_noreplace') as rename:
            with self.assertRaises(PermissionError):
                yakala._agent_create_bytes(self.vault / 'result.json', b'data')
        rename.assert_not_called()

    def test_changed_stage_is_not_published_or_removed_as_our_private_file(self):
        target = self.vault / 'result.json'
        replaced = []

        def swap(source, destination, **options):
            source.rename(source.with_suffix('.owned'))  # Retain the inode to preclude reuse.
            source.write_bytes(b'REPLACEMENT OWNED BY ANOTHER WRITER')
            replaced.append(source)
            raise OSError(errno.ENOTSUP, 'No links')

        with patch.object(yakala.os, 'link', side_effect=swap):
            with self.assertRaisesRegex(ValueError, 'Gecici yayin dosyasi degisti'):
                yakala._agent_create_bytes(target, b'original result')
        self.assertFalse(target.exists())
        self.assertEqual(replaced[0].read_bytes(), b'REPLACEMENT OWNED BY ANOTHER WRITER')

    def test_linux_and_macos_exclusive_flags_and_errno_are_used(self):
        source, target = self.vault / 'source', self.vault / 'target'
        for platform, symbol, expected in (
                ('linux', 'renameat2', (-100, os.fsencode(source), -100, os.fsencode(target), 1)),
                ('darwin', 'renamex_np', (os.fsencode(source), os.fsencode(target), 4))):
            call = Mock(return_value=0)
            library = types.SimpleNamespace(**{symbol: call})
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform), \
                    patch.object(ctypes, 'CDLL', return_value=library):
                yakala._agent_rename_noreplace(source, target)
                call.assert_called_once_with(*expected)
                call.return_value = -1
                with patch.object(ctypes, 'get_errno', return_value=errno.EEXIST):
                    with self.assertRaises(FileExistsError):
                        yakala._agent_rename_noreplace(source, target)

    def test_windows_uses_rename_without_replace_or_cross_volume_copy_flags(self):
        source, target = self.vault / 'source', self.vault / 'target'
        native = Mock()
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(yakala, 'os', types.SimpleNamespace(name='nt', rename=native)):
            yakala._agent_rename_noreplace(source, target)
        native.assert_called_once_with(source, target)

    def test_missing_native_symbol_does_not_fall_back_to_overwriting_rename(self):
        with patch.object(sys, 'platform', 'linux'), patch.object(ctypes, 'CDLL', return_value=object()), \
                patch.object(yakala.os, 'replace') as replace, patch.object(yakala.os, 'rename') as rename:
            with self.assertRaises(OSError):
                yakala._agent_rename_noreplace(self.vault / 'source', self.vault / 'target')
        replace.assert_not_called()
        rename.assert_not_called()

    def test_two_real_processes_publish_one_complete_winner(self):
        target = self.vault / 'winner.md'
        program = '''
import errno, pathlib, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as yakala
print('ready', flush=True)
sys.stdin.readline()
try:
    with patch.object(yakala.os, 'link', side_effect=OSError(errno.ENOTSUP, 'No hard links')):
        yakala._agent_create_bytes(pathlib.Path(sys.argv[2]), sys.argv[3].encode() * 200000)
except FileExistsError:
    print('conflict', flush=True)
else:
    print('published', flush=True)
'''
        processes = [subprocess.Popen([sys.executable, '-c', program, str(SCRIPTS), str(target), value],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) for value in ('A', 'B')]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
            for process in processes:
                process.stdin.write('go\n')
                process.stdin.flush()
            results = []
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                results.append(output.strip())
            self.assertCountEqual(results, ['published', 'conflict'])
            self.assertIn(target.read_bytes(), (b'A' * 200000, b'B' * 200000))
            self.assertEqual(list(self.vault.glob('.ajan-new-*')), [])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def prepared_download(self):
        job = self.job()
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        (folder / 'downloads').mkdir(parents=True)
        data = b'ID3\x04\x00\x00\x00\x00\x00\x00Verified media'
        (folder / 'downloads/track.mp3').write_bytes(data)
        item = {'filename': 'track.mp3', 'staging_path': 'track.mp3', 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'source_url': job['url']}
        yakala._agent_json(folder / 'download-manifests/track.mp3.json', item)
        return job, item, data

    def test_download_final_publication_also_works_without_hard_links(self):
        job, item, data = self.prepared_download()
        with self.no_links():
            published = yakala._agent_publish_downloads(self.vault, self.state, job, {'downloads': [item]})
        target = Path(published[0]['path'])
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(target.with_name(target.name + '.part').exists())
        saved = json.loads((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['publication_intents'][0]['state'], 'published')
        self.assertEqual(saved['published'], published)

    def test_download_conflict_keeps_user_bytes_and_complete_part(self):
        job, item, data = self.prepared_download()
        destination = Path(job['config']['downloads_dir'])
        destination.mkdir()
        target = destination / ('track-yakala-' + job['id'][:8] + '.mp3')
        target.write_bytes(b'USER TRACK')
        with self.no_links(), self.assertRaises(FileExistsError):
            yakala._agent_publish_downloads(self.vault, self.state, job, {'downloads': [item]})
        self.assertEqual(target.read_bytes(), b'USER TRACK')
        self.assertEqual(target.with_name(target.name + '.part').read_bytes(), data)
        self.assertEqual(job['published'], [])


class CaptureStoreScopeTest(StorageFiles):
    def test_unrelated_case_variants_of_dosyalar_are_readable_and_searchable(self):
        job = self.job()
        for relative in ('Dosyalar/ordinary.md', 'knowledge/DOSYALAR/plan.md', 'projects/Dosyalar/notes/idea.md'):
            self.note(relative, 'NORMAL_FOLDER_CANARY')
            safe = yakala._agent_safe_note(self.vault, relative, job, state=self.state)
            self.assertEqual(yakala._agent_read_note(safe), 'NORMAL_FOLDER_CANARY')
        matches = yakala._agent_note_search(self.vault, 'NORMAL_FOLDER_CANARY', self.state)['matches']
        self.assertEqual(len(matches), 3)

    def test_actual_store_requires_link_even_for_ordinary_filename(self):
        job = self.job()
        parent = Path(job['card_path']).parent
        own = (parent / yakala.FILES / 'linked.md').as_posix()
        other = (parent / yakala.FILES / 'other.md').as_posix()
        self.note(own, 'OWN_CAPTURE')
        self.note(other, 'OTHER_CAPTURE')
        card = self.vault / job['card_path']
        card.write_text(card.read_text(encoding='utf-8') + '\n## Dosyalar\n\n- [[' + own + ']]\n', encoding='utf-8')
        self.assertEqual(yakala._agent_safe_note(self.vault, own, job, state=self.state), self.vault / own)
        with self.assertRaises(ValueError):
            yakala._agent_safe_note(self.vault, other, job, state=self.state)
        self.assertEqual(yakala._agent_note_search(self.vault, 'CAPTURE', self.state)['matches'], [])

    def test_custom_configured_store_is_protected_before_first_card_commit(self):
        self.state.mkdir()
        (self.state / 'yakala.json').write_text(json.dumps({'klasor': 'work/collect'}))
        path = 'work/collect/DOSYALAR/copied.md'
        self.note(path, 'COPY_IN_PROGRESS_CANARY')
        with self.assertRaises(ValueError):
            yakala._agent_safe_note(self.vault, path, state=self.state)
        self.assertEqual(yakala._agent_note_search(self.vault, 'COPY_IN_PROGRESS_CANARY', self.state)['matches'], [])

    def test_historical_moved_store_stays_private_after_inbox_change(self):
        job = self.job()
        parent = (self.vault / job['card_path']).parent
        self.note((parent.relative_to(self.vault) / 'dosyalar/nested/private.md').as_posix(), 'OLD_STORE_CANARY')
        moved = self.vault / 'old-collection'
        parent.rename(moved)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'yakala.json').write_text(json.dumps({'klasor': 'current-collection'}))
        (self.vault / 'current-collection').mkdir()
        # No newly introduced marker, nor reliance on the old store's name or .gitignore.
        self.assertFalse((moved / 'dosyalar/.gitignore').exists())
        with self.assertRaises(ValueError):
            yakala._agent_safe_note(self.vault, 'old-collection/dosyalar/nested/private.md', state=self.state)
        self.assertEqual(yakala._agent_note_search(self.vault, 'OLD_STORE_CANARY', self.state)['matches'], [])

    def test_tur_yakala_in_an_ordinary_body_is_not_capture_evidence(self):
        self.note('knowledge/example.md', '---\ntitle: example\n---\nExample:\ntur: yakala\n')
        path = self.note('knowledge/Dosyalar/ordinary.md', 'NORMAL_BODY_CANARY')
        self.assertEqual(yakala._agent_safe_note(self.vault, path.relative_to(self.vault).as_posix()), path)

    def test_orphaned_captured_files_remain_private_after_their_cards_move_away(self):
        incoming = self.base / 'incoming.md'
        incoming.write_text('ORPHAN_CAPTURE_CANARY', encoding='utf-8')
        self.state.mkdir()
        setting = self.state / 'yakala.json'
        setting.write_text(json.dumps({'klasor': 'original-capture'}))
        (self.vault / 'original-capture').mkdir()
        captured = yakala.capture(self.vault, files=[incoming], state=self.state)
        card = self.vault / captured['path']
        original = card.parent
        card.rename(self.vault / 'moved-card.md')
        (self.vault / 'new-capture').mkdir()
        setting.write_text(json.dumps({'klasor': 'new-capture'}))
        self.assertEqual((original / 'dosyalar/.gitignore').read_text(), '*\n')
        with self.assertRaises(ValueError):
            yakala._agent_safe_note(self.vault, 'original-capture/dosyalar/incoming.md', state=self.state)
        self.assertEqual(yakala._agent_note_search(self.vault, 'ORPHAN_CAPTURE_CANARY', self.state)['matches'], [])

    def test_unrelated_gitignore_rules_do_not_identify_an_attachment_store(self):
        path = self.note('knowledge/Dosyalar/ordinary.md', 'NORMAL_RULE_CANARY')
        self.note('knowledge/Dosyalar/.gitignore', '*.tmp\n')
        self.assertEqual(yakala._agent_safe_note(self.vault, path.relative_to(self.vault).as_posix()), path)

    def test_private_metadata_inside_ordinary_dosyalar_is_still_respected(self):
        path = self.note('knowledge/Dosyalar/personal.md', '---\nvisibility: private\n---\nPRIVATE_CANARY')
        safe = yakala._agent_safe_note(self.vault, path.relative_to(self.vault).as_posix())
        with self.assertRaises(ValueError):
            yakala._agent_read_note(safe)
        self.assertEqual(yakala._agent_note_search(self.vault, 'PRIVATE_CANARY', self.state)['matches'], [])


class AgentJournalTest(StorageFiles):
    def journal_job(self):
        # A fresh source per job: capturing the same URL again returns the existing card instead.
        job = self.job('https://example.com/j/' + uuid.uuid4().hex[:6])
        job.update(session_id=str(uuid.uuid4()), journal_time='2026-10-20T12:34:56+03:00',
                   outcome=(Path(job['card_path']).parent / 'Ajan' / job['id'] / '__host-result.md').as_posix(),
                   receipt='memory/receipt-' + job['id'] + '.md')
        self.note(job['outcome'], '---\nvisibility: private\n---\nPRIVATE_RESULT_CANARY')
        self.note(job['receipt'], '# Receipt\n')
        return job

    def write(self, job, terminal='completed'):
        return yakala._agent_write_journal(self.vault, self.state, job, terminal)

    def test_configured_capture_folder_is_used_without_companion_scan(self):
        self.state.mkdir(parents=True)
        (self.state / yakala.STATE_FILE).write_text(json.dumps({'klasor': 'Custom/Captures'}), encoding='utf-8')
        (self.vault / 'Custom/Captures').mkdir(parents=True)
        job = self.journal_job()
        unrelated = [self.note(path, '# User session\n') for path in (
            '850-Companion/Last-Session.md', '850-Echo/Last-Session.md',
            'archive/850-Companion/Last-Session.md', 'projects/docs/Last-Session.md')]
        before = [path.read_bytes() for path in unrelated]
        with patch.object(yakala.os, 'scandir', side_effect=AssertionError('No companion discovery')):
            journal = self.write(job)
        self.assertEqual(journal, self.vault / 'Custom/Captures/Ajan/Gunluk-2026-10.md')
        self.assertEqual([path.read_bytes() for path in unrelated], before)
        self.assertEqual(job['journal'], 'Custom/Captures/Ajan/Gunluk-2026-10.md')

    def test_missing_file_has_internal_frontmatter_title_and_one_short_entry(self):
        job = self.journal_job()
        journal = self.write(job)
        text = yakala._agent_read_note(journal)  # No own/private exemption.
        self.assertIn('visibility: "internal"', text)
        self.assertIn('# Yakala ajan gunlugu - 2026-10\n\n- 2026-10-20 12:34', text)
        entry = text[text.index('- 2026-10-20'):]
        self.assertEqual(entry.count('<!-- yakala-ajan:' + job['id'] + ' -->'), 1)
        self.assertLessEqual(len(entry), 300)  # Includes all link targets and the UUID marker.
        self.assertIn('claude · ' + job['session_id'][:8] + ' · completed', entry)
        self.assertNotIn(job['session_id'], entry)
        matches = yakala._agent_note_search(self.vault, 'Gunluk-2026-10', self.state)['matches']
        self.assertEqual([item['path'] for item in matches], [job['journal']])

    def test_repeat_after_reload_and_month_rollover_keeps_one_byte_identical_entry(self):
        job = self.journal_job()
        journal = self.write(job)
        before = journal.read_bytes()
        saved = json.loads((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json').read_text(encoding='utf-8'))
        with patch.object(yakala, 'now', return_value=yakala.datetime(2026, 11, 1)), \
                patch.object(yakala, '_atomic_bytes') as replace, patch.object(yakala, '_agent_create_bytes') as create:
            self.assertEqual(self.write(saved), journal)
        replace.assert_not_called()
        create.assert_not_called()
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse(journal.with_name('Gunluk-2026-11.md').exists())

    def test_first_attempt_pins_month_before_publication_even_when_write_fails(self):
        job = self.journal_job()
        job.pop('journal_time')
        with patch.object(yakala, 'now', return_value=yakala.datetime(2026, 10, 31, 23, 59)), \
                patch.object(yakala, '_agent_create_bytes', side_effect=OSError('Disk full')), \
                self.assertRaises(OSError):
            self.write(job)
        saved = json.loads((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['journal_time'], '2026-10-31T23:59:00')
        self.assertNotIn('journal', saved)
        with patch.object(yakala, 'now', return_value=yakala.datetime(2026, 11, 1)):
            journal = self.write(saved)
        self.assertEqual(journal.name, 'Gunluk-2026-10.md')
        self.assertEqual(journal.read_text(encoding='utf-8').count('<!-- yakala-ajan:'), 1)

    def test_newest_first_preserves_frontmatter_earlier_bytes_and_crlf(self):
        first = self.journal_job()
        journal = self.write(first)
        before = journal.read_bytes().replace(b'\n', b'\r\n')
        journal.write_bytes(before)
        second = self.journal_job()
        second['journal_time'] = '2026-10-20T12:35:00+03:00'
        self.write(second, 'approval_required')
        text = journal.read_bytes()
        split = before.index(b'- 2026-10-20')
        self.assertTrue(text.startswith(before[:split]))
        self.assertTrue(text.endswith(before[split:]))
        self.assertLess(text.index(second['id'].encode()), text.index(first['id'].encode()))
        self.assertEqual(text.count(b'\n'), text.count(b'\r\n'))
        self.assertEqual(text.count(b'# Yakala ajan gunlugu'), 1)
        self.assertEqual(text.count(b'<!-- yakala-ajan:'), 2)
        self.assertIn('visibility: "internal"', yakala._agent_read_note(journal))

    def test_new_job_in_new_month_creates_separate_journal(self):
        job = self.journal_job()
        october = self.write(job)
        before = october.read_bytes()
        next_job = self.journal_job()
        next_job['journal_time'] = '2026-11-01T00:00:00+03:00'
        november = self.write(next_job)
        self.assertEqual(november.name, 'Gunluk-2026-11.md')
        self.assertEqual(october.read_bytes(), before)
        self.assertNotIn(job['id'], november.read_text(encoding='utf-8'))

    def test_legacy_last_session_field_is_not_a_write_target(self):
        job = self.journal_job()
        job['last_session'] = 'nonexistent/Last-Session.md'
        self.write(job)
        self.assertNotIn('last_session', job)
        self.assertFalse((self.vault / 'nonexistent').exists())

    def test_linked_journal_is_not_followed(self):
        job = self.journal_job()
        outside = self.note('elsewhere/user.md', 'USER BYTES')
        journal = self.vault / Path(job['outcome']).parent.parent / 'Gunluk-2026-10.md'
        try:
            journal.symlink_to(outside)
        except OSError as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        with self.assertRaises(ValueError):
            self.write(job)
        self.assertEqual(outside.read_bytes(), b'USER BYTES')

    def test_linked_journal_directory_is_not_followed(self):
        job = self.journal_job()
        other = self.vault / 'Other'
        other.mkdir()
        # Use a fresh card parent so the per-job notes do not already occupy Ajan/.
        job['card_path'] = 'Other/card.md'
        self.note(job['card_path'])
        try:
            (other / 'Ajan').symlink_to(self.vault / Path(job['outcome']).parent.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        with self.assertRaises(ValueError):
            self.write(job)
        self.assertFalse((other / 'Ajan/Gunluk-2026-10.md').exists())

    def test_creation_race_does_not_replace_other_writer(self):
        job = self.journal_job()
        original = yakala._agent_create_bytes

        def race(path, data):
            path.write_bytes(b'OTHER WRITER')
            return original(path, data)

        with patch.object(yakala, '_agent_create_bytes', side_effect=race), self.assertRaises(FileExistsError):
            self.write(job)
        journal = self.vault / Path(job['outcome']).parent.parent / 'Gunluk-2026-10.md'
        self.assertEqual(journal.read_bytes(), b'OTHER WRITER')
        self.assertNotIn('journal', job)

    def test_full_readback_is_verified_even_when_marker_survives(self):
        job = self.journal_job()
        original = yakala._agent_create_bytes

        def corrupt(path, data):
            return original(path, data.replace(b'completed', b'CORRUPTED'))

        with patch.object(yakala, '_agent_create_bytes', side_effect=corrupt), \
                self.assertRaisesRegex(ValueError, 'geri okunamadi'):
            self.write(job)
        self.assertNotIn('journal', job)

    def test_oversized_journal_is_preserved(self):
        job = self.journal_job()
        journal = self.vault / Path(job['outcome']).parent.parent / 'Gunluk-2026-10.md'
        before = b'x' * (2 * 1024 * 1024 + 1)
        journal.write_bytes(before)
        with self.assertRaisesRegex(ValueError, 'cok buyuk'):
            self.write(job)
        self.assertEqual(journal.read_bytes(), before)


class CaptureLockScopeTest(StorageFiles):
    @unittest.skipIf(os.name == 'nt', 'Windows uses its existing named mutex')
    def test_agent_disabled_capture_creates_no_vault_lock_or_agent_state(self):
        yakala.capture(self.vault, text='ordinary capture', state=self.state)
        self.assertFalse((self.vault / '.beyin-yakala-card.lock').exists())
        self.assertTrue(yakala._capture_lock_path(self.vault).is_file())
        self.assertFalse((self.local_state / 'yakala/ajan').exists())
        self.assertFalse((self.state / 'yakala/ajan').exists())

    @unittest.skipIf(os.name == 'nt', 'Windows uses its existing named mutex')
    def test_lock_domain_does_not_change_with_enabled_agent_or_pinned_state(self):
        expected = yakala._capture_lock_path(self.vault)
        self.note('.beyin-runtime.json', json.dumps({'state': str(self.state)}))
        for client in ('claude', 'kapali'):
            yakala._agent_json(yakala._agent_root(self.state) / 'config.json',
                               dict(yakala._agent_defaults(self.vault), client=client))
            yakala.capture(self.vault, text=client, state=self.state)
            self.assertEqual(yakala._capture_lock_path(self.vault), expected)
        self.assertFalse((self.vault / '.beyin-yakala-card.lock').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX cross-process flock integration')
    def test_different_explicit_states_still_serialize_two_real_processes(self):
        program = '''
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as yakala
yakala.default_state = lambda vault: Path(sys.argv[3])
print('ready', flush=True)
yakala.capture(Path(sys.argv[2]), text='from child', state=Path(sys.argv[4]))
print('captured', flush=True)
'''
        process = None
        try:
            with yakala._WindowsCommitLock(self.vault):
                process = subprocess.Popen([sys.executable, '-c', program, str(SCRIPTS), str(self.vault),
                                            str(self.local_state), str(self.base / 'another-selected-state')],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.3)
                self.assertEqual(yakala.cards(self.vault, self.state), [])
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn('captured', output)
            self.assertEqual(len(yakala.cards(self.vault, self.state)), 1)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
