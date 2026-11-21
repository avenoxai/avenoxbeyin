"""extras/video-motoru is an optional pack: it must import as a skill and stay a blank start."""
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "extras/video-motoru"
NOTEBOOK = ("Marka.md", "Dersler.md", "Sahne-Envanteri.md", "Araclar.md", "Acik-Isler.md",
            "Referanslar.md", "Kutuphane.md")
GUIDES = ("KURULUM.md", "REFERANS.md", "KUTUPHANE.md", "REPO.md")


def skills_module():
    spec = importlib.util.spec_from_file_location("v3_skills_extras", ROOT / "template/.claude/scripts/beyin_v3_skills.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


class VideoMotoruPackTest(unittest.TestCase):
    def test_pack_imports_with_templates_into_shared_and_client_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"; vault.mkdir()
            state = Path(tmp) / "state"; state.mkdir()
            result = skills_module().import_skill(vault, state, PACK, mode="copy")
            self.assertEqual(result["conflicts"], [])
            for side in (".agents", ".claude"):
                root = vault / side / "skills/video-motoru"
                self.assertTrue((root / "SKILL.md").is_file(), side)
                for name in GUIDES:
                    self.assertTrue((root / name).is_file(), f"{side}/{name}")
                for name in NOTEBOOK + ("KURGU-NOTU.md",):
                    self.assertEqual((root / "sablonlar" / name).read_bytes(),
                                     (PACK / "sablonlar" / name).read_bytes(), name)

    def test_skill_names_itself_and_links_only_to_files_it_ships(self):
        text = (PACK / "SKILL.md").read_text(encoding="utf-8")
        front = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(front)
        self.assertIn("name: video-motoru", front.group(1).splitlines())
        self.assertRegex(front.group(1), r"(?m)^description: \S")
        for path in PACK.rglob("*.md"):
            for target in re.findall(r"\]\((?!https?://)([^)#]+)", path.read_text(encoding="utf-8")):
                self.assertTrue((path.parent / target).exists(), f"{path.name}: {target}")
        for name in NOTEBOOK:
            self.assertIn(f"`{name}`", text, name)
        for name in GUIDES:
            self.assertIn(f"]({name})", text, name)

    def test_notebook_starts_blank(self):
        # The pack ships no brand: every list field under Marka.md is empty and no lesson is dated.
        fields = [line for line in (PACK / "sablonlar/Marka.md").read_text(encoding="utf-8").splitlines()
                  if line.startswith("- ")]
        self.assertTrue(fields)
        for line in fields:
            self.assertTrue(line.endswith(":"), line)
        for name in ("Dersler.md", "Acik-Isler.md", "Kutuphane.md", "Referanslar.md"):
            for line in (PACK / "sablonlar" / name).read_text(encoding="utf-8").splitlines():
                self.assertNotRegex(line, r"^- \d{4}-\d{2}-\d{2}", f"{name}: {line}")


if __name__ == "__main__":
    unittest.main()
