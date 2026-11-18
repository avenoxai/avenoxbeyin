from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "template/.claude/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import beyin_v3 as runtime
import beyin_v3_selftest as selftest


class V3SelftestTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="selftest-test-")
        self.vault = Path(self.tmp_dir.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp_dir.name) / "state"
        self.state.mkdir()

        # Initialize memory store and ingest a test note
        self.store = runtime.MemoryStore(self.state, self.vault)
        (self.vault / "projects").mkdir()
        note_path = self.vault / "projects" / "alpha.md"
        note_path.write_text("# Project Alpha\n\nSecret code is OMEGA-99 for deployment.\n", encoding="utf-8")

        self.store.ingest({
            "id": "alpha-1",
            "kind": "note",
            "visibility": "internal",
            "text": "# Project Alpha\n\nSecret code is OMEGA-99 for deployment.\n",
            "source": "projects/alpha.md",
            "updated_at": "2026-11-18T00:00:00Z"
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_missing_questions_returns_no_questions(self):
        result = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(result["status"], "no_questions")
        self.assertIn("knowledge/v3/selftest.json", result["questions_path"])
        self.assertIn("hint", result)

    def test_add_question_and_run_passing_selftest(self):
        add_res = selftest.add_question(
            self.vault,
            None,
            query="Project Alpha OMEGA-99",
            source="projects/alpha.md",
            sentence="Secret code is OMEGA-99"
        )
        self.assertEqual(add_res["status"], "ok")

        # Run selftest
        res = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["passed"], 1)
        self.assertEqual(res["missed"], [])

    def test_selftest_without_sentence(self):
        selftest.add_question(
            self.vault,
            None,
            query="Project Alpha",
            source="projects/alpha.md"
        )
        res = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["passed"], 1)

    def test_selftest_missed_when_unmatched(self):
        selftest.add_question(
            self.vault,
            None,
            query="Unrelated Query That Matches Nothing",
            source="projects/alpha.md"
        )
        res = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["passed"], 0)
        self.assertEqual(len(res["missed"]), 1)
        self.assertEqual(res["missed"][0]["expected_source"], "projects/alpha.md")

    def test_regression_tracking(self):
        # 1. Add question that passes
        selftest.add_question(
            self.vault,
            None,
            query="Project Alpha",
            source="projects/alpha.md"
        )
        res1 = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(res1["passed"], 1)
        self.assertIsNone(res1["previous"])

        # 2. Modify questions file to change expected source to something wrong
        q_file = self.vault / "knowledge/v3/selftest.json"
        data = json.loads(q_file.read_text(encoding="utf-8"))
        data["questions"][0]["source"] = "projects/nonexistent.md"
        q_file.write_text(json.dumps(data), encoding="utf-8")

        res2 = selftest.run_selftest(self.vault, self.state)
        self.assertEqual(res2["passed"], 0)
        self.assertIsNotNone(res2["previous"])
        self.assertEqual(len(res2["regressed"]), 1)
        self.assertEqual(res2["regressed"][0]["expected_source"], "projects/nonexistent.md")

    def test_path_traversal_and_symlink_guards(self):
        with self.assertRaises(ValueError):
            selftest.add_question(self.vault, "../outside.json", "query", "projects/alpha.md")

        with self.assertRaises(ValueError):
            selftest.add_question(self.vault, None, "query", "../outside.md")

        res = selftest.run_selftest(self.vault, self.state, questions_path="../outside.json")
        self.assertEqual(res["status"], "error")

    def test_read_only_vault_immutability(self):
        alpha_path = self.vault / "projects" / "alpha.md"
        content_before = alpha_path.read_text(encoding="utf-8")

        selftest.add_question(self.vault, None, "Project Alpha", "projects/alpha.md")
        selftest.run_selftest(self.vault, self.state)

        self.assertEqual(alpha_path.read_text(encoding="utf-8"), content_before)


if __name__ == "__main__":
    unittest.main()
