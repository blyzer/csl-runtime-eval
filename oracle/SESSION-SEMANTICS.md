# S0 — Persistent Session Semantics (v0)

Status: **FINAL v0** (approved 2026-09-29; implementation authorized). This document
defines *semantics only*: no byte-level framing and no candidate-language concept.
Bytes are defined by a transport binding
([SESSION-BINDING-JSONL.md](SESSION-BINDING-JSONL.md)). Decision context and scope:
[ADR-0007](../adr/0007-gate1-remaining-workloads.md); mutation rules:
[ADR-0008](../adr/0008-mutation-semantics.md). Operation and field names are fixed for
v0 by the binding's wire appendix.

Compatibility rule: the existing one-shot mode
(`workload --id ... --corpus ... --params ...`) is frozen and unchanged. S0 is
additive. Query and result semantics are those of [SEMANTICS.md](SEMANTICS.md).

## 1. Terms

* **Logical state**: entities, relations and evidence rows plus the **context**
  (`snapshot` label, `epoch`, `complete`). Independent of memory layout, interning,
  capacity, file format or index structure.
* **Context**: the semantic/configuration values `snapshot`, `epoch`, `complete`. In v0
  the context is **immutable for the lifetime of a session** (section 3.3).
* **Store**: a candidate's live representation of one logical state.
* **Session**: the lifetime of one store, from `open` to `close`.
* **Snapshot repository**: a directory made available to the candidate at process
  start; it holds the candidate's snapshots (section 4.2).
* **Checkpoint**: an explicit, separately costed conformance operation
  (`state_digest`).

## 2. Logical state and `state_digest`

Logical state is *resolved*, not stored. An entity carries its name string, not a
`name_sid`, so interning strategy (W4) is not observable in the state. Canonical
logical state is:

* `entities`: `{id, kind, name, container}` sorted ascending by `id`;
* `relations`: `{subject, relation, object}` sorted by
  `(subject, relation, object)`, duplicates retained (multiset);
* `evidence`: the eight result fields, sorted by the result sort key
  `(subject, relation, object, proposition, lineage, polarity, quality, freshness_epoch)`,
  duplicates retained;
* context: `snapshot`, `epoch` (`null` when absent), `complete` (`false` when absent).

**Only reachable content is state.** Strings that no entity references are not part of
logical state. Interner garbage, string-table capacity, arena or allocator slack, index
capacity and any other representation detail can **never** affect `state_digest`: two
stores with the same logical state report the same digest regardless of how they got
there (fixture load, mutation history, restore) and regardless of layout.

`state_digest` is `sha256:` over the canonical JSON (sorted keys, compact separators,
UTF-8, no ASCII escaping; control characters below U+0020 escaped as `\b \f \n \r \t`
or `\u00xx` in lowercase hex) of that structure.

**Cost is a first-class metric.** `state_digest` is mandatory but is an explicit
conformance *checkpoint*, never computed implicitly by `open`, `query`, `mutate`,
`snapshot` or `restore`. Its cost is reported separately: the response carries
`state_digest_ms` (the candidate's own wall time for the operation) and
`bytes_processed` (canonical bytes hashed; `null` if the candidate cannot report it),
and the host also times the round trip. These figures are **excluded** from query,
mutate and restore workload timing.

## 3. Session model

### 3.1 Lifecycle

```
NEW --open--> OPEN --close--> CLOSED
                 \--unrecoverable fault--> FAILED (terminal)
```

One store per session. Every operation has valid states; outside them it fails with
`INVALID_STATE` and has no effect. `close` releases the store. `FAILED` accepts only
`close`.

`open` has exactly two sources in v0:

* **fixture**: builds a store from a fixture file; the fixture supplies the context;
* **empty**: builds an empty store (no entities, relations or evidence) with an
  explicitly supplied context. It exists so a snapshot can be restored into a fresh
  process without a fixture.

**`open(snapshot_id)` is not part of S0 v0.** Cold restore is the sequence
`process start -> open(empty) -> restore(snapshot_id) -> first valid query` (section 4.3).

### 3.2 Identifiers: three distinct concepts

| Identifier | What it is | Scope | Never |
|---|---|---|---|
| `generation` | Non-negative counter of state-changing operations within one session | One session | Restored, rewound, compared across sessions or candidates |
| `snapshot_id` | Opaque name of one persisted snapshot | Snapshot repository (section 4.2) | Interpreted by the host; portable across candidates or artifact versions |
| `state_digest` | SHA-256 of canonical logical state | Any store, any candidate | Computed implicitly; a substitute for the other two |

`generation`:

* `open` establishes `generation = 0`.
* It is **monotonic** within a session: never decreases, never reset.
* It advances by **exactly one** per successful `mutate` batch and by **exactly one**
  per successful `restore`. No other operation changes it.
* A failed request (including an empty or invalid batch) does not advance it.
* `restore` never restores, rewinds or adopts a generation number; only *logical
  content* is restored.

### 3.3 Immutable context

The context (`snapshot`, `epoch`, `complete`) is fixed at `open` and immutable for the
lifetime of the session. No operation changes it. `restore` requires the snapshot's
context to equal the session's context; otherwise it fails with `INVALID_INPUT` and has
no effect. A different context needs a different session.

## 4. Operations

Optional capabilities are marked (opt). Names are fixed for v0 by the wire appendix of
the binding.

| Operation | Valid in | Effect | Result |
|---|---|---|---|
| open | NEW | Builds a store from a source (fixture or empty); `generation = 0` | semantics version, advertised capabilities, mutation strategy, `generation` |
| query | OPEN | Evaluates one query IR against the current state; no state change | canonical result (section 9), `generation` |
| mutate | OPEN | Applies one atomic batch (section 5) | new `generation` |
| state_digest | OPEN | **Explicit checkpoint**: digest of the current state | `state_digest`, `state_digest_ms`, `bytes_processed`, `generation` |
| snapshot | OPEN | Persists the current logical state into the snapshot repository | `snapshot_id`, `captured_generation` |
| restore | OPEN | Replaces the logical content with the content captured under `snapshot_id`; advances `generation` by one | new `generation` |
| cancel (opt) | OPEN | Requests cancellation of an earlier request (section 7) | outcome of the cancel request |
| stats | OPEN | Reports memory and count statistics (section 10) | stats record |
| close | OPEN, FAILED | Ends the session | acknowledgement |

**`state_digest` is never computed implicitly.** No other result carries it. `snapshot`
records no digest by itself; the harness obtains the reference digest by issuing
`state_digest` immediately before `snapshot`, and the comparison digest by issuing it
immediately after `restore`. The sequential baseline (section 6) guarantees no mutation
intervenes between the pair.

**Timing rule.** The elapsed time of `query`, `mutate`, `snapshot`, `restore` and `open`
excludes any `state_digest` checkpoint. A candidate may maintain digest data
incrementally as a representation choice, but then that maintenance cost belongs to the
timing of the mutation or restore that incurs it.

### 4.1 Mutation strategy disclosure

`open` reports how the candidate implements `mutate`: `full-rebuild` (rebuild all
derived structures from the logical state) or `incremental` (update them in place).
A `full-rebuild` strategy is a valid, explicitly reported fallback. It is the
correctness reference and a performance baseline, but does **not** by itself satisfy
the W8 incremental-invalidation workload (ADR-0008).

### 4.2 Snapshot repository and `snapshot_id`

The repository is process-level configuration (a directory) given when the candidate
process starts; it is not part of any request. `snapshot` writes a candidate-native
image there and returns an **opaque** `snapshot_id`. For Gate #1 a `snapshot_id` is
guaranteed **only** within that benchmark snapshot repository and for a compatible
candidate and artifact version: a candidate must reject (with `INVALID_INPUT`) a
`snapshot_id` that is unknown, or written by an incompatible artifact version. No
production portability or long-term on-disk compatibility is promised, and a
`snapshot_id` has no meaning across candidates (cross-candidate equality of a restored
state is checked by `state_digest`).

### 4.3 Cold restore (W5.S1)

```
process start -> open(empty, context) -> restore(snapshot_id) -> first valid query
```

The four phases are measured independently by the host: `process_start` (spawn until the
process is ready to serve, taken as the moment its first response is received minus the
`open` service time, reported as spawn-to-open-response), `open`, `restore`, and
`first_query`. **Total time-to-first-query** is also reported. A `state_digest`
checkpoint and any result verification are outside all four phases.

## 5. Mutation semantics (v0)

* A batch is a list of operations over entities, relations and evidence rows.
* **Empty batch**: `INVALID_INPUT`. It changes no state, does not advance `generation`,
  and does not change `state_digest`.
* **Atomic**: all operations apply or none do. A failed `mutate` leaves logical state,
  `generation` and `state_digest` exactly as they were.
* A batch is validated as a *whole*, on the resulting state, so operation order within a
  batch does not affect validity or result.
* **No implicit cascade.** Removing an entity that is still referenced by relations,
  evidence rows or as a `container` in the resulting state is rejected with
  `INVALID_INPUT`. Removing the entity together with all its dependents in the *same*
  batch is valid.
* `UPDATE_ENTITY` is the only operation that modifies an entity; there is no separate
  rename operation.
* **Not cancellable in the sequential baseline.** Once admitted, a mutation batch
  executes atomically and produces **exactly one terminal response**.
* `generation` advances by exactly one per successful batch (section 3.2).
* Duplicate-row semantics, conflicting operations and every remaining rule are defined in
  [ADR-0008](../adr/0008-mutation-semantics.md).

## 6. Ordering, concurrency and the sequential baseline

* The baseline is **sequential**: one request is processed at a time and **responses are
  emitted in request order**. Each operation observes exactly the state left by the
  previous one. A baseline implementation is **not required** to read further requests
  while executing one.
* **Concurrent request processing** and **cancellation** are **optional advertised
  capabilities** (`open` lists them), outside the Gate-required baseline. A candidate that
  does not advertise a capability returns `UNSUPPORTED` for the corresponding operation;
  that outcome is recorded as `UNSUPPORTED`, never omitted and never counted as zero.
* The semantics of concurrent execution are not defined in v0 and are added additively
  when X-CONC is specified.

## 7. Cancellation (optional capability)

* Not part of the Gate-required baseline; a baseline implementation returns
  `UNSUPPORTED` for `cancel`. X-CANCEL is non-blocking for Gate #1.
* **A mutation is never cancellable in the sequential baseline** (section 5). How an
  advertised capability treats `mutate` is defined by that capability's own specification.
* When advertised, for cancellable requests: every request still receives **exactly one
  terminal response**; a cancelled request's terminal response is `CANCELLED` with **no
  partial semantic result**; the observable state after the cancel equals the state before
  the cancelled request; the first terminal response produced stands.
* The `cancel` request has its own terminal response stating whether it took effect.
* Time-to-quiescence is a *measurement* (X-CANCEL), not a contract bound.

## 8. Error model

Closed set of codes; only the code is compared across candidates, never message text.

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | Malformed request, unknown operation or field |
| `INVALID_STATE` | Operation not valid in the current lifecycle state |
| `INVALID_INPUT` | Well-formed but semantically invalid: schema or integrity violation, referenced-entity removal, empty batch, unknown or incompatible `snapshot_id`, context mismatch on `restore` |
| `UNSUPPORTED` | Capability or operation not implemented by this candidate |
| `CANCELLED` | Request cancelled before completion (only with the capability) |
| `LIMIT_EXCEEDED` | A resource limit was hit (memory, size), according to preregistered resource constraints; there is no universal semantic batch-size limit |
| `INTERNAL` | Candidate fault; the session becomes `FAILED` |

Any failed request leaves logical state and `generation` unchanged (except that
`INTERNAL` ends the session).

### 8.1 Error-code clarifications fixed by the v0 implementations

The two independent implementations had to choose a code where the text was silent; both
chose the same, and v0 now fixes them: `open(empty)` with a malformed context, `open` with
`max_line_bytes` below 65536, and `cancel` without `target` -> `INVALID_REQUEST`; `close` (or any
other operation) before `open` -> `INVALID_STATE`; an unreadable or invalid fixture in `open`, a
query whose IR is invalid, and an unknown, corrupt or incompatible `snapshot_id` -> `INVALID_INPUT`;
an unknown operation or unknown/missing fields are rejected as `INVALID_REQUEST` *before* the
lifecycle check; malformed JSON or a non-object line -> `INVALID_REQUEST` with `"id": null`.
Integer-valued fields must be JSON integers: float spellings such as `1.0` are rejected by the
candidates, and a boolean is never an id.

## 9. Ownership and determinism

* The candidate owns the store. The host holds only opaque `session` and `snapshot_id`
  values. **No pointers, shared-memory handles or file descriptors cross S0 v0**;
  mmap/shared view is W5.S2.
* A `query` result is byte-identical, in canonical JSON, to the oracle's result for the
  same logical state and the same query. The existing content digest is unchanged.
  `generation` is context beside the result, never inside it.
* Transport framing, including chunking of large results, is a binding concern and never
  alters semantics or canonical logical results.

## 10. Stats and memory accounting

`stats` returns a versioned record (`csl.eval.session.stats/v0.1`). Every field a
candidate cannot measure reliably is `null` with a reason, never 0.

| Field | Definition |
|---|---|
| `generation` | Current generation |
| `entities`, `relations`, `evidence` | Current row counts |
| `unique_strings` | Distinct *reachable* name strings in the store |
| `live_heap_bytes` | Bytes currently allocated by the store through the candidate's allocator, including string data and indexes |
| `peak_heap_bytes` | Peak of the above since `open` |
| `heap_breakdown` | Optional: strings / entities / relations / evidence / indexes, each nullable |
| `allocations_total` | Allocation calls since `open`, where reliably countable |

Host-measured process RSS is reported by the harness, never by the candidate. The two
sources are recorded independently. Bytes per entity/relation/evidence row/unique string
are derived only from `live_heap_bytes`/`heap_breakdown`, never from RSS divided by a
count. Categories a candidate excludes (for example a hybrid's two allocators) must be
stated per candidate.

## 11. Versioning and capabilities

* Semantics identifier: `csl.eval.session/v0.1`. The transport binding carries its own
  version, independent of semantics.
* `open` performs a handshake returning the semantics version and the advertised optional
  capabilities. Unknown operations return `UNSUPPORTED`. Changes within a minor version are
  additive only. The one-shot mode is versioned independently and frozen.

## 12. Validation after mutation and restore

* After `open`, every `mutate` and every `restore`, the harness compares the candidate
  against the oracle for the same sequence (the oracle gains `apply(state, batch)`) using
  the explicit `state_digest` checkpoint and the `generation` value.
* Queries issued after any state change are compared, byte-for-byte, with the oracle result
  on that state.
* `state_digest` before `snapshot` must equal `state_digest` after `restore`. A failed or
  empty `mutate` must leave `state_digest` and `generation` unchanged.
* The oracle stays independent: candidates are never used to validate each other.

## 13. Equivalent workloads for Rust, Zig and Hybrid

A **scenario script** is a deterministic sequence of semantic requests generated from a
seed, independent of any transport. The same script is bound to the same bytes and
replayed into every candidate; the oracle produces the expected outcome for each step.
Only protocol-level operations appear, so no candidate-specific operation exists. A
candidate lacking a capability records `UNSUPPORTED` for the affected steps; a step is
never dropped for one candidate and never recorded as zero. Checkpoint (`state_digest`)
steps are explicit script steps, timed separately.

## 14. Status of earlier open questions

All resolved by the 2026-09-29 decisions: `state_digest` cost is measured separately, with
no semantic bound; cold restore is `open(empty) -> restore(snapshot_id)`; `snapshot_id`
is repository-scoped and opaque; mutation is not cancellable in the baseline; context is
immutable in v0. Remaining work is implementation, not contract.
