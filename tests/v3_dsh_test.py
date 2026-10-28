"""DeepSeek Harness (dsh) contract: Claude-shaped hooks, vault gate, profile patch."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_v3.py"
SCRIPTS = ROOT / "template/.claude/scripts"
HOOK_FILE = ".claude/dsh/beyin-v3-hooks.json"
PROFILE_ENTRY = '  name: "@deepseek-ai/dsh-sandbox-policy"'


def load_adapter():
    spec = importlib.util.spec_from_file_location("beyin_test_dsh", SCRIPTS / "beyin_v3_dsh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DshIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-dsh-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.vault = root / "Synthetic DSH Vault"
        self.vault.mkdir()
        self.state = root / "state"
        self.profile_dir = root / "dsh-profile"
        self.profile_dir.mkdir()
        self.profile = self.profile_dir / "cordis.patch.yml"
        self.profile.write_text(
            "# Synthetic DSH profile patch\n"
            "- id: agent-default-model\n"
            '  name: "@deepseek-ai/dsh-agent-default-model"\n'
            "  config:\n"
            "    provider: synthetic\n"
            "    model: synthetic-model\n"
            "- insert:\n"
            "    - id: hooks-claude-code\n"
            '      name: "@deepseek-ai/dsh-hooks-claude-code"\n'
            "      config:\n"
            f'        configPath: "{self.vault / HOOK_FILE}"\n',
            encoding="utf-8")
        self.env = inherited_env(PYTHONDONTWRITEBYTECODE="1", BEYIN_V3_NO_SPAWN="1")

    def run_python(self, script, *args, payload=None, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding="utf-8", capture_output=True,
                                cwd=str(cwd or self.vault), env=env or self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def install(self, *extra):
        return json.loads(self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state, *extra))

    def cli(self, *args):
        return json.loads(self.run_python(self.vault / "beyin.py", *args))

    def hook_handlers(self):
        data = json.loads((self.vault / HOOK_FILE).read_text(encoding="utf-8"))
        return data["hooks"]

    def test_install_hook_config_shape_is_idempotent_and_removed_on_uninstall(self):
        self.install()
        hooks = self.hook_handlers()
        self.assertEqual(sorted(hooks), ["PostToolUse", "SessionStart", "Stop", "UserPromptSubmit"])
        # DSH's bridge treats a word-and-pipe matcher as an exact list of tool names, and its
        # tools are lower-case; Claude's "Edit|Write" would select nothing here.
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "edit|write|str_replace_editor")
        from v3_hook_reconcile_test import decoded_command
        for event, groups in hooks.items():
            handler = groups[0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            command = decoded_command(handler["command"])
            self.assertIn("beyin_v3_dsh.py", command)
            self.assertIn(str(self.vault), command)
            self.assertIn("--state", command)
            self.assertIn("--event " + event, command)
            self.assertNotIn("--harness", command)
        self.install()  # idempotent registration
        self.assertEqual(json.loads((self.vault / HOOK_FILE).read_text(encoding="utf-8"))["hooks"], hooks)
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state, "--uninstall")
        self.assertFalse((self.vault / HOOK_FILE).exists())
        self.assertFalse((self.vault / ".claude/scripts/beyin_v3_dsh.py").exists())

    def test_hook_answers_inside_the_vault_and_stays_quiet_outside(self):
        self.install()
        note = self.vault / "notes/compass.md"
        note.parent.mkdir()
        note.write_text("The synthetic violet compass points to the library.\n", encoding="utf-8")
        self.run_python(self.vault / "beyin.py", "sync")
        adapter = self.vault / ".claude/scripts/beyin_v3_dsh.py"
        prompt = {"session_id": "dsh-test-session", "hook_event_name": "UserPromptSubmit",
                  "prompt": "Where does the violet compass point?"}
        inside = self.run_python(adapter, "--vault", self.vault, "--state", self.state,
                                 "--event", "UserPromptSubmit", payload=prompt)
        parsed = json.loads(inside)
        self.assertIn("violet compass", parsed["hookSpecificOutput"]["additionalContext"])
        # One DSH profile serves every folder: a session outside the vault must see nothing.
        outside = self.run_python(adapter, "--vault", self.vault, "--state", self.state,
                                  "--event", "UserPromptSubmit", payload=prompt,
                                  cwd=self.profile_dir)
        self.assertEqual(json.loads(outside), {})
        quiet = self.run_python(adapter, "--vault", self.vault, "--state", self.state,
                                "--event", "Stop", payload={"session_id": "dsh-test-session"})
        self.assertEqual(json.loads(quiet), {})
        broken = subprocess.run([sys.executable, str(adapter), "--vault", str(self.vault),
                                 "--state", str(self.state), "--event", "UserPromptSubmit"],
                                input="\ufeffnot json", text=True, encoding="utf-8",
                                capture_output=True, cwd=str(self.vault), env=self.env, timeout=60)
        self.assertEqual(broken.returncode, 0, broken.stderr)
        self.assertEqual(broken.stdout.strip(), "{}")
        if os.name != "nt":
            # Run the registered command exactly as DSH's shell bridge does.
            command = self.hook_handlers()["UserPromptSubmit"][0]["hooks"][0]["command"]
            actual = subprocess.run(command, shell=True, input=json.dumps(prompt), text=True,
                                    encoding="utf-8", capture_output=True, cwd=str(self.vault),
                                    env=self.env, timeout=60)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertIn("violet compass", actual.stdout)

    def test_profile_entry_is_marked_idempotent_backed_up_and_reversible(self):
        self.install()
        original = self.profile.read_text(encoding="utf-8")
        applied = self.cli("dsh-profile", "apply", "--profile", self.profile_dir)
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(Path(applied["backup"]).is_file())
        self.assertEqual(applied["hook_entry"]["status"], "foreign")
        self.assertIn("configPath", applied["warnings"][0])
        text = self.profile.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original.rstrip("\n") + "\n"))
        self.assertIn("- id: sandbox-policy", text)
        self.assertIn(PROFILE_ENTRY, text)
        self.assertIn("    mode: workspace-write", text)
        self.assertIn(json.dumps(str(self.state.resolve())), text)
        self.assertIn("# beyin-v3:begin sandbox-policy", text)
        self.assertEqual(text.count("- id: hooks-claude-code"), 1)
        backups = sorted(self.profile_dir.glob("cordis.patch.yml.bak-beyin-*"))
        again = self.cli("dsh-profile", "apply", "--profile", self.profile_dir)
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(sorted(self.profile_dir.glob("cordis.patch.yml.bak-beyin-*")), backups)
        status = self.cli("dsh-profile", "status", "--profile", self.profile_dir)
        self.assertEqual(status["status"], "present")
        self.assertTrue(status["current"])
        self.assertEqual(status["hook_entry"]["status"], "foreign")
        self.assertTrue(status["hook_config_referenced"])
        removed = self.cli("dsh-profile", "remove", "--profile", self.profile_dir)
        self.assertTrue(removed["removed"])
        self.assertEqual(self.profile.read_text(encoding="utf-8"), original)
        self.assertFalse((self.state / "dsh-profile.json").exists())
        self.assertEqual(self.cli("dsh-profile", "remove", "--profile", self.profile_dir)["status"], "absent")
        self.assertEqual(self.cli("dsh-profile", "status", "--profile", self.profile_dir)["status"], "absent")

    def test_unmanaged_sandbox_entry_is_reported_and_never_rewritten(self):
        self.install()
        self.profile.write_text(
            "- id: sandbox-policy\n"
            '  name: "@deepseek-ai/dsh-sandbox-policy"\n'
            "  config:\n"
            "    mode: danger-full-access\n",
            encoding="utf-8")
        before = self.profile.read_bytes()
        result = self.cli("dsh-profile", "apply", "--profile", self.profile_dir)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["applied"])
        self.assertIn("workspaceRoot", result["warning"])
        text = self.profile.read_text(encoding="utf-8")
        self.assertIn("danger-full-access", text)
        self.assertNotIn("    workspaceRoot:", text)
        self.assertIn("- id: hooks-claude-code", text)
        # The profile is recorded so doctor keeps reporting the unmanaged entry it could not use.
        status = self.cli("dsh-profile", "status", "--profile", self.profile_dir)
        self.assertEqual(status["status"], "foreign")
        self.assertEqual(status["hook_entry"]["status"], "present")
        self.assertTrue(status["hook_entry"]["current"])
        self.assertTrue(status["hook_config_referenced"])
        removed = self.cli("dsh-profile", "remove", "--profile", self.profile_dir)
        self.assertTrue(removed["removed"])
        self.assertEqual(self.profile.read_bytes(), before)

    def test_flow_style_profile_is_refused_instead_of_guessed(self):
        self.install()
        self.profile.write_text("[{id: agent-default-model}]\n", encoding="utf-8")
        before = self.profile.read_bytes()
        result = self.cli("dsh-profile", "apply", "--profile", self.profile_dir)
        self.assertFalse(result["applied"])
        self.assertIn("sequence", result["warning"])
        self.assertEqual(self.profile.read_bytes(), before)

    def test_a_moved_state_root_needs_an_explicit_replace(self):
        self.install()
        adapter = load_adapter()
        self.cli("dsh-profile", "apply", "--profile", self.profile_dir)
        other = Path(self.temp.name) / "state-two"
        status = adapter.read_profile_status(self.profile, other, self.vault)
        self.assertEqual(status["status"], "outdated")
        before = self.profile.read_bytes()
        refused = adapter.apply_profile(self.profile, other, self.vault)
        self.assertFalse(refused["applied"])
        self.assertIn("--replace", refused["warning"])
        self.assertEqual(self.profile.read_bytes(), before)
        replaced = adapter.apply_profile(self.profile, other, self.vault, replace=True)
        self.assertTrue(replaced["applied"])
        self.assertTrue(replaced["replaced"])
        self.assertIn(json.dumps(str(other.resolve())), self.profile.read_text(encoding="utf-8"))
        self.assertEqual(self.profile.read_text(encoding="utf-8").count("- id: sandbox-policy"), 1)

    def test_clean_profile_gets_both_managed_blocks_and_remove_restores_it(self):
        self.install()
        original = (
            "# Synthetic profile\n"
            "- id: agent-default-model\n"
            '  name: "@deepseek-ai/dsh-agent-default-model"\n'
            "  config:\n"
            "    provider: synthetic\n")
        self.profile.write_text(original, encoding="utf-8")
        applied = self.cli("dsh-profile", "apply", "--profile", self.profile)
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["hook_entry"]["status"], "present")
        self.assertTrue(applied["hook_entry"]["current"])
        text = self.profile.read_text(encoding="utf-8")
        self.assertEqual(text.count("- id: sandbox-policy"), 1)
        self.assertEqual(text.count("- id: hooks-claude-code"), 1)
        self.assertIn("beyin-v3-hooks.json", text)
        self.assertIn("# beyin-v3:begin hooks-claude-code", text)
        again = self.cli("dsh-profile", "apply", "--profile", self.profile)
        self.assertTrue(again.get("unchanged"))
        self.assertEqual(self.profile.read_text(encoding="utf-8"), text)
        removed = self.cli("dsh-profile", "remove", "--profile", self.profile)
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["removed_blocks"], ["hooks-claude-code", "sandbox-policy"])
        self.assertEqual(self.profile.read_text(encoding="utf-8"), original)

    def test_hook_reconcile_repairs_dsh_commands_without_dropping_the_adapter(self):
        self.install()
        path = self.vault / HOOK_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        for groups in data["hooks"].values():
            for group in groups:
                for handler in group["hooks"]:
                    handler["command"] = ('"C:/old/place/python.exe" "C:/old/place/.claude/scripts/'
                                          'beyin_v3_dsh.py" --vault "C:/old/place" --state "C:/old/state" '
                                          '--event SessionStart')
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.cli("hook-reconcile", "--force")
        self.assertEqual(result["status"], "reconciled")
        files = {item["file"]: item for item in result["files"]}
        self.assertIn(HOOK_FILE, files)
        self.assertEqual(files[HOOK_FILE]["reconciled"], 4)
        from v3_hook_reconcile_test import decoded_command
        repaired = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        for event, groups in repaired.items():
            command = decoded_command(groups[0]["hooks"][0]["command"])
            self.assertIn("beyin_v3_dsh.py", command)
            self.assertIn(str(self.vault), command)
            self.assertIn("--vault", command)
            self.assertIn("--event " + event, command)
            self.assertNotIn("--harness", command)

    def test_install_with_dsh_profile_applies_it_and_uninstall_removes_it(self):
        result = self.install("--dsh-profile", self.profile_dir)
        self.assertEqual(result["dsh_profile"]["status"], "applied")
        self.assertIn("- id: sandbox-policy", self.profile.read_text(encoding="utf-8"))
        record = json.loads((self.state / "dsh-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(record["patch"]), self.profile.resolve())
        uninstalled = self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state, "--uninstall")
        self.assertEqual(json.loads(uninstalled)["dsh_profile"]["status"], "removed")
        self.assertNotIn("- id: sandbox-policy", self.profile.read_text(encoding="utf-8"))
        self.assertFalse((self.state / "dsh-profile.json").exists())

    def test_doctor_reports_hook_config_and_profile(self):
        self.install("--dsh-profile", self.profile_dir)
        self.cli("sync")
        doctor = self.cli("doctor", "--json")
        report = doctor["dsh"]
        self.assertTrue(report["hook_config"]["present"])
        self.assertTrue(report["hook_config"]["adapter_pinned"])
        self.assertEqual(len(report["hook_config"]["events"]), 4)
        self.assertTrue(report["state_writable"])
        self.assertEqual(report["profile"]["status"], "present")
        self.assertTrue(report["profile"]["hook_config_referenced"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(doctor["hook_paths"]["status"], "ok")

    def test_profile_discovery_uses_only_the_given_home(self):
        adapter = load_adapter()
        home = Path(self.temp.name) / "home"
        profile = home / ".dsh/profiles/synthetic"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text("- id: synthetic\n", encoding="utf-8")
        self.assertEqual(adapter.discover_profiles(home), [profile / "cordis.patch.yml"])
        self.assertEqual(adapter.discover_profiles(Path(self.temp.name) / "empty"), [])

    def test_dsh_adapter_can_be_excluded_and_restored(self):
        self.install()
        self.assertTrue((self.vault / HOOK_FILE).is_file())
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state,
                        "--exclude-component", "adapters/dsh")
        self.assertFalse((self.vault / HOOK_FILE).exists())
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state,
                        "--include-component", "adapters/dsh")
        self.assertTrue((self.vault / HOOK_FILE).is_file())

    def test_update_adds_dsh_to_older_install_and_rollback_removes_it(self):
        self.install()
        self.assertEqual(self.cli("dsh-profile", "apply", "--profile", self.profile_dir)["status"], "applied")
        dsh_paths = [HOOK_FILE, ".claude/scripts/beyin_v3_dsh.py"]
        manifest_path = self.state / "v3-install.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name in dsh_paths:
            (self.vault / name).unlink()
            manifest["files"].pop(name)
        manifest["commands"] = [command for command in manifest["commands"] if "beyin_v3_dsh.py" not in command]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        package = Path(self.temp.name) / "dsh-upgrade.zip"
        self.run_python(ROOT / "scripts/build_v3_release.py", "--output", package, "--version", "3.99.0")
        updated = self.cli("update", "--package", package)
        self.assertEqual(updated["status"], "updated")
        self.assertIn(HOOK_FILE, updated["trust_review_files"])
        self.assertTrue(all((self.vault / name).is_file() for name in dsh_paths))
        rolled = self.cli("rollback")
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertTrue(all(not (self.vault / name).exists() for name in dsh_paths))
        # The rollback target has no DSH adapter, so its profile blocks cannot be used anymore.
        self.assertNotIn("- id: sandbox-policy", self.profile.read_text(encoding="utf-8"))
        self.assertFalse((self.state / "dsh-profile.json").exists())

    def test_release_contains_dsh_adapter(self):
        package = Path(self.temp.name) / "synthetic-dsh.zip"
        self.run_python(ROOT / "scripts/build_v3_release.py", "--output", package, "--version", "3.99.0")
        import zipfile
        with zipfile.ZipFile(package) as archive:
            self.assertIn("template/.claude/scripts/beyin_v3_dsh.py", archive.namelist())


if __name__ == "__main__":
    unittest.main()
