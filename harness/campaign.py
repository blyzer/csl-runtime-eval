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
from harness import scenarios
from oracle.compact import CompactOracle
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


def schedule(repeat, seed, workloads, candidates=('rust', 'zig'), jobs=None):
    """Seeded query order; each query's candidate group is rotated by one
    position per repeat, so every candidate takes every position (1st, 2nd,
    ...) an equal (or near-equal, for repeat % len(candidates) != 0) number
    of times. For exactly two candidates this reproduces the original
    alternating rust/zig schedule exactly."""
    rng = random.Random(seed)
    if jobs is None:
        jobs = [(workload, query, None) for workload in workloads for query in queries(workload)]
    else:
        jobs = [(job['workload'], job['query'], job.get('resolve')) for job in jobs]
    rng.shuffle(jobs)
    n = len(candidates)
    order = []
    for workload, query, resolve in jobs:
        base = list(candidates)
        rng.shuffle(base)
        for iteration in range(repeat):
            rotation = iteration % n
            group = base[rotation:] + base[:rotation]
            for candidate in group:
                order.append({'candidate': candidate, 'workload': workload,
                              'query': query, 'iteration': iteration, **({'resolve': resolve} if resolve else {})})
    return order


def reference_worker(fixture, directory, workloads, jobs_file=None):
    # Streaming/columnar oracle (ADR-0006): the fixture is validated record by
    # record and each reference is written as canonical JSON without ever
    # materializing the fixture or the result as a Python object tree.
    oracle = CompactOracle(fixture)
    index = []
    if jobs_file:
        jobs = load(jobs_file)
    else:
        jobs = [{'workload': workload, 'query': query} for workload in workloads for query in queries(workload)]
    for job in jobs:
        workload, query = job['workload'], job['query']
        target = directory / f'{workload}-{query["query_id"]}.json'
        info = oracle.execute_to(query, target)
        row = {'workload': workload, 'query': query,
               'path': str(target), 'file_digest': digest(target),
               'result_digest': info['digest'], 'propositions': info['propositions'],
               'bytes': info['bytes']}
        if job.get('resolve'):
            row['lookup'] = oracle.name_lookup(job['resolve']['names'], job['resolve']['rounds'])
        index.append(row)
        print(json.dumps({'reference': target.name, 'state': 'PASS', 'bytes': info['bytes']}), flush=True)
    save(directory / 'index.json', index)


def scenario_jobs(scenario_id, variant, fixture, meta_path, output):
    """Materialize a scenario's jobs; oracle-derived queries load the fixture once, here."""
    meta = load(meta_path)
    entry, variant = scenarios.resolve_scenario(scenario_id, variant)
    oracle = CompactOracle(fixture) if entry['dynamic'] else None
    save(output, scenarios.build_jobs(scenario_id, variant, meta, oracle))


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
# Above this size a mismatching (or non-canonical) output is rejected instead of
# being parsed into Python objects for the tolerant tree comparison.
TREE_COMPARE_LIMIT = 128 * 1024 * 1024
_RESULT_MARKER = b'"result":{'


def _same_bytes(stream, start, end, reference_path, block=1 << 22):
    """Compare stream[start:end] with the whole reference file without loading either."""
    reference = Path(reference_path)
    if reference.stat().st_size != end - start:
        return False
    stream.seek(start)
    with reference.open('rb') as expected:
        remaining = end - start
        while remaining:
            want = min(block, remaining)
            if stream.read(want) != expected.read(want):
                return False
            remaining -= want
    return True


def verify_output(stream, reference_path, profiled):
    """Return (envelope metadata, matched) for a candidate's stdout.

    Candidates and the oracle both emit canonical JSON (sorted keys, compact
    separators, UTF-8), so equal results are byte-identical and are compared
    in constant memory, which is stricter than tree equality (it also pins key
    order, number spelling and value types). Only when the bytes differ and both
    sides are small is the tolerant JSON-tree comparison used, so semantically
    equal but non-canonical output keeps passing at S scale and below.
    """
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(max(0, size - 64))
    tail = stream.read()
    trimmed = size - (len(tail) - len(tail.rstrip()))
    start, end, envelope = 0, trimmed, {}
    if profiled:
        stream.seek(0)
        head = stream.read(1 << 24)
        marker = head.find(_RESULT_MARKER)
        if marker >= 0 and head[:marker].rstrip().endswith(b',') and tail.rstrip().endswith(b'}}'):
            envelope = json.loads(head[:marker].rstrip()[:-1] + b'}')
            start, end = marker + len(_RESULT_MARKER) - 1, trimmed - 1
    if envelope or not profiled:
        if _same_bytes(stream, start, end, reference_path):
            return ({**envelope, 'result': None} if profiled else {}), True
    if size > TREE_COMPARE_LIMIT or Path(reference_path).stat().st_size > TREE_COMPARE_LIMIT:
        return None, False
    stream.seek(0)
    payload = json.load(stream)
    result = payload['result'] if profiled else payload
    return (payload if profiled else {}), strict_equal(result, load(reference_path))


def result_digest(reference_path):
    with Path(reference_path).open('rb') as stream:
        match = re.search(rb'"digest"\s*:\s*"(sha256:[0-9a-f]{64})"', stream.read(1 << 16))
    if match:
        return match[1].decode()
    if Path(reference_path).stat().st_size > TREE_COMPARE_LIMIT:
        raise ValueError('large reference has no digest near its start')
    return load(reference_path)['digest']


def check_lookup(reported, expected):
    """The W4 in-process lookups must reproduce the oracle's digest and counts."""
    if not reported or not expected:
        raise ValueError('W4 sample without name_index or oracle expectation')
    for key in ('lookup_digest', 'lookup_ids_total', 'unique_strings', 'lookups'):
        if reported.get(key) != expected[key]:
            raise ValueError(f'name_index {key} mismatch: {reported.get(key)!r} != {expected[key]!r}')
    if len(reported['lookup_ns']) != expected['lookups']:
        raise ValueError('name_index lookup_ns length mismatch')


def sample_worker(spec_path, target):
    spec = load(spec_path)
    job = spec['job']
    # Hybrid is a whole-fixture FFI batch (ADR-0002): it has no pure-store
    # decode/index/query/materialize/encode phases to report, so it runs
    # without --profile and is measured on elapsed_ns/RSS only, same as
    # rust/zig but without phases_ns/phase_detail_ns/phase_subdetail_ns.
    profiled = job['candidate'] in PROFILE_CAPABLE
    params = {'query': job['query']}
    if job.get('resolve') and profiled:
        params['resolve'] = job['resolve']
    command = [spec['binary'], 'workload', '--id', job['workload'], '--corpus', spec['fixture'],
               '--params', json.dumps(params)]
    if profiled:
        command.append('--profile')
        if spec.get('stats'):
            command.append('--stats')
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
        payload, matched = verify_output(out, spec['reference'], profiled)
    # Reference schema was sampled once before timing. Full equality also proves
    # the candidate result has that schema, without millions of repeated checks.
    if not matched:
        raise ValueError('full oracle result mismatch; sample rejected')
    metadata = profile_metadata(payload, elapsed) if profiled else {}
    if job.get('resolve') and profiled:
        check_lookup(metadata.get('name_index'), spec.get('expected_lookup'))
    failures = condition_failures(before, spec['max_load'], spec['min_memory'])
    failures += condition_failures(after, spec['max_load'], spec['min_memory'])
    if before['swapout_pages'] is not None and after['swapout_pages'] is not None and after['swapout_pages'] > before['swapout_pages']:
        failures.append('swapout counter increased during sample')
    record = {**metadata, 'elapsed_ns': elapsed,
              'rss_peak_bytes': int(usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)),
              'result_digest': spec.get('result_digest') or result_digest(spec['reference']), 'conformance': True,
              'conditions_before': before, 'conditions_after': after,
              'condition_failures': sorted(set(failures))}
    save(target, record)


def memory_pass(args, output, corpus, refs, binaries, order, env, manifest, variant):
    """X-MEM: one untimed `--stats` run per pure candidate and query, kept apart from timed records.

    Candidate heap counters need instrumentation (a counting allocator), so these runs are never
    used for timing. Host peak RSS is recorded alongside. The hybrid has no counters (null, with reason).
    """
    rows = []
    seen = set()
    with (output / 'memory.jsonl').open('w') as stream:
        for job in order:
            candidate, key = job['candidate'], (job['workload'], job['query']['query_id'])
            if candidate not in PROFILE_CAPABLE or (candidate, key) in seen:
                continue
            seen.add((candidate, key))
            spec = {'job': job, 'binary': binaries[candidate]['path'], 'fixture': str(corpus), 'reference': refs[key]['path'],
                    'result_digest': refs[key]['result_digest'], 'expected_lookup': refs[key].get('lookup'),
                    'max_load': args.max_load_per_cpu, 'min_memory': args.min_memory_gib * 1024**3, 'stats': True}
            save(output / 'sample-spec.json', spec)
            with (output / 'sample-worker.log').open('a') as log:
                subprocess.run([sys.executable, __file__, '_sample', '--spec', str(output / 'sample-spec.json'),
                                '--output', str(output / 'sample-result.json')], stdout=log, stderr=subprocess.STDOUT, check=True)
            record = load(output / 'sample-result.json')
            record.update(candidate=candidate, workload=job['workload'], query=job['query']['query_id'], variant=variant,
                          scenario=args.scenario, classification='memory-pass (untimed)', corpus=str(corpus.relative_to(ROOT)))
            rows.append(record)
            stream.write(json.dumps(record) + '\n')
            stream.flush()
    return rows


def memory_summary(rows, corpus):
    """Per candidate/query heap and RSS from candidate-reported live heap only, never RSS/count.

    `live_after_index` covers the whole store *including the fixture bytes read from disk*, so it is
    reported per entity (meaningful for the string-heavy W4 corpora) and per record (entities +
    relations + evidence rows).
    """
    entities, records = corpus['entities'], corpus['entities'] + corpus['edges'] + corpus['evidence']
    out = []
    for row in rows:
        heap = row.get('heap')
        entry = {'candidate': row['candidate'], 'workload': row['workload'], 'query': row['query'],
                 'host_rss_peak_bytes': row['rss_peak_bytes']}
        if heap:
            entry.update(heap={k: heap[k] for k in ('allocations', 'total_requested', 'peak_live', 'live_after_load',
                                                    'live_after_index', 'live_after_query', 'retained')},
                         live_after_index_bytes_per_entity=round(heap['live_after_index'] / entities, 2),
                         live_after_index_bytes_per_record=round(heap['live_after_index'] / records, 2))
        else:
            entry['heap'] = None
        out.append(entry)
    return out


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
                'scenario': args.scenario, 'variant': args.variant,
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
        jobs_file = None
        if args.scenario:
            entry, variant = scenarios.resolve_scenario(args.scenario, args.variant)
            spec = entry['variants'][variant]
            manifest['variant'] = variant
            n, m = scenarios.scale_for(entry['family'], args.preset)
            if spec['shape'] == 'chain':
                m = n - 1
            corpus = ROOT / 'corpus/synthetic' / f'{args.scenario}-{variant}-{args.preset}-{args.seed}.json'
            meta = scenarios.generate_corpus(corpus, n, m, args.seed, spec)
            manifest['corpus'] = {'path': str(corpus), 'digest': digest(corpus), 'entities': n, 'edges': m, 'evidence': m}
            manifest['scenario_spec'] = spec
            unsupported = sorted(set(('rust', 'zig', 'hybrid')) - set(entry['candidates']))
            if unsupported:
                manifest['candidates_not_run'] = {c: 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)' for c in unsupported}
            jobs_file = output / 'jobs.json'
            subprocess.run([sys.executable, __file__, '_scenario_jobs', '--scenario', args.scenario, '--variant', variant,
                            '--fixture', str(corpus), '--meta', str(corpus.with_suffix('.meta.json')), '--output', str(jobs_file)], check=True)
            order = schedule(args.repeat, args.seed, None, args.candidates, jobs=load(jobs_file))
            workloads_used = sorted({job['workload'] for job in load(jobs_file)})
        else:
            corpus = ROOT / 'corpus/synthetic' / f'{args.preset}-campaign-{args.shape}-{args.seed}.json'
            n, m = PRESETS[args.preset]
            # Always regenerate from the declared deterministic parameters.
            generate(corpus, n, m, args.seed, args.shape)
            manifest['corpus'] = {'path': str(corpus), 'digest': digest(corpus), 'entities': n, 'edges': m, 'evidence': m}
            order = schedule(args.repeat, args.seed, args.workload, args.candidates)
            workloads_used = args.workload
        save(output / 'order.json', order)
        manifest['state'] = 'PREPARING_ORACLE'
        save(output / 'manifest.json', manifest)
        references = output / 'references'
        references.mkdir()
        reference_command = [sys.executable, __file__, '_references', '--fixture', str(corpus), '--output', str(references)]
        reference_command += ['--jobs', str(jobs_file)] if jobs_file else ['--workloads', *args.workload]
        with (output / 'oracle.log').open('w') as log:
            subprocess.run(reference_command, stdout=log, stderr=subprocess.STDOUT, check=True)
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
                        'reference': refs[key]['path'], 'result_digest': refs[key]['result_digest'],
                        'expected_lookup': refs[key].get('lookup'), 'max_load': args.max_load_per_cpu,
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
                                  **({'scenario': args.scenario, 'variant': variant} if args.scenario else {}),
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
        memory_rows = []
        if args.scenario and args.memory_pass:
            manifest['state'] = 'MEMORY_PASS'
            save(output / 'manifest.json', manifest)
            memory_rows = memory_pass(args, output, corpus, refs, binaries, order, env, manifest, variant)
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
            lookups = [ns for row in samples for ns in row.get('name_index', {}).get('lookup_ns', ())]
            w4 = {}
            if lookups:
                ordered = sorted(lookups)
                pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]   # noqa: E731
                w4 = {'w4': {'lookups': len(ordered), 'lookup_p50_ns': pick(.50), 'lookup_p95_ns': pick(.95), 'lookup_p99_ns': pick(.99),
                             'median_intern_ns': statistics.median(row['name_index']['intern_ns'] for row in samples),
                             'unique_strings': samples[0]['name_index']['unique_strings']}}
            summaries.append({'workload': workload, 'query': query, 'candidate': candidate, 'samples': len(samples), **w4,
                              **({'median_phases_ns': {k: statistics.median(row['phases_ns'][k] for row in samples) for k in ('load', 'index', 'query', 'result')}} if has_phases else {}),
                              **({'median_phase_detail_ns': {k: statistics.median(row['phase_detail_ns'][k] for row in samples) for k in ('decode', 'construct', 'materialize', 'encode')}} if has_detail else {}),
                              **({'median_phase_subdetail_ns': {k: statistics.median(row['phase_subdetail_ns'][k] for row in samples) for k in ('read', 'parse', 'entities', 'adjacency', 'sort')}} if has_subdetail else {}),
                              'median_elapsed_ns': statistics.median(row['elapsed_ns'] for row in samples),
                              'median_peak_rss_bytes': statistics.median(row['rss_peak_bytes'] for row in samples)})
        save(output / 'summary.json', {'state': 'PASS', 'classification': manifest['classification'], 'records': len(rows),
                                      'condition_failure_samples': sum(bool(r['condition_failures']) for r in rows), 'groups': summaries,
                                      **({'memory': memory_summary(memory_rows, manifest['corpus'])} if memory_rows else {})})
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
    run.add_argument('--preset', choices=('SMOKE', 'S', 'M'), default='S')
    run.add_argument('--shape', choices=('mixed', 'cycle', 'fanout'), default='mixed')
    run.add_argument('--repeat', type=int, default=10)
    run.add_argument('--seed', type=int, default=20260928)
    run.add_argument('--workload', choices=('W1', 'W2'), action='append')
    run.add_argument('--candidates', nargs='+', choices=('rust', 'zig', 'hybrid'), default=None)
    run.add_argument('--scenario', help='Gate #1 scenario id, e.g. W2.X1 or W4.S4 (see harness/scenarios.py)')
    run.add_argument('--variant', help='scenario variant (default: the scenario default)')
    run.add_argument('--no-memory-pass', dest='memory_pass', action='store_false', help='skip the untimed --stats memory pass')
    run.add_argument('--exploratory', action='store_true')
    run.add_argument('--max-load-per-cpu', type=float, default=0.5)
    run.add_argument('--min-memory-gib', type=float, default=3)
    ref = commands.add_parser('_references')
    ref.add_argument('--fixture', required=True)
    ref.add_argument('--output', required=True)
    ref.add_argument('--workloads', nargs='+', choices=('W1', 'W2'))
    ref.add_argument('--jobs', help='scenario jobs file (replaces --workloads)')
    scen = commands.add_parser('_scenario_jobs')
    scen.add_argument('--scenario', required=True)
    scen.add_argument('--variant')
    scen.add_argument('--fixture', required=True)
    scen.add_argument('--meta', required=True)
    scen.add_argument('--output', required=True)
    sample = commands.add_parser('_sample')
    sample.add_argument('--spec', required=True)
    sample.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == '_references':
        if bool(args.workloads) == bool(args.jobs):
            parser.error('_references needs exactly one of --workloads or --jobs')
        reference_worker(args.fixture, Path(args.output), args.workloads, args.jobs)
    elif args.command == '_scenario_jobs':
        scenario_jobs(args.scenario, args.variant, args.fixture, args.meta, args.output)
    elif args.command == '_sample':
        sample_worker(args.spec, args.output)
    else:
        if args.repeat < 1 or args.max_load_per_cpu <= 0 or args.min_memory_gib <= 0:
            parser.error('repeat and condition thresholds must be positive')
        args.workload = list(dict.fromkeys(args.workload or ['W1', 'W2']))
        if args.scenario:
            entry, _ = scenarios.resolve_scenario(args.scenario, args.variant)
            args.candidates = args.candidates or list(entry['candidates'])
            if not set(args.candidates) <= set(entry['candidates']):
                parser.error(f'{args.scenario} supports only {", ".join(entry["candidates"])}')
            if args.preset not in scenarios.SCALES[entry['family']]:
                parser.error(f'no scale {args.preset} for {args.scenario}')
        else:
            args.candidates = args.candidates or ['rust', 'zig']
        sys.exit(run_campaign(args))


if __name__ == '__main__':
    main()
