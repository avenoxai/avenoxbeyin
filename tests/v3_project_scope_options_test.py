"""Unsupported sharing options must not look successful or mutate project scopes (#607)."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python, snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_projects as projects

FOLDER = 'Projeler/Deneme'
PROJECT = 'deneme'
ERROR = '--shared-unscoped is only supported with project-scope add'


class ProjectScopeFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-project-scope-options-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        (self.vault / FOLDER).mkdir(parents=True)
        (self.vault / FOLDER / 'Not.md').write_text('# Sentetik not\n', encoding='utf-8')
        self.scope_file = self.vault / projects.PROJECT_SCOPES_FILE

    def scopes(self, shared=False, missing=False):
        data = {'folders': {FOLDER: PROJECT}, 'shared_unscoped': shared}
        if missing:
            data['folders']['Projeler/Kayip'] = 'kayip'
        # Preserve the user's BOM, whitespace and line endings on rejected/read-only calls.
        self.scope_file.write_bytes(('\ufeff' + json.dumps(data, ensure_ascii=False, indent=4)
                                     + '\r\n').encode('utf-8'))

    def filesystem(self):
        # Check bytes, file identity/mtime/permissions, and empty directory creation.
        paths = [self.root, *sorted(self.root.rglob('*'))]
        metadata = {p.relative_to(self.root).as_posix():
                    (p.is_dir(), p.stat().st_mode, p.stat().st_mtime_ns, p.stat().st_ino)
                    for p in paths}
        return snapshot(self.root), metadata


class ProjectScopeOptionsTest(ProjectScopeFixture, unittest.TestCase):
    def test_manage_rejects_non_add_before_inspecting_or_writing_configuration(self):
        for raw in (None, '{broken', '{"folders": {}, "shared_unscoped": false}'):
            if raw is None:
                self.scope_file.unlink(missing_ok=True)
            else:
                self.scope_file.write_text(raw, encoding='utf-8')
            for action in ('list', 'remove', 'prune', 'unknown'):
                for value in ('on', 'off'):
                    with self.subTest(raw=raw, action=action, value=value):
                        before = self.filesystem()
                        with self.assertRaisesRegex(ValueError, ERROR):
                            projects.manage(self.vault, action, folder=FOLDER, project=PROJECT,
                                            shared_unscoped=value)
                        self.assertEqual(self.filesystem(), before)


class InstalledProjectScopeOptionsTest(ProjectScopeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.state = self.root / 'state'
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def cli(self, *args):
        return run_python(self.vault / 'beyin.py', ['project-scope', *args], self.vault, self.env)

    def assert_rejected_without_changes(self, args, presentation):
        before = self.filesystem()
        done = self.cli(*args, presentation)
        self.assertEqual(self.filesystem(), before)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(done.stdout, b'')
        stderr = done.stderr.decode('utf-8')
        if presentation == '--json':
            error = json.loads(stderr)
            self.assertEqual(error['error'], 'ValueError')
            stderr = error['message']
        self.assertIn(ERROR, stderr)
        for required in ('--folder', '--project', '--shared-unscoped on|off'):
            self.assertIn(required, stderr)
        self.assertNotIn('Traceback', stderr)

    def test_default_and_list_reject_on_and_off_in_both_output_modes(self):
        for shared in (False, True):
            self.scopes(shared)
            for action in ([], ['list']):
                for value in ('on', 'off'):
                    for presentation in ('--json', '--human'):
                        with self.subTest(shared=shared, action=action, value=value, mode=presentation):
                            self.assert_rejected_without_changes(
                                [*action, '--shared-unscoped', value], presentation)

    def test_remove_and_prune_reject_before_their_other_mutations(self):
        for action in (['remove', '--folder', FOLDER], ['prune']):
            for shared in (False, True):
                for value in ('on', 'off'):
                    for presentation in ('--json', '--human'):
                        with self.subTest(action=action, shared=shared, value=value, mode=presentation):
                            self.scopes(shared, missing=True)
                            self.assert_rejected_without_changes(
                                [*action, '--shared-unscoped', value], presentation)

    def test_rejection_does_not_create_missing_configuration_or_runtime(self):
        shutil.rmtree(self.state)
        for action in ([], ['list'], ['remove', '--folder', FOLDER], ['prune']):
            for value in ('on', 'off'):
                with self.subTest(action=action, value=value):
                    self.assert_rejected_without_changes([*action, '--shared-unscoped', value], '--json')
                    self.assertFalse(self.state.exists())
                    self.assertFalse(self.scope_file.exists())

    def test_default_and_list_without_flag_stay_read_only(self):
        shutil.rmtree(self.state)
        for shared in (None, False, True):
            if shared is not None:
                self.scopes(shared)
            for action in ([], ['list']):
                for presentation in ('--json', '--human'):
                    with self.subTest(shared=shared, action=action, mode=presentation):
                        before = self.filesystem()
                        done = self.cli(*action, presentation)
                        self.assertEqual(done.returncode, 0, done.stderr)
                        self.assertEqual(done.stderr, b'')
                        self.assertEqual(self.filesystem(), before)
                        if presentation == '--json':
                            result = json.loads(done.stdout)
                            self.assertEqual(result['status'], 'absent' if shared is None else 'ok')
                            self.assertEqual(result['action'], 'list')
                            self.assertEqual(result['shared_unscoped'], bool(shared))
                        self.assertFalse(self.state.exists())

    def test_explicit_add_still_sets_on_and_off_and_preserves_other_data(self):
        self.scopes(False, missing=True)
        for value in ('on', 'off'):
            with self.subTest(value=value):
                before = snapshot(self.root)
                done = self.cli('add', '--folder', FOLDER, '--project', PROJECT,
                                '--shared-unscoped', value, '--json')
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stderr, b'')
                result = json.loads(done.stdout)
                saved = json.loads(self.scope_file.read_text(encoding='utf-8'))
                self.assertEqual(result['status'], 'saved')
                self.assertEqual(result['action'], 'add')
                self.assertEqual(result['shared_unscoped'], value == 'on')
                self.assertEqual(saved, {'folders': {FOLDER: PROJECT, 'Projeler/Kayip': 'kayip'},
                                         'shared_unscoped': value == 'on'})
                after = snapshot(self.root)
                key = self.scope_file.relative_to(self.root).as_posix()
                self.assertEqual({k: v for k, v in before.items() if k != key},
                                 {k: v for k, v in after.items() if k != key})

    def test_add_without_flag_preserves_existing_choice_and_defaults_to_off(self):
        for shared in (None, False, True):
            with self.subTest(shared=shared):
                if shared is None:
                    self.scope_file.unlink(missing_ok=True)
                else:
                    self.scopes(shared)
                done = self.cli('add', '--folder', FOLDER, '--project', PROJECT, '--json')
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout)['shared_unscoped'], bool(shared))

    def test_help_identifies_the_add_only_flag(self):
        done = self.cli('--help')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('add only:', done.stdout.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
