"""DeepSeek Harness (dsh) contract: Claude-shaped hooks and a read-only profile report."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from v3_package_helpers import inherited_env


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_v3.py"
SCRIPTS = ROOT / "template/.claude/scripts"
HOOK_FILE = ".claude/dsh/beyin-v3-hooks.json"
SANDBOX_ENTRY = '  name: "@deepseek-ai/dsh-sandbox-policy"'
HOOK_ENTRY = '      name: "@deepseek-ai/dsh-hooks-claude-code"'
MODE_VALUE = "!!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'"
MODE_EXPRESSION = "mode: " + MODE_VALUE


def load_adapter():
    spec = importlib.util.spec_from_file_location("beyin_test_dsh", SCRIPTS / "beyin_v3_dsh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DshIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-dsh-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "Synthetic DSH Vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = inherited_env(PYTHONDONTWRITEBYTECODE="1", BEYIN_V3_NO_SPAWN="1")

    def run_python(self, script, *args, payload=None, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding="utf-8", capture_output=True,
                                cwd=str(cwd or self.vault), env=env or self.env, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def install(self, *extra):
        return json.loads(self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state, *extra))

    def cli(self, *args, env=None, cwd=None):
        return json.loads(self.run_python(self.vault / "beyin.py", *args, env=env, cwd=cwd))

    def doctor(self, home=None, **extra):
        env = dict(self.env, HOME=str(home or self.home), USERPROFILE=str(home or self.home), **extra)
        return self.cli("doctor", "--json", env=env)

    def hook_handlers(self):
        data = json.loads((self.vault / HOOK_FILE).read_text(encoding="utf-8"))
        return data["hooks"]

    def write_profile(self, text, name="synthetic"):
        profile = self.home / ".dsh/profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        path = profile / "cordis.patch.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def profile_text(self, config_path=None, root=None, mode=MODE_VALUE,
                     preset=None, folded=False):
        """A DSH-shaped profile: a flat sandbox entry and the nested hooks insert."""
        config_path = config_path if config_path is not None else str(self.vault / HOOK_FILE)
        root = root if root is not None else str(self.state)

        def scalar(indent, key, value):
            target = json.dumps(value)
            cut = target.rfind(" ", 0, len(target) // 2 + 12)
            if folded and len(target) > 40 and cut > 0:
                return [" " * indent + key + ": " + target[:cut],
                        " " * (indent + 2) + target[cut + 1:]]
            return [" " * indent + key + ": " + target]

        lines = ["# Synthetic DSH profile patch", "- id: agent-default-model",
                 '  name: "@deepseek-ai/dsh-agent-default-model"', "  config:",
                 "    provider: synthetic", "    model: synthetic-model",
                 "- id: synthetic-plugin", '  name: "@deepseek-ai/dsh-synthetic"',
                 "  config:", "    args:", "      - --synthetic", "      - --quiet"]
        lines += ["- id: sandbox-policy", SANDBOX_ENTRY, "  config:", "    mode: " + mode]
        lines += scalar(4, "workspaceRoot", root)
        if preset:
            lines += ["- id: permission", '  name: "@deepseek-ai/dsh-permission-presets"',
                      "  config:", "    defaultPreset: " + preset]
        lines += ["- insert:", "    - id: hooks-claude-code", HOOK_ENTRY, "      config:"]
        lines += scalar(8, "configPath", config_path)
        return "\n".join(lines) + "\n"

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
                                  cwd=self.root)
        self.assertEqual(json.loads(outside), {})
        # DSH sends the session directory in the payload; it decides, not the process cwd.
        payload_cwd = dict(prompt, cwd=str(self.vault))
        via_payload = self.run_python(adapter, "--vault", self.vault, "--state", self.state,
                                      "--event", "UserPromptSubmit", payload=payload_cwd, cwd=self.root)
        self.assertIn("violet compass", json.loads(via_payload)["hookSpecificOutput"]["additionalContext"])
        payload_outside = dict(prompt, cwd=str(self.root))
        via_payload_outside = self.run_python(adapter, "--vault", self.vault, "--state", self.state,
                                              "--event", "UserPromptSubmit", payload=payload_outside)
        self.assertEqual(json.loads(via_payload_outside), {})
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

    def test_doctor_prints_the_manual_entries_when_no_profile_exists(self):
        self.install()
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        self.assertEqual(report["profiles"], [])
        setup = report["manual_setup"]
        self.assertIn(".dsh", setup["path"])
        self.assertIn("- id: sandbox-policy", setup["block"])
        self.assertIn(MODE_EXPRESSION, setup["block"])
        self.assertIn("workspaceRoot:", setup["block"])
        self.assertIn("- id: hooks-claude-code", setup["block"])
        self.assertIn("beyin-v3-hooks.json", setup["block"])
        self.assertTrue(any("Start DSH once" in note for note in setup["notes"]))
        # Doctor is a report: it created no profile and no install record.
        self.assertFalse((self.home / ".dsh").exists())
        self.assertFalse((self.state / "dsh-profile.json").exists())

    def test_doctor_reads_a_registered_profile_and_reports_ok(self):
        self.install()
        self.write_profile(self.profile_text(preset="danger-full-access"))
        self.cli("sync")
        done = self.state / "hook-done"
        done.mkdir(parents=True, exist_ok=True)
        (done / (("a" * 64) + ".json")).write_text(
            json.dumps({"harness": "dsh", "event": "SessionStart", "at": time.time() - 30}),
            encoding="utf-8")
        (self.state / "hook-health.json").write_text(
            json.dumps({"at": time.time() - 20, "sync": {"status": "succeeded"}}), encoding="utf-8")
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("manual_setup", report)
        self.assertTrue(report["hook_config"]["present"])
        self.assertTrue(report["hook_config"]["adapter_pinned"])
        self.assertTrue(report["hook_config"]["matcher"])
        self.assertEqual(len(report["hook_config"]["events"]), 4)
        self.assertEqual(report["profiles"][0]["status"], "ok")
        self.assertTrue(report["profiles"][0]["hook"]["current"])
        self.assertTrue(report["profiles"][0]["sandbox"]["covers_state"])
        self.assertTrue(report["profiles"][0]["sandbox"]["env_mode"])
        self.assertEqual(report["profiles"][0]["permission_default_preset"], "danger-full-access")
        # The state report is honest about its own limits: it can only observe.
        state = report["state"]
        self.assertIn("writable", state)
        self.assertEqual(state["last_hook_event"], "SessionStart")
        self.assertGreaterEqual(state["last_hook_age_seconds"], 29)
        self.assertEqual(state["last_sync_status"], "succeeded")
        self.assertEqual(self.doctor()["hook_paths"]["status"], "ok")

    def test_doctor_flags_a_stale_hook_config_path_without_suggesting_a_duplicate(self):
        self.install()
        self.write_profile(self.profile_text(config_path="/old/place/.claude/dsh/beyin-v3-hooks.json"))
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        profile = report["profiles"][0]
        self.assertEqual(profile["hook"]["status"], "outdated")
        self.assertFalse(profile["hook"]["current"])
        self.assertTrue(profile["sandbox"]["covers_state"])
        setup = report["manual_setup"]
        self.assertEqual(setup["block"], "")  # the entries exist; only the value is wrong
        self.assertTrue(any(str(self.vault / HOOK_FILE) in note for note in setup["notes"]))

    def test_doctor_flags_read_only_mode_and_a_missing_workspace_root(self):
        self.install()
        profile = self.write_profile(self.profile_text(mode="read-only"))
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        self.assertEqual(report["profiles"][0]["sandbox"]["mode"], "read-only")
        self.assertTrue(any("read-only" in warning for warning in report["profiles"][0]["warnings"]))
        profile.write_text(self.profile_text(root=""), encoding="utf-8")
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        sandbox = report["profiles"][0]["sandbox"]
        self.assertIsNone(sandbox["workspaceRoot"])
        self.assertFalse(sandbox["covers_state"])
        self.assertTrue(any("workspaceRoot" in warning for warning in report["profiles"][0]["warnings"]))
        self.assertEqual(report["manual_setup"]["block"], "")

    def test_doctor_suggests_the_sandbox_entry_when_only_the_hook_is_registered(self):
        self.install()
        text = self.profile_text()
        sandbox_start = text.index("- id: sandbox-policy")
        sandbox_end = text.index("- insert:")
        self.write_profile(text[:sandbox_start] + text[sandbox_end:])
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        self.assertEqual(report["profiles"][0]["hook"]["status"], "present")
        setup = report["manual_setup"]
        self.assertIn("- id: sandbox-policy", setup["block"])
        self.assertIn(MODE_EXPRESSION, setup["block"])
        self.assertNotIn("- id: hooks-claude-code", setup["block"])

    def test_doctor_warns_when_a_non_default_mode_has_no_default_preset(self):
        self.install()
        self.write_profile(self.profile_text())
        report = self.doctor(DSH_PERMISSION_MODE="danger-full-access")["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        self.assertTrue(any("#7567" in warning for warning in report["profiles"][0]["warnings"]))
        # Without the environment override the bundle default matches a preset, so it is ready.
        self.assertEqual(self.doctor()["dsh"]["profiles"][0]["status"], "ok")

    def test_doctor_reads_folded_and_commented_scalars(self):
        self.install()
        path = self.write_profile(self.profile_text(folded=True))
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith("- id: sandbox-policy"):
                lines[index] = '- id: "sandbox-policy"  # quoted with a trailing comment'
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = self.doctor()["dsh"]
        profile = report["profiles"][0]
        self.assertEqual(report["status"], "ok", profile)
        self.assertEqual(profile["sandbox"]["mode"], MODE_VALUE)
        self.assertTrue(profile["sandbox"]["covers_state"])
        self.assertTrue(profile["hook"]["current"])

    def test_doctor_never_claims_ok_for_an_entry_it_cannot_read(self):
        self.install()
        self.write_profile("[{id: sandbox-policy, config: {mode: read-only}}]\n")
        report = self.doctor()["dsh"]
        self.assertNotEqual(report["status"], "ok")
        self.assertEqual(report["manual_setup"]["block"], "")
        self.assertTrue(any("inspect" in note for note in report["manual_setup"]["notes"]))

    def test_doctor_is_read_only(self):
        self.install()
        profile = self.write_profile(self.profile_text())
        before = profile.read_bytes()
        self.doctor()
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse((self.state / "dsh-profile.json").exists())
        self.assertFalse(list(profile.parent.glob("cordis.patch.yml.bak-beyin-*")))

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

    def test_install_never_writes_a_profile(self):
        profile = self.write_profile(self.profile_text())
        before = profile.read_bytes()
        self.install()
        self.assertEqual(profile.read_bytes(), before)
        self.assertFalse((self.state / "dsh-profile.json").exists())
        # The old write path is gone from the CLI and the installer.
        refused = subprocess.run([sys.executable, str(self.vault / "beyin.py"), "dsh-profile", "apply"],
                                 text=True, encoding="utf-8", capture_output=True,
                                 cwd=str(self.vault), env=self.env, timeout=120)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("invalid choice", refused.stderr)
        self.assertEqual(profile.read_bytes(), before)

    def test_profile_discovery_uses_only_the_given_home(self):
        adapter = load_adapter()
        profile = self.home / ".dsh/profiles/synthetic"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text("- id: synthetic\n", encoding="utf-8")
        self.assertEqual(adapter.discover_profiles(self.home), [profile / "cordis.patch.yml"])
        self.assertEqual(adapter.discover_profiles(self.root / "empty"), [])

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
        self.assertNotIn("dsh_profile", rolled)

    def test_release_contains_dsh_adapter(self):
        package = Path(self.temp.name) / "synthetic-dsh.zip"
        self.run_python(ROOT / "scripts/build_v3_release.py", "--output", package, "--version", "3.99.0")
        import zipfile
        with zipfile.ZipFile(package) as archive:
            self.assertIn("template/.claude/scripts/beyin_v3_dsh.py", archive.namelist())


if __name__ == "__main__":
    unittest.main()
