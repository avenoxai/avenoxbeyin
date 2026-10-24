"""P4 authorized navigation and path-impact regressions."""
import json
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from v3_navigation_fixture import NavigationFixture
from beyin_v3_lint import scoped_vault_index, lint_vault, plan_link_impact, plan_moc, generate_moc
from beyin_v3_sync import SyncEngine, parse, render
from beyin_v3_workspaces import canonical_bytes, content_hash, envelope, render_document
from beyin_v3_workspace_context import request_scope
from beyin_v3_sources import SourceResolver

SPEC = importlib.util.spec_from_file_location('knowledge_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class ScopedNavigationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = NavigationFixture()
        self.addCleanup(self.fixture.close)
        self.vault, self.state = self.fixture.vault, self.fixture.state

    def note(self, source, body, **metadata):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(id=metadata.pop('id', Path(source).stem), workspace_id='observatory',
                    visibility='internal', trust='trusted', kind='note', revision=1)
        data.update(metadata)
        path.write_text(render(data, body), encoding='utf-8')
        return path

    def inventory(self):
        return scoped_vault_index(self.vault, self.state, 'observatory', 'synthetic-approval')

    def allow_memory_write(self):
        binding_path = self.state / 'workspace-bindings/memory.json'
        binding = json.loads(binding_path.read_text())
        binding['data']['grants'].append(dict(workspace_id='observatory', access='write_memory'))
        binding_path.write_bytes(canonical_bytes(binding))
        permission = dict(workspace_id='observatory', source_id='memory', locator=str(self.vault), access='write_memory')
        read_permission = dict(permission, access='read')
        self.fixture.authority.record_approval('synthetic-approval', machine_id='synthetic-machine',
            permissions=[read_permission, permission], audiences=('internal',),
            evidence='Synthetic read and write approval')

    def test_authorized_impact_keeps_unknown_and_unresolved_visible(self):
        self.note('notes/workspaces/target.md', '# Target', aliases=['Müşteri'])
        relation = dict(id='edge-1', workspace_id='observatory', type='related_to',
            **{'from': {'workspace_id': 'observatory', 'record_id': 'source'},
               'to': {'workspace_id': 'observatory', 'record_id': 'target'}}, evidence_refs=[])
        self.note('notes/workspaces/source.md', '[[target|Display]] [[Müşteri]] [[missing]] [[target#Absent]]',
                  workspace_relations=[relation])
        self.note('notes/hidden.md', 'SECRET-CANARY [[target]]', workspace_id='another')
        SyncEngine(self.vault, self.state).sync()
        index = self.inventory()
        self.assertNotIn('notes/hidden.md', index['texts'])
        plan = plan_link_impact(index, 'notes/workspaces/target.md', 'notes/workspaces/moved.md')
        self.assertEqual(plan['outside_inventory'], 'unknown')
        self.assertEqual(plan['canonical_aliases'], ['Müşteri'])
        self.assertEqual(plan['incoming_wikilinks'][0]['display_alias'], 'Display')
        self.assertIn('canonical_alias', [x['match_kind'] for x in plan['incoming_wikilinks']])
        self.assertIn('missing', [x['target'] for x in plan['known_unresolved']])
        self.assertEqual(plan['record_relations'][0]['relation_id'], 'edge-1')
        self.assertNotIn('SECRET-CANARY', json.dumps(plan))
        report = lint_vault(self.vault, vault_index=index)
        self.assertEqual(report['broken_links_count'], 1)
        self.assertEqual(report['unknown_links_count'], 1)

    def test_scoped_missing_wikilink_and_media_are_unknown_without_presence_leak(self):
        self.note('notes/workspaces/source.md', '[[other]] ![[diagram.png]]')
        self.note('notes/workspaces/other.md', '# Hidden', workspace_id='another')
        image = self.vault / 'notes/workspaces/diagram.png'
        image.write_bytes(b'IMAGE-CANARY')
        SyncEngine(self.vault, self.state).sync()
        report = lint_vault(self.vault, vault_index=self.inventory())
        self.assertEqual(report['status'], 'indeterminate')
        self.assertEqual(report['broken_links_count'], 0)
        self.assertEqual(report['unknown_links_count'], 2)
        self.assertEqual({x['type'] for x in report['unknown_links']},
                         {'unknown_wikilink', 'unknown_media'})
        self.assertNotIn('Hidden', json.dumps(report))
        self.assertNotIn('IMAGE-CANARY', json.dumps(report))
        image.unlink()
        report_without_media = lint_vault(self.vault, vault_index=self.inventory())
        self.assertEqual(report_without_media['unknown_links'], report['unknown_links'])

    def test_ambiguous_alias_and_strict_task_evidence_path_are_reported(self):
        self.note('notes/workspaces/proof.md', '# Proof', aliases=['Müşteri'])
        self.note('notes/workspaces/other.md', '# Other', aliases=['Müşteri'])
        self.note('notes/workspaces/source.md', '[[Müşteri]]')
        self.note('tasks/check.md', 'Check evidence.', kind='task', status='done', owner='Reviewer',
                  completion_contract='strict', completion_criterion='Evidence recorded.',
                  evidence_refs=['notes/workspaces/proof.md'])
        SyncEngine(self.vault, self.state).sync()
        index = self.inventory()
        plan = plan_link_impact(index, 'notes/workspaces/proof.md')
        self.assertEqual(plan['incoming_wikilinks'], [])
        self.assertIn('Müşteri', [x['target'] for x in plan['known_unresolved'] if x['reason'] == 'ambiguous'])
        self.assertIn(dict(source='tasks/check.md', field='evidence_refs', reference_kind='path'),
                      plan['historical_source_refs'])

    def test_destination_conflict_is_unknown_outside_authorized_inventory(self):
        self.note('notes/workspaces/target.md', '# Target')
        self.note('notes/workspaces/source.md', '# Source')
        self.note('notes/workspaces/hidden.md', 'SECRET-CANARY', workspace_id='another')
        SyncEngine(self.vault, self.state).sync()
        index = self.inventory()
        for destination in ('notes/workspaces/hidden.md', 'notes/workspaces/absent.md'):
            plan = plan_link_impact(index, 'notes/workspaces/target.md', destination)
            self.assertEqual(plan['destination_status'], 'unknown')
            self.assertIsNone(plan['destination_conflict'])
            self.assertNotIn('SECRET-CANARY', json.dumps(plan))
        known = plan_link_impact(index, 'notes/workspaces/target.md', 'notes/workspaces/source.md')
        self.assertEqual(known['destination_status'], 'known_conflict')
        self.assertIs(known['destination_conflict'], True)

    def test_revoked_grant_fails_closed_and_legacy_guard_remains(self):
        self.note('notes/workspaces/target.md', '# Target')
        SyncEngine(self.vault, self.state).sync()
        with self.assertRaisesRegex(ValueError, 'active_workspace_requires_scoped_context'):
            plan_moc(self.vault, 'notes/workspaces', state=self.state)
        self.assertEqual(len(self.inventory()['texts']), 1)
        self.fixture.authority.revoke('synthetic-approval')
        with self.assertRaisesRegex(Exception, 'scope_denied'):
            self.inventory()

    def test_stale_source_and_revocation_during_read_withhold_inventory(self):
        target = self.note('notes/workspaces/target.md', '# Target')
        SyncEngine(self.vault, self.state).sync()
        target.write_text(target.read_text() + '\nNew edit')
        index = self.inventory()
        self.assertEqual(index['texts'], {})
        self.assertGreater(index['scope']['stale_count'], 0)
        SyncEngine(self.vault, self.state).sync()
        original = SourceResolver.resolve
        calls = []
        def revoke_on_second(resolver, *args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                self.fixture.authority.revoke('synthetic-approval')
            return original(resolver, *args, **kwargs)
        with patch.object(SourceResolver, 'resolve', revoke_on_second):
            with self.assertRaisesRegex(Exception, 'scope_denied'):
                self.inventory()

    def test_revoked_source_binding_is_not_saved_by_another_live_binding(self):
        self.note('notes/workspaces/target.md', '# Target')
        SyncEngine(self.vault, self.state).sync()
        registry = self.fixture.registry
        registry.compare_and_save(envelope('SourceDescriptor',
            dict(id='z-memory', kind='vault', label='z-memory')), None)
        profile_path = self.vault / '.beyin-workspaces/workspaces/observatory.json'
        profile = json.loads(profile_path.read_text())
        prior = content_hash(profile_path.read_bytes())
        profile['data']['revision'] += 1
        profile['data']['source_ids'].append('z-memory')
        registry.compare_and_save(profile, prior)
        a_path = self.state / 'workspace-bindings/memory.json'
        a = json.loads(a_path.read_text())
        a['data']['authority_ref'] = 'synthetic-own-a'
        a_path.write_bytes(canonical_bytes(a))
        b = dict(a['data'], source_id='z-memory', authority_ref='synthetic-own-b')
        (self.state / 'workspace-bindings/z-memory.json').write_bytes(
            render_document(envelope('SourceBinding', b)))
        a_permission = dict(workspace_id='observatory', source_id='memory',
                            locator=str(self.vault), access='read')
        b_permission = dict(a_permission, source_id='z-memory')
        self.fixture.authority.record_approval('synthetic-own-a', machine_id='synthetic-machine',
            permissions=[a_permission], evidence='Synthetic A owner')
        self.fixture.authority.record_approval('synthetic-own-b', machine_id='synthetic-machine',
            permissions=[b_permission], evidence='Synthetic B owner')
        self.fixture.authority.record_approval('synthetic-approval', machine_id='synthetic-machine',
            permissions=[a_permission, b_permission], evidence='Synthetic two-source request')
        original = SourceResolver.resolve
        a_calls = []
        def revoke_after_a_read(resolver, *args, **kwargs):
            result = original(resolver, *args, **kwargs)
            if args[1] == 'memory':
                a_calls.append(args)
                if len(a_calls) == 2:
                    self.fixture.authority.revoke('synthetic-own-a')
            return result
        with patch.object(SourceResolver, 'resolve', revoke_after_a_read):
            with self.assertRaisesRegex(Exception, 'scope_denied'):
                self.inventory()
        self.assertEqual(len(a_calls), 2)
        self.assertTrue(self.fixture.authority.allows('synthetic-approval', b, 'observatory'))

    def test_path_derived_stable_id_preview_and_reference_impact_agree(self):
        target = self.note('notes/workspaces/path-note.md', '# Path note')
        metadata, body = parse(target.read_text())
        del metadata['id']
        target.write_text(render(metadata, body))
        self.note('notes/workspaces/source.md', '[[path-note]]')
        engine = SyncEngine(self.vault, self.state)
        engine.sync()
        index = self.inventory()
        impact = plan_link_impact(index, 'notes/workspaces/path-note.md',
                                  'notes/workspaces/moved.md')
        self.allow_memory_write()
        with request_scope('observatory', 'synthetic-approval'):
            stable = engine.preview_stable_id(impact['record_id'])
        self.assertEqual(stable['status'], 'ready')
        self.assertEqual(stable['stable_id'], impact['record_id'])
        self.assertEqual(stable['expected_source_sha256'], impact['source_sha256'])
        self.assertEqual(impact['incoming_wikilinks'][0]['source'], 'notes/workspaces/source.md')
        self.assertEqual(impact['outside_inventory'], 'unknown')

    def test_existing_moc_requires_write_grant_and_uses_shared_writer(self):
        self.note('notes/workspaces/target.md', '# Target')
        moc = self.note('notes/workspaces/MOC.md', '# Manual\n\nFooter')
        SyncEngine(self.vault, self.state).sync()
        index = self.inventory()
        engine = SyncEngine(self.vault, self.state)
        with self.assertRaisesRegex(Exception, 'scope_denied'):
            generate_moc(engine, 'notes/workspaces', vault_index=index,
                         workspace_id='observatory', grant_ref='synthetic-approval', source_id='memory')
        self.allow_memory_write()
        index = self.inventory()
        result = generate_moc(engine, 'notes/workspaces', vault_index=index,
                              workspace_id='observatory', grant_ref='synthetic-approval', source_id='memory')
        self.assertEqual(result['status'], 'written')
        self.assertIn('Footer', moc.read_text())
        self.assertIn('[[notes/workspaces/target.md|Target]]', moc.read_text())

    def test_new_moc_uses_canonical_create_under_explicit_write_grant(self):
        self.note('notes/workspaces/target.md', '# Target')
        SyncEngine(self.vault, self.state).sync()
        self.allow_memory_write()
        engine = SyncEngine(self.vault, self.state)
        result = generate_moc(engine, 'notes/workspaces', vault_index=self.inventory(),
                              workspace_id='observatory', grant_ref='synthetic-approval', source_id='memory')
        self.assertEqual(result['status'], 'written')
        moc = self.vault / 'notes/workspaces/MOC.md'
        self.assertIn('[[notes/workspaces/target.md|Target]]', moc.read_text())
        self.assertIn('workspace_id', moc.read_text())
        self.assertIn('notes/workspaces/MOC.md', self.inventory()['texts'])
        self.assertEqual(generate_moc(engine, 'notes/workspaces', vault_index=self.inventory(),
                                      workspace_id='observatory', grant_ref='synthetic-approval',
                                      source_id='memory')['status'], 'unchanged')

    def test_cli_scoped_routes_preserve_active_guard(self):
        self.note('notes/workspaces/target.md', '# Target')
        self.note('notes/workspaces/source.md', '[[target]] [[missing]]')
        self.note('notes/workspaces/MOC.md', '# Manual')
        SyncEngine(self.vault, self.state).sync()
        base = ['--vault', str(self.vault), '--state', str(self.state)]
        result, code = cli.main(base + ['lint'], return_result=True)
        self.assertEqual(code, 4, result)
        scoped = ['--workspace-id', 'observatory', '--grant-ref', 'synthetic-approval']
        result, code = cli.main(base + ['lint'] + scoped, return_result=True)
        self.assertEqual(code, 3, result)
        self.assertEqual(result['status'], 'indeterminate')
        self.assertEqual(result['scope']['outside_inventory'], 'unknown')
        result, code = cli.main(base + ['link-impact'] + scoped +
                                ['--source', 'notes/workspaces/target.md'], return_result=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['incoming_wikilinks'][0]['source'], 'notes/workspaces/source.md')
        result, code = cli.main(base + ['moc'] + scoped +
                                ['--folder', 'notes/workspaces', '--dry-run'], return_result=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'preview')
        for wid, grant in [('missing', 'synthetic-approval'), ('observatory', 'missing')]:
            denied, code = cli.main(base + ['link-impact', '--workspace-id', wid,
                '--grant-ref', grant, '--source', 'notes/workspaces/target.md'], return_result=True)
            self.assertNotEqual(code, 0)
            self.assertNotIn('Target', json.dumps(denied))
            self.assertNotIn('notes/workspaces/target.md', json.dumps(denied))


if __name__ == '__main__':
    unittest.main()
