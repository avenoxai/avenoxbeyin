"""P3 need preview: non-academic decisions and source-backed candidate review."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import v3_workspace_discovery_test as fixture
import v3_workspace_p2_test as p2_fixture
from v3_workspace_test_io import run_json_cli
from beyin_v3_sync import SyncEngine
from beyin_v3_workspace_discovery import preview
from beyin_v3_workspace_setup import execute
from beyin_v3_workspace_context import membership_hash


class NeedPreviewTest(unittest.TestCase):
    def setUp(self):
        self.case = fixture.DiscoveryTest('test_metadata_only_is_bounded_and_never_extracts_cataloged_files')
        self.case.setUp()
        self.addCleanup(self.case.tmp.cleanup)
        self.vault, self.state = self.case.vault, self.case.state

    def request(self, goal, **changes):
        request = self.case.request(goal=goal, decision=dict(kind='record_only',
            rationale='Existing work area already has the appropriate purpose and access.'))
        request.update(changes)
        return request

    def test_s1_bug_exact_source_is_candidate_without_a_write(self):
        selector = self.case.note('bug-17')
        SyncEngine(self.vault, self.state).sync()
        before = selector.copy()
        before_bytes = (self.vault / selector['relative_path']).read_bytes()
        result = preview(self.vault, self.state, self.request('Continue BUG-17 from the new log',
            evidence=[selector], candidate_record_ids=['bug-17']))
        self.assertEqual(result['record_candidates'][0]['relation'], 'exact_source')
        self.assertEqual(result['proposed_operations'][0]['kind'], 'record_only')
        self.assertTrue(result['writes_performed'] is False)
        self.assertEqual((self.vault / before['relative_path']).read_bytes(), before_bytes)
        self.assertFalse((self.state / 'workspace-contexts').exists())
        automatic = preview(self.vault, self.state, self.request('Continue BUG-17', evidence=[selector]))
        self.assertEqual([c['record_id'] for c in automatic['record_candidates']], ['bug-17'])

    def test_s2_revised_contract_remains_unconfirmed(self):
        self.case.note('accepted-contract')
        SyncEngine(self.vault, self.state).sync()
        result = preview(self.vault, self.state, self.request('Review revised customer scope',
            candidate_record_ids=['accepted-contract']))
        self.assertEqual(result['record_candidates'][0]['relation'], 'unconfirmed')
        self.assertIn('record_relation_unconfirmed:accepted-contract', result['review'])

    def test_s3_same_company_different_role_cannot_merge_by_name(self):
        self.case.note('role-engineer')
        SyncEngine(self.vault, self.state).sync()
        result = preview(self.vault, self.state, self.request('Track another position at the same company',
            candidate_record_ids=['role-engineer']))
        self.assertEqual(result['record_candidates'][0]['action'], 'review_relation')
        self.assertNotIn('update_record', json.dumps(result))

    def test_new_workspace_requires_review_when_existing_purpose_matches(self):
        request = self.request('Keep related bug investigations together', name='Gözlem evi',
            workspace_id='another-purpose', decision=dict(kind='new_workspace',
                rationale='A separate lifecycle might be needed.'), evidence=[])
        result = preview(self.vault, self.state, request)
        self.assertIn('existing_workspace_must_be_distinguished', result['review'])
        self.assertEqual(result['proposed_operations'], [])

    def test_denied_record_and_no_memory_are_not_exposed(self):
        self.case.note('private-cv', visibility='private')
        SyncEngine(self.vault, self.state).sync()
        request = self.request('Review application', candidate_record_ids=['private-cv'])
        result = preview(self.vault, self.state, request)
        self.assertEqual(result['record_candidates'], [])
        self.assertIn('record_unavailable_or_denied:private-cv', result['review'])
        before = self.case.tree()
        disabled = preview(self.vault, self.state, dict(request, no_memory=True))
        self.assertEqual(disabled['record_candidates'], [])
        self.assertEqual(disabled['review'], ['no_memory'])
        self.assertEqual(self.case.tree(), before)

    def test_revoked_grant_cannot_list_a_candidate(self):
        selector = self.case.note('customer-issue')
        SyncEngine(self.vault, self.state).sync()
        self.case.authority.revoke('synthetic-approval')
        result = preview(self.vault, self.state, self.request('Review customer issue',
            evidence=[selector], candidate_record_ids=['customer-issue']))
        self.assertEqual(result['record_candidates'], [])
        self.assertIn('candidate_scope_or_source_review', result['review'])

    def test_stale_index_record_cannot_be_nominated_by_old_hash(self):
        selector = self.case.note('bug-17')
        SyncEngine(self.vault, self.state).sync()
        path = self.vault / selector['relative_path']
        path.write_text(path.read_text(encoding='utf-8') + '\nNew unindexed source revision.\n', encoding='utf-8')
        result = preview(self.vault, self.state, self.request('Continue BUG-17',
            evidence=[selector], candidate_record_ids=['bug-17']))
        self.assertEqual(result['record_candidates'], [])
        self.assertIn('record_unavailable_or_denied:bug-17', result['review'])

    def test_p2_membership_revocation_removes_shared_candidate(self):
        case = p2_fixture.WorkspaceP2Test('test_explicit_owner_record_is_one_id_in_two_authorized_workspaces')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._authorize_owner_share()
        case._adopt(case.custom_spec, 'academic-learning')
        request = dict(goal='Review a shared technical note', workspace_id='custom',
            grant_ref='custom-user', evidence=[dict(source_id='custom-vault',
                relative_path='notes/academic-learning.md')],
            decision=dict(kind='record_only', rationale='The existing custom workspace has the right purpose.'),
            candidate_record_ids=['academic-learning'])
        before = preview(case.vault, case.state, request)
        self.assertEqual(before['record_candidates'][0]['record_id'], 'academic-learning')
        case.runtime.release_membership('academic-learning', 'custom', 'academic-user',
                                        membership_hash(case.runtime.registry))
        after = preview(case.vault, case.state, request)
        self.assertEqual(after['record_candidates'], [])

    def test_setup_action_returns_the_same_read_only_preview(self):
        result, code = execute(self.vault, self.state, 'preview', self.request(
            'Keep project issue records together', candidate_record_ids=[]))
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['operation_status'], 'planned')
        self.assertFalse((self.state / 'workspace-contexts').exists())

    def test_cli_accepts_preview_and_returns_read_only_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        before = self.case.tree()
        payload = self.request('Continue the existing issue', no_memory=True,
            candidate_record_ids=['bug-17'])
        process = run_json_cli([sys.executable, '-B', str(root / 'scripts/beyin_v3.py'),
            '--vault', str(self.vault), '--state', str(self.state),
            'workspace-setup', 'preview', '--file', '-'], input=json.dumps(payload),
            capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        detail = json.loads(next(i['detail'] for i in result['data']['issues']
                                 if i['code'] == 'setup_artifacts'))
        self.assertEqual(detail['review'], ['no_memory'])
        self.assertFalse(detail['writes_performed'])
        self.assertEqual(self.case.tree(), before)


if __name__ == '__main__':
    unittest.main()
