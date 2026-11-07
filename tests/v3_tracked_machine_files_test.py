"""Read-only local Git index advisory for machine-local installation files (#539)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, install, snapshot

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tracked_machine_cli', ROOT / 'scripts/beyin_v3.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@unittest.skipUnless(shutil.which('git'), 'Git optional; executable unavailable')
class TrackedMachineFilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-tracked-machine-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / 'parent repo'
        self.vault = self.repo / 'Ölçüm vault with spaces'
        self.vault.mkdir(parents=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home),
                                 APPDATA=str(self.home / 'appdata'), LOCALAPPDATA=str(self.home / 'local'),
                                 PYTHONDONTWRITEBYTECODE='1')
        self.env = {k: v for k, v in self.env.items() if not k.startswith('GIT_')}
        self.git('init')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def add(self, relative, text='synthetic'):
        target = self.vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        self.git('add', '--', str(target))

    def test_nested_unicode_vault_only_reports_fixed_names_without_writes(self):
        self.add('.beyin-runtime.json', '{}')
        self.add('.claude/settings.local.json', '{}')
        self.add('.claude/scripts/compile.py')
        self.add('Beyni Guncelle.cmd')
        self.add('.claude/scripts/my-sync.py')
        self.add('notes/ordinary.md')
        # Indexed but deleted from the worktree: checking source existence would miss it.
        (self.vault / '.claude/scripts/compile.py').unlink()
        parent_pin = self.repo / '.beyin-runtime.json'
        parent_pin.write_text('{}')
        self.git('add', '--', str(parent_pin))
        before = snapshot(self.repo)
        report = CLI.tracked_machine_files(self.vault)
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(report['tracked'], ['.beyin-runtime.json', '.claude/scripts/compile.py', '.claude/settings.local.json', 'Beyni Guncelle.cmd'])
        self.assertEqual(snapshot(self.repo), before)

    def test_same_name_indexed_directory_does_not_expand_findings(self):
        for number in range(30):
            self.add('.beyin-runtime.json/sub/f%d' % number)
        before = snapshot(self.repo)
        report = CLI.tracked_machine_files(self.vault)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['tracked'], [])
        self.assertEqual(snapshot(self.repo), before)

    def test_git_environment_cannot_redirect_index_config_or_write_trace(self):
        self.add('.beyin-runtime.json', '{}')
        self.git('config', 'core.fsmonitor', str(self.root / 'must-not-execute'))
        trace = self.root / 'must-not-write-trace'
        poison = dict(GIT_DIR=str(self.root / 'wrong.git'), GIT_WORK_TREE=str(self.root),
                      GIT_INDEX_FILE=str(self.root / 'wrong-index'), GIT_TRACE=str(trace),
                      GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.fsmonitor',
                      GIT_CONFIG_VALUE_0=str(self.root / 'must-not-execute'),
                      GIT_LITERAL_PATHSPECS='1')
        before = snapshot(self.repo)
        with patch.dict(os.environ, poison):
            report = CLI.tracked_machine_files(self.vault)
        self.assertEqual(report['tracked'], ['.beyin-runtime.json'])
        self.assertFalse(trace.exists())
        self.assertFalse((self.root / 'wrong-index').exists())
        self.assertEqual(snapshot(self.repo), before)

    def test_real_sparse_index_is_unavailable_without_expansion_or_fetch(self):
        self.add('.beyin-runtime.json', '{}')
        keep = self.repo / 'keep' / 'note.md'
        keep.parent.mkdir()
        keep.write_text('# synthetic')
        self.git('add', '--', str(keep))
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-m', 'fixture')
        sparse = subprocess.run(['git', '-C', str(self.repo), 'sparse-checkout', 'init', '--cone', '--sparse-index'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        if sparse.returncode and ('unknown option' in sparse.stderr or 'not a git command' in sparse.stderr):
            self.skipTest('Git does not support the sparse-index fixture')
        self.assertEqual(sparse.returncode, 0, sparse.stderr)
        self.git('sparse-checkout', 'set', 'keep')
        self.vault.mkdir(exist_ok=True)
        before = snapshot(self.repo)
        report = CLI.tracked_machine_files(self.vault)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['reason'], 'sparse_checkout_or_index')
        self.assertEqual(snapshot(self.repo), before)

    def test_not_repo_is_explicitly_skipped(self):
        report = CLI.tracked_machine_files(self.home)
        self.assertEqual(report['status'], 'skipped')
        self.assertEqual(report['reason'], 'not_git_repository')

    def test_installed_doctor_advisory_does_not_change_health_or_sources(self):
        state = self.root / 'state'
        result = install(self.vault, state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = [sys.executable, str(self.vault / 'beyin.py'), 'doctor']
        initial = subprocess.run(command, cwd=self.vault, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        initial_report = json.loads(initial.stdout)
        self.git('add', '--', str(self.vault / '.beyin-runtime.json'))
        before = snapshot(self.repo)
        result = subprocess.run(command, cwd=self.vault, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['tracked_machine_files']['status'], 'warning')
        self.assertEqual(report['status'], initial_report['status'])
        human = subprocess.run(command + ['--human'], cwd=self.vault, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('Git', human.stdout)
        self.assertIn('.beyin-runtime.json', human.stdout)
        self.assertEqual(snapshot(self.repo), before)


class TrackedMachineFailureTest(unittest.TestCase):
    def test_missing_git_is_optional_and_timeouts_errors_unavailable(self):
        for error, status in ((FileNotFoundError(), 'skipped'),
                              (subprocess.TimeoutExpired('git', 2), 'unavailable'),
                              (OSError('sensitive path'), 'unavailable')):
            with patch.object(CLI.subprocess, 'run', side_effect=error):
                report = CLI.tracked_machine_files(Path.cwd())
            self.assertEqual(report['status'], status)
            self.assertNotIn('sensitive path', json.dumps(report))

    def test_advisory_does_not_change_scheduled_issue_policy(self):
        CLI._ensure_scripts_path()
        from beyin_v3_scheduled import issues
        self.assertEqual(issues({}, {}), issues({}, {'tracked_machine_files':
                         {'status': 'warning', 'tracked': ['.beyin-runtime.json']}}))

    def test_command_failure_does_not_look_healthy_or_expose_stderr(self):
        result = subprocess.CompletedProcess([], 128, stdout=b'', stderr=b'fatal: PRIVATE PATH\n')
        with patch.object(CLI.subprocess, 'run', return_value=result):
            report = CLI.tracked_machine_files(Path.cwd())
        self.assertEqual(report['status'], 'unavailable')
        self.assertNotIn('PRIVATE', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
