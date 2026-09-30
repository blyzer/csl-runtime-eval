#!/usr/bin/env python3
"""W5.S1 cold restore (preregistered): process start -> open(empty) -> restore(snapshot_id) -> first valid query.

Per cold restore the host measures, independently: process start (spawn to open response minus the
candidate's open service time), open, restore, first query, and total time-to-first-query; then,
outside the timed phases, `stats` (persistent heap), the `state_digest` checkpoint (its cost is
reported on its own) and the correctness gate (digest and first-query bytes equal the oracle's).
Snapshot write time and size come from a separate preparation step. Conformance is a hard gate:
a sample that fails it aborts the run.

    python harness/w5.py run --candidates rust zig hybrid --repeat 20 --output DIR [--exploratory]
"""
import argparse
import json
import os
import re
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.perf_common import (bootstrap_ratio_ci, ensure_corpus, environment, executable, failures_between, file_digest,
                                 machine_snapshot, median, preflight, rotated_orders, source_digest, timestamp)
from harness.s0 import BINDING, Client, context_of
from oracle.compact import CompactOracle
from oracle.oracle import canon_bytes
from oracle.session_model import SessionModel

QUERIES = {
    'lookup-first': {'schema': 'csl.eval.query/v0.1', 'query_id': 'lookup-first', 'op': 'RESOLVE', 'entity_id': 1},
    'depth-4': {'schema': 'csl.eval.query/v0.1', 'query_id': 'depth-4', 'op': 'TRAVERSE', 'input': {'op': 'RESOLVE', 'entity_id': 1},
                'max_depth': 4, 'max_paths': 100000},
}
PHASES = ('process_start_ns', 'open_service_ns', 'restore_service_ns', 'first_query_service_ns', 'restore_roundtrip_ns',
          'first_query_roundtrip_ns', 'ttfq_ns')


def prepare_snapshot(name, exe, repository, corpus, want, ctx, repeats, max_line):
    """Open the fixture, checkpoint, snapshot; repeated to characterize write time. Untimed for TTFQ."""
    rows, snapshot_id = [], None
    for k in range(repeats):
        c = Client(exe, repository, max_line_bytes=max_line)
        opened = c.open_fixture(corpus)
        digest = c.call('state_digest')                    # excluded from every workload timing
        if not (opened.ok and digest.message.get('state_digest') == want):
            c.close()
            raise SystemExit(f'{name}: preparation failed conformance: {opened.message} {digest.message}')
        snap = c.call('snapshot')
        snapshot_id = snap.message['snapshot_id']
        size = sum(f.stat().st_size for f in Path(repository).iterdir() if snapshot_id in f.name)
        stats = c.call('stats')                                  # outside snapshot service timing
        rss = c.finish()
        phase_records = parse_phase_records(c.diagnostics())
        rows.append({'candidate': name, 'repeat': k, 'fixture_open_service_ns': opened.service_ns, 'state_digest_service_ns': digest.service_ns,
                     'state_digest_ms': digest.message.get('state_digest_ms'), 'bytes_processed': digest.message.get('bytes_processed'),
                     'snapshot_roundtrip_ns': snap.ns, 'snapshot_service_ns': snap.service_ns, 'snapshot_bytes': size,
                     'snapshot_id': snapshot_id, 'peak_rss_bytes': rss, 'strategy': opened.message.get('strategy'),
                     'heap': {k: stats.message['stats'].get(k) for k in ('live_heap_bytes', 'peak_heap_bytes', 'allocations_total', 'derived_rebuilds_total')} if stats.ok else None,
                     'persistence_diagnostics': phase_records})
    return snapshot_id, rows


def cold_restore(name, exe, repository, snapshot_id, ctx, cell, query, expected, want, max_line, limits):
    before = machine_snapshot()
    t0 = time.perf_counter_ns()
    c = Client(exe, repository, max_line_bytes=max_line)
    opened = c.open_empty(ctx)
    t_open = time.perf_counter_ns()
    restored = c.call('restore', snapshot_id=snapshot_id)
    first = c.call('query', query=query)
    t_query = time.perf_counter_ns()
    stats = c.call('stats')                                 # outside the timed phases
    digest = c.call('state_digest')                         # checkpoint, timed on its own
    after = machine_snapshot()
    ok = (opened.ok and restored.ok and first.ok and first.result_bytes() == expected
          and digest.message.get('state_digest') == want and restored.generation == 1)
    boundary = stats.message.get('stats', {}).get('boundary') if stats.ok else None
    rss = c.finish()
    phase_records = parse_phase_records(c.diagnostics())
    spawn_to_open = t_open - t0
    record = {'candidate': name, 'cell': cell, 'query': query['query_id'], 'conformant': bool(ok),
              'spawn_to_open_response_ns': spawn_to_open, 'process_start_ns': spawn_to_open - opened.service_ns,
              'open_service_ns': opened.service_ns, 'open_roundtrip_ns': opened.ns,
              'restore_service_ns': restored.service_ns, 'restore_roundtrip_ns': restored.ns,
              'first_query_service_ns': first.service_ns, 'first_query_roundtrip_ns': first.ns,
              'first_query_chunked': first.payload is not None, 'ttfq_ns': t_query - t0,
              'state_digest_ms': digest.message.get('state_digest_ms'), 'state_digest_roundtrip_ns': digest.ns,
              'peak_rss_bytes': rss,
              'heap': {k: stats.message['stats'].get(k) for k in ('live_heap_bytes', 'peak_heap_bytes', 'allocations_total', 'derived_rebuilds_total')} if stats.ok else None,
              'boundary': boundary, 'conditions_before': before, 'conditions_after': after,
              'condition_failures': failures_between(before, after, *limits),
              'persistence_diagnostics': phase_records}
    return record


def parse_phase_records(lines):
    """Normalize Rust JSON and Zig key/value records without changing candidate responses."""
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = {}
            for key, value in re.findall(r'([a-z_]+)=([0-9]+)', line):
                record[key] = int(value)
            op = re.search(r'operation=(snapshot|restore)', line)
            if op:
                record['operation'] = op.group(1)
        records.append(record)
    return records


def summarize(records, prep):
    out = {'cells': {}, 'snapshot': {}}
    for cand in sorted({r['candidate'] for r in records}):
        for cell in sorted({r['cell'] for r in records}):
            rs = [r for r in records if r['candidate'] == cand and r['cell'] == cell]
            if not rs:
                continue
            out['cells'][f'{cand}/{cell}'] = {'n': len(rs), **{k: median(r[k] for r in rs) for k in PHASES + ('peak_rss_bytes',)},
                                              'live_heap_bytes': median((r['heap'] or {}).get('live_heap_bytes') for r in rs),
                                              'peak_heap_bytes': median((r['heap'] or {}).get('peak_heap_bytes') for r in rs),
                                              'state_digest_ms': median(r['state_digest_ms'] for r in rs)}
    for cand in sorted({p['candidate'] for p in prep}):
        ps = [p for p in prep if p['candidate'] == cand]
        out['snapshot'][cand] = {'write_service_ns': median(p['snapshot_service_ns'] for p in ps),
                                 'write_roundtrip_ns': median(p['snapshot_roundtrip_ns'] for p in ps),
                                 'bytes': ps[-1]['snapshot_bytes'], 'fixture_open_service_ns': median(p['fixture_open_service_ns'] for p in ps),
                                 'state_digest_ms': median(p['state_digest_ms'] for p in ps)}
    # paired ratios per repeat: candidate / rust (ttfq), for the record only; materiality is decided in a separate analysis
    pairs = {}
    for cell in sorted({r['cell'] for r in records}):
        by = {}
        for r in records:
            if r['cell'] == cell:
                by.setdefault(r['candidate'], {})[r['repeat']] = r['ttfq_ns']
        for cand in by:
            for other in by:
                if cand < other:
                    common = sorted(set(by[cand]) & set(by[other]))
                    ci = bootstrap_ratio_ci([by[cand][i] for i in common], [by[other][i] for i in common])
                    if ci:
                        pairs[f'{cell}: ttfq {cand}/{other}'] = ci
    out['paired_ttfq_ratio'] = pairs
    return out


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env, machine, failures, classification = preflight(args.exploratory, args.max_load_per_cpu, args.min_memory_gib)
    manifest = {'schema': 'csl.eval.w5/v0.1', 'workload': 'W5.S1', 'started_utc': timestamp(), 'classification': classification,
                'environment': env, 'preflight_machine': machine, 'preflight_failures': failures, 'repeat': args.repeat,
                'candidates': args.candidates, 'source_digest': source_digest(), 'protocol': BINDING, 'max_line_bytes': args.max_line_bytes,
                'artifact_source': os.environ.get('CSL_W5_ARTIFACT_SOURCE', 'workspace build'),
                'notes': 'warm OS page cache (snapshots were just written); cold-cache is not attempted; state_digest excluded from all phases'}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if failures and not args.exploratory:
        print(json.dumps({'state': 'BLOCKED', 'reasons': failures}))
        return 2
    corpus = ensure_corpus('S')
    manifest['corpus'] = {'path': str(corpus.relative_to(ROOT)), 'digest': file_digest(corpus)}
    fx = json.loads(corpus.read_text())
    model = SessionModel.from_fixture(fx)
    want, _ = model.state_digest()
    ctx = context_of(fx)
    del model, fx
    oracle = CompactOracle(corpus)
    expected = {}
    for cell, q in QUERIES.items():
        target = output / f'reference-{cell}.json'
        oracle.execute_to(q, target)
        expected[cell] = target.read_bytes()
    del oracle
    binaries = {c: executable(c) for c in args.candidates}
    manifest['artifacts'] = {c: {'path': str(b), 'digest': file_digest(b)} for c, b in binaries.items()}
    manifest['expected_state_digest'] = want
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    repos, sids, prep = {}, {}, []
    for c in args.candidates:
        repos[c] = output / 'repos' / c
        repos[c].mkdir(parents=True)
        sids[c], rows = prepare_snapshot(c, binaries[c], repos[c], corpus, want, ctx, args.prep_repeat, args.max_line_bytes)
        prep.extend(rows)
    rss_values = {p['peak_rss_bytes'] for p in prep}
    if len(args.candidates) > 1 and len(rss_values) == 1:
        raise SystemExit(f'peak RSS is identical across candidates ({rss_values}): the RSS measurement is inheriting the harness')
    (output / 'prep.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in prep))
    records = []
    limits = (args.max_load_per_cpu, args.min_memory_gib)
    with (output / 'records.jsonl').open('w') as stream:
        for repeat, order in enumerate(rotated_orders(args.candidates, args.repeat, SEED_ORDER)):
            for cell, q in QUERIES.items():
                for c in order:
                    if repeat == 0:                         # one unmeasured warm-up cold restore per candidate/cell
                        warm = cold_restore(c, binaries[c], repos[c], sids[c], ctx, cell, q, expected[cell], want, args.max_line_bytes, limits)
                        if not warm['conformant']:
                            raise SystemExit(f'{c}: warm-up cold restore failed conformance')
                    r = cold_restore(c, binaries[c], repos[c], sids[c], ctx, cell, q, expected[cell], want, args.max_line_bytes, limits)
                    r['repeat'] = repeat
                    if not r['conformant']:
                        raise SystemExit(f'{c}/{cell}: conformance failure at repeat {repeat}')
                    if r['condition_failures'] and classification == 'controlled':
                        raise SystemExit(f'{c}/{cell}: machine conditions violated: {r["condition_failures"]}')
                    stream.write(json.dumps(r) + '\n')
                    stream.flush()
                    records.append(r)
            print(json.dumps({'repeat': repeat + 1, 'of': args.repeat}), flush=True)
    summary = summarize(records, prep)
    summary.update(state='PASS', classification=classification, records=len(records),
                   condition_failure_samples=sum(bool(r['condition_failures']) for r in records))
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    manifest.update(state='PASS', finished_utc=timestamp(), records=len(records))
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'state': 'PASS', 'classification': classification, 'records': len(records), 'output': str(output)}))
    return 0


SEED_ORDER = 20260929


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    r = commands.add_parser('run')
    r.add_argument('--output', required=True)
    r.add_argument('--candidates', nargs='+', choices=('rust', 'zig', 'hybrid'), default=['rust', 'zig'])
    r.add_argument('--repeat', type=int, default=20)
    r.add_argument('--prep-repeat', type=int, default=5)
    r.add_argument('--max-line-bytes', type=int, default=1 << 20)
    r.add_argument('--exploratory', action='store_true')
    r.add_argument('--max-load-per-cpu', type=float, default=0.75)
    r.add_argument('--min-memory-gib', type=float, default=3)
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == '__main__':
    main()
