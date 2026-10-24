"""P4 preparation: a path-derived note keeps its canonical ID before a move."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import copy

from beyin_v3_sync import SyncEngine, parse, render, RevisionConflict
from beyin_v3_workspaces import WorkspaceError, envelope
from beyin_v3_workspace_context import membership_hash, request_scope
from v3_workspace_helpers import install_fixture


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
        self.path.write_bytes(self.body.encode('utf-8'))
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
        self.path.write_bytes((self.body + 'Manual edit.\n').encode('utf-8'))
        with self.assertRaisesRegex(RevisionConflict, 'source changed'):
            self.engine.adopt_stable_id(preview)
        self.assertNotIn('id:', self.path.read_text(encoding='utf-8'))

    def test_revoked_grant_or_changed_owner_blocks_apply(self):
        preview = self.engine.preview_stable_id(self.record_id)
        before = self.path.read_bytes()
        for reason in ('scope_denied', 'edit_owner_required'):
            with self.subTest(reason=reason):
                with patch.object(self.engine, '_workspace_write_guard', side_effect=WorkspaceError(reason)):
                    with self.assertRaisesRegex(WorkspaceError, reason):
                        self.engine.adopt_stable_id(preview)
                self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_and_task_sources_are_not_adopted(self):
        self.path.write_bytes(render({'id': 'known', 'kind': 'note'}, self.body).encode('utf-8'))
        self.engine.sync()
        self.assertEqual(self.engine.preview_stable_id('known')['status'], 'already_stable')
        self.path.write_bytes(render({'kind': 'task', 'revision': 1, 'status': 'active', 'owner': 'Can'}, self.body).encode('utf-8'))
        self.engine.sync()
        with self.assertRaisesRegex(ValueError, 'dedicated writer'):
            self.engine.preview_stable_id(self.record_id)

    def test_private_and_untrusted_notes_do_not_expose_preview_metadata(self):
        for metadata in ({'visibility': 'private'}, {'trust': 'untrusted'}):
            with self.subTest(metadata=metadata):
                self.path.write_bytes(render(metadata, self.body).encode('utf-8'))
                self.engine.sync()
                with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
                    self.engine.preview_stable_id(self.record_id)


class WorkspaceStableIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime, self.spec, _ = install_fixture(self.base, 'academic')
        install_fixture(self.base, 'custom')
        self.vault = self.runtime.registry.vault
        self.engine = SyncEngine(self.vault, self.runtime.registry.state)
        self.source = 'notes/stable-prep.md'
        self.path = self.vault / self.source
        self.path.write_text('Workspace owner evidence.\n', encoding='utf-8')
        self.engine.sync()
        self.record_id = 'md-' + hashlib.sha256(self.source.encode()).hexdigest()[:24]
        spec = copy.deepcopy(self.spec)
        spec['adopt_record_refs'] = [{'workspace_id': 'academic', 'record_id': self.record_id}]
        self.runtime.adopt(envelope('WorkspaceSpec', spec), membership_hash(self.runtime.registry))
        self.runtime.activate()

    def test_owner_applies_and_unrelated_member_cannot_probe_identity(self):
        before = self.path.read_bytes()
        with request_scope('custom', 'custom-user'):
            for record_id in (self.record_id, 'md-nonexistent'):
                with self.subTest(record_id=record_id):
                    with self.assertRaisesRegex(WorkspaceError, '^scope_denied$'):
                        self.engine.preview_stable_id(record_id)
        with request_scope('academic', 'academic-user'):
            preview = self.engine.preview_stable_id(self.record_id)
            self.assertEqual(preview['expected_source_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(preview['workspace_membership_sha256'], membership_hash(self.runtime.registry))
            self.assertEqual(self.engine.adopt_stable_id(preview)['record_id'], self.record_id)
        self.assertEqual(parse(self.path.read_bytes().decode())[0]['id'], self.record_id)

    def test_live_grant_revoke_and_membership_change_invalidate_preview(self):
        with request_scope('academic', 'academic-user'):
            preview = self.engine.preview_stable_id(self.record_id)
        before = self.path.read_bytes()
        # Another legitimate adoption changes the membership projection, even
        # though the target note's source bytes remain untouched.
        other = self.vault / 'notes' / 'other.md'
        other.write_text('Another note.\n', encoding='utf-8')
        self.engine.sync()
        other_id = 'md-' + hashlib.sha256('notes/other.md'.encode()).hexdigest()[:24]
        spec = copy.deepcopy(self.spec)
        spec['adopt_record_refs'] = [{'workspace_id': 'academic', 'record_id': other_id}]
        self.runtime.adopt(envelope('WorkspaceSpec', spec), membership_hash(self.runtime.registry))
        with request_scope('academic', 'academic-user'):
            with self.assertRaisesRegex(RevisionConflict, 'plan changed'):
                self.engine.adopt_stable_id(preview)
            fresh = self.engine.preview_stable_id(self.record_id)
        self.runtime.authority.revoke('academic-user')
        with request_scope('academic', 'academic-user'):
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
                self.engine.adopt_stable_id(fresh)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
