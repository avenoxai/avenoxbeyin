"""Tests for Conversational Workspace Builder (RFC #368 P0-D)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3 as runtime
import beyin_v3_projects as projects
import beyin_v3_workspaces as workspaces
from v3_package_helpers import inherited_env

CLI_SPEC = importlib.util.spec_from_file_location("beyin_cli", ROOT / "scripts/beyin_v3.py")
cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(cli)


class WorkspaceBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def test_positional_dry_run_leaves_vault_untouched(self):
        before = list(self.vault.iterdir())
        result = workspaces.provision_workspace(self.vault, 'preview', 'general', None, None, None, None, True)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(list(self.vault.iterdir()), before)

    def test_provision_binds_note_root_to_workspace(self):
        workspaces.provision_workspace(self.vault, 'alpha')
        scopes = projects.read_project_scopes(self.vault)
        self.assertEqual(runtime.record_scope({'source': 'notes/alpha/note.md'}, scopes), 'alpha')

    def test_provision_with_source_keeps_rules_inside_the_workspace_scope(self):
        # --source replaces the default project folder as a source, but the rules and MOC
        # templates still live there; left unbound they joined every project via shared_unscoped.
        (self.vault / 'clients/beta').mkdir(parents=True)
        (self.vault / 'clients/beta/b.md').write_text('# Beta\nBeta notu.\n', encoding='utf-8')
        scope_file = self.vault / projects.PROJECT_SCOPES_FILE
        scope_file.write_text(json.dumps({'folders': {'clients/beta': 'beta'}, 'shared_unscoped': True}),
                              encoding='utf-8')
        result, code = cli.main(['--vault', str(self.vault), '--state', str(self.state), 'workspace', 'provision',
                                 'alpha', '--source', 'clients/alpha',
                                 '--rules', 'Alpha müşterisi Zeta Bank, fiyat tabanı 9 milyon'], return_result=True)
        self.assertEqual((code, result['status'], result['verified']), (0, 'provisioned', True))
        scopes = projects.read_project_scopes(self.vault)
        for source in ('projects/alpha/Kurallar.md', 'projects/alpha/MOC.md', 'clients/alpha/x.md', 'notes/alpha/x.md'):
            self.assertEqual(runtime.record_scope({'source': source}, scopes), 'alpha', source)
        self.assertEqual(sorted(result['workspace']['sources']), ['clients/alpha', 'notes/alpha', 'projects/alpha'])
        for reader, visible in (('beta', False), ('alpha', True)):
            answer, code = cli.main(['--vault', str(self.vault), '--state', str(self.state), 'context',
                                     '--project', reader, 'Zeta Bank fiyat'], return_result=True)
            self.assertEqual(code, 0, answer)
            self.assertEqual('Zeta Bank' in json.dumps(answer, ensure_ascii=False), visible, reader)

    def test_provision_uses_the_vaults_own_project_folder(self):
        # The official template keeps projects under '🏰 300-Projects'; a parallel projects/
        # tree would split one project across two folders.
        parent = self.vault / '🏰 300-Projects'
        (parent / 'eski').mkdir(parents=True)
        projects.manage(self.vault, 'add', folder='🏰 300-Projects/eski', project='eski')
        result = workspaces.provision_workspace(self.vault, 'alpha', name='Alfa')
        self.assertFalse((self.vault / 'projects').exists())
        self.assertTrue((parent / 'alpha/Kurallar.md').is_file())
        self.assertIn('[[🏰 300-Projects/alpha/Kurallar|', (parent / 'alpha/MOC.md').read_text(encoding='utf-8'))
        self.assertEqual(result['workspace']['rules_file'], '🏰 300-Projects/alpha/Kurallar.md')
        self.assertIn('🏰 300-Projects/alpha', result['workspace']['sources'])
        legacy = workspaces.inspect_workspace(self.vault, 'eski')
        self.assertEqual(legacy['rules_file'], '🏰 300-Projects/eski/Kurallar.md')

    def test_provision_rolls_back_when_kept_rules_belong_to_another_project(self):
        rules = self.vault / 'projects/alpha/Kurallar.md'
        rules.parent.mkdir(parents=True)
        rules.write_text('---\nproject: beta\n---\n# Beta kuralları\n', encoding='utf-8')
        with self.assertRaisesRegex(workspaces.WorkspaceProvisionError, 'outside the workspace scope') as caught:
            workspaces.provision_workspace(self.vault, 'alpha')
        report = caught.exception.workspace_reconciliation
        self.assertEqual((report['rollback'], report['registry']), ('complete', 'removed'))
        self.assertEqual(rules.read_text(encoding='utf-8'), '---\nproject: beta\n---\n# Beta kuralları\n')
        self.assertEqual(json.loads((self.vault / projects.PROJECT_SCOPES_FILE).read_text(encoding='utf-8'))['folders'], {})

    def test_failed_provision_does_not_erase_later_registry_change(self):
        def concurrent_change(vault, workspace_id):
            workspaces.create_workspace(vault, 'beta')
            raise RuntimeError('verification failed after other update')
        with patch.object(workspaces, 'inspect_workspace', concurrent_change):
            with self.assertRaises(RuntimeError):
                workspaces.provision_workspace(self.vault, 'alpha')
        self.assertIn('beta', workspaces.read_workspaces(self.vault))
        self.assertNotIn('alpha', workspaces.read_workspaces(self.vault))

    def test_failed_provision_rolls_back_despite_external_scope_edit(self):
        # An editor that does not take the vault lock changes the scope file mid-provision.
        scopes = self.vault / projects.PROJECT_SCOPES_FILE
        def external_edit(vault, workspace_id):
            data = json.loads(scopes.read_text(encoding='utf-8'))
            data['shared_unscoped'] = True
            scopes.write_text(json.dumps(data), encoding='utf-8')
            raise RuntimeError('verification failed')
        with patch.object(workspaces, 'inspect_workspace', external_edit):
            with self.assertRaises(workspaces.WorkspaceProvisionError) as caught:
                workspaces.provision_workspace(self.vault, 'alpha')
        report = caught.exception.workspace_reconciliation
        self.assertEqual((report['rollback'], report['registry']), ('complete', 'removed'))
        self.assertEqual(sorted(report['unbound_folders']), ['notes/alpha', 'projects/alpha'])
        self.assertNotIn('alpha', workspaces.read_workspaces(self.vault))
        self.assertEqual(json.loads(scopes.read_text(encoding='utf-8')), {'folders': {}, 'shared_unscoped': True})
        self.assertFalse((self.vault / 'projects/alpha').exists())
        # Nothing is left behind: the same command now succeeds.
        self.assertEqual(workspaces.provision_workspace(self.vault, 'alpha')['status'], 'provisioned')

    def test_rollback_keeps_template_edited_during_provision(self):
        rules = self.vault / 'projects/alpha/Kurallar.md'
        def user_edit(vault, workspace_id):
            rules.write_text('# Kendi kurallarım\n- Şifre notları Arşiv dışında\n', encoding='utf-8')
            raise RuntimeError('verification failed')
        with patch.object(workspaces, 'inspect_workspace', user_edit):
            with self.assertRaises(workspaces.WorkspaceProvisionError) as caught:
                workspaces.provision_workspace(self.vault, 'alpha')
        report = caught.exception.workspace_reconciliation
        self.assertEqual(report['preserved_files'], ['projects/alpha/Kurallar.md'])
        self.assertEqual(report['removed_files'], ['projects/alpha/MOC.md'])
        self.assertIn('Şifre notları', rules.read_text(encoding='utf-8'))

    def test_incomplete_rollback_keeps_a_failed_record_that_provision_resumes(self):
        scopes = self.vault / projects.PROJECT_SCOPES_FILE
        saved = {}
        def break_scope_file(vault, workspace_id):
            saved['bytes'] = scopes.read_bytes()
            scopes.write_text('{', encoding='utf-8')  # an external editor mid-save
            raise RuntimeError('verification failed')
        with patch.object(workspaces, 'inspect_workspace', break_scope_file):
            with self.assertRaises(workspaces.WorkspaceProvisionError) as caught:
                workspaces.provision_workspace(self.vault, 'alpha', name='Research Lab', kind='research',
                                               sources=['papers'], rules='- Cite sources')
        report = caught.exception.workspace_reconciliation
        self.assertEqual(report['rollback'], 'incomplete')
        self.assertEqual(report['registry'], 'kept_failed')
        self.assertIn('beyin.py workspace provision alpha', report['recovery'])
        self.assertIn('resume with: beyin.py workspace provision alpha', str(caught.exception))
        self.assertEqual(scopes.read_text(encoding='utf-8'), '{')  # never overwritten
        registry = json.loads((self.vault / workspaces.WORKSPACES_FILE).read_text(encoding='utf-8'))
        self.assertEqual(registry['workspaces']['alpha']['provision']['status'], 'failed')
        # The user repairs the scope file; the same command resumes the recorded workspace.
        scopes.write_bytes(saved['bytes'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            workspaces.create_workspace(self.vault, 'alpha')
        result = workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual((result['status'], result['verified'], result.get('resumed')), ('provisioned', True, True))
        info = workspaces.inspect_workspace(self.vault, 'alpha')
        self.assertEqual(info['provision']['status'], 'complete')
        self.assertEqual((info['name'], info['kind']), ('Research Lab', 'research'))
        self.assertIn('papers', info['sources'])
        self.assertIn('Cite sources', (self.vault / info['rules_file']).read_text(encoding='utf-8'))
        self.assertTrue(info['rules_exist'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            workspaces.provision_workspace(self.vault, 'alpha')

    def test_interrupted_provision_stays_visible_and_resumable(self):
        with patch.object(workspaces, '_bind', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspaces.provision_workspace(self.vault, 'alpha')
        info = workspaces.inspect_workspace(self.vault, 'alpha')
        self.assertEqual(info['provision']['status'], 'pending')
        self.assertNotIn('token', info['provision'])
        result = workspaces.provision_workspace(self.vault, 'alpha')
        self.assertTrue(result['resumed'])
        self.assertEqual({item['status'] for item in result['files']}, {'preserved'})
        self.assertEqual(sorted(result['workspace']['sources']), ['notes/alpha', 'projects/alpha'])

    def test_custom_plan_survives_interruptions_at_each_provision_phase(self):
        for phase in ('_make_dirs', '_bind', '_finish_provision'):
            with self.subTest(phase=phase):
                vault = self.vault / phase
                vault.mkdir()
                options = dict(name='Research Lab', kind='research', sources=['papers'],
                               description='Original research', rules='- Preserve citations')
                with patch.object(workspaces, phase, side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        workspaces.provision_workspace(vault, 'alpha', **options)
                # Later vault structure changes must not change the original scaffold path.
                (vault / '🏰 300-Projects').mkdir()
                result = workspaces.manage(vault, 'provision', 'alpha')
                self.assertTrue(result['verified'])
                self.assertTrue(result['resumed'])
                self.assertEqual(result['workspace']['name'], 'Research Lab')
                self.assertEqual(result['workspace']['kind'], 'research')
                self.assertEqual(sorted(result['workspace']['sources']),
                                 ['notes/alpha', 'papers', 'projects/alpha'])
                rules = (vault / 'projects/alpha/Kurallar.md').read_text(encoding='utf-8')
                self.assertIn('Original research', rules)
                self.assertIn('Preserve citations', rules)
                self.assertIn('Research Lab', rules)
                self.assertIn('research', rules)
                self.assertIn('papers', rules)
                self.assertFalse((vault / '🏰 300-Projects/alpha').exists())

    def test_failed_resume_keeps_custom_plan_and_user_content(self):
        with patch.object(workspaces, '_make_dirs', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspaces.provision_workspace(self.vault, 'alpha', name='Research Lab',
                                               kind='research', sources=['papers'], rules='- Cite sources')
        with patch.object(workspaces, '_bind', side_effect=RuntimeError('binding failed')):
            with self.assertRaises(workspaces.WorkspaceProvisionError):
                workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual(workspaces.inspect_workspace(self.vault, 'alpha')['provision']['status'], 'failed')
        rules = self.vault / 'projects/alpha/Kurallar.md'
        rules.parent.mkdir(parents=True)
        rules.write_text('# User edited rules\n', encoding='utf-8')
        result = workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual(result['workspace']['name'], 'Research Lab')
        self.assertEqual(result['workspace']['kind'], 'research')
        self.assertIn('papers', result['workspace']['sources'])
        self.assertEqual(rules.read_text(encoding='utf-8'), '# User edited rules\n')
        self.assertIn('Research Lab', (rules.parent / 'MOC.md').read_text(encoding='utf-8'))

    def test_resume_rejects_conflicting_options_without_mutation(self):
        options = dict(name='Research Lab', kind='research', sources=['papers'],
                       description='Original research', rules='- Cite sources')
        with patch.object(workspaces, '_bind', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspaces.provision_workspace(self.vault, 'alpha', **options)
        def snapshot():
            return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                    for p in self.vault.rglob('*') if p.is_file()}
        before = snapshot()
        for changed in (dict(name='Other'), dict(kind='general'), dict(sources=['other']),
                        dict(description='Other'), dict(rules='- Other')):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, 'conflict with the saved plan'):
                    workspaces.provision_workspace(self.vault, 'alpha', **changed)
                self.assertEqual(snapshot(), before)
        self.assertTrue(workspaces.provision_workspace(self.vault, 'alpha', **options)['verified'])

    def test_resume_revalidates_saved_paths_and_refuses_missing_plan(self):
        with patch.object(workspaces, '_bind', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspaces.provision_workspace(self.vault, 'alpha', sources=['papers'])
        registry_path = self.vault / workspaces.WORKSPACES_FILE
        original = json.loads(registry_path.read_text(encoding='utf-8'))
        for field, value in (('project_parent', '../outside'), ('sources', ['../outside']),
                             ('project_parent', str(self.vault.parent / 'outside'))):
            with self.subTest(field=field, value=value):
                registry = json.loads(json.dumps(original))
                registry['workspaces']['alpha']['provision']['plan'][field] = value
                registry_path.write_text(json.dumps(registry), encoding='utf-8')
                before = registry_path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'vault-relative'):
                    workspaces.provision_workspace(self.vault, 'alpha')
                self.assertEqual(registry_path.read_bytes(), before)
                self.assertFalse((self.vault.parent / 'outside').exists())
        del original['workspaces']['alpha']['provision']['plan']
        registry_path.write_text(json.dumps(original), encoding='utf-8')
        before = registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'without a valid saved plan'):
            workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual(registry_path.read_bytes(), before)

    def test_resume_rejects_saved_source_replaced_by_external_symlink(self):
        with patch.object(workspaces, '_make_dirs', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                workspaces.provision_workspace(self.vault, 'alpha', sources=['papers'])
        outside = self.vault.parent / 'outside'
        outside.mkdir()
        try:
            (self.vault / 'papers').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable')
        registry_path = self.vault / workspaces.WORKSPACES_FILE
        before = registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'remain inside the vault'):
            workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual(registry_path.read_bytes(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_provision_refuses_folder_owned_by_another_project_before_writing(self):
        (self.vault / 'projects/alpha').mkdir(parents=True)
        projects.manage(self.vault, 'add', folder='projects/alpha', project='beta')
        with self.assertRaisesRegex(ValueError, 'already assigned to workspace beta'):
            workspaces.provision_workspace(self.vault, 'alpha')
        self.assertFalse((self.vault / workspaces.WORKSPACES_FILE).exists())
        self.assertFalse((self.vault / 'notes').exists())

    def test_provisioned_moc_carries_the_block_moc_may_regenerate(self):
        import beyin_v3_lint as linter
        workspaces.provision_workspace(self.vault, 'alpha', name='Alfa')
        (self.vault / 'projects/alpha/Karar.md').write_text('# Karar kaydı\n', encoding='utf-8')
        result = linter.generate_moc(self.vault, 'projects/alpha')
        self.assertEqual(result['status'], 'updated')
        text = (self.vault / 'projects/alpha/MOC.md').read_text(encoding='utf-8')
        self.assertIn('[[projects/alpha/Kurallar|Çalışma Alanı Kuralları]]', text)
        self.assertIn('[[projects/alpha/Karar.md|Karar kaydı]]', text)

    def test_cli_failed_provision_reports_reconciliation(self):
        def fail(vault, workspace_id):
            raise RuntimeError('verification failed')
        with patch.object(workspaces, 'inspect_workspace', fail):
            result, code = cli.main(['--vault', str(self.vault), '--state', str(self.state / 'fresh'),
                                     'workspace', 'provision', 'alpha'], return_result=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'WorkspaceProvisionError')
        self.assertEqual(result['reconciliation']['rollback'], 'complete')
        self.assertFalse((self.state / 'fresh').exists())

    def test_provision_rejects_symlink_outside_vault(self):
        outside = self.vault.parent / 'outside'
        outside.mkdir()
        try:
            (self.vault / 'projects').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError):
            workspaces.provision_workspace(self.vault, 'alpha')
        self.assertEqual(list(outside.iterdir()), [])

    def test_plan_workspace_structure(self):
        plan = workspaces.plan_workspace(
            self.vault,
            "photo-lab",
            kind="personal",
            name="Photo Lab",
            description="Photography experiments and gear tracking",
            rules="- RAW dosyaları harici diskte tutulur.",
        )
        self.assertEqual(plan["workspace_id"], "photo-lab")
        self.assertEqual(plan["name"], "Photo Lab")
        self.assertEqual(plan["kind"], "personal")
        self.assertIn("projects/photo-lab", plan["directories"])
        self.assertIn("notes/photo-lab", plan["directories"])

        # Verify files to create
        file_paths = [f["path"] for f in plan["files"]]
        self.assertIn("projects/photo-lab/Kurallar.md", file_paths)
        self.assertIn("projects/photo-lab/MOC.md", file_paths)

        # Ensure plan_workspace does not create files on disk
        self.assertFalse((self.vault / "projects/photo-lab").exists())
        self.assertFalse((self.vault / projects.PROJECT_SCOPES_FILE).exists())

    def test_plan_workspace_validation(self):
        with self.assertRaisesRegex(ValueError, "workspace ID must be a non-empty string"):
            workspaces.plan_workspace(self.vault, "")

        with self.assertRaisesRegex(ValueError, "workspace ID must be 1-64 characters"):
            workspaces.plan_workspace(self.vault, "invalid name with spaces!")

        with self.assertRaisesRegex(ValueError, "invalid workspace kind"):
            workspaces.plan_workspace(self.vault, "valid-id", kind="nonexistent")

        # Duplicate detection
        workspaces.create_workspace(self.vault, "already-exists", kind="general")
        with self.assertRaisesRegex(ValueError, "workspace already exists"):
            workspaces.plan_workspace(self.vault, "already-exists", kind="software")

    def test_provision_dry_run(self):
        res = workspaces.provision_workspace(
            self.vault,
            "robotics",
            kind="research",
            name="Robotics Research",
            dry_run=True,
        )
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["action"], "provision")
        self.assertEqual(res["plan"]["workspace_id"], "robotics")

        # Zero disk mutation check
        self.assertFalse((self.vault / "projects/robotics").exists())
        self.assertFalse((self.vault / projects.PROJECT_SCOPES_FILE).exists())

    def test_provision_success_and_verification(self):
        res = workspaces.provision_workspace(
            self.vault,
            "fintech",
            kind="software",
            name="FinTech Engine",
            description="Core financial ledger system",
            rules="- Zero lost update kuralı esastır.",
        )
        self.assertEqual(res["status"], "provisioned")
        self.assertTrue(res["verified"])
        self.assertEqual(res["workspace"]["id"], "fintech")

        # Verify disk artifacts
        rules_path = self.vault / "projects/fintech/Kurallar.md"
        moc_path = self.vault / "projects/fintech/MOC.md"
        notes_dir = self.vault / "notes/fintech"

        self.assertTrue(rules_path.is_file())
        self.assertTrue(moc_path.is_file())
        self.assertTrue(notes_dir.is_dir())

        rules_text = rules_path.read_text(encoding="utf-8")
        self.assertIn("# FinTech Engine — Kurallar", rules_text)
        self.assertIn("Zero lost update kuralı esastır", rules_text)

        moc_text = moc_path.read_text(encoding="utf-8")
        self.assertIn("# FinTech Engine — Map of Content (MOC)", moc_text)
        self.assertIn("[[projects/fintech/Kurallar|Çalışma Alanı Kuralları]]", moc_text)

        # Inspect verification check
        info = workspaces.inspect_workspace(self.vault, "fintech")
        self.assertTrue(info["rules_exist"])
        self.assertTrue(all(s["exists"] for s in info["sources_detail"]))

    def test_provision_preserves_existing_user_files(self):
        # Pre-create directory and Kurallar.md with user content
        proj_dir = self.vault / "projects/my-app"
        proj_dir.mkdir(parents=True)
        user_rules = proj_dir / "Kurallar.md"
        user_rules.write_text("# My App Custom Rules\n- Handcrafted rule that must be preserved", encoding="utf-8")

        res = workspaces.provision_workspace(self.vault, "my-app", kind="software")
        self.assertEqual(res["status"], "provisioned")

        # File content must not be overwritten
        self.assertEqual(user_rules.read_text(encoding="utf-8"), "# My App Custom Rules\n- Handcrafted rule that must be preserved")
        file_statuses = {f["path"]: f["status"] for f in res["files"]}
        self.assertEqual(file_statuses["projects/my-app/Kurallar.md"], "preserved")

    def test_provision_atomic_rollback_on_failure(self):
        manifest_path = self.vault / projects.PROJECT_SCOPES_FILE
        initial_manifest = {"folders": {}, "shared_unscoped": False}
        manifest_path.write_text(json.dumps(initial_manifest), encoding="utf-8")

        # Simulate unexpected failure while binding the workspace folders
        with patch("beyin_v3_workspaces._bind", side_effect=RuntimeError("Simulated registration failure")):
            with self.assertRaisesRegex(RuntimeError, "Simulated registration failure"):
                workspaces.provision_workspace(self.vault, "fail-ws", kind="software")

        # Verify atomic rollback:
        # 1. Created files must be cleaned up
        self.assertFalse((self.vault / "projects/fail-ws/Kurallar.md").exists())
        self.assertFalse((self.vault / "projects/fail-ws/MOC.md").exists())

        # 2. Created empty directories must be removed
        self.assertFalse((self.vault / "projects/fail-ws").exists())
        self.assertFalse((self.vault / "notes/fail-ws").exists())

        # 3. Manifest must be restored exactly and the pending record removed
        restored = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(restored, initial_manifest)
        self.assertNotIn("fail-ws", workspaces.read_workspaces(self.vault))
        self.assertFalse((self.vault / "projects").exists())

    def test_cli_plan_and_provision_end_to_end(self):
        # 1. CLI plan via cli.main
        args_plan = [
            "--vault", str(self.vault),
            "--state", str(self.state),
            "workspace", "plan", "cli-builder-ws",
            "--name", "CLI Builder Test",
            "--kind", "learning",
            "--description", "Testing conversational builder CLI",
        ]
        res_plan, code_plan = cli.main(args_plan, return_result=True)
        self.assertEqual(code_plan, 0)
        self.assertEqual(res_plan["status"], "ok")
        self.assertEqual(res_plan["plan"]["workspace_id"], "cli-builder-ws")
        self.assertEqual(res_plan["plan"]["kind"], "learning")

        # 2. CLI provision dry-run via cli.main
        args_dry = [
            "--vault", str(self.vault),
            "--state", str(self.state),
            "workspace", "provision", "cli-builder-ws",
            "--name", "CLI Builder Test",
            "--kind", "learning",
            "--dry-run",
        ]
        res_dry, code_dry = cli.main(args_dry, return_result=True)
        self.assertEqual(code_dry, 0)
        self.assertEqual(res_dry["status"], "dry_run")
        self.assertFalse((self.vault / "projects/cli-builder-ws").exists())

        # 3. CLI provision live via subprocess
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/beyin_v3.py"),
                "--vault", str(self.vault),
                "--state", str(self.state),
                "workspace", "provision", "cli-builder-ws",
                "--name", "CLI Builder Test",
                "--kind", "learning",
                "--rules", "- Daily study 30m.",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=inherited_env(),
        )
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "provisioned")
        self.assertTrue(parsed["verified"])
        self.assertEqual(parsed["workspace"]["id"], "cli-builder-ws")

        # 4. CLI inspect via subprocess
        proc_insp = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/beyin_v3.py"),
                "--vault", str(self.vault),
                "--state", str(self.state),
                "workspace", "inspect", "cli-builder-ws",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=inherited_env(),
        )
        parsed_insp = json.loads(proc_insp.stdout)
        self.assertEqual(parsed_insp["id"], "cli-builder-ws")
        self.assertTrue(parsed_insp["rules_exist"])


if __name__ == "__main__":
    unittest.main()
