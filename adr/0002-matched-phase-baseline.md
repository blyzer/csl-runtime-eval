# Matched typed/hash baseline and phase timing

Date: 2026-09-28. This is an evaluation-method decision, not a runtime selection.

## Representation pass 1

The original Rust JSON-value/ordered-map implementation and Zig typed/hash-map
implementation were not comparable store layouts. Both pure candidates now use:

- typed entity, edge and evidence arrays; enum kinds, relations, qualities and
  polarities; u64 IDs/epochs and u32 string IDs/lineage;
- an ID hash map containing entity records by value;
- separate outgoing/incoming hash maps with vectors of edges by value, sorted by
  `(subject, relation name, object)`;
- hash sets for selection/visited IDs and a vector queue with a moving position;
- sorted traversal seeds and output IDs, and evidence sorted by the semantic
  contract. Enum declaration order is never the canonical string sort order.

Rust changes from JSON records and ordered maps/sets to this layout. Zig already
used it; its reviewed pass retains that representation and separates evaluation
from encoding. Neither receives additional capacity, compressed-adjacency,
custom-hasher or evidence-index tuning in this pass. Both retain the source
fixture, including the unindexed evidence array, and scan evidence per query.

This matches data structures and broad algorithms, not byte layout or every
implementation detail. Rust uses the standard randomized hash implementation,
owned strings, ordinary allocation/deallocation and a JSON query tree. Zig uses
AutoHashMap, typed queries and an arena, retaining temporary allocations until
process exit. Rust constructs JSON values for output; Zig serializes typed
records. These differences remain experimental variables, especially for RSS.

## Optional workload profile

`workload --id W1|W2 --corpus PATH --params '{"query": ...}' --profile`
returns an envelope with `profile_schema: csl.eval.profile/v0.1`,
`representation: typed-hash-v1`, `phases_ns`, and the unchanged semantic `result`.
Ordinary query/workload stdout retains the original result contract. Hybrid
remains a whole-fixture FFI batch and does not advertise these pure-store phases.

| Phase | Measured work |
|---|---|
| `load` | Read fixture bytes and parse typed records, including structural/type validation |
| `index` | Validate schema/cross-references/unique IDs, build ID and adjacency indexes, sort adjacency |
| `query` | Validate the decoded query, select/traverse/filter, scan/filter evidence, sort IDs and evidence |
| `result` | Construct canonical payload, encode/hash it, construct and serialize full semantic result |

Timers use monotonic native clocks in nanoseconds. Zero is a possible observed
clock-resolution result. Each process runs one query. Query parameter decoding,
profile-envelope construction/serialization, stdout, cleanup, process startup
and harness overhead are outside these phases but inside process `elapsed_ns`.
Rust serializes the semantic result once for the result phase and serializes it
again as part of the envelope; that envelope work is outside `result`. Zig embeds
its serialized result directly. The sum of phases must not exceed process elapsed.

A query phase is **not isolated graph traversal or ID lookup**: evidence scanning,
allocation and sorting are included. Index timing includes semantic validation;
it is not an index-only throughput claim. No phase-specific RSS is measured.
Peak RSS includes the whole profiled process and its envelope allocations.
Cold-process does not mean cold OS filesystem caches. No warm-index, incremental
update, cancellation, persistent store, or controlled S/M result is claimed.

## Acceptance and evidence

`benchctl run/compare --profile` accepts only the pure candidates and writes
`results/phases/w1|w2/`, separate from unprofiled records. It validates envelope
version/layout, nonnegative integer timings, phase sum, result schema, and full
oracle equality before accepting records. Source, artifact and corpus hashes,
toolchains, platform and SDK-workaround status accompany records.

`conformance --candidate rust --candidate zig --profile` exercises the profile
path with valid and invalid semantic cases and reversed insertion order. The
normal suite also covers hybrid, whose shared Zig semantic code uses the same
split evaluation/encoding functions. CI runs both suites and profile smoke runs.

Next: repeat this pass on a supported native compiler/SDK pair, then controlled
S-scale repeated runs with fixed machine/load conditions and recorded ordering.
Any further representation tuning must be named and made available to both pure
candidates before drawing comparative conclusions.

## Representation pass 2 — phase decomposition and Rust encode fairness (2026-09-28)

Prompted by the controlled S-scale campaign in [ADR-0003](0003-s-scale-campaign.md),
which showed Rust ~2-2.3x slower and using ~2-2.3x more peak RSS than Zig on
large-result queries (`scan-type`, `depth-8`). Pass 1's `query`/`result` split
already conflated distinct work (line 44-45 above); this pass separates it and
investigates the RSS/time gap's actual source per ADR-0004 section 7-8.

**Finding**: Rust's `encode()` built a `serde_json::Value` tree via the `json!()`
macro before serializing it — every id, enum variant and evidence row was
allocated twice, once into the generic `Value` and once into output bytes. Zig's
`encode()` already called `std.json.stringifyAlloc` directly on typed structs,
with no intermediate generic representation. This is exactly the "generic JSON
DOM vs typed records" asymmetry pass 1 was meant to close, but pass 1 only
covered the fixture/store side; the result-encoding side still had it.

**Fix**: `Store::encode` now defines typed `Payload`/`Envelope` structs (fields
declared alphabetically, matching the oracle's `sort_keys=True` canonicalization,
so the digest stays byte-identical — verified against `tests/golden_fixture.json`)
and serializes them directly with `serde_json::to_vec`, matching Zig's approach.
Separately, `valid_query` was being re-run recursively at every nested `eval()`
level even though its own recursion already validates the whole tree in one top
-level call; the redundant per-level calls were removed. Both changes are
representation/implementation fixes, not language comparisons: they remove
asymmetries pass 1 didn't reach, they do not favor either candidate by design.

**Phase split**: `query` (pass 1) is now `query` (traversal only, via a new
`Store::query`) plus `materialize` (sorted ids + filtered/sorted evidence, via
a new `Store::materialize`); `load` conceptually splits into `decode`+`construct`,
but both candidates parse bytes directly into typed structs in one call, so
`construct` is definitionally `0` for both — not a missing measurement. These
are exposed as an additive `phase_detail_ns: {decode, construct, materialize,
encode}` object alongside the unchanged `phases_ns: {load, index, query, result}`
(now `load=decode`, `result=materialize+encode`), so historical `phases_ns`
records and their consumers are unaffected. `harness/benchctl.py` enforces
`decode+construct==load` and `materialize+encode==result` before accepting a
record. Zig's `select()` received the identical `query`/`materialize` split for
phase symmetry; it needed no encode-side fix.

**SMOKE-scale effect (Rust only; local Zig build blocked by this Mac's SDK
issue, see TOOLCHAINS.md)**: `scan-type`'s `result` phase (materialize+encode)
dropped from a 3,392,062 ns median (pass 1) to roughly 2,450,000-2,510,000 ns
across five reruns — encode alone now runs at ~2.0-2.1ms, materialize at
~0.35-0.4ms, confirming encoding (not result construction) was the dominant
cost the Value-tree indirection added. This is a ~25-28% reduction on this
one query at SMOKE scale; it does not by itself close the S-scale gap and must
be reconfirmed at S scale on matching hardware before any conclusion.

**Execution modes**: only E2E (decode through encode, one process per query,
per ADR-0003's isolation design) is implemented; it is what `workload` already
does. KERNEL mode (amortize decode/index once, repeat query/materialize/encode
in-process) is designed — an opt-in `--repeat-in-process N` flag — but not
built this iteration. MATERIALIZE mode (isolate materialize+encode from a
precomputed id/evidence selection) is **BLOCKED**: it needs a stable
precomputed-selection input format that does not exist yet, and one process
per timing sample is deliberate for the existing isolation guarantees; forcing
it now would either bypass those guarantees or require a larger redesign than
this pass's scope.

Next: verified Zig conformance on hosted CI, then a fresh controlled S-scale
campaign under this pass, compared query-by-query against the pass-1 S-scale
evidence in the "Controlled S-scale campaign (hosted)" section of STATUS.md.
