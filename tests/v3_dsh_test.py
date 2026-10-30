"""DeepSeek Harness (dsh) contract: Claude-shaped hooks and a read-only profile report."""
import ast
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
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
# The release whose receipt validator predates dsh; a rollback lands on exactly this code.
RELEASED = "v3.12.0"
SANDBOX_ENTRY = '  name: "@deepseek-ai/dsh-sandbox-policy"'
HOOK_ENTRY = '      name: "@deepseek-ai/dsh-hooks-claude-code"'
MODE_VALUE = "!!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'"
MODE_EXPRESSION = "mode: " + MODE_VALUE


def load_adapter():
    spec = importlib.util.spec_from_file_location("beyin_test_dsh", SCRIPTS / "beyin_v3_dsh.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_script(name):
    """Import one adjacent script by path so its own directory stays importable."""
    spec = importlib.util.spec_from_file_location("beyin_test_dsh_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipt_source(event_id):
    return "receipts/" + hashlib.sha256(event_id.encode("utf-8")).hexdigest() + ".md"


def skip_or_fail(test, reason):
    """Local clones may lack a ref; in CI that means the checkout is too shallow to prove anything."""
    if os.environ.get("CI"):
        test.fail(reason + "; CI must check out the full history (actions/checkout fetch-depth: 0)")
    test.skipTest(reason)


def harness_choices(path):
    """Every `choices=(...)` on a `--harness` argument in a source file, in source order."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--harness"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices":
                found.append(tuple(ast.literal_eval(keyword.value)))
    return found


def assigned_constant(path_or_text, name, rev=None):
    """Literal value of a module-level assignment, from a file, a blob or a git ref's copy."""
    if rev is None:
        text = (path_or_text.read_text(encoding="utf-8") if isinstance(path_or_text, Path) else path_or_text)
    else:
        text = subprocess.run(["git", "show", f"{rev}:{path_or_text}"], cwd=str(ROOT),
                              capture_output=True, check=True).stdout.decode("utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name
                                                for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


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
            self.assertRegex(command, r"--event'?\s+'?" + event + r"\b")
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
        expected = (self.vault / HOOK_FILE).resolve().as_posix()  # the note's JSON path form
        self.assertTrue(any(expected in note for note in setup["notes"]), setup["notes"])

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

    def test_doctor_with_several_profiles_still_prints_the_entries(self):
        self.install()
        for name in ("desktop", "headless"):
            self.write_profile("- id: agent-default-model\n", name=name)
        report = self.doctor()["dsh"]
        self.assertEqual(report["status"], "manual_setup")
        self.assertEqual(len(report["profiles"]), 2)
        setup = report["manual_setup"]
        self.assertIsNone(setup["path"])
        self.assertIn("- id: sandbox-policy", setup["block"])
        self.assertIn("- id: hooks-claude-code", setup["block"])
        self.assertTrue(any("desktop" in note and "headless" in note for note in setup["notes"]))

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
            self.assertRegex(command, r"--event'?\s+'?" + event + r"\b")
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

    def test_update_adds_dsh_files_and_rollback_removes_them(self):
        """Package upgrade and rollback of the two dsh files.

        Deleting the files only fakes the file layout of an install that never received
        dsh; it is no evidence that an older install can still read what the new code
        writes, and it never ran an older validator. That claim belongs to
        DshReceiptCompatibilityTest, which syncs a dsh receipt with the released code.
        """
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


class DshReceiptCompatibilityTest(unittest.TestCase):
    """A receipt written by the dsh build must survive a rollback to the released validator."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-dsh-receipt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.sync_module = load_script("beyin_v3_sync")
        self.engine = self.sync_module.SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: getattr(self.engine.store, "close", lambda: None)())
        self.note = self.vault / "notes/dsh-work.md"
        self.note.parent.mkdir()
        self.note.write_text("Synthetic dsh checkpoint source.\n", encoding="utf-8")

    def dsh_checkpoint(self, session="dsh-session-1", state=None):
        """Record a real dsh checkpoint and return the session id the projections store."""
        state = state or self.state
        hook = load_script("beyin_v3_hook")
        queue = state / "hook-queue"
        queue.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for offset, event in enumerate(("UserPromptSubmit", "PostToolUse", "Stop")):
            item = queue / f"{int((now + offset) * 1000)}-{event}.json"
            item.write_text(json.dumps({"harness": "dsh", "event": event, "session": session,
                                        "at": now + offset}), encoding="utf-8")
        hook.drain_queue(self.vault, state)
        gaps = json.loads((state / "receipt-gaps.json").read_text(encoding="utf-8"))
        self.assertEqual([item["harness"] for item in gaps["checkpoints"]], ["dsh"])
        return gaps["checkpoints"][0]["session"]

    def write_receipt(self, event_id, metadata, summary="Synthetic outcome.\n"):
        metadata = dict({"kind": "receipt", "event_id": event_id, "visibility": "internal",
                         "refs": ["notes/dsh-work.md"]}, **metadata)
        path = self.vault / receipt_source(event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + json.dumps(metadata, ensure_ascii=False) + "\n---\n" + summary,
                        encoding="utf-8", newline="\n")
        return path

    def old_release_scripts(self):
        """Extract every script of the released tag into its own directory, never mixed with this tree."""
        available = subprocess.run(["git", "rev-parse", "--verify", "-q", RELEASED + "^{commit}"],
                                   cwd=str(ROOT), capture_output=True, text=True)
        if available.returncode != 0:
            skip_or_fail(self, f"{RELEASED} cannot be read from this clone (git missing, shallow or squashed "
                               "history); the released validator cannot be extracted, so the rollback path stays unproven")
        listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", RELEASED,
                                  "--", "template/.claude/scripts/"],
                                 cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                 check=True).stdout.split()
        target = self.root / (RELEASED + "-scripts")
        target.mkdir()
        for entry in listing:
            if not entry.endswith(".py"):
                continue
            # Bytes as released: a text pipe would decode with the Windows ANSI code page.
            blob = subprocess.run(["git", "show", f"{RELEASED}:{entry}"], cwd=str(ROOT),
                                  capture_output=True, check=True).stdout
            (target / Path(entry).name).write_bytes(blob)
        self.assertTrue((target / "beyin_v3_sync.py").is_file())
        return target

    def test_the_written_receipt_names_the_client_a_released_validator_accepts(self):
        session = self.dsh_checkpoint()
        result = self.engine.receipt("dsh-outcome-1", "Synthetic dsh outcome.",
                                     ["notes/dsh-work.md"], "dsh", session=session)
        self.assertEqual(result["status"], "succeeded")
        source = receipt_source("dsh-outcome-1")
        metadata, body = self.sync_module.parse((self.vault / source).read_text(encoding="utf-8"))
        self.assertEqual(metadata["harness"], "manual")
        self.assertEqual(metadata["client"], "dsh")
        self.assertEqual(metadata["event_id"], "dsh-outcome-1")
        self.assertEqual(metadata["refs"], ["notes/dsh-work.md"])
        self.assertEqual(metadata["session"], session)
        self.assertIn("created_at", metadata)
        self.assertEqual(body, "Synthetic dsh outcome.\n")
        # This build still attributes the receipt to dsh: the stored name is not the identity.
        self.assertEqual(self.engine._receipt_event(source)["harness"], "dsh")
        with self.engine.store._connect() as db:
            row = db.execute("SELECT payload FROM receipts WHERE id=?", ("dsh-outcome-1",)).fetchone()
        self.assertEqual(json.loads(row[0])["harness"], "dsh")
        # Re-submitting the same outcome is idempotent, not a manual-change conflict.
        again = self.engine.receipt("dsh-outcome-1", "Synthetic dsh outcome.",
                                    ["notes/dsh-work.md"], "dsh", session=session)
        self.assertEqual(again["status"], "succeeded")

    def test_the_dsh_checkpoint_is_closed_by_its_receipt(self):
        session = self.dsh_checkpoint()
        gaps = json.loads((self.state / "receipt-gaps.json").read_text(encoding="utf-8"))
        self.assertEqual(gaps["potential_missing_receipts"], 1)
        self.engine.receipt("dsh-outcome-2", "Synthetic dsh outcome.",
                            ["notes/dsh-work.md"], "dsh", session=session)
        self.engine.sync(full=True)
        gaps = json.loads((self.state / "receipt-gaps.json").read_text(encoding="utf-8"))
        self.assertEqual(gaps["potential_missing_receipts"], 0)
        self.assertEqual(gaps["checkpoints"], [])
        self.assertEqual(gaps["receipt_coverage"]["covered"], 1)
        self.assertEqual(gaps["receipt_coverage"]["missing"], 0)

    def test_an_unusable_client_leaves_the_receipt_manual(self):
        for index, (harness, client) in enumerate((("manual", "not-a-client"), ("manual", 7),
                                                   ("manual", ["dsh"]), ("manual", None),
                                                   ("claude", "dsh"))):
            event_id = f"manual-outcome-{index}"
            metadata = {"harness": harness}
            if client is not None:
                metadata["client"] = client
            source = receipt_source(event_id)
            self.write_receipt(event_id, metadata)
            event = self.engine._receipt_event(source)
            expected = "manual" if harness == "manual" else "claude"
            self.assertEqual(event["harness"], expected, (harness, client))

    def run_released_sync(self, old_scripts, state, event_id):
        """Sync the vault with the released code in its own process and report what it saw."""
        driver = self.root / "old_release_sync.py"
        driver.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "import sqlite3\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import beyin_v3_sync\n"
            "engine = beyin_v3_sync.SyncEngine(Path(sys.argv[2]), Path(sys.argv[3]))\n"
            "result = engine.sync()\n"
            "source = 'receipts/' + __import__('hashlib').sha256(sys.argv[4].encode()).hexdigest() + '.md'\n"
            "source_path = Path(sys.argv[2]) / source\n"
            "with sqlite3.connect(str(Path(sys.argv[3]) / 'memory.sqlite3')) as db:\n"
            "    rows = [json.loads(payload) for (payload,) in db.execute('SELECT payload FROM receipts')]\n"
            "print(json.dumps({'status': result.get('status'),\n"
            "                  'warnings': result.get('warnings', []),\n"
            "                  'conflicts': result.get('conflicts', []),\n"
            "                  'receipt_event': engine._receipt_event(source) if source_path.exists() else None,\n"
            "                  'rows': rows}))\n",
            encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", str(driver), str(old_scripts), str(self.vault), str(state), event_id],
            text=True, encoding="utf-8", capture_output=True, cwd=str(self.root),
            env=inherited_env(PYTHONDONTWRITEBYTECODE="1", BEYIN_V3_NO_SPAWN="1"), timeout=180)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_rollback_to_the_released_validator_still_syncs_the_receipt(self):
        old_scripts = self.old_release_scripts()
        session = self.dsh_checkpoint()
        self.engine.receipt("dsh-outcome-rollback", "Synthetic dsh rollback outcome.",
                            ["notes/dsh-work.md"], "dsh", session=session)
        # A rollback restores the vault, not the state directory it already wrote.
        report = self.run_released_sync(old_scripts, self.state, "dsh-outcome-rollback")
        reasons = [item.get("reason", "") for item in report["warnings"] + report["conflicts"]]
        self.assertEqual(report["status"], "succeeded", reasons)
        self.assertFalse([reason for reason in reasons if "invalid harness" in reason], reasons)
        self.assertEqual(report["receipt_event"]["harness"], "manual")
        self.assertEqual(report["receipt_event"]["event_id"], "dsh-outcome-rollback")
        self.assertEqual(report["receipt_event"]["session"], session)
        # A reset state re-indexes the same file from disk under the released name.
        cold = self.run_released_sync(old_scripts, self.root / "cold-state", "dsh-outcome-rollback")
        cold_reasons = [item.get("reason", "") for item in cold["warnings"] + cold["conflicts"]]
        self.assertFalse([reason for reason in cold_reasons if "invalid harness" in reason], cold_reasons)
        # Receipt sources are all accepted; the view files a later release formats differently are
        # preservation notices, not receipt failures, and they exist with or without dsh.
        self.assertFalse([item['source'] for item in cold["warnings"] + cold["conflicts"]
                          if item.get("source", "").startswith("receipts/")], cold_reasons)
        self.assertEqual([row["harness"] for row in cold["rows"]], ["manual"])
        self.assertEqual([row["event_id"] for row in cold["rows"]], ["dsh-outcome-rollback"])
        self.assertEqual([row["session"] for row in cold["rows"]], [session])

    def test_a_rescan_restores_a_released_index_to_dsh(self):
        old_scripts = self.old_release_scripts()
        cold_state = self.root / "cold-state"
        # The released build has already synced this vault once, so its receipt views are tracked
        # and the scan below is the clean pass that records a signature.
        first = self.run_released_sync(old_scripts, cold_state, "dsh-outcome-repair")
        self.assertEqual(first["status"], "succeeded", first["warnings"])
        # A dsh turn recorded its checkpoint before the candidate build wrote the receipt.
        session = self.dsh_checkpoint(state=cold_state)
        self.write_receipt("dsh-outcome-repair", {"harness": "manual", "client": "dsh", "session": session,
                                                  "created_at": datetime.now(timezone.utc).isoformat()},
                           summary="Synthetic dsh repair outcome.\n")
        # Age the directory past the racy window so the released build stores its scan signature;
        # otherwise a missing signature, not the format change, would explain the later rescan.
        receipts_dir = self.vault / "receipts"
        settled = time.time() - 10
        os.utime(receipts_dir, (settled, settled))
        # The state the rollback left behind: the released build indexed the file as manual.
        cold = self.run_released_sync(old_scripts, cold_state, "dsh-outcome-repair")
        self.assertEqual(cold["status"], "succeeded", cold["warnings"])
        self.assertEqual([row["harness"] for row in cold["rows"]], ["manual"])
        with closing(sqlite3.connect(str(cold_state / "memory.sqlite3"))) as db:
            stored = db.execute("SELECT value FROM metadata WHERE key='receipt_scan_signature'").fetchone()
        self.assertIsNotNone(stored, "released build stored no scan signature; the rescan below would pass trivially")
        # This build returns, scans the same receipts again and reads the client from the source.
        engine = self.sync_module.SyncEngine(self.vault, cold_state)
        self.addCleanup(lambda: getattr(engine.store, "close", lambda: None)())
        engine.sync()
        with engine.store._connect() as db:
            payload = db.execute("SELECT payload FROM receipts WHERE id=?",
                                 ("dsh-outcome-repair",)).fetchone()[0]
        self.assertEqual(json.loads(payload)["harness"], "dsh")
        gaps = json.loads((cold_state / "receipt-gaps.json").read_text(encoding="utf-8"))
        self.assertEqual(gaps["potential_missing_receipts"], 0)
        self.assertEqual(gaps["checkpoints"], [])
        self.assertEqual(gaps["receipt_coverage"]["covered"], 1)

    def test_resubmitting_after_the_released_build_keeps_the_immutable_source(self):
        old_scripts = self.old_release_scripts()
        session = self.dsh_checkpoint()
        self.engine.receipt("dsh-outcome-old", "Synthetic old-build outcome.",
                            ["notes/dsh-work.md"], "dsh", session=session)
        source = self.vault / receipt_source("dsh-outcome-old")
        original = source.read_bytes()
        # A rollback indexed the file as plain manual in its own state.
        cold_state = self.root / "old-build-state"
        cold = self.run_released_sync(old_scripts, cold_state, "dsh-outcome-old")
        self.assertEqual([row["harness"] for row in cold["rows"]], ["manual"])
        # This build resubmits the same logical receipt: the source stays as written and the
        # stored row regains the client the released index dropped.
        engine = self.sync_module.SyncEngine(self.vault, cold_state)
        self.addCleanup(lambda: getattr(engine.store, "close", lambda: None)())
        again = engine.receipt("dsh-outcome-old", "Synthetic old-build outcome.",
                               ["notes/dsh-work.md"], "dsh", session=session)
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(source.read_bytes(), original)
        with engine.store._connect() as db:
            payload = db.execute("SELECT payload FROM receipts WHERE id=?",
                                 ("dsh-outcome-old",)).fetchone()[0]
        self.assertEqual(json.loads(payload)["harness"], "dsh")

    def test_resubmitting_a_bare_client_name_keeps_the_immutable_source(self):
        path = self.write_receipt("dsh-outcome-bare", {"harness": "dsh"})
        original = path.read_bytes()
        result = self.engine.receipt("dsh-outcome-bare", "Synthetic outcome.",
                                     ["notes/dsh-work.md"], "dsh")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.engine._receipt_event(receipt_source("dsh-outcome-bare"))["harness"], "dsh")

    def test_no_harness_claims_a_plain_manual_receipt(self):
        # First-writer attribution: a resubmission never renames a manual source, client or not.
        for harness in ("claude", "dsh"):
            with self.subTest(harness=harness):
                event_id = "manual-outcome-" + harness
                path = self.write_receipt(event_id, {"harness": "manual"})
                original = path.read_bytes()
                result = self.engine.receipt(event_id, "Synthetic outcome.", ["notes/dsh-work.md"], harness)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(path.read_bytes(), original)
                with self.engine.store._connect() as db:
                    payload = db.execute("SELECT payload FROM receipts WHERE id=?", (event_id,)).fetchone()[0]
                self.assertEqual(json.loads(payload)["harness"], "manual")

    def test_a_changed_summary_is_still_a_conflict(self):
        self.write_receipt("dsh-outcome-summary", {"harness": "manual", "client": "dsh"})
        with self.assertRaises(self.sync_module.ReceiptConflict):
            self.engine.receipt("dsh-outcome-summary", "Different outcome.",
                                ["notes/dsh-work.md"], "dsh")

    def test_every_client_harness_writes_manual_plus_client_and_reads_back(self):
        v3 = load_script("beyin_v3")
        clients = [name for name in v3.HARNESSES if name not in v3.RECEIPT_NATIVE_HARNESSES]
        self.assertTrue(clients, "no harness outside the released validator's list")
        for harness in clients:
            with self.subTest(harness=harness):
                written, client = v3.receipt_harness_fields(harness)
                self.assertEqual((written, client), ("manual", harness))
                self.assertEqual(v3.receipt_harness({"harness": written, "client": client}), harness)
        for harness in v3.RECEIPT_NATIVE_HARNESSES:
            with self.subTest(native=harness):
                self.assertEqual(v3.receipt_harness_fields(harness), (harness, None))
                self.assertEqual(v3.receipt_harness({"harness": harness}), harness)

    def test_the_native_list_is_the_released_validator_and_choices_cover_every_harness(self):
        v3 = load_script("beyin_v3")
        available = subprocess.run(["git", "rev-parse", "--verify", "-q", RELEASED + "^{commit}"],
                                   cwd=str(ROOT), capture_output=True, text=True)
        if available.returncode != 0:
            skip_or_fail(self, f"{RELEASED} cannot be read from this clone; the native list cannot be compared "
                               "with the released validator")
        released = assigned_constant("template/.claude/scripts/beyin_v3.py", "HARNESSES", rev=RELEASED)
        self.assertEqual(tuple(v3.RECEIPT_NATIVE_HARNESSES), tuple(released))
        expected = set(v3.HARNESSES)
        cli = harness_choices(ROOT / "scripts/beyin_v3.py")
        client_cli = [choices for choices in cli if "dsh" in choices]
        self.assertEqual(len(client_cli), 2, f"context and receipt name every client: {cli}")
        for choices in client_cli:
            self.assertEqual(sorted(expected - set(choices)), [], choices)
        hook = harness_choices(SCRIPTS / "beyin_v3_hook.py")
        self.assertEqual(len(hook), 1, hook)
        self.assertEqual(sorted(expected - set(hook[0])), [], hook[0])


if __name__ == "__main__":
    unittest.main()
