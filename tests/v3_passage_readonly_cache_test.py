"""A read-only store (hook turn while a sync is pending or deferred) still persists passage-index progress."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))
import beyin_v3 as runtime  # noqa: E402
import beyin_v3_passage as passage  # noqa: E402

QUERY = "quartz observatory calibration"
RECORDS = 6


class ReadOnlyPassageCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="v3-ro-cache-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / "vault", root / "state"
        self.vault.mkdir()
        writer = runtime.MemoryStore(self.state, self.vault)
        for number in range(RECORDS):
            source = f"knowledge/note-{number}.md"
            text = f"Quartz observatory calibration number {number} uses a zircon reference {number}."
            (self.vault / "knowledge").mkdir(exist_ok=True)
            (self.vault / source).write_text(text, encoding="utf-8")
            writer.ingest({"id": f"n{number}", "source": source, "text": text, "facts": {}, "kind": "note",
                           "status": "active", "visibility": "internal", "updated_at": "2026-09-20T10:00:00Z"})
        self.cache = self.state / passage.CACHE_NAME
        self.assertFalse(self.cache.exists())

    def read_only_store(self):
        store = runtime.MemoryStore(self.state, self.vault, read_only=True)
        self.addCleanup(store.close)
        return store

    def test_progress_survives_between_read_only_turns(self):
        database = hashlib.sha256((self.state / "memory.sqlite3").read_bytes()).hexdigest()
        with patch.object(passage, "BUILD_SECONDS", -1.0):  # one record of progress per turn
            with self.assertRaises(passage.IndexPending):
                passage.context_for(self.read_only_store(), "claude", QUERY, strict=True)
            self.assertTrue(self.cache.is_file(), "progress must be persisted by a read-only turn")
            for _ in range(RECORDS):
                try:
                    context = passage.context_for(self.read_only_store(), "claude", QUERY, strict=True)
                    break
                except passage.IndexPending:
                    continue
            else:
                self.fail("the index never completed across read-only turns")
        self.assertTrue(context["records"])
        self.assertEqual(hashlib.sha256((self.state / "memory.sqlite3").read_bytes()).hexdigest(), database)

    def test_unwritable_cache_is_only_slower(self):
        with patch.object(passage, "BUILD_SECONDS", -1.0), patch.object(passage.tempfile, "NamedTemporaryFile",
                                                                       side_effect=PermissionError("read-only state")):
            with self.assertRaises(passage.IndexPending):
                passage.context_for(self.read_only_store(), "claude", QUERY, strict=True)
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
