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
