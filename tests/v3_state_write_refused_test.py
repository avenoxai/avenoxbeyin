"""A state folder the client may not write to is named as such by the CLI.

A sandboxed client (a workspace-write profile, a restricted Windows token) can run `beyin.py`
inside the vault but refuses writes to the runtime state, which lives outside it. `sync` and
`receipt` then stopped with the bare refusal, for example
`{"error": "PermissionError", "message": "[WinError 5] Access is denied: '...memory.sqlite3'"}`
or `{"error": "OperationalError", "message": "attempt to write a readonly database"}`, with no
word on what was refused or what to do. The lifecycle hook already treats the same refusal as
"this client cannot write state" (2614eab); the CLI now adds a hint that says so. The error and
message keep their shape, and a refusal elsewhere (a vault note, a full disk, a locked index)
gets no such hint.
"""
from contextlib import closing
import errno
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/beyin_v3.py'
NOTE = '---\nid: state-refusal-note\nkind: fact\nstatus: active\n---\nYerel kalan bir not.\n'


def load_cli():
    spec = importlib.util.spec_from_file_location('state_write_refused_cli', CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateWriteRefusedClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='v3-state-refused-unit-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'local' / 'beyin-v3' / 'key'

    def refused(self, exc):
        return self.cli.state_write_refused(exc, self.state)

    def test_a_permission_refusal_inside_or_above_the_state_is_recognized(self):
        for code in (errno.EACCES, errno.EPERM, errno.EROFS):
            for path in (self.state / 'memory.sqlite3', self.state, self.state.parent):
                with self.subTest(errno=errno.errorcode[code], path=path.name):
                    self.assertTrue(self.refused(PermissionError(code, os.strerror(code), str(path))))

    def test_a_refused_parent_counts_because_the_runtime_creates_it_for_state(self):
        # mkdir(parents=True) names the first parent it could not create, which may be far up:
        # the hint stays on, as it is still the state folder that could not be written.
        for path in (self.state.parent.parent, Path(self.state.anchor)):
            with self.subTest(path=str(path)):
                self.assertTrue(self.refused(PermissionError(errno.EACCES, 'Permission denied', str(path))))

    def test_the_second_path_of_a_rename_is_checked_too(self):
        exc = PermissionError(errno.EACCES, 'Permission denied', str(self.root / 'tmp' / 'x.tmp'),
                              None, str(self.state / 'hook-health.json'))
        self.assertEqual(exc.filename2, str(self.state / 'hook-health.json'))
        self.assertTrue(self.refused(exc))

    def test_a_refusal_elsewhere_is_not_blamed_on_the_state(self):
        for path in (self.vault / 'not.md', self.root / 'other' / 'memory.sqlite3', None):
            with self.subTest(path=path):
                exc = PermissionError(errno.EACCES, 'Permission denied', None if path is None else str(path))
                self.assertFalse(self.refused(exc))
        # A sibling of state shares only its parent, which is neither path.
        self.assertFalse(self.refused(PermissionError(errno.EACCES, 'Permission denied',
                                                      str(self.state.parent / 'other-key'))))

    def test_other_failures_on_the_state_are_not_permission_refusals(self):
        for code in (errno.ENOSPC, errno.EIO, errno.ENOENT, errno.EBUSY):
            with self.subTest(errno=errno.errorcode[code]):
                self.assertFalse(self.refused(OSError(code, os.strerror(code), str(self.state / 'memory.sqlite3'))))
        self.assertFalse(self.refused(ValueError('runtime belongs to another vault')))
        self.assertFalse(self.refused(sqlite3.OperationalError('no such table: records')))
        moved = sqlite3.OperationalError('attempt to write a readonly database')
        moved.sqlite_errorcode = sqlite3.SQLITE_READONLY_DBMOVED
        self.assertFalse(self.refused(moved))
        self.assertFalse(self.refused(None))

    @unittest.skipUnless(os.name == 'nt', 'winerror is set only on Windows')
    def test_windows_access_denied_and_write_protect_are_refusals(self):
        for winerror in (5, 19):
            with self.subTest(winerror=winerror):
                exc = OSError(None, 'Access is denied', str(self.state / 'memory.sqlite3'), winerror)
                self.assertEqual(exc.winerror, winerror)
                self.assertTrue(self.refused(exc))

    def test_a_read_only_database_is_a_refusal_and_a_locked_one_is_not(self):
        self.state.mkdir(parents=True)
        database = self.state / 'memory.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE t(x)')
        uri = database.resolve().as_uri()
        with closing(sqlite3.connect(uri + '?mode=ro', uri=True)) as reader:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                reader.execute('INSERT INTO t VALUES (1)')
        self.assertTrue(self.refused(caught.exception))
        with closing(sqlite3.connect(database, isolation_level=None)) as holder, \
                closing(sqlite3.connect(database, timeout=0)) as writer:
            holder.execute('BEGIN EXCLUSIVE')
            try:
                with self.assertRaises(sqlite3.OperationalError) as locked:
                    writer.execute('INSERT INTO t VALUES (2)')
            finally:
                holder.execute('ROLLBACK')
        self.assertFalse(self.refused(locked.exception))


class CliStateWriteRefusedTest(unittest.TestCase):
    """End to end: `sync` and `receipt` against a state folder that refuses writes."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='v3-state-refused-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        (self.vault / 'not.md').write_text(NOTE, encoding='utf-8', newline='\n')
        first = self.run_cli('sync')
        self.assertEqual(first.returncode, 0, first.stderr.decode())

    def run_cli(self, *args, state=None):
        env = inherited_env(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                            BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1')
        return subprocess.run([sys.executable, str(CLI), '--vault', str(self.vault),
                               '--state', str(state or self.state), *map(str, args)],
                              capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=120)

    def require_refused(self, probe, what):
        """Skip, not fail, where the refusal did not take (CAP_DAC_OVERRIDE, an ACL a runner ignores)."""
        try:
            probe()
        except PermissionError:
            return
        self.skipTest(what + ' did not refuse a write for this user')

    def append_to_index(self):
        with open(self.state / 'memory.sqlite3', 'ab'):
            pass

    def deny_on_windows(self, rights):
        user = os.environ.get('USERNAME')
        if not user:
            self.skipTest('no USERNAME for an ACL entry')
        grant = subprocess.run(['icacls', str(self.state), '/deny', f'{user}:(OI)(CI){rights}'],
                               capture_output=True, timeout=60)
        if grant.returncode:
            self.skipTest('icacls could not add a deny entry here')
        self.addCleanup(subprocess.run, ['icacls', str(self.state), '/remove:d', user],
                        capture_output=True, timeout=60)
        self.require_refused(self.append_to_index, 'the deny entry')

    def assert_named_refusal(self, state=None):
        (self.vault / 'not.md').write_text(NOTE + 'Degisti.\n', encoding='utf-8', newline='\n')
        receipt = ('receipt', '--harness', 'codex', '--session', 'sandboxed', '--event-id', 'refused-1',
                   '--summary', 'deneme', '--ref', 'not.md')
        for args in (('sync',), receipt):
            with self.subTest(command=args[0]):
                result = self.run_cli(*args, state=state)
                self.assertEqual(result.returncode, 1, result.stdout.decode())
                error = json.loads(result.stderr)
                # The existing shape stays: the hint is the only addition.
                self.assertIn(error['error'], ('PermissionError', 'OperationalError'))
                self.assertTrue(error['message'])
                self.assertIn('beyin.py doctor', error.get('hint', ''))

    @unittest.skipUnless(os.name == 'nt', 'Windows ACLs')
    def test_windows_state_that_refuses_file_data_writes(self):
        # Only file data and folder entries are refused: SQLite opens the index read-only and the
        # first write fails as "attempt to write a readonly database".
        self.deny_on_windows('(WD,AD)')
        self.assert_named_refusal()

    @unittest.skipUnless(os.name == 'nt', 'Windows ACLs')
    def test_windows_state_that_refuses_attribute_writes(self):
        # Attribute writes are refused too: the runtime's own chmod of the folder raises WinError 5.
        self.deny_on_windows('(WD,AD,WEA,WA)')
        self.assert_named_refusal()

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'POSIX permissions and a non-root user')
    def test_posix_read_only_index_file(self):
        # The runtime chmods its own folder back to 0700, so a read-only folder would not hold; a
        # read-only index file does, and SQLite then refuses the first write as SQLITE_READONLY.
        database = self.state / 'memory.sqlite3'
        database.chmod(stat.S_IRUSR)
        self.addCleanup(database.chmod, stat.S_IRUSR | stat.S_IWUSR)
        self.require_refused(self.append_to_index, 'a 0400 index file')
        self.assert_named_refusal()

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'POSIX permissions and a non-root user')
    def test_posix_state_folder_that_cannot_be_created(self):
        # A sandbox that refuses the state location itself: the runtime's mkdir raises EACCES.
        parent = self.root / 'locked'
        parent.mkdir()
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(parent.chmod, stat.S_IRWXU)
        self.require_refused(lambda: (parent / 'probe').mkdir(), 'a 0500 folder')
        self.assert_named_refusal(state=parent / 'state')

    def test_a_read_only_command_gets_no_write_hint(self):
        # doctor only reads the state; when even that is refused, "a step that writes state" is wrong.
        if os.name == 'nt':
            self.deny_on_windows('(W,D,DC,AD,WD)')
        else:
            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                self.skipTest('POSIX permissions and a non-root user')
            self.state.chmod(0)
            self.addCleanup(self.state.chmod, stat.S_IRWXU)
            self.require_refused(lambda: os.listdir(self.state), 'a 0000 folder')
        result = self.run_cli('doctor')
        if result.returncode == 0:
            self.skipTest('doctor could still read this state')
        self.assertNotIn('beyin.py doctor', json.loads(result.stderr).get('hint', ''))

    def test_an_unrelated_error_gets_no_state_hint(self):
        result = self.run_cli('receipt', '--harness', 'codex', '--session', 'sandboxed', '--event-id', 'missing-ref', '--summary', 'deneme',
                              '--ref', 'yok.md')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('beyin.py doctor', json.loads(result.stderr).get('hint', ''))


if __name__ == '__main__':
    unittest.main()
