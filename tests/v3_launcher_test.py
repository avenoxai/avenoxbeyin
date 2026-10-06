"""Unit tests for the platform-aware smart hook launcher and hook reconciliation."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "template/.claude/scripts"
LAUNCHER = SCRIPTS / "beyin_v3_launcher.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_launcher():
    spec = importlib.util.spec_from_file_location("beyin_v3_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["beyin_v3_launcher"] = module
    spec.loader.exec_module(module)
    return module


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-launcher-test-")
        self.addCleanup(self.tmp.cleanup)
        os.environ["BEYIN_V3_NO_SPAWN"] = "1"
        self.addCleanup(lambda: os.environ.pop("BEYIN_V3_NO_SPAWN", None))
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.claude_scripts = self.vault / ".claude/scripts"
        self.claude_scripts.mkdir(parents=True)
        # Copy hook and launcher into test vault
        (self.claude_scripts / "beyin_v3_hook.py").write_bytes((SCRIPTS / "beyin_v3_hook.py").read_bytes())
        (self.claude_scripts / "beyin_v3_launcher.py").write_bytes(LAUNCHER.read_bytes())
        self.launcher = load_launcher()

    def test_is_foreign_command_detection(self):
        win_powershell = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -NonInteractive -EncodedCommand JgAgAC4ALgAu"
        posix_python = "'/usr/bin/python3' '/Users/ada/vault/.claude/scripts/beyin_v3_hook.py' '--vault' '/Users/ada/vault'"
        if sys.platform == "win32":
            self.assertTrue(self.launcher.is_foreign_command(posix_python))
            self.assertFalse(self.launcher.is_foreign_command(win_powershell))
        else:
            self.assertTrue(self.launcher.is_foreign_command(win_powershell))
            self.assertFalse(self.launcher.is_foreign_command(posix_python))
        self.assertFalse(self.launcher.is_foreign_command(""))

    def test_resolve_vault_and_state_with_foreign_paths(self):
        foreign_vault = "/Users/ada/ForeignVault" if sys.platform == "win32" else "C:\\Users\\ada\\ForeignVault"
        foreign_state = "/Users/ada/ForeignState" if sys.platform == "win32" else "C:\\Users\\ada\\ForeignState"

        resolved_vault = self.launcher.resolve_vault(foreign_vault)
        # Should fallback to current test vault (where launcher exists)
        self.assertTrue((resolved_vault / ".claude/scripts/beyin_v3_hook.py").is_file())

        resolved_state = self.launcher.resolve_state(resolved_vault, foreign_state)
        # Foreign state should fallback to _default_state outside vault
        expected_default = self.launcher._default_state(resolved_vault)
        self.assertEqual(resolved_state, expected_default)

    def test_reconcile_foreign_hook_files(self):
        agents_dir = self.vault / ".agents"
        agents_dir.mkdir(parents=True)
        hooks_json = agents_dir / "hooks.json"
        claude_dir = self.vault / ".claude"
        settings_local = claude_dir / "settings.local.json"
        codex_dir = self.vault / ".codex"
        codex_dir.mkdir(parents=True)
        codex_hooks = codex_dir / "hooks.json"

        # Seed foreign commands
        posix_seed, win_seed = self.launcher.local_commands([
            "/usr/bin/python3" if sys.platform == "win32" else "C:/Python/python.exe",
            "/Users/ada/vault/.claude/scripts/beyin_v3_hook.py" if sys.platform == "win32" else "C:/vault/.claude/scripts/beyin_v3_hook.py",
            "--vault",
            "/Users/ada/vault" if sys.platform == "win32" else "C:/vault",
        ])
        foreign_cmd = posix_seed if sys.platform == "win32" else win_seed

        hooks_json.write_text(json.dumps({
            "beyin-v3": {
                "PreInvocation": [{"type": "command", "command": foreign_cmd, "timeout": 20}],
                "Stop": [{"type": "command", "command": foreign_cmd, "timeout": 20}],
            }
        }), encoding="utf-8")

        settings_local.write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": foreign_cmd, "timeout": 20}]}]
            }
        }), encoding="utf-8")

        codex_hooks.write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": foreign_cmd, "commandWindows": foreign_cmd, "timeout": 20}]}]
            }
        }), encoding="utf-8")

        results = self.launcher.reconcile_all_hooks(self.vault)
        self.assertTrue(results[".agents/hooks.json"])
        self.assertTrue(results[".claude/settings.local.json"])
        self.assertTrue(results[".codex/hooks.json"])

        # Verify updated files now contain local commands
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
        updated_cmd = data["beyin-v3"]["PreInvocation"][0]["command"]
        self.assertFalse(self.launcher.is_foreign_command(updated_cmd))
        if sys.platform == "win32":
            self.assertIn("powershell.exe", updated_cmd.lower())
        else:
            self.assertNotIn("powershell", updated_cmd.lower())

        # Second run should be a no-op
        second = self.launcher.reconcile_all_hooks(self.vault)
        self.assertFalse(second[".agents/hooks.json"])
        self.assertFalse(second[".claude/settings.local.json"])
        self.assertFalse(second[".codex/hooks.json"])

    def test_sync_engine_reconciles_foreign_hooks(self):
        agents_dir = self.vault / ".agents"
        agents_dir.mkdir(parents=True)
        hooks_json = agents_dir / "hooks.json"
        posix_seed, win_seed = self.launcher.local_commands([
            "/usr/bin/python3" if sys.platform == "win32" else "C:/Python/python.exe",
            "/Users/ada/vault/.claude/scripts/beyin_v3_hook.py" if sys.platform == "win32" else "C:/vault/.claude/scripts/beyin_v3_hook.py",
            "--vault",
            "/Users/ada/vault" if sys.platform == "win32" else "C:/vault",
        ])
        foreign_cmd = posix_seed if sys.platform == "win32" else win_seed
        hooks_json.write_text(json.dumps({
            "beyin-v3": {
                "PreInvocation": [{"type": "command", "command": foreign_cmd, "timeout": 20}]
            }
        }), encoding="utf-8")

        from beyin_v3_sync import SyncEngine
        state = self.launcher._default_state(self.vault)
        engine = SyncEngine(self.vault, state)
        engine.sync()

        data = json.loads(hooks_json.read_text(encoding="utf-8"))
        updated_cmd = data["beyin-v3"]["PreInvocation"][0]["command"]
        self.assertFalse(self.launcher.is_foreign_command(updated_cmd))

    def test_launcher_dispatches_hook_in_process(self):
        import io
        comp = self.vault / "🔮 850-Companion"
        comp.mkdir(parents=True)
        (comp / "Last-Session.md").write_text("# Son oturum\nLauncher verified.\n", encoding="utf-8")
        from beyin_v3_sync import SyncEngine
        state = self.launcher._default_state(self.vault)
        SyncEngine(self.vault, state).sync()

        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "launcher-session"})
        
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(payload)
        sys.stdout = io.StringIO()
        try:
            code = self.launcher.launch([
                "--vault", str(self.vault),
                "--state", str(self.launcher._default_state(self.vault)),
                "--harness", "claude"
            ])
            output = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout

        self.assertEqual(code, 0)
        result = json.loads(output or "{}")
        self.assertIn("hookSpecificOutput", result)
        self.assertIn("Launcher verified", result["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()

