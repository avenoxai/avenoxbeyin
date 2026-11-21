"""Offline real-package lifecycle for schema-1 Defuddle consent.

Both synthetic versions contain today's runtime. This is not historical-release
security evidence: rolling back to a runtime predating consent removes its gate.
No Git history, external packages or network are required.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from v3_package_helpers import build_package, inherited_env, isolated_env


# Load the installed module in a fresh process, not the checkout's imported module.
PROBE = r'''
import io, json, pathlib, socket, sys
from contextlib import nullcontext
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import beyin_v3_yakala as y
state, mode = pathlib.Path(sys.argv[2]), sys.argv[3]
class Terminal(io.StringIO):
    def isatty(self):
        return True
if mode == 'approve':
    with patch('sys.stdin', Terminal('yes\n')):
        assert y._prompt_defuddle_approval(state)
else:
    # Noninteractive ready input must not create consent. No real helper or I/O.
    with patch.object(y, '_safe_network_url'), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('offline fixture')), \
         patch.object(y, '_HelperNetwork', side_effect=lambda: nullcontext()), \
         patch.object(y, '_run', return_value=(0, json.dumps({'content': 'x' * 100}), '')) as run, \
         patch('sys.stdin', io.StringIO('yes\n')):
        text = y.via_defuddle('https://example.com/article', {'npx': 'synthetic-npx'}, state=state)
        assert run.call_count == (1 if mode == 'valid' else 0), (mode, run.call_count)
        assert bool(text) == (mode == 'valid'), (mode, text)
'''


class ConsentPackageLifecycleTest(unittest.TestCase):
    def test_real_zip_update_and_rollback_preserve_consent_contract(self):
        with tempfile.TemporaryDirectory(prefix='consent-lifecycle-') as tmp:
            root = Path(tmp)
            env = inherited_env(**isolated_env(root / 'home'))
            for name in ('CONFIG', 'CACHE', 'DATA', 'STATE'):
                env['XDG_' + name + '_HOME'] = str(root / name.lower())
            env['XDG_RUNTIME_DIR'] = str(root / 'runtime-dir')

            def run(*args):
                result = subprocess.run([sys.executable, *map(str, args)], cwd=root, env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, encoding='utf-8', timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            old = build_package(root / 'old.zip', '3.0.0', env)
            new = build_package(root / 'new.zip', '3.0.1', env)
            unpacked = root / 'package'
            with zipfile.ZipFile(old) as package:
                package.extractall(unpacked)
            vault, state = root / 'Synthetic Vault', root / 'state'
            vault.mkdir()
            run(unpacked / 'scripts/install_v3.py', '--vault', vault, '--state', state,
                '--version', '3.0.0')
            runtime = vault / '.claude/scripts'
            approval = state / 'yakala/defuddle-approval.json'
            run('-c', PROBE, runtime, state, 'approve')
            saved = approval.read_bytes()
            record = json.loads(saved)
            self.assertEqual(record['schema'], 1)
            self.assertIs(record['approved'], True)

            for arguments, status, version in (
                    (('update', '--package', new), 'updated', '3.0.1'),
                    (('rollback',), 'rolled_back', '3.0.0')):
                with self.subTest(operation=arguments[0]):
                    result = json.loads(run(vault / 'beyin.py', *arguments))
                    self.assertEqual(result['status'], status)
                    self.assertEqual((vault / '.beyin-version').read_text().strip(), version)
                    self.assertEqual(approval.read_bytes(), saved)
                    run('-c', PROBE, runtime, state, 'valid')
                    self.assertEqual(approval.read_bytes(), saved)
                    invalid = {'missing': None, 'malformed': b'{', 'array': b'[]',
                               'unsupported-schema': json.dumps(dict(record, schema=2)).encode(),
                               'wrong-version': json.dumps(dict(record, version='defuddle@0.0.0')).encode()}
                    for mode, raw in invalid.items():
                        with self.subTest(record=mode):
                            if raw is None:
                                approval.unlink()
                            else:
                                approval.write_bytes(raw)
                            run('-c', PROBE, runtime, state, mode)
                            if raw is None:
                                self.assertFalse(approval.exists())
                            else:
                                self.assertEqual(approval.read_bytes(), raw)
                    approval.write_bytes(saved)


if __name__ == '__main__':
    unittest.main()
