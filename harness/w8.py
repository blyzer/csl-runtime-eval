#!/usr/bin/env python3
"""W8.S1 incremental invalidation (ADR-0008): apply a delta incrementally vs rebuild, with a hard gate.

Hard invariant, checked for every case against the oracle: ColdBuild(S + delta) == IncrementalApply(S,
delta). The candidate that applied the delta must report the oracle's canonical `state_digest`, answer
the fixed query set Q (W1/W2 set, evidence-predicate queries, delta-targeted queries) byte-identically to
the oracle on S + delta, and a delta with one invalid operation must be rejected (INVALID_INPUT) leaving
digest and generation untouched. The cold-build path (`open` of the S + delta fixture) must agree too.
Timing covers only `mutate` / `open` themselves; oracle work, `state_digest` and query comparisons are
outside every timed interval. A full-rebuild strategy is measured as the reference and baseline and is
reported as such; it does not satisfy W8 by itself.

    python harness/w8.py run --candidates rust zig --strategies incremental full-rebuild --repeat 10 --output DIR
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.deltas import COMPOSITIONS, FRACTIONS, delta_queries, make_delta, standard_queries, total_rows
from harness.perf_common import (ensure_corpus, executable, failures_between, file_digest, machine_snapshot, median, preflight,
                                 rotated_orders, source_digest, timestamp)
from harness.s0 import BINDING, Client, context_of
from oracle.compact import CompactOracle
from oracle.session_model import SessionModel

SEED_ORDER = 20260929
MAX_LINE = 1 << 31           # deltas are sent as single lines; nothing is chunked in this workload


def prepare_case(base_model, fx, composition, fraction, seed, directory, tag):
    """Oracle side of one case: delta, expected digest, expected Q results, S + delta fixture."""
    batch = make_delta(fx, composition, fraction, seed)
    queries = standard_queries(fx) + delta_queries(batch, fx)
    model = base_model.copy()
    model.mutate(batch)                                     # raises if the delta breaks an ADR-0008 rule
    expected_digest = model.state_digest()[0]
    fixture_path = ROOT / 'corpus/synthetic' / f'W8-{tag}-{composition}-{fraction}.json'
    fixture_path.write_text(json.dumps(model.to_fixture(), separators=(',', ':')))
    del model
    oracle = CompactOracle(fixture_path)
    refs = {}
    for q in queries:
        target = directory / f'{q["query_id"]}.json'
        oracle.execute_to(q, target)
        refs[q['query_id']] = target.read_bytes()
    del oracle
    bad = batch + [{'op': 'ADD_RELATION', 'subject': 10**9, 'relation': 'CALLS', 'object': fx['entities'][0]['id']}]
    return {'batch': batch, 'bad_batch': bad, 'queries': queries, 'refs': refs, 'digest': expected_digest, 'fixture': fixture_path}


def measure(name, strategy, exe, corpus, ctx, case, want_base, repeat, full_check, limits):
    before = machine_snapshot()
    c = Client(exe, Path(corpus).parent / 'w8-repo', max_line_bytes=MAX_LINE, strategy=strategy)
    opened = c.open_fixture(corpus)
    stats0 = c.call('stats')
    m = c.call('mutate', batch=case['batch'])
    stats1 = c.call('stats')
    first = c.call('query', query=case['queries'][0])       # time to first query after apply
    after = machine_snapshot()
    record = {'candidate': name, 'strategy': (opened.message.get('strategy') or {}).get('mutation'), 'repeat': repeat, 'kind': 'apply',
              'open_service_ns': opened.service_ns, 'mutate_service_ns': m.service_ns, 'mutate_roundtrip_ns': m.ns,
              'mutate_ok': bool(m.ok), 'generation': m.generation, 'first_query_service_ns': first.service_ns,
              'first_query_roundtrip_ns': first.ns, 'operations': len(case['batch']),
              'heap_before': {k: (stats0.message.get('stats') or {}).get(k) for k in ('live_heap_bytes', 'peak_heap_bytes', 'derived_rebuilds_total')},
              'heap_after': {k: (stats1.message.get('stats') or {}).get(k) for k in ('live_heap_bytes', 'peak_heap_bytes', 'allocations_total', 'derived_rebuilds_total')},
              'boundary': (stats1.message.get('stats') or {}).get('boundary'),
              'conditions_before': before, 'conditions_after': after, 'condition_failures': failures_between(before, after, *limits)}
    digest = c.call('state_digest')                          # checkpoint: outside the timed intervals
    record['state_digest_ms'] = digest.message.get('state_digest_ms')
    ok = m.ok and digest.message.get('state_digest') == case['digest']
    if full_check:
        for q in case['queries']:
            resp = c.call('query', query=q)
            if not (resp.ok and resp.result_bytes() == case['refs'][q['query_id']]):
                ok = False
                record.setdefault('query_mismatches', []).append(q['query_id'])
    record['peak_rss_bytes'] = c.finish()
    record['conformant'] = bool(ok)
    return record


def measure_rejection(name, strategy, exe, corpus, want_base, case):
    c = Client(exe, Path(corpus).parent / 'w8-repo', max_line_bytes=MAX_LINE, strategy=strategy)
    c.open_fixture(corpus)
    bad = c.call('mutate', batch=case['bad_batch'])
    digest = c.call('state_digest')
    ok = (not bad.ok) and bad.code == 'INVALID_INPUT' and digest.message.get('state_digest') == want_base and digest.generation == 0
    c.finish()
    return bool(ok)


def measure_cold(name, exe, case, repeat):
    c = Client(exe, Path(case['fixture']).parent / 'w8-repo', max_line_bytes=MAX_LINE)
    t0 = time.perf_counter_ns()
    opened = c.open_fixture(case['fixture'])
    wall = time.perf_counter_ns() - t0
    stats = c.call('stats')
    digest = c.call('state_digest')
    rss = c.finish()
    return {'candidate': name, 'repeat': repeat, 'kind': 'cold', 'open_service_ns': opened.service_ns, 'open_roundtrip_ns': wall,
            'conformant': bool(opened.ok and digest.message.get('state_digest') == case['digest']), 'peak_rss_bytes': rss,
            'heap': {k: (stats.message.get('stats') or {}).get(k) for k in ('live_heap_bytes', 'peak_heap_bytes', 'derived_rebuilds_total')}}


def summarize(records, cases):
    cells = {}
    for r in records:
        key = (r['composition'], r['fraction'])
        cells.setdefault(key, []).append(r)
    out = {}
    for (comp, frac), rs in sorted(cells.items()):
        cell = {}
        for cand in sorted({r['candidate'] for r in rs}):
            cold = [r for r in rs if r['candidate'] == cand and r['kind'] == 'cold']
            cell[cand] = {'cold_build_open_service_ms': (median(r['open_service_ns'] for r in cold) or 0) / 1e6 if cold else None}
            for strategy in sorted({r['strategy'] for r in rs if r['kind'] == 'apply' and r['candidate'] == cand}):
                ap = [r for r in rs if r['candidate'] == cand and r['kind'] == 'apply' and r['strategy'] == strategy]
                mut = median(r['mutate_service_ns'] for r in ap)
                cell[cand][strategy] = {'n': len(ap), 'operations': ap[0]['operations'], 'mutate_service_ms': mut / 1e6,
                                        'mutate_roundtrip_ms': median(r['mutate_roundtrip_ns'] for r in ap) / 1e6,
                                        'first_query_service_ms': median(r['first_query_service_ns'] for r in ap) / 1e6,
                                        'derived_rebuilds_total_after': median((r['heap_after'] or {}).get('derived_rebuilds_total') for r in ap),
                                        'derived_rebuilds_total_before': median((r['heap_before'] or {}).get('derived_rebuilds_total') for r in ap),
                                        'live_heap_after_mb': (median((r['heap_after'] or {}).get('live_heap_bytes') for r in ap) or 0) / 1e6,
                                        'peak_rss_mb': (median(r['peak_rss_bytes'] for r in ap) or 0) / 1e6,
                                        'state_digest_ms': median(r['state_digest_ms'] for r in ap)}
                if cold:
                    cell[cand][strategy]['mutate_over_cold_open'] = mut / median(r['open_service_ns'] for r in cold)
        out[f'{comp}@{frac}'] = cell
    return out


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env, machine, failures, classification = preflight(args.exploratory, args.max_load_per_cpu, args.min_memory_gib)
    manifest = {'schema': 'csl.eval.w8/v0.1', 'workload': 'W8.S1', 'started_utc': timestamp(), 'classification': classification,
                'environment': env, 'preflight_machine': machine, 'preflight_failures': failures, 'repeat': args.repeat,
                'candidates': args.candidates, 'strategies': args.strategies, 'compositions': args.compositions,
                'fractions': args.fractions, 'source_digest': source_digest(), 'protocol': BINDING,
                'note': 'timed intervals cover mutate/open only; oracle work, state_digest and query comparison are excluded'}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if failures and not args.exploratory:
        print(json.dumps({'state': 'BLOCKED', 'reasons': failures}))
        return 2
    corpus = ensure_corpus('S')
    (corpus.parent / 'w8-repo').mkdir(exist_ok=True)
    manifest['corpus'] = {'path': str(corpus.relative_to(ROOT)), 'digest': file_digest(corpus)}
    fx = json.loads(corpus.read_text())
    base = SessionModel.from_fixture(fx)
    want_base = base.state_digest()[0]
    ctx = context_of(fx)
    binaries = {c: executable(c) for c in args.candidates}
    manifest['artifacts'] = {c: {'path': str(b), 'digest': file_digest(b)} for c, b in binaries.items()}
    cases, tag = {}, output.name
    for comp in args.compositions:
        for frac in args.fractions:
            directory = output / 'references' / f'{comp}-{frac}'
            directory.mkdir(parents=True)
            cases[(comp, frac)] = prepare_case(base, fx, comp, frac, args.seed, directory, tag)
            (output / 'references' / f'{comp}-{frac}' / 'delta.json').write_text(json.dumps(cases[(comp, frac)]['batch']))
            print(json.dumps({'oracle_case': f'{comp}@{frac}', 'operations': len(cases[(comp, frac)]['batch'])}), flush=True)
    del base
    manifest['cases'] = {f'{c}@{f}': {'operations': len(v['batch']), 'expected_state_digest': v['digest']} for (c, f), v in cases.items()}
    manifest['base_state_digest'] = want_base
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    limits = (args.max_load_per_cpu, args.min_memory_gib)
    combos = [(c, s) for c in args.candidates for s in args.strategies]
    records, rejections = [], []
    with (output / 'records.jsonl').open('w') as stream:
        for repeat, order in enumerate(rotated_orders(combos, args.repeat, SEED_ORDER)):
            for (comp, frac), case in cases.items():
                for cand, strategy in order:
                    r = measure(cand, strategy, binaries[cand], corpus, ctx, case, want_base, repeat, repeat == 0, limits)
                    r.update(composition=comp, fraction=frac, requested_strategy=strategy)
                    if not r['conformant']:
                        raise SystemExit(f'CONFORMANCE FAILURE {cand}/{strategy} {comp}@{frac} repeat {repeat}: {r.get("query_mismatches")}')
                    if r['condition_failures'] and classification == 'controlled':
                        raise SystemExit(f'machine conditions violated: {r["condition_failures"]}')
                    stream.write(json.dumps(r) + '\n')
                    stream.flush()
                    records.append(r)
                if repeat < args.cold_repeat:
                    for cand in args.candidates:
                        r = measure_cold(cand, binaries[cand], case, repeat)
                        r.update(composition=comp, fraction=frac)
                        if not r['conformant']:
                            raise SystemExit(f'CONFORMANCE FAILURE cold build {cand} {comp}@{frac}')
                        stream.write(json.dumps(r) + '\n')
                        records.append(r)
            print(json.dumps({'repeat': repeat + 1, 'of': args.repeat}), flush=True)
    for (comp, frac), case in cases.items():
        for cand, strategy in combos:
            ok = measure_rejection(cand, strategy, binaries[cand], corpus, want_base, case)
            rejections.append({'candidate': cand, 'strategy': strategy, 'composition': comp, 'fraction': frac, 'rejection_equivalence': ok})
            if not ok:
                raise SystemExit(f'REJECTION EQUIVALENCE FAILURE {cand}/{strategy} {comp}@{frac}')
    (output / 'rejections.json').write_text(json.dumps(rejections, indent=2) + '\n')
    summary = {'state': 'PASS', 'classification': classification, 'records': len(records), 'rejection_checks': len(rejections),
               'condition_failure_samples': sum(bool(r.get('condition_failures')) for r in records), 'cells': summarize(records, cases)}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    manifest.update(state='PASS', finished_utc=timestamp(), records=len(records))
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for case in cases.values():
        case['fixture'].unlink(missing_ok=True)
    print(json.dumps({'state': 'PASS', 'classification': classification, 'records': len(records), 'output': str(output)}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    r = commands.add_parser('run')
    r.add_argument('--output', required=True)
    r.add_argument('--candidates', nargs='+', choices=('rust', 'zig', 'hybrid'), default=['rust', 'zig'])
    r.add_argument('--strategies', nargs='+', choices=('incremental', 'full-rebuild'), default=['incremental', 'full-rebuild'])
    r.add_argument('--compositions', nargs='+', choices=COMPOSITIONS, default=list(COMPOSITIONS))
    r.add_argument('--fractions', nargs='+', type=float, default=list(FRACTIONS))
    r.add_argument('--repeat', type=int, default=10)
    r.add_argument('--cold-repeat', type=int, default=5)
    r.add_argument('--seed', type=int, default=20260929)
    r.add_argument('--exploratory', action='store_true')
    r.add_argument('--max-load-per-cpu', type=float, default=0.75)
    r.add_argument('--min-memory-gib', type=float, default=3)
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == '__main__':
    main()
