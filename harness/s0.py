#!/usr/bin/env python3
"""S0 session conformance runner (host side).

Drives a candidate's `session` process over the JSONL binding (oracle/SESSION-BINDING-JSONL.md),
replays deterministic scenario scripts, and compares every observable outcome with the independent
oracle model (oracle/session_model.py): success/error code, generation, `state_digest`, and query
result bytes. Conformance is a hard gate; timing here is informational only.

    python harness/s0.py --candidate rust --candidate zig [--fuzz 200] [--json OUT]
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.generate import generate
from harness.benchctl import executable, queries
from oracle.oracle import canon_bytes
from oracle.session_model import EVIDENCE_FIELDS, SessionError, SessionModel

BINDING = 'csl.eval.session.jsonl/v0'
CODES = {'INVALID_REQUEST', 'INVALID_STATE', 'INVALID_INPUT', 'UNSUPPORTED', 'CANCELLED', 'LIMIT_EXCEEDED', 'INTERNAL'}


class ProtocolFault(Exception):
    """A transport-level violation (framing, canonical form, ordering), not a semantic outcome."""


class Response:
    def __init__(self, message, payload, ns):
        self.message, self.payload, self.ns = message, payload, ns

    ok = property(lambda self: self.message.get('ok') is True)
    code = property(lambda self: self.message.get('code'))
    generation = property(lambda self: self.message.get('generation'))

    def result_bytes(self):
        if self.payload is not None:
            return self.payload
        return canon_bytes(self.message['result'])


def _has_float(value):
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_float(v) for v in value)
    return False


class Client:
    """One candidate `session` process."""

    def __init__(self, exe, repository, max_line_bytes=1 << 20, request_chunk=None):
        self.exe, self.repository = str(exe), str(repository)
        self.max_line_bytes, self.request_chunk = max_line_bytes, request_chunk
        self.errors = tempfile.TemporaryFile()
        self.started = time.perf_counter_ns()
        self.proc = subprocess.Popen([self.exe, 'session', '--repository', self.repository], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.errors, bufsize=1 << 20)   # unbuffered readline() reads byte by byte
        self.next_id = 1
        self.opened = None

    # -- transport ---------------------------------------------------------
    def _write(self, message):
        line = canon_bytes(message) + b'\n'
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_message(self):
        line = self.proc.stdout.readline()
        if not line:
            self.errors.seek(0)
            raise ProtocolFault(f'process closed stdout; stderr={self.errors.read(600)!r}')
        if not line.endswith(b'\n'):
            raise ProtocolFault('response line not newline-terminated')
        body = line[:-1]
        message = json.loads(body)
        if canon_bytes(message) != body and not _has_float(message):
            raise ProtocolFault('response line is not canonical JSON')   # floats may print differently per language
        return message

    def _send_request(self, message):
        body = canon_bytes(message)
        limit = self.request_chunk
        if limit is None or len(body) <= limit:
            self._write(message)
            return
        head = {k: message[k] for k in ('id', 'op')}
        rest = canon_bytes({k: v for k, v in message.items() if k not in head})
        self._write({**head, 'frame': 'begin'})
        chunks = [rest[i:i + limit] for i in range(0, len(rest), limit)]
        for seq, chunk in enumerate(chunks):
            self._write({'id': head['id'], 'frame': 'chunk', 'seq': seq, 'data': base64.b64encode(chunk).decode()})
        self._write({'id': head['id'], 'frame': 'end', 'chunks': len(chunks), 'bytes': len(rest),
                     'sha256': hashlib.sha256(rest).hexdigest()})

    def call(self, op, **fields):
        request = {'id': self.next_id, 'op': op, **fields}
        self.next_id += 1
        start = time.perf_counter_ns()
        self._send_request(request)
        message = self._read_message()
        payload = None
        if message.get('frame') == 'begin':
            parts, hasher = [], hashlib.sha256()
            while True:
                frame = self._read_message()
                if frame.get('id') != request['id']:
                    raise ProtocolFault('interleaved frame')
                kind = frame.get('frame')
                if kind == 'chunk':
                    if frame['seq'] != len(parts):
                        raise ProtocolFault('chunk seq gap')
                    data = base64.b64decode(frame['data'], validate=True)
                    parts.append(data)
                    hasher.update(data)
                elif kind == 'end':
                    total = sum(len(p) for p in parts)
                    if frame['chunks'] != len(parts) or frame['bytes'] != total or frame['sha256'] != hasher.hexdigest():
                        raise ProtocolFault('chunk count/length/hash mismatch')
                    payload = b''.join(parts)
                    message = {k: v for k, v in frame.items() if k not in ('frame', 'chunks', 'bytes', 'sha256')}
                    break
                elif kind == 'abort':
                    message = {k: v for k, v in frame.items() if k != 'frame'}
                    break
                else:
                    raise ProtocolFault('unexpected frame')
        if message.get('id') != request['id']:
            raise ProtocolFault(f"response id {message.get('id')} for request {request['id']}")
        if message.get('ok') is False and message.get('code') not in CODES:
            raise ProtocolFault(f"unknown error code {message.get('code')!r}")
        return Response(message, payload, time.perf_counter_ns() - start)

    def open_fixture(self, path):
        response = self.call('open', binding=BINDING, max_line_bytes=self.max_line_bytes,
                             source={'kind': 'fixture', 'path': str(path)})
        self.opened = response
        return response

    def open_empty(self, context):
        response = self.call('open', binding=BINDING, max_line_bytes=self.max_line_bytes,
                             source={'kind': 'empty', 'context': context})
        self.opened = response
        return response

    def close(self):
        try:
            if self.proc.poll() is None:
                self.call('close')
        except (ProtocolFault, BrokenPipeError, OSError):
            pass
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            self.proc.wait(timeout=30)


# -- fixtures ---------------------------------------------------------------
def special_fixture():
    """Names that need escaping, a shared name, unreferenced strings and a container link."""
    names = ['plain', 'quote"back\\slash', 'ctl\x01\x1f\t\n', 'unicode-é-漢字-😀', 'plain', 'unused-1', 'unused-2']
    return {'schema': 'csl.eval.fixture/v0.1', 'snapshot': 'SP', 'epoch': 3, 'complete': True, 'strings': names,
            'entities': [{'id': 1, 'kind': 'TYPE', 'name_sid': 0, 'container': None},
                         {'id': 2, 'kind': 'METHOD', 'name_sid': 1, 'container': 1},
                         {'id': 3, 'kind': 'FIELD', 'name_sid': 2, 'container': 1},
                         {'id': 4, 'kind': 'FUNCTION', 'name_sid': 3, 'container': None},
                         {'id': 5, 'kind': 'MODULE', 'name_sid': 4, 'container': None},
                         {'id': 2**64 - 1, 'kind': 'VARIABLE', 'name_sid': 0, 'container': None}],
            'relations': [{'subject': 1, 'relation': 'CONTAINS', 'object': 2}, {'subject': 1, 'relation': 'CONTAINS', 'object': 2},
                          {'subject': 2, 'relation': 'CALLS', 'object': 4}, {'subject': 4, 'relation': 'CALLS', 'object': 2**64 - 1}],
            'evidence': [dict(zip(EVIDENCE_FIELDS, (1, 1, 'CONTAINS', 2, 'POSITIVE', 'EXACT', 1, 0))),
                         dict(zip(EVIDENCE_FIELDS, (1, 1, 'CONTAINS', 2, 'POSITIVE', 'EXACT', 1, 0))),
                         dict(zip(EVIDENCE_FIELDS, (2, 2, 'CALLS', 4, 'NEGATIVE', 'PROBABLE', 2, 7)))]}


def relabelled(fixture):
    """The same logical state with a different string table layout (extra unused strings, reversed order)."""
    fx = json.loads(json.dumps(fixture))
    used = [fx['strings'][e['name_sid']] for e in fx['entities']]
    table = ['zzz-unused', *reversed(sorted(set(used))), 'aaa-unused']
    fx['strings'] = table
    for e, name in zip(fx['entities'], used):
        e['name_sid'] = table.index(name)
    return fx


# -- scenario helpers ------------------------------------------------------------
class Report:
    def __init__(self, candidate):
        self.candidate, self.results = candidate, []

    def check(self, name, condition, detail=''):
        self.results.append({'name': name, 'pass': bool(condition), 'detail': '' if condition else str(detail)[:400]})
        return bool(condition)

    @property
    def failed(self):
        return [r for r in self.results if not r['pass']]


def context_of(fixture):
    return {'snapshot': fixture['snapshot'], 'epoch': fixture.get('epoch'), 'complete': fixture.get('complete', False)}


def expected_outcome(model, batch):
    trial = model.copy()
    try:
        trial.mutate(batch)
        return 'ok', trial
    except SessionError as error:
        return error.code, None


def sync_check(report, label, client, model, query_set, digest=True):
    """Compare digest and a set of query results between candidate and model."""
    if digest:
        response = client.call('state_digest')
        want, size = model.state_digest()
        report.check(f'{label}: state_digest', response.ok and response.message.get('state_digest') == want,
                     (response.message, want))
        if response.ok:
            report.check(f'{label}: state_digest reports cost fields',
                         isinstance(response.message.get('state_digest_ms'), (int, float))
                         and response.message.get('bytes_processed') in (None, size), response.message)
            report.check(f'{label}: generation', response.generation == model.generation, (response.generation, model.generation))
    for q in query_set:
        response = client.call('query', query=q)
        want = model.query_bytes(q)
        report.check(f'{label}: query {q["query_id"]}', response.ok and response.result_bytes() == want,
                     response.message if not response.ok else 'result bytes differ')


def std_queries(model, count=8):
    ids = sorted(model.entities)
    if not ids:
        return []
    first = ids[0]
    base = {'op': 'RESOLVE', 'entity_id': first}
    q = lambda qid, op, **kw: {'schema': 'csl.eval.query/v0.1', 'query_id': qid, 'op': op, **kw}     # noqa: E731
    names = sorted({e['name'] for e in model.entities.values()})
    out = [q('resolve', 'RESOLVE', entity_id=first), q('missing', 'RESOLVE', entity_id=2**64 - 2),
           q('scan', 'FILTER', kind='TYPE'), q('out', 'RELATED', input=base), q('in', 'RELATED', input=base, direction='IN'),
           q('trav', 'TRAVERSE', input=base, max_depth=4, max_paths=1000),
           q('name', 'RESOLVE', name=names[0]),
           q('evid', 'FILTER', evidence={'min_quality': 'EXACT'})]
    return out[:count]


def evidence_op(kind, row):
    return {'op': kind, **dict(zip(EVIDENCE_FIELDS, row))}


def random_batch(rng, model, invalid_rate=0.25):
    """A random batch over the model's current state; some batches are deliberately invalid."""
    ids = sorted(model.entities)
    fresh = max(ids, default=0) + 1 + rng.randrange(3)
    ops = []
    for _ in range(rng.randint(1, 6)):
        kind = rng.choice(['ADD_ENTITY', 'UPDATE_ENTITY', 'ADD_RELATION', 'REMOVE_RELATION', 'ADD_EVIDENCE', 'REMOVE_EVIDENCE', 'REMOVE_ENTITY'])
        if kind == 'ADD_ENTITY':
            ops.append({'op': kind, 'id': fresh, 'kind': rng.choice(['TYPE', 'METHOD', 'FIELD']), 'name': f'gen{rng.randrange(4)}',
                        'container': rng.choice([None] + ids[:2])})
            fresh += 1
        elif kind == 'UPDATE_ENTITY' and ids:
            ops.append({'op': kind, 'id': rng.choice(ids), 'set': rng.choice([{'name': f'ren{rng.randrange(3)}'}, {'kind': 'FUNCTION'}, {'container': None}])})
        elif kind in ('ADD_RELATION', 'REMOVE_RELATION') and ids:
            if kind == 'REMOVE_RELATION' and model.relations and rng.random() < .7:
                s, r, o = rng.choice(sorted(model.relations))
            else:
                s, r, o = rng.choice(ids), rng.choice(['CALLS', 'REFERENCES']), rng.choice(ids)
            ops.append({'op': kind, 'subject': s, 'relation': r, 'object': o})
        elif kind in ('ADD_EVIDENCE', 'REMOVE_EVIDENCE') and ids:
            if kind == 'REMOVE_EVIDENCE' and model.evidence and rng.random() < .7:
                row = rng.choice(sorted(model.evidence))
            else:
                row = (rng.randint(1, 50), rng.choice(ids), 'CALLS', rng.choice(ids), rng.choice(['POSITIVE', 'NEGATIVE']),
                       rng.choice(['EXACT', 'VERIFIED', 'LEXICAL']), rng.randint(1, 3), rng.randint(0, 3))
            ops.append(evidence_op(kind, row))
        elif kind == 'REMOVE_ENTITY' and ids:
            ops.append({'op': kind, 'id': rng.choice(ids)})
    if rng.random() < invalid_rate and ops:
        ops.append(rng.choice([{'op': 'ADD_RELATION', 'subject': 10**9, 'relation': 'CALLS', 'object': ids[0]} if ids else {'op': 'NOPE'},
                               {'op': 'UPDATE_ENTITY', 'id': ids[0], 'set': {}} if ids else {'op': 'NOPE'}]))
    return ops


# -- scenarios ------------------------------------------------------------------
def run_candidate(name, exe, repository, fuzz=200, seed=20260929):
    report = Report(name)
    small = Path(repository) / 'small.json'
    generate(small, 60, 240, 5, 'mixed')
    small_fx = json.loads(small.read_text())
    special_path = Path(repository) / 'special.json'
    special_path.write_text(json.dumps(special_fixture()))
    relabel_path = Path(repository) / 'relabelled.json'
    relabel_path.write_text(json.dumps(relabelled(special_fixture())))
    repo = Path(repository) / 'repo'
    repo.mkdir()

    def session(**kw):
        return Client(exe, repo, **kw)

    # 1. lifecycle and handshake
    c = session()
    try:
        report.check('lifecycle: query before open is INVALID_STATE', c.call('query', query=std_queries(SessionModel.from_fixture(small_fx))[0]).code == 'INVALID_STATE')
        report.check('lifecycle: state_digest before open is INVALID_STATE', c.call('state_digest').code == 'INVALID_STATE')
        opened = c.open_fixture(small)
        m = opened.message
        report.check('open: fixture ok, generation 0', opened.ok and opened.generation == 0, m)
        report.check('open: handshake fields', m.get('semantics') == 'csl.eval.session/v0.1' and m.get('binding') == BINDING
                     and m.get('capabilities') == [] and m.get('strategy', {}).get('mutation') in ('full-rebuild', 'incremental')
                     and isinstance(m.get('artifact'), str) and isinstance(m.get('chunk_bytes'), int) and isinstance(m.get('max_line_bytes'), int), m)
        report.check('lifecycle: second open is INVALID_STATE', c.open_fixture(small).code == 'INVALID_STATE')
        model = SessionModel.from_fixture(small_fx)
        sync_check(report, 'initial', c, model, std_queries(model))
        report.check('cancel is UNSUPPORTED in the baseline', c.call('cancel', target=1).code == 'UNSUPPORTED')
        report.check('unknown operation is INVALID_REQUEST', c.call('frobnicate').code == 'INVALID_REQUEST')
        stats = c.call('stats')
        counts = model.counts()
        report.check('stats: counts match', stats.ok and all(stats.message['stats'].get(k) == v for k, v in counts.items()), stats.message)
        report.check('stats: schema fields present', stats.ok and {'live_heap_bytes', 'peak_heap_bytes', 'allocations_total', 'generation'} <= set(stats.message['stats']), stats.message)
        closed = c.call('close')
        report.check('close ok', closed.ok, closed.message)
        report.check('lifecycle: process exits after close', c.proc.wait(timeout=30) == 0)
    finally:
        c.close()

    # 2. digest independence of representation
    digests = {}
    for label, path in (('special', special_path), ('relabelled', relabel_path)):
        c = session()
        try:
            c.open_fixture(path)
            digests[label] = c.call('state_digest').message.get('state_digest')
            sm = SessionModel.from_fixture(json.loads(path.read_text()))
            sync_check(report, f'{label} fixture', c, sm, std_queries(sm, 6))
        finally:
            c.close()
    report.check('digest ignores string-table layout and unreferenced strings', digests['special'] == digests['relabelled'] and digests['special'], digests)

    # 3. mutation: each operation, multiset, dependents, atomic failure
    c = session()
    try:
        c.open_fixture(special_path)
        model = SessionModel.from_fixture(special_fixture())
        step = 0

        def attempt(label, batch, valid_queries=True):
            nonlocal step
            before = (model.state_digest()[0], model.generation)
            expect, trial = expected_outcome(model, batch)
            response = c.call('mutate', batch=batch)
            if expect == 'ok':
                report.check(f'mutate {label}: accepted', response.ok, response.message)
                if response.ok:
                    model.mutate(batch)
                    report.check(f'mutate {label}: generation +1', response.generation == model.generation, (response.generation, model.generation))
            else:
                report.check(f'mutate {label}: rejected {expect}', (not response.ok) and response.code == expect, response.message)
                after = c.call('state_digest')
                report.check(f'mutate {label}: rejection leaves digest and generation', after.message.get('state_digest') == before[0] and after.generation == before[1], (after.message, before))
            step += 1
            sync_check(report, f'after {label}', c, model, std_queries(model, 5), digest=(step % 2 == 0 or expect != 'ok'))

        ev = (9, 2, 'CALLS', 4, 'POSITIVE', 'VERIFIED', 1, 3)
        attempt('empty batch', [])
        attempt('add entity', [{'op': 'ADD_ENTITY', 'id': 10, 'kind': 'METHOD', 'name': 'added "q"', 'container': 1}])
        attempt('add existing entity', [{'op': 'ADD_ENTITY', 'id': 10, 'kind': 'METHOD', 'name': 'x', 'container': None}])
        attempt('update entity rename+kind', [{'op': 'UPDATE_ENTITY', 'id': 10, 'set': {'name': 'renamed', 'kind': 'FIELD'}}])
        attempt('update with no fields', [{'op': 'UPDATE_ENTITY', 'id': 10, 'set': {}}])
        attempt('update missing entity', [{'op': 'UPDATE_ENTITY', 'id': 99, 'set': {'name': 'z'}}])
        attempt('add relation duplicate twice', [{'op': 'ADD_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4}] * 2)
        attempt('remove one occurrence', [{'op': 'REMOVE_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4}])
        attempt('remove too many occurrences', [{'op': 'REMOVE_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4}] * 3)
        attempt('add and remove same relation', [{'op': 'ADD_RELATION', 'subject': 3, 'relation': 'REFERENCES', 'object': 4},
                                                 {'op': 'REMOVE_RELATION', 'subject': 3, 'relation': 'REFERENCES', 'object': 4}])
        attempt('add evidence', [evidence_op('ADD_EVIDENCE', ev)])
        attempt('remove evidence', [evidence_op('REMOVE_EVIDENCE', ev)])
        attempt('remove missing evidence', [evidence_op('REMOVE_EVIDENCE', ev)])
        attempt('remove referenced entity', [{'op': 'REMOVE_ENTITY', 'id': 4}])
        attempt('remove referenced container', [{'op': 'REMOVE_ENTITY', 'id': 1}])
        attempt('two ops on one entity', [{'op': 'UPDATE_ENTITY', 'id': 10, 'set': {'name': 'a'}}, {'op': 'REMOVE_ENTITY', 'id': 10}])
        attempt('relation to missing entity', [{'op': 'ADD_RELATION', 'subject': 1, 'relation': 'CALLS', 'object': 777}])
        attempt('relation with entity added in the same batch', [{'op': 'ADD_ENTITY', 'id': 11, 'kind': 'TYPE', 'name': 'n11', 'container': None},
                                                                   {'op': 'ADD_RELATION', 'subject': 11, 'relation': 'CALLS', 'object': 1}])
        attempt('remove entity with dependents in the same batch', [{'op': 'REMOVE_ENTITY', 'id': 11},
                                                                      {'op': 'REMOVE_RELATION', 'subject': 11, 'relation': 'CALLS', 'object': 1}])
        attempt('invalid enum', [{'op': 'ADD_ENTITY', 'id': 12, 'kind': 'BOGUS', 'name': 'n', 'container': None}])
        attempt('unknown op name', [{'op': 'EXPLODE'}])
        attempt('unknown field', [{'op': 'REMOVE_ENTITY', 'id': 10, 'extra': 1}])
        attempt('cross-check remove entity 2 with all dependents', [
            {'op': 'REMOVE_ENTITY', 'id': 2}, {'op': 'REMOVE_ENTITY', 'id': 3},
            {'op': 'REMOVE_RELATION', 'subject': 1, 'relation': 'CONTAINS', 'object': 2},
            {'op': 'REMOVE_RELATION', 'subject': 1, 'relation': 'CONTAINS', 'object': 2},
            {'op': 'REMOVE_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4},
            {'op': 'UPDATE_ENTITY', 'id': 1, 'set': {'container': None}}])
    finally:
        c.close()

    # 4. snapshot / restore, context immutability, cold restore
    c = session()
    snap_id = None
    try:
        c.open_fixture(small)
        model = SessionModel.from_fixture(small_fx)
        first = model.state_digest()[0]
        digest_before = c.call('state_digest').message.get('state_digest')
        report.check('snapshot: digest before snapshot', digest_before == first)
        snap = c.call('snapshot')
        report.check('snapshot ok, opaque id, captured generation', snap.ok and isinstance(snap.message.get('snapshot_id'), str)
                     and snap.message.get('snapshot_id') and snap.message.get('captured_generation') == 0 and snap.generation == 0, snap.message)
        snap_id = snap.message.get('snapshot_id')
        ids = sorted(model.entities)
        batch = [{'op': 'UPDATE_ENTITY', 'id': ids[0], 'set': {'name': 'changed'}}]
        c.call('mutate', batch=batch)
        model.mutate(batch)
        report.check('restore of an unknown snapshot_id is INVALID_INPUT', c.call('restore', snapshot_id='no-such-snapshot').code == 'INVALID_INPUT')
        restored = c.call('restore', snapshot_id=snap_id)
        report.check('restore: accepted and generation advances by exactly one (never rewinds)', restored.ok and restored.generation == 2, restored.message)
        back = c.call('state_digest')
        report.check('restore: state_digest equals the one before snapshot', back.message.get('state_digest') == first, back.message)
        base_model = SessionModel.from_fixture(small_fx)
        base_model.generation = 2
        sync_check(report, 'after restore', c, base_model, std_queries(base_model))
    finally:
        c.close()
    if snap_id:
        cold = session()
        try:
            opened = cold.open_empty(context_of(small_fx))
            report.check('cold restore: open(empty) ok generation 0', opened.ok and opened.generation == 0, opened.message)
            restored = cold.call('restore', snapshot_id=snap_id)
            report.check('cold restore: restore in a fresh process, generation 1', restored.ok and restored.generation == 1, restored.message)
            cold_model = SessionModel.from_fixture(small_fx)
            cold_model.generation = 1
            sync_check(report, 'cold restore', cold, cold_model, std_queries(cold_model))
        finally:
            cold.close()
        other = session()
        try:
            other.open_empty({**context_of(small_fx), 'snapshot': 'DIFFERENT'})
            report.check('restore with a different context is INVALID_INPUT', other.call('restore', snapshot_id=snap_id).code == 'INVALID_INPUT')
            report.check('failed restore changes nothing', other.call('state_digest').generation == 0)
        finally:
            other.close()

    # 5. chunked responses and requests
    big = Path(repository) / 'big.json'
    generate(big, 400, 4000, 9, 'mixed')
    big_fx = json.loads(big.read_text())
    for label, options in (('chunked responses', {'max_line_bytes': 65536}), ('chunked requests and responses', {'max_line_bytes': 65536, 'request_chunk': 4096})):
        c = session(**options)
        try:
            c.open_fixture(big)
            model = SessionModel.from_fixture(big_fx)
            scan = {'schema': 'csl.eval.query/v0.1', 'query_id': 'scan-all', 'op': 'FILTER'}
            response = c.call('query', query=scan)
            report.check(f'{label}: large result is chunked', response.ok and response.payload is not None and len(response.payload) > 65536,
                         (response.message, None if response.payload is None else len(response.payload)))
            report.check(f'{label}: reassembled bytes equal the oracle result', response.ok and response.result_bytes() == model.query_bytes(scan))
            ids = sorted(model.entities)
            batch = [{'op': 'ADD_RELATION', 'subject': ids[i % 50], 'relation': 'REFERENCES', 'object': ids[(i * 7) % 50]} for i in range(600)]
            mutated = c.call('mutate', batch=batch)
            model.mutate(batch)
            report.check(f'{label}: large mutate batch accepted', mutated.ok and mutated.generation == 1, mutated.message)
            sync_check(report, f'{label} after big batch', c, model, std_queries(model, 3))
        finally:
            c.close()

    # 6. seeded fuzz against the model
    rng = random.Random(seed)
    c = session()
    try:
        c.open_fixture(small)
        model = SessionModel.from_fixture(small_fx)
        mismatches = 0
        for i in range(fuzz):
            batch = random_batch(rng, model)
            expect, _ = expected_outcome(model, batch)
            response = c.call('mutate', batch=batch)
            if expect == 'ok':
                good = response.ok
                if good:
                    model.mutate(batch)
                    good = response.generation == model.generation
            else:
                good = (not response.ok) and response.code == expect
            if not good:
                mismatches += 1
                report.check(f'fuzz step {i}: mutate outcome', False, (expect, response.message, batch))
                break
            if i % 10 == 9:
                digest = c.call('state_digest').message.get('state_digest')
                if digest != model.state_digest()[0]:
                    mismatches += 1
                    report.check(f'fuzz step {i}: digest', False, (digest, model.state_digest()[0]))
                    break
                for q in std_queries(model, 3):
                    if c.call('query', query=q).result_bytes() != model.query_bytes(q):
                        mismatches += 1
                        report.check(f'fuzz step {i}: query {q["query_id"]}', False, '')
                        break
        report.check(f'fuzz: {fuzz} seeded batches agree with the model', mismatches == 0)
    finally:
        c.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='append', choices=('rust', 'zig'), required=True)
    parser.add_argument('--fuzz', type=int, default=200)
    parser.add_argument('--json')
    args = parser.parse_args()
    reports = []
    for name in args.candidate:
        with tempfile.TemporaryDirectory(prefix=f's0-{name}-') as directory:
            report = run_candidate(name, executable(name), directory, args.fuzz)
        reports.append({'candidate': name, 'checks': len(report.results), 'failed': report.failed})
        print(json.dumps({'candidate': name, 'checks': len(report.results), 'failed': len(report.failed)}))
        for failure in report.failed[:15]:
            print('  FAIL', failure['name'], failure['detail'])
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2) + '\n')
    sys.exit(1 if any(r['failed'] for r in reports) else 0)


if __name__ == '__main__':
    main()
