# S0 — Persistent Session Semantics (preliminary)

Status: PRELIMINARY DESIGN. Not implemented; operation names and field names are
provisional until reviewed. This document defines *semantics only*. It contains no
byte-level framing and no candidate-language concept; bytes are defined by a
transport binding ([SESSION-BINDING-JSONL.md](SESSION-BINDING-JSONL.md)). Decision
context and scope: [ADR-0007](../adr/0007-gate1-remaining-workloads.md).

Compatibility rule: the existing one-shot mode
(`workload --id ... --corpus ... --params ...`) is frozen and unchanged. S0 is
additive. Query and result semantics are those of [SEMANTICS.md](SEMANTICS.md).

## 1. Terms

* **Logical state**: the set of entities, relations and evidence rows plus the
  fixture context (`snapshot` label, `epoch`, `complete`). Independent of memory
  layout, interning, file format or index structure.
* **Store**: a candidate's live representation of one logical state.
* **Session**: the lifetime of one store, from `open` to `close`.
* **Generation**: a non-negative integer counting state changes in a session.
* **Snapshot**: a persisted image of the logical state, identified by `snapshot_id`.

## 2. Logical state and `state_digest`

Logical state is *resolved*, not stored: an entity carries its name string, not a
`name_sid`, so interning strategy (W4) is not observable in the state. Concretely,
canonical logical state is:

* `entities`: `{id, kind, name, container}` sorted ascending by `id`;
* `relations`: `{subject, relation, object}` sorted by `(subject, relation, object)`,
  duplicates retained (multiset);
* `evidence`: the eight result fields, sorted by the result sort key
  `(subject, relation, object, proposition, lineage, polarity, quality, freshness_epoch)`,
  duplicates retained;
* context: `snapshot`, `epoch` (`null` when absent), `complete` (`false` when absent).

`state_digest` (**mandatory**) is `sha256:` over the canonical JSON (sorted keys,
compact separators, UTF-8, no ASCII escaping) of that structure. It is the
cross-candidate equality test for a state: two candidates in the same state must
report the same `state_digest`, whatever their layouts. Strings not referenced by
any entity are not part of logical state. Computing it may be expensive at M scale,
so its cost is reported separately and never counted in query latency.

## 3. Session lifecycle

```
NEW --open--> OPEN --close--> CLOSED
                 \--any unrecoverable fault--> FAILED (terminal)
```

One store per session. Every operation has a valid state; outside it the operation
fails with `INVALID_STATE` and has no effect. `close` is idempotent in effect and
releases the store. `FAILED` accepts only `close`.

## 4. Operations (provisional)

| Operation | Valid in | Effect | Result |
|---|---|---|---|
| open | NEW | Loads a fixture (fixture format unchanged) into a store; `generation = 0` | protocol/semantics version, advertised capabilities, `generation`, `state_digest` |
| query | OPEN | Evaluates one query IR against the current state; no state change | canonical result (section 9), `generation` |
| mutate | OPEN | Applies one atomic batch (section 5) | new `generation`, `state_digest` |
| state_digest | OPEN | Computes the digest of the current state | `state_digest`, `generation` |
| snapshot | OPEN | Persists the current state | `snapshot_id`, the captured `generation` and `state_digest` |
| restore | OPEN | Replaces the current state by the state captured under `snapshot_id` | new `generation`, `state_digest` |
| cancel | OPEN | Requests cancellation of an earlier request (section 7) | outcome of the cancel request |
| stats | OPEN | Reports memory and count statistics (section 10) | stats record |
| close | OPEN, FAILED | Ends the session | acknowledgement |

## 5. Mutation semantics (v0)

* A batch is a list of operations over entities, relations and evidence rows (add /
  remove). A batch must contain at least one operation; an empty batch is
  `INVALID_INPUT`.
* **Atomic**: all operations apply or none do. A failed mutation leaves logical
  state and `generation` exactly as they were.
* A batch is validated as a *whole*: the state after applying all operations must
  satisfy every fixture invariant (schemas, unique entity ids, valid references,
  valid string fields). Validation is on that final state, so operation order within
  a batch does not matter for validity.
* **No implicit cascade.** Removing an entity that is still referenced by relations
  or evidence rows in the resulting state is rejected with `INVALID_INPUT`.
  Removing the entity together with all its dependents in the *same* batch is valid.
* `generation` is monotonic and increases by exactly one per successful batch.
* Fine-grained rules (duplicate-row removal, conflicting operations on the same key,
  entity rename) are deferred to the mutation-semantics ADR (ADR-0008, not yet
  written), which is a prerequisite of W8.S1.

## 6. Ordering and concurrency

* Execution is **sequential** in the baseline: one request is processed at a time
  and **responses are emitted in request order**. Each operation observes exactly
  the state left by the previous one.
* Concurrency is an **optional advertised capability** (`open` lists it), used only
  by X-CONC. Its semantics (which generation a concurrent query observes, whether
  mutations act as barriers) are not defined in v0 and are added, additively, when
  X-CONC is specified. A candidate without the capability is `UNSUPPORTED` for it,
  not "zero".

## 7. Cancellation

* Optional, advertised capability; without it `cancel` returns `UNSUPPORTED`.
* Every request receives **exactly one terminal response**. A cancelled request's
  terminal response is `CANCELLED` and carries **no partial semantic result**.
* The observable state after a cancel equals the state before the cancelled
  request: a cancelled `query` has no effect; a cancelled `mutate` either commits
  fully before the cancel takes effect (the request completes normally) or not at
  all. Whichever terminal response is produced first stands; there is no second one.
* The `cancel` request has its own terminal response stating whether it took effect
  (target cancelled, target already finished, or target unknown).
* Time-to-quiescence is a *measurement* (X-CANCEL), not a contract bound.

## 8. Error model

Closed set of codes; only the code is compared across candidates, never message text.

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | Malformed request, unknown operation or field |
| `INVALID_STATE` | Operation not valid in the current lifecycle state |
| `INVALID_INPUT` | Well-formed but semantically invalid: schema or integrity violation, referenced-entity removal, empty batch, unknown `snapshot_id` |
| `UNSUPPORTED` | Capability or operation not implemented by this candidate |
| `CANCELLED` | Request cancelled before completion |
| `LIMIT_EXCEEDED` | A resource limit was hit (memory, size) |
| `INTERNAL` | Candidate fault; the session becomes `FAILED` |

Any failed request leaves logical state and `generation` unchanged (except that
`INTERNAL` ends the session).

## 9. Ownership and determinism

* The candidate owns the store. The host holds only opaque `session` and
  `snapshot_id` values. **No pointers, shared-memory handles or file descriptors
  cross S0 v0**; mmap/shared view is W5.S2.
* A `query` result is byte-identical, in canonical JSON, to the oracle's result for
  the same logical state and the same query. The existing content digest is
  unchanged. `generation` is context beside the result, never inside it.
* `snapshot_id` is opaque and stable (it names one persisted snapshot). It has no
  meaning across candidates, because native snapshot formats differ; cross-candidate
  equality of a restored state is checked by `state_digest`.

## 10. Stats and memory accounting

`stats` returns a versioned record (`csl.eval.session.stats/v0.1`). Every field that
a candidate cannot measure reliably is `null` (never 0) with a reason.

| Field | Definition |
|---|---|
| `generation` | Current generation |
| `entities`, `relations`, `evidence` | Current row counts |
| `unique_strings` | Distinct name strings in the store |
| `live_heap_bytes` | Bytes currently allocated by the store through the candidate's allocator, including string data and indexes |
| `peak_heap_bytes` | Peak of the above since `open` |
| `heap_breakdown` | Optional: strings / entities / relations / evidence / indexes, each nullable |
| `allocations_total` | Allocation calls since `open`, where reliably countable |

Host-measured process RSS is reported by the harness, never by the candidate. The
two sources are recorded independently, and bytes per entity/relation/evidence
row/unique string are derived only from `live_heap_bytes`/`heap_breakdown`, never
from RSS divided by a count. Categories a candidate excludes (for example a Hybrid's
two allocators) must be stated per candidate.

## 11. Versioning and capabilities

* Semantics identifier: `csl.eval.session/v0.1`; the transport binding carries its
  own version, separate from semantics.
* `open` performs a handshake returning the semantics version and the list of
  advertised optional capabilities (`concurrency`, `cancel`, ...). Unknown
  operations return `UNSUPPORTED`. Changes within a minor version are additive
  only. The one-shot mode is versioned independently and frozen.

## 12. Validation after mutation and restore

* After `open`, every `mutate`, and every `restore`, the harness computes the
  oracle's state for the same sequence (the oracle gains an `apply(state, batch)`
  operation) and compares `state_digest` and `generation`.
* Queries issued after any state change are compared, byte-for-byte, with the oracle
  result on that state.
* `snapshot` then `restore` must reproduce the `state_digest` recorded by
  `snapshot`. A failed `mutate` must leave the digest and generation unchanged.
* The oracle side stays independent: the streaming oracle (ADR-0006) produces the
  expected values; candidates are never used to validate each other.

## 13. Equivalent workloads for Rust, Zig and Hybrid

A **scenario script** is a deterministic sequence of semantic requests generated from
a seed, independent of any transport. The same script is bound to the same bytes and
replayed into every candidate; the oracle produces the expected outcome for each
step. Only protocol-level operations appear in a script, so no candidate-specific
operation exists. A candidate lacking a capability records `UNSUPPORTED` for the
affected steps; a step is never dropped for one candidate and never recorded as zero.

## 14. Open questions (not settled by the approvals)

1. **`restore` and `generation`.** `restore` replaces state; to keep `generation`
   monotonic it is proposed to advance by one like a mutation (not to rewind).
   Confirm.
2. **Empty batch.** Proposed `INVALID_INPUT` (so "advances once per successful
   batch" stays unambiguous). Confirm.
3. **Cancellation with a sequential baseline.** A candidate must read requests while
   executing one to observe `cancel`. Is that acceptable inside the baseline, or does
   it force the concurrency capability?
4. **Cost of mandatory `state_digest` at M scale** (10M+ rows): acceptable as an
   explicitly costed operation, or does a bounded/incremental form need defining?
5. **Result size.** M-scale results reach hundreds of MB; how a transport frames them
   is a binding concern (see the JSONL binding), but the semantics should not require
   a single in-memory result.
6. **Unreferenced strings** are excluded from logical state; confirm no scenario
   needs them observable.
7. Duplicate-row removal, conflicting operations, entity rename: deferred to
   ADR-0008.
