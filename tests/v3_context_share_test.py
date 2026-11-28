#!/usr/bin/env python3
"""Tests for balanced context budget sharing (#975): synthetic local vaults only."""
import json
import os
from pathlib import Path
import random
import string
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
if str(ROOT / 'scripts') not in sys.path:
    sys.path.append(str(ROOT / 'scripts'))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import beyin_v3 as runtime  # noqa: E402
import beyin_v3_preferences as prefs  # noqa: E402
import beyin_entry  # noqa: E402
from v3_package_helpers import inherited_env  # noqa: E402


def _make_record(i, text_len, *, metadata=False, extra_text=""):
    unit = f"seg{i} "
    body = f"Note {i} overview: " + (unit * (text_len // len(unit) + 1))
    body = body[:text_len] + extra_text
    rec = {
        "id": f"rec-{i:04d}",
        "source": f"notes/topic-{i}.md",
        "kind": "note",
        "visibility": "internal",
        "revision": 1,
        "text": body,
        "facts": {},
        "supersedes": [],
    }
    if metadata:
        rec.update(
            title=f"Topic Note {i} Reference Title",
            aliases=[f"alias-{i}-alpha", f"alias-{i}-beta", f"alias-{i}-gamma"],
            tags=["architecture", "reference", "notes"],
            sources=[f"daily/2026-09-{i + 1:02d}.md"],
            updated_at="2026-09-20T10:00:00Z",
            source_sha256="a" * 64,
        )
    return rec


def _structured_long_note(index, topic_phrase, answer_marker, pad_sections=10):
    """Build a ~3,300+ char Markdown note with a deep matching section."""
    filler_para = (
        f"Background section for note {index}: routine operational context, "
        f"historical measurements, and generic placeholder details repeated for length. "
    ) * 2
    parts = [f"# Synthetic Dossier {index}", f"Opening summary for dossier {index}."]
    for sec in range(pad_sections):
        parts.append(f"## Background {sec}\n\n{filler_para}")
    parts.append(
        f"## Target Analysis\n\n"
        f"{topic_phrase}. Key finding for dossier {index}: {answer_marker}. "
        f"This section holds the concrete answer deep inside dossier {index}."
    )
    parts.append(f"## Appendix\n\n{filler_para}")
    return "\n\n".join(parts) + "\n"


class BalancedPackContextUnitTest(unittest.TestCase):
    def test_five_long_notes_balanced_gives_every_record_at_least_1000_chars(self):
        records = [_make_record(i, 6000, metadata=True) for i in range(5)]
        balanced = runtime.pack_context(records, limit=5, budget_chars=8000, share="balanced")
        top = runtime.pack_context(records, limit=5, budget_chars=8000, share="top")

        self.assertEqual(len(balanced["records"]), 5)
        self.assertEqual(balanced["omitted_count"], 0)
        self.assertLessEqual(balanced["used_chars"], 8000)
        self.assertGreater(balanced["used_chars"], 7900)
        for rec in balanced["records"]:
            self.assertGreaterEqual(len(rec["text"]), 1000, rec["id"])
            self.assertTrue(rec.get("text_truncated"))
        self.assertTrue(balanced["truncated"])

        # Top mode remains unchanged: rank 1 dominates while ranks 2-5 stay near floor.
        self.assertGreater(len(top["records"][0]["text"]), 4000)
        for rec in top["records"][1:]:
            self.assertLess(len(rec["text"]), 250)

    def test_top_share_is_identical_to_default_pack_context(self):
        records = [_make_record(i, 3000, metadata=True) for i in range(5)]
        default_packed = runtime.pack_context(records, limit=5, budget_chars=8000)
        top_packed = runtime.pack_context(records, limit=5, budget_chars=8000, share="top")
        self.assertEqual(
            json.dumps(default_packed, sort_keys=True, ensure_ascii=False),
            json.dumps(top_packed, sort_keys=True, ensure_ascii=False),
        )

    def test_short_note_redistribution_gives_leftover_to_long_notes(self):
        short0 = _make_record(0, 90)
        short1 = _make_record(1, 220)
        longs = [_make_record(i, 3500) for i in range(2, 5)]
        records = [short0, short1] + longs

        result = runtime.pack_context(records, limit=5, budget_chars=8000, share="balanced")
        self.assertEqual(len(result["records"]), 5)
        self.assertEqual(result["omitted_count"], 0)
        self.assertLessEqual(result["used_chars"], 8000)
        self.assertGreater(result["used_chars"], 7900)

        # Short notes remain completely untruncated.
        self.assertEqual(result["records"][0]["text"], short0["text"])
        self.assertFalse(result["records"][0].get("text_truncated", False))
        self.assertEqual(result["records"][1]["text"], short1["text"])
        self.assertFalse(result["records"][1].get("text_truncated", False))

        # Long notes receive the redistributed leftover (> 1,900 chars each vs ~1,200 5-way).
        long_lengths = [len(r["text"]) for r in result["records"][2:]]
        for length in long_lengths:
            self.assertGreater(length, 1900)
        self.assertLessEqual(max(long_lengths) - min(long_lengths), 20)

    def test_multi_round_water_filling_cascade(self):
        # 1 very short (80), 1 medium (700), 3 long (4000) in budget 6000.
        # Round 1 saturates short (80); round 2 saturates medium (700); remaining 3 split evenly.
        r_short = _make_record(0, 80)
        r_med = _make_record(1, 700)
        r_longs = [_make_record(i, 4000) for i in range(2, 5)]
        result = runtime.pack_context([r_short, r_med] + r_longs, limit=5, budget_chars=6000, share="balanced")
        self.assertEqual(len(result["records"]), 5)
        self.assertLessEqual(result["used_chars"], 6000)
        self.assertEqual(result["records"][0]["text"], r_short["text"])
        self.assertEqual(result["records"][1]["text"], r_med["text"])
        long_lengths = [len(r["text"]) for r in result["records"][2:]]
        self.assertTrue(all(length > 1200 for length in long_lengths))
        self.assertLessEqual(max(long_lengths) - min(long_lengths), 20)

    def test_all_short_notes_fit_untruncated_in_balanced_mode(self):
        records = [_make_record(i, 300) for i in range(5)]
        result = runtime.pack_context(records, limit=5, budget_chars=8000, share="balanced")
        self.assertEqual(len(result["records"]), 5)
        self.assertFalse(result["truncated"])
        for orig, got in zip(records, result["records"]):
            self.assertEqual(got["text"], orig["text"])

    def test_asymmetric_metadata_still_shares_text_budget_fairly(self):
        heavy = _make_record(0, 3200, metadata=True)
        heavy["facts"] = {"notes": "m" * 400}
        lights = [_make_record(i, 3200, metadata=False) for i in range(1, 5)]
        result = runtime.pack_context([heavy] + lights, limit=5, budget_chars=8000, share="balanced")
        self.assertEqual(len(result["records"]), 5)
        self.assertLessEqual(result["used_chars"], 8000)
        text_lengths = [len(r["text"]) for r in result["records"]]
        for length in text_lengths:
            self.assertGreaterEqual(length, 1000)
        self.assertLessEqual(max(text_lengths) - min(text_lengths), 50)

    def test_dominant_rank1_score_falls_back_to_top_allocation(self):
        records = [_make_record(i, 3200, metadata=True) for i in range(5)]
        top = runtime.pack_context(records, limit=5, budget_chars=8000, share="top")

        for scores in (
            [4.0, 2.0, 1.9, 1.8, 1.7],     # exact 2.0x boundary
            [10.0, 2.5, 2.0, 1.5, 1.0],    # strictly > 2.0x
            [2.0, 0.0, -10.0, -20.0, -30.0],  # score_0 > 0 and score_1 <= 0
            [1.0, -50.0, -50.0, -50.0, -50.0],
        ):
            with self.subTest(scores=scores):
                got = runtime.pack_context(records, limit=5, budget_chars=8000, share="balanced", scores=scores)
                self.assertEqual(got["records"], top["records"])
                self.assertEqual(got["used_chars"], top["used_chars"])

        # Just below 2.0x stays balanced; equal zero/negative scores also stay balanced.
        for scores in (
            [3.98, 2.0, 1.9, 1.8, 1.7],
            [0.0, 0.0, 0.0, 0.0, 0.0],
            [-10.0, -40.0, -45.0, -50.0, -55.0],
        ):
            with self.subTest(non_dominant=scores):
                got = runtime.pack_context(records, limit=5, budget_chars=8000, share="balanced", scores=scores)
                self.assertTrue(all(len(r["text"]) >= 1000 for r in got["records"]))

    def test_oversized_metadata_candidate_skipped_without_consuming_slot(self):
        rec0 = _make_record(0, 400)
        rec1 = dict(_make_record(1, 1000), facts={"blob": "z" * 1200})
        rec2 = _make_record(2, 400)
        result = runtime.pack_context([rec0, rec1, rec2], limit=2, budget_chars=1200, share="balanced")
        self.assertLessEqual(result["used_chars"], 1200)
        self.assertEqual([r["id"] for r in result["records"]], [rec0["id"], rec2["id"]])
        self.assertEqual(result["omitted_count"], 1)

    def test_balanced_strict_budget_invariant_under_json_escaping(self):
        chars_pool = string.printable + '\u0000\u0001\u001f\t\n\r"\\ '
        rng = random.Random(2026)
        for _ in range(200):
            n_rec = rng.randint(1, 6)
            records = []
            for j in range(n_rec):
                t = ''.join(rng.choice(chars_pool) for _ in range(rng.randint(0, 1600)))
                records.append({
                    'id': f'esc-{j}',
                    'source': f'notes/esc_{j}.md',
                    'kind': 'note',
                    'text': t,
                    'facts': {'k': rng.choice(['v1', 'v2'])},
                })
            limit = rng.randint(1, 5)
            budget = rng.randint(200, 5000)
            res = runtime.pack_context(records, limit=limit, budget_chars=budget, share="balanced")
            self.assertLessEqual(res["used_chars"], budget)
            actual = sum(len(runtime._json(r)) + len(runtime._json(c))
                         for r, c in zip(res["records"], res["citations"]))
            self.assertEqual(actual, res["used_chars"])
            order = [r["id"] for r in records]
            delivered = [r["id"] for r in res["records"]]
            self.assertEqual(delivered, sorted(delivered, key=order.index))
            for rec in res["records"]:
                original = records[order.index(rec["id"])]["text"]
                if rec["text"] != original:
                    kept = rec["text"][:-len(runtime.CONTEXT_MARKER)]
                    self.assertTrue(original.startswith(kept))
                    self.assertGreaterEqual(len(kept), runtime.MIN_CONTEXT_TEXT)

    def test_invalid_share_value_raises_value_error(self):
        records = [_make_record(0, 200)]
        for bad in ("invalid", "", "equal", 123, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    runtime.pack_context(records, share=bad)
                with self.assertRaises(ValueError):
                    runtime.pack_widened(records, {}, share=bad)

    def test_pack_widened_and_render_context_preserve_balanced_mode(self):
        records = [_make_record(i, 3000) for i in range(5)]
        packed = runtime.pack_widened(records, {}, limit=5, budget_chars=8000, share="balanced")
        self.assertTrue(all(len(r["text"]) >= 1000 for r in packed["records"]))
        rendered, delivered = runtime.render_context(packed, 8000)
        self.assertLessEqual(len(rendered), 8000)
        self.assertEqual(len(delivered["records"]), 5)
        self.assertTrue(all(len(r["text"]) >= 1000 for r in delivered["records"]))


class ContextShareEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-context-share-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        (self.vault / 'notes').mkdir(parents=True)
        self.state = self.root / 'state'

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
             '--vault', str(self.vault), '--state', str(self.state), *args],
            capture_output=True, text=True, encoding='utf-8', env=inherited_env(), timeout=20,
        )

    def cli_json(self, *args):
        res = self.run_cli(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def hook(self, event, prompt=None, harness='codex'):
        payload = {'hook_event_name': event, 'session_id': 'synthetic-share', 'event_id': event}
        if prompt is not None:
            payload['prompt'] = prompt
        res = subprocess.run(
            [sys.executable, str(ROOT / 'template/.claude/scripts/beyin_v3_hook.py'),
             '--vault', str(self.vault), '--state', str(self.state), '--harness', harness],
            input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
            env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=20,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def _populate_five_similar_long_notes(self):
        topic = "cobalt turbine calibration protocol"
        markers = []
        for i in range(5):
            marker = f"TURBINE_CANARY_{i}_VERIFIED"
            markers.append(marker)
            content = _structured_long_note(i, topic, marker, pad_sections=20)
            self.assertGreater(len(content), 6000)
            (self.vault / f'notes/dossier-{i}.md').write_text(
                f'---\n{{"id":"dossier-{i}","kind":"note","visibility":"internal","updated_at":"2026-09-20T10:00:00Z"}}\n---\n'
                + content,
                encoding='utf-8',
            )
        self.cli_json('sync')
        return topic, markers

    def test_acceptance_975_five_long_notes_balanced_vs_top_byte_identical(self):
        topic, markers = self._populate_five_similar_long_notes()

        raw_default = self.run_cli('context', topic, '--limit', '5', '--no-sync')
        raw_top = self.run_cli('context', topic, '--limit', '5', '--share', 'top', '--no-sync')
        raw_balanced = self.run_cli('context', topic, '--limit', '5', '--share', 'balanced', '--no-sync')

        self.assertEqual(raw_default.returncode, 0, raw_default.stderr)
        self.assertEqual(raw_top.returncode, 0, raw_top.stderr)
        self.assertEqual(raw_balanced.returncode, 0, raw_balanced.stderr)

        # --share top and omitted --share must be byte-for-byte identical.
        self.assertEqual(raw_default.stdout, raw_top.stdout)

        bal = json.loads(raw_balanced.stdout)
        top = json.loads(raw_top.stdout)

        # Acceptance criterion (#975): all 5 records returned, each len(text) >= 1000, within budget_chars.
        self.assertEqual(len(bal["records"]), 5)
        self.assertEqual(bal["omitted_count"], 0)
        self.assertLessEqual(bal["used_chars"], 8000)
        for rec in bal["records"]:
            self.assertGreaterEqual(len(rec["text"]), 1000, rec["source"])
            self.assertTrue(rec.get("text_truncated"))

        # Matched-passage centering: deep answer marker survives in EVERY balanced record.
        delivered_by_id = {r["id"]: r["text"] for r in bal["records"]}
        for i, marker in enumerate(markers):
            self.assertIn(marker, delivered_by_id[f"dossier-{i}"])

        # Contrast with top mode: rank 1 gets bulk while ranks 2-5 get clipped small.
        self.assertEqual(len(top["records"]), 5)
        self.assertGreater(len(top["records"][0]["text"]), 4500)
        for rec in top["records"][1:]:
            self.assertLess(len(rec["text"]), 250)

        # Direct Python API (MemoryStore.retrieve & shared_context) matches CLI balanced behavior.
        store = runtime.MemoryStore(self.state, self.vault, read_only=True)
        api_bal = runtime.shared_context(store, 'codex', topic, limit=5, budget_chars=8000, share='balanced')
        self.assertEqual(len(api_bal["records"]), 5)
        self.assertTrue(all(len(r["text"]) >= 1000 for r in api_bal["records"]))
        with self.assertRaises(ValueError):
            store.retrieve(topic, share='unknown')

    def test_end_to_end_dominant_rank1_falls_back_to_top(self):
        # Rank 1 matches exact note title (+20 name salience); ranks 2-5 mention words only once.
        (self.vault / 'notes/cobalt-turbine.md').write_text(
            '---\n{"id":"dominant","title":"Cobalt Turbine","kind":"note","visibility":"internal"}\n---\n'
            + _structured_long_note(0, "cobalt turbine calibration", "DOMINANT_CANARY", pad_sections=18),
            encoding='utf-8',
        )
        for i in range(1, 5):
            (self.vault / f'notes/secondary-{i}.md').write_text(
                f'---\n{{"id":"sec-{i}","title":"Misc {i}","kind":"note","visibility":"internal"}}\n---\n'
                + _structured_long_note(i, "cobalt turbine mention", f"SEC_CANARY_{i}", pad_sections=18),
                encoding='utf-8',
            )
        self.cli_json('sync')

        raw_top = self.run_cli('context', 'cobalt turbine', '--limit', '5', '--share', 'top', '--no-sync')
        raw_bal = self.run_cli('context', 'cobalt turbine', '--limit', '5', '--share', 'balanced', '--no-sync')
        self.assertEqual(raw_top.returncode, 0, raw_top.stderr)
        self.assertEqual(raw_bal.returncode, 0, raw_bal.stderr)
        self.assertEqual(raw_bal.stdout, raw_top.stdout)
        top_data = json.loads(raw_bal.stdout)
        self.assertEqual(top_data["records"][0]["id"], "dominant")
        self.assertGreater(len(top_data["records"][0]["text"]), 4500)
        for rec in top_data["records"][1:]:
            self.assertLess(len(rec["text"]), 250)

    def test_privacy_and_stale_gates_preserved_in_balanced_mode(self):
        topic, _ = self._populate_five_similar_long_notes()
        (self.vault / 'notes/secret.md').write_text(
            '---\n{"id":"secret-1","visibility":"private"}\n---\n' + topic + ' PRIVATE_SECRET_CANARY\n',
            encoding='utf-8',
        )
        self.cli_json('sync')
        # Modify dossier-4 on disk after sync so it becomes stale under --no-sync.
        (self.vault / 'notes/dossier-4.md').write_text('modified externally\n', encoding='utf-8')
        bal = self.cli_json('context', topic, '--limit', '5', '--share', 'balanced', '--no-sync')
        self.assertEqual(bal["stale_count"], 1)
        ids = [r["id"] for r in bal["records"]]
        self.assertNotIn("secret-1", ids)
        self.assertNotIn("dossier-4", ids)
        self.assertEqual(len(ids), 4)
        self.assertTrue(all(len(r["text"]) >= 1000 for r in bal["records"]))

    def test_preferences_context_share_persistence_override_and_hook_isolation(self):
        topic, _ = self._populate_five_similar_long_notes()

        # Default preference is 'top' without creating vault preference files.
        initial = self.cli_json('preferences')
        self.assertEqual(initial['context_share'], 'top')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())
        self.assertFalse((self.state / 'context-share.json').exists())

        # Capture baseline hook output before setting balanced preference.
        hook_before = self.hook('UserPromptSubmit', prompt=topic)

        # Save balanced preference (machine-local & rollback-safe).
        saved = self.cli_json('preferences', '--context-share', 'balanced')
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(saved['context_share'], 'balanced')
        self.assertFalse((self.vault / '.beyin-preferences.json').exists())
        self.assertTrue((self.state / 'context-share.json').exists())
        self.assertEqual(
            json.loads((self.state / 'context-share.json').read_text(encoding='utf-8')),
            {'schema': 1, 'context_share': 'balanced'},
        )
        # Human preferences output surfaces the active mode cleanly.
        human_text = beyin_entry.human_result(saved, 'preferences')
        self.assertIn('balanced', human_text)

        # Profile changes leave context_share intact and keep doctor profile accurate.
        after_profile = self.cli_json('preferences', '--profile', 'economical')
        self.assertEqual(after_profile['context_share'], 'balanced')
        self.assertEqual(self.cli_json('doctor')['setup_summary']['profile'], 'economical')

        # context without --share uses saved 'balanced' preference.
        from_pref = self.cli_json('context', topic, '--limit', '5', '--no-sync')
        explicit_bal = self.cli_json('context', topic, '--limit', '5', '--share', 'balanced', '--no-sync')
        self.assertEqual(from_pref, explicit_bal)
        self.assertTrue(all(len(r["text"]) >= 1000 for r in from_pref["records"]))

        # Explicit --share top overrides saved 'balanced' preference.
        override_top = self.cli_json('context', topic, '--limit', '5', '--share', 'top', '--no-sync')
        self.assertGreater(len(override_top["records"][0]["text"]), 4000)
        self.assertLess(len(override_top["records"][1]["text"]), 300)

        # --file JSON "share" works and CLI --share overrides --file.
        req_file = self.root / 'req.json'
        req_file.write_text(json.dumps({'query': topic, 'limit': 5, 'share': 'top'}), encoding='utf-8')
        file_top = self.cli_json('context', '--file', str(req_file), '--no-sync')
        self.assertEqual(file_top, override_top)
        cli_over_file = self.cli_json('context', '--file', str(req_file), '--share', 'balanced', '--no-sync')
        self.assertEqual(cli_over_file, explicit_bal)

        # Reset profile back to normal so hook context_mode is 'turn', then verify hook output is unchanged.
        self.cli_json('preferences', '--profile', 'normal')
        hook_after = self.hook('UserPromptSubmit', prompt=topic)
        self.assertEqual(hook_after, hook_before)

        # Switch back to 'top' cleanly.
        reverted = self.cli_json('preferences', '--context-share', 'top')
        self.assertEqual(reverted['status'], 'saved')
        self.assertEqual(reverted['context_share'], 'top')
        back_to_top = self.cli_json('context', topic, '--limit', '5', '--no-sync')
        self.assertEqual(back_to_top, override_top)

    def test_damaged_or_oversized_context_share_file_fails_open_on_read_and_refuses_overwrite(self):
        topic, _ = self._populate_five_similar_long_notes()
        path = self.state / 'context-share.json'
        for raw in ('{broken', '{"schema": 1, "context_share": "unknown"}', '{"schema": 2, "context_share": "balanced"}', '[]', 'x' * 5000):
            with self.subTest(raw=raw[:30]):
                path.write_text(raw, encoding='utf-8')
                self.assertEqual(prefs.read_context_share(self.state), ('top', False))
                # Reading context fails open to 'top'.
                ctx = self.cli_json('context', topic, '--limit', '5', '--no-sync')
                self.assertGreater(len(ctx["records"][0]["text"]), 4000)
                # preferences read shows notice.
                pref_res = self.cli_json('preferences')
                self.assertEqual(pref_res['context_share'], 'top')
                self.assertIn('context_share_notice', pref_res)
                # Saving refuses to overwrite damaged file and saves no other flags.
                fail = self.run_cli('preferences', '--profile', 'manual', '--context-share', 'balanced')
                self.assertNotEqual(fail.returncode, 0)
                self.assertEqual(path.read_text(encoding='utf-8'), raw)
                self.assertFalse((self.vault / '.beyin-preferences.json').exists())

        # Symlink target is refused and fails open on read.
        path.unlink()
        outside = self.root / 'outside-share.json'
        outside.write_text('{"schema": 1, "context_share": "balanced"}', encoding='utf-8')
        try:
            os.symlink(outside, path)
        except OSError:
            pass  # Unprivileged Windows accounts may not have symlink privilege
        else:
            self.assertEqual(prefs.read_context_share(self.state), ('top', False))
            with self.assertRaises(ValueError):
                prefs.save_context_share(self.state, 'balanced')
            path.unlink()

    def test_share_with_jev_raises_descriptive_value_error(self):
        topic, _ = self._populate_five_similar_long_notes()
        for share in ('balanced', 'top'):
            with self.subTest(share=share):
                res = self.run_cli('context', topic, '--share', share, '--jev', '--project', 'demo')
                self.assertNotEqual(res.returncode, 0)
                self.assertEqual(res.stdout, '')
                err = json.loads(res.stderr)
                self.assertEqual(err['error'], 'ValueError')
                self.assertIn('--share is only available for local context', err['message'])

        # Passing "share" via --file with --jev is also rejected.
        req_file = self.root / 'jev_req.json'
        req_file.write_text(json.dumps({'query': topic, 'project': 'demo', 'share': 'balanced'}), encoding='utf-8')
        res_file = self.run_cli('context', '--file', str(req_file), '--jev')
        self.assertNotEqual(res_file.returncode, 0)
        self.assertIn('--share is only available for local context', res_file.stderr)

        # Saved preference alone (without explicit --share) does NOT raise the --share error with --jev.
        self.cli_json('preferences', '--context-share', 'balanced')
        res_pref = self.run_cli('context', topic, '--jev', '--project', 'demo')
        self.assertNotIn('--share is only available for local context', res_pref.stderr)


if __name__ == '__main__':
    unittest.main()
