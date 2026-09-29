#!/usr/bin/env python3
"""Paired pure-candidate campaigns with isolated oracle/verification workers."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.benchctl import (environment, executable, load, profile_metadata,
                              queries, save, source_digest, tool)
from harness.generate import generate, PRESETS
from oracle.oracle import PreparedOracle
from oracle.validation import VALIDATORS


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def command_output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def snapshot():
    record = {'utc': timestamp(), 'load_average': list(os.getloadavg()),
              'cpu_count': os.cpu_count(), 'available_memory_bytes': None,
              'swapout_pages': None, 'power': None, 'thermal': None}
    if sys.platform == 'darwin':
        memory = command_output(['vm_stat'])
        if memory:
            page = re.search(r'page size of (\d+)', memory)
            values = {key.strip(): int(value) for key, value in re.findall(r'^([^:\n]+):\s+(\d+)\.', memory, re.M)}
            if page and all(k in values for k in ('Pages free', 'Pages inactive', 'Pages speculative')):
                record['available_memory_bytes'] = int(page[1]) * sum(values[k] for k in ('Pages free', 'Pages inactive', 'Pages speculative'))
            record['swapout_pages'] = values.get('Swapouts')
        record['power'] = command_output(['pmset', '-g', 'batt'])
        record['thermal'] = command_output(['pmset', '-g', 'therm'])
    elif sys.platform.startswith('linux'):
        memory = Path('/proc/meminfo').read_text()
        match = re.search(r'^MemAvailable:\s+(\d+)', memory, re.M)
        if match:
            record['available_memory_bytes'] = int(match[1]) * 1024
        match = re.search(r'^pswpout\s+(\d+)', Path('/proc/vmstat').read_text(), re.M)
        if match:
            record['swapout_pages'] = int(match[1])
    return record


def condition_failures(record, max_load, min_memory):
    failures = []
    if not record['cpu_count'] or record['load_average'][0] / record['cpu_count'] > max_load:
        failures.append('one-minute load exceeds declared per-CPU ceiling')
    if record['available_memory_bytes'] is None or record['available_memory_bytes'] < min_memory:
        failures.append('available-memory estimate below declared minimum or unavailable')
    if sys.platform == 'darwin' and (not record['power'] or 'AC Power' not in record['power']):
        failures.append('AC power not confirmed')
    return failures


def native_probe():
    zig = tool('zig')
    if not zig:
        return {'state': 'FAIL', 'reason': 'Zig unavailable'}
    with tempfile.TemporaryDirectory(prefix='csl-native-probe-') as temp:
        source = Path(temp) / 'probe.zig'
        source.write_text('const std = @import("std"); pub fn main() void { std.io.getStdOut().writer().writeAll("native-sdk-probe\\n") catch unreachable; }\n')
        binary = Path(temp) / 'probe'
        env = os.environ.copy()
        # Prefer the original system xcrun over any project SDK-overlay shim.
        if sys.platform == 'darwin':
            env['PATH'] = '/usr/bin:/bin:' + env.get('PATH', '')
        command = [zig, 'build-exe', str(source), '-lc', '-femit-bin=' + str(binary)]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True) if result.returncode == 0 else None
        passed = run is not None and run.returncode == 0 and run.stdout == 'native-sdk-probe\n'
        return {'state': 'PASS' if passed else 'FAIL', 'command': command,
                'build_exit': result.returncode, 'diagnostic': result.stderr,
                'run_exit': run.returncode if run else None,
                'sdk': command_output(['/usr/bin/xcrun', '--show-sdk-path']) if sys.platform == 'darwin' else None}


def compiler_label(env, candidate):
    tools = env['tools']
    if candidate == 'rust':
        return tools['rustc']['version']
    if candidate == 'zig':
        return tools['zig']['version']
    # hybrid: Rust harness/ABI wrapper linking a Zig-compiled kernel, both toolchains apply.
    return f"rustc {tools['rustc']['version']} + zig {tools['zig']['version']}"


def schedule(repeat, seed, workloads, candidates=('rust', 'zig')):
    """Seeded query order; each query's candidate group is rotated by one
    position per repeat, so every candidate takes every position (1st, 2nd,
    ...) an equal (or near-equal, for repeat % len(candidates) != 0) number
    of times. For exactly two candidates this reproduces the original
    alternating rust/zig schedule exactly."""
    rng = random.Random(seed)
    jobs = [(workload, query) for workload in workloads for query in queries(workload)]
    rng.shuffle(jobs)
    n = len(candidates)
    order = []
    for workload, query in jobs:
        base = list(candidates)
        rng.shuffle(base)
        for iteration in range(repeat):
            rotation = iteration % n
            group = base[rotation:] + base[:rotation]
            for candidate in group:
                order.append({'candidate': candidate, 'workload': workload,
                              'query': query, 'iteration': iteration})
    return order


def reference_worker(fixture, directory, workloads):
    oracle = PreparedOracle(load(fixture))
    index = []
    for workload in workloads:
        for query in queries(workload):
            result = oracle.execute(query)
            VALIDATORS['result'].validate(result)
            target = directory / f'{workload}-{query["query_id"]}.json'
            with target.open('w') as stream:
                json.dump(result, stream, separators=(',', ':'))
            index.append({'workload': workload, 'query': query,
                          'path': str(target), 'file_digest': digest(target),
                          'result_digest': result['digest']})
            print(json.dumps({'reference': target.name, 'state': 'PASS'}), flush=True)
            del result
    save(directory / 'index.json', index)


def strict_equal(actual, expected):
    """JSON tree equality that cannot confuse booleans with integer IDs."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(strict_equal(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(strict_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


PROFILE_CAPABLE = ('rust', 'zig')


def sample_worker(spec_path, target):
    spec = load(spec_path)
    job = spec['job']
    # Hybrid is a whole-fixture FFI batch (ADR-0002): it has no pure-store
    # decode/index/query/materialize/encode phases to report, so it runs
    # without --profile and is measured on elapsed_ns/RSS only, same as
    # rust/zig but without phases_ns/phase_detail_ns/phase_subdetail_ns.
    profiled = job['candidate'] in PROFILE_CAPABLE
    command = [spec['binary'], 'workload', '--id', job['workload'], '--corpus', spec['fixture'],
               '--params', json.dumps({'query': job['query']})]
    if profiled:
        command.append('--profile')
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        before = snapshot()
        start = time.perf_counter_ns()
        process = subprocess.Popen(command, stdout=out, stderr=err)
        _, status, usage = os.wait4(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
        elapsed = time.perf_counter_ns() - start
        after = snapshot()
        if process.returncode:
            err.seek(0)
            raise RuntimeError(f'candidate exited {process.returncode}: {err.read(4096).decode(errors="replace")}')
        out.seek(0)
        payload = json.load(out)
    metadata = profile_metadata(payload, elapsed) if profiled else {}
    result = payload['result'] if profiled else payload
    reference = load(spec['reference'])
    # Reference schema was checked once before timing. Full equality also proves
    # the candidate result has that schema, without millions of repeated checks.
    if not strict_equal(result, reference):
        raise ValueError('full oracle result mismatch; sample rejected')
    failures = condition_failures(before, spec['max_load'], spec['min_memory'])
    failures += condition_failures(after, spec['max_load'], spec['min_memory'])
    if before['swapout_pages'] is not None and after['swapout_pages'] is not None and after['swapout_pages'] > before['swapout_pages']:
        failures.append('swapout counter increased during sample')
    record = {**metadata, 'elapsed_ns': elapsed,
              'rss_peak_bytes': int(usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)),
              'result_digest': reference['digest'], 'conformance': True,
              'conditions_before': before, 'conditions_after': after,
              'condition_failures': sorted(set(failures))}
    save(target, record)


def run_campaign(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = environment()
    machine = snapshot()
    probe = native_probe()
    failures = condition_failures(machine, args.max_load_per_cpu, args.min_memory_gib * 1024**3)
    if probe['state'] != 'PASS':
        failures.append('unmodified native toolchain/SDK probe failed')
    if env['sdk_workaround']:
        failures.append('project SDK overlay is present; build provenance is not native')
    manifest = {'schema': 'csl.eval.campaign/v0.1', 'state': 'PREFLIGHT',
                'started_utc': timestamp(), 'environment': env, 'preflight_machine': machine,
                'native_probe': probe, 'preflight_failures': failures,
                'classification': 'exploratory' if args.exploratory else 'controlled',
                'preset': args.preset, 'shape': args.shape, 'repeat': args.repeat,
                'seed': args.seed, 'source_digest': source_digest(),
                'policy': {'max_one_minute_load_per_cpu': args.max_load_per_cpu,
                           'minimum_available_memory_gib': args.min_memory_gib,
                           'swapping': 'reject controlled samples with increased swapout counter',
                           'cache': 'one validated unmeasured warm-up per candidate/query; OS caches not flushed',
                           'order': 'seeded query order; candidate group rotated by one position per repeat',
                           'isolation': 'serial native processes; oracle preparation and result verification outside native timing'}}
    save(output / 'manifest.json', manifest)
    if failures and not args.exploratory:
        manifest['state'] = 'BLOCKED'
        save(output / 'manifest.json', manifest)
        print(json.dumps({'state': 'BLOCKED', 'reasons': failures, 'output': str(output)}))
        return 2
    try:
        binaries = {}
        for candidate in args.candidates:
            binary = executable(candidate)
            if not binary.exists():
                raise ValueError(f'build {candidate} first')
            info = json.loads(subprocess.check_output([str(binary), 'info']))
            if info.get('representation') != 'typed-hash-v1':
                raise ValueError(f'{candidate}: unsupported representation')
            binaries[candidate] = {'path': str(binary), 'digest': digest(binary), 'bytes': binary.stat().st_size}
        manifest['artifacts'] = binaries
        corpus = ROOT / 'corpus/synthetic' / f'{args.preset}-campaign-{args.shape}-{args.seed}.json'
        n, m = PRESETS[args.preset]
        # Always regenerate from the declared deterministic parameters.
        generate(corpus, n, m, args.seed, args.shape)
        manifest['corpus'] = {'path': str(corpus), 'digest': digest(corpus), 'entities': n, 'edges': m, 'evidence': m}
        order = schedule(args.repeat, args.seed, args.workload, args.candidates)
        save(output / 'order.json', order)
        manifest['state'] = 'PREPARING_ORACLE'
        save(output / 'manifest.json', manifest)
        references = output / 'references'
        references.mkdir()
        with (output / 'oracle.log').open('w') as log:
            subprocess.run([sys.executable, __file__, '_references', '--fixture', str(corpus), '--output', str(references), '--workloads', *args.workload], stdout=log, stderr=subprocess.STDOUT, check=True)
        refs = {(row['workload'], row['query']['query_id']): row for row in load(references / 'index.json')}
        manifest['state'] = 'RUNNING'
        save(output / 'manifest.json', manifest)
        seen = set()
        rows = []
        with (output / 'records.jsonl').open('w') as records, (output / 'warmups.jsonl').open('w') as warmups:
            for sequence, job in enumerate(order):
                candidate = job['candidate']
                key = (job['workload'], job['query']['query_id'])
                spec = {'job': job, 'binary': binaries[candidate]['path'], 'fixture': str(corpus),
                        'reference': refs[key]['path'], 'max_load': args.max_load_per_cpu,
                        'min_memory': args.min_memory_gib * 1024**3}
                for warmup in ([True, False] if (candidate, key) not in seen else [False]):
                    if digest(binaries[candidate]['path']) != binaries[candidate]['digest']:
                        raise ValueError('candidate artifact changed during campaign')
                    save(output / 'sample-spec.json', spec)
                    with (output / 'sample-worker.log').open('a') as log:
                        subprocess.run([sys.executable, __file__, '_sample', '--spec', str(output / 'sample-spec.json'), '--output', str(output / 'sample-result.json')], stdout=log, stderr=subprocess.STDOUT, check=True)
                    record = load(output / 'sample-result.json')
                    record.update(schema='csl.eval.benchmark/v0.1', candidate=candidate, candidate_commit=None,
                                  workload=job['workload'], query=job['query']['query_id'], corpus=str(corpus.relative_to(ROOT)),
                                  seed=args.seed, shape=args.shape, iteration=job['iteration'], sequence=sequence,
                                  mode='cold-process', compiler=compiler_label(env, candidate),
                                  os=env['os'], architecture=env['architecture'], hardware=env['hardware'],
                                  source_digest=manifest['source_digest'], corpus_digest=manifest['corpus']['digest'],
                                  artifact_digest=binaries[candidate]['digest'], artifact_bytes=binaries[candidate]['bytes'],
                                  classification=manifest['classification'], warmup=warmup,
                                  sdk_workaround=env['sdk_workaround'])
                    VALIDATORS['benchmark-record'].validate(record)
                    if record['condition_failures'] and not args.exploratory:
                        save(output / 'rejected-sample.json', record)
                        raise ValueError('machine conditions violated; controlled sample rejected')
                    stream = warmups if warmup else records
                    stream.write(json.dumps(record) + '\n')
                    stream.flush()
                    if not warmup:
                        rows.append(record)
                    seen.add((candidate, key))
                print(json.dumps({'completed': sequence + 1, 'total': len(order), 'candidate': candidate, 'query': job['query']['query_id']}), flush=True)
        if source_digest() != manifest['source_digest'] or digest(corpus) != manifest['corpus']['digest']:
            raise ValueError('source or corpus changed during campaign')
        for candidate, artifact in binaries.items():
            if digest(artifact['path']) != artifact['digest']:
                raise ValueError(f'{candidate} artifact changed during campaign')
        for reference in refs.values():
            if digest(reference['path']) != reference['file_digest']:
                raise ValueError('oracle reference changed during campaign')
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row['workload'], row['query'], row['candidate'])].append(row)
        summaries = []
        for (workload, query, candidate), samples in sorted(grouped.items()):
            has_phases = all('phases_ns' in row for row in samples)
            has_detail = all('phase_detail_ns' in row for row in samples)
            has_subdetail = all('phase_subdetail_ns' in row for row in samples)
            summaries.append({'workload': workload, 'query': query, 'candidate': candidate, 'samples': len(samples),
                              **({'median_phases_ns': {k: statistics.median(row['phases_ns'][k] for row in samples) for k in ('load', 'index', 'query', 'result')}} if has_phases else {}),
                              **({'median_phase_detail_ns': {k: statistics.median(row['phase_detail_ns'][k] for row in samples) for k in ('decode', 'construct', 'materialize', 'encode')}} if has_detail else {}),
                              **({'median_phase_subdetail_ns': {k: statistics.median(row['phase_subdetail_ns'][k] for row in samples) for k in ('read', 'parse', 'entities', 'adjacency', 'sort')}} if has_subdetail else {}),
                              'median_elapsed_ns': statistics.median(row['elapsed_ns'] for row in samples),
                              'median_peak_rss_bytes': statistics.median(row['rss_peak_bytes'] for row in samples)})
        save(output / 'summary.json', {'state': 'PASS', 'classification': manifest['classification'], 'records': len(rows),
                                      'condition_failure_samples': sum(bool(r['condition_failures']) for r in rows), 'groups': summaries})
        manifest.update(state='PASS', finished_utc=timestamp(), records=len(rows))
        save(output / 'manifest.json', manifest)
        print(json.dumps({'state': 'PASS', 'classification': manifest['classification'], 'records': len(rows), 'output': str(output)}))
        return 0
    except Exception as error:
        manifest.update(state='FAIL', finished_utc=timestamp(), error=str(error))
        save(output / 'manifest.json', manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--output', required=True)
    run.add_argument('--preset', choices=('SMOKE', 'S'), default='S')
    run.add_argument('--shape', choices=('mixed', 'cycle', 'fanout'), default='mixed')
    run.add_argument('--repeat', type=int, default=10)
    run.add_argument('--seed', type=int, default=20260928)
    run.add_argument('--workload', choices=('W1', 'W2'), action='append')
    run.add_argument('--candidates', nargs='+', choices=('rust', 'zig', 'hybrid'), default=['rust', 'zig'])
    run.add_argument('--exploratory', action='store_true')
    run.add_argument('--max-load-per-cpu', type=float, default=0.5)
    run.add_argument('--min-memory-gib', type=float, default=3)
    ref = commands.add_parser('_references')
    ref.add_argument('--fixture', required=True)
    ref.add_argument('--output', required=True)
    ref.add_argument('--workloads', nargs='+', required=True, choices=('W1', 'W2'))
    sample = commands.add_parser('_sample')
    sample.add_argument('--spec', required=True)
    sample.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == '_references':
        reference_worker(args.fixture, Path(args.output), args.workloads)
    elif args.command == '_sample':
        sample_worker(args.spec, args.output)
    else:
        if args.repeat < 1 or args.max_load_per_cpu <= 0 or args.min_memory_gib <= 0:
            parser.error('repeat and condition thresholds must be positive')
        args.workload = list(dict.fromkeys(args.workload or ['W1', 'W2']))
        sys.exit(run_campaign(args))


if __name__ == '__main__':
    main()
