#!/usr/bin/env python3
"""scheduled-check: sync then doctor for an OS scheduler; no model call, nothing repaired."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]


class ScheduledCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-scheduled-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        (self.vault / 'notes').mkdir(parents=True)
        (self.vault / 'notes/a.md').write_text('---\n{"id": "a"}\n---\nSynthetic note.\n', encoding='utf-8')

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                               '--state', str(self.state), *args], capture_output=True, text=True,
                              encoding='utf-8', env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=120)

    def snapshot(self):
        return {path: path.read_bytes() for path in self.vault.rglob('*') if path.is_file()}

    def test_healthy_run_is_recorded_and_shown_by_doctor_without_touching_notes(self):
        self.assertEqual(json.loads(self.cli('doctor').stdout)['scheduled_check'], {'status': 'never_run'})
        before = self.snapshot()
        ran = self.cli('scheduled-check')
        self.assertEqual(ran.returncode, 0, ran.stderr)
        record = json.loads(ran.stdout)
        self.assertEqual((record['status'], record['sync_status']), ('ok', 'succeeded'))
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.state / 'scheduled-check.lock').exists())
        shown = json.loads(self.cli('doctor').stdout)['scheduled_check']
        self.assertEqual((shown['status'], shown['finished_at'], shown['age_hours']), ('ok', record['finished_at'], 0))

    def test_a_sync_conflict_exits_two(self):
        (self.vault / 'notes/b.md').write_text('---\n{"id": "a"}\n---\nSame id, other text.\n', encoding='utf-8')
        ran = self.cli('scheduled-check')
        self.assertEqual(ran.returncode, 2, ran.stdout + ran.stderr)
        self.assertEqual(json.loads(ran.stdout)['status'], 'needs_attention')

    def test_a_held_lock_skips_and_a_stale_one_is_reclaimed(self):
        self.state.mkdir(parents=True)
        lock = self.state / 'scheduled-check.lock'
        lock.write_text('123', encoding='ascii')
        held = self.cli('scheduled-check')
        self.assertEqual(held.returncode, 3, held.stderr)
        self.assertEqual(json.loads(held.stdout)['status'], 'already_running')
        self.assertFalse((self.state / 'scheduled-check.json').exists(), 'a skipped run records nothing')
        old = time.time() - 3 * 3600
        os.utime(lock, (old, old))
        reclaimed = self.cli('scheduled-check')
        self.assertEqual(reclaimed.returncode, 0, reclaimed.stderr)
        self.assertTrue(json.loads(reclaimed.stdout)['reclaimed_stale_lock'])
        self.assertFalse(lock.exists())


if __name__ == '__main__':
    unittest.main()
