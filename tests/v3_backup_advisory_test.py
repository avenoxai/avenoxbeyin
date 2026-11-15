"""A configured Git remote is a hint, not proof of a recoverable backup (#678)."""
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
SPEC = importlib.util.spec_from_file_location('backup_cli', ROOT / 'scripts/beyin_v3.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@unittest.skipUnless(shutil.which('git'), 'Git optional; executable unavailable')
class BackupAdvisoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-backup-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault = self.root / 'Ölçüm vault'
        self.vault.mkdir()
        self.env = inherited_env(HOME=str(self.root), USERPROFILE=str(self.root),
                                 PYTHONDONTWRITEBYTECODE='1')
        self.env = {k: v for k, v in self.env.items() if not k.upper().startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.vault), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_plain_folder_and_no_remote_have_unknown_backup(self):
        before = snapshot(self.vault)
        report = CLI.backup_advisory(self.vault)
        self.assertEqual(report['status'], 'unknown')
        self.assertFalse(report['backup_verified'])
        self.assertEqual(report['reason'], 'not_git_repository')
        self.assertEqual(snapshot(self.vault), before)
        self.git('init')
        report = CLI.backup_advisory(self.vault)
        self.assertEqual(report['reason'], 'no_remote_url')
        self.assertFalse(report['remote_configured'])

    def test_remote_configuration_is_not_backup_proof_and_does_not_leak(self):
        self.git('init')
        self.git('remote', 'add', 'SECRET-NAME', 'https://SECRET-TOKEN@example.invalid/PRIVATE.git')
        before = snapshot(self.vault)
        report = CLI.backup_advisory(self.vault)
        self.assertEqual(report['status'], 'remote_configured')
        self.assertTrue(report['remote_configured'])
        self.assertFalse(report['backup_verified'])
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(snapshot(self.vault), before)

    def test_empty_url_does_not_count_and_git_environment_is_ignored(self):
        self.git('init')
        self.git('config', 'remote.empty.url', '')
        trace = self.root / 'must-not-write'
        with patch.dict(os.environ, {'GIT_DIR': '/missing', 'GIT_TRACE': str(trace),
                                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.fake.url',
                                     'GIT_CONFIG_VALUE_0': 'https://example.invalid'}):
            report = CLI.backup_advisory(self.vault)
        self.assertEqual(report['reason'], 'no_remote_url')
        self.assertFalse(trace.exists())

    def test_installed_doctor_is_informational_and_human_output_explains_limits(self):
        state = self.root / 'state'
        install(self.vault, state, self.env)
        command = [sys.executable, str(self.vault / 'beyin.py'), 'doctor']
        def doctor(*extra):
            result = subprocess.run(command + list(extra), env=self.env, cwd=self.vault,
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        initial = json.loads(doctor())
        self.assertEqual(initial['backup']['status'], 'unknown')
        self.assertIn('Yedek (bilgi)', doctor('--human'))
        self.git('init')
        self.git('remote', 'add', 'origin', 'https://example.invalid/private.git')
        before = snapshot(self.vault)
        later = json.loads(doctor())
        self.assertEqual(later['status'], initial['status'])
        self.assertFalse(later['backup']['backup_verified'])
        self.assertIn('dogrulanmadi', doctor('--human'))
        self.assertEqual(snapshot(self.vault), before)


class BackupFailuresTest(unittest.TestCase):
    def test_errors_are_bounded_private_and_do_not_suppress_doctor(self):
        for error in (FileNotFoundError('PRIVATE'), subprocess.TimeoutExpired('git', 2), OSError('PRIVATE')):
            with patch.object(CLI.subprocess, 'run', side_effect=error):
                report = CLI.backup_advisory(Path.cwd())
            self.assertEqual(report['status'], 'unavailable')
            self.assertFalse(report['backup_verified'])
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_advisory_does_not_change_scheduled_issue_policy(self):
        CLI._ensure_scripts_path()
        from beyin_v3_scheduled import issues
        self.assertEqual(issues({}, {}), issues({}, {'backup': {'status': 'unknown'}}))


if __name__ == '__main__':
    unittest.main()
