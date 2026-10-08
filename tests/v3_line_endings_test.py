"""A CRLF rewrite of a managed file must not block install, uninstall or rollback."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
REWRITTEN = b'kullanici bu dosyayi bastan yazdi\n'


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class LineEndingGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-line-endings-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'Örnek Beyin'
        self.vault.mkdir()
        self.state = self.base / 'state'
        self.snapshot = self.base / 'snapshot'
        self.installer = module(ROOT / 'scripts/install_v3.py', 'beyin_line_ending_installer')
        self.updater = module(ROOT / 'template/.claude/scripts/beyin_v3_update.py', 'beyin_line_ending_updater')
        self.installer.install(self.vault, self.state)
        # A second install makes the rollback journal restore real bytes instead of absence,
        # so deleting a managed file is a rollback conflict rather than the intended end state.
        self.installer.install(self.vault, self.state)
        self.managed = list(json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))['files'])
        self.save()

    def save(self):
        shutil.rmtree(self.snapshot, ignore_errors=True)
        self.snapshot.mkdir()
        shutil.copytree(self.vault, self.snapshot / 'vault')
        shutil.copytree(self.state, self.snapshot / 'state')
        self.saved = self.fingerprint()

    def restore(self):
        # Rewrites only what a gate changed: on Windows every fresh copy is rescanned on first
        # open, so recopying the whole tree per subtest made these tests dominate the suite.
        live = self.fingerprint()
        for path in live.keys() - self.saved.keys():
            path.unlink()
        for path, stamp in self.saved.items():
            if live.get(path) != stamp:
                path.parent.mkdir(parents=True, exist_ok=True)
                source = self.snapshot / ('vault' if self.vault in path.parents else 'state') / path.relative_to(
                    self.vault if self.vault in path.parents else self.state)
                path.write_bytes(source.read_bytes())
                self.saved[path] = self.stamp(path)

    @staticmethod
    def stamp(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    def fingerprint(self):
        # stat only: checking what a gate wrote must not reopen every file.
        return {path: self.stamp(path) for root in (self.vault, self.state)
                for path in root.rglob('*') if path.is_file()}

    def crlf(self, name):
        path = self.vault / name
        data = path.read_bytes()
        # The Windows launcher ships as CRLF, so there the rewrite goes the other way.
        flipped = data.replace(b'\r\n', b'\n') if b'\r\n' in data else data.replace(b'\n', b'\r\n')
        self.assertNotEqual(flipped, data, name + ' has no line endings to rewrite')
        path.write_bytes(flipped)

    def mutate(self, name, case):
        path = self.vault / name
        if case == 'crlf':
            self.crlf(name)
        elif case == 'edited':
            path.write_bytes(REWRITTEN)
        elif case == 'edited_crlf':
            path.write_bytes(REWRITTEN.replace(b'\n', b'\r\n'))
        else:
            path.unlink()

    def reinstall(self):
        return self.installer.install(self.vault, self.state, plan_only=True)

    def uninstall(self):
        return self.installer.install(self.vault, self.state, uninstall=True)

    def rollback(self):
        return self.updater.rollback(self.vault, self.state)

    def test_managed_set_is_not_empty(self):
        self.assertGreater(len(self.managed), 30)

    def test_crlf_rewrite_keeps_every_gate_open(self):
        for name in self.managed:
            for gate, run in (('install', self.reinstall), ('uninstall', self.uninstall), ('rollback', self.rollback)):
                with self.subTest(file=name, gate=gate):
                    self.crlf(name)
                    try:
                        run()
                    finally:
                        self.restore()

    def test_real_change_still_conflicts_on_every_gate(self):
        for name in self.managed:
            for case in ('edited', 'edited_crlf', 'deleted'):
                for gate, run in (('install', self.reinstall), ('uninstall', self.uninstall), ('rollback', self.rollback)):
                    with self.subTest(file=name, case=case, gate=gate):
                        self.mutate(name, case)
                        before = self.fingerprint()
                        try:
                            with self.assertRaises(ValueError):
                                run()
                            # A conflict must stop the gate before it writes anything.
                            self.assertEqual(self.fingerprint(), before)
                        finally:
                            self.restore()

    def test_conflict_message_names_the_case(self):
        target = '.claude/scripts/beyin_v3_hook.py'
        for case, expected in (('edited', 'content differs'), ('deleted', 'deleted')):
            for gate, run, prefix in (('install', self.reinstall, 'Reinstall conflict: managed file changed '),
                                      ('uninstall', self.uninstall, 'Uninstall conflict: managed file changed; preserve and reconcile '),
                                      ('rollback', self.rollback, 'rollback conflict: changed managed file ')):
                with self.subTest(case=case, gate=gate):
                    self.restore()
                    self.mutate(target, case)
                    with self.assertRaises(ValueError) as raised:
                        run()
                    self.assertIn(prefix + target, str(raised.exception))
                    self.assertIn(expected, str(raised.exception))

    def test_accepted_crlf_file_is_rewritten_to_the_hashed_stock_bytes(self):
        stock = {name: (self.vault / name).read_bytes() for name in self.managed}
        for name in self.managed:
            self.crlf(name)
        self.installer.install(self.vault, self.state)
        manifest = json.loads((self.state / 'v3-install.json').read_text(encoding='utf-8'))['files']
        for name in self.managed:
            with self.subTest(file=name):
                data = (self.vault / name).read_bytes()
                # Stock bytes, whichever ending the stock file ships with.
                self.assertEqual(data, stock[name])
                self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[name]['installed_hash'])
                self.assertEqual(base64.b64decode(manifest[name]['installed_content']), data)

    def test_uninstall_after_crlf_restores_the_pre_install_state(self):
        for name in self.managed:
            self.crlf(name)
        self.assertEqual(self.uninstall()['status'], 'uninstalled')
        for name in self.managed:
            self.assertFalse((self.vault / name).exists(), name)


if __name__ == '__main__':
    unittest.main()
