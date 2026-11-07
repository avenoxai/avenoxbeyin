"""Tests for Unified Workspace Registry (RFC #368 P0-C)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
import time
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


class WorkspacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def test_source_aliases_cannot_duplicate_or_steal_legacy_scope(self):
        (self.vault / 'team').mkdir()
        path = self.vault / projects.PROJECT_SCOPES_FILE
        path.write_text(json.dumps({'folders': {'team/': 'legacy'}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            workspaces.create_workspace(self.vault, 'new', sources=['team'])
        path.unlink()
        workspaces.create_workspace(self.vault, 'one', sources=['team'])
        workspaces.connect_source(self.vault, 'one', 'TEAM')
        workspaces.disconnect_source(self.vault, 'one', 'team')
        data = json.loads(path.read_text())
        self.assertEqual(data['folders'], {})
        self.assertEqual(workspaces.read_workspaces(self.vault)['one']['sources'], [])

    def test_concurrent_creates_preserve_both_workspaces(self):
        original_save = workspaces._save_registry
        first_entered = threading.Event()
        errors = []
        def delayed_save(vault, data):
            if 'one' in data.get('workspaces', {}) and 'two' not in data.get('workspaces', {}):
                first_entered.set()
                time.sleep(0.15)
            original_save(vault, data)
        def create(name):
            try:
                workspaces.create_workspace(self.vault, name)
            except Exception as exc:
                errors.append(exc)
        with patch.object(workspaces, '_save_registry', delayed_save):
            first = threading.Thread(target=create, args=('one',))
            second = threading.Thread(target=create, args=('two',))
            first.start()
            self.assertTrue(first_entered.wait(2))
            second.start()
            first.join(3)
            second.join(3)
        self.assertFalse(errors)
        self.assertEqual({item['id'] for item in workspaces.list_workspaces(self.vault)}, {'one', 'two'})

    def test_read_only_workspace_dispatch_does_not_create_state(self):
        fresh = self.state / 'not-created'
        result, code = cli.main(['--vault', str(self.vault), '--state', str(fresh),
                                 'workspace', 'list'], return_result=True)
        self.assertEqual(code, 0)
        self.assertFalse(fresh.exists())

    def test_disconnect_preserves_reassigned_folder_owner(self):
        (self.vault / 'team').mkdir()
        workspaces.create_workspace(self.vault, 'one', sources=['team'])
        projects.manage(self.vault, 'add', folder='team', project='two')
        # The folder now belongs to two; one has nothing to disconnect.
        with self.assertRaisesRegex(ValueError, 'not connected'):
            workspaces.disconnect_source(self.vault, 'one', 'team')
        data = json.loads((self.vault / projects.PROJECT_SCOPES_FILE).read_text())
        self.assertEqual(data['folders'], {'team': 'two'})
        self.assertEqual(workspaces.read_workspaces(self.vault)['one']['sources'], [])

    def test_scope_file_keeps_the_format_older_releases_read(self):
        # An update rollback restores a reader that accepts only folders and shared_unscoped.
        for folder in ('team', 'docs', 'extra'):
            (self.vault / folder).mkdir()
        workspaces.create_workspace(self.vault, 'one', name='One', kind='software', sources=['team'])
        workspaces.connect_source(self.vault, 'one', 'docs')
        workspaces.archive_workspace(self.vault, 'one')
        workspaces.unarchive_workspace(self.vault, 'one')
        workspaces.create_workspace(self.vault, 'two', sources=['extra'])
        scopes = json.loads((self.vault / projects.PROJECT_SCOPES_FILE).read_text(encoding='utf-8'))
        self.assertEqual(set(scopes), {'folders', 'shared_unscoped'})
        self.assertEqual(scopes['folders'], {'team': 'one', 'docs': 'one', 'extra': 'two'})
        self.assertEqual(dict(projects.read_project_scopes(self.vault)['folders']),
                         {'team': 'one', 'docs': 'one', 'extra': 'two'})
        registry = json.loads((self.vault / workspaces.WORKSPACES_FILE).read_text(encoding='utf-8'))
        self.assertEqual(registry['schema_version'], workspaces.REGISTRY_SCHEMA_VERSION)
        self.assertEqual(registry['workspaces']['one']['kind'], 'software')
        self.assertNotIn('sources', registry['workspaces']['one'])

    def test_sources_follow_scope_edits_made_without_the_registry(self):
        # project-scope add/remove (or an older release after rollback) only edits folders.
        for folder in ('team', 'later'):
            (self.vault / folder).mkdir()
        workspaces.create_workspace(self.vault, 'one', kind='research', sources=['team'])
        projects.manage(self.vault, 'remove', folder='team')
        projects.manage(self.vault, 'add', folder='later', project='one')
        view = workspaces.read_workspaces(self.vault)['one']
        self.assertEqual(view['sources'], ['later'])
        self.assertEqual(view['kind'], 'research')
        self.assertTrue(view['registered'])
        projects.manage(self.vault, 'add', folder='team', project='fresh')
        fresh = workspaces.read_workspaces(self.vault)['fresh']
        self.assertEqual((fresh['sources'], fresh['registered']), (['team'], False))

    def test_registry_from_newer_release_or_invalid_is_not_rewritten(self):
        path = self.vault / workspaces.WORKSPACES_FILE
        for raw in (json.dumps({'schema_version': 2, 'workspaces': {}}), '{broken',
                    json.dumps({'schema_version': 1, 'workspaces': []}),
                    json.dumps({'schema_version': 1, 'workspaces': {'x': {'note_roots': 'notes/x'}}})):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding='utf-8')
                with self.assertRaises(ValueError):
                    workspaces.create_workspace(self.vault, 'new')
                self.assertEqual(path.read_text(encoding='utf-8'), raw)

    def test_invalid_manifest_is_rejected_without_rewriting_bytes(self):
        path = self.vault / projects.PROJECT_SCOPES_FILE
        for data in ({'folders': [], 'shared_unscoped': True},
                     {'folders': {}, 'shared_unscoped': 'true'},
                     {'folders': {'../outside': 'one'}}):
            with self.subTest(data=data):
                original = json.dumps(data).encode()
                path.write_bytes(original)
                with self.assertRaises(ValueError):
                    workspaces.create_workspace(self.vault, 'new')
                self.assertEqual(path.read_bytes(), original)

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

        # Metadata goes to the registry; a workspace without sources never touches the scope file.
        self.assertFalse((self.vault / projects.PROJECT_SCOPES_FILE).exists())
        saved = json.loads((self.vault / workspaces.WORKSPACES_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 1)
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
        self.assertEqual(workspaces.read_workspaces(self.vault)["app-ws"]["sources"], ["projects/app"])
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
            env=inherited_env(),
        )
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["count"], 1)
        self.assertEqual(parsed["workspaces"][0]["id"], "auth-service")


if __name__ == "__main__":
    unittest.main()
