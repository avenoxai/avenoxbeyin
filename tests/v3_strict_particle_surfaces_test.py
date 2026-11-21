"""Turkish particles and fillers must not fill the two-shared-term gate of the strict passage path (exact surfaces, removed before stemming)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402

FILLER = "Yeni çok kuvars gözlemevi kalibrasyonu burada anlatılır."
OTHERS = [f"Sentetik not {n} baska konular hakkinda bilgi icerir tablo grafik rapor {n}." for n in range(10)]


class StrictParticlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-particles-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "knowledge").mkdir(parents=True)
        self.store = runtime.MemoryStore(root / "state", self.vault)
        self.addCleanup(self.store.close)
        (self.store.state_dir / "retrieval.json").write_text(json.dumps({"strict_floor": 0.0}), encoding="utf-8")
        for ident, text in [("target", FILLER)] + [(f"other{n}", t) for n, t in enumerate(OTHERS)]:
            source = f"knowledge/{ident}.md"
            (self.vault / source).write_text(text, encoding="utf-8")
            self.store.ingest({"id": ident, "source": source, "text": text, "facts": {}, "kind": "note", "status": "active",
                               "visibility": "internal", "updated_at": "2026-10-11T10:00:00Z"})

    def ids(self, query):
        return [r["id"] for r in self.store.context_for("claude", query, strict=True)["records"]]

    def test_particles_do_not_pass_the_two_term_gate(self):
        # Three of the four query words are in the note, but only one of them is a topic word.
        self.assertEqual(self.ids("yeni çok da kuvars cihaz"), [])

    def test_a_prompt_of_particles_alone_delivers_nothing(self):
        self.assertEqual(self.ids("yeni çok"), [])
        self.assertEqual(self.ids("tamam yap"), [])

    def test_only_the_exact_surface_is_removed_before_stemming(self):
        # "yenisi" shares the stem of the particle "yeni" but is another word: it still counts as a term.
        self.assertEqual(self.ids("yenisi kuvars"), ["target"])
        from beyin_v3_passage import _particle_stripped_tokens
        self.assertNotIn("yeni", _particle_stripped_tokens("yeni çok da"))
        self.assertIn("yeni", _particle_stripped_tokens("yenisi"))
        self.assertIn("kuvars", _particle_stripped_tokens("yeni çok da kuvars"))

    def test_topic_words_still_find_the_note(self):
        self.assertEqual(self.ids("kuvars gözlemevi"), ["target"])
        self.assertEqual(self.ids("yeni kuvars gözlemevi da"), ["target"])
        self.assertEqual(self.ids("kuvars"), ["target"])


if __name__ == "__main__":
    unittest.main()
