"""The suite keeps Yakala card locks and default state out of the user's real state root.

Card locks live in the parent of default_state so that every spelling of one vault shares a
lock, even when a caller passes another --state. Each in-process capture on a throwaway test
vault therefore left one zero-byte yakala-card-locks/vault-<dev>-<ino>.lock in the developer's
real state root per run. Importing this module also installs the isolation for a whole discover
run, so a new test module that forgets isolate_user_state still cannot write there in CI.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env, isolate_user_state


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
SCRIPTS = ROOT / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402
isolate_user_state(yakala)

CHILD = '''
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import beyin_v3_yakala as y
if sys.argv[3] == 'isolated':
    from v3_package_helpers import isolate_user_state
    isolate_user_state(y)
vault = Path(sys.argv[4])
y.capture(vault, text='Isolation probe text', why='Isolation probe', state=Path(sys.argv[5]))
print(y._capture_lock_path(vault))
'''


def _user_roots():
    home = Path.home()
    roots = [home / 'Library/Application Support', home / 'AppData/Local', home / '.local/state']
    roots += [Path(os.environ[key]) for key in ('LOCALAPPDATA', 'XDG_STATE_HOME') if os.environ.get(key)]
    return [os.path.normcase(str(Path(root).resolve() / 'beyin-v3')) for root in roots]


def _under(path, roots):
    text = os.path.normcase(str(Path(path).resolve()))
    return any(text == root or text.startswith(root + os.sep) for root in roots)


class UserStateIsolationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yakala-state-isolation-')
        self.addCleanup(temporary.cleanup)
        # Windows runners hand out 8.3 short names; product paths are resolved.
        self.base = Path(temporary.name).resolve()
        self.vault = self.base / 'Örnek Kasa'
        self.vault.mkdir()

    def test_default_state_and_card_lock_stay_out_of_the_user_state_root(self):
        root = yakala.default_state._beyin_test_root
        state = yakala.default_state(self.vault)
        self.assertEqual(state.parent, root)
        self.assertEqual(state.name, hashlib.sha256(str(self.vault).encode()).hexdigest()[:16])
        lock = yakala._capture_lock_path(self.vault)
        self.assertEqual(lock.parent, root / 'yakala-card-locks')
        self.assertFalse(_under(state, _user_roots()))
        self.assertFalse(_under(lock, _user_roots()))

    def test_spellings_of_one_vault_keep_sharing_the_lock_folder(self):
        other = yakala.default_state(Path(str(self.vault).upper()))
        self.assertNotEqual(other, yakala.default_state(self.vault))
        self.assertEqual(other.parent, yakala.default_state(self.vault).parent)

    def test_a_test_owned_state_home_is_left_alone(self):
        home = self.base / 'home'
        env = {'HOME': str(home), 'USERPROFILE': str(home), 'LOCALAPPDATA': str(home / 'local'),
               'XDG_STATE_HOME': str(home / 'xdg')}
        with patch.dict(os.environ, env):
            expected = Path(yakala.default_state.__wrapped__(self.vault))
            self.assertEqual(yakala.default_state(self.vault), expected)
        self.assertTrue(_under(expected, [os.path.normcase(str(home))]))

    def test_isolation_is_installed_once_and_patches_still_override_it(self):
        installed = yakala.default_state
        self.assertIs(isolate_user_state(yakala), installed)
        with patch.object(yakala, 'default_state', return_value=self.base / 'state'):
            self.assertEqual(yakala._capture_lock_path(self.vault).parent, self.base / 'yakala-card-locks')
        self.assertIs(yakala.default_state, installed)

    def run_child(self, mode, home):
        vault, state = self.base / (mode + '-vault'), self.base / (mode + '-state')
        vault.mkdir()
        env = inherited_env(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / 'local'),
                            APPDATA=str(home / 'roaming'), XDG_STATE_HOME=str(home / 'xdg'))
        result = subprocess.run([sys.executable, '-c', CHILD, str(TESTS), str(SCRIPTS), mode, str(vault),
                                 str(state)], env=env, capture_output=True, text=True,
                                encoding='utf-8', timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip().splitlines()[-1])

    def test_real_capture_leaves_no_file_under_the_user_home(self):
        home = self.base / 'isolated-home'
        home.mkdir()
        self.run_child('isolated', home)
        self.assertEqual([p for p in home.rglob('*') if not p.is_dir()], [])

    @unittest.skipIf(os.name == 'nt', 'Windows serializes card commits with a named mutex, not a file')
    def test_without_isolation_the_same_capture_writes_a_lock_under_the_user_home(self):
        # Control for the test above: the leak this module guards against is real.
        home = self.base / 'plain-home'
        home.mkdir()
        lock = self.run_child('plain', home)
        self.assertTrue(lock.is_file())
        self.assertTrue(_under(lock, [os.path.normcase(str(home.resolve()))]))
        self.assertEqual(lock.parent.name, 'yakala-card-locks')


if __name__ == '__main__':
    unittest.main()
