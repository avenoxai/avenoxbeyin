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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3 as runtime
import beyin_v3_projects as projects
import beyin_v3_workspaces as workspaces

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

    def test_failed_provision_does_not_erase_later_registry_change(self):
        def concurrent_change(vault, workspace_id):
            workspaces.create_workspace(vault, 'beta')
            raise RuntimeError('verification failed after other update')
        with patch.object(workspaces, 'inspect_workspace', concurrent_change):
            with self.assertRaises(RuntimeError):
                workspaces.provision_workspace(self.vault, 'alpha')
        self.assertIn('beta', workspaces.read_workspaces(self.vault))

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
        initial_manifest = {"schema_version": 2, "workspaces": {}, "folders": {}, "shared_unscoped": False}
        manifest_path.write_text(json.dumps(initial_manifest), encoding="utf-8")

        # Simulate unexpected failure during create_workspace registration
        with patch("beyin_v3_workspaces.create_workspace", side_effect=RuntimeError("Simulated registration failure")):
            with self.assertRaisesRegex(RuntimeError, "Simulated registration failure"):
                workspaces.provision_workspace(self.vault, "fail-ws", kind="software")

        # Verify atomic rollback:
        # 1. Created files must be cleaned up
        self.assertFalse((self.vault / "projects/fail-ws/Kurallar.md").exists())
        self.assertFalse((self.vault / "projects/fail-ws/MOC.md").exists())

        # 2. Created empty directories must be removed
        self.assertFalse((self.vault / "projects/fail-ws").exists())
        self.assertFalse((self.vault / "notes/fail-ws").exists())

        # 3. Manifest must be restored exactly
        restored = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(restored, initial_manifest)

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
        )
        parsed_insp = json.loads(proc_insp.stdout)
        self.assertEqual(parsed_insp["id"], "cli-builder-ws")
        self.assertTrue(parsed_insp["rules_exist"])


if __name__ == "__main__":
    unittest.main()
