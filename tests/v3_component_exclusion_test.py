"""Tests for component and skill exclusion (Issue #94 Item 7)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import ROOT, build_package, isolated_env, run_python

START, END = "<!-- beyin-v3:start -->", "<!-- beyin-v3:end -->"


class ComponentExclusionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-component-exclusion-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / "Örnek Vault"
        self.vault.mkdir()
        self.state = self.base / "state"
        self.env = isolated_env(self.base / "home")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/install_v3.py"),
                               "--vault", str(self.vault), "--state", str(self.state), *args],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=120)

    def manifest(self):
        return json.loads((self.state / "v3-install.json").read_text(encoding="utf-8"))

    def test_exclude_starter_skill_initial_install(self):
        result = self.cli("--exclude-component", "skills/beyin-guncelle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())
        self.assertFalse((self.vault / ".agents/skills/beyin-guncelle/SKILL.md").exists())
        self.assertTrue((self.vault / ".claude/skills/beyin/SKILL.md").exists())
        self.assertTrue((self.vault / ".claude/skills/beyin-doktor/SKILL.md").exists())
        manifest = self.manifest()
        self.assertEqual(manifest.get("excluded_components"), ["skills/beyin-guncelle"])
        self.assertNotIn(".claude/skills/beyin-guncelle/SKILL.md", manifest["files"])

    def test_exclude_component_removes_unchanged_file(self):
        initial = self.cli()
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertTrue((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())

        update = self.cli("--exclude-component", "skills/beyin-guncelle")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertFalse((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())
        self.assertFalse((self.vault / ".agents/skills/beyin-guncelle/SKILL.md").exists())
        manifest = self.manifest()
        self.assertNotIn(".claude/skills/beyin-guncelle/SKILL.md", manifest["files"])
        self.assertEqual(manifest.get("excluded_components"), ["skills/beyin-guncelle"])

    def test_exclude_component_preserves_modified_file_without_conflict(self):
        initial = self.cli()
        self.assertEqual(initial.returncode, 0, initial.stderr)
        target = self.vault / ".claude/skills/beyin-guncelle/SKILL.md"
        custom_content = "# Kullanıcının kendi güncelleyici skill'i\nprint('custom')\n"
        target.write_text(custom_content, encoding="utf-8")

        update = self.cli("--exclude-component", "skills/beyin-guncelle")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), custom_content)
        manifest = self.manifest()
        self.assertNotIn(".claude/skills/beyin-guncelle/SKILL.md", manifest["files"])

    def test_exclude_launchers(self):
        result = self.cli("--exclude-component", "launchers")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.vault / "Beyni Guncelle.cmd").exists())
        self.assertFalse((self.vault / "Beyni Guncelle.command").exists())
        self.assertFalse((self.vault / "Beyni Güncelle.sh").exists())
        self.assertFalse((self.vault / "Beyni Güncelle.desktop").exists())

    def test_core_paths_and_unknown_names_are_refused(self):
        self.assertEqual(self.cli().returncode, 0)
        for name in ("beyin.py", ".claude/scripts/beyin_v3_hook.py", "harnesses/claude", "harnesses/codex", "skils"):
            result = self.cli("--exclude-component", name)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("excludable:", result.stderr)
        self.assertTrue((self.vault / "beyin.py").is_file())
        self.assertTrue((self.vault / ".claude/scripts/beyin_v3_hook.py").is_file())
        self.assertNotIn("excluded_components", self.manifest())
        exclusions = self.vault / ".beyin-exclusions.json"
        exclusions.write_text(json.dumps({"excluded_components": ["skils"]}), encoding="utf-8")
        self.assertIn("excludable:", self.cli().stderr)

    def test_exclude_agents_block_on_existing_install_keeps_user_text(self):
        agents_md = self.vault / "AGENTS.md"
        agents_md.write_text("# My rules\nNever delete my notes.\n", encoding="utf-8")
        self.assertEqual(self.cli().returncode, 0)
        self.assertIn(START, agents_md.read_text(encoding="utf-8"))
        result = self.cli("--exclude-component", "agents_block")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = agents_md.read_text(encoding="utf-8")
        self.assertNotIn(START, text)
        self.assertIn("Never delete my notes.", text)

    def test_exclude_hermes_adapter(self):
        self.assertEqual(self.cli().returncode, 0)
        self.assertTrue((self.vault / ".claude/hermes-plugin/plugin.yaml").exists())
        self.assertEqual(self.cli("--exclude-component", "adapters/hermes").returncode, 0)
        self.assertFalse((self.vault / ".claude/hermes-plugin/plugin.yaml").exists())
        self.assertTrue((self.vault / ".opencode/plugins").is_dir())

    def test_exclusions_file_is_the_one_source(self):
        self.assertEqual(self.cli("--exclude-component", "skills/beyin-guncelle").returncode, 0)
        exclusions = self.vault / ".beyin-exclusions.json"
        self.assertEqual(json.loads(exclusions.read_text(encoding="utf-8"))["excluded_components"], ["skills/beyin-guncelle"])
        # Re-including through exclusions file alone must bring it back on the next flagless run.
        exclusions.write_text(json.dumps({"excluded_components": []}), encoding="utf-8")
        self.assertEqual(self.cli().returncode, 0)
        self.assertTrue((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())

    def test_installed_updater_removes_excluded_files(self):
        self.assertEqual(self.cli().returncode, 0)
        (self.vault / ".beyin-exclusions.json").write_text(
            json.dumps({"excluded_components": ["skills", "launchers", "adapters"]}), encoding="utf-8")
        package = build_package(self.base / "next.zip", "9.9.9", self.env)
        scripts = str(self.vault / ".claude/scripts")
        sys.path.insert(0, scripts)
        self.addCleanup(sys.path.remove, scripts)
        spec = importlib.util.spec_from_file_location("installed_updater", self.vault / ".claude/scripts/beyin_v3_update.py")
        updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
        before = set(self.manifest()["files"])
        res = updater.update(self.vault, self.state, package=package)
        self.assertEqual(res["status"], "updated")
        self.assertTrue(res.get("removed"))
        dropped = before - set(self.manifest()["files"])
        self.assertTrue(dropped)
        self.assertEqual([name for name in dropped if (self.vault / name).exists()], [])
        self.assertTrue((self.vault / "beyin.py").is_file())
        updater.rollback(self.vault, self.state)
        self.assertTrue((self.vault / ".claude/skills/beyin/SKILL.md").is_file())

    def test_exclude_agents_block(self):
        agents_md = self.vault / "AGENTS.md"
        agents_md.write_text("# My rules\nNever delete my notes.\n", encoding="utf-8")
        result = self.cli("--exclude-component", "agents_block")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = agents_md.read_text(encoding="utf-8")
        self.assertNotIn(START, text)
        self.assertIn("Never delete my notes.", text)

    def test_exclusions_file_excluded_components_respected(self):
        exclusions_file = self.vault / ".beyin-exclusions.json"
        exclusions_file.write_text(json.dumps({"excluded_components": ["skills/beyin-doktor"]}), encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.vault / ".claude/skills/beyin-doktor/SKILL.md").exists())
        self.assertTrue((self.vault / ".claude/skills/beyin/SKILL.md").exists())

    def test_re_include_component(self):
        self.cli("--exclude-component", "skills/beyin-guncelle")
        self.assertFalse((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())
        reinclude = self.cli("--include-component", "skills/beyin-guncelle")
        self.assertEqual(reinclude.returncode, 0, reinclude.stderr)
        self.assertTrue((self.vault / ".claude/skills/beyin-guncelle/SKILL.md").exists())
        manifest = self.manifest()
        self.assertNotIn("skills/beyin-guncelle", manifest.get("excluded_components", []))

    def test_rollback_compatibility_with_3_4_0(self):
        # Point 1: preferences file must not contain excluded_components
        self.assertEqual(self.cli("--exclude-component", "skills/beyin-guncelle").returncode, 0)
        prefs_path = self.vault / ".beyin-preferences.json"
        if prefs_path.exists():
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
            self.assertNotIn("excluded_components", data)

    def test_agents_block_manually_deleted_then_uninstall_succeeds(self):
        # Point 2 & 3: User deletes block manually, then excludes agents_block, then uninstalls
        self.assertEqual(self.cli().returncode, 0)
        agents_md = self.vault / "AGENTS.md"
        self.assertTrue(agents_md.exists())
        # User manually deletes block
        agents_md.write_text("# My rules\nUser content here.\n", encoding="utf-8")
        # Exclude agents_block
        self.assertEqual(self.cli("--exclude-component", "agents_block").returncode, 0)
        # Uninstall must succeed cleanly
        uninst = self.cli("--uninstall")
        self.assertEqual(uninst.returncode, 0, uninst.stderr)

    def test_agents_block_empty_file_removed(self):
        # Point 3: When AGENTS.md had no original content, excluding agents_block removes the file
        self.assertEqual(self.cli().returncode, 0)
        self.assertTrue((self.vault / "AGENTS.md").exists())
        self.assertEqual(self.cli("--exclude-component", "agents_block").returncode, 0)
        self.assertFalse((self.vault / "AGENTS.md").exists())

    def test_agents_block_exact_original_bytes_restored(self):
        # Point 3: Exact bytes including newlines restored
        orig_bytes = b"# Rules\n\nPreserve this exact spacing.\n\n"
        (self.vault / "AGENTS.md").write_bytes(orig_bytes)
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.cli("--exclude-component", "agents_block").returncode, 0)
        self.assertEqual((self.vault / "AGENTS.md").read_bytes(), orig_bytes)

    def test_antigravity_hooks_manually_deleted_then_update_and_uninstall(self):
        # Point 2: User manually removed beyin-v3 key or file
        self.assertEqual(self.cli().returncode, 0)
        hooks_path = self.vault / ".agents/hooks.json"
        self.assertTrue(hooks_path.exists())
        # User removes beyin-v3
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        data.pop("beyin-v3", None)
        hooks_path.write_text(json.dumps(data), encoding="utf-8")
        # Exclude harnesses/antigravity
        self.assertEqual(self.cli("--exclude-component", "harnesses/antigravity").returncode, 0)
        # Delete file completely
        hooks_path.unlink()
        self.assertEqual(self.cli("--exclude-component", "harnesses/antigravity").returncode, 0)
        uninst = self.cli("--uninstall")
        self.assertEqual(uninst.returncode, 0, uninst.stderr)

    def test_include_unknown_component_fails_cli(self):
        # Point 7: include unknown component must fail
        res = self.cli("--include-component", "olmayan-bilesen")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("excludable:", res.stderr)

    def test_windows_backslashes_normalized(self):
        # Point 7: backslashes normalized
        res = self.cli("--exclude-component", "skills\\beyin-guncelle")
        self.assertEqual(res.returncode, 0, res.stderr)
        exclusions = json.loads((self.vault / ".beyin-exclusions.json").read_text(encoding="utf-8"))
        self.assertEqual(exclusions["excluded_components"], ["skills/beyin-guncelle"])


if __name__ == "__main__":
    unittest.main()
