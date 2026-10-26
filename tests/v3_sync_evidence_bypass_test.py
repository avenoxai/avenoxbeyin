import unittest
from pathlib import Path
import tempfile
import shutil
import sys
import os

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine


class TestSyncEvidenceBypass(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'vault'
        self.root.mkdir()
        self.state_dir = self.tmp / 'state'
        self.state_dir.mkdir()
        self.db = self.state_dir / 'index.sqlite'
        self.engine = SyncEngine(self.root, self.db)

        self.task_file = self.root / 'task.md'
        self.task_file.write_text('task body', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_symlink(self, target, link):
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest('Symlink privilege not held on this system')

    def test_identifier_truncated_at_scan_boundary_cannot_match(self):
        evidence = self.root / 'bounded.md'
        evidence.write_text(' ' * (1024 * 1024 - 3) + 'T-10', encoding='utf-8')
        issue = self.engine._completion_issue({'id': 'T-1', 'status': 'done',
            'completion_contract': 'strict', 'completion_criterion': 'result',
            'evidence_refs': ['bounded.md']}, 'task.md')
        self.assertIn('matching task identifier', issue)

    def test_update_rejects_identifier_followed_by_partial_utf8_at_scan_limit(self):
        self.engine.task_create('tasks/task.md', 'Synthetic task',
            {'id': 'T-1', 'owner': 'Reviewer', 'status': 'active',
             'completion_contract': 'strict', 'completion_criterion': 'Result recorded'})
        evidence = self.root / 'bounded.md'
        evidence.write_bytes(b' ' * (1024 * 1024 - 4) + 'T-1ş'.encode('utf-8'))
        with self.assertRaisesRegex(ValueError, 'matching task identifier'):
            self.engine.update_task('T-1', 1,
                {'status': 'done', 'evidence_refs': ['bounded.md']})

    def test_update_accepts_identifier_at_exact_scan_limit_eof(self):
        self.engine.task_create('tasks/task.md', 'Synthetic task',
            {'id': 'T-1', 'owner': 'Reviewer', 'status': 'active',
             'completion_contract': 'strict', 'completion_criterion': 'Result recorded'})
        evidence = self.root / 'bounded.md'
        evidence.write_bytes(b' ' * (1024 * 1024 - 3) + b'T-1')
        self.assertEqual(self.engine.update_task('T-1', 1,
            {'status': 'done', 'evidence_refs': ['bounded.md']})['status'], 'done')

    def test_evidence_scan_is_bounded_by_utf8_bytes(self):
        evidence = self.root / 'bounded.md'
        evidence.write_text('ş' * (512 * 1024) + ' T-1', encoding='utf-8')
        issue = self.engine._completion_issue({'id': 'T-1', 'status': 'done',
            'completion_contract': 'strict', 'completion_criterion': 'result',
            'evidence_refs': ['bounded.md']}, 'task.md')
        self.assertIn('matching task identifier', issue)

    def test_update_rejects_another_task_identifier_substring(self):
        self.engine.task_create('tasks/task.md', 'Synthetic task',
            {'id': 'T-1', 'owner': 'Reviewer', 'status': 'active',
             'completion_contract': 'strict', 'completion_criterion': 'Result recorded'})
        evidence = self.root / 'result.md'
        for text in ('Result for T-10 only', 'Result for OTHER-T-1 only', 'Result for T-1-extra'):
            evidence.write_text(text, encoding='utf-8')
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'matching task identifier'):
                self.engine.update_task('T-1', 1, {'status': 'done', 'evidence_refs': ['result.md']})
        evidence.write_text('Result for `T-1`: complete.', encoding='utf-8')
        self.assertEqual(self.engine.update_task('T-1', 1,
            {'status': 'done', 'evidence_refs': ['result.md']})['status'], 'done')

    def test_case_variation(self):
        evidence = self.root / 'Evidence.txt'
        evidence.write_text('evidence', encoding='utf-8')
        if (self.root / 'evidence.txt').exists():
            # NTFS and default APFS: both spellings are one existing source, so no issue is right.
            self.skipTest('case-insensitive volume')
        metadata = {
            'id': 'T-1',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['evidence.txt']
        }
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)

    def test_symlink_inside_to_outside(self):
        intermediate = self.root / 'folder'
        intermediate.mkdir()
        outside = self.tmp / 'outside.txt'
        outside.write_text('out', encoding='utf-8')
        link = intermediate / 'link.txt'
        self._make_symlink(outside, link)
        metadata = {
            'id': 'T-2',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['folder/link.txt']
        }
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)

    def test_symlink_loop_handled_without_crash(self):
        link1 = self.root / 'link1.txt'
        link2 = self.root / 'link2.txt'
        self._make_symlink('link2.txt', link1)
        self._make_symlink('link1.txt', link2)
        metadata = {
            'id': 'T-3',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['link1.txt']
        }
        # Calling _completion_issue on circular symlink should not raise unhandled RuntimeError
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)

    def test_dir_symlink(self):
        out_dir = self.tmp / 'out_dir'
        out_dir.mkdir()
        outside_file = out_dir / 'evidence.txt'
        outside_file.write_text('outside', encoding='utf-8')
        dir_link = self.root / 'dir_link'
        self._make_symlink(out_dir, dir_link)
        metadata = {
            'id': 'T-4',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['dir_link/evidence.txt']
        }
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)

    def test_double_dot_tricks(self):
        folder = self.root / 'folder'
        folder.mkdir()
        file = folder / 'file.txt'
        file.write_text('file', encoding='utf-8')
        metadata = {
            'id': 'T-5',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['folder/../folder/file.txt']
        }
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)


    def test_spoofed_evidence_ref_without_task_id(self):
        metadata = {
            'id': 'T-6',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['no_id.txt']
        }
        evidence = self.root / 'no_id.txt'
        evidence.write_text('evidence without id', encoding='utf-8')
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must contain the matching task identifier", issue)

    def test_spoofed_evidence_ref_with_task_id(self):
        metadata = {
            'id': 'T-7',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['with_id.txt']
        }
        evidence = self.root / 'with_id.txt'
        evidence.write_text('evidence with id T-7 inside', encoding='utf-8')
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIsNone(issue)

    def test_spoofed_evidence_ref_binary_file(self):
        metadata = {
            'id': 'T-8',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['binary.bin']
        }
        evidence = self.root / 'binary.bin'
        evidence.write_bytes(b'\x00\xff\x00\xff')
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must contain the matching task identifier", issue)

    def test_missing_evidence_ref(self):
        metadata = {
            'id': 'T-9',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['missing.txt']
        }
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("must be an existing vault source", issue)

    def test_spoofed_mtime_signature_manipulation(self):
        metadata = {
            'id': 'T-10',
            'title': 'Test',
            'status': 'done',
            'completion_contract': 'strict',
            'completion_criterion': 'done',
            'evidence_refs': ['mtime.txt']
        }
        evidence = self.root / 'mtime.txt'
        evidence.write_text('evidence with id T-10 inside', encoding='utf-8')
        
        # Test future mtime manipulation
        import time
        os.utime(evidence, (evidence.stat().st_atime, time.time() + 3600))
        issue = self.engine._completion_issue(metadata, 'task.md')
        self.assertIn("invalid future modification time", issue)


if __name__ == '__main__':
    unittest.main()
