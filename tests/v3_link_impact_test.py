"""P4 authorized navigation and path-impact regressions."""
import json
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import v3_workspace_discovery_test as fixture
from beyin_v3_lint import scoped_vault_index, lint_vault, plan_link_impact, plan_moc, generate_moc
from beyin_v3_sync import SyncEngine, parse, render
from beyin_v3_workspaces import canonical_bytes
from beyin_v3_workspace_context import request_scope
from beyin_v3_sources import SourceResolver

SPEC = importlib.util.spec_from_file_location('knowledge_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class ScopedNavigationTest(unittest.TestCase):
    def setUp(self):
        self.case = fixture.DiscoveryTest('test_metadata_only_is_bounded_and_never_extracts_cataloged_files')
        self.case.setUp()
        self.addCleanup(self.case.tmp.cleanup)
        self.vault, self.state = self.case.vault, self.case.state
        self.state.joinpath('workspace-runtime.json').write_text('{"enabled":true}')

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
        self.case.authority.record_approval('synthetic-approval', machine_id='synthetic-machine',
            permissions=[read_permission, permission,
                         dict(workspace_id='observatory', source_id='materials', locator=str(self.case.external), access='read')],
            audiences=('internal', 'private'), evidence='Synthetic read and write approval')

    def test_authorized_impact_keeps_unknown_and_unresolved_visible(self):
        self.note('notes/workspaces/target.md', '# Target', aliases=['Other name'])
        relation = dict(id='edge-1', workspace_id='observatory', type='related_to',
            **{'from': {'workspace_id': 'observatory', 'record_id': 'source'},
               'to': {'workspace_id': 'observatory', 'record_id': 'target'}}, evidence_refs=[])
        self.note('notes/workspaces/source.md', '[[target|Display]] [[missing]] [[target#Absent]]',
                  workspace_relations=[relation])
        self.note('notes/hidden.md', 'SECRET-CANARY [[target]]', workspace_id='another')
        SyncEngine(self.vault, self.state).sync()
        index = self.inventory()
        self.assertNotIn('notes/hidden.md', index['texts'])
        plan = plan_link_impact(index, 'notes/workspaces/target.md', 'notes/workspaces/moved.md')
        self.assertEqual(plan['outside_inventory'], 'unknown')
        self.assertEqual(plan['canonical_aliases'], ['Other name'])
        self.assertEqual(plan['incoming_wikilinks'][0]['display_alias'], 'Display')
        self.assertIn('missing', [x['target'] for x in plan['known_unresolved']])
        self.assertEqual(plan['record_relations'][0]['relation_id'], 'edge-1')
        self.assertNotIn('SECRET-CANARY', json.dumps(plan))
        self.assertEqual(lint_vault(self.vault, vault_index=index)['broken_links_count'], 2)

    def test_revoked_grant_fails_closed_and_legacy_guard_remains(self):
        self.note('notes/workspaces/target.md', '# Target')
        SyncEngine(self.vault, self.state).sync()
        with self.assertRaisesRegex(ValueError, 'active_workspace_requires_scoped_context'):
            plan_moc(self.vault, 'notes/workspaces', state=self.state)
        self.assertEqual(len(self.inventory()['texts']), 1)
        self.case.authority.revoke('synthetic-approval')
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
                self.case.authority.revoke('synthetic-approval')
            return original(resolver, *args, **kwargs)
        with patch.object(SourceResolver, 'resolve', revoke_on_second):
            with self.assertRaisesRegex(Exception, 'scope_denied'):
                self.inventory()

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
        self.assertEqual(code, 1, result)
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
