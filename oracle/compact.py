#!/usr/bin/env python3
"""Streaming, columnar oracle with the same semantics as oracle.oracle.

oracle.oracle stays the small, obviously-correct reference (one dict per
record). This module exists so the oracle can prepare M-scale fixtures
(1M entities, 10M relations, 10M evidence rows) inside a bounded, ~GB-scale
memory budget instead of tens of GB of Python objects:

* the fixture is read in chunks and decoded one record at a time, every record
  validated against the same JSON schema the classic oracle uses;
* records live in stdlib `array` columns and CSR adjacency, not dicts;
* results are written incrementally as canonical JSON (sorted keys, compact
  separators, ensure_ascii=False) while the SHA-256 digest is computed over the
  exact canonical payload bytes, so no result tree is ever materialized.

Equivalence with oracle.oracle is enforced by tests/test_compact_oracle.py.
"""
from array import array
from collections import deque
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from types import GeneratorType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jsonschema
from oracle.validation import VALIDATORS

_FIXTURE_SCHEMA = VALIDATORS['fixture'].schema
_PROPS = _FIXTURE_SCHEMA['properties']
_ARRAYS = ('strings', 'entities', 'relations', 'evidence')
_ITEM = {name: jsonschema.Draft202012Validator(_PROPS[name]['items']) for name in _ARRAYS}
_ENT_KINDS = _PROPS['entities']['items']['properties']['kind']['enum']
# Orderings the result contract sorts by are plain string comparisons; ranks
# over the schema enums (sorted) preserve them exactly.
_RELS = sorted(_PROPS['relations']['items']['properties']['relation']['enum'])
_POLS = sorted(_PROPS['evidence']['items']['properties']['polarity']['enum'])
_QUALS = sorted(_PROPS['evidence']['items']['properties']['quality']['enum'])
QUALITY = {"LEXICAL": 0, "PROBABLE": 1, "DERIVED": 2, "EXACT": 3, "VERIFIED": 4}
assert set(_QUALS) == set(QUALITY)
_REL_CODE = {name: i for i, name in enumerate(_RELS)}
_POL_CODE = {name: i for i, name in enumerate(_POLS)}
_QUAL_CODE = {name: i for i, name in enumerate(_QUALS)}
_KIND_CODE = {name: i for i, name in enumerate(_ENT_KINDS)}
_QUALITY_OF_CODE = [QUALITY[name] for name in _QUALS]
_ROW = (b'{"freshness_epoch":%d,"lineage":%d,"object":%d,"polarity":"%s","proposition":%d,'
        b'"quality":"%s","relation":"%s","subject":%d}')
_WS = re.compile(r'[ \t\n\r]*')
_DECODER = json.JSONDecoder()
_BATCH = 20000
_MAX_RANK = 1 << 32


class _Reader:
    """Incremental decoder of one JSON object whose values may be huge arrays."""

    def __init__(self, path, chunk=1 << 23):
        self.stream = open(path, 'r', encoding='utf-8')
        self.chunk, self.buf, self.pos, self.eof = chunk, '', 0, False

    def close(self):
        self.stream.close()

    def _fill(self):
        block = self.stream.read(self.chunk)
        if not block:
            self.eof = True
            return False
        self.buf = self.buf[self.pos:] + block
        self.pos = 0
        return True

    def _peek(self):
        while True:
            self.pos = _WS.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._fill():
                return ''

    def _expect(self, char):
        if self._peek() != char:
            raise ValueError(f'invalid fixture JSON: expected {char!r}')
        self.pos += 1

    def _value(self):
        if not self._peek():
            raise ValueError('invalid fixture JSON: unexpected end')
        while True:
            try:
                value, end = _DECODER.raw_decode(self.buf, self.pos)
                # A number/literal that touches the buffer end may be truncated.
                if end < len(self.buf) or self.eof:
                    self.pos = end
                    return value
            except json.JSONDecodeError:
                if self.eof:
                    raise ValueError('invalid fixture JSON') from None
            if not self._fill():
                self.eof = True

    def _items(self):
        """Yield elements of the array whose '[' is next; must be consumed fully."""
        self._expect('[')
        if self._peek() == ']':
            self.pos += 1
            return
        while True:
            yield self._value()
            char = self._peek()
            self.pos += 1
            if char == ']':
                return
            if char != ',':
                raise ValueError('invalid fixture JSON: expected , or ]')

    def members(self):
        """Yield (key, value-or-item-generator) for the top-level object."""
        self._expect('{')
        if self._peek() == '}':
            self.pos += 1
            return
        while True:
            key = self._value()
            if not isinstance(key, str):
                raise ValueError('invalid fixture JSON: key must be a string')
            self._expect(':')
            if key in _ARRAYS and self._peek() == '[':
                yield key, self._items()
            else:
                yield key, self._value()
            char = self._peek()
            self.pos += 1
            if char == '}':
                break
            if char != ',':
                raise ValueError('invalid fixture JSON: expected , or }')
        if self._peek():
            raise ValueError('invalid fixture JSON: trailing data')


def _validated(name, items):
    check = _ITEM[name].iter_errors
    for item in items:
        for error in check(item):
            raise jsonschema.ValidationError(error.message)
        yield item


class CompactOracle:
    """Validate and index one fixture file; queries stream results to disk."""

    def __init__(self, path):
        reader = _Reader(path)
        scalars = {}      # top-level keys; arrays are recorded as [] once fully validated
        strings = None
        ent = []          # (id, kind_code, name_sid, container)
        rel_s, rel_o, rel_r = array('Q'), array('Q'), array('B')
        ev_s, ev_o, ev_p = array('Q'), array('Q'), array('Q')
        ev_e, ev_l = array('Q'), array('I')
        ev_r, ev_pol, ev_q = array('B'), array('B'), array('B')
        try:
            for key, value in reader.members():
                if key in scalars:
                    raise ValueError(f'duplicate fixture key {key}')
                scalars[key] = [] if isinstance(value, GeneratorType) else value
                if key == 'strings':
                    strings = list(_validated('strings', value))
                elif key == 'entities':
                    for e in _validated('entities', value):
                        ent.append((e['id'], _KIND_CODE[e['kind']], e['name_sid'], e.get('container')))
                elif key == 'relations':
                    for r in _validated('relations', value):
                        rel_s.append(r['subject']); rel_o.append(r['object']); rel_r.append(_REL_CODE[r['relation']])
                elif key == 'evidence':
                    for e in _validated('evidence', value):
                        ev_s.append(e['subject']); ev_o.append(e['object']); ev_p.append(e['proposition'])
                        ev_e.append(e['freshness_epoch']); ev_l.append(e['lineage'])
                        ev_r.append(_REL_CODE[e['relation']]); ev_pol.append(_POL_CODE[e['polarity']])
                        ev_q.append(_QUAL_CODE[e['quality']])
        finally:
            reader.close()
        # Whole-object schema: required keys, unknown keys, scalar types. Arrays are
        # replaced by [] because every item was already validated while streaming.
        VALIDATORS['fixture'].validate(scalars)
        self.snapshot = scalars['snapshot']
        self.complete = scalars.get('complete', False)
        self.strings = strings
        self._finish(ent, (rel_s, rel_o, rel_r), (ev_s, ev_o, ev_p, ev_e, ev_l, ev_r, ev_pol, ev_q))
        self._names = None
        self.last_traversal_steps = None

    def _finish(self, ent, rel, ev):
        ent.sort(key=lambda row: row[0])
        n = len(ent)
        if n >= _MAX_RANK:
            raise ValueError('compact oracle supports fewer than 2**32 entities')
        rank = {row[0]: i for i, row in enumerate(ent)}
        if len(rank) != n:
            raise ValueError('duplicate entity id')
        self.n = n
        self.ent_id = array('Q', (row[0] for row in ent))
        self.ent_kind = bytearray(row[1] for row in ent)
        self.ent_name = array('I', (row[2] for row in ent))
        if any(sid >= len(self.strings) for sid in self.ent_name) or \
                any(row[3] is not None and row[3] not in rank for row in ent):
            raise ValueError('invalid entity reference')
        self.rank = rank
        rel_s, rel_o, rel_r = rel
        try:
            rs, ro = [rank[x] for x in rel_s], [rank[x] for x in rel_o]
            es, eo = [rank[x] for x in ev[0]], [rank[x] for x in ev[1]]
        except KeyError:
            raise ValueError('invalid entity reference') from None
        self.out = self._csr(n, ((s << 40) | (r << 32) | o for s, r, o in zip(rs, rel_r, ro)))
        self.inc = self._csr(n, ((o << 40) | (s << 8) | r for s, r, o in zip(rs, rel_r, ro)), incoming=True)
        del rs, ro
        _, _, ev_p, ev_e, ev_l, ev_r, ev_pol, ev_q = ev
        # (subject, relation, object, proposition, lineage, polarity, quality, epoch)
        keys = [(((((((((s << 3 | r) << 32 | o) << 64 | p) << 32 | l) << 1 | pol) << 3 | q) << 64) | e))
                for s, r, o, p, l, pol, q, e in zip(es, ev_r, eo, ev_p, ev_l, ev_pol, ev_q, ev_e)]
        del es, eo
        keys.sort()
        self.m = len(keys)
        cols = [array('I'), array('B'), array('I'), array('Q'), array('I'), array('B'), array('B'), array('Q')]
        for key in keys:
            key, e = key >> 64, key & 0xFFFFFFFFFFFFFFFF
            key, q = key >> 3, key & 7
            key, pol = key >> 1, key & 1
            key, l = key >> 32, key & 0xFFFFFFFF
            key, p = key >> 64, key & 0xFFFFFFFFFFFFFFFF
            key, o = key >> 32, key & 0xFFFFFFFF
            s, r = key >> 3, key & 7
            cols[0].append(s); cols[1].append(r); cols[2].append(o); cols[3].append(p)
            cols[4].append(l); cols[5].append(pol); cols[6].append(q); cols[7].append(e)
        del keys
        (self.ev_s, self.ev_r, self.ev_o, self.ev_p, self.ev_l, self.ev_pol, self.ev_q, self.ev_e) = cols

    @staticmethod
    def _csr(n, keys, incoming=False):
        """CSR adjacency over sorted packed keys: (offsets, relation code, other endpoint rank)."""
        packed = sorted(keys)
        offsets = array('I', bytes(4 * (n + 1)))
        rels, other = array('B'), array('I')
        for key in packed:
            head = key >> 40
            offsets[head + 1] += 1
            if incoming:   # key = head<<40 | subject<<8 | relation
                rels.append(key & 0xFF)
                other.append((key >> 8) & 0xFFFFFFFF)
            else:          # key = head<<40 | relation<<32 | object
                rels.append((key >> 32) & 0xFF)
                other.append(key & 0xFFFFFFFF)
        for i in range(n):
            offsets[i + 1] += offsets[i]
        return offsets, rels, other

    # -- query evaluation ------------------------------------------------
    def _name_index(self):
        if self._names is None:
            names = {}
            for r in range(self.n):
                names.setdefault(self.strings[self.ent_name[r]], []).append(r)
            self._names = names
        return self._names

    def _evaluate(self, q):
        truncated = False
        n, ent_kind = self.n, self.ent_kind

        def ev(node):
            nonlocal truncated
            op = node['op']
            if op == 'RESOLVE':
                if 'name' in node:
                    return set(self._name_index().get(node['name'], ()))
                r = self.rank.get(node['entity_id'])
                return {r} if r is not None else set()
            if op == 'RELATED':
                base = ev(node['input']); rel = node.get('relation'); direction = node.get('direction', 'OUT')
                offsets, rels, other = self.out if direction == 'OUT' else self.inc
                code = _REL_CODE.get(rel) if rel else None
                got = set()
                if rel and code is None:
                    return got
                for x in base:
                    for i in range(offsets[x], offsets[x + 1]):
                        if code is None or rels[i] == code:
                            got.add(other[i])
                return got
            if op == 'TRAVERSE':
                base = ev(node['input']); rels_f = set(node.get('relations', [])); direction = node.get('direction', 'OUT')
                depth = node.get('max_depth', 1); cap = node.get('max_paths', 100000)
                offsets, rels, other = self.out if direction == 'OUT' else self.inc
                allowed = {_REL_CODE[r] for r in rels_f if r in _REL_CODE}
                seen = set(base); result = set(); dq = deque((x, 0) for x in sorted(base)); steps = 0
                while dq and steps < cap:
                    x, d = dq.popleft()
                    if d >= depth:
                        continue
                    for i in range(offsets[x], offsets[x + 1]):
                        if rels_f and rels[i] not in allowed:
                            continue
                        y = other[i]; steps += 1
                        if y not in seen:
                            seen.add(y); result.add(y); dq.append((y, d + 1))
                        if steps >= cap:
                            truncated = True
                            break
                self.last_traversal_steps = steps
                return result
            if op == 'FILTER':
                base = ev(node['input']) if 'input' in node else range(n)
                kind = node.get('kind')
                code = _KIND_CODE.get(kind) if kind else None
                if kind and code is None:
                    return set()
                return {x for x in base if code is None or ent_kind[x] == code}
            raise ValueError(f'unsupported op {op}')

        return ev(q), truncated

    def traversal_steps(self, q):
        """Edges the (last) TRAVERSE in `q` inspects; used to place cap-boundary queries."""
        from oracle.validation import validate_query
        validate_query(q)
        self.last_traversal_steps = None
        self._evaluate(q)
        if self.last_traversal_steps is None:
            raise ValueError('query has no TRAVERSE')
        return self.last_traversal_steps

    def name_lookup(self, names, rounds=1):
        """Expected W4 lookup block: digest over round 0 and total ids over all rounds.

        Digest bytes per name, in order: b'[' + ascending ids comma-joined + b']\\n'.
        """
        index = self._name_index()
        h = hashlib.sha256()
        total = 0
        for name in names:
            ids = [int(self.ent_id[r]) for r in index.get(name, ())]
            h.update(b'[' + b','.join(b'%d' % i for i in ids) + b']\n')
            total += len(ids)
        return {'lookup_digest': 'sha256:' + h.hexdigest(), 'lookup_ids_total': total * rounds,
                'unique_strings': len({self.strings[self.ent_name[r]] for r in range(self.n)}),
                'lookups': len(names) * rounds}

    def execute_to(self, q, target):
        """Evaluate `q`, write the canonical result JSON to `target`; return its summary."""
        from oracle.validation import validate_query
        validate_query(q)
        selected, truncated = self._evaluate(q)
        ranks = sorted(selected)
        mask = bytearray(self.n)
        for r in ranks:
            mask[r] = 1
        minq = q.get('evidence', {}).get('min_quality')
        epoch = q.get('evidence', {}).get('freshness_epoch')
        minq_v = QUALITY[minq] if minq else None
        ent_id = self.ent_id
        ev_s, ev_o, ev_q, ev_e = self.ev_s, self.ev_o, self.ev_q, self.ev_e
        ev_r, ev_p, ev_l, ev_pol = self.ev_r, self.ev_p, self.ev_l, self.ev_pol
        rel_b = [name.encode() for name in _RELS]; pol_b = [name.encode() for name in _POLS]
        qual_b = [name.encode() for name in _QUALS]
        completeness = 'TRUNCATED' if truncated else 'COMPLETE' if self.complete else 'OBSERVED'
        entities = b','.join(b'%d' % ent_id[r] for r in ranks)
        h = hashlib.sha256(b'{"completeness":{"entity_set":"%s"},"entities":[%s],"knowledge":{"model":"open-world"},"propositions":['
                           % (completeness.encode(), entities))
        count, sample = 0, []
        target = Path(target)
        with tempfile.TemporaryFile() as rows:
            batch = []
            for i in range(self.m):
                if not (mask[ev_s[i]] or mask[ev_o[i]]):
                    continue
                if minq_v is not None and _QUALITY_OF_CODE[ev_q[i]] < minq_v:
                    continue
                if epoch is not None and ev_e[i] != epoch:
                    continue
                batch.append(_ROW % (ev_e[i], ev_l[i], ent_id[ev_o[i]], pol_b[ev_pol[i]], ev_p[i],
                                     qual_b[ev_q[i]], rel_b[ev_r[i]], ent_id[ev_s[i]]))
                if count % 1024 == 0 or count < 64:
                    sample.append(batch[-1])
                count += 1
                if len(batch) >= _BATCH:
                    self._flush(batch, rows, h, count == len(batch))
                    batch = []
            if batch:
                self._flush(batch, rows, h, count == len(batch))
            h.update(b']}')
            digest = 'sha256:' + h.hexdigest()
            prefix = (b'{"completeness":{"entity_set":"%s"},"digest":"%s","entities":[%s],"knowledge":{"model":"open-world"},'
                      b'"propositions":[' % (completeness.encode(), digest.encode(), entities))
            suffix = (b'],"query_id":%s,"schema":"csl.eval.result/v0.1","snapshot":%s}' % (
                json.dumps(q['query_id'], ensure_ascii=False).encode(), json.dumps(self.snapshot, ensure_ascii=False).encode()))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as out:
                out.write(prefix)
                rows.seek(0)
                for block in iter(lambda: rows.read(1 << 22), b''):
                    out.write(block)
                out.write(suffix)
        skeleton = json.loads(prefix + b','.join(sample) + suffix)
        VALIDATORS['result'].validate(skeleton)
        return {'digest': digest, 'entities': len(ranks), 'propositions': count, 'bytes': target.stat().st_size}

    @staticmethod
    def _flush(batch, rows, h, first):
        data = b','.join(batch)
        if not first:
            data = b',' + data
        h.update(data)
        rows.write(data)

    def execute(self, q):
        """Materialized result (tests / small fixtures only)."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'result.json'
            self.execute_to(q, path)
            return json.loads(path.read_bytes())


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', required=True); p.add_argument('--query', required=True); p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(CompactOracle(a.fixture).execute_to(json.load(open(a.query)), a.output)))
