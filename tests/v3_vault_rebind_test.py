"""A moved vault and its state index (#593).

memory.sqlite3 records the vault root it was built for and refuses any other one. Rerunning
the installer for a moved vault used to report "installed" and leave that binding behind, so
doctor, sync and every lifecycle hook stopped with "runtime belongs to another vault". The
installer now stops before writing anything, and --rebind-state moves the binding only when
the recorded folder is gone (or is this folder under another name), never onto a second copy
that still uses the same state.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, inherited_env, isolated_env

INSTALLER = ROOT / 'scripts/install_v3.py'
CLI = ROOT / 'scripts/beyin_v3.py'
NOTE = '---\nid: rebind-note\nkind: fact\nstatus: active\n---\nŞifre politikası: Müşteri arşivi yerel kalır.\n'


def binding(state):
    with closing(sqlite3.connect(Path(state) / 'memory.sqlite3')) as db:
        return db.execute("SELECT value FROM metadata WHERE key='vault_root'").fetchone()[0]


def counts(database):
    with closing(sqlite3.connect(database)) as db:
        return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('records', 'events'))


class VaultRebindTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-vault-rebind-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.env = inherited_env(**isolated_env(self.base / 'home'), BEYIN_UPDATES_OFF='1')
        self.old = self.base / 'Örnek Beyin'
        self.new = self.base / 'Taşınan Beyin'
        self.state = self.base / 'state dışarıda'
        (self.old / 'notes').mkdir(parents=True)
        (self.old / 'notes/kural.md').write_text(NOTE, encoding='utf-8')
        self.ok(INSTALLER, '--vault', self.old, '--state', self.state)
        self.ok(CLI, '--vault', self.old, '--state', self.state, 'sync')
        self.before = counts(self.state / 'memory.sqlite3')
        self.assertGreater(self.before[0], 0)

    def run_python(self, script, *args):
        # cwd outside every vault: Windows cannot rename a folder a process stands in.
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                              encoding='utf-8', env=self.env, cwd=self.base, timeout=120)

    def ok(self, script, *args):
        result = self.run_python(script, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def refused(self, *args):
        result = self.run_python(INSTALLER, *args)
        self.assertEqual(result.returncode, 1, result.stdout)
        return json.loads(result.stderr)['message']

    def move(self):
        os.rename(self.old, self.new)

    def snapshot(self, vault, state):
        files = {}
        for root in (vault, state):
            for path in sorted(Path(root).rglob('*')):
                if path.is_file():
                    files[str(path)] = path.read_bytes()
        return files

    def test_moved_vault_without_the_flag_stops_before_any_write(self):
        self.move()
        before = self.snapshot(self.new, self.state)
        message = self.refused('--vault', self.new, '--state', self.state)
        self.assertIn(str(self.old), message)
        self.assertIn(str(self.new), message)
        self.assertIn('--rebind-state', message)
        self.assertEqual(self.snapshot(self.new, self.state), before)
        # The plan an installed `beyin.py update` asks for stops the same way.
        self.assertIn('--rebind-state', self.refused('--vault', self.new, '--state', self.state, '--plan'))
        self.assertEqual(binding(self.state), str(self.old))

    def test_runtime_error_names_the_rebind_step(self):
        self.move()
        result = self.run_python(CLI, '--vault', self.new, '--state', self.state, 'doctor')
        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stderr)
        self.assertEqual(error['message'], 'runtime belongs to another vault')
        self.assertIn('--rebind-state', error['hint'])

    def test_rebind_keeps_the_index_and_backs_it_up(self):
        self.move()
        plan = self.ok(INSTALLER, '--vault', self.new, '--state', self.state, '--rebind-state', '--plan')
        self.assertEqual(plan['rebind'], {'from': str(self.old), 'to': str(self.new)})
        self.assertEqual(binding(self.state), str(self.old))
        self.assertFalse((self.state / 'memory-before-rebind.sqlite3').exists())

        result = self.ok(INSTALLER, '--vault', self.new, '--state', self.state, '--rebind-state')
        backup = self.state / 'memory-before-rebind.sqlite3'
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(result['rebound'], {'from': str(self.old), 'to': str(self.new), 'backup': str(backup)})
        self.assertEqual(binding(self.state), str(self.new))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='vault_root'").fetchone()[0], str(self.old))
        self.assertEqual(counts(backup), self.before)
        self.assertEqual(counts(self.state / 'memory.sqlite3'), self.before)
        self.ok(CLI, '--vault', self.new, '--state', self.state, 'doctor')
        synced = self.ok(CLI, '--vault', self.new, '--state', self.state, 'sync')
        self.assertEqual(synced['status'], 'succeeded')
        self.assertEqual(counts(self.state / 'memory.sqlite3')[0], self.before[0])
        pin = json.loads((self.new / '.beyin-runtime.json').read_text(encoding='utf-8'))
        self.assertEqual(Path(pin['state']).resolve(), self.state.resolve())
        hooks = (self.new / '.claude/settings.local.json').read_text(encoding='utf-8')
        self.assertNotIn(self.old.name, hooks)
        # Done once: a rerun has nothing to rebind and needs no flag.
        again = self.ok(INSTALLER, '--vault', self.new, '--state', self.state)
        self.assertNotIn('rebound', again)
        second = self.ok(INSTALLER, '--vault', self.new, '--state', self.state, '--rebind-state')
        self.assertNotIn('rebound', second)
        self.assertFalse((self.state / 'memory-before-rebind.1.sqlite3').exists())

    def test_a_copy_never_takes_the_state_its_original_still_uses(self):
        shutil.copytree(self.old, self.new, symlinks=True)
        before = self.snapshot(self.new, self.state)
        message = self.refused('--vault', self.new, '--state', self.state, '--rebind-state')
        self.assertIn(str(self.old), message)
        self.assertIn('still exists', message)
        self.assertEqual(self.snapshot(self.new, self.state), before)
        self.assertEqual(binding(self.state), str(self.old))
        self.ok(CLI, '--vault', self.old, '--state', self.state, 'doctor')

    def test_a_copy_installed_with_a_copy_of_the_state_gets_its_own_index(self):
        shutil.copytree(self.old, self.new, symlinks=True)
        copy = self.base / 'state kopyası'
        shutil.copytree(self.state, copy)
        result = self.ok(INSTALLER, '--vault', self.new, '--state', copy, '--rebind-state')
        self.assertEqual(result['rebound']['to'], str(self.new))
        self.assertEqual(binding(copy), str(self.new))
        self.assertEqual(binding(self.state), str(self.old))
        self.ok(CLI, '--vault', self.new, '--state', copy, 'doctor')
        self.ok(CLI, '--vault', self.old, '--state', self.state, 'doctor')

    def test_another_vault_cannot_take_a_gone_vaults_state(self):
        self.move()
        other = self.base / 'Başka Vault'
        other.mkdir()
        message = self.refused('--vault', other, '--state', self.state, '--rebind-state')
        self.assertIn('.beyin-runtime.json', message)
        self.assertEqual(binding(self.state), str(self.old))
        self.assertFalse((other / '.beyin-runtime.json').exists())

    def test_a_recorded_path_from_another_os_is_refused(self):
        self.move()
        foreign = '/home/eski/Beynim' if os.name == 'nt' else 'C:\\Users\\eski\\Beynim'
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key='vault_root'", (foreign,))
        message = self.refused('--vault', self.new, '--state', self.state, '--rebind-state')
        self.assertIn('not an absolute path', message)
        self.assertEqual(binding(self.state), foreign)

    def test_the_same_folder_under_another_name_is_rebound(self):
        self.move()
        try:
            os.symlink(self.new, self.old, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('directory links unavailable: ' + str(exc))
        result = self.ok(INSTALLER, '--vault', self.new, '--state', self.state, '--rebind-state')
        self.assertEqual(result['rebound']['from'], str(self.old))
        self.assertEqual(binding(self.state), str(self.new))

    def test_the_flag_needs_an_explicit_state_and_an_install(self):
        self.move()
        for extra in ([], ['--state', self.state, '--uninstall'], ['--state', self.state, '--reconcile-hooks']):
            result = self.run_python(INSTALLER, '--vault', self.new, '--rebind-state', *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('--rebind-state', result.stderr)
        self.assertEqual(binding(self.state), str(self.old))


if __name__ == '__main__':
    unittest.main()
