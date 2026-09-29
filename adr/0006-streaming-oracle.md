# ADR-0006: Streaming, columnar oracle for M-scale campaigns

Status: IMPLEMENTED (equivalence proven at SMOKE and S; not yet exercised at M).

## Problem

`oracle.oracle.PreparedOracle` keeps the fixture as Python dicts. At S scale
(100k entities, 1M relations, 1M evidence rows, 210 MB JSON) that already costs
~2 GB; M (10x) needs tens of GB, more than a 16 GB Mac or the hosted runner.
The campaign also parsed every full result and every candidate output as a
Python tree, at M hundreds of MB to GB each, once per sample.

## Decision

* `oracle/compact.py` is a second oracle implementing the identical contract
  ([SEMANTICS.md](../oracle/SEMANTICS.md)) with stdlib only (no numpy): chunked
  fixture reader, per-record schema validation, `array` columns, CSR adjacency
  ordered exactly as the classic oracle orders its buckets, evidence pre-sorted
  once by the full result key, and results written as canonical JSON while the
  SHA-256 is computed over the same canonical payload bytes.
* `oracle/oracle.py` is unchanged and remains the independent reference.
  `tests/test_compact_oracle.py` requires byte-identical results (and digests)
  on every semantic case, shuffled/reformatted fixtures, 7-byte read chunks,
  generated mixed/cycle/fanout graphs, all malformed cases and invalid JSON.
  Also checked byte-for-byte on the 100k/1M S corpus for all nine W1/W2 queries.
* `harness/campaign.py` builds references with the compact oracle and verifies
  candidate stdout by streaming byte comparison against the canonical reference
  (raw result, or the `"result":{...}` span of a profile envelope). This is
  stricter than the previous tree equality (it also pins key order, number
  spelling and types). If bytes differ and both sides are under 128 MiB the old
  tolerant tree comparison still runs; above that a mismatch is rejected.

## Measured (S corpus, this Mac)

Classic oracle: ~50 s load, ~2 GB RSS. Compact: ~55 s for load plus all nine
references, **286 MB peak RSS**, queries 0.1-1.0 s. Extrapolates to roughly
3 GB and ~10 min at M.

## Limits and honesty

* Reference *result schema* validation is now on the envelope plus a sample of
  propositions (first 64, then every 1024th), not every row. Rows are built from
  fixture records that were each validated, so this checks the skeleton, not an
  independent re-derivation.
* Per-record `jsonschema` validation costs ~28 us/record (~10 min at M); kept
  for fidelity to the schema rather than replaced by hand-written checks.
* Equivalence at M itself is not established by comparison (the classic oracle
  cannot run there); it rests on SMOKE/S/shape equality plus the algorithm being
  size-independent.
