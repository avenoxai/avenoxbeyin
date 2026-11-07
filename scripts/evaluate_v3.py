#!/usr/bin/env python3
"""Offline frozen retrieval benchmark. Never invokes a model or reads a user vault."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/v3/semantic_scenarios.json'
HASH = FIXTURE.with_suffix('.sha256')
RUNTIME = ROOT / 'template/.claude/scripts/beyin_v3.py'
CANARY = 'SYNTHETIC_PRIVATE_CANARY'


def read_fixture():
    raw = FIXTURE.read_bytes()
    expected = HASH.read_text().split()[0]
    actual = hashlib.sha256(raw).hexdigest()
    if expected != actual:
        raise ValueError('frozen-fixture-hash-mismatch')
    return json.loads(raw), actual


def load_runtime():
    if not RUNTIME.is_file():
        return None
    runtime_dir = str(RUNTIME.parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    spec = importlib.util.spec_from_file_location('beyin_v3_evaluation_runtime', RUNTIME)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


def seed_store(module, root, fixture):
    vault = root / 'vault'
    vault.mkdir()
    for record in fixture['records']:
        source = vault / record['source']
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(record['text'] + '\n', encoding='utf-8')
    store = module.MemoryStore(root / 'runtime', vault)
    for record in fixture['records']:
        store.ingest(dict(record))
    return store


def close_store(store):
    if callable(getattr(store, 'close', None)):
        store.close()


def normalized_context(module, store, harness, query, kwargs):
    # This MUST invoke production adapter code, never simply compare two retrieve calls.
    adapter = getattr(module, harness + '_context')
    return adapter(store, query, **kwargs)


def score_case(module, store, case, fixture_by_id):
    kwargs = {key: case[key] for key in ('project', 'audience', 'statuses') if key in case}
    kwargs.update(limit=5, budget_chars=8000)
    response = store.retrieve(case['query'], **kwargs)
    for key in ('records', 'citations', 'abstained', 'truncated', 'omitted_count'):
        if key not in response:
            raise ValueError('retrieval-missing-field:' + key)
    records = response['records']
    ids = [record['id'] for record in records]
    expected = set(case['expected_ids'])
    relevant = set(case.get('relevant_ids', case['expected_ids']))
    forbidden = set(case.get('forbidden_ids', []))
    forbidden.update(record['id'] for record in fixture_by_id.values()
                     if record.get('visibility') == 'private' or record.get('kind') == 'untrusted'
                     or (case.get('audience') == 'public' and record.get('visibility') != 'public'))
    hits = len(set(ids[:5]) & expected)
    actual = {record['id']: record for record in records}
    facts_total = facts_kept = 0
    for record_id in expected:
        for key, value in fixture_by_id[record_id].get('facts', {}).items():
            facts_total += 1
            facts_kept += actual.get(record_id, {}).get('facts', {}).get(key) == value
    for key, value in case.get('expected_facts', {}).items():
        facts_total += 1
        facts_kept += any(r.get('facts', {}).get(key) == value for r in records if r['id'] in expected)
    should_abstain = case.get('abstain', not expected)
    normalized = [normalized_context(module, store, h, case['query'], kwargs) for h in ('codex', 'claude')]
    # Source record fields, citations and budget metadata must be equivalent too.
    equivalent = normalized[0] == normalized[1] == response
    citation_blob = json.dumps(response['citations'], ensure_ascii=False)
    source_retention = all(actual.get(i, {}).get('source') == fixture_by_id[i]['source']
                           and fixture_by_id[i]['source'] in citation_blob for i in expected)
    result = {
        'case': case['id'], 'split': case.get('split', 'development'),
        'required_count': len(expected), 'required_hits': hits,
        'recall_at_5': hits / len(expected) if expected else None,
        'precision_at_5': len(set(ids[:5]) & relevant) / len(ids[:5]) if ids else (1.0 if not relevant else 0.0),
        'forbidden_count': len(set(ids) & forbidden),
        'abstention_correct': bool(response['abstained']) == should_abstain and (not ids if should_abstain else bool(ids)),
        'facts_total': facts_total, 'facts_kept': facts_kept,
        'privacy_canary_leaks': int(CANARY in json.dumps([response] + normalized, ensure_ascii=False)),
        'harness_equivalent': equivalent, 'sources_preserved': source_retention,
        'limit_respected': len(ids) <= 5 and len(ids) == len(set(ids)),
    }
    result['passed'] = (hits == len(expected) and result['forbidden_count'] == 0
                        and result['abstention_correct'] and facts_total == facts_kept
                        and result['privacy_canary_leaks'] == 0 and equivalent
                        and source_retention and result['limit_respected'])
    return result


def aggregate(rows):
    required = sum(r['required_count'] for r in rows)
    facts = sum(r['facts_total'] for r in rows)
    return {
        'cases': len(rows), 'passed_cases': sum(r['passed'] for r in rows),
        'recall_at_5': sum(r['required_hits'] for r in rows) / required if required else None,
        'precision_at_5_diagnostic': sum(r['precision_at_5'] for r in rows) / len(rows) if rows else None,
        'forbidden_count': sum(r['forbidden_count'] for r in rows),
        'abstention_accuracy': sum(r['abstention_correct'] for r in rows) / len(rows) if rows else None,
        'structured_fact_retention': sum(r['facts_kept'] for r in rows) / facts if facts else None,
        'privacy_canary_leaks': sum(r['privacy_canary_leaks'] for r in rows),
        'harness_equivalence': all(r['harness_equivalent'] for r in rows),
        'passed': bool(rows) and all(r['passed'] for r in rows),
    }


def evaluate(split="all"):
    fixture, digest = read_fixture()
    module = load_runtime()
    report = {'benchmark_version': fixture['version'], 'fixture_sha256': digest,
              'scope': 'Synthetic offline retrieval and persistence only; no generalization claim.',
              'precision_definition': 'Relevant returned IDs / returned IDs among first 5; empty correct abstention = 1.',
              'status': 'not_implemented' if module is None else 'evaluated',
              'runtime_sha256': hashlib.sha256(RUNTIME.read_bytes()).hexdigest() if module else None,
              'development': None, 'holdout': None, 'passed': False}
    if module is None:
        return report
    rows = []
    with tempfile.TemporaryDirectory(prefix='beyin-v3-eval-') as tmp:
        store = seed_store(module, Path(tmp), fixture)
        try:
            by_id = {r['id']: r for r in fixture['records']}
            for case in fixture['cases']:
                if split != 'all' and case.get('split', 'development') != split:
                    continue
                rows.append(score_case(module, store, case, by_id))
        finally:
            close_store(store)
    for group in ('development', 'holdout'):
        group_rows = [r for r in rows if r['split'] == group]
        if group_rows:
            report[group] = aggregate(group_rows)
    report['cases'] = rows
    report['passed'] = bool(rows) and all(r['passed'] for r in rows)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--split', choices=['all', 'development', 'holdout'], default='all')
    args = parser.parse_args()
    try:
        report = evaluate(args.split)
    except Exception as exc:
        report = {'status': 'evaluation_error', 'error_type': type(exc).__name__, 'error': str(exc), 'passed': False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
