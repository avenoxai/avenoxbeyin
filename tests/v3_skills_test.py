import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def module():
    spec = importlib.util.spec_from_file_location("v3_skills", ROOT / "template/.claude/scripts/beyin_v3_skills.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)/"vault"; self.vault.mkdir()
        self.state = Path(self.tmp.name)/"state"; self.state.mkdir()
    def write(self,side,name,text):
        if not text.startswith("---"): text = "---\nname: " + name + "\ndescription: desc\n---\n" + text
        p=self.vault/side/"skills"/name/"SKILL.md";p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p
    def test_import_and_copy_roundtrip(self):
        old=self.write(".claude","sample","original")
        m=module();self.assertEqual(m.sync_skills(self.vault,self.state,mode="copy")["conflicts"],[])
        canonical=self.vault/".agents/skills/sample/SKILL.md";self.assertIn("original", canonical.read_text())
        old.write_text("---\nname: sample\ndescription: d\n---\nuser revision");m.sync_skills(self.vault,self.state,mode="copy")
        self.assertIn("user revision", canonical.read_text())
    def test_two_sided_conflict_preserves_both(self):
        m=module();a=self.write(".agents","sample","one");m.sync_skills(self.vault,self.state,mode="copy")
        a.write_text("---\nname: sample\ndescription: d\n---\ncodex edit");b=self.vault/".claude/skills/sample/SKILL.md";b.write_text("---\nname: sample\ndescription: d\n---\nclaude edit")
        result=m.sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],["sample"]);self.assertIn("codex edit", a.read_text()); self.assertIn("claude edit", b.read_text())
    def test_existing_conflict_not_overwritten(self):
        self.write(".agents","sample","a");self.write(".claude","sample","b")
        self.assertEqual(module().sync_skills(self.vault,self.state,mode="copy")["conflicts"],["sample"])
    def test_symlink_mode_single_store(self):
        self.write(".agents","sample","one")
        m=module();result=m.sync_skills(self.vault,self.state,mode="symlink")
        self.assertEqual(result['conflicts'], [])
        p=self.vault/".claude/skills/sample";self.assertTrue(p.is_dir())
        linked=p.is_symlink()
        (p/"SKILL.md").write_text("---\nname: sample\ndescription: d\n---\nupdated")
        if not linked:
            self.assertEqual(m.sync_skills(self.vault,self.state,mode="symlink")['conflicts'], [])
        self.assertEqual((self.vault/".agents/skills/sample/SKILL.md").read_text(),"---\nname: sample\ndescription: d\n---\nupdated")
    def test_symlink_permission_denial_falls_back_to_reconciled_copy(self):
        self.write(".agents","fallback","original")
        m=module()
        with patch.object(Path, 'symlink_to', side_effect=OSError('Synthetic symlink privilege denial')):
            result=m.sync_skills(self.vault,self.state,mode="symlink")
        self.assertEqual(result['conflicts'], [])
        target=self.vault/'.claude/skills/fallback'
        self.assertTrue(target.is_dir());self.assertFalse(target.is_symlink())
        (target/'SKILL.md').write_text("---\nname: fallback\ndescription: d\n---\nfallback edit")
        self.assertEqual(m.sync_skills(self.vault,self.state,mode="symlink")['conflicts'], [])
        self.assertEqual((self.vault/'.agents/skills/fallback/SKILL.md').read_text(),"---\nname: fallback\ndescription: d\n---\nfallback edit")
    def test_external_symlink_is_unmanaged_not_a_queue_blocking_conflict(self):
        outside=Path(self.tmp.name)/"external";outside.mkdir();(outside/"SKILL.md").write_text("---\nname: external\ndescription: d\n---\nPRIVATE_CANARY")
        p=self.vault/".claude/skills";p.mkdir(parents=True)
        try:(p/"external").symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest("symlink unavailable on host")
        result=module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],[]);self.assertEqual(result["unmanaged"],["external"])
        self.assertFalse((self.vault/".agents/skills/external").exists())

    def test_matching_external_symlinks_on_both_harnesses_are_unmanaged(self):
        outside=Path(self.tmp.name)/"external-dual";outside.mkdir();(outside/"SKILL.md").write_text("---\nname: external\ndescription: d\n---\nPRIVATE_CANARY")
        left=self.vault/".agents/skills";right=self.vault/".claude/skills";left.mkdir(parents=True);right.mkdir(parents=True)
        try:
            (left/"external").symlink_to(outside,target_is_directory=True)
            (right/"external").symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest("symlink unavailable on host")
        result=module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],[]);self.assertEqual(result["unmanaged"],["external"])

    def test_external_symlink_collision_remains_a_conflict(self):
        outside=Path(self.tmp.name)/"external-collision";outside.mkdir();(outside/"SKILL.md").write_text("---\nname: external\ndescription: d\n---\nPRIVATE_CANARY")
        left=self.vault/".agents/skills";left.mkdir(parents=True)
        try:(left/"sample").symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest("symlink unavailable on host")
        self.write(".claude","sample","managed")
        result=module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],["sample"]);self.assertEqual(result["unmanaged"],[])
    def test_non_skill_entries_are_unmanaged_not_conflicts(self):
        self.write(".agents","sample","one")
        (self.vault/".claude/skills").mkdir(parents=True,exist_ok=True)
        (self.vault/".claude/skills/LICENSE-upstream.txt").write_text("upstream license")
        shared=self.vault/".agents/skills/shared_utils";shared.mkdir(parents=True)
        (shared/"helper.py").write_text("pass")
        result=module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],[])
        self.assertEqual(result["unmanaged"],["LICENSE-upstream.txt","shared_utils"])
        self.assertEqual(result["synced"],["sample"])
        self.assertIn("one", (self.vault/".claude/skills/sample/SKILL.md").read_text())
        self.assertFalse((self.vault/".agents/skills/LICENSE-upstream.txt").exists())
        self.assertFalse((self.vault/".claude/skills/shared_utils").exists())

    def test_managed_skill_losing_skill_md_stays_a_conflict(self):
        self.write(".agents","sample","one")
        m=module();self.assertEqual(m.sync_skills(self.vault,self.state,mode="copy")["synced"],["sample"])
        for side in (".agents",".claude"):
            (self.vault/side/"skills/sample/SKILL.md").unlink()
            (self.vault/side/"skills/sample/leftover.txt").write_text("leftover")
        result=m.sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],["sample"]);self.assertEqual(result["unmanaged"],[])

    def test_explicit_import_preserves_assets_and_rejects_overwrite(self):
        source=Path(self.tmp.name)/"my-skill";source.mkdir();(source/"SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\nsynthetic skill")
        (source/"asset.txt").write_text("asset")
        m=module();m.import_skill(self.vault,self.state,source,mode="copy")
        self.assertEqual((self.vault/".agents/skills/my-skill/asset.txt").read_text(),"asset")
        with self.assertRaises(ValueError):m.import_skill(self.vault,self.state,source,mode="copy")

    def test_nested_asset_preserved(self):
        self.write(".agents","sample","one");asset=self.vault/".agents/skills/sample/scripts/a.py";asset.parent.mkdir();asset.write_text("pass")
        module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual((self.vault/".claude/skills/sample/scripts/a.py").read_text(),"pass")

    def test_skill_backup_placed_outside_skills_root_and_leaves_skills_clean(self):
        m = module()
        old = self.write(".claude", "sample", "version 1")
        m.sync_skills(self.vault, self.state, mode="copy")
        canonical = self.vault / ".agents/skills/sample/SKILL.md"
        self.assertIn("version 1", canonical.read_text())

        canonical.write_text("---\nname: sample\ndescription: d\n---\nversion 2")
        m.sync_skills(self.vault, self.state, mode="copy")
        self.assertIn("version 2", (self.vault / ".claude/skills/sample/SKILL.md").read_text())

        # Verify .claude/skills contains no backup or staging folders
        claude_skills = [p.name for p in (self.vault / ".claude/skills").iterdir()]
        self.assertEqual(claude_skills, ["sample"])

        # Verify backup was safely placed in .claude/.skill-backups/
        backup_dir = self.vault / ".claude/.skill-backups"
        self.assertTrue(backup_dir.is_dir())
        backups = list(backup_dir.glob(".v3-backup-sample-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("version 1", (backups[0] / "SKILL.md").read_text())

    def test_parked_folders_from_older_releases_move_out_of_skills_roots(self):
        # Vaults synced before #157 hold backups and crashed stages inside the roots,
        # where Claude Code lists them as extra skills. The next sync moves them, intact.
        self.write(".agents","sample","v1");self.write(".claude","sample","v1")
        old=self.vault/".claude/skills/.v3-backup-sample-0123abcd";old.mkdir();(old/"SKILL.md").write_text("---\nname: sample\ndescription: d\n---\noriginal")
        stale=self.vault/".agents/skills/.v3-skill-k2j3";stale.mkdir();(stale/"SKILL.md").write_text("---\nname: sample\ndescription: d\n---\nhalf copy")
        result=module().sync_skills(self.vault,self.state,mode="copy")
        self.assertEqual(result["conflicts"],[])
        self.assertEqual(sorted(result["relocated_backups"]),[".v3-backup-sample-0123abcd",".v3-skill-k2j3"])
        for side in (".agents",".claude"):
            self.assertEqual([p.name for p in (self.vault/side/"skills").iterdir()],["sample"])
        self.assertIn("original", (self.vault/".claude/.skill-backups/.v3-backup-sample-0123abcd/SKILL.md").read_text())
        self.assertIn("half copy", (self.vault/".agents/.skill-backups/.v3-skill-k2j3/SKILL.md").read_text())
        self.assertEqual((self.vault/".claude/.skill-backups/.gitignore").read_text(),"*\n")
        self.assertNotIn("relocated_backups",module().sync_skills(self.vault,self.state,mode="copy"))

    def test_broken_yaml_frontmatter_is_conflict(self):
        left = self.vault / ".agents/skills/broken_yaml"
        left.mkdir(parents=True)
        (left / "SKILL.md").write_text("---\nbroken: yaml: : ---\nContent", encoding="utf-8")
        result = module().sync_skills(self.vault, self.state, mode="copy")
        self.assertIn("broken_yaml", result["conflicts"])
        self.assertNotIn("broken_yaml", result["synced"])

    def test_missing_name_or_description_is_conflict(self):
        left = self.vault / ".agents/skills/missing_fields"
        left.mkdir(parents=True)
        (left / "SKILL.md").write_text("---\ntitle: something\n---\nContent", encoding="utf-8")
        result = module().sync_skills(self.vault, self.state, mode="copy")
        self.assertIn("missing_fields", result["conflicts"])

    def test_binary_file_in_skill_is_conflict(self):
        left = self.vault / ".agents/skills/binary_file"
        left.mkdir(parents=True)
        (left / "SKILL.md").write_text("---\nname: valid\ndescription: valid\n---\nContent", encoding="utf-8")
        (left / "data.bin").write_bytes(b"\x00\x01\x02\xFF")
        result = module().sync_skills(self.vault, self.state, mode="copy")
        self.assertIn("binary_file", result["conflicts"])

if __name__ == "__main__":unittest.main()
