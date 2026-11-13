#!/usr/bin/env python3
"""Offline paired query-rewrite experiment; no runtime/ranking changes or model calls.

Both arms use the same synthetic/sanitized search fixture and target labels.
A rewrite must be supplied for every case (unchanged queries are allowed).
This reports target recall, not precision or generalization to a user's vault.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unicodedata

import evaluate_v3_search as search

MODES = ("non_strict", "context")
METRICS = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")


def paired_cases(fixture, variants):
    """Validate a complete paired set; never let a rewrite change target labels."""
    if not isinstance(fixture, dict) or not isinstance(variants, dict):
        raise ValueError("fixture and variants must be JSON objects")
    records, cases, changes = fixture.get("records"), fixture.get("cases"), variants.get("variants")
    if not all(isinstance(items, list) and items for items in (records, cases, changes)):
        raise ValueError("records, cases and variants must be non-empty lists")
    record_ids, sources = set(), set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("record must be an object")
        identity, source = record.get("id"), record.get("source")
        if (not isinstance(record.get("text"), str)
                or any(key in record and not isinstance(record[key], str)
                       for key in ("title", "kind", "visibility", "updated_at"))
                or not isinstance(record.get("facts", {}), dict)):
            raise ValueError("records need text, string metadata and object facts")
        if not isinstance(identity, str) or not identity or identity in record_ids:
            raise ValueError("record ids must be unique non-empty strings")
        if (not isinstance(source, str) or not source or "\\" in source or ":" in source
                or any(part in ("", ".", "..") for part in source.split("/"))
                or any(ord(c) < 32 for c in source)
                or PurePosixPath(source).is_absolute()
                or any(c in source for c in '<>"|?*')
                or any(part.endswith((".", " ")) or part.split(".")[0].upper() in
                       {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))} for part in source.split("/"))
                or unicodedata.normalize("NFC", source).casefold() in sources):
            raise ValueError("sources must be unique vault-relative paths without traversal")
        record_ids.add(identity)
        sources.add(unicodedata.normalize("NFC", source).casefold())
    case_ids = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not case["id"] or case["id"] in case_ids
                or not isinstance(case.get("query"), str) or not case["query"].strip()
                or not isinstance(case.get("target_id"), str)
                or case["target_id"] not in record_ids
                or not isinstance(case.get("category", "unclassified"), str)):
            raise ValueError("cases need unique ids, non-empty queries and known target ids")
        case_ids.add(case["id"])
    rewrites = {}
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {"id", "query"}
                or not isinstance(change["id"], str) or change["id"] in rewrites
                or not isinstance(change["query"], str) or not change["query"].strip()):
            raise ValueError("each variant must contain only a unique id and non-empty query")
        rewrites[change["id"]] = change["query"]
    if set(rewrites) != case_ids:
        raise ValueError("variants must cover exactly every case; keep unchanged queries explicitly")
    return [dict(case, query=rewrites[case["id"]]) for case in cases]


def evaluate(fixture_path, variants_path, modes=MODES, limit=5):
    if type(limit) is not int or limit != 5:
        raise ValueError("limit must be 5 for comparable manual and context arms")
    if not modes or any(mode not in MODES for mode in modes):
        raise ValueError("modes must be non_strict and/or context")
    fixture, digest = search.read_fixture(fixture_path)
    data = Path(variants_path).read_bytes()
    variants = json.loads(data.decode("utf-8"))
    rewritten = paired_cases(fixture, variants)
    runtime = search.load_runtime()
    report = {
        "scope": "paired query rewrite; same corpus and labels; no engine change",
        "fixture_digest": digest,
        "variants_digest": hashlib.sha256(data).hexdigest(),
        "corpus_records": len(fixture["records"]),
        "cases": len(rewritten), "limit": limit, "modes": {},
        "limits": ["single labelled target recall, not precision", "synthetic/sanitized corpus only",
                   "not a live hook/Jev or user-vault configuration measurement",
                   "latency is diagnostic; baseline warms caches for rewrite"],
    }
    with tempfile.TemporaryDirectory(prefix="beyin-paired-queries-") as tmp:
        store, _ = search.seed_store(runtime, Path(tmp), fixture)
        try:
            for mode in modes:
                baseline = search.evaluate_mode(store, fixture["cases"], mode, limit, runtime)
                changed = search.evaluate_mode(store, rewritten, mode, limit, runtime)
                pairs = []
                for before, after in zip(baseline["case_results"], changed["case_results"]):
                    old, new = before["rank"], after["rank"]
                    old_score, new_score = old or limit + 1, new or limit + 1
                    pairs.append({"case_id": before["case_id"], "target_id": before["target_id"],
                                  "before_rank": old, "after_rank": new,
                                  "change": "improved" if new_score < old_score else
                                  "regressed" if new_score > old_score else "unchanged"})
                report["modes"][mode] = {
                    "baseline": baseline, "rewritten": changed, "pairs": pairs,
                    "delta": {key: round(changed[key] - baseline[key], 3) for key in METRICS},
                }
        finally:
            store.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--variants", required=True, type=Path)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--limit", type=int, choices=[5], default=5)
    args = parser.parse_args()
    try:
        result = evaluate(args.fixture, args.variants, args.modes, args.limit)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
