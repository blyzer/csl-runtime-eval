#!/usr/bin/env python3
"""Gate #1 scenario registry (ADR-0007): W2.X1-X5, W3.S1-S2, W4.S1-S6.

Each scenario is a deterministic corpus specification plus a query set. The
existing `harness.generate.generate` (W1/W2 corpora) is untouched; scenario
corpora come from `generate_corpus`, which reproduces that generator's bytes
for the plain `mixed` shape with default evidence and names (tested), so the two
stay comparable.

A *job* is `{workload, query, resolve?}`: `workload` is the id passed to the
candidate (`W2`, `W3`, `W4`), `query` a query IR, and `resolve` the optional W4
in-process lookup block (`{names, rounds}`).
"""
import bisect
import json
from pathlib import Path
import random

REL = ['CALLS', 'REFERENCES', 'IMPLEMENTS', 'OVERRIDES', 'CONTAINS']
QUAL = ['LEXICAL', 'PROBABLE', 'DERIVED', 'EXACT', 'VERIFIED']
MAX_ID = 2**64 - 1

# Scale per family. `SMOKE` keeps local checks fast; `S` matches the controlled
# S campaigns. W4 stresses strings, so it uses more entities and few edges.
SCALES = {
    'graph': {'SMOKE': (2_000, 8_000), 'S': (100_000, 1_000_000), 'M': (1_000_000, 10_000_000)},
    'strings': {'SMOKE': (5_000, 2_000), 'S': (200_000, 20_000), 'M': (2_000_000, 200_000)},
}


def base_spec(**over):
    spec = {'shape': 'mixed', 'dup_factor': 1, 'sparse_ids': False, 'evidence': 'default', 'names': None}
    spec.update(over)
    return spec


def name_of(k, length):
    """Deterministic name number `k` of exactly `length` (>= 8) characters."""
    base = f'{k:08x}'
    return (base * (length // 8 + 1))[:length]


def _zipf_cum(count, s):
    total, cum = 0.0, []
    for k in range(1, count + 1):
        total += 1.0 / k ** s
        cum.append(total)
    return cum


def _evidence_attrs(kind, rng, i):
    if kind == 'default':
        return 'POSITIVE', 'EXACT', 1, i % 8
    if kind == 'w3s1':
        # Quality uniform over five levels. Epoch 1..4 get 50% / 10% / 1% / 0.1% of
        # the rows, so `freshness_epoch` selects a known fraction; the rest spread
        # over epochs 5..1000.
        u = rng.random()
        epoch = 1 if u < 0.5 else 2 if u < 0.6 else 3 if u < 0.61 else 4 if u < 0.611 else rng.randint(5, 1000)
        return 'POSITIVE', QUAL[rng.randrange(5)], epoch, i % 8
    if kind == 'w3s2':
        u = rng.random()
        quality = QUAL[0 if u < .05 else 1 if u < .20 else 2 if u < .50 else 3 if u < .80 else 4]
        lineage = min(int(rng.paretovariate(1.2)) - 1, 100_000)
        return ('NEGATIVE' if rng.random() < .3 else 'POSITIVE'), quality, rng.randint(1, 3), lineage
    raise ValueError(f'unknown evidence mix {kind}')


def generate_corpus(path, n, m, seed, spec):
    """Write a fixture for `spec`; return metadata used to build queries."""
    shape, dup = spec['shape'], spec['dup_factor']
    step = (MAX_ID // n) if spec['sparse_ids'] else 1
    ident = lambda index: index * step            # noqa: E731  index is 1-based
    zipf = _zipf_cum(n, 1.1) if shape == 'powerlaw' else None

    def edges():
        """Yield (unique_edge_number, subject_index, relation, object_index)."""
        rng = random.Random(seed)
        produced = 0
        if shape == 'dupes':
            unique = -(-m // dup)
            for j in range(unique):
                s, o = rng.randint(1, n), rng.randint(1, n)
                for _ in range(dup):
                    if produced >= m:
                        return
                    yield j, s, REL[j % 5], o
                    produced += 1
            return
        for i in range(m):
            if shape == 'chain':
                s = i % (n - 1) + 1
                o = s + 1
            elif shape == 'powerlaw':
                s = bisect.bisect_left(zipf, rng.random() * zipf[-1]) + 1
                o = rng.randint(1, n)
            else:
                s, o = rng.randint(1, n), rng.randint(1, n)
            yield i, s, REL[i % 5], o

    names = spec['names']
    if names:
        unique = max(1, round(n * names['unique_ratio']))
        name_rng = random.Random(seed ^ 0x57A11)
        cum = _zipf_cum(unique, names['zipf']) if names.get('zipf') else None
        picks = ((bisect.bisect_left(cum, name_rng.random() * cum[-1])) if cum else (i % unique) for i in range(n))
        table = [name_of(pick, names['length']) for pick in picks]
    else:
        table = [f'sym{i}' for i in range(n)]

    def evidence():
        rng = random.Random(seed * 7919 + 1)
        previous = None
        for i, (j, s, rel, o) in enumerate(edges()):
            if spec['evidence'] == 'w3s2' and previous is not None:
                u = rng.random()
                if u < .10:                       # duplicate row, kept visible
                    yield previous
                    continue
                if u < .15:                       # conflicting support: same key, other polarity
                    previous = {**previous, 'polarity': 'NEGATIVE' if previous['polarity'] == 'POSITIVE' else 'POSITIVE'}
                    yield previous
                    continue
            pol, qual, epoch, lineage = _evidence_attrs(spec['evidence'], rng, j if shape == 'dupes' else i)
            previous = {'proposition': j + 1 if shape == 'dupes' else i + 1, 'subject': ident(s), 'relation': rel,
                        'object': ident(o), 'polarity': pol, 'quality': qual, 'freshness_epoch': epoch, 'lineage': lineage}
            yield previous

    def relations():
        for _, s, rel, o in edges():
            yield {'subject': ident(s), 'relation': rel, 'object': ident(o)}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dump = lambda item: json.dumps(item, separators=(',', ':'))   # noqa: E731
    with path.open('w') as f:
        f.write('{"schema":"csl.eval.fixture/v0.1","snapshot":"S0","epoch":1,')

        def array(name, items):
            f.write(json.dumps(name) + ':[')
            for k, item in enumerate(items):
                if k:
                    f.write(',')
                f.write(dump(item))
            f.write(']')
        array('strings', table); f.write(',')
        array('entities', ({'id': ident(i + 1), 'kind': 'METHOD' if i % 3 else 'TYPE', 'name_sid': i, 'container': None}
                           for i in range(n))); f.write(',')
        array('relations', relations()); f.write(',')
        array('evidence', evidence())
        f.write('}')
    meta = {'entities': n, 'edges': m, 'seed': seed, 'spec': spec, 'first_id': ident(1), 'last_id': ident(n)}
    path.with_suffix('.meta.json').write_text(json.dumps(meta, sort_keys=True) + '\n')
    return meta


def _q(query_id, op, **kw):
    return {'schema': 'csl.eval.query/v0.1', 'query_id': query_id, 'op': op, **kw}


def w2_queries(base_id):
    base = {'op': 'RESOLVE', 'entity_id': base_id}
    return [_q('outgoing', 'RELATED', input=base), _q('incoming', 'RELATED', input=base, direction='IN'),
            *[_q(f'depth-{d}', 'TRAVERSE', input=base, max_depth=d, max_paths=100000) for d in (2, 4, 8)],
            _q('mixed-relations', 'TRAVERSE', input=base, relations=['CALLS', 'REFERENCES'], max_depth=4)]


def _job(workload, query, resolve=None):
    job = {'workload': workload, 'query': query}
    if resolve:
        job['resolve'] = resolve
    return job


def _all(**evidence):
    """Select every entity, so evidence predicates alone drive the result size."""
    return _q('placeholder', 'FILTER', **({'evidence': evidence} if evidence else {}))


def _named(job, name):
    job['query'] = {**job['query'], 'query_id': name}
    return job


def _w3s1(meta, oracle):
    cases = [('sel-100', {}), ('min-exact', {'min_quality': 'EXACT'}), ('min-verified', {'min_quality': 'VERIFIED'}),
             ('epoch-1', {'freshness_epoch': 1}), ('epoch-2', {'freshness_epoch': 2}),
             ('epoch-3', {'freshness_epoch': 3}), ('epoch-4', {'freshness_epoch': 4}),
             ('verified-epoch-1', {'min_quality': 'VERIFIED', 'freshness_epoch': 1})]
    return [_named(_job('W3', _all(**e)), name) for name, e in cases]


def _w3s2(meta, oracle):
    hub = {'op': 'RESOLVE', 'entity_id': meta['first_id']}
    return [_named(_job('W3', _all()), 'sel-100'),
            _named(_job('W3', _all(min_quality='VERIFIED')), 'min-verified'),
            _named(_job('W3', _all(min_quality='EXACT')), 'min-exact'),
            _named(_job('W3', _all(freshness_epoch=1)), 'epoch-1'),
            _job('W3', _q('hub-outgoing', 'RELATED', input=hub, evidence={'min_quality': 'PROBABLE'})),
            _job('W3', _q('hub-depth-4', 'TRAVERSE', input=hub, max_depth=4, max_paths=100000,
                          evidence={'min_quality': 'PROBABLE'}))]


def _x5(meta, oracle):
    """Cap-boundary queries around the exact number of edges a traversal inspects."""
    jobs = []
    for direction in ('OUT', 'IN'):
        base = {'op': 'RESOLVE', 'entity_id': meta['first_id']}
        probe = _q('probe', 'TRAVERSE', input=base, direction=direction, max_depth=4, max_paths=2**62)
        total = oracle.traversal_steps(probe)
        tag = direction.lower()
        if total < 3:
            raise ValueError(f'{direction} traversal from the first entity inspects only {total} edges')
        for label, cap in (('cap-1', 1), ('cap-2', 2), ('cap-below', total - 1), ('cap-exact', total),
                           ('cap-above', total + 1)):
            jobs.append(_job('W2', _q(f'{tag}-{label}', 'TRAVERSE', input=base, direction=direction, max_depth=4,
                                      max_paths=cap)))
    return jobs


def _w4_names(spec, count, seed):
    unique = max(1, round(spec['n'] * spec['names']['unique_ratio']))
    rng = random.Random(seed ^ 0xBEEF)
    zipf = spec['names'].get('zipf')
    cum = _zipf_cum(unique, zipf) if zipf else None
    ks = [(bisect.bisect_left(cum, rng.random() * cum[-1]) if cum else rng.randrange(unique)) for _ in range(count)]
    return [name_of(k, spec['names']['length']) for k in ks]


def _w4(count, rounds):
    def build(meta, oracle):
        names = _w4_names(meta['spec'] | {'n': meta['entities']}, count, meta['seed'])
        query = _q('resolve-primary', 'RESOLVE', name=names[0])
        return [_job('W4', query, {'names': names, 'rounds': rounds})]
    return build


def _scenario(id_, family, description, variants, build, candidates=('rust', 'zig', 'hybrid'), dynamic=False):
    return {'id': id_, 'family': family, 'description': description, 'variants': variants, 'build': build,
            'candidates': candidates, 'dynamic': dynamic}


def _w2_from_first(meta, oracle):
    return [_job('W2', q) for q in w2_queries(meta['first_id'])]


def _w2_powerlaw(meta, oracle):
    jobs = _w2_from_first(meta, oracle)
    tail = {'op': 'RESOLVE', 'entity_id': meta['last_id']}
    return jobs + [_job('W2', _q('tail-outgoing', 'RELATED', input=tail)),
                   _job('W2', _q('tail-depth-4', 'TRAVERSE', input=tail, max_depth=4, max_paths=100000))]


def _w2_chain(meta, oracle):
    base = {'op': 'RESOLVE', 'entity_id': meta['first_id']}
    return [_job('W2', _q('outgoing', 'RELATED', input=base)),
            _job('W2', _q('incoming', 'RELATED', input=base, direction='IN')),
            *[_job('W2', _q(f'depth-{d}', 'TRAVERSE', input=base, max_depth=d, max_paths=100000)) for d in (2, 8, 64)],
            _job('W2', _q('capped-depth-64', 'TRAVERSE', input=base, max_depth=64, max_paths=10)),
            _job('W2', _q('mixed-relations-64', 'TRAVERSE', input=base, relations=['CALLS', 'REFERENCES'], max_depth=64))]


def _w2_sparse(meta, oracle):
    return _w2_from_first(meta, oracle) + [
        _job('W2', _q('lookup-first', 'RESOLVE', entity_id=meta['first_id'])),
        _job('W2', _q('lookup-missing', 'RESOLVE', entity_id=MAX_ID)),
        _job('W2', _q('scan-type', 'FILTER', kind='TYPE'))]


def _names(unique_ratio, length, zipf=None):
    return {'unique_ratio': unique_ratio, 'length': length, 'zipf': zipf}


def _strings_spec(**names):
    return base_spec(names=_names(**names))


REGISTRY = {s['id']: s for s in [
    _scenario('W2.X1', 'graph', 'power-law out-degree (Zipf 1.1)', {'default': base_spec(shape='powerlaw')}, _w2_powerlaw),
    _scenario('W2.X2', 'graph', 'single long chain (depth up to the 64 limit)', {'default': base_spec(shape='chain')}, _w2_chain),
    _scenario('W2.X3', 'graph', 'duplicate/parallel edges and identical evidence rows (x4)',
              {'default': base_spec(shape='dupes', dup_factor=4)}, _w2_from_first),
    _scenario('W2.X4', 'graph', 'sparse u64 entity IDs near 2^64', {'default': base_spec(sparse_ids=True)}, _w2_sparse),
    _scenario('W2.X5', 'graph', 'cap boundary: max_paths at, below and above the inspected-edge count',
              {'default': base_spec()}, _x5, dynamic=True),
    _scenario('W3.S1', 'graph', 'evidence selectivity via min_quality / freshness_epoch',
              {'default': base_spec(evidence='w3s1')}, _w3s1),
    _scenario('W3.S2', 'graph', 'polarity, lineage skew, duplicate and conflicting evidence rows',
              {'default': base_spec(evidence='w3s2')}, _w3s2),
    _scenario('W4.S1', 'strings', 'intern-on-load (all names unique)', {'default': _strings_spec(unique_ratio=1.0, length=24)},
              _w4(1, 1), candidates=('rust', 'zig')),
    _scenario('W4.S2', 'strings', 'name -> entity resolution (unique names)', {'default': _strings_spec(unique_ratio=1.0, length=24)},
              _w4(1000, 20), candidates=('rust', 'zig')),
    _scenario('W4.S3', 'strings', 'ambiguous-name resolution (1% unique, ~100 entities per name)',
              {'default': _strings_spec(unique_ratio=0.01, length=24)}, _w4(1000, 20), candidates=('rust', 'zig')),
    _scenario('W4.S4', 'strings', 'duplication-ratio sweep',
              {f'r{int(r * 100)}': _strings_spec(unique_ratio=r, length=24) for r in (1.0, 0.5, 0.1, 0.01)},
              _w4(200, 5), candidates=('rust', 'zig')),
    _scenario('W4.S5', 'strings', 'name-length and reuse-distribution sweep',
              {'len8-uniform': _strings_spec(unique_ratio=0.5, length=8), 'len64-uniform': _strings_spec(unique_ratio=0.5, length=64),
               'len512-uniform': _strings_spec(unique_ratio=0.5, length=512),
               'len64-zipf': _strings_spec(unique_ratio=0.5, length=64, zipf=1.2)},
              _w4(500, 5), candidates=('rust', 'zig')),
    _scenario('W4.S6', 'strings', 'repeated resolution (zipf-distributed hot names, 100k lookups)',
              {'default': _strings_spec(unique_ratio=0.5, length=24, zipf=1.2)}, _w4(2000, 50), candidates=('rust', 'zig')),
]}


def resolve_scenario(scenario_id, variant=None):
    if scenario_id not in REGISTRY:
        raise ValueError(f'unknown scenario {scenario_id}; known: {", ".join(REGISTRY)}')
    entry = REGISTRY[scenario_id]
    variant = variant or 'default'
    if variant not in entry['variants']:
        raise ValueError(f'{scenario_id} has no variant {variant}; known: {", ".join(entry["variants"])}')
    return entry, variant


def scale_for(family, preset):
    return SCALES[family][preset]


def build_jobs(scenario_id, variant, meta, oracle=None):
    entry, _ = resolve_scenario(scenario_id, variant)
    if entry['dynamic'] and oracle is None:
        raise ValueError(f'{scenario_id} derives its queries from the oracle')
    jobs = entry['build'](meta, oracle)
    ids = [j['query']['query_id'] for j in jobs]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate query ids in scenario')
    return jobs


if __name__ == '__main__':
    for entry in REGISTRY.values():
        print(entry['id'], entry['family'], list(entry['variants']), '-', entry['description'])
