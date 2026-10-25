import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env, install, isolated_env, snapshot


class CliJsonModeTest(unittest.TestCase):
    """Issue #417: CLI usage errors keep exit 2 and emit JSON on stderr when --json is passed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-cli-json-test-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.cli = ROOT / 'scripts/beyin_v3.py'
        self.env = inherited_env(**isolated_env(Path(self.tmp.name) / 'home'), BEYIN_UPDATES_OFF='1')

    def test_missing_command_in_cli_json_mode_returns_json_usage_error(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('command', error['message'])

    def test_missing_command_in_cli_human_mode_exits_with_argparse_stderr(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault)],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        self.assertIn('the following arguments are required', result.stderr.decode())

    def test_invalid_command_in_cli_json_mode_returns_json_usage_error(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json', 'invalid-command'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('invalid choice', error['message'])

    def test_help_in_cli_json_mode_exits_zero_with_stdout_usage(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json', '--help'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'usage:', result.stdout)
        self.assertEqual(result.stderr.strip(), b'')

    def test_subcommand_invalid_flag_in_cli_json_mode_fails_with_json_error(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json', 'sync', '--nonexistent-flag'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('unrecognized arguments', error['message'])

    def test_receipt_flag_conflict_in_cli_json_mode_fails_with_json_error(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json', 'receipt',
             '--file', 'test.json', '--event-id', 'ev-1'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('--file cannot be combined with receipt flags', error['message'])

    def test_missing_vault_and_invalid_type_return_json_without_writing_state(self):
        for args in (['--json', 'sync'],
                     ['--vault', str(self.vault), '--json', 'recap', '--days', 'abc']):
            with self.subTest(args=args):
                before = snapshot(self.vault)
                result = subprocess.run([sys.executable, str(self.cli), *args],
                                        capture_output=True, env=self.env, timeout=30)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(json.loads(result.stderr)['error'], 'SystemExit')
                self.assertEqual(snapshot(self.vault), before)

    def test_bridge_usage_validation_keeps_json_and_exit_2(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json',
             'bridge', '--harness', 'claude', '--install'],
            capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b'')
        error = json.loads(result.stderr)
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('--project-root is required', error['message'])

    def test_note_usage_json_stdout_contract_takes_precedence(self):
        result = subprocess.run(
            [sys.executable, str(self.cli), '--vault', str(self.vault), '--json', 'note-read'],
            capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(result.stdout)['status'], 'usage_error')

    def test_return_result_keeps_usage_exit_code_and_has_no_output(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location('cli_json_return_test', self.cli)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result, code = cli.main(['--json'], return_result=True)
        self.assertEqual(code, 2)
        self.assertEqual(result['error'], 'SystemExit')
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))


class InstalledEntryJsonModeTest(unittest.TestCase):
    """Issue #417: The installed entrypoint beyin.py formats argument errors as JSON when --json is passed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-installed-entry-test-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.env = inherited_env(**isolated_env(Path(self.tmp.name) / 'home'), BEYIN_UPDATES_OFF='1')
        res = install(self.vault, self.state, self.env)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_installed_entry_json_mode_invalid_command_returns_json(self):
        result = subprocess.run(
            [sys.executable, str(self.vault / 'beyin.py'), '--json', 'invalid-command'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('invalid choice', error['message'])

    def test_installed_entry_json_mode_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(self.vault / 'beyin.py'), '--json', '--help'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'usage:', result.stdout)
        self.assertEqual(result.stderr.strip(), b'')

    def test_installed_entry_json_mode_update_invalid_flag_returns_json(self):
        result = subprocess.run(
            [sys.executable, str(self.vault / 'beyin.py'), '--json', 'update', '--nonexistent-flag'],
            capture_output=True,
            env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), b'')
        error = json.loads(result.stderr.decode().strip())
        self.assertEqual(error['error'], 'SystemExit')
        self.assertIn('unrecognized arguments', error['message'])

    def test_installed_update_rollback_recover_usage_errors_touch_nothing(self):
        before = snapshot(self.vault), snapshot(self.state)
        for command, flags in (('update', ['--metadata-only']), ('rollback', ['--check']),
                               ('recover', ['--nonexistent-flag'])):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(self.vault / 'beyin.py'), command, '--json', *flags],
                    capture_output=True, env=self.env, timeout=30)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(json.loads(result.stderr)['error'], 'SystemExit')
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)


if __name__ == '__main__':
    unittest.main()
