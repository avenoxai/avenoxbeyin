"""Tests for Unified Workspace Registry (RFC #368 P0-C)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3 as runtime
import beyin_v3_projects as projects
import beyin_v3_workspaces as workspaces

CLI_SPEC = importlib.util.spec_from_file_location("beyin_cli", ROOT / "scripts/beyin_v3.py")
cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(cli)


class WorkspacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def test_read_workspaces_empty_when_manifest_absent(self):
        self.assertEqual(workspaces.read_workspaces(self.vault), {})

    def test_read_workspaces_synthesized_from_legacy_folders(self):
        scopes_path = self.vault / projects.PROJECT_SCOPES_FILE
        scopes_path.write_text(json.dumps({
            "folders": {
                "projects/core-engine": "core-engine",
                "projects/core-engine/sub": "core-engine",
                "learning/rust": "rust-study"
            },
            "shared_unscoped": True
        }), encoding="utf-8")

        res = workspaces.read_workspaces(self.vault)
        self.assertIn("core-engine", res)
        self.assertIn("rust-study", res)
        self.assertEqual(res["core-engine"]["kind"], "general")
        self.assertEqual(res["core-engine"]["lifecycle"], "active")
        self.assertEqual(res["core-engine"]["sources"], ["projects/core-engine", "projects/core-engine/sub"])
        self.assertEqual(res["rust-study"]["sources"], ["learning/rust"])

    def test_create_workspace_success(self):
        ws = workspaces.create_workspace(self.vault, "ai-lab", name="AI Research Lab", kind="research")
        self.assertEqual(ws["id"], "ai-lab")
        self.assertEqual(ws["name"], "AI Research Lab")
        self.assertEqual(ws["kind"], "research")
        self.assertEqual(ws["lifecycle"], "active")
        self.assertEqual(ws["sources"], [])

        # Check saved manifest
        scopes_path = self.vault / projects.PROJECT_SCOPES_FILE
        saved = json.loads(scopes_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 2)
        self.assertIn("ai-lab", saved["workspaces"])

    def test_create_workspace_validation(self):
        with self.assertRaisesRegex(ValueError, "workspace ID must be 1-64 characters"):
            workspaces.create_workspace(self.vault, "invalid name with spaces!")

        with self.assertRaisesRegex(ValueError, "invalid workspace kind"):
            workspaces.create_workspace(self.vault, "test-ws", kind="nonexistent_kind")

        workspaces.create_workspace(self.vault, "unique-id", kind="software")
        with self.assertRaisesRegex(ValueError, "workspace already exists"):
            workspaces.create_workspace(self.vault, "unique-id", kind="software")

    def test_create_workspace_with_existing_sources(self):
        (self.vault / "projects/engine").mkdir(parents=True)
        ws = workspaces.create_workspace(
            self.vault,
            "engine-ws",
            name="Engine Workspace",
            kind="software",
            sources=["projects/engine"]
        )
        self.assertEqual(ws["sources"], ["projects/engine"])

        # Nonexistent folder must raise ValueError
        with self.assertRaisesRegex(ValueError, "source folder not found in vault"):
            workspaces.create_workspace(
                self.vault,
                "bad-ws",
                kind="software",
                sources=["nonexistent/dir"]
            )

    def test_connect_and_disconnect_source(self):
        (self.vault / "projects/app").mkdir(parents=True)
        (self.vault / "projects/docs").mkdir(parents=True)
        ws = workspaces.create_workspace(self.vault, "app-ws", kind="software")

        # Connect sources
        workspaces.connect_source(self.vault, "app-ws", "projects/app")
        workspaces.connect_source(self.vault, "app-ws", "projects/docs")

        inspected = workspaces.inspect_workspace(self.vault, "app-ws")
        self.assertEqual(len(inspected["sources_detail"]), 2)
        self.assertTrue(all(s["exists"] for s in inspected["sources_detail"]))

        # Disconnecting unlinks from workspace and folders mapping
        test_file = self.vault / "projects/docs/notes.md"
        test_file.write_text("# Important Notes", encoding="utf-8")
        workspaces.disconnect_source(self.vault, "app-ws", "projects/docs")

        # Verify disk safety: folder and file MUST NOT be deleted
        self.assertTrue(test_file.exists(), "disconnect_source must never delete disk files")
        self.assertTrue((self.vault / "projects/docs").exists(), "disconnect_source must never delete disk directories")

        # Manifest check
        manifest = json.loads((self.vault / projects.PROJECT_SCOPES_FILE).read_text(encoding="utf-8"))
        self.assertEqual(manifest["workspaces"]["app-ws"]["sources"], ["projects/app"])
        self.assertNotIn("projects/docs", manifest["folders"])

    def test_connect_source_collision_rejection(self):
        (self.vault / "shared_folder").mkdir(parents=True)
        workspaces.create_workspace(self.vault, "ws-one", kind="software", sources=["shared_folder"])
        workspaces.create_workspace(self.vault, "ws-two", kind="research")

        with self.assertRaisesRegex(ValueError, "already assigned to workspace ws-one"):
            workspaces.connect_source(self.vault, "ws-two", "shared_folder")

    def test_archive_and_unarchive_lifecycle(self):
        workspaces.create_workspace(self.vault, "active-ws", kind="personal")
        workspaces.create_workspace(self.vault, "legacy-ws", kind="career")

        # Initially both are active
        active_list = workspaces.list_workspaces(self.vault)
        self.assertEqual([w["id"] for w in active_list], ["active-ws", "legacy-ws"])

        # Archive legacy-ws
        workspaces.archive_workspace(self.vault, "legacy-ws")

        # list without --all hides legacy-ws
        active_list = workspaces.list_workspaces(self.vault)
        self.assertEqual([w["id"] for w in active_list], ["active-ws"])

        # list with include_archived returns both
        all_list = workspaces.list_workspaces(self.vault, include_archived=True)
        self.assertEqual([w["id"] for w in all_list], ["active-ws", "legacy-ws"])

        # Unarchive restores to active
        workspaces.unarchive_workspace(self.vault, "legacy-ws")
        restored = workspaces.list_workspaces(self.vault)
        self.assertEqual([w["id"] for w in restored], ["active-ws", "legacy-ws"])

    def test_inspect_workspace(self):
        (self.vault / "projects/omega").mkdir(parents=True)
        rules_path = self.vault / "projects/omega/Kurallar.md"
        rules_path.write_text("# Project Rules\n- Rule 1", encoding="utf-8")

        workspaces.create_workspace(self.vault, "omega", kind="software", sources=["projects/omega"])
        info = workspaces.inspect_workspace(self.vault, "omega")
        self.assertTrue(info["rules_exist"])
        self.assertEqual(info["sources_detail"], [{"folder": "projects/omega", "exists": True}])

    def test_cli_workspace_subcommands(self):
        # Test manage CLI dispatcher
        (self.vault / "src/cli_test").mkdir(parents=True)
        res_create = workspaces.manage(self.vault, "create", workspace_id="cli-ws", name="CLI WS", kind="software", source="src/cli_test")
        self.assertEqual(res_create["status"], "created")

        res_list = workspaces.manage(self.vault, "list")
        self.assertEqual(res_list["count"], 1)
        self.assertEqual(res_list["workspaces"][0]["id"], "cli-ws")

        res_inspect = workspaces.manage(self.vault, "inspect", workspace_id="cli-ws")
        self.assertEqual(res_inspect["id"], "cli-ws")

        res_archive = workspaces.manage(self.vault, "archive", workspace_id="cli-ws")
        self.assertEqual(res_archive["status"], "archived")

        res_list_active = workspaces.manage(self.vault, "list")
        self.assertEqual(res_list_active["count"], 0)

        res_list_all = workspaces.manage(self.vault, "list", include_archived=True)
        self.assertEqual(res_list_all["count"], 1)

        res_unarchive = workspaces.manage(self.vault, "unarchive", workspace_id="cli-ws")
        self.assertEqual(res_unarchive["status"], "unarchived")

    def test_project_scopes_doctor_compatibility_with_workspaces(self):
        (self.vault / "projects/hub").mkdir(parents=True)
        workspaces.create_workspace(self.vault, "hub", kind="software", sources=["projects/hub"])

        # projects.check_project_scopes must not report unsupported keys or errors
        report = projects.check_project_scopes(self.vault)
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("unsupported_keys", report["warnings"])
        self.assertEqual(report["folders"], {"projects/hub": "hub"})

    def test_end_to_end_cli_main_and_subprocess(self):
        (self.vault / "modules/auth").mkdir(parents=True)

        # 1. Test via cli.main with return_result=True
        args_create = [
            "--vault", str(self.vault),
            "--state", str(self.state),
            "workspace", "create", "auth-service",
            "--name", "Authentication Service",
            "--kind", "software",
            "--source", "modules/auth",
        ]
        res, code = cli.main(args_create, return_result=True)
        self.assertEqual(code, 0)
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["workspace"]["id"], "auth-service")

        # 2. Test inspect via cli.main
        args_inspect = [
            "--vault", str(self.vault),
            "--state", str(self.state),
            "workspace", "inspect", "auth-service",
        ]
        res_insp, code_insp = cli.main(args_inspect, return_result=True)
        self.assertEqual(code_insp, 0)
        self.assertEqual(res_insp["id"], "auth-service")
        self.assertEqual(res_insp["sources"], ["modules/auth"])

        # 3. Test list via subprocess running scripts/beyin_v3.py
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/beyin_v3.py"), "--vault", str(self.vault), "--state", str(self.state), "workspace", "list"],
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["count"], 1)
        self.assertEqual(parsed["workspaces"][0]["id"], "auth-service")


if __name__ == "__main__":
    unittest.main()
