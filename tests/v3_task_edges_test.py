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

    def test_task_files_from_older_versions_stay_indexed(self):
        """Status and date checks guard task-create/task-update only. A task file an older
        version wrote (or a hand edit) and an Obsidian `updated` date must not drop out of
        the index on the next sync."""
        (self.vault / 'tasks').mkdir()
        (self.vault / 'tasks/hand.md').write_text(
            '---\nid: legacy-task\nkind: task\ntitle: Elle\nstatus: todo\nowner: Tester\n'
            'due_at: 2026-10-10 sabah\nrevision: 1\n---\nElle yazilmis gorev.\n', encoding='utf-8')
        (self.vault / 'obsidian.md').write_text(
            '---\nupdated: 2026-10-08 (Persembe)\n---\nObsidian notu.\n', encoding='utf-8')
        result = self.engine.sync()
        self.assertEqual(result['warnings'], [])
        with self.engine.store._connect() as db:
            ids = {row[0] for row in db.execute('SELECT id FROM records')}
        self.assertIn('legacy-task', ids)
        self.assertEqual(len(ids), 2)

    def test_task_create_checks_dates_the_same_on_every_python(self):
        for due in ('2026-10-10 sabah', '2026-10-15T24:00:00', '2025-13-45'):
            with self.subTest(due=due), self.assertRaisesRegex(ValueError, 'due_at must be an ISO date or timestamp'):
                self.engine.task_create('tasks/dated.md', 'Body.', dict(self.metadata, due_at=due))
        created = self.engine.task_create('tasks/dated.md', 'Body.',
                                          dict(self.metadata, due_at='2026-10-15T09:30:00+03:00'))
        self.assertEqual(created['due_at'], '2026-10-15T09:30:00+03:00')

if __name__ == '__main__':
    unittest.main()
