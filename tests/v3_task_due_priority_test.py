#!/usr/bin/env python3
"""Tests for due_at priority, snapshot context, and closed task ordering (Issue #503)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from beyin_v3 import MemoryStore, is_due_task
import beyin_v3_companion as companion_mod


class TaskDuePriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-task-due-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.store = MemoryStore(self.state, self.vault)
        self.addCleanup(lambda: getattr(self.store, 'close', lambda: None)())

    def record(self, rid, **kwargs):
        data = {
            'id': rid,
            'source': f'notes/{rid}.md',
            'text': f'Content of {rid}',
            'visibility': 'internal',
            'status': 'active',
            'updated_at': '2026-10-15T10:00:00Z',
        }
        data.update(kwargs)
        path = self.vault / data['source']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data['text'], encoding='utf-8')
        self.store.ingest(data)
        return data

    def test_is_due_task_predicate(self):
        today = '2026-10-21'
        # Regular note with due_at is not a task
        note = {'kind': 'note', 'source': 'notes/n.md', 'status': 'active', 'due_at': '2026-10-20'}
        self.assertFalse(is_due_task(note, today))

        # Task in tasks/ directory with past/today date
        task_due = {'source': 'tasks/a.md', 'status': 'active', 'due_at': '2026-10-20'}
        self.assertTrue(is_due_task(task_due, today))

        task_today = {'kind': 'task', 'source': 'tasks/today.md', 'status': 'waiting', 'due_at': '2026-10-21T09:00:00+03:00'}
        self.assertTrue(is_due_task(task_today, today))

        task_inbox = {'kind': 'task', 'source': 'tasks/inbox.md', 'status': 'inbox', 'due_at': '2026-10-21'}
        self.assertTrue(is_due_task(task_inbox, today))

        # Future date is not due
        task_future = {'kind': 'task', 'source': 'tasks/f.md', 'status': 'active', 'due_at': '2026-10-22'}
        self.assertFalse(is_due_task(task_future, today))

        # Closed status is not due
        task_done = {'kind': 'task', 'source': 'tasks/d.md', 'status': 'done', 'due_at': '2026-10-20'}
        self.assertFalse(is_due_task(task_done, today))

        # Invalid date format does not crash
        task_invalid = {'kind': 'task', 'source': 'tasks/inv.md', 'status': 'active', 'due_at': 'not-a-date'}
        self.assertFalse(is_due_task(task_invalid, today))

    def test_snapshot_context_prioritizes_due_tasks_over_future_tasks_and_notes(self):
        today = '2026-10-21'
        # Populate notes and tasks exceeding limit (limit=5)
        for i in range(1, 6):
            self.record(f'note-{i}', source=f'notes/note{i}.md', text=f'Knowledge note {i}',
                        updated_at=f'2026-10-20T1{i}:00:00Z')

        # task-a: due today, but older updated_at and lower alphabetical id
        self.record('task-a', source='tasks/task-a.md', text='Urgent task due today', kind='task',
                    status='active', due_at='2026-10-21', updated_at='2026-10-01T00:00:00Z')

        # task-z: future task (due in 2027), but newer updated_at and higher alphabetical id
        self.record('task-z', source='tasks/task-z.md', text='Future task', kind='task',
                    status='active', due_at='2027-10-21', updated_at='2026-10-20T23:59:59Z')

        snapshot = self.store.snapshot_context(limit=5, today=today)
        ids = [r['id'] for r in snapshot['records']]
        # task-a MUST be first due to DUE_BONUS
        self.assertEqual(ids[0], 'task-a')
        self.assertIn('task-z', ids)

    def test_snapshot_context_includes_inbox_tasks(self):
        today = '2026-10-21'
        self.record('inbox-task', source='tasks/inbox-task.md', text='New inbox item',
                    kind='task', status='inbox', due_at=today)
        snapshot = self.store.snapshot_context(limit=5, today=today)
        ids = [r['id'] for r in snapshot['records']]
        self.assertIn('inbox-task', ids)

    def test_active_task_outranks_done_task_in_general_retrieve(self):
        # Both records match query "database migration", but done task is newer
        self.record('task-active', source='tasks/task-active.md', text='database migration instructions',
                    kind='task', status='active', updated_at='2026-10-10T10:00:00Z')
        self.record('task-done', source='tasks/task-done.md', text='database migration instructions',
                    kind='task', status='done', updated_at='2026-10-20T10:00:00Z')

        # With limit=1, active task MUST win despite older updated_at
        res = self.store.retrieve('database migration', limit=1)
        self.assertEqual([r['id'] for r in res['records']], ['task-active'])

        # With limit=2, done task is still accessible for queries
        res2 = self.store.retrieve('database migration', limit=2)
        self.assertEqual([r['id'] for r in res2['records']], ['task-active', 'task-done'])

    def test_closed_task_retrievable_when_uncontested_or_explicit(self):
        # Uncontested query returns closed task
        self.record('task-finished', source='tasks/finished.md', text='special unique query keyword',
                    kind='task', status='done')
        res = self.store.retrieve('special unique query keyword')
        self.assertEqual([r['id'] for r in res['records']], ['task-finished'])

        # Explicit statuses=['done'] does not penalize
        res_explicit = self.store.retrieve('special unique query keyword', statuses=['done'])
        self.assertEqual([r['id'] for r in res_explicit['records']], ['task-finished'])

    def test_companion_context_preserves_due_tasks_when_companion_clipped(self):
        today = '2026-10-21'
        companion_dir = self.vault / '🔮 850-Companion'
        companion_dir.mkdir(parents=True, exist_ok=True)
        # Create very large Kurallar.md so companion_clipped becomes True
        companion_files = {
            'Kurallar.md': '# Kurallar\n\n' + ('- Kural satir canary.\n' * 300),
            'Threads.md': '# Threads\n\n## Active Threads\n- Thread canary\n',
            'Core.md': '# Kimlik\nBen Ada.\n',
            'Last-Session.md': '# Son oturum\nOzet canary.\n',
        }
        for fname, content in companion_files.items():
            rel = f'🔮 850-Companion/{fname}'
            (self.vault / rel).write_text(content, encoding='utf-8')
            self.store.ingest({'id': fname, 'kind': 'note', 'status': 'active', 'text': content,
                               'source': rel, 'updated_at': '2026-10-20T10:00:00Z'})

        # Ambient note
        self.record('ambient-note', source='knowledge/arch.md', text='General architecture overview note.')

        # Due task
        self.record('urgent-task', source='tasks/urgent.md', text='Must resolve urgent server crash today.',
                    kind='task', status='active', due_at=today)

        # Build opening context with moderate budget
        output = companion_mod.context(self.store, 5000, 'session-12345678', 'claude')
        # Ambient note should yield budget, but urgent-task MUST be included
        self.assertIn('tasks/urgent.md', output)
        self.assertNotIn('knowledge/arch.md', output)


if __name__ == '__main__':
    unittest.main()
