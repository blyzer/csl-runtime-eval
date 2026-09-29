#!/usr/bin/env python3
"""Evaluate the preregistered Hybrid framework on W5.S1 / W8.S1 evidence (read-only).

Thresholds, comparator and method are those of
results/preregistration/gate1-materiality-and-index-stability-20260929.md (Part B): per dimension and
workload cell, the median paired ratio `candidate / best_other` with a 95% paired bootstrap CI; 'better'
and 'worse' only when the whole CI lies beyond the threshold, otherwise 'equivalent'. Pareto dominance
uses those verdicts; there is no weighted score.

    python harness/gate1_decision.py --w5 DIR --w8 DIR [--w8 DIR ...] [--json OUT]
"""
import argparse
import collections
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.perf_common import bootstrap_ratio_ci

# dimension -> (ratio must be <= better, ratio must be >= worse)
THRESHOLDS = {'latency': (0.90, 1.10), 'memory': (0.85, 1.15), 'startup': (0.90, 1.10), 'artifact': (0.80, 1.25)}
BOUNDARY_TAX_WORSE = 0.05        # boundary time as a fraction of end-to-end latency, any Gate-required cell
OWNERSHIP_WORSE = 0.10           # boundary-attributable bytes copied / live heap
MIN_PAIRS = 30
CANDIDATES = ('rust', 'zig', 'hybrid')


def verdict(ci, better, worse):
    if ci is None:
        return 'insufficient'
    lo, hi = ci['ci95']
    if hi <= better:
        return 'better'
    if lo >= worse:
        return 'worse'
    return 'equivalent'


def load(path):
    return [json.loads(line) for line in (Path(path) / 'records.jsonl').read_text().splitlines() if line.strip()]


def cells_from_w5(directory):
    """(dimension, cell) -> candidate -> {repeat: value}."""
    out = collections.defaultdict(lambda: collections.defaultdict(dict))
    for r in load(directory):
        cell = f"W5.S1/{r['cell']}"
        c, i = r['candidate'], r['repeat']
        out[('latency', f'{cell} restore')][c][i] = r['restore_service_ns']
        out[('latency', f'{cell} first-query')][c][i] = r['first_query_service_ns']
        out[('latency', f'{cell} time-to-first-query')][c][i] = r['ttfq_ns']
        out[('startup', f'{cell} process-start')][c][i] = r['process_start_ns']
        out[('startup', f'{cell} cold time-to-first-query')][c][i] = r['ttfq_ns']
        out[('memory', f'{cell} peak-rss')][c][i] = r['peak_rss_bytes']
        heap = (r.get('heap') or {}).get('live_heap_bytes')
        if heap:
            out[('memory', f'{cell} live-heap')][c][i] = heap
    return out


def cells_from_w8(directory):
    out = collections.defaultdict(lambda: collections.defaultdict(dict))
    for r in load(directory):
        if r['kind'] != 'apply':
            continue
        cell = f"W8.S1/{r['composition']}@{r['fraction']}/{r['strategy']}"
        c, i = r['candidate'], r['repeat']
        out[('latency', f'{cell} mutate')][c][i] = r['mutate_service_ns']
        out[('memory', f'{cell} peak-rss')][c][i] = r['peak_rss_bytes']
        heap = (r.get('heap_after') or {}).get('live_heap_bytes')
        if heap:
            out[('memory', f'{cell} live-heap')][c][i] = heap
    return out


def compare(cells, subject='hybrid'):
    """For each cell: subject vs the best other candidate (lowest median), plus every pair for Pareto."""
    rows = []
    for (dim, cell), by in sorted(cells.items()):
        if subject not in by or len(by) < 2:
            continue
        others = {c: v for c, v in by.items() if c != subject}
        best = min(others, key=lambda c: statistics.median(others[c].values()))
        common = sorted(set(by[subject]) & set(others[best]))
        ci = bootstrap_ratio_ci([by[subject][i] for i in common], [others[best][i] for i in common]) if len(common) >= MIN_PAIRS else None
        better, worse = THRESHOLDS[dim]
        row = {'dimension': dim, 'cell': cell, 'best_other': best, 'pairs': len(common),
               'median_ratio': ci['median'] if ci else None, 'ci95': ci['ci95'] if ci else None, 'verdict': verdict(ci, better, worse)}
        if ci is None and len(common) >= 2:      # below the preregistered minimum: report, never judge
            small = bootstrap_ratio_ci([by[subject][i] for i in common], [others[best][i] for i in common])
            row.update(median_ratio=small['median'], ci95=small['ci95'], note=f'{len(common)} pairs < {MIN_PAIRS}: insufficient for a materiality claim')
        rows.append(row)
    return rows


def pairwise(cells, x, y):
    """Dimension verdicts of x against y (x vs y ratios), aggregated over cells."""
    per_dim = collections.defaultdict(list)
    for (dim, cell), by in cells.items():
        if x in by and y in by:
            common = sorted(set(by[x]) & set(by[y]))
            if len(common) < MIN_PAIRS:
                continue
            ci = bootstrap_ratio_ci([by[x][i] for i in common], [by[y][i] for i in common])
            better, worse = THRESHOLDS[dim]
            per_dim[dim].append(verdict(ci, better, worse))
    result = {}
    for dim, verdicts in per_dim.items():
        if 'better' in verdicts and 'worse' in verdicts:
            result[dim] = 'mixed'
        elif 'better' in verdicts:
            result[dim] = 'better'
        elif 'worse' in verdicts:
            result[dim] = 'worse'
        else:
            result[dim] = 'equivalent'
    return result


def dominates(verdicts_x_over_y):
    values = verdicts_x_over_y.values()
    return 'worse' not in values and 'mixed' not in values and 'better' in values


def pareto(cells, hybrid_costs):
    """Pareto relations among qualifying candidates. `hybrid_costs`: cost dimensions only Hybrid incurs."""
    table = {}
    for x in CANDIDATES:
        for y in CANDIDATES:
            if x == y:
                continue
            v = pairwise(cells, x, y)
            if 'hybrid' in (x, y):
                sign = 'worse' if x == 'hybrid' else 'better'
                for dim, bad in hybrid_costs.items():
                    if bad:
                        v[dim] = sign
            table[f'{x} vs {y}'] = {'verdicts': v, 'dominates': dominates(v)}
    return table


def boundary_costs(records, fraction_worse=BOUNDARY_TAX_WORSE, ownership_worse=OWNERSHIP_WORSE):
    """Hybrid-only cost dimensions from the `boundary` blocks the hybrid reports in stats."""
    taxes, copies = [], []
    for r in records:
        b = r.get('boundary')
        if not b or r.get('candidate') != 'hybrid':
            continue
        total = b['boundary_ns_total'] + b['kernel_ns_total'] + b.get('wrapper_ns_total', 0)
        if total:
            taxes.append(b['boundary_ns_total'] / total)
        heap = ((r.get('heap') or r.get('heap_after')) or {}).get('live_heap_bytes')
        if heap:
            copies.append(b['copy_bytes'] / heap)
    return {'boundary_tax_fraction_median': statistics.median(taxes) if taxes else None,
            'boundary_tax_worse': bool(taxes) and statistics.median(taxes) >= fraction_worse,
            'ownership_fraction_median': statistics.median(copies) if copies else None,
            'ownership_worse': bool(copies) and statistics.median(copies) >= ownership_worse,
            'toolchain_complexity_worse': True}      # a second toolchain and a non-empty boundary, by definition


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--w5')
    parser.add_argument('--w8', action='append', default=[])
    parser.add_argument('--json')
    args = parser.parse_args()
    cells = collections.defaultdict(lambda: collections.defaultdict(dict))
    records = []
    for name, loader in ((args.w5, cells_from_w5),) + tuple((d, cells_from_w8) for d in args.w8):
        if name:
            for key, by in loader(name).items():
                for c, values in by.items():
                    cells[key][c].update(values)
            records += load(name)
    costs = boundary_costs(records)
    out = {'hybrid_vs_best_other': compare(cells), 'hybrid_costs': costs,
           'pareto': pareto(cells, {'boundary_tax': costs['boundary_tax_worse'], 'ownership': costs['ownership_worse'],
                                    'complexity': costs['toolchain_complexity_worse']})}
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2) + '\n')


if __name__ == '__main__':
    main()
