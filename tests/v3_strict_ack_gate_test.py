"""A prompt made only of acknowledgement or continuation words injects nothing; any topic word keeps retrieval."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402

NOTES = {
    "knowledge/a.md": "Devam eden iş: tamam durumu, evet onayı ve yes done go continue notları; kuvars gözlemevi kalibrasyonu.",
    "knowledge/b.md": "Evet tamam devam: onaylıyorum, bitti, peki olur. Tarih ve durum listesi, kuvars günlüğü.",
    "knowledge/c.md": "Başka konu: hepsi yap, teşekkürler sağol, ok okay. Durum raporu ve tarih kaydı.",
    "knowledge/d.md": "Tamam yap devam bitti evet onayla hayır: yes no done go continue; durum tarih.",
}
NOTES.update({f"knowledge/filler{n}.md": f"Sentetik dolgu notu {n}: bütçe fatura tahsilat iade kargo palet stok sayım {n}." for n in range(12)})


class AckGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-ack-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "knowledge").mkdir(parents=True)
        self.store = runtime.MemoryStore(root / "state", self.vault)
        self.addCleanup(self.store.close)
        (self.store.state_dir / "retrieval.json").write_text('{"strict_floor": 0.0}', encoding="utf-8")
        for source, text in NOTES.items():
            (self.vault / source).write_text(text, encoding="utf-8")
            self.store.ingest({"id": Path(source).stem, "source": source, "text": text, "facts": {}, "kind": "note",
                               "status": "active", "visibility": "internal", "updated_at": "2026-10-11T10:00:00Z"})

    def sources(self, query):
        return [r["source"] for r in self.store.context_for("claude", query, strict=True)["records"]]

    def test_acknowledgement_only_prompts_inject_nothing(self):
        for prompt in ("evet", "Tamam, devam", "tamam yap", "Evet, onaylıyorum", "peki olur", "Yes, done", "ok go continue",
                       "teşekkürler sağol", "hayır", "bitti"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.sources(prompt), [])

    def test_a_topic_word_keeps_the_prompt_alive(self):
        # one-word topic prompts must still retrieve (upstream tests use durum and tarih the same way)
        self.assertTrue(self.sources("durum"))
        self.assertTrue(self.sources("tarih"))
        self.assertIn("knowledge/a.md", self.sources("tamam, kuvars gözlemevi"))
        self.assertIn("knowledge/a.md", self.sources("evet devam kuvars kalibrasyonu"))


if __name__ == "__main__":
    unittest.main()
