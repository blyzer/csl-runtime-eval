# ADR-0008: Mutation semantics and the W8 correctness invariant

Status: **ACCEPTED** (approved 2026-09-29 with the decisions in section 13). Design; the
mutation operations are implemented as part of S0, the incremental strategies as part of W8.S1.
It is a prerequisite of W8.S1 ([ADR-0007](0007-gate1-remaining-workloads.md)) and completes
the mutation rules that
[S0 session semantics](../oracle/SESSION-SEMANTICS.md) deliberately defer. Query and
result semantics remain those of [SEMANTICS.md](../oracle/SEMANTICS.md).

## 1. Scope

S0 already fixes: batches are atomic; an empty batch is `INVALID_INPUT` and changes
nothing; removing a still-referenced entity is `INVALID_INPUT` with no implicit
cascade (dependents may be removed in the same batch); `generation` advances once per
successful batch; a failed batch leaves state, generation and `state_digest`
unchanged. This ADR defines the operation set, duplicate handling, conflicting
operations, validation, completeness effects, and the correctness invariant W8
measures.

## 2. Data model the rules act on

* **Entities**: a map `id -> {kind, name, container}` (ids unique).
* **Relations**: a multiset of `{subject, relation, object}` rows.
* **Evidence**: a multiset of rows with the eight result fields
  (`proposition, subject, relation, object, polarity, quality, freshness_epoch,
  lineage`).
* **Context**: `snapshot` label, `epoch`, `complete`.

Relations and evidence are *value objects* identified by their full value. Identical
rows are indistinguishable, so "which duplicate" is never a question.

## 3. Operation set (v0)

| Operation | Fields | Precondition (on the pre-batch state) |
|---|---|---|
| ADD_ENTITY | `id, kind, name, container?` | `id` does not exist |
| REMOVE_ENTITY | `id` | `id` exists |
| UPDATE_ENTITY | `id, set:{kind?, name?, container?}` (at least one field) | `id` exists |
| ADD_RELATION | `subject, relation, object` | none |
| REMOVE_RELATION | `subject, relation, object` | multiplicity of that row is at least the number of removals of it in the batch |
| ADD_EVIDENCE | the eight evidence fields | none |
| REMOVE_EVIDENCE | the eight evidence fields | multiplicity as above |

**Rename and update.** An entity's `kind`, `name` and `container` change only through
`UPDATE_ENTITY`; a rename is `UPDATE_ENTITY` with `set.name`. There is no in-place
update of relation or evidence rows: changing one is `REMOVE_x` of the old value plus
`ADD_x` of the new value in the same batch (they are different values, so this is not
a conflict). Names are plain strings; the batch never mentions string tables or
interning, so W4's representation is not observable through mutation.

## 4. Duplicate rows (multiset semantics)

* `ADD_RELATION` / `ADD_EVIDENCE` of a value that already exists is **valid** and
  raises its multiplicity by one, because the fixture semantics retain duplicates
  ("Duplicate evidence rows remain visible").
* Several adds of the same value in one batch each add one occurrence.
* `REMOVE_RELATION` / `REMOVE_EVIDENCE` removes **exactly one occurrence** of the
  named value per operation. Removing more occurrences than exist before the batch is
  `INVALID_INPUT`. Removal is by full value, never by position.

## 5. Conflicting operations within a batch

To keep results independent of operation order, these are rejected as
`INVALID_INPUT` (a *conflict*), before any state change:

1. Two or more entity operations (`ADD_ENTITY`, `REMOVE_ENTITY`, `UPDATE_ENTITY`) for
   the same `id`.
2. The same relation value, or the same evidence value, both added and removed in one
   batch (it would be ambiguous whether the removal targets a pre-existing or a newly
   added occurrence).
3. `UPDATE_ENTITY` with no `set` fields.

Not conflicts: several adds of one value; several removes of one value (subject to
multiplicity); removing a value while adding a *different* value; `ADD_ENTITY` for an
id together with relations that use it.

## 6. Deterministic validation on the final state

A batch is validated in two steps, both independent of operation order:

1. **Per-operation and conflict checks** (sections 3-5) against the pre-batch state
   and the batch itself.
2. **Final-state integrity**: after applying every operation to a working copy, the
   resulting state must satisfy all fixture invariants: schema validity of every
   touched row, unique entity ids, every relation and evidence `subject`/`object` is a
   final entity id, every `container` is `null` or a final entity id.

Any failure is `INVALID_INPUT`, nothing is applied, `generation` does not advance and
`state_digest` does not change. **No implicit cascade**: a referenced entity's removal
fails at step 2 unless the same batch also removes every relation row, evidence row
and contained entity that references it. Adding a relation that uses an entity added
in the same batch is valid (final-state check).

## 7. Effect on completeness and evidence

* `TRUNCATED` / `COMPLETE` / `OBSERVED` are functions of `(state, query)` under the
  unchanged SEMANTICS.md rules. A mutation has no special completeness rule: it
  changes adjacency and therefore which steps a capped traversal inspects, and the new
  classification follows deterministically. A batch can therefore flip a query between
  `TRUNCATED` and non-truncated results.
* `complete` is a provider assertion carried by the state. Mutations cannot change it
  in v0 (no operation touches context), so it is preserved as-is. See open question 2.
* Evidence rows are independent of relation rows in the query semantics. A relation
  operation never adds or removes evidence and vice versa; there is no derived
  coupling to maintain or invalidate at the semantic level.

## 8. Ordering independence

The resulting logical state, `state_digest` and every query result depend only on the
pre-batch state and the *set* of operations in the batch, not on their order, and not
on insertion history. Two histories that reach the same logical state have the same
`state_digest`.

## 9. The W8 correctness invariant

For a state `S` and a valid batch `Δ`, under the same semantics and configuration:

```
ColdBuild(S + Δ)  ≡  IncrementalApply(S, Δ)
```

* `S + Δ` is the logical state obtained by applying `Δ` to `S` per sections 3-8, as
  computed by the **oracle**.
* `ColdBuild(S + Δ)`: a fresh store opened from a serialized fixture of `S + Δ`.
* `IncrementalApply(S, Δ)`: a session whose store was built from `S`, then `mutate(Δ)`.
  A candidate may implement this by a full internal rebuild (correct, but it will show
  no gain) or by true incremental update; both are measured honestly.

`≡` requires equality of the canonical logical `state_digest` and of canonical semantic
query results, and in full, at minimum:

1. **`state_digest`** (canonical logical state), from the explicit checkpoint;
2. **canonical query results**, byte-identical, over a fixed query set `Q` (the W1/W2
   set plus queries targeted at the delta);
3. **generation-independent semantic content**: the comparison never involves
   `generation`. A cold build sits at generation 0 and an incremental session at
   generation `k`; they must still be equivalent;
4. **evidence and proposition state**: multiset equality of evidence rows (implied by
   the digest) and equality of `propositions` in evidence-predicate queries
   (`min_quality`, `freshness_epoch`) included in `Q`, since ordinary relation queries
   can hide evidence divergence;
5. **rejection equivalence**: an invalid `Δ` leaves the store unchanged (digest and
   generation), exactly as if it had not been submitted.

### 9.1 No false CURRENT

A store may keep derived data: cached query results, adjacency or name indexes,
memoized traversals. A derived artifact is **CURRENT** when the candidate treats it
as valid at the present generation without recomputation.

* **Rule**: invalidation must be conservative. **Over-invalidation is permitted**
  (recomputing something that had not changed). **Under-invalidation is forbidden**: a
  misclassified invalidation must never produce a false CURRENT result, i.e. serve
  data that differs from what a cold build of the current state would produce.
* **Detection is black-box and authoritative**: the harness cannot see the cache, so a
  false CURRENT shows up only as a mismatch, and any mismatch fails the sample. After
  **every** batch the harness runs `Q` on the incremental session and compares each
  result byte-for-byte with the oracle on `S + Δ` (optionally also with a cold-build
  store), and compares `state_digest` at checkpoints.
* **Targeted adversarial deltas** are part of the required correctness gate, not
  timed: a query cached before the batch and re-asked after it; a change deep inside a
  previously cached depth-8 traversal; removal of the only edge on a path; an added
  shortcut edge; an evidence-only delta with evidence-predicate queries cached; an
  entity rename affecting name resolution; a `kind` change affecting a `FILTER`
  result; an edge added early in adjacency order that shifts which steps a `max_paths`
  cap inspects (flipping `TRUNCATED`); duplicate add/remove that changes only
  multiplicity; and unrelated deltas that must leave cached results untouched *and*
  correct.
* Optional candidate diagnostics (cache hits, invalidation counts) may be reported in
  `stats` for interpretation but are never used to decide correctness.

## 10. Oracle requirements

* `oracle.apply(state, Δ)` on the **streaming oracle** (ADR-0006): applies a batch to
  a fixture file and writes the resulting fixture file, reading entities, relations and
  evidence in one streaming pass each. Removal sets and multiplicity counters for a
  ~1% batch are small; a set of final entity ids (bounded by the entity count) supports
  the final-state referential check. Working memory stays in the same budget as the
  current oracle.
* An independent dict-based `apply` on the classic oracle (`oracle/oracle.py`) is
  required for small states, and the two must agree byte-for-byte on every test case,
  following the ADR-0006 pattern. Both validate with the same rules of sections 3-6.
* `oracle.state_digest(state)`: computed from the same sorted columns the streaming
  oracle already builds, so it needs no second sort pass over candidate data.
* The oracle produces expected outcomes for the entire scenario script, including
  expected `INVALID_INPUT` steps.

## 11. W8.S1 — incremental invalidation (scenario definition)

* **Sizes**: S (100k entities / 1M relations / 1M evidence rows) and, if S is clean, M
  (1M / 10M / 10M).
* **Mutation sweep** (fraction of all rows touched): **0.01%, 0.1%, 1% (the primary
  comparison point), 5%**.
* **Compositions**, each run across the sweep: *entity-only* (adds, `UPDATE_ENTITY`,
  removals with dependents removed in the same batch), *relation-only* (adds/removes),
  *evidence-only* (adds/removes), and *mixed* (the proposal below). Deliberate duplicate
  adds are included in relation and evidence compositions.
* **Mixed composition** (touches about the target fraction): relation adds 40%, relation
  removes 20%, evidence adds 20%, evidence removes 10%, entity adds 5% (each with its
  relations), entity updates 3% (name and `kind`), entity removes 2% (each with its
  dependents removed in the same batch).
* **Untimed correctness gate**: the adversarial deltas of section 9.1, run at SMOKE and S.
* **Metrics**: apply latency (`mutate`) versus cold rebuild latency (`open` of the
  serialized `S + Δ`), their ratio and the break-even fraction; time to first query after
  each path; live/peak heap and host RSS after apply versus after a cold build (X-MEM); and
  the correctness verdict of section 9, a **hard gate**: a sample whose incremental state
  fails any of the equalities of section 9 is invalid, not slow.
* **Mutation strategy is always recorded** (`open` reports `full-rebuild` or `incremental`).
  A `full-rebuild` fallback is reported explicitly as such: it serves as the correctness
  reference path and as a performance baseline, and is an allowed fallback, but it does
  **not** by itself satisfy the incremental-invalidation workload.
* **Measured vs excluded**: timed intervals cover only the `mutate` and `open` operations
  themselves (request fully sent to terminal response received). The `state_digest`
  checkpoint is timed as its own metric and is **excluded** from mutate, restore,
  cold-build and query timing. Oracle work, fixture serialization of `S + Δ`, and harness
  comparisons are outside all timed intervals.

## 12. Batch size

There is **no universal semantic batch-size limit**. Workloads define their batch sizes
(section 11), and an implementation may answer `LIMIT_EXCEEDED` according to resource
constraints preregistered before the measurement; a `LIMIT_EXCEEDED` outcome is recorded,
never dropped or counted as zero.

## 13. Decisions recorded at approval (2026-09-29)

1. `UPDATE_ENTITY` is the **only** entity modification operation in v0; there is no
   separate rename operation. Remove-plus-add of the same entity id in one batch is a
   conflict (section 5, rule 1).
2. Context (`snapshot`, `epoch`, `complete`) is immutable in v0 (S0 semantics 3.3).
3. No universal batch-size limit (section 12).
4. The W8.S1 sweep is 0.01%, 0.1%, 1% (primary), 5%, across entity, relation, evidence and
   mixed compositions (section 11).
5. Atomic final-state validation, no implicit cascade, multiset semantics with
   one-occurrence removal, and rejection equivalence are preserved.
6. The hard invariant `ColdBuild(S + Δ) ≡ IncrementalApply(S, Δ)` requires equality of the
   canonical logical `state_digest` and of canonical semantic query results (section 9);
   the remaining equalities listed there stay as further checks.
7. A full rebuild is the correctness reference path, a performance baseline and an allowed,
   explicitly reported fallback, but does not by itself satisfy the incremental-invalidation
   workload (section 11).
8. Optimistic preconditions (for example an expected `state_digest`) are deferred to the
   concurrency capability.

## 14. What "incremental" means (v0 requirements, added with the W8 implementation)

A candidate may report `strategy.mutation = "incremental"` only if all of these hold; otherwise
it reports `full-rebuild`:

1. **Work proportional to the delta.** Derived structures (entity map, adjacency indexes,
   evidence store, name index) are updated in place, at a cost that depends on the batch (and on
   local index sizes such as a node's degree), not on the total row count; `mutate` does not
   rescan or rebuild them.
2. **Validation proportional to the delta.** Final-state integrity (section 6) is decided from
   the touched entities and reference counts, not by re-validating the whole state.
3. **Atomic**: a rejected batch leaves every derived structure unchanged (validate first, or undo).
4. **No stale derived data**: nothing derived is served without reflecting every applied batch
   (section 9.1). Any cache or memo must be invalidated conservatively.
5. **Fallbacks are counted**: any full rebuild (compaction, resize, corruption recovery) increments
   `derived_rebuilds_total` (binding appendix). W8 results report that counter next to every
   incremental measurement; a run with fallback rebuilds is reported as such, not as clean
   incremental evidence.

`full-rebuild` remains the reference path and a baseline: `mutate` returns only after every derived
structure is rebuilt from the logical state.
