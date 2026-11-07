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


if __name__ == '__main__':
    unittest.main()
