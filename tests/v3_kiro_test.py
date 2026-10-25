"""Kiro's project hook format, plain-text adapter, and shared skill lifecycle."""
import base64
import json
import re
import shlex
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_v3.py"


class KiroIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v3-kiro-")
        self.addCleanup(self.temp.cleanup)
        # Windows TEMP may use an 8.3 alias; the installer persists resolved paths.
        root = Path(self.temp.name).resolve()
        self.vault = root / "Synthetic Kiro Vault"
        self.vault.mkdir()
        self.state = root / "state"
        self.env = inherited_env(PYTHONDONTWRITEBYTECODE="1", BEYIN_V3_NO_SPAWN="1")

    def run_python(self, script, *args, payload=None, env=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding="utf-8", capture_output=True,
                                cwd=self.vault, env=env or self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def install(self):
        return json.loads(self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state))

    def test_install_hook_schema_skills_and_uninstall(self):
        self.install()
        hook_file = self.vault / ".kiro/hooks/beyin-v3.json"
        data = json.loads(hook_file.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "v1")
        self.assertEqual([h["trigger"] for h in data["hooks"]],
                         ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
        self.assertEqual(data["hooks"][2]["matcher"], "write")
        self.assertTrue(all(h["action"]["type"] == "command" for h in data["hooks"]))
        from v3_hook_reconcile_test import decoded_command
        self.assertTrue(all("beyin_v3_kiro.py" in decoded_command(h["action"]["command"]) for h in data["hooks"]))
        for skill in ("beyin", "beyin-doktor", "beyin-guncelle"):
            canonical = self.vault / ".agents/skills" / skill / "SKILL.md"
            mirror = self.vault / ".kiro/skills" / skill / "SKILL.md"
            self.assertEqual(mirror.read_bytes(), canonical.read_bytes())
        self.install()  # idempotent registration
        self.assertEqual(len(json.loads(hook_file.read_text(encoding="utf-8"))["hooks"]), 4)
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state, "--uninstall")
        self.assertFalse(hook_file.exists())
        self.assertFalse((self.vault / ".kiro/skills/beyin/SKILL.md").exists())

    def test_prompt_context_is_plain_text_and_quiet_events_print_nothing(self):
        self.install()
        note = self.vault / "notes/compass.md"
        note.parent.mkdir()
        note.write_text("The synthetic violet compass points to the library.\n", encoding="utf-8")
        self.run_python(self.vault / "beyin.py", "sync")
        adapter = self.vault / ".claude/scripts/beyin_v3_kiro.py"
        args = ("--vault", self.vault, "--state", self.state)
        env = dict(self.env, USER_PROMPT="Where does the violet compass point?")
        output = self.run_python(adapter, *args, "--event", "UserPromptSubmit",
                                 payload={"session_id": "kiro-test-session", "cwd": str(self.vault)}, env=env)
        self.assertIn("violet compass", output)
        self.assertNotIn("hookSpecificOutput", output)
        if os.name != "nt":
            definitions = json.loads((self.vault / ".kiro/hooks/beyin-v3.json").read_text(encoding="utf-8"))
            command = next(h["action"]["command"] for h in definitions["hooks"]
                           if h["trigger"] == "UserPromptSubmit")
            actual = subprocess.run(command, shell=True, input="\ufeff" + json.dumps({"session_id": "kiro-shell"}),
                                    text=True, encoding="utf-8", capture_output=True,
                                    cwd=self.vault, env=env, timeout=30)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertIn("violet compass", actual.stdout)
        quiet = self.run_python(adapter, *args, "--event", "Stop",
                                payload={"session_id": "kiro-test-session", "cwd": str(self.vault)})
        self.assertEqual(quiet, "")
        doctor = json.loads(self.run_python(self.vault / "beyin.py", "doctor"))
        self.assertEqual(doctor["hook_paths"]["status"], "ok")
        self.assertGreaterEqual(doctor["acknowledged_events"], 0)

    def test_kiro_skill_edit_syncs_and_divergence_is_preserved(self):
        self.install()
        cli = self.vault / "beyin.py"
        self.run_python(cli, "skill-sync")
        kiro = self.vault / ".kiro/skills/beyin/SKILL.md"
        kiro.write_text(kiro.read_text(encoding="utf-8") + "\nKiro synthetic edit.\n", encoding="utf-8")
        result = json.loads(self.run_python(cli, "skill-sync"))
        self.assertNotIn("beyin", result["conflicts"])
        for root in (".agents", ".claude"):
            self.assertIn("Kiro synthetic edit", (self.vault / root / "skills/beyin/SKILL.md").read_text(encoding="utf-8"))
        codex = self.vault / ".agents/skills/beyin/SKILL.md"
        claude = self.vault / ".claude/skills/beyin/SKILL.md"
        kiro.write_text(kiro.read_text(encoding="utf-8") + "Kiro second edit.\n", encoding="utf-8")
        claude.write_text(claude.read_text(encoding="utf-8") + "Claude second edit.\n", encoding="utf-8")
        result = json.loads(self.run_python(cli, "skill-sync"))
        self.assertIn("beyin", result["conflicts"])
        self.assertNotIn("second edit", codex.read_text(encoding="utf-8"))
        self.assertIn("Kiro second edit", kiro.read_text(encoding="utf-8"))
        self.assertIn("Claude second edit", claude.read_text(encoding="utf-8"))

    def test_kiro_hook_path_repair_and_adapter_exclusion(self):
        self.install()
        other = self.vault / ".kiro/hooks/user.json"
        other.write_text('{"version":"v1","hooks":[]}\n', encoding="utf-8")
        hook_file = self.vault / ".kiro/hooks/beyin-v3.json"
        data = json.loads(hook_file.read_text(encoding="utf-8"))
        old_command = data["hooks"][0]["action"]["command"]
        if os.name != "nt":
            # Exercise Windows transport parsing on POSIX too. This fixture is
            # only inspected by doctor/reconcile, never executed as PowerShell.
            code = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in shlex.split(old_command))
            code += "; exit $LASTEXITCODE"
            old_command = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(code.encode("utf-16le")).decode("ascii")
        match = re.search(r"(-EncodedCommand\s+)([A-Za-z0-9+/=]+)", old_command, re.IGNORECASE)
        decoded = base64.b64decode(match.group(2)).decode("utf-16le") if match else old_command
        stale = decoded.replace(str(self.vault), str(self.vault.parent / "old-vault"))
        self.assertNotEqual(stale, decoded, "fixture must change the actual command path")
        if match:
            encoded = base64.b64encode(stale.encode("utf-16le")).decode("ascii")
            stale = old_command[:match.start(2)] + encoded + old_command[match.end(2):]
        data["hooks"][0]["action"]["command"] = stale
        hook_file.write_text(json.dumps(data), encoding="utf-8")
        cli = self.vault / "beyin.py"
        doctor = json.loads(self.run_python(cli, "doctor"))
        self.assertEqual(doctor["hook_paths"]["status"], "stale")
        self.run_python(cli, "hook-reconcile")
        doctor = json.loads(self.run_python(cli, "doctor"))
        self.assertEqual(doctor["hook_paths"]["status"], "ok")
        self.assertTrue(list(hook_file.parent.glob("beyin-v3.json*.bak")))
        self.assertEqual(other.read_text(encoding="utf-8"), '{"version":"v1","hooks":[]}\n')
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state,
                        "--exclude-component", "adapters/kiro")
        self.assertFalse(hook_file.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.vault / ".kiro/skills/beyin/SKILL.md").is_file())
        self.run_python(INSTALLER, "--vault", self.vault, "--state", self.state,
                        "--include-component", "adapters/kiro")
        self.assertTrue(hook_file.is_file())

    def test_release_contains_kiro_adapter(self):
        package = Path(self.temp.name) / "synthetic.zip"
        self.run_python(ROOT / "scripts/build_v3_release.py", "--output", package,
                        "--version", "3.99.0")
        import zipfile
        with zipfile.ZipFile(package) as archive:
            self.assertIn("template/.claude/scripts/beyin_v3_kiro.py", archive.namelist())

    def test_update_adds_kiro_to_older_install_and_rollback_removes_it(self):
        self.install()
        kiro_paths = [".claude/scripts/beyin_v3_kiro.py", ".kiro/hooks/beyin-v3.json"]
        kiro_paths += [f".kiro/skills/{name}/SKILL.md"
                       for name in ("beyin", "beyin-doktor", "beyin-guncelle")]
        manifest_path = self.state / "v3-install.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name in kiro_paths:
            (self.vault / name).unlink()
            manifest["files"].pop(name)
        from v3_hook_reconcile_test import decoded_command
        manifest["commands"] = [command for command in manifest["commands"]
                                if "beyin_v3_kiro.py" not in decoded_command(command)]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        user_hook = self.vault / ".kiro/hooks/user.json"
        user_hook.write_text('{"version":"v1","hooks":[]}\n', encoding="utf-8")
        package = Path(self.temp.name) / "kiro-upgrade.zip"
        self.run_python(ROOT / "scripts/build_v3_release.py", "--output", package,
                        "--version", "3.99.0")
        cli = self.vault / "beyin.py"
        updated = json.loads(self.run_python(cli, "update", "--package", package))
        self.assertEqual(updated["status"], "updated")
        self.assertTrue(updated["trust_review_required"])
        # Only the Kiro hook file changed, so only Kiro's review is named.
        self.assertEqual(updated["trust_review_files"], [".kiro/hooks/beyin-v3.json"])
        self.assertTrue(all((self.vault / name).is_file() for name in kiro_paths))
        self.assertEqual(user_hook.read_text(encoding="utf-8"), '{"version":"v1","hooks":[]}\n')
        rolled = json.loads(self.run_python(cli, "rollback"))
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertTrue(all(not (self.vault / name).exists() for name in kiro_paths))
        self.assertEqual(user_hook.read_text(encoding="utf-8"), '{"version":"v1","hooks":[]}\n')

    def test_existing_kiro_hook_file_is_preserved_on_name_collision(self):
        hook_file = self.vault / ".kiro/hooks/beyin-v3.json"
        hook_file.parent.mkdir(parents=True)
        original = b'{"version":"v1","hooks":[{"name":"user-owned"}]}\n'
        hook_file.write_bytes(original)
        result = subprocess.run([sys.executable, str(INSTALLER), "--vault", str(self.vault),
                                 "--state", str(self.state)], text=True, capture_output=True,
                                cwd=self.vault, env=self.env, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unmanaged file conflict .kiro/hooks/beyin-v3.json", result.stderr)
        self.assertEqual(hook_file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
