import json
import unittest
import tempfile
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_sync import SyncEngine, parse

class TaskEdgesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.metadata = {
            'id': 'edge-task',
            'kind': 'task',
            'title': 'Edge case task',
            'status': 'active',
            'owner': 'Tester',
            'project': 'atlas',
            'visibility': 'internal'
        }

    def test_invalid_status_in_update_task(self):
        self.engine.task_create('tasks/edge.md', 'Test body.', self.metadata)
        with self.assertRaisesRegex(ValueError, "valid explicit task status required"):
            self.engine.update_task('edge-task', 1, {'status': 'archived'})

    def test_duplicate_task_id(self):
        self.engine.task_create('tasks/edge.md', 'Test body.', self.metadata)
        t2 = self.vault / 'tasks/t2.md'
        t2.write_text('---\n' + json.dumps(self.metadata) + '\n---\nTest body 2.', encoding='utf-8')
        result = self.engine.sync()
        self.assertEqual(result['status'], 'conflict')
        self.assertTrue(any(c['id'] == 'edge-task' and 'duplicate source id' in c['reason'] for c in result['conflicts']))

    def test_invalid_date_format_in_due_at(self):
        self.engine.task_create('tasks/edge.md', 'Test body.', self.metadata)
        with self.assertRaisesRegex(ValueError, "due_at must be an ISO date or timestamp"):
            self.engine.update_task('edge-task', 1, {'due_at': '2025-13-45'})

    def test_invalid_date_format_in_updated_at(self):
        self.engine.task_create('tasks/edge.md', 'Test body.', self.metadata)
        with self.assertRaisesRegex(ValueError, "updated_at must be an ISO date or timestamp"):
            self.engine.update_task('edge-task', 1, {'updated_at': '2025-13-45'})

if __name__ == '__main__':
    unittest.main()
