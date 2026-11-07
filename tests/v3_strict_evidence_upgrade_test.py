"""Strict evidence identity is checked when evidence is linked, never retroactively (#494)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine, render
from v3_package_helpers import inherited_env

CRITERION = 'A reviewed synthetic result is recorded.'


class StrictEvidenceUpgradeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-strict-evidence-')
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.vault = self.tmp / 'vault'
        self.vault.mkdir()
        self.state = self.tmp / 'state'
        self.engine = SyncEngine(self.vault, self.state)

    def note(self, source, text):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return source

    def closed_before_upgrade(self, refs, contract='strict'):
        """The canonical source an earlier task-update wrote when it closed the task."""
        metadata = {'id': 'T-1', 'title': 'Synthetic task', 'owner': 'Synthetic Reviewer',
                    'status': 'done', 'kind': 'task', 'revision': 2,
                    'updated_at': '2026-10-01', 'created_at': '2026-10-01', 'evidence_refs': refs}
        if contract:
            metadata.update(completion_contract=contract, completion_criterion=CRITERION)
        (self.vault / 'tasks').mkdir(exist_ok=True)
        (self.vault / 'tasks/t.md').write_text(render(metadata, 'Synthetic task.\n'), encoding='utf-8')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')

    def doctor(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True,
                                encoding='utf-8', env=inherited_env(BEYIN_UPDATES_OFF='1'), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_completed_strict_task_with_legacy_evidence_stays_valid(self):
        self.closed_before_upgrade([self.note('notes/result.md', 'Synthetic result without the identifier.\n')])
        self.assertEqual(self.engine.completion_health()['strict_issue_count'], 0)
        self.assertEqual(self.doctor()['task_completion']['strict_issue_count'], 0)
        # An unrelated edit, or restating the refs it already has, links no new evidence.
        self.assertEqual(self.engine.update_task('T-1', 2, {'priority': 'low'})['revision'], 3)
        self.assertEqual(self.engine.update_task('T-1', 3, {'evidence_refs': ['notes/./result.md']})['revision'], 4)

    def test_evidence_added_to_a_completed_task_must_name_it(self):
        legacy = self.note('notes/result.md', 'Synthetic result without the identifier.\n')
        self.closed_before_upgrade([legacy])
        self.note('notes/other.md', 'Result for T-10 only.\n')
        with self.assertRaisesRegex(ValueError, 'matching task identifier'):
            self.engine.update_task('T-1', 2, {'evidence_refs': [legacy, 'notes/other.md']})
        self.note('notes/named.md', 'Follow-up result for T-1 recorded\n')
        updated = self.engine.update_task('T-1', 2, {'evidence_refs': [legacy, 'notes/named.md']})
        self.assertEqual(updated['evidence_refs'], [legacy, 'notes/named.md'])

    def test_closing_again_judges_every_ref(self):
        legacy = self.note('notes/result.md', 'Synthetic result without the identifier.\n')
        self.closed_before_upgrade([legacy])
        self.engine.update_task('T-1', 2, {'status': 'active'})
        with self.assertRaisesRegex(ValueError, 'matching task identifier'):
            self.engine.update_task('T-1', 3, {'status': 'done'})
        self.note('notes/result.md', 'Synthetic result for T-1 recorded\n')
        self.assertEqual(self.engine.update_task('T-1', 3, {'status': 'done'})['status'], 'done')

    def test_opting_a_completed_task_in_judges_its_evidence(self):
        legacy = self.note('notes/result.md', 'Synthetic result without the identifier.\n')
        self.closed_before_upgrade([legacy], contract=None)
        opt_in = {'completion_contract': 'strict', 'completion_criterion': CRITERION}
        with self.assertRaisesRegex(ValueError, 'matching task identifier'):
            self.engine.update_task('T-1', 2, opt_in)
        self.note('notes/result.md', 'Synthetic result for T-1 recorded\n')
        self.assertEqual(self.engine.update_task('T-1', 2, opt_in)['completion_contract'], 'strict')

    def test_task_created_done_must_name_its_evidence(self):
        metadata = {'id': 'T-2', 'owner': 'Synthetic Reviewer', 'status': 'done',
                    'completion_contract': 'strict', 'completion_criterion': CRITERION,
                    'evidence_refs': [self.note('notes/result.md', 'Unrelated synthetic note.\n')]}
        with self.assertRaisesRegex(ValueError, 'matching task identifier'):
            self.engine.task_create('tasks/t2.md', 'Synthetic task.', metadata)
        self.assertFalse((self.vault / 'tasks/t2.md').exists())
        self.note('notes/result.md', 'Result recorded for T-2 here\n')
        self.assertEqual(self.engine.task_create('tasks/t2.md', 'Synthetic task.', metadata)['status'], 'done')

    def test_future_timestamp_tolerates_time_zone_and_clock_skew(self):
        self.engine.task_create('tasks/t.md', 'Synthetic task.', {
            'id': 'T-1', 'owner': 'Synthetic Reviewer', 'status': 'active',
            'completion_contract': 'strict', 'completion_criterion': CRITERION})
        evidence = self.vault / self.note('notes/result.md', 'Result recorded for T-1 here\n')
        close = {'status': 'done', 'evidence_refs': ['notes/result.md']}
        os.utime(evidence, (time.time(), time.time() + 30 * 24 * 3600))
        with self.assertRaisesRegex(ValueError, 'future modification time'):
            self.engine.update_task('T-1', 1, close)
        # A local-time FAT stamp read 26 hours across UTC offsets, or a skewed synced copy.
        os.utime(evidence, (time.time(), time.time() + 26 * 3600))
        self.assertEqual(self.engine.update_task('T-1', 1, close)['status'], 'done')
        # A later skewed stamp on accepted evidence does not reopen the question.
        os.utime(evidence, (time.time(), time.time() + 30 * 24 * 3600))
        self.assertEqual(self.engine.completion_health()['strict_issue_count'], 0)
        self.assertEqual(self.engine.update_task('T-1', 2, {'priority': 'low'})['revision'], 3)

    def test_decomposed_evidence_names_a_composed_identifier(self):
        task_id = 'görev-ş1'
        metadata = {'id': task_id, 'owner': 'Synthetic Reviewer', 'status': 'active',
                    'completion_contract': 'strict', 'completion_criterion': CRITERION}
        source = self.vault / 'tasks/turkish.md'
        source.parent.mkdir()
        source.write_text(render(dict(metadata, kind='task', revision=1), 'Synthetic task.\n'), encoding='utf-8')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.note('notes/nfd.md', unicodedata.normalize('NFD', 'Sonuç: ' + task_id + ' tamamlandı.\n'))
        self.assertEqual(self.engine.update_task(task_id, 1, {'status': 'done', 'evidence_refs': ['notes/nfd.md']})
                         ['status'], 'done')

    def test_truncated_scan_over_a_long_identifier_run_is_linear(self):
        """A long [\\w.-] run ending before the scan limit must not backtrack quadratically."""
        limit = 1024 * 1024
        self.note('notes/large.md', 'T-1 ' + 'a' * (limit - 6) + '  ' + 'beyond the scan limit\n')
        self.engine.task_create('tasks/t.md', 'Synthetic task.', {
            'id': 'T-1', 'owner': 'Synthetic Reviewer', 'status': 'active',
            'completion_contract': 'strict', 'completion_criterion': CRITERION})
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from beyin_v3_sync import SyncEngine; '
                'e = SyncEngine(sys.argv[2], sys.argv[3]); '
                'print(e.update_task("T-1", 1, {"status": "done", "evidence_refs": ["notes/large.md"]})["status"])')
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'template/.claude/scripts'),
                                 str(self.vault), str(self.state)], capture_output=True, text=True,
                                encoding='utf-8', env=inherited_env(BEYIN_UPDATES_OFF='1'), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'done')


if __name__ == '__main__':
    unittest.main()
