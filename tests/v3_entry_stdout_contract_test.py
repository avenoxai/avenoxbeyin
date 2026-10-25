"""Output contract of the installed beyin.py: stdout carries only a successful result.

A failure is a non-zero exit with its message on stderr and nothing on stdout, in --json mode
and in human mode alike. The QUICKSTART example and every caller that parses stdout rely on it.

This is a pin, not a fix: it passes on the code it was added to. It exists because no test
failed when #229 moved the entry point's JSON error object to stdout.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python, snapshot


class EntryStdoutContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix='v3-entry-contract-')
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.vault, cls.state = cls.root / 'vault', cls.root / 'state'
        cls.vault.mkdir()
        cls.env = isolated_env(cls.root / 'home')
        installed = install(cls.vault, cls.state, cls.env)
        if installed.returncode:
            raise AssertionError(installed.stderr.decode('utf-8', errors='replace'))

    def run_entry(self, entry, *args):
        done = run_python(entry, args, entry.parent, self.env)
        return done.returncode, done.stdout, done.stderr.decode('utf-8', errors='replace')

    def test_usage_error_exits_2_with_the_message_on_stderr_and_an_empty_stdout(self):
        cases = (
            # Refused by the CLI's own parser, reached through the entry point.
            (('recap', '--days', 'abc'), "argument --days: invalid int value: 'abc'"),
            # Refused by the parser the entry point builds itself for update, rollback and recover.
            (('update', '--no-such-flag'), 'unrecognized arguments: --no-such-flag'),
        )
        before = snapshot(self.vault), snapshot(self.state)
        for args, message in cases:
            for mode in ('--json', '--human'):
                with self.subTest(args=args, mode=mode):
                    code, stdout, stderr = self.run_entry(self.vault / 'beyin.py', *args, mode)
                    self.assertEqual(code, 2, stderr)
                    self.assertEqual(stdout, b'')
                    self.assertIn(message, stderr)
                    if mode == '--json':
                        error = json.loads(stderr)
                        self.assertEqual(error['error'], 'SystemExit')
                        self.assertEqual(error['message'], message)
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)

    def test_failed_command_exits_1_with_the_error_on_stderr_and_an_empty_stdout(self):
        # A copy of the installed entry point in a folder that was never installed fails in
        # beyin.py itself, before any command runs; the other case is refused inside the CLI.
        stray = self.root / 'not-installed'
        stray.mkdir()
        shutil.copyfile(self.vault / 'beyin.py', stray / 'beyin.py')
        cases = (
            (self.vault / 'beyin.py', ('recap', '--days', '0'), 'recap days must be 1..366 and limit must be 1..100'),
            (stray / 'beyin.py', ('doctor',), 'Kurulum ayari eksik'),
        )
        for entry, args, message in cases:
            for mode in ('--json', '--human'):
                with self.subTest(entry=entry.parent.name, args=args, mode=mode):
                    code, stdout, stderr = self.run_entry(entry, *args, mode)
                    self.assertEqual(code, 1, stderr)
                    self.assertEqual(stdout, b'')
                    self.assertIn(message, stderr)
                    if mode == '--json':
                        error = json.loads(stderr.strip().splitlines()[-1])
                        self.assertEqual(error['error'], 'ValueError')
                        self.assertTrue(error['message'].startswith(message), error)


if __name__ == '__main__':
    unittest.main()
