"""Hyphenated identifiers (D-68, ADR-0007) are terms of their own in the strict passage path, on both sides."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402
import beyin_v3_passage as passage  # noqa: E402

NOTES = {
    "knowledge/kararlar.md": "# Kararlar\n\nD-68 kararı: kuvars gözlemevi kalibrasyonu haftalık yapılır.\n\nD-69 kararı: arşiv sayfası yeniden adlandırılır.",
    "knowledge/yazim.md": "Kısa yazım notu: D68 kodu başka bir listede bitişik yazılmıştır, kuvars ile ilgisi yok.",
    "knowledge/adr.md": "ADR-0007 mimari kararı: kuyruk sağlayıcısı değiştirilmez, tablo grafik rapor notları.",
    "knowledge/genel.md": "Genel d notu: 68 kalem listesi ve tablo grafik rapor.",
}
LONG = " ".join(f"dolgu{i} kelime{i % 7} satir{i % 11}." for i in range(120))
NOTES["knowledge/uzun.md"] = "Uzun kayıt. " + LONG + " Karar D-71 burada kayıtlıdır. " + LONG
NOTES.update({f"knowledge/filler{n}.md": f"Sentetik dolgu notu {n}: bütçe fatura tahsilat iade kargo palet stok sayım {n}." for n in range(12)})


class StrictIdentifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-identifier-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "knowledge").mkdir(parents=True)
        self.store = runtime.MemoryStore(root / "state", self.vault)
        self.addCleanup(self.store.close)
        for source, text in NOTES.items():
            (self.vault / source).write_text(text, encoding="utf-8")
            self.store.ingest({"id": Path(source).stem, "source": source, "text": text, "facts": {}, "kind": "note",
                               "status": "active", "visibility": "internal", "updated_at": "2026-10-11T10:00:00Z"})

    def sources(self, query):
        return [r["source"] for r in self.store.context_for("claude", query, strict=True)["records"]]

    def test_a_bare_identifier_finds_its_note(self):
        self.assertIn("knowledge/kararlar.md", self.sources("D-68"))
        self.assertNotIn("knowledge/genel.md", self.sources("D-68"))

    def test_a_turkish_apostrophe_suffix_is_stripped(self):
        self.assertIn("knowledge/kararlar.md", self.sources("D-68'i"))
        self.assertIn("knowledge/kararlar.md", self.sources("D-68’e bak"))

    def test_the_catenated_spelling_matches_in_both_directions(self):
        self.assertIn("knowledge/yazim.md", self.sources("D-68"))   # note writes D68
        self.assertIn("knowledge/kararlar.md", self.sources("D68")) # note writes D-68

    def test_one_exact_identifier_satisfies_the_two_term_rule(self):
        self.assertIn("knowledge/adr.md", self.sources("tamam ADR-0007 üzerinde ilerle"))
        self.assertIn("knowledge/kararlar.md", self.sources("tamam D-68'e geç"))

    def test_an_exact_identifier_clears_the_floor(self):
        # The floor is calibrated for fuzzy lexical matches; a rare exact identifier is the strongest evidence a prompt can
        # give (a bare D-68 passage in a long rules note weighs just under the default 0.20). Ordinary words keep the floor.
        (self.store.state_dir / "retrieval.json").write_text('{"strict_floor": 2.0}', encoding="utf-8")
        self.assertEqual(self.sources("D-71"), ["knowledge/uzun.md"])
        self.assertEqual(self.sources("kuvars gözlemevi kalibrasyonu"), [])

    def test_another_identifier_does_not_match(self):
        self.assertNotIn("knowledge/kararlar.md", self.sources("D-70"))

    def test_the_identifier_tokens_are_emitted_on_the_passage_side(self):
        self.assertEqual(passage._identifier_tokens("bkz. D-68'i ve ADR-0007; 2026-11-17 tarihli"), {"d-68", "d68", "adr-0007", "adr0007"})
        self.assertEqual(passage._identifier_tokens("CVE-2026-109619 ve utf-8, gpt-4, e-posta"), {"cve-2026-109619", "cve2026109619"})


if __name__ == "__main__":
    unittest.main()
