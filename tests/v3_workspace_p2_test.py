"""P2 acceptance: one canonical record, separate membership and edit ownership."""
import copy
from pathlib import Path
import json
import tempfile
import unittest

from v3_workspace_helpers import FIX, install_fixture
from v3_workspace_test_io import load_utf8_json
from beyin_v3_workspace_context import membership_hash, request_scope
from beyin_v3_workspace_memory import MemoryIntentService
from beyin_v3_workspaces import WorkspaceError, canonical_bytes, content_hash, envelope
from beyin_v3_sync import SyncEngine, parse, render


class WorkspaceP2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.runtime, self.academic_spec, self.academic_request = install_fixture(self.base, 'academic')
        _, self.custom_spec, self.custom_request = install_fixture(self.base, 'custom')
        _, self.career_spec, self.career_request = install_fixture(self.base, 'career')
        self.vault = self.runtime.registry.vault
        self.state = self.runtime.registry.state
        # The shipped fixtures predate canonical create and have no owner field.
        # Promote two fixture records to the explicit-owner case P2 must support.
        for source in ('notes/academic-learning.md', 'tasks/academic-task.md'):
            path = self.vault/source
            metadata, body = parse(path.read_text(encoding='utf-8'))
            metadata['workspace_id'] = 'academic'
            path.write_text(render(metadata, body), encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        self.runtime.activate()

    def _subject_request(self, request, record_id):
        document = copy.deepcopy(request)
        wid = document['data']['signals'][0]['workspace_id']
        document['data']['signals'][0]['subject_refs'] = [{'workspace_id': wid, 'record_id': record_id}]
        document['data']['query'] = record_id
        return document

    def _record(self, request, record_id):
        context = self.runtime.context(self._subject_request(request, record_id))['data']
        return context, next((r for r in context['records'] if r['id'] == record_id), None)

    def _adopt(self, spec, record_id):
        document = copy.deepcopy(spec)
        wid = document['desired_profile']['id']
        document['adopt_record_refs'] = [{'workspace_id': wid, 'record_id': record_id}]
        return self.runtime.adopt(envelope('WorkspaceSpec', document))

    def _authorize_owner_share(self):
        """The adopting reference has explicit owner write and member read authority."""
        authority = self.runtime.authority
        previous = authority._load()['grants']['custom-user']
        owner_binding = self.runtime.registry.bindings()[0]['academic-vault']
        owner_permission = {'workspace_id': 'academic', 'source_id': 'academic-vault',
                            'locator': owner_binding['locator'], 'access': 'write_memory'}
        authority.record_approval('custom-user', machine_id='fixture-machine',
                                  permissions=[owner_permission], audiences=('internal',),
                                  evidence='Synthetic explicit owner-approved sharing', preserve_current=True,
                                  expected_grant_sha256=content_hash(canonical_bytes(previous)))

    def _intent(self, request, context, record, action, *, key):
        document = copy.deepcopy(next(d for d in load_utf8_json(FIX/'academic/golden.json')['documents']
                                  if d['type'] == 'MemoryWriteIntent'))
        wid = request['data']['signals'][0]['workspace_id']
        change = copy.deepcopy(next(c for c in document['data']['changes'] if c['action'] == action))
        change['target'].update(record_id=record['id'], relative_path=record['source_ref']['relative_path'],
                                source_id=wid+'-vault', expected_revision=record['revision'],
                                expected_source_sha256=record['source_ref']['content_sha256'])
        change['evidence_refs'] = []
        change['relation_changes'] = []
        if action == 'update_note':
            change['body'] = 'P2 owner update.'
        document['data'].update(workspace_id=wid, context_token=context['context_token'],
                                profile_revision=context['profile_revision'],
                                binding_snapshot_sha256=context['binding_snapshot_sha256'],
                                idempotency_key=key, changes=[change])
        return document

    def test_explicit_owner_record_is_one_id_in_two_authorized_workspaces(self):
        canonical = self.vault/'notes/academic-learning.md'
        before = canonical.read_bytes()
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        owner, owned = self._record(self.academic_request, 'academic-learning')
        shared, member = self._record(self.custom_request, 'academic-learning')
        _, outsider = self._record(self.career_request, 'academic-learning')
        self.assertEqual(owned['id'], member['id'])
        self.assertEqual(owned['source_ref']['content_sha256'], member['source_ref']['content_sha256'])
        self.assertEqual(owner['workspace_id'], 'academic')
        self.assertEqual(shared['workspace_id'], 'custom')
        self.assertIsNone(outsider)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertFalse((self.vault/'notes/custom-learning-copy.md').exists())

    def test_explicit_owner_adoption_requires_owner_write_authority_on_same_reference(self):
        path = self.vault/'notes/academic-learning.md'
        before = path.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied|adoption_authority_required|adoption_scope_conflict'):
            self._adopt(self.custom_spec, 'academic-learning')
        self.assertEqual(path.read_bytes(), before)
        _, denied = self._record(self.custom_request, 'academic-learning')
        self.assertIsNone(denied)
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        _, shared = self._record(self.custom_request, 'academic-learning')
        self.assertEqual(shared['id'], 'academic-learning')

    def test_shared_read_and_workspace_write_grant_do_not_transfer_edit_ownership(self):
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        self._adopt(self.custom_spec, 'academic-task')
        self.runtime.context(self.custom_request)  # Grant is live for this workspace.
        service = MemoryIntentService(self.runtime)
        for record_id, action in [('academic-learning', 'update_note'), ('academic-task', 'update_task')]:
            context, record = self._record(self.custom_request, record_id)
            path = self.vault/record['source_ref']['relative_path']
            before = path.read_bytes()
            intent = self._intent(self.custom_request, context, record, action, key='p2/member/'+record_id)
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied|edit_owner_required'):
                service.apply(intent, _refresh_views=False)
            self.assertEqual(path.read_bytes(), before)
        engine = SyncEngine(self.vault, self.state)
        with request_scope('custom', 'custom-user'):
            source = 'notes/academic-learning.md'
            before = engine.note_read(source)
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied|edit_owner_required'):
                engine.note_edit({'source': source, 'expected_sha256': before['sha256'],
                                  'edits': [{'old': 'Karmaşıklık', 'new': 'UNAUTHORIZED'}]})
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied|edit_owner_required'):
                engine.update_task('academic-task', 1, {'next_action': 'UNAUTHORIZED'})

    def test_owner_update_keeps_canonical_identity_and_member_reads_new_revision(self):
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        context, record = self._record(self.academic_request, 'academic-learning')
        intent = self._intent(self.academic_request, context, record, 'update_note', key='p2/owner/update')
        result = MemoryIntentService(self.runtime).apply(intent, _refresh_views=False)['data']
        self.assertEqual(result['operation_status'], 'applied')
        _, updated = self._record(self.custom_request, 'academic-learning')
        self.assertEqual(updated['id'], record['id'])
        self.assertEqual(updated['revision'], record['revision']+1)
        self.assertIn('P2 owner update.', updated['text'])

    def test_legacy_unscoped_adoption_still_reads_in_two_workspaces(self):
        path = self.vault/'notes/p2-legacy.md'
        path.write_text(render({'id': 'p2-legacy', 'kind': 'note', 'revision': 1,
                                'visibility': 'internal'}, 'Shared legacy method.'), encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        self._adopt(self.academic_spec, 'p2-legacy')
        self._adopt(self.custom_spec, 'p2-legacy')
        for request in (self.academic_request, self.custom_request):
            _, record = self._record(request, 'p2-legacy')
            self.assertEqual(record['id'], 'p2-legacy')
        _, denied = self._record(self.career_request, 'p2-legacy')
        self.assertIsNone(denied)

    def test_legacy_single_member_can_edit_but_two_members_need_owner_decision(self):
        path = self.vault/'notes/p2-legacy-edit.md'
        path.write_text(render({'id': 'p2-legacy-edit', 'kind': 'note', 'revision': 1,
                                'visibility': 'internal'}, 'Original legacy text.'), encoding='utf-8')
        engine = SyncEngine(self.vault, self.state)
        engine.sync()
        self._adopt(self.academic_spec, 'p2-legacy-edit')
        with request_scope('academic', 'academic-user'):
            before = engine.note_read('notes/p2-legacy-edit.md')
            engine.note_edit({'source': 'notes/p2-legacy-edit.md', 'expected_sha256': before['sha256'],
                              'edits': [{'old': 'Original legacy text.', 'new': 'Single owner edit.'}]})
        self._adopt(self.custom_spec, 'p2-legacy-edit')
        before = path.read_bytes()
        with request_scope('academic', 'academic-user'):
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied|edit_owner_required'):
                engine.note_edit({'source': 'notes/p2-legacy-edit.md',
                                  'expected_sha256': content_hash(before),
                                  'edits': [{'old': 'Single owner edit.', 'new': 'Ambiguous edit.'}]})
        self.assertEqual(path.read_bytes(), before)
        old_membership = membership_hash(self.runtime.registry)
        self.runtime.assign_legacy_owner('p2-legacy-edit', 'academic', 'academic-user', old_membership)
        with self.assertRaisesRegex(WorkspaceError, 'membership_changed'):
            self.runtime.assign_legacy_owner('p2-legacy-edit', 'custom', 'custom-user', old_membership)
        with request_scope('academic', 'academic-user'):
            engine.note_edit({'source': 'notes/p2-legacy-edit.md',
                              'expected_sha256': content_hash(before),
                              'edits': [{'old': 'Single owner edit.', 'new': 'Assigned owner edit.'}]})
        with request_scope('custom', 'custom-user'):
            with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
                engine.note_edit({'source': 'notes/p2-legacy-edit.md',
                                  'expected_sha256': content_hash(path.read_bytes()),
                                  'edits': [{'old': 'Assigned owner edit.', 'new': 'Unauthorized.'}]})

    def test_membership_change_invalidates_old_context_and_sessions_stay_separate(self):
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        old, record = self._record(self.custom_request, 'academic-learning')
        self.assertIsNotNone(record)
        self.runtime.release_membership('academic-learning', 'custom', 'academic-user',
                                        membership_hash(self.runtime.registry))
        _, denied = self._record(self.custom_request, 'academic-learning')
        self.assertIsNone(denied)
        # A token issued before membership revocation cannot authorize a later write.
        stale = self._intent(self.custom_request, old, record, 'update_note', key='p2/stale/adoption')
        with self.assertRaisesRegex(WorkspaceError, 'context_changed'):
            MemoryIntentService(self.runtime)._context(stale['data'])
        first = self.runtime.auto_context('codex', 'Algoritmalar ödev', session_id='p2-one')['data']
        second = self.runtime.auto_context('codex', 'Düşük ışık çekim', session_id='p2-two')['data']
        self.assertEqual(first['workspace_id'], 'academic')
        self.assertEqual(second['workspace_id'], 'custom')
        self.assertEqual(self.runtime.auto_context('codex', 'p2unknowncontinuation', session_id='p2-one')['data']['workspace_id'], 'academic')

    def test_two_sessions_cannot_overwrite_same_revision(self):
        first, record = self._record(self.academic_request, 'academic-learning')
        second, same = self._record(self.academic_request, 'academic-learning')
        one = self._intent(self.academic_request, first, record, 'update_note', key='p2/session/one')
        two = self._intent(self.academic_request, second, same, 'update_note', key='p2/session/two')
        two['data']['changes'][0]['body'] = 'Stale parallel update.'
        service = MemoryIntentService(self.runtime)
        service.apply(one, _refresh_views=False)
        with self.assertRaisesRegex(WorkspaceError, 'revision_conflict|context_changed'):
            service.apply(two, _refresh_views=False)

    def test_project_label_does_not_choose_between_two_workspace_purposes(self):
        # The same external project may be bound to two purposes; a project label
        # alone is neither an explicit workspace choice nor a new grant.
        registry = self.runtime.registry
        for wid in ('academic', 'custom'):
            source = '.beyin-workspaces/workspaces/'+wid+'.json'
            document = registry.read(self.vault, source, 'WorkspaceProfile')
            profile = copy.deepcopy(document['data'])
            profile['revision'] += 1
            profile['extensions']['project_id'] = 'shared-project'
            registry.compare_and_save(envelope('WorkspaceProfile', profile),
                                      content_hash((self.vault/source).read_bytes()))
        result = self.runtime.auto_context('codex', 'p2unknowncontinuation', session_id='p2-project', project='shared-project')['data']
        self.assertIn(result['state'], ('ambiguous', 'denied'))
        self.assertIsNone(result['workspace_id'])
        first = self.runtime.auto_context('codex', 'Algoritmalar ödev', session_id='p2-project-one', project='shared-project')['data']
        second = self.runtime.auto_context('codex', 'Düşük ışık çekim', session_id='p2-project-two', project='shared-project')['data']
        self.assertEqual(first['workspace_id'], 'academic')
        self.assertEqual(second['workspace_id'], 'custom')

    def test_unknown_project_does_not_fall_back_to_an_unrelated_workspace(self):
        context = self.runtime.auto_context('codex', 'Algoritmalar ödev',
                                            session_id='p2-unknown-project',
                                            project='unbound-project')['data']
        self.assertEqual(context['state'], 'denied')
        self.assertIsNone(context['workspace_id'])
        self.assertEqual(context['records'], [])

    def test_same_session_identifier_in_another_client_has_no_continuity(self):
        first = self.runtime.auto_context('codex', 'Algoritmalar ödev',
                                          session_id='shared-session')['data']
        self.assertEqual(first['workspace_id'], 'academic')
        other = self.runtime.auto_context('claude', 'p2unknowncontinuation',
                                          session_id='shared-session')['data']
        self.assertIsNone(other['workspace_id'])
        resumed = self.runtime.auto_context('codex', 'p2unknowncontinuation',
                                            session_id='shared-session')['data']
        self.assertEqual(resumed['workspace_id'], 'academic')

    def test_legacy_membership_layout_migrates_on_write_without_rewriting_record(self):
        path = self.vault/'notes/p2-migration.md'
        path.write_text(render({'id': 'p2-migration', 'kind': 'note', 'revision': 1,
                                'visibility': 'internal'}, 'Existing canonical note.'), encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        before = path.read_bytes()
        sidecar = self.state/'workspace-adoptions.json'
        for old in ({'p2-migration': ['academic']},
                    {'contract_version': 'workspace-membership/0.1.0',
                     'records': {'p2-migration': ['academic']}}):
            sidecar.write_text(json.dumps(old), encoding='utf-8')
            _, record = self._record(self.academic_request, 'p2-migration')
            self.assertEqual(record['id'], 'p2-migration')
            self._adopt(self.custom_spec, 'p2-migration')
            migrated = json.loads(sidecar.read_text(encoding='utf-8'))
            self.assertEqual(migrated['contract_version'], 'workspace-membership/0.2.0')
            self.assertEqual(migrated['records']['p2-migration'], ['academic', 'custom'])
            self.assertEqual(migrated['owners'], {})
            self.assertEqual(path.read_bytes(), before)

    def test_unknown_membership_version_fails_closed(self):
        sidecar = self.state/'workspace-adoptions.json'
        sidecar.write_text(json.dumps({'contract_version': 'workspace-membership/99.0.0',
                                       'records': {}, 'owners': {}}), encoding='utf-8')
        with self.assertRaisesRegex(WorkspaceError, 'invalid_membership_state'):
            self.runtime.context(self.academic_request)

    def test_revoke_requires_current_owner_grant_and_exact_cas(self):
        self._authorize_owner_share()
        self._adopt(self.custom_spec, 'academic-learning')
        expected = membership_hash(self.runtime.registry)
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            self.runtime.release_membership('academic-learning', 'custom', 'career-user', expected)
        self.runtime.release_membership('academic-learning', 'custom', 'academic-user', expected)
        with self.assertRaisesRegex(WorkspaceError, 'membership_changed'):
            self.runtime.release_membership('academic-learning', 'custom', 'academic-user', expected)


if __name__ == '__main__':
    unittest.main()
