"""update, recover and rollback when only the state root's children are redirected (#139).

An MSIX-packaged client sees %LOCALAPPDATA% through a merged view. When the state
directory also exists outside the package (a pre-#114 install pinned the unredirected
spelling, or a process outside the package created it), resolving the root keeps that
spelling while files the package wrote resolve under Packages\\<id>\\LocalCache\\Local.
Comparing the two spellings made every transaction on such a vault fail with
'transaction target escapes root', and recover and the installer's resume replayed the
same journal into the same wall.

macOS and Linux cannot produce that view, so these tests fake it: Path.resolve() maps
anything strictly below the state root into a package-container prefix, and leaves the
root itself alone. The container prefix is a link back to the real directory, so the
redirected spelling still reaches the same files, as it does in the package. Links that
lead out of either root must still be rejected under the same view.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import INSTALLER, ROOT, build_package, install, isolated_env, rewrite_zip

MODULE = ROOT / 'template/.claude/scripts/beyin_v3_update.py'
ORIGINAL_RESOLVE = Path.resolve


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RedirectedChildrenTest(unittest.TestCase):
    def setUp(self):
        self.updater = load(MODULE, 'beyin_v3_update_redirect_test_subject')
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-update-redirect-')
        self.addCleanup(self.tmp.cleanup)
        # Resolve the fixture base first so the only alias under test is the faked one.
        self.base = ORIGINAL_RESOLVE(Path(self.tmp.name))
        self.vault = self.base / 'Synthetic Vault Ölçüm'
        self.vault.mkdir()
        self.local = self.base / 'AppData' / 'Local'
        self.state = self.local / 'beyin-v3' / 'a1b2c3'
        self.container = self.base / 'AppData' / 'Local' / 'Packages' / 'Claude_pzs8sxrjxfjjc' / 'LocalCache' / 'Local'
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        (self.outside / 'v3-install.json').write_text('{"outside": true}\n', encoding='utf-8')
        self.env = isolated_env(self.base / 'home')

    def install(self):
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        # The container spelling reaches the same directory, as the merged view does.
        self.container.parent.mkdir(parents=True)
        try:
            os.symlink(self.local, self.container, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this runner')

    def package(self, version='3.0.1'):
        path = build_package(self.base / 'release.zip', version, self.env)
        def changed_runtime(files):
            import hashlib
            name = 'template/.claude/scripts/beyin_v3.py'
            files[name] += b'\n# Synthetic offline release change.\n'
            metadata = json.loads(files['manifest.json'])
            metadata['files'][name] = hashlib.sha256(files[name]).hexdigest()
            files['manifest.json'] = json.dumps(metadata).encode()
        rewrite_zip(path, path, changed_runtime)
        return path

    def redirected(self):
        """Resolve children of the state root into the package container, but not the root."""
        root = ORIGINAL_RESOLVE(self.state)
        local, container = self.local, self.container

        def resolve(path, strict=False):
            real = ORIGINAL_RESOLVE(path, strict=strict)
            if real != root and real.is_relative_to(root):
                return container / real.relative_to(local)
            return real
        return patch.object(Path, 'resolve', resolve)

    def version(self):
        return (self.vault / '.beyin-version').read_text(encoding='utf-8').strip()

    def assert_view_is_faked(self):
        self.assertEqual(self.state.resolve(), ORIGINAL_RESOLVE(self.state), 'the root keeps its spelling')
        self.assertTrue((self.state / 'v3-install.json').resolve().is_relative_to(self.container),
                        'a child resolves into the package container')

    def test_update_completes_when_state_children_resolve_into_the_package_container(self):
        self.install()
        package = self.package()
        with self.redirected():
            self.assert_view_is_faked()
            result = self.updater.update(self.vault, self.state, package)
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.version(), '3.0.1')
        self.assertFalse((self.state / 'update-journal.json').exists())
        self.assertEqual(json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))['version'], '3.0.1')
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['v3-install.json'])

    def test_recover_finishes_a_journal_left_by_the_redirected_view(self):
        self.install()
        package = self.package()
        state_index = []
        def crash(phase, index=None):
            # Stop right before the state-scope write, where #139's update stopped.
            if phase == 'after_replace' and index == state_index[0] - 1:
                raise OSError('Synthetic interruption before the state target')
        original_apply = self.updater._apply
        def apply(vault, state, journal, migration=None):
            state_index[:] = [next(i for i, op in enumerate(journal['operations']) if op['scope'] == 'state')]
            return original_apply(vault, state, journal, migration)
        with patch.object(self.updater, '_apply', apply), patch.object(self.updater, 'transaction_hook', crash):
            with self.assertRaises(OSError):
                self.updater.update(self.vault, self.state, package)
        self.assertEqual(self.version(), '3.0.0')
        self.assertTrue((self.state / 'update-journal.json').exists())
        with self.redirected():
            self.assert_view_is_faked()
            result = self.updater.recover(self.vault, self.state)
        self.assertEqual(result, {'status': 'recovered', 'version': '3.0.1'})
        self.assertFalse((self.state / 'update-journal.json').exists())
        self.assertEqual(json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))['version'], '3.0.1')
        with self.redirected():
            rolled = self.updater.rollback(self.vault, self.state)
        self.assertEqual(rolled['status'], 'rolled_back')
        self.assertEqual(self.version(), '3.0.0')

    def test_installer_resume_recovers_through_the_same_check(self):
        # install_v3.py hands a pending journal to recover(); that path must not be a closed loop.
        self.install()
        package = self.package()
        def crash(phase, index=None):
            if phase == 'after_replace' and index == 0:
                raise OSError('Synthetic interruption')
        with patch.object(self.updater, 'transaction_hook', crash):
            with self.assertRaises(OSError):
                self.updater.update(self.vault, self.state, package)
        installer = load(INSTALLER, 'beyin_install_redirect_test_subject')
        with self.redirected():
            result = installer.install(self.vault, self.state)
        self.assertTrue(result['install_resumed'])
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(self.version(), '3.0.1')
        pin = (self.vault / '.beyin-runtime.json').read_bytes()
        # The second run of a newer installer is how a stuck vault gets the fixed updater.
        with self.redirected():
            again = installer.install(self.vault, self.state, version='3.0.2')
        self.assertEqual(again['status'], 'installed')
        self.assertEqual(self.version(), '3.0.2')
        self.assertEqual((self.vault / '.beyin-runtime.json').read_bytes(), pin, 'the pin keeps its keys and value')
        self.assertFalse((self.state / 'update-journal.json').exists())

    def test_nested_targets_below_the_redirected_root_are_accepted(self):
        (self.state / 'hook-queue').mkdir(parents=True)
        (self.state / 'hook-queue' / 'entry.json').write_text('{}\n', encoding='utf-8')
        self.container.parent.mkdir(parents=True)
        try:
            os.symlink(self.local, self.container, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable on this runner')
        with self.redirected():
            for name in ('v3-install.json', 'hook-queue/entry.json', 'hook-queue/new/absent.json'):
                with self.subTest(name=name):
                    operation = {'scope': 'state', 'name': name}
                    self.assertEqual(self.updater._destination(self.vault, self.state, operation), self.state / name)

    def test_links_that_leave_the_root_are_still_rejected_under_the_redirected_view(self):
        self.state.mkdir(parents=True)
        self.container.parent.mkdir(parents=True)
        (self.state / 'plain').mkdir()  # outside the try: a fixture error must fail, not skip
        try:
            os.symlink(self.local, self.container, target_is_directory=True)
            os.symlink(self.outside, self.state / 'escape', target_is_directory=True)
            os.symlink(self.outside / 'v3-install.json', self.state / 'leaf.json')
            os.symlink(self.outside, self.vault / '.claude', target_is_directory=True)
            os.symlink(self.outside, self.state / 'plain' / 'deeper', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this runner')
        cases = [('state', 'escape/v3-install.json'), ('state', 'leaf.json'),
                 ('state', 'plain/deeper/v3-install.json'), ('state', 'escape/new/absent.json'),
                 ('vault', '.claude/scripts/beyin_v3.py')]
        with self.redirected():
            for scope, name in cases:
                with self.subTest(scope=scope, name=name):
                    with self.assertRaisesRegex(ValueError, 'transaction target escapes root'):
                        self.updater._destination(self.vault, self.state, {'scope': scope, 'name': name})
            for name in ('../outside/v3-install.json', 'plain/../../outside/v3-install.json',
                         str(self.outside / 'v3-install.json')):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, 'unsafe transaction path'):
                        self.updater._destination(self.vault, self.state, {'scope': 'state', 'name': name})
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['v3-install.json'])

    @unittest.skipUnless(os.name == 'nt', 'junctions exist only on Windows')
    def test_junctions_that_leave_the_root_are_still_rejected(self):
        import _winapi
        self.state.mkdir(parents=True)
        _winapi.CreateJunction(str(self.outside), str(self.state / 'junction'))
        with self.redirected():
            with self.assertRaisesRegex(ValueError, 'transaction target escapes root'):
                self.updater._destination(self.vault, self.state, {'scope': 'state', 'name': 'junction/v3-install.json'})


if __name__ == '__main__':
    unittest.main()
