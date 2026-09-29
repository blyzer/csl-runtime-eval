#!/usr/bin/env python3
"""Deterministic W8.S1 mutation batches (ADR-0008 section 11).

`make_delta(fixture, composition, fraction, seed)` returns a valid S0 `mutate` batch that touches
`fraction` of all rows (entities + relations + evidence). Compositions: entity, relation, evidence,
mixed. Every batch respects the ADR-0008 rules by construction (no conflicts, entity removals carry
all their dependents, removal counts never exceed multiplicity) and is checked against the oracle
model before it is used.
"""
from collections import defaultdict
import hashlib
import random

REL = ['CALLS', 'REFERENCES', 'IMPLEMENTS', 'OVERRIDES', 'CONTAINS']
QUAL = ['LEXICAL', 'PROBABLE', 'DERIVED', 'EXACT', 'VERIFIED']
KINDS = ['TYPE', 'METHOD', 'FUNCTION', 'FIELD', 'MODULE', 'FILE', 'VARIABLE']
EVIDENCE_FIELDS = ('proposition', 'subject', 'relation', 'object', 'polarity', 'quality', 'freshness_epoch', 'lineage')
COMPOSITIONS = ('entity', 'relation', 'evidence', 'mixed')
FRACTIONS = (0.0001, 0.001, 0.01, 0.05)

# operation shares of the budget K
SHARES = {
    'entity': {'entity_add': .50, 'entity_update': .40, 'entity_remove': .10},
    'relation': {'relation_add': .60, 'relation_remove': .40},
    'evidence': {'evidence_add': .60, 'evidence_remove': .40},
    'mixed': {'relation_add': .40, 'relation_remove': .20, 'evidence_add': .20, 'evidence_remove': .10,
              'entity_add': .05, 'entity_update': .03, 'entity_remove': .02},
}


def _seed(seed, composition, fraction):
    digest = hashlib.sha256(f'{seed}/{composition}/{fraction}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big')


def total_rows(fixture):
    return len(fixture['entities']) + len(fixture['relations']) + len(fixture['evidence'])


def make_delta(fixture, composition, fraction, seed=20260929):
    if composition not in SHARES:
        raise ValueError(f'unknown composition {composition}')
    rng = random.Random(_seed(seed, composition, fraction))
    budget = max(4, round(fraction * total_rows(fixture)))
    shares = SHARES[composition]
    ids = [e['id'] for e in fixture['entities']]
    strings = fixture['strings']
    names = {e['id']: strings[e['name_sid']] for e in fixture['entities']}
    rels = fixture['relations']
    evs = fixture['evidence']
    ops = []
    used_ops = 0

    # -- entity removals first: they fix which rows and ids are off limits ---------------------
    removed_ids, removed_rel_idx, removed_ev_idx = set(), set(), set()
    n_remove_ops = round(shares.get('entity_remove', 0) * budget)
    if n_remove_ops:
        touch_rel, touch_ev = defaultdict(list), defaultdict(list)
        for i, r in enumerate(rels):
            touch_rel[r['subject']].append(i)
            if r['object'] != r['subject']:
                touch_rel[r['object']].append(i)
        for i, e in enumerate(evs):
            touch_ev[e['subject']].append(i)
            if e['object'] != e['subject']:
                touch_ev[e['object']].append(i)
        contained = {e['container'] for e in fixture['entities'] if e.get('container') is not None}
        candidates = rng.sample(ids, min(len(ids), 4 * n_remove_ops + 16))
        spent = 0
        for victim in candidates:
            if spent >= n_remove_ops:
                break
            if victim in contained or victim in removed_ids:
                continue
            rel_idx = [i for i in touch_rel[victim] if i not in removed_rel_idx]
            ev_idx = [i for i in touch_ev[victim] if i not in removed_ev_idx]
            # a removed entity's dependents may not touch another removed entity that is not yet decided
            cost = 1 + len(rel_idx) + len(ev_idx)
            if spent + cost > max(n_remove_ops, cost) and spent:
                continue
            removed_ids.add(victim)
            removed_rel_idx.update(rel_idx)
            removed_ev_idx.update(ev_idx)
            spent += cost
        for victim in sorted(removed_ids):
            ops.append({'op': 'REMOVE_ENTITY', 'id': victim})
        for i in sorted(removed_rel_idx):
            ops.append({'op': 'REMOVE_RELATION', **{k: rels[i][k] for k in ('subject', 'relation', 'object')}})
        for i in sorted(removed_ev_idx):
            ops.append({'op': 'REMOVE_EVIDENCE', **{k: evs[i][k] for k in EVIDENCE_FIELDS}})
        used_ops += spent
    live_ids = [i for i in ids if i not in removed_ids]

    # -- entity updates and adds --------------------------------------------------------------
    n_update = round(shares.get('entity_update', 0) * budget)
    updated = rng.sample(live_ids, min(n_update, len(live_ids)))
    for j, victim in enumerate(updated):
        change = rng.choice(['name', 'kind', 'both'])
        fields = {}
        if change in ('name', 'both'):
            fields['name'] = f'upd-{seed % 997}-{j % 50}'      # repeated names on purpose: ambiguity
        if change in ('kind', 'both'):
            fields['kind'] = rng.choice(KINDS)
        ops.append({'op': 'UPDATE_ENTITY', 'id': victim, 'set': fields})
    used_ops += len(updated)
    next_id = max(ids) + 1
    n_add = round(shares.get('entity_add', 0) * budget)
    new_ids = []
    with_relations = composition == 'mixed'
    for j in range(n_add):
        new_id = next_id + j
        new_ids.append(new_id)
        ops.append({'op': 'ADD_ENTITY', 'id': new_id, 'kind': rng.choice(KINDS), 'name': f'new-{seed % 997}-{j}', 'container': None})
        used_ops += 1
        if with_relations:
            for _ in range(2):
                ops.append({'op': 'ADD_RELATION', 'subject': new_id, 'relation': rng.choice(REL), 'object': rng.choice(live_ids)})
                used_ops += 1
    endpoints = live_ids + new_ids

    # -- relations -------------------------------------------------------------------------------
    n_rel_remove = round(shares.get('relation_remove', 0) * budget)
    pool = [i for i in range(len(rels)) if i not in removed_rel_idx] if n_rel_remove else []
    chosen = rng.sample(pool, min(n_rel_remove, len(pool))) if pool else []
    removal_values = set()
    for i in chosen:
        value = (rels[i]['subject'], rels[i]['relation'], rels[i]['object'])
        removal_values.add(value)
        ops.append({'op': 'REMOVE_RELATION', 'subject': value[0], 'relation': value[1], 'object': value[2]})
    used_ops += len(chosen)
    n_rel_add = round(shares.get('relation_add', 0) * budget)
    made = 0
    while made < n_rel_add:
        if rels and rng.random() < .2:                       # a duplicate of an existing row: multiplicity only
            r = rels[rng.randrange(len(rels))]
            value = (r['subject'], r['relation'], r['object'])
            if r['subject'] in removed_ids or r['object'] in removed_ids:
                continue
        else:
            value = (rng.choice(endpoints), rng.choice(REL), rng.choice(endpoints))
        if value in removal_values:
            continue
        ops.append({'op': 'ADD_RELATION', 'subject': value[0], 'relation': value[1], 'object': value[2]})
        made += 1
    used_ops += made

    # -- evidence --------------------------------------------------------------------------------
    n_ev_remove = round(shares.get('evidence_remove', 0) * budget)
    pool = [i for i in range(len(evs)) if i not in removed_ev_idx] if n_ev_remove else []
    chosen = rng.sample(pool, min(n_ev_remove, len(pool))) if pool else []
    ev_removed = set()
    for i in chosen:
        row = tuple(evs[i][k] for k in EVIDENCE_FIELDS)
        ev_removed.add(row)
        ops.append({'op': 'REMOVE_EVIDENCE', **dict(zip(EVIDENCE_FIELDS, row))})
    used_ops += len(chosen)
    n_ev_add = round(shares.get('evidence_add', 0) * budget)
    next_prop = max((e['proposition'] for e in evs), default=0) + 1
    for j in range(n_ev_add):
        row = (next_prop + j, rng.choice(endpoints), rng.choice(REL), rng.choice(endpoints), rng.choice(['POSITIVE', 'NEGATIVE']),
               rng.choice(QUAL), rng.randint(1, 3), rng.randint(0, 7))
        ops.append({'op': 'ADD_EVIDENCE', **dict(zip(EVIDENCE_FIELDS, row))})
    used_ops += n_ev_add
    rng.shuffle(ops)                                          # operation order must not matter
    return ops


def delta_queries(batch, fixture):
    """Queries aimed at what the delta touched (plus one far from it): stale derived data shows here."""
    q = lambda qid, op, **kw: {'schema': 'csl.eval.query/v0.1', 'query_id': qid, 'op': op, **kw}      # noqa: E731
    touched = []
    for op in batch:
        for key in ('id', 'subject', 'object'):
            if key in op and op[key] not in touched:
                touched.append(op[key])
        if len(touched) >= 6:
            break
    known = {e['id'] for e in fixture['entities']}
    live = [t for t in touched if t in known] or [fixture['entities'][0]['id']]
    out = []
    for i, entity in enumerate(live[:3]):
        base = {'op': 'RESOLVE', 'entity_id': entity}
        out += [q(f'd{i}-out', 'RELATED', input=base), q(f'd{i}-in', 'RELATED', input=base, direction='IN'),
                q(f'd{i}-d3', 'TRAVERSE', input=base, max_depth=3, max_paths=5000),
                q(f'd{i}-cap', 'TRAVERSE', input=base, max_depth=4, max_paths=7)]
    names = [op['set']['name'] for op in batch if op['op'] == 'UPDATE_ENTITY' and 'name' in op.get('set', {})][:1]
    names += [op['name'] for op in batch if op['op'] == 'ADD_ENTITY'][:1]
    out += [q(f'd-name{i}', 'RESOLVE', name=n) for i, n in enumerate(names)]
    return out


def standard_queries(fixture):
    """The fixed Q of ADR-0008 section 9: the W1/W2 set plus evidence-predicate queries."""
    first = fixture['entities'][0]['id']
    base = {'op': 'RESOLVE', 'entity_id': first}
    q = lambda qid, op, **kw: {'schema': 'csl.eval.query/v0.1', 'query_id': qid, 'op': op, **kw}      # noqa: E731
    return [q('lookup-first', 'RESOLVE', entity_id=first), q('lookup-missing', 'RESOLVE', entity_id=2**64 - 1),
            q('scan-type', 'FILTER', kind='TYPE'), q('outgoing', 'RELATED', input=base),
            q('incoming', 'RELATED', input=base, direction='IN'), q('depth-4', 'TRAVERSE', input=base, max_depth=4, max_paths=100000),
            q('mixed-relations', 'TRAVERSE', input=base, relations=['CALLS', 'REFERENCES'], max_depth=4),
            q('ev-verified', 'FILTER', evidence={'min_quality': 'VERIFIED'}), q('ev-epoch2', 'FILTER', evidence={'freshness_epoch': 2}),
            q('ev-hub', 'RELATED', input=base, evidence={'min_quality': 'PROBABLE'})]
