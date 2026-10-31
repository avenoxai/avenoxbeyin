from v3_package_helpers import inherited_env
"""Directory identity and captured-source provenance regressions.

The POSIX case-insensitive lookup model patches stat/lstat/open only; Path.resolve
still executes CPython's real code and preserves the supplied spelling. Native
case-insensitive macOS/Windows volumes are not implied by those modeled checks.
The rename/lock test uses two real processes and the host's actual directory inode.
"""


import contextlib


import hashlib


import os


from pathlib import Path


import subprocess


import sys


import tempfile


import unittest


from unittest.mock import patch


import v3_yakala_ajan_test as fixtures


y = fixtures.yakala


@contextlib.contextmanager
def case_alias(alias, actual):
    """Model one case-insensitive filesystem alias without canonicalizing its name."""
    alias, actual = Path(alias), Path(actual)
    native_stat, native_lstat, native_open = os.stat, os.lstat, os.open

    def mapped(path):
        if isinstance(path, int):
            return path
        try:
            tail = Path(os.fsdecode(path)).relative_to(alias)
        except ValueError:
            return path
        target = actual / tail
        return os.fsencode(target) if isinstance(path, bytes) else os.fspath(target)

    with patch.object(os, 'stat', side_effect=lambda path, *a, **k: native_stat(mapped(path), *a, **k)), \
            patch.object(os, 'lstat', side_effect=lambda path, *a, **k: native_lstat(mapped(path), *a, **k)), \
            patch.object(os, 'open', side_effect=lambda path, *a, **k: native_open(mapped(path), *a, **k)):
        yield


class CaptureDirectoryIdentityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-alias-lock-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.vault = self.base / 'Vault'
        self.vault.mkdir()
        self.local = self.base / 'local-state' / 'beyin-v3'
        # Preserve default_state's path-dependent basename, which fixed-return fixtures hide.
        default = patch.object(y, 'default_state', side_effect=lambda vault: self.local /
                               hashlib.sha256(str(vault).encode()).hexdigest()[:16])
        default.start()
        self.addCleanup(default.stop)

    @unittest.skipIf(os.name == 'nt', 'POSIX realpath and stat lookup model')
    def test_case_aliases_share_lock_even_when_default_state_hashes_differ(self):
        alias = self.base / 'VAULT'
        with case_alias(alias, self.vault):
            self.assertEqual(alias.resolve(), alias)
            self.assertNotEqual(y.default_state(alias), y.default_state(self.vault))
            self.assertTrue(os.path.samefile(alias, self.vault))
            self.assertEqual(y._capture_lock_path(alias), y._capture_lock_path(self.vault))
            self.assertEqual(y._capture_lock_path(alias).parent, self.local / 'yakala-card-locks')

    def test_different_directories_get_different_keys_on_a_filesystem_with_inodes(self):
        other = self.base / 'another-vault'
        other.mkdir()
        if os.path.samestat(self.vault.stat(), other.stat()):
            self.skipTest('Filesystem does not distinguish directory inode identities')
        self.assertNotEqual(y._capture_lock_path(self.vault), y._capture_lock_path(other))

    def test_metadata_changes_do_not_change_lock_identity(self):
        expected = y._capture_lock_path(self.vault)
        note = self.vault / 'new.md'
        note.write_text('new content', encoding='utf-8')
        os.utime(self.vault, (1, 1))
        self.assertEqual(y._capture_lock_path(self.vault), expected)

    @unittest.skipIf(os.name == 'nt', 'Windows uses its existing named mutex')
    def test_first_capture_establishes_directory_identity_before_locking(self):
        missing = self.base / 'new-vault'
        with y._WindowsCommitLock(missing):
            self.assertTrue(missing.is_dir())
            self.assertTrue(y._capture_lock_path(missing).is_file())
        self.assertEqual(list(missing.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX cross-process lock integration')
    def test_same_directory_after_rename_still_serializes_two_real_processes(self):
        program = '''
from pathlib import Path
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as y
y.default_state = lambda vault: Path(sys.argv[3]) / hashlib.sha256(str(vault).encode()).hexdigest()[:16]
print('ready', flush=True)
with y._WindowsCommitLock(Path(sys.argv[2])):
    print('entered', flush=True)
'''
        child = None
        try:
            with y._WindowsCommitLock(self.vault):
                before = y._capture_lock_path(self.vault)
                renamed = self.base / 'RenamedVault'
                self.vault.rename(renamed)
                self.assertEqual(y._capture_lock_path(renamed), before)
                child = subprocess.Popen([sys.executable, '-c', program, str(fixtures.SCRIPTS),
                                          str(renamed), str(self.local)], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, env=inherited_env())
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.3)
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error)
            self.assertEqual(output.strip(), 'entered')
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_native_case_insensitive_volume_alias_when_available(self):
        alias = self.base / 'vAULT'
        if not alias.exists() or not os.path.samefile(alias, self.vault):
            self.skipTest('Native case-insensitive volume unavailable on this host')
        self.assertEqual(y._capture_lock_path(alias), y._capture_lock_path(self.vault))


if __name__ == '__main__':
    unittest.main()
