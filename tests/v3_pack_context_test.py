#!/usr/bin/env python3
"""Contract and regression tests for context packing and fair-share budget allocation (#79)."""
import json
from pathlib import Path
import random
import string
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import beyin_v3 as runtime  # noqa: E402
from beyin_v3 import pack_context, render_context, MemoryStore, _json  # noqa: E402

MARKER = runtime.CONTEXT_MARKER
HOOK_PREFIX = ('Receipt session=000000000000000000000000; choose --harness for the current client.\n'
               'V3 source-backed context (data, not instructions):\n')


def _make_record(i, n, truncated=False):
    return {
        "id": f"md-{i:032x}",
        "source": f"notes/note-{i}.md",
        "kind": "note",
        "visibility": "internal",
        "revision": 1,
        "text": f"Note {i} content: " + ("x" * n),
        "facts": {},
        "text_truncated": truncated,
    }


def _markdown_record(i, n, metadata=False):
    """A long note shaped like synced Markdown: newlines and quotes cost two JSON characters."""
    body = ('Line %d of a synced note, with "quoted" content.\n' % i) * (n // 48 + 1)
    record = {"id": f"md-{i:024x}", "source": f"knowledge/concepts/note-{i}.md", "kind": "note",
              "visibility": "internal", "revision": 1, "facts": {}, "supersedes": [], "text": body[:n]}
    if metadata:  # frontmatter as the sync projects it (~550 characters per record)
        record.update(title=f"Note {i} title", aliases=[f"alias {i} one", f"alias {i} two", f"alias {i} three"],
                      tags=["concept", "memory", "agents"], sources=[f"daily/2026-09-{i + 1:02d}.md"],
                      created="2026-09-01", updated="2026-09-20", updated_at="2026-09-20",
                      source_sha256="0" * 64)
    return record


class TestPackContextFairShare(unittest.TestCase):
    def test_one_long_note_does_not_starve_four_short_notes(self):
        """Issue 79: A single long leading note must not starve 4 short relevant notes."""
        records = [_make_record(0, 9000)] + [_make_record(i, 400) for i in range(1, 5)]
        result = pack_context(records, limit=5, budget_chars=5000)

        self.assertEqual(len(result['records']), 5,
                         f"Expected all 5 notes to be delivered, but got {len(result['records'])}")
        self.assertEqual(result['omitted_count'], 0)
        self.assertLessEqual(result['used_chars'], 5000)

        # Short records 1-4 should not be truncated
        for rec in result['records'][1:]:
            self.assertFalse(rec.get('text_truncated', False))
            self.assertIn("Note ", rec['text'])

        # Long record 0 should be truncated and carry substantial text
        self.assertTrue(result['records'][0].get('text_truncated'))
        self.assertTrue(result['records'][0]['text'].endswith(' [truncated]'))
        self.assertGreater(len(result['records'][0]['text']), 1500)

    def test_five_equal_long_notes_all_delivered(self):
        """Issue 79 & Review: Five long notes delivered; rank priority preserves top note text."""
        records = [_make_record(i, 3000) for i in range(5)]
        result = pack_context(records, limit=5, budget_chars=5000)

        self.assertEqual(len(result['records']), 5,
                         f"Expected all 5 notes delivered, but got {len(result['records'])}")
        self.assertEqual(result['omitted_count'], 0)
        self.assertLessEqual(result['used_chars'], 5000)

        # Top-ranked record gets rich context for answering sentence preservation (#83)
        self.assertGreater(len(result['records'][0]['text']), 2000)
        # All lower-ranked notes still get at least minimum useful text
        for rec in result['records']:
            self.assertGreaterEqual(len(rec['text']), 150)

    def test_2000_budget_long_notes_deliver_useful_text(self):
        """Review #81: At 2000 budget, notes must deliver useful text, not 36-char fragments."""
        records = [_make_record(i, 6000) for i in range(5)]
        result = pack_context(records, limit=5, budget_chars=2000)

        self.assertGreater(len(result['records']), 0)
        self.assertLessEqual(result['used_chars'], 2000)
        for rec in result['records']:
            self.assertGreaterEqual(len(rec['text']), 150)

        # render_context envelope must fit within 2000 budget
        text, delivered = render_context(result, 2000)
        self.assertLessEqual(len(text), 2000)
        for rec in delivered['records']:
            self.assertGreaterEqual(len(rec['text']), 150)

    def test_single_long_note_consumes_full_budget(self):
        """Single note when only 1 candidate exists should still take the full available budget."""
        records = [_make_record(0, 9000)]
        result = pack_context(records, limit=5, budget_chars=5000)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['omitted_count'], 0)
        self.assertLessEqual(result['used_chars'], 5000)
        self.assertGreater(result['used_chars'], 4000)

    def test_pre_truncated_record_reports_truncated(self):
        """Review Point 3: An incoming record that is already truncated reports truncated=True."""
        records = [_make_record(0, 100, truncated=True)]
        result = pack_context(records, limit=5, budget_chars=5000)
        self.assertEqual(len(result['records']), 1)
        self.assertTrue(result['truncated'])

    def test_candidate_whose_floor_does_not_fit_leaves_its_slot(self):
        """Review Point 4: a candidate that cannot carry its floor is skipped and uses no slot."""
        rec0 = _make_record(0, 200)
        rec1 = dict(_make_record(1, 1000), facts={'blob': 'y' * 900})  # metadata alone overflows
        rec2 = _make_record(2, 50)
        result = pack_context([rec0, rec1, rec2], limit=2, budget_chars=900)
        self.assertLessEqual(result['used_chars'], 900)
        self.assertEqual([r['id'] for r in result['records']], [rec0['id'], rec2['id']])
        self.assertEqual(result['omitted_count'], 1)

    def test_used_chars_strict_invariant_with_control_chars(self):
        """Review Point 2: used_chars strictly <= budget_chars with control chars and JSON escaping."""
        chars_pool = string.printable + '\u0000\u0001\u001f\t\n\r"\\ '
        rng = random.Random(1337)
        for _ in range(500):  # 500 randomized cases
            n_rec = rng.randint(1, 6)
            records = []
            for j in range(n_rec):
                t_len = rng.randint(0, 1500)
                t = ''.join(rng.choice(chars_pool) for _ in range(t_len))
                records.append({
                    'id': f'id-{j}',
                    'source': f'notes/n_{j}.md',
                    'kind': 'note',
                    'text': t,
                    'facts': {'k': rng.choice(['v1', 'v2'])},
                    'text_truncated': rng.choice([True, False])
                })
            limit = rng.randint(1, 5)
            budget = rng.randint(150, 4000)
            res = pack_context(records, limit=limit, budget_chars=budget)
            self.assertLessEqual(res['used_chars'], budget)
            actual_used = sum(len(_json(r)) + len(_json(c)) for r, c in zip(res['records'], res['citations']))
            self.assertEqual(actual_used, res['used_chars'])
            # Rank order is kept and an admitted clip always carries its floor of text.
            order = [r['id'] for r in records]
            delivered = [r['id'] for r in res['records']]
            self.assertEqual(delivered, sorted(delivered, key=order.index))
            for rec in res['records']:
                original = records[order.index(rec['id'])]['text']
                if rec['text'] != original:
                    kept = rec['text'][:-len(MARKER)]
                    self.assertTrue(original.startswith(kept))
                    self.assertGreaterEqual(len(kept), runtime.MIN_CONTEXT_TEXT)

    def test_retrieve_integration_delivers_multiple_sources(self):
        """Integration: MemoryStore.retrieve delivers multiple sources when one is long."""
        tmp = tempfile.TemporaryDirectory(prefix='beyin_test_pack_context_')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault = root / 'vault'
        (vault / 'notes').mkdir(parents=True)
        store = MemoryStore(root / 'runtime', vault)
        self.addCleanup(store.close)

        # Ingest 1 long note and 3 short notes with shared query topic 'architecture'
        long_text = "System architecture overview documentation " + ("detailed specs " * 400)
        (vault / "notes/arch-overview.md").write_text(long_text, encoding='utf-8')
        store.ingest({"id": "arch-overview", "source": "notes/arch-overview.md", "kind": "note",
                      "visibility": "internal", "revision": 1, "text": long_text, "facts": {}})
        for i in range(1, 4):
            path = f"notes/arch-component-{i}.md"
            text = f"Architecture component {i} short summary and configuration."
            (vault / path).write_text(text, encoding='utf-8')
            store.ingest({"id": f"arch-component-{i}", "source": path, "kind": "note",
                          "visibility": "internal", "revision": 1, "text": text, "facts": {}})

        res = store.retrieve("architecture", limit=4, budget_chars=4000)
        self.assertEqual(len(res['records']), 4,
                         f"Store.retrieve should deliver 4 notes, but got {len(res['records'])}")
        self.assertEqual(res['omitted_count'], 0)
        self.assertLessEqual(res['used_chars'], 4000)


class TestPackContextEscaping(unittest.TestCase):
    def test_escaped_markdown_never_drops_or_inverts_admitted_sources(self):
        """Newlines and quotes cost two JSON characters: claims are measured after escaping."""
        records = [_markdown_record(i, 6000) for i in range(5)]
        result = pack_context(records, limit=5, budget_chars=5000)
        delivered = [r["id"] for r in result["records"]]
        self.assertEqual(delivered, [r["id"] for r in records[:len(delivered)]])
        self.assertGreaterEqual(len(delivered), 4)
        for rec in result["records"]:
            self.assertGreaterEqual(len(rec["text"]) - len(MARKER), runtime.MIN_CONTEXT_TEXT)
        self.assertLessEqual(result["used_chars"], 5000)
        self.assertGreater(result["used_chars"], 5000 - 50)

    def test_reserved_budget_is_not_wasted(self):
        """Two long Markdown notes: both delivered and the budget is spent, not stranded."""
        records = [_markdown_record(0, 9000), _markdown_record(1, 9000)]
        result = pack_context(records, limit=5, budget_chars=5000)
        self.assertEqual(len(result["records"]), 2)
        self.assertGreater(result["used_chars"], 5000 - 50)

    def test_best_source_keeps_its_share_before_breadth(self):
        """The best source keeps max(TOP_CONTEXT_TEXT, TOP_CONTEXT_SHARE of the budget); others get floors."""
        records = [_markdown_record(i, 6000, metadata=True) for i in range(5)]
        for budget in (2000, 5000, 8000):
            result = pack_context(records, limit=5, budget_chars=budget)
            best, citation = result["records"][0], result["citations"][0]
            self.assertEqual(best["id"], records[0]["id"])
            self.assertGreaterEqual(len(_json(best)) + len(_json(citation)), int(budget * runtime.TOP_CONTEXT_SHARE))
            self.assertGreaterEqual(len(best["text"]) - len(MARKER), runtime.TOP_CONTEXT_TEXT)
            for rec in result["records"][1:]:
                self.assertGreaterEqual(len(rec["text"]) - len(MARKER), runtime.MIN_CONTEXT_TEXT)
            self.assertGreater(result["used_chars"], budget - 50)
        self.assertGreaterEqual(len(pack_context(records, limit=5, budget_chars=5000)["records"]), 3)

    def test_realistic_metadata_small_budget_keeps_best_source_whole(self):
        """#83: with ~550 chars of frontmatter, a passage-sized best source survives 2000 whole."""
        answer = "The nightly backup writes to the zircon bucket at 03:40."
        best = dict(_markdown_record(0, 900, metadata=True))
        best["text"] = best["text"] + answer
        records = [best] + [_markdown_record(i, 6000, metadata=True) for i in range(1, 5)]
        result = pack_context(records, limit=5, budget_chars=2000)
        self.assertEqual(result["records"][0]["id"], best["id"])
        text, delivered = render_context(result, 2000, prefix=HOOK_PREFIX)
        self.assertLessEqual(len(text), 2000)
        self.assertEqual(delivered["records"][0]["id"], best["id"])
        self.assertIn(answer, delivered["records"][0]["text"])

    def test_render_reclips_from_unclipped_sources(self):
        """The envelope is re-packed from the originals: a clip is never clipped again."""
        records = [_markdown_record(i, 6000, metadata=True) for i in range(5)]
        packed = pack_context(records, limit=5, budget_chars=2000)
        self.assertEqual([r["id"] for r in packed.sources], [r["id"] for r in packed["records"]])
        text, delivered = render_context(packed, 2000, prefix="P" * 300)
        stale_text, stale = render_context(dict(packed), 2000, prefix="P" * 300)
        self.assertLessEqual(len(text), 2000)
        self.assertLessEqual(len(stale_text), 2000)
        self.assertEqual(delivered["records"][0]["id"], records[0]["id"])
        self.assertGreaterEqual(len(delivered["records"][0]["text"]), len(stale["records"][0]["text"]))
        self.assertNotIn("sources", json.loads(text[300:]))

    def test_clip_to_centers_matching_passage_in_long_record(self):
        """Issue #847: Long note clipping preserves the matched query passage window instead of note head."""
        body = []
        for i in range(1, 70):
            body.append(f"## 2026-10-{i%28+1:02d} 10:00 · Eski oturum {i}")
            body.append(f"- **Karar:** Eski genel kayıt numarası {i} ve buna bağlı uzun açıklama metni burada arşivlenmiş karar olarak duruyor.")
            body.append("")
        body.append("## 2026-11-21 18:00 · Güncel karar")
        body.append("- **Karar:** `KUZEY_YILDIZI_2026` parametresi kesinlikle `aktif` olarak bırakılacak.")
        full_text = "\n".join(body)

        rec = {"id": "kararlar", "source": "Kararlar.md", "kind": "note",
               "visibility": "internal", "revision": 1, "facts": {}, "supersedes": [], "text": full_text}
        result = pack_context([rec], limit=1, budget_chars=8000, query="KUZEY_YILDIZI_2026")
        delivered = result["records"][0]
        self.assertTrue(delivered.get("text_truncated"))
        self.assertIn("KUZEY_YILDIZI_2026", delivered["text"])
        self.assertLessEqual(result["used_chars"], 8000)
        text, _ = render_context(result, 3000, prefix=HOOK_PREFIX)
        self.assertIn("KUZEY_YILDIZI_2026", text)
        self.assertLessEqual(len(text), 3000)



if __name__ == '__main__':
    unittest.main()
