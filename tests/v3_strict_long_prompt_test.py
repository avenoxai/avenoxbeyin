"""A long pasted prompt (hand-off, report, brief) is reduced to its rarest terms before strict matching; typed prompts are unchanged."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402

COMMON = [f"yaygin{n:03d}x" for n in range(140)]  # in every note: far more than 25% of the sources
PASTE_TAIL = "Bu metin yapıştırıldı. " * 40


class LongPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-long-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "knowledge").mkdir(parents=True)
        self.store = runtime.MemoryStore(root / "state", self.vault)
        self.addCleanup(self.store.close)
        (self.store.state_dir / "retrieval.json").write_text(json.dumps({"strict_floor": 0.0}), encoding="utf-8")
        notes = {f"note{n}": " ".join(COMMON[:60]) + f" konu{n}z sentetik dolgu" for n in range(20)}
        notes["target"] = " ".join(COMMON[:60]) + " kuvars gözlemevi kalibrasyonu burada anlatılır"
        for ident, text in notes.items():
            source = f"knowledge/{ident}.md"
            (self.vault / source).write_text(text, encoding="utf-8")
            self.store.ingest({"id": ident, "source": source, "text": text, "facts": {}, "kind": "note", "status": "active",
                               "visibility": "internal", "updated_at": "2026-10-11T10:00:00Z"})

    def ids(self, query):
        return [r["id"] for r in self.store.context_for("claude", query, strict=True)["records"]]

    def test_a_long_prompt_of_common_words_injects_nothing(self):
        paste = "Devir notu: " + " ".join(COMMON) + ". " + PASTE_TAIL
        self.assertGreater(len(paste), 1500)
        self.assertEqual(self.ids(paste), [])

    def test_a_long_prompt_naming_a_topic_with_enough_rare_terms_still_finds_the_note(self):
        paste = "Devir notu: " + " ".join(COMMON) + ". kuvars gözlemevi kalibrasyonu burada anlatılır. " + PASTE_TAIL
        self.assertEqual(self.ids(paste), ["target"])

    def test_a_long_prompt_sharing_only_a_few_rare_terms_injects_nothing(self):
        # Four rare words survive the reduction, but the note shares only two of them (search caps the gate at the
        # number of terms left, so a long prompt with fewer than four rare terms is judged on all of them).
        paste = "Devir notu: " + " ".join(COMMON) + ". kuvars gözlemevi konu0z konu1z. " + PASTE_TAIL
        self.assertEqual(self.ids(paste), [])
        self.assertIn("target", self.ids("kuvars gözlemevi konu0z konu1z"))  # the same words typed short: two shared terms suffice

    def test_short_prompts_are_unchanged(self):
        short = " ".join(COMMON[:6]) + " kuvars"
        self.assertEqual(self.ids(short)[0], "target")
        self.assertEqual(self.ids("kuvars gözlemevi"), ["target"])
        # many terms but still a typed prompt (under both limits): no reduction, common words still count
        typed = " ".join(COMMON[:40])
        self.assertEqual(len(self.ids(typed)), 5)


if __name__ == "__main__":
    unittest.main()
