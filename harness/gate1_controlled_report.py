#!/usr/bin/env python3
"""Validate and summarize the controlled Gate #1 W5 diagnostic and W11/R4 bundle."""
import argparse
import json
from pathlib import Path
from harness.perf_common import bootstrap_ratio_ci

def read_json(path):
    return json.loads(Path(path).read_text())

def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]

def median(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None

def fmt(value):
    return 'n/a' if value is None else f'{value:,.0f}'

def run(root, output):
    root = Path(root)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    w5 = root / 'w5-phase'
    required = [w5 / arm / name for arm in ('phases', 'overhead-control') for name in ('prep.jsonl', 'records.jsonl')]
    if not all(path.is_file() for path in required):
        missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
        Path(output).write_text('# Controlled Gate #1 evidence\n\nW5 phase report incomplete; required evidence files were not produced:\n\n' + ''.join(f'- `{path}`\n' for path in missing))
        return
    phase_prep = jsonl(w5 / 'phases' / 'prep.jsonl')
    phase_restore = jsonl(w5 / 'phases' / 'records.jsonl')
    control_prep = jsonl(w5 / 'overhead-control' / 'prep.jsonl')
    control_restore = jsonl(w5 / 'overhead-control' / 'records.jsonl')
    expected = {'rust', 'zig'}
    seen = {row['candidate'] for row in phase_prep + phase_restore}
    if seen != expected:
        Path(output).write_text('# Controlled Gate #1 evidence\n\nW5 report incomplete; candidate coverage was ' + ', '.join(sorted(seen)) + '. See uploaded partial JSONL and runner logs.\n')
        return
    incomplete = []
    for candidate in expected:
        p = [d for row in phase_prep if row['candidate'] == candidate for d in row.get('persistence_diagnostics', []) if d.get('operation') == 'snapshot']
        r = [d for row in phase_restore if row['candidate'] == candidate for d in row.get('persistence_diagnostics', []) if d.get('operation') == 'restore']
        if len(p) < 30 or len(r) < 30:
            incomplete.append(f'- `{candidate}`: snapshot records={len(p)}; restore records={len(r)}.')
    if incomplete:
        Path(output).write_text('# Controlled Gate #1 evidence\n\nW5 diagnostic run incomplete:\n\n' + '\n'.join(incomplete) + '\n\nSee uploaded partial JSONL and runner logs.\n')
        return

    lines = ['# Controlled Gate #1 evidence', '', 'All W5 values below are medians from the paired Linux ARM64 run. Times are nanoseconds; local macOS observations are excluded.', '',
             '## W5 phase decomposition', '', '| Candidate | Operation | Phase | Median ns |', '|---|---|---|---:|']
    for op, rows in (('snapshot', phase_prep), ('restore', phase_restore)):
        for candidate in sorted(expected):
            samples = [d for row in rows if row['candidate'] == candidate for d in row.get('persistence_diagnostics', []) if d.get('operation') == op]
            keys = sorted({k for sample in samples for k in sample.get('phases_ns', sample).keys() if k.endswith('_ns') or k in ('encode', 'integrity_checksum', 'snapshot_id_generation', 'physical_write', 'physical_read', 'parse', 'logical_table_construction_aggregation', 'logical_state_to_fixture_conversion', 'derived_store_construction', 'total_service')})
            for key in keys:
                val = median((sample.get('phases_ns', sample)).get(key) for sample in samples)
                lines.append(f'| {candidate} | {op} | {key} | {fmt(val)} |')
    lines += ['', 'Snapshot bytes and bytes processed are retained per sample in `prep.jsonl` / `records.jsonl`. RSS and heap counters are retained alongside W5 samples; they are process/session counters, not phase-attributed allocation counts.', '', '## Total W5 service time', '', '| Candidate | Operation | Diagnostics off median ns | Diagnostics on median ns |', '|---|---|---:|---:|']
    for candidate in sorted(expected):
        for op, offrows, onrows, field in (
            ('snapshot', control_prep, phase_prep, 'snapshot_service_ns'),
            ('restore', control_restore, phase_restore, 'restore_service_ns'),
        ):
            lines.append(f'| {candidate} | {op} | {fmt(median(row[field] for row in offrows if row["candidate"] == candidate))} | {fmt(median(row[field] for row in onrows if row["candidate"] == candidate))} |')
    lines += ['', '## Instrumentation overhead bound', '', '| Candidate | Metric | Diagnostics off | Diagnostics on | Relative delta, paired median ratio [95% CI] |', '|---|---|---:|---:|---:|']
    for candidate in sorted(expected):
        for label, offrows, onrows, metric, key_fn in (
            ('snapshot service', control_prep, phase_prep, 'snapshot_service_ns', lambda r: (r['repeat'],)),
            ('restore service', control_restore, phase_restore, 'restore_service_ns', lambda r: (r['cell'], r['query'], r['repeat'])),
        ):
            off = {key_fn(row): row[metric] for row in offrows if row['candidate'] == candidate}
            on = {key_fn(row): row[metric] for row in onrows if row['candidate'] == candidate}
            common = sorted(off.keys() & on.keys())
            ci = bootstrap_ratio_ci([on[k] for k in common], [off[k] for k in common]) if common else None
            off_med, on_med = median(off.values()), median(on.values())
            if ci:
                ratio = (ci['median'] - 1) * 100
                bound = f'{ratio:.2f}% [{(ci["ci95"][0]-1)*100:.2f}%; {(ci["ci95"][1]-1)*100:.2f}%]'
            else:
                bound = 'n/a'
            lines.append(f'| {candidate} | {label} (n={len(common)}) | {fmt(off_med)} | {fmt(on_med)} | {bound} |')
    lines += ['', '## W11/R4', '', 'Artifact hash and size files are under `w11/<candidate>/path-a` and `path-b`. `same-path-hash.diff` tests a clean rebuild at the same output root; `cross-path-hash.diff` compares independent roots. An empty diff means matching hashes. Build timing and peak RSS are in each root’s `time.txt`; tests, conformance, and S0 outputs are in this run directory.', '']
    for candidate in ('rust', 'zig', 'hybrid'):
        a = root / 'w11' / candidate / 'path-a'
        b = root / 'w11' / candidate / 'path-b'
        same = (root / 'w11' / candidate / 'same-path-hash.diff').exists() and not (root / 'w11' / candidate / 'same-path-hash.diff').read_text().strip()
        cross = (root / 'w11' / candidate / 'cross-path-hash.diff').exists() and not (root / 'w11' / candidate / 'cross-path-hash.diff').read_text().strip()
        lines.append(f'- **{candidate}:** same-path reproducible={same}; cross-path hashes equal={cross}; A={a / "artifact.sha256"}; B={b / "first-clean-build.sha256"}.')
    lines.append('')
    Path(output).write_text('\n'.join(lines))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.root, args.output)

if __name__ == '__main__':
    main()
