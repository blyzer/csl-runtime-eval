#!/usr/bin/env python3
"""Independent S0 session model (oracle side).

A deliberately simple, dict/Counter-based implementation of the S0 v0 semantics
(oracle/SESSION-SEMANTICS.md) and the ADR-0008 mutation rules. It never shares code with a
candidate: it produces the expected `generation`, `state_digest`, error codes and (through the
classic oracle) query results that a candidate must reproduce byte-for-byte. Small states only.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jsonschema
from oracle.oracle import canon_bytes, execute
from oracle.validation import VALIDATORS, validate_fixture

_PROPS = VALIDATORS['fixture'].schema['properties']
_ENTITY = jsonschema.Draft202012Validator(_PROPS['entities']['items'])
_RELATION = jsonschema.Draft202012Validator(_PROPS['relations']['items'])
_EVIDENCE = jsonschema.Draft202012Validator(_PROPS['evidence']['items'])
EVIDENCE_FIELDS = ('proposition', 'subject', 'relation', 'object', 'polarity', 'quality', 'freshness_epoch', 'lineage')
RELATION_FIELDS = ('subject', 'relation', 'object')
ENTITY_FIELDS = {'ADD_ENTITY': {'op', 'id', 'kind', 'name', 'container'}, 'REMOVE_ENTITY': {'op', 'id'},
                 'UPDATE_ENTITY': {'op', 'id', 'set'}}
OPS = ('ADD_ENTITY', 'REMOVE_ENTITY', 'UPDATE_ENTITY', 'ADD_RELATION', 'REMOVE_RELATION', 'ADD_EVIDENCE', 'REMOVE_EVIDENCE')


class SessionError(Exception):
    """A protocol-level failure: `code` is one of the closed S0 error codes."""

    def __init__(self, code, message=''):
        super().__init__(f'{code}: {message}')
        self.code, self.message = code, message


def _bad_input(message):
    return SessionError('INVALID_INPUT', message)


def _valid(validator, item):
    return not any(validator.iter_errors(item))


class SessionModel:
    """Logical state plus generation for one S0 session (no snapshots stored here)."""

    def __init__(self, context, entities=None, relations=None, evidence=None):
        self.context = {'snapshot': context['snapshot'], 'epoch': context.get('epoch'),
                        'complete': bool(context.get('complete', False))}
        self.entities = dict(entities or {})            # id -> {kind, name, container}
        self.relations = Counter(relations or {})       # (subject, relation, object) -> multiplicity
        self.evidence = Counter(evidence or {})         # 8-tuple -> multiplicity
        self.generation = 0

    # -- construction ------------------------------------------------------
    @classmethod
    def from_fixture(cls, fx):
        validate_fixture(fx)
        model = cls({'snapshot': fx['snapshot'], 'epoch': fx.get('epoch'), 'complete': fx.get('complete', False)})
        for e in fx['entities']:
            model.entities[e['id']] = {'kind': e['kind'], 'name': fx['strings'][e['name_sid']], 'container': e.get('container')}
        model.relations = Counter((r['subject'], r['relation'], r['object']) for r in fx['relations'])
        model.evidence = Counter(tuple(e[f] for f in EVIDENCE_FIELDS) for e in fx['evidence'])
        return model

    @classmethod
    def empty(cls, context):
        return cls(context)

    def copy(self):
        clone = SessionModel(self.context, copy.deepcopy(self.entities), self.relations.copy(), self.evidence.copy())
        clone.generation = self.generation
        return clone

    # -- logical state -----------------------------------------------------
    def canonical_state(self):
        entities = [{'id': i, 'kind': e['kind'], 'name': e['name'], 'container': e['container']}
                    for i, e in sorted(self.entities.items())]
        relations = [{'subject': s, 'relation': r, 'object': o} for (s, r, o), n in sorted(self.relations.items()) for _ in range(n)]
        key = lambda row: (row[1], row[2], row[3], row[0], row[7], row[4], row[5], row[6])   # noqa: E731
        evidence = [dict(zip(EVIDENCE_FIELDS, row)) for row, n in sorted(self.evidence.items(), key=lambda kv: key(kv[0])) for _ in range(n)]
        return {'snapshot': self.context['snapshot'], 'epoch': self.context['epoch'], 'complete': self.context['complete'],
                'entities': entities, 'relations': relations, 'evidence': evidence}

    def state_digest(self):
        payload = canon_bytes(self.canonical_state())
        return 'sha256:' + hashlib.sha256(payload).hexdigest(), len(payload)

    def to_fixture(self):
        """A fixture equal to this state, for the classic oracle (interning layout is arbitrary)."""
        strings, sid, entities = [], {}, []
        for i, e in sorted(self.entities.items()):
            if e['name'] not in sid:
                sid[e['name']] = len(strings)
                strings.append(e['name'])
            entities.append({'id': i, 'kind': e['kind'], 'name_sid': sid[e['name']], 'container': e['container']})
        fx = {'schema': 'csl.eval.fixture/v0.1', 'snapshot': self.context['snapshot'], 'strings': strings, 'entities': entities,
              'relations': [{'subject': s, 'relation': r, 'object': o} for (s, r, o), n in sorted(self.relations.items()) for _ in range(n)],
              'evidence': [dict(zip(EVIDENCE_FIELDS, row)) for row, n in sorted(self.evidence.items()) for _ in range(n)],
              'complete': self.context['complete']}
        if self.context['epoch'] is not None:
            fx['epoch'] = self.context['epoch']
        return fx

    def query(self, q):
        return execute(self.to_fixture(), q)

    def query_bytes(self, q):
        return canon_bytes(self.query(q))

    # -- mutation ----------------------------------------------------------
    def mutate(self, batch):
        """Apply one batch atomically; return the new generation or raise SessionError."""
        new = self._applied(batch)
        self.entities, self.relations, self.evidence = new.entities, new.relations, new.evidence
        self.generation += 1
        return self.generation

    def _applied(self, batch):
        if not isinstance(batch, list):
            raise SessionError('INVALID_REQUEST', 'batch must be a list')
        if not batch:
            raise _bad_input('empty batch')
        entity_ops, rel_add, rel_rem, ev_add, ev_rem = {}, Counter(), Counter(), Counter(), Counter()
        for op in batch:
            if not isinstance(op, dict) or op.get('op') not in OPS:
                raise SessionError('INVALID_REQUEST', 'unknown or malformed operation')
            kind = op['op']
            if kind in ENTITY_FIELDS:
                if set(op) - ENTITY_FIELDS[kind] or not {'op', 'id'} <= set(op):
                    raise SessionError('INVALID_REQUEST', f'{kind} fields')
                if kind == 'ADD_ENTITY' and not {'kind', 'name'} <= set(op):
                    raise SessionError('INVALID_REQUEST', 'ADD_ENTITY needs kind and name')
                if kind == 'UPDATE_ENTITY' and not isinstance(op.get('set'), dict):
                    raise SessionError('INVALID_REQUEST', 'UPDATE_ENTITY needs set')
                if type(op['id']) is not int:
                    raise _bad_input('entity id must be an integer')       # bool is not an id
                if op['id'] in entity_ops:
                    raise _bad_input('two entity operations for one id')
                entity_ops[op['id']] = op
            elif kind in ('ADD_RELATION', 'REMOVE_RELATION'):
                if set(op) != {'op', *RELATION_FIELDS}:
                    raise SessionError('INVALID_REQUEST', f'{kind} fields')
                row = {f: op[f] for f in RELATION_FIELDS}
                if not _valid(_RELATION, row):
                    raise _bad_input('invalid relation row')
                (rel_add if kind == 'ADD_RELATION' else rel_rem)[tuple(row[f] for f in RELATION_FIELDS)] += 1
            else:
                if set(op) != {'op', *EVIDENCE_FIELDS}:
                    raise SessionError('INVALID_REQUEST', f'{kind} fields')
                row = {f: op[f] for f in EVIDENCE_FIELDS}
                if not _valid(_EVIDENCE, row):
                    raise _bad_input('invalid evidence row')
                (ev_add if kind == 'ADD_EVIDENCE' else ev_rem)[tuple(row[f] for f in EVIDENCE_FIELDS)] += 1
        if set(rel_add) & set(rel_rem) or set(ev_add) & set(ev_rem):
            raise _bad_input('a value is both added and removed in one batch')
        entities = copy.deepcopy(self.entities)
        for i, op in entity_ops.items():
            if op['op'] == 'ADD_ENTITY':
                row = {'id': i, 'kind': op['kind'], 'name_sid': 0, 'container': op.get('container')}
                if i in self.entities or not isinstance(op['name'], str) or not _valid(_ENTITY, row):
                    raise _bad_input('invalid ADD_ENTITY')
                entities[i] = {'kind': op['kind'], 'name': op['name'], 'container': op.get('container')}
            elif op['op'] == 'REMOVE_ENTITY':
                if i not in self.entities:
                    raise _bad_input('REMOVE_ENTITY of a missing id')
                del entities[i]
            else:
                changes = op['set']
                if i not in self.entities or not changes or set(changes) - {'kind', 'name', 'container'}:
                    raise _bad_input('invalid UPDATE_ENTITY')
                merged = {**entities[i], **changes}
                row = {'id': i, 'kind': merged['kind'], 'name_sid': 0, 'container': merged['container']}
                if not isinstance(merged['name'], str) or not _valid(_ENTITY, row):
                    raise _bad_input('invalid UPDATE_ENTITY values')
                entities[i] = merged
        relations, evidence = self.relations.copy(), self.evidence.copy()
        for rows, removals, counter in ((rel_rem, rel_rem, relations), (ev_rem, ev_rem, evidence)):
            for value, n in removals.items():
                if counter[value] < n:
                    raise _bad_input('removing more occurrences than exist')
        relations.update(rel_add); relations.subtract(rel_rem)
        evidence.update(ev_add); evidence.subtract(ev_rem)
        relations = +relations
        evidence = +evidence
        for (s, _, o) in relations:
            if s not in entities or o not in entities:
                raise _bad_input('relation references a missing entity')
        for row in evidence:
            if row[1] not in entities or row[3] not in entities:
                raise _bad_input('evidence references a missing entity')
        for e in entities.values():
            if e['container'] is not None and e['container'] not in entities:
                raise _bad_input('container references a missing entity')
        result = SessionModel(self.context, entities, relations, evidence)
        result.generation = self.generation
        return result

    # -- restore -----------------------------------------------------------
    def restore_from(self, other):
        """Replace content with `other`'s logical content; context must match; generation +1."""
        if other.context != self.context:
            raise _bad_input('context mismatch')
        self.entities, self.relations, self.evidence = copy.deepcopy(other.entities), other.relations.copy(), other.evidence.copy()
        self.generation += 1
        return self.generation

    def counts(self):
        return {'entities': len(self.entities), 'relations': sum(self.relations.values()), 'evidence': sum(self.evidence.values()),
                'unique_strings': len({e['name'] for e in self.entities.values()})}
