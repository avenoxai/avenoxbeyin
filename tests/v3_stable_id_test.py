"""P4 preparation: a path-derived note keeps its canonical ID before a move."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from beyin_v3_sync import SyncEngine, parse, render, RevisionConflict
from beyin_v3_workspaces import WorkspaceError


class StableIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.engine = SyncEngine(self.vault, root / 'state')
        self.source = 'notes/customer.md'
        self.path = self.vault / self.source
        self.path.parent.mkdir()
        self.body = 'Accepted scope and original evidence.\r\n'
        self.path.write_text(self.body, encoding='utf-8')
        self.engine.sync()
        self.record_id = 'md-' + hashlib.sha256(self.source.encode()).hexdigest()[:24]

    def test_preview_apply_preserves_identity_and_body(self):
        preview = self.engine.preview_stable_id(self.record_id)
        self.assertEqual(preview['status'], 'ready')
        self.assertEqual(preview['stable_id'], self.record_id)
        self.assertEqual(self.path.read_bytes(), self.body.encode())
        linked = self.vault / 'notes' / 'reference.md'
        linked.write_text('See [[customer]] and record ' + self.record_id + '.\n', encoding='utf-8')
        historical = linked.read_bytes()
        result = self.engine.adopt_stable_id(preview)
        self.assertEqual(result['status'], 'succeeded')
        metadata, body = parse(self.path.read_bytes().decode('utf-8'))
        self.assertEqual(metadata['id'], self.record_id)
        self.assertEqual(body, self.body)
        self.assertEqual(linked.read_bytes(), historical)
        self.assertEqual(self.engine.preview_stable_id(self.record_id)['status'], 'already_stable')
        moved = self.vault / 'notes' / 'renamed.md'
        self.path.rename(moved)
        self.engine.sync()
        with self.engine.store._connect() as db:
            row = db.execute('SELECT source FROM markdown_sources WHERE id=?', (self.record_id,)).fetchone()
        self.assertEqual(row[0], 'notes/renamed.md')

    def test_user_edit_and_plan_tampering_fail_before_write(self):
        preview = self.engine.preview_stable_id(self.record_id)
        edited = dict(preview, stable_id='different')
        with self.assertRaisesRegex(RevisionConflict, 'plan changed'):
            self.engine.adopt_stable_id(edited)
        edited = dict(preview, plan_sha256='0' * 64)
        with self.assertRaisesRegex(RevisionConflict, 'plan changed'):
            self.engine.adopt_stable_id(edited)
        self.path.write_text(self.body + 'Manual edit.\n', encoding='utf-8')
        with self.assertRaisesRegex(RevisionConflict, 'source changed'):
            self.engine.adopt_stable_id(preview)
        self.assertNotIn('id:', self.path.read_text(encoding='utf-8'))

    def test_revoked_grant_or_changed_owner_blocks_apply(self):
        preview = self.engine.preview_stable_id(self.record_id)
        before = self.path.read_bytes()
        for reason in ('scope_denied', 'edit_owner_required'):
            with self.subTest(reason=reason):
                with patch.object(self.engine, '_workspace_write_guard', side_effect=WorkspaceError(reason)):
                    with self.assertRaisesRegex(RevisionConflict, 'plan changed'):
                        self.engine.adopt_stable_id(preview)
                self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_and_task_sources_are_not_adopted(self):
        self.path.write_text(render({'id': 'known', 'kind': 'note'}, self.body), encoding='utf-8')
        self.engine.sync()
        self.assertEqual(self.engine.preview_stable_id('known')['status'], 'already_stable')
        self.path.write_text(render({'kind': 'task', 'revision': 1, 'status': 'active', 'owner': 'Can'}, self.body), encoding='utf-8')
        self.engine.sync()
        with self.assertRaisesRegex(ValueError, 'dedicated writer'):
            self.engine.preview_stable_id(self.record_id)


if __name__ == '__main__':
    unittest.main()
