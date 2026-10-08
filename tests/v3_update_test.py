"""Tests-first offline semver package update and recovery contract."""
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from concurrent.futures import ThreadPoolExecutor

from v3_package_helpers import ROOT, build_package, clean_environ, install, isolated_env, rewrite_zip, snapshot, run_python

MODULE = ROOT / 'template/.claude/scripts/beyin_v3_update.py'


def load_updater():
    if not MODULE.is_file():
        raise AssertionError('Versioned updater not implemented')
    spec = importlib.util.spec_from_file_location('beyin_v3_update_test_subject', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OfflineUpdateTest(unittest.TestCase):
    def setUp(self):
        self.module = load_updater()
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-update-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Synthetic Vault Ölçüm'
        self.vault.mkdir()
        self.state = self.base / 'runtime'
        self.env = isolated_env(self.base / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(self.version(), '3.0.0')
        self.note = self.vault / 'user-note.md';self.note.write_text('Synthetic original user note.\n')
        self.package = build_package(self.base / 'release.zip', '3.0.1', self.env)
        def changed_runtime(files):
            name = 'template/.claude/scripts/beyin_v3.py'
            files[name] += b'\n# Synthetic offline release change.\n'
            metadata = json.loads(files['manifest.json'])
            metadata['files'][name] = hashlib.sha256(files[name]).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()
        rewrite_zip(self.package, self.package, changed_runtime)

    def version(self):
        return (self.vault / '.beyin-version').read_text(encoding='utf-8').strip()

    def test_retry_interrupted_rollback_finishes_original_rollback(self):
        core = self.vault / '.claude/scripts/beyin_v3.py'
        original = core.read_bytes()
        self.module.update(self.vault, self.state, self.package)
        user_note = self.vault / 'after-update.md'
        user_note.write_text('Synthetic note survives rollback retry.')
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('Synthetic rollback interruption')
        with patch.object(self.module, 'transaction_hook', crash):
            with self.assertRaises(OSError):
                self.module.rollback(self.vault, self.state)
        self.assertEqual(json.loads((self.state / 'update-journal.json').read_text())['direction'], 'rollback')
        result = self.module.rollback(self.vault, self.state)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(self.version(), '3.0.0')
        self.assertEqual(core.read_bytes(), original)
        self.assertEqual(user_note.read_text(), 'Synthetic note survives rollback retry.')
        self.assertFalse((self.state / 'update-journal.json').exists())
        self.assertFalse((self.state / 'last-update.json').exists())

    def test_check_is_read_only_with_available_version(self):
        before_vault, before_state = snapshot(self.vault), snapshot(self.state)
        report = self.module.update(self.vault, self.state, self.package, check=True)
        self.assertEqual(report['status'], 'available')
        self.assertEqual(snapshot(self.vault), before_vault)
        self.assertEqual(snapshot(self.state), before_state)
        self.assertEqual(self.version(), '3.0.0')

    def test_update_idempotent_and_rollback_preserves_new_user_note(self):
        core = self.vault / '.claude/scripts/beyin_v3.py'
        original = core.read_bytes()
        report = self.module.update(self.vault, self.state, self.package)
        self.assertEqual(report['status'], 'updated')
        self.assertEqual(self.version(), '3.0.1')
        self.assertNotEqual(core.read_bytes(), original)
        before = snapshot(self.vault)
        self.assertEqual(self.module.update(self.vault, self.state, self.package)['status'], 'noop')
        self.assertEqual(snapshot(self.vault), before)
        self.note.write_text('User edit after system update.\n')
        rolled = self.module.rollback(self.vault, self.state)
        self.assertEqual(rolled['status'], 'rolled_back')
        self.assertEqual(self.version(), '3.0.0')
        self.assertEqual(core.read_bytes(), original)
        self.assertEqual(self.note.read_text(), 'User edit after system update.\n')

    def test_update_lock_does_not_create_empty_sqlite_file(self):
        import sqlite3
        memory_db = self.state / 'memory.sqlite3'
        self.assertFalse(memory_db.exists())
        with self.module.locked(self.vault, self.state):
            pass
        self.assertFalse(memory_db.exists())

    def test_rollback_preserves_original_file_mtime(self):
        import os
        core = self.vault / '.claude/scripts/beyin_v3.py'
        old_time = 1500000000.0
        os.utime(core, (old_time, old_time))
        self.module.update(self.vault, self.state, self.package)
        self.assertEqual(self.version(), '3.0.1')
        self.module.rollback(self.vault, self.state)
        self.assertEqual(self.version(), '3.0.0')
        self.assertEqual(core.stat().st_mtime, old_time)

    def test_interrupted_apply_keeps_old_version_and_recovers_once(self):
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('Synthetic interruption after first managed replacement')
        with patch.object(self.module, 'transaction_hook', side_effect=crash):
            with self.assertRaises(OSError):
                self.module.update(self.vault, self.state, self.package)
        self.assertEqual(self.version(), '3.0.0', 'Version stamp must be the last successful step')
        self.assertEqual(self.note.read_text(), 'Synthetic original user note.\n')
        recovered = self.module.recover(self.vault, self.state)
        self.assertEqual(recovered['status'], 'recovered')
        self.assertEqual(self.version(), '3.0.1')
        before = snapshot(self.vault)
        self.module.recover(self.vault, self.state)
        self.assertEqual(snapshot(self.vault), before)

    def test_recover_completes_after_a_user_note_changes_following_the_interruption(self):
        # The cutover receipt from the first install is already on disk, so recovery has no
        # migration left to prove; an edited note must not make it impossible.
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('Synthetic interruption after first managed replacement')
        with patch.object(self.module, 'transaction_hook', side_effect=crash):
            with self.assertRaises(OSError):
                self.module.update(self.vault, self.state, self.package)
        self.assertTrue((self.state / 'update-journal.json').exists())
        self.note.write_text('User edit after the interruption.\n')
        recovered = self.module.recover(self.vault, self.state)
        self.assertEqual(recovered['status'], 'recovered')
        self.assertEqual(self.version(), '3.0.1')
        self.assertEqual(self.note.read_text(), 'User edit after the interruption.\n')
        self.assertFalse((self.state / 'update-journal.json').exists())


    def test_builder_manifest_contains_matching_allowlisted_file_hashes(self):
        with zipfile.ZipFile(self.package) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            metadata = json.loads(archive.read('manifest.json'))
            self.assertEqual(metadata['schema'], 1)
            self.assertEqual(metadata['version'], '3.0.1')
            self.assertEqual(metadata['min_python'], '3.11')
            self.assertEqual(metadata['migrations'], [])
            self.assertEqual(set(names), {'manifest.json'} | set(metadata['files']))
            for name, expected in metadata['files'].items():
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn('..', Path(name).parts)
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected)
            self.assertIn('scripts/install_v3.py', metadata['files'])
            self.assertIn('scripts/beyin_entry.py', metadata['files'])

    def test_corrupt_checksum_rejected_without_any_vault_write(self):
        def corrupt(files):
            files['template/.claude/scripts/beyin_v3.py'] += b'\n# corrupt undeclared content\n'
        bad = rewrite_zip(self.package, self.base / 'corrupt.zip', corrupt)
        before = snapshot(self.vault)
        with self.assertRaises(ValueError):
            self.module.update(self.vault, self.state, bad)
        self.assertEqual(snapshot(self.vault), before)
        self.assertEqual(self.version(), '3.0.0')

    def test_path_traversal_and_nonallowlisted_paths_rejected(self):
        for name in ('../escaped.txt', '/absolute.txt', 'notes/user-note.md'):
            def extra(files, name=name):
                files[name] = b'Synthetic forbidden payload'
                metadata = json.loads(files['manifest.json'])
                metadata['files'][name] = hashlib.sha256(files[name]).hexdigest()
                files['manifest.json'] = json.dumps(metadata).encode()
            bad = rewrite_zip(self.package, self.base / ('bad-' + str(abs(hash(name))) + '.zip'), extra)
            before = snapshot(self.vault)
            with self.subTest(entry=name):
                with self.assertRaises(ValueError):
                    self.module.update(self.vault, self.state, bad)
                self.assertEqual(snapshot(self.vault), before)
        self.assertFalse((self.base / 'escaped.txt').exists())

    def test_edited_managed_runtime_conflicts_without_overwrite_or_version_stamp(self):
        core = self.vault / '.claude/scripts/beyin_v3.py'
        modified = core.read_bytes() + b'\n# User managed-runtime customization\n'
        core.write_bytes(modified)
        with self.assertRaises(ValueError):
            self.module.update(self.vault, self.state, self.package)
        self.assertEqual(core.read_bytes(), modified)
        self.assertEqual(self.version(), '3.0.0')
        self.assertEqual(self.note.read_text(), 'Synthetic original user note.\n')

    def test_unrelated_config_addition_survives_update_and_rollback(self):
        config = self.vault / '.claude/settings.local.json'
        value = json.loads(config.read_text(encoding='utf-8'))
        value['custom_product_preference'] = 'Synthetic ölçüm preference'
        config.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        self.module.update(self.vault, self.state, self.package)
        self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['custom_product_preference'], value['custom_product_preference'])
        after = json.loads(config.read_text(encoding='utf-8'))
        after['post_update_user_preference'] = 'Do not roll back user preferences'
        config.write_text(json.dumps(after, ensure_ascii=False), encoding='utf-8')
        self.module.rollback(self.vault, self.state)
        restored = json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(restored['custom_product_preference'], value['custom_product_preference'])
        self.assertEqual(restored['post_update_user_preference'], after['post_update_user_preference'])

    def test_semver_uses_numeric_order_and_rejects_downgrade(self):
        newer = build_package(self.base / 'newer.zip', '3.0.10', self.env)
        self.assertEqual(self.module.update(self.vault, self.state, newer)['status'], 'updated')
        older = build_package(self.base / 'older.zip', '3.0.2', self.env)
        with self.assertRaises(ValueError):
            self.module.update(self.vault, self.state, older)
        self.assertEqual(self.version(), '3.0.10')

    def test_parallel_updates_are_serialized_and_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: self.module.update(self.vault, self.state, self.package), range(2)))
        self.assertEqual(sorted(r['status'] for r in reports), ['noop', 'updated'])
        self.assertEqual(self.version(), '3.0.1')
        self.assertEqual(self.note.read_text(), 'Synthetic original user note.\n')

    def test_recovery_preserves_user_edit_to_partially_replaced_managed_file(self):
        replaced = []
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                replaced.append(True)
                raise OSError('Synthetic interruption')
        with patch.object(self.module, 'transaction_hook', side_effect=crash):
            with self.assertRaises(OSError):
                self.module.update(self.vault, self.state, self.package)
        self.assertTrue(replaced)
        core = self.vault / '.claude/scripts/beyin_v3.py'
        core.write_bytes(core.read_bytes() + b'\n# User edit during recovery window\n')
        modified = core.read_bytes()
        with self.assertRaises(ValueError):
            self.module.recover(self.vault, self.state)
        self.assertEqual(core.read_bytes(), modified)
        self.assertEqual(self.version(), '3.0.0')


    def test_installed_entrypoint_check_apply_and_rollback(self):
        entry = self.vault / 'beyin.py'
        self.assertTrue(entry.is_file())
        for arguments, status in [(['update', '--check', '--package', str(self.package)], 'available'),
                                  (['update', '--package', str(self.package)], 'updated'),
                                  (['rollback'], 'rolled_back')]:
            result = run_python(entry, arguments, self.vault, self.env)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(json.loads(result.stdout)['status'], status)
        self.assertEqual(self.version(), '3.0.0')


    def test_truncated_download_artifact_cannot_apply_or_stamp_version(self):
        truncated = self.base / 'truncated.zip'
        raw = self.package.read_bytes()
        truncated.write_bytes(raw[:len(raw) // 2])
        before = snapshot(self.vault)
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            self.module.update(self.vault, self.state, truncated)
        self.assertEqual(snapshot(self.vault), before)
        self.assertEqual(self.version(), '3.0.0')


    def test_checksum_valid_broken_runtime_fails_preflight_before_target_writes(self):
        for label, code in [('syntax', b'def broken(:\n    pass\n'),
                            ('import-crash', b'raise RuntimeError("SYNTHETIC_PACKAGE_PREFLIGHT_FAILURE")\n')]:
            def broken(files, code=code):
                name = 'template/.claude/scripts/beyin_v3.py'
                files[name] = code
                metadata = json.loads(files['manifest.json'])
                metadata['files'][name] = hashlib.sha256(code).hexdigest()
                files['manifest.json'] = json.dumps(metadata).encode()
            package = rewrite_zip(self.package, self.base / (label + '.zip'), broken)
            before = snapshot(self.vault)
            with self.subTest(failure=label):
                with self.assertRaises((ValueError, SyntaxError, RuntimeError)):
                    self.module.update(self.vault, self.state, package)
                self.assertEqual(snapshot(self.vault), before)
                self.assertEqual(self.version(), '3.0.0')


    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits; native Windows mode semantics differ')
    def test_recovery_repairs_mode_after_replace_before_chmod_failure(self):
        original_chmod = Path.chmod
        failed_target = []
        def fail_first_target_mode(path, mode, *args, **kwargs):
            if path.resolve().is_relative_to(self.vault.resolve()) and not failed_target:
                failed_target.append((path, mode))
                raise OSError('Synthetic failure after replacement before chmod')
            return original_chmod(path, mode, *args, **kwargs)
        with patch.object(Path, 'chmod', fail_first_target_mode):
            with self.assertRaises(OSError):
                self.module.update(self.vault, self.state, self.package)
        self.assertEqual(len(failed_target), 1)
        path, intended_mode = failed_target[0]
        self.assertEqual(self.version(), '3.0.0')
        self.assertEqual(self.module.recover(self.vault, self.state)['status'], 'recovered')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), intended_mode)
        self.assertEqual(self.version(), '3.0.1')


    def test_rollback_restores_bundled_skill_copies_after_mirror_state_advances(self):
        canonical = self.vault / '.agents/skills/beyin/SKILL.md'
        mirror = self.vault / '.claude/skills/beyin/SKILL.md'
        self.assertFalse(mirror.is_symlink(), 'Exercise Windows-style physical copy reconciliation')
        original = canonical.read_bytes()
        spec = importlib.util.spec_from_file_location('product_copy_skills', ROOT / 'template/.claude/scripts/beyin_v3_skills.py')
        skills = importlib.util.module_from_spec(spec);spec.loader.exec_module(skills)
        self.assertEqual(skills.sync_skills(self.vault, self.state, mode='copy')['conflicts'], [])
        def changed_skill(files):
            name = 'template/.agents/skills/beyin/SKILL.md'
            files[name] += b'\nSynthetic new release skill marker.\n'
            metadata = json.loads(files['manifest.json'])
            metadata['files'][name] = hashlib.sha256(files[name]).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()
        package = rewrite_zip(self.package, self.base / 'changed-skill.zip', changed_skill)
        self.module.update(self.vault, self.state, package)
        self.assertNotEqual(canonical.read_bytes(), original)
        self.assertEqual(mirror.read_bytes(), canonical.read_bytes())
        self.assertEqual(skills.sync_skills(self.vault, self.state, mode='copy')['conflicts'], [])
        self.module.rollback(self.vault, self.state)
        self.assertEqual(skills.sync_skills(self.vault, self.state, mode='copy')['conflicts'], [])
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(mirror.read_bytes(), original)

    def test_update_refreshes_release_cache_and_avoids_stale_ahead_status(self):
        # status() honours BEYIN_UPDATES_OFF; a suite run from a configured shell must not inherit it.
        with clean_environ():
            self._update_refreshes_release_cache()

    def _update_refreshes_release_cache(self):
        spec = importlib.util.spec_from_file_location('beyin_v3_releases', ROOT / 'template/.claude/scripts/beyin_v3_releases.py')
        releases = importlib.util.module_from_spec(spec); spec.loader.exec_module(releases)
        cache_file = self.state / 'release-cache.json'
        old_cache = {
            'schema': 1,
            'checked_at': 1000,
            'attempted_at': 1000,
            'next_check_at': 1000 + 86400,
            'etag': '"old-etag"',
            'release': {
                'release_id': 1,
                'version': '3.0.0',
                'published_at': '2026-09-01T00:00:00Z',
                'release_url': 'https://github.com/avenoxai/avenoxbeyin/releases/tag/v3.0.0',
                'asset_id': 1,
                'asset_name': 'beyin-v3-3.0.0.zip',
                'asset_url': 'https://github.com/avenoxai/avenoxbeyin/releases/download/v3.0.0/beyin-v3-3.0.0.zip',
                'asset_size': 1000,
                'asset_sha256': '00' * 32,
                'checksum_url': None,
            },
            'failures': 0,
        }
        releases.atomic_json(cache_file, old_cache)
        self.module.update(self.vault, self.state, self.package)
        self.assertFalse(cache_file.exists())
        self.assertEqual(releases.status(self.vault, self.state)['status'], 'unknown')
        releases.atomic_json(cache_file, old_cache)
        self.module.rollback(self.vault, self.state)
        self.assertFalse(cache_file.exists())
        fake_meta = {
            'release_id': 2,
            'version': '3.0.1',
            'published_at': '2026-09-10T00:00:00Z',
            'release_url': 'https://github.com/avenoxai/avenoxbeyin/releases/tag/v3.0.1',
            'asset_id': 2,
            'asset_name': 'beyin-v3-3.0.1.zip',
            'asset_url': 'https://github.com/avenoxai/avenoxbeyin/releases/download/v3.0.1/beyin-v3-3.0.1.zip',
            'asset_size': 1000,
            'asset_sha256': '11' * 32,
            'checksum_url': None,
        }
        with patch.object(self.module, '_download', return_value=(self.package, '3.0.1', fake_meta, '"etag-301"')):
            self.module.update(self.vault, self.state)
        self.assertTrue(cache_file.exists())
        st = releases.status(self.vault, self.state)
        self.assertEqual(st['status'], 'up_to_date')
        self.assertEqual(st['version'], '3.0.1')
        self.assertEqual(st['current_version'], '3.0.1')

    def test_cache_left_by_an_older_updater_is_not_reported_as_ahead(self):
        # 3.6.0 and 3.7.0 run their own updater, which never touches the cache (#188). The
        # first fixed release must still read the cache it inherits correctly: installed doctor.
        self.module.update(self.vault, self.state, self.package)
        stamped = (self.vault / '.beyin-version').stat().st_mtime
        release = {'release_id': 1, 'version': '3.0.0', 'published_at': '2026-09-01T00:00:00Z',
                   'release_url': 'https://github.com/avenoxai/avenoxbeyin/releases/tag/v3.0.0',
                   'asset_id': 1, 'asset_name': 'beyin-v3-3.0.0.zip',
                   'asset_url': 'https://github.com/avenoxai/avenoxbeyin/releases/download/v3.0.0/beyin-v3-3.0.0.zip',
                   'asset_size': 1000, 'asset_sha256': '00' * 32, 'checksum_url': None}
        cache = self.state / 'release-cache.json'
        def doctor():
            result = run_python(self.vault / 'beyin.py', ['doctor', '--json'], self.vault, self.env)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            return json.loads(result.stdout)['updates']
        checked = stamped - 60
        cache.write_text(json.dumps({'schema': 1, 'checked_at': checked, 'attempted_at': checked,
                                     'next_check_at': checked + 86400, 'etag': '"old"', 'release': release, 'failures': 0}))
        self.assertEqual(doctor(), {'status': 'unknown'})
        # A check made after the install that still finds 3.0.0 is a genuine dev install ahead.
        checked = time.time()
        self.assertGreater(checked, stamped)
        cache.write_text(json.dumps({'schema': 1, 'checked_at': checked, 'attempted_at': checked,
                                     'next_check_at': checked + 86400, 'etag': '"old"', 'release': release, 'failures': 0}))
        self.assertEqual(doctor()['status'], 'ahead')


if __name__ == '__main__':
    unittest.main()
