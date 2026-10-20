import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'template/.claude/scripts/beyin_v3_hook.py'


class HookFuzzingTest(unittest.TestCase):
    def setUp(self):
        # A per-test temporary root: fixed /tmp paths collide between parallel runs and on Windows
        # land in the drive root; rmtree on them could delete a folder the test never created.
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-fuzz-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.state = Path(self.tmp.name) / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        # Without this an agent session's BEYIN_V3_SKIP answers {} before any parsing (#169).
        self.env = inherited_env()

    def test_hook_malformed_inputs_never_crash(self):
        payloads = [
            b"",
            b"{",
            b"\xef\xbb\xbf{}",
            b'{"foo": "' + b'a' * 1100000 + b'"}',
        ]
        for p in payloads:
            with self.subTest(size=len(p)):
                res = subprocess.run([
                    sys.executable, str(HOOK),
                    '--vault', str(self.vault),
                    '--state', str(self.state),
                    '--harness', 'codex'
                ], input=p, capture_output=True, env=self.env, timeout=60)
                self.assertEqual(res.returncode, 0, f"Hook crashed with payload size {len(p)}! stderr: {res.stderr.decode('utf-8', 'replace')}")
                self.assertIsInstance(json.loads(res.stdout), dict)


if __name__ == '__main__':
    unittest.main()
