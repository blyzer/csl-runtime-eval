#!/usr/bin/env python3
"""Analyze the preregistered index-stability experiment (read-only).

Protocol and classification rule: results/preregistration/gate1-materiality-and-index-stability-20260929.md
(Part A). Unit of analysis: per (query, repeat) pair, R = zig_index / rust_index.

    python harness/index_stability.py CAMPAIGN_DIR [--json OUT]
"""
import argparse
import collections
import json
from pathlib import Path
import random
import statistics

RESAMPLES = 10_000
SEED = 20260929
MATERIAL = 1.10
CONSISTENT = 0.90


def bootstrap_ci(values, seed=SEED, resamples=RESAMPLES):
    rng = random.Random(seed)
    n = len(values)
    medians = sorted(statistics.median(values[rng.randrange(n)] for _ in range(n)) for _ in range(resamples))
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples) - 1]


def spread(values):
    values = sorted(values)
    q = statistics.quantiles(values, n=4)
    return {'median': statistics.median(values), 'iqr': q[2] - q[0],
            'cv': statistics.pstdev(values) / statistics.mean(values), 'min': values[0], 'max': values[-1]}


def analyze(directory):
    directory = Path(directory)
    rows = [json.loads(line) for line in (directory / 'records.jsonl').read_text().splitlines() if line.strip()]
    pairs = collections.defaultdict(dict)
    for r in rows:
        pairs[(r['query'], r['iteration'])][r['candidate']] = r
    pairs = {k: v for k, v in pairs.items() if {'rust', 'zig'} <= set(v)}
    if not pairs:
        raise ValueError('no rust/zig pairs')
    parts = ('index', 'entities', 'adjacency', 'sort')

    def value(r, part):
        return r['phases_ns']['index'] if part == 'index' else r['phase_subdetail_ns'][part]

    out = {'records': len(rows), 'pairs': len(pairs), 'conformant': sum(bool(r['conformance']) for r in rows),
           'condition_failure_samples': sum(bool(r['condition_failures']) for r in rows),
           'sdk_workaround': sorted({r['sdk_workaround'] for r in rows}), 'corpus_digest': rows[0]['corpus_digest'],
           'source_digest': rows[0]['source_digest'], 'bootstrap': {'resamples': RESAMPLES, 'seed': SEED}, 'parts': {}}
    for part in parts:
        ratios = [value(p['zig'], part) / value(p['rust'], part) for p in pairs.values() if value(p['rust'], part)]
        low, high = bootstrap_ci(ratios)
        out['parts'][part] = {
            'median_ratio_zig_over_rust': statistics.median(ratios), 'ci95': [low, high],
            'fraction_pairs_rust_faster': sum(r > 1 for r in ratios) / len(ratios),
            'fraction_pairs_zig_faster': sum(r < 1 for r in ratios) / len(ratios),
            'rust_ns': spread([value(p['rust'], part) for p in pairs.values()]),
            'zig_ns': spread([value(p['zig'], part) for p in pairs.values()])}
    repeats = sorted({k[1] for k in pairs})
    block = {c: [statistics.median(value(p[c], 'index') for k, p in pairs.items() if k[1] == it) for it in repeats] for c in ('rust', 'zig')}
    out['per_repeat_block_median_index_ns'] = {c: spread(v) for c, v in block.items()}
    main = out['parts']['index']
    if main['ci95'][0] >= MATERIAL and main['fraction_pairs_rust_faster'] >= CONSISTENT:
        verdict = 'Rust materially faster within this instance (cross-instance stability not established)'
    elif main['ci95'][1] <= 1 / MATERIAL and main['fraction_pairs_zig_faster'] >= CONSISTENT:
        verdict = 'Zig materially faster within this instance (cross-instance stability not established)'
    else:
        verdict = 'NO STABLE MATERIAL INDEX ADVANTAGE ESTABLISHED'
    out['classification'] = verdict
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--json')
    args = parser.parse_args()
    result = analyze(args.directory)
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
