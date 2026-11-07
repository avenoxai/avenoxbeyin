"""Corrupt user hook configuration must never be replaced by an empty object."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'template/.claude/scripts'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_launchers as launchers
import render_antigravity_hooks as antigravity
import render_codex_hooks as codex


class HookRendererInputTest(unittest.TestCase):
    def test_missing_python_executable_is_rejected(self):
        for executable in (None, ''):
            with self.subTest(executable=executable), patch.object(sys, 'executable', executable):
                with self.assertRaisesRegex(ValueError, 'python-executable-missing'):
                    launchers.plan_launchers('/unused-vault', {})

    def test_corrupt_or_unreadable_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            adapter = vault / '.claude/scripts/antigravity_hooks.py'
            adapter.parent.mkdir(parents=True)
            adapter.touch()
            for module, folder in ((antigravity, '.agents'), (codex, '.codex')):
                destination = vault / folder / 'hooks.json'
                destination.parent.mkdir()
                for raw in (b'{"user-hook": broken', b'\xff', b'[1, 2]'):
                    with self.subTest(folder=folder, raw=raw):
                        destination.write_bytes(raw)
                        with self.assertRaises(ValueError):
                            module.write(vault, 'posix')
                        self.assertEqual(destination.read_bytes(), raw)
                with patch.object(Path, 'read_text', side_effect=PermissionError('test read denial')):
                    with self.assertRaises(PermissionError):
                        module.write(vault, 'posix')
                self.assertEqual(destination.read_bytes(), raw)
                destination.write_text(json.dumps({'user-key': {'keep': True}}), encoding='utf-8')
                module.write(vault, 'posix')
                self.assertEqual(json.loads(destination.read_text(encoding='utf-8'))['user-key'], {'keep': True})


if __name__ == '__main__':
    unittest.main()
