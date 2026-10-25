"""Tests for Obsidian-Native Knowledge Organization & Lint (RFC #368 P0-E)."""
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
import beyin_v3_lint as linter

CLI_SPEC = importlib.util.spec_from_file_location("beyin_cli", ROOT / "scripts/beyin_v3.py")
cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(cli)


class KnowledgeLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def test_moc_rejects_escape_and_preserves_external_file(self):
        (self.vault / 'knowledge').mkdir()
        outside = self.vault.parent / 'outside.md'
        outside.write_text('User text', encoding='utf-8')
        with self.assertRaises(ValueError):
            linter.generate_moc(self.vault, 'knowledge', '../../outside.md')
        self.assertEqual(outside.read_text(), 'User text')

    def test_moc_update_preserves_backslashes_in_titles(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'knowledge/note.md').write_text('# C:\\Users\\Synthetic\n', encoding='utf-8')
        linter.generate_moc(self.vault)
        before = (self.vault / 'knowledge/MOC.md').read_text()
        linter.generate_moc(self.vault)
        self.assertEqual((self.vault / 'knowledge/MOC.md').read_text(), before)

    def test_extract_wikilinks(self):
        sample_text = """
# System Overview
This note links to [[Architecture]] and also [[Architecture|System Architecture]].
Here is an anchor link [[Architecture#Database]] and [[Architecture#Database|DB Schema]].
Here is an internal heading link: [[#Summary]].
Here is an embedded image: ![[diagram.png]].
And another link on line 8: [[Components/Auth]].
"""
        links = linter.extract_wikilinks(sample_text)
        self.assertEqual(len(links), 7)

        # 1. [[Architecture]]
        self.assertEqual(links[0]["file_target"], "Architecture")
        self.assertIsNone(links[0]["display"])
        self.assertIsNone(links[0]["heading"])
        self.assertFalse(links[0]["is_embed"])
        self.assertEqual(links[0]["line"], 3)

        # 2. [[Architecture|System Architecture]]
        self.assertEqual(links[1]["file_target"], "Architecture")
        self.assertEqual(links[1]["display"], "System Architecture")

        # 3. [[Architecture#Database]]
        self.assertEqual(links[2]["file_target"], "Architecture")
        self.assertEqual(links[2]["heading"], "Database")

        # 4. [[Architecture#Database|DB Schema]]
        self.assertEqual(links[3]["file_target"], "Architecture")
        self.assertEqual(links[3]["heading"], "Database")
        self.assertEqual(links[3]["display"], "DB Schema")

        # 5. [[#Summary]]
        self.assertEqual(links[4]["file_target"], "")
        self.assertEqual(links[4]["heading"], "Summary")

        # 6. ![[diagram.png]]
        self.assertEqual(links[5]["file_target"], "diagram.png")
        self.assertTrue(links[5]["is_embed"])

        # 7. [[Components/Auth]]
        self.assertEqual(links[6]["file_target"], "Components/Auth")
        self.assertEqual(links[6]["line"], 7)

    def test_resolve_wikilink(self):
        # Setup notes
        (self.vault / "knowledge/concepts").mkdir(parents=True)
        (self.vault / "knowledge/concepts/clean-code.md").write_text("# Clean Code", encoding="utf-8")
        (self.vault / "projects/app").mkdir(parents=True)
        (self.vault / "projects/app/main.md").write_text("# Main App", encoding="utf-8")
        (self.vault / "projects/app/local-note.md").write_text("# Local", encoding="utf-8")

        index = linter.build_vault_index(self.vault)

        # 1. Exact path
        res_exact = linter.resolve_wikilink(self.vault, "knowledge/concepts/clean-code.md", vault_index=index)
        self.assertTrue(res_exact["exists"])
        self.assertEqual(res_exact["resolved_path"], "knowledge/concepts/clean-code.md")

        # 2. Exact path without extension
        res_no_ext = linter.resolve_wikilink(self.vault, "knowledge/concepts/clean-code", vault_index=index)
        self.assertTrue(res_no_ext["exists"])
        self.assertEqual(res_no_ext["resolved_path"], "knowledge/concepts/clean-code.md")

        # 3. Parent-relative path
        res_rel = linter.resolve_wikilink(self.vault, "local-note", source_file="projects/app/main.md", vault_index=index)
        self.assertTrue(res_rel["exists"])
        self.assertEqual(res_rel["resolved_path"], "projects/app/local-note.md")

        # 4. Basename shortest path lookup
        res_basename = linter.resolve_wikilink(self.vault, "clean-code", vault_index=index)
        self.assertTrue(res_basename["exists"])
        self.assertEqual(res_basename["resolved_path"], "knowledge/concepts/clean-code.md")

        # 5. Nonexistent
        res_missing = linter.resolve_wikilink(self.vault, "nonexistent-doc", vault_index=index)
        self.assertFalse(res_missing["exists"])
        self.assertIsNone(res_missing["resolved_path"])

    def test_lint_vault_broken_links_and_orphans(self):
        # Setup notes
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/note-a.md").write_text("# Note A\nLinks to [[note-b]].", encoding="utf-8")
        (self.vault / "knowledge/note-b.md").write_text("# Note B\nLinks to [[broken-target]].", encoding="utf-8")
        (self.vault / "knowledge/orphan.md").write_text("# Orphan Note\nNo links at all.", encoding="utf-8")
        (self.vault / "knowledge/unlinked.md").write_text("# Unlinked\nLinks to [[note-a]], but nobody links to it.", encoding="utf-8")

        report = linter.lint_vault(self.vault)
        self.assertEqual(report["status"], "issues_found")
        self.assertEqual(report["broken_links_count"], 1)
        self.assertEqual(report["broken_links"][0]["target"], "broken-target")
        self.assertEqual(report["broken_links"][0]["source"], "knowledge/note-b.md")
        self.assertEqual(report["broken_links"][0]["line"], 2)

        self.assertIn("knowledge/orphan.md", report["orphans"])
        self.assertIn("knowledge/unlinked.md", report["unlinked"])

    def test_lint_vault_clean_pass(self):
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/alpha.md").write_text("# Alpha\nLinks to [[beta]].", encoding="utf-8")
        (self.vault / "knowledge/beta.md").write_text("# Beta\nLinks to [[alpha]].", encoding="utf-8")

        report = linter.lint_vault(self.vault)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["broken_links_count"], 0)
        self.assertEqual(report["orphan_count"], 0)

    def test_generate_moc_fresh_creation(self):
        (self.vault / "knowledge/concepts").mkdir(parents=True)
        (self.vault / "knowledge/concepts/solid.md").write_text("# SOLID Principles", encoding="utf-8")
        (self.vault / "knowledge/patterns").mkdir(parents=True)
        (self.vault / "knowledge/patterns/factory.md").write_text("---\ntitle: Factory Pattern\n---\n# Pattern Note", encoding="utf-8")

        res = linter.generate_moc(self.vault, "knowledge", "MOC.md")
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["indexed_notes_count"], 2)

        moc_file = self.vault / "knowledge/MOC.md"
        self.assertTrue(moc_file.is_file())
        text = moc_file.read_text(encoding="utf-8")

        self.assertIn("# Knowledge — Map of Content (MOC)", text)
        self.assertIn("### Concepts", text)
        self.assertIn("[[knowledge/concepts/solid.md|SOLID Principles]]", text)
        self.assertIn("### Patterns", text)
        self.assertIn("[[knowledge/patterns/factory.md|Factory Pattern]]", text)
        self.assertIn(linter.MOC_START_MARKER, text)
        self.assertIn(linter.MOC_END_MARKER, text)

    def test_generate_moc_preserves_user_content(self):
        (self.vault / "projects/my-ws").mkdir(parents=True)
        (self.vault / "projects/my-ws/note1.md").write_text("# Module One", encoding="utf-8")

        moc_path = self.vault / "projects/my-ws/MOC.md"
        user_header = "# Handcrafted Project Dashboard\n\nCustom user guidance here.\n\n"
        user_footer = "\n\n## User Checklist\n- [x] Setup repository\n- [ ] Deploy"
        existing_moc = user_header + linter.MOC_START_MARKER + "\nold stale index\n" + linter.MOC_END_MARKER + user_footer
        moc_path.write_text(existing_moc, encoding="utf-8")

        # Run MOC update
        res = linter.generate_moc(self.vault, "projects/my-ws", "MOC.md")
        self.assertEqual(res["status"], "updated")

        new_text = moc_path.read_text(encoding="utf-8")
        self.assertTrue(new_text.startswith(user_header))
        self.assertTrue(new_text.endswith(user_footer))
        self.assertIn("[[projects/my-ws/note1.md|Module One]]", new_text)
        self.assertNotIn("old stale index", new_text)

    def test_cli_lint_and_moc_subcommands(self):
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/doc1.md").write_text("# Doc 1\n[[doc2]]", encoding="utf-8")
        (self.vault / "knowledge/doc2.md").write_text("# Doc 2\n[[doc1]]", encoding="utf-8")

        # 1. Test CLI lint via cli.main
        res_lint, code_lint = cli.main(["--vault", str(self.vault), "--state", str(self.state), "lint"], return_result=True)
        self.assertEqual(code_lint, 0)
        self.assertEqual(res_lint["status"], "ok")
        self.assertEqual(res_lint["broken_links_count"], 0)

        # 2. Test CLI moc via subprocess
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/beyin_v3.py"),
                "--vault", str(self.vault),
                "--state", str(self.state),
                "moc",
                "--folder", "knowledge",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "created")
        self.assertEqual(parsed["indexed_notes_count"], 2)
        self.assertTrue((self.vault / "knowledge/MOC.md").is_file())


if __name__ == "__main__":
    unittest.main()
