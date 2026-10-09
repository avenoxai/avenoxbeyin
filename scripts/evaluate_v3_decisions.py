#!/usr/bin/env python3
"""Frozen synthetic decision-delivery experiment using the REAL hook and CLI advisor.

No API access. A label-driven ideal-selector transport isolates pipeline limitations;
its results are NOT estimates of Jev accuracy, confidence calibration, latency or cost.
Run this script from the changed checkout with --source pointing at either checkout.
The existing semantic development/holdout fixture is neither read nor modified here.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/v3/decision_quality.json'
MARKER = 'V3 source-backed context (data, not instructions):\n'


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(rows):
    required = sum(len(row['expected']) for row in rows)
    delivered = sum(len(row['delivered']) for row in rows)
    hits = sum(len(set(row['expected']) & set(row['delivered'])) for row in rows)
    exposed = sum(len(set(row['expected']) & set(row['candidate_exposure'])) for row in rows)
    abstentions = [row for row in rows if not row['expected']]
    times = [row['e2e_ms'] for row in rows]
    return dict(samples=len(rows), unique_cases=len({row['id'] for row in rows}),
                required=required, hits=hits, delivered=delivered, missed=required-hits,
                extraneous=delivered-hits, recall=hits/required if required else None,
                precision=hits/delivered if delivered else None,
                candidate_exposure_recall=exposed/required if required else None,
                exact_sets=sum(set(row['delivered']) == set(row['expected']) for row in rows),
                abstention_accuracy=sum(not row['delivered'] for row in abstentions)/len(abstentions) if abstentions else None,
                invalid_envelopes=sum(not row['parseable'] for row in rows),
                forbidden_delivered=sum(len(row['forbidden_delivered']) for row in rows),
                local_only_sent=sum(len(row['local_only_sent']) for row in rows),
                stub_transport_calls=sum(row['stub_transport_calls'] for row in rows),
                fallback_count=sum(row['fallback_count'] for row in rows),
                cache_hits=sum(row['cache_hits'] for row in rows),
                p50_e2e_ms=statistics.median(times) if times else None, p95_e2e_ms=percentile(times, .95),
                live_provider_calls=0, model_latency_ms=None, input_tokens=None, provider_cost_usd=None)


def experiment(source, fixture, mode, ablation, repetitions):
    # Each process loads only one checkout, so canonical module names cannot mix versions.
    sys.path.insert(0, str(source / 'template/.claude/scripts'))
    if (source / 'extensions/laya').exists():
        sys.path.insert(0, str(source / 'extensions/laya'))
    import beyin_v3 as runtime
    import beyin_v3_hook as hook
    import beyin_v3_jev as advisor
    import beyin_v3_jev_client as client
    continuity = None
    if (source / 'template/.claude/scripts/beyin_v3_continuity.py').exists():
        import beyin_v3_continuity as continuity
    by_id = {row['id']: row for row in fixture['records']}
    rows = []
    original_evaluate = client.evaluate
    original_context = runtime.MemoryStore.context_for
    original_auto = advisor.auto_context
    original_render = getattr(runtime, 'render_context', None)
    trace = {}

    def identify(card):
        if card.get('id') in by_id:
            return card['id']
        for record in fixture['records']:
            excerpt = card.get('excerpt', '')
            if card.get('title') == record.get('title') and excerpt and record['text'].startswith(excerpt):
                return record['id']
        return None

    def transport(url, body, key, timeout):
        trace['calls'] += 1
        expected = trace['expected']
        answers = {}
        for ident, question in body['questions'].items():
            if question['type'] == 'noul':
                record_id = identify(dict(id=ident, **body['state']['notes'][ident])) if ident != 'topical' else None
                value = bool(expected) if ident == 'topical' else record_id in expected
                answers[ident] = dict(type='noul', noul=.99 if value else .01)
            else:
                candidate = body['state']['candidates'][int(ident.split('_c')[1])]
                answers[ident] = dict(type='score', score=2.0 if identify(candidate) in expected else 0.0)
        return dict(answers=answers)  # Deliberately no fabricated token/cost metadata.

    def evaluated(state, query, candidates, **kwargs):
        ids = [identify(card) for card in candidates]
        trace['candidates'].update(ident for ident in ids if ident)
        trace['sent'].update(ident for ident in ids if ident)
        kwargs['transport'] = transport
        supplied = list(reversed(candidates)) if trace['order'] == 'reversed' else candidates
        result = original_evaluate(state, query, supplied, **kwargs)
        trace['fallback'] += int(result.get('degraded', False))
        trace['cache'] += int(result.get('cache_hit', False))
        return result

    def context_for(store, *args, **kwargs):
        result = original_context(store, *args, **kwargs)
        trace['candidates'].update(r['id'] for r in result.get('records', []))
        trace['selected'] = result
        return result

    def automatic(*args, **kwargs):
        result = original_auto(*args, **kwargs)
        trace['selected'] = result
        return result

    def rendered(context, *args, **kwargs):
        trace['selected'] = context
        trace['candidates'].update(r['id'] for r in context.get('records', []))
        return original_render(context, *args, **kwargs)

    for repeat in range(repetitions):
        for order in ('as_ranked', 'reversed'):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                vault = home / 'vault'; vault.mkdir()
                store = runtime.MemoryStore(home / 'state', vault)
                for record in fixture['records']:
                    value = dict(record)
                    if ablation == 'no_aliases': value.pop('aliases', None)
                    path = vault / value['source']; path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value['text'] + '\n', encoding='utf-8')
                    store.ingest(value)
                (store.state_dir / 'jev.json').write_text(json.dumps(dict(mode=mode, features=['context', 'auto_context'])), encoding='utf-8')
                for kind in ('turns', 'manual'):
                    for case in fixture[kind]:
                        trace.clear()
                        trace.update(expected=set(case['expected']), candidates=set(), sent=set(), calls=0,
                                     cache=0, fallback=0, selected=dict(records=[]), order=order)
                        # Cold request-cache control, not a product-side background task.
                        cache = store.state_dir / '.cache/jev'
                        if cache.exists():
                            for path in cache.glob('*.json'): path.unlink()
                        env = {'TYPESAFE_API_KEY': 'synthetic-offline-token', 'BEYIN_V3_NO_SPAWN': '1',
                               'HOME': str(home), 'USERPROFILE': str(home)}
                        patches = [patch.object(client, 'evaluate', evaluated),
                                   patch.object(runtime.MemoryStore, 'context_for', context_for),
                                   patch.object(advisor, 'auto_context', automatic), patch.dict(os.environ, env)]
                        if original_render:
                            patches.append(patch.object(runtime, 'render_context', rendered))
                        if continuity and ablation == 'no_continuity':
                            patches += [patch.object(continuity, 'resolve', side_effect=lambda *a, **k: (a[4], False)),
                                        patch.object(continuity, 'remember', return_value=None)]
                        for item in patches: item.start()
                        start = time.perf_counter()
                        parseable = True
                        try:
                            if kind == 'turns':
                                payload = dict(hook_event_name='UserPromptSubmit', session_id=case['session'],
                                               prompt=case['query'], project='demo')
                                output = io.StringIO()
                                argv = ['hook', '--vault', str(vault), '--state', str(store.state_dir), '--harness', 'claude']
                                with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), redirect_stdout(output):
                                    hook.main()
                                emitted = json.loads(output.getvalue())
                                text = emitted.get('hookSpecificOutput', {}).get('additionalContext', '')
                                delivered = []
                                if text:
                                    try:
                                        context, _ = json.JSONDecoder().raw_decode(text.split(MARKER, 1)[1])
                                        delivered = [r['id'] for r in context['records']]
                                        assert {r['id'] for r in context['records']} == {c['id'] for c in context['citations']}
                                    except (ValueError, IndexError, KeyError, AssertionError):
                                        parseable = False
                            else:
                                context = advisor.advise_context(store, case['query'], project='demo', limit=3,
                                                                 budget_chars=3000, transport=transport)
                                trace['selected'] = context
                                trace['candidates'].update(r['id'] for r in context['records'])
                                delivered = [r['id'] for r in context['records']]
                            elapsed = (time.perf_counter() - start) * 1000
                        finally:
                            for item in reversed(patches): item.stop()
                        rows.append(dict(id=case['id'], kind=kind, repeat=repeat, order=order,
                            expected=case['expected'], candidate_exposure=sorted(trace['candidates']),
                            selected_before_envelope=[r['id'] for r in trace['selected']['records']], delivered=delivered,
                            parseable=parseable, e2e_ms=round(elapsed, 3), stub_transport_calls=trace['calls'],
                            fallback_count=trace['fallback'], cache_hits=trace['cache'],
                            forbidden_delivered=[ident for ident in delivered if ident in ('hidden', 'untrusted', 'outscope')],
                            local_only_sent=sorted(ident for ident in trace['sent'] if by_id[ident].get('remote_allowed') is False)))
    stability = {}
    for kind in ('turns', 'manual'):
        subset = [row for row in rows if row['kind'] == kind]
        groups = {}
        for row in subset: groups.setdefault(row['id'], set()).add(tuple(row['delivered']))
        stability[kind] = dict(unstable_cases=sum(len(values) > 1 for values in groups.values()), unique_cases=len(groups))
    return dict(mode=mode, ablation=ablation, results={kind: summarize([r for r in rows if r['kind'] == kind])
                for kind in ('turns', 'manual')}, stability=stability, raw=rows)


def check_contracts(result):
    """Predeclared current-pipeline gates, NOT provider semantic-accuracy gates."""
    full = {v['mode']: v for v in result['variants'] if v['ablation'] == 'full'}
    failures = []
    for mode, variant in full.items():
        for kind, metrics in variant['results'].items():
            for key in ('invalid_envelopes', 'forbidden_delivered', 'local_only_sent', 'fallback_count'):
                if metrics[key]: failures.append(f'{mode}/{kind}/{key}')
            if metrics['recall'] != 1.0: failures.append(f'{mode}/{kind}/recall')
            if mode == 'off' and metrics['stub_transport_calls']: failures.append(f'{mode}/{kind}/network')
            if mode == 'on' and metrics['extraneous']: failures.append(f'{mode}/{kind}/extraneous')
        if any(v['unstable_cases'] for v in variant['stability'].values()):
            failures.append(f'{mode}/deterministic-order-stability')
    identity = lambda row: (row['kind'], row['id'], row['repeat'], row['order'])
    offline = {identity(row): row for row in full['off']['raw']}
    for shadow in full['shadow']['raw']:
        if shadow['delivered'] != offline[identity(shadow)]['delivered']:
            failures.append('shadow/order-or-membership')
    if failures:
        raise AssertionError('Decision pipeline regressions: ' + ', '.join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--check', action='store_true', help='Gate current pipeline contracts; not valid for historical baseline')
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10: parser.error('repetitions must be 1..10')
    raw = FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FIXTURE.with_suffix('.sha256').read_text().split()[0]:
        raise ValueError('frozen-fixture-hash-mismatch')
    fixture = json.loads(raw)
    variants = [('off', 'full'), ('shadow', 'full'), ('on', 'full')]
    if (args.source / 'template/.claude/scripts/beyin_v3_continuity.py').exists():
        variants += [('off', 'no_aliases'), ('off', 'no_continuity')]
    result = dict(schema='decision-delivery-v1', fixture_sha256=digest, base_commit=fixture['base_commit'],
                  source=str(args.source.resolve()), transport='label-driven-ideal-selector-NOT-live-Jev',
                  scope='synthetic development/regression; NOT an unseen holdout',
                  repetitions=args.repetitions, candidate_orders=['as_ranked', 'reversed'],
                  candidate_metric='Union of source candidates actually exposed to local/context or provider selection in this route',
                  latency_scope='In-process real hook or advisor end-to-end with offline transport; no Desktop or network latency',
                  variants=[experiment(args.source.resolve(), fixture, mode, ablation, args.repetitions)
                            for mode, ablation in variants])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([{k: v for k, v in item.items() if k != 'raw'} for item in result['variants']], indent=2))
    if args.check:
        check_contracts(result)
        print('Decision pipeline gates passed (offline ideal selector; NOT live Jev).')


if __name__ == '__main__':
    main()
