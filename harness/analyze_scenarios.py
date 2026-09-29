#!/usr/bin/env python3
"""Summarize Gate #1 scenario campaigns (records.jsonl, memory.jsonl, manifest.json).

Read-only. Prints, per scenario/variant and query, medians and ratios, and always
states how many records were conformant and how many carried machine-condition
failures, so a reader cannot mistake exploratory data for controlled evidence.

    python harness/analyze_scenarios.py DIR [DIR ...]      # campaign output directories
"""
import argparse
import collections
import json
from pathlib import Path
import statistics

CANDS = ('rust', 'zig', 'hybrid')


def load_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def median(values):
    values = list(values)
    return statistics.median(values) if values else None


def ms(ns):
    return None if ns is None else round(ns / 1e6, 1)


def ratio(a, b):
    return None if a is None or b in (None, 0) else round(a / b, 2)


def analyze(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    records = load_jsonl(directory / 'records.jsonl')
    memory = load_jsonl(directory / 'memory.jsonl')
    out = {'dir': str(directory), 'scenario': manifest.get('scenario'), 'variant': manifest.get('variant') or (records[0].get('variant') if records else None),
           'state': manifest['state'], 'classification': manifest['classification'],
           'records': len(records), 'conformant': sum(bool(r['conformance']) for r in records),
           'condition_failure_samples': sum(bool(r['condition_failures']) for r in records),
           'sdk_workaround': sorted({r['sdk_workaround'] for r in records}),
           'candidates_not_run': manifest.get('candidates_not_run', {}), 'corpus': manifest.get('corpus'),
           'queries': []}
    by = collections.defaultdict(list)
    for r in records:
        by[(r['query'], r['candidate'])].append(r)
    mem = {(m['query'], m['candidate']): m for m in memory}
    for query in sorted({q for q, _ in by}, key=lambda q: [k for k, _ in by].index(q)):
        row = {'query': query}
        for c in CANDS:
            rs = by.get((query, c))
            if not rs:
                continue
            entry = {'n': len(rs), 'elapsed_ms': ms(median(r['elapsed_ns'] for r in rs)),
                     'rss_mb': round(median(r['rss_peak_bytes'] for r in rs) / 1e6),
                     'results_bytes_digest': rs[0]['result_digest'][:19]}
            if all('phases_ns' in r for r in rs):
                entry['phases_ms'] = {k: ms(median(r['phases_ns'][k] for r in rs)) for k in ('load', 'index', 'query', 'result')}
            if all('phase_subdetail_ns' in r for r in rs):
                entry['sub_ms'] = {k: ms(median(r['phase_subdetail_ns'][k] for r in rs)) for k in ('read', 'parse', 'entities', 'adjacency', 'sort')}
                entry['detail_ms'] = {k: ms(median(r['phase_detail_ns'][k] for r in rs)) for k in ('materialize', 'encode')}
            lookups = sorted(ns for r in rs for ns in r.get('name_index', {}).get('lookup_ns', ()))
            if lookups:
                pick = lambda q: lookups[min(len(lookups) - 1, int(q * len(lookups)))]      # noqa: E731
                entry['w4'] = {'lookups': len(lookups), 'p50_ns': pick(.5), 'p95_ns': pick(.95), 'p99_ns': pick(.99),
                               'intern_ms': ms(median(r['name_index']['intern_ns'] for r in rs)),
                               'unique_strings': rs[0]['name_index']['unique_strings']}
            if (query, c) in mem and mem[(query, c)].get('heap'):
                h = mem[(query, c)]['heap']
                total = (manifest['corpus']['entities'] + manifest['corpus']['edges'] + manifest['corpus']['evidence'])
                entry['heap'] = {'live_after_index_mb': round(h['live_after_index'] / 1e6, 1), 'peak_live_mb': round(h['peak_live'] / 1e6, 1),
                                 'retained_mb': None if h['retained'] is None else round(h['retained'] / 1e6, 1),
                                 'allocations': h['allocations'],
                                 'bytes_per_entity': round(h['live_after_index'] / manifest['corpus']['entities'], 1),
                                 'bytes_per_record': round(h['live_after_index'] / total, 1)}
            row[c] = entry
        if 'rust' in row and 'zig' in row:
            row['zig_over_rust'] = ratio(row['zig']['elapsed_ms'], row['rust']['elapsed_ms'])
            if 'phases_ms' in row['rust'] and 'phases_ms' in row['zig']:
                row['phase_zig_over_rust'] = {k: ratio(row['zig']['phases_ms'][k], row['rust']['phases_ms'][k]) for k in row['rust']['phases_ms']}
        if 'zig' in row and 'hybrid' in row:
            row['hybrid_over_zig'] = ratio(row['hybrid']['elapsed_ms'], row['zig']['elapsed_ms'])
            row['hybrid_rss_over_zig'] = ratio(row['hybrid']['rss_mb'], row['zig']['rss_mb'])
        out['queries'].append(row)
    return out


def render(result):
    lines = [f"## {result['scenario']} / {result['variant']}  ({result['classification']}, {result['state']})",
             f"records {result['records']}, conformant {result['conformant']}, samples with condition failures "
             f"{result['condition_failure_samples']}, sdk_workaround {result['sdk_workaround']}"]
    if result['candidates_not_run']:
        lines.append(f"not run: {result['candidates_not_run']}")
    lines.append('| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |')
    lines.append('|---|---|---|---|---|---|---|')
    for q in result['queries']:
        cell = lambda c, k='elapsed_ms': q.get(c, {}).get(k, '-')                       # noqa: E731
        rss = '/'.join(str(cell(c, 'rss_mb')) for c in CANDS)
        lines.append(f"| {q['query']} | {cell('rust')} | {cell('zig')} | {cell('hybrid')} | {q.get('zig_over_rust', '-')} | {q.get('hybrid_over_zig', '-')} | {rss} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dirs', nargs='+')
    parser.add_argument('--json', help='write the full analysis here')
    args = parser.parse_args()
    results = [analyze(d) for d in args.dirs]
    for result in results:
        print(render(result), end='\n\n')
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
