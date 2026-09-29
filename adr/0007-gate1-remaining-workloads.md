# ADR-0007: Gate #1 workload map, scope, and the S0 session prerequisite

Status: ACCEPTED (reconciliation and scope, 2026-09-29). Design and
classification only: nothing here is implemented or measured, and Gate #1
remains **OPEN**.

This ADR replaces an earlier draft that proposed its own W3-W12 definitions
before the program's canonical workload map was known. The useful ideas of that
draft are re-filed below under the canonical numbering.

## 1. Canonical map (permanent, not renumbered)

| W | Canonical meaning | Implemented here today |
|---|---|---|
| W1 | Entity representation / store | Yes. Load/validate, ID lookup, kind scan. SMOKE, controlled S and M |
| W2 | Relation graph | Yes. Indexed outgoing/incoming, bounded depth 2/4/8, mixed relations. SMOKE, controlled S and M |
| W3 | Evidence | No dedicated workload. Evidence rows ride along in W1/W2 results |
| W4 | String interning / symbol-name representation | No. The fixture has a string table (`strings`, `name_sid`); no interning workload |
| W5 | Persistence / mmap | Partial. W10 C seam (`harness/shared_view.py`) and tests; no native format, no mmap measurement |
| W6 | SCIP projection | No |
| W7 | Tree-sitter | No |
| W8 | Incremental invalidation | No. E0 is read-only; the 1% update path is explicitly unsupported |
| W9 | Planner kernel | No |
| W10 | Hybrid boundary | Bootstrap A/B done (200 samples); C partial; separate P4 track (section 7) |
| W11 | Build / artifact | Smoke builds done (6 PASS); caches may be warm |
| W12 | Host integration | No |

W-numbers are canonical and are never reused for other experiments.
Cross-cutting scenarios and metrics use `X-*` identifiers (`X-MEM`, `X-CONC`,
`X-CANCEL`, `X-SOAK`). Sub-experiments of a workload use `Wn.Sk` (scenario) and,
for W2 extensions, `W2.Xk`.

## 2. Where each earlier idea now lives

| Idea | Kind | Home |
|---|---|---|
| Evidence-predicate selectivity (0.1%..100% via `min_quality`, `freshness_epoch`) | scenario | **W3.S1** |
| Polarity / lineage / duplicate-evidence mixes | scenario | W3.S2 |
| Graph-shape robustness: `powerlaw`, `chain` | scenario | W2.X1, W2.X2 |
| Duplicate/parallel edges retained by the semantics | scenario | W2.X3 |
| Sparse u64 IDs near 2^64 | scenario | W2.X4 (also exercises the W1 store) |
| Cap-boundary queries (`max_paths` below/at/above reachable steps; TRUNCATED) | scenario | W2.X5 |
| Snapshot write / cold restore, time-to-first-query vs JSON load | scenario | **W5.S1** |
| mmap shared-view reads (existing W10 C seam) | scenario | W5.S2 |
| Incremental 1% mutation vs full rebuild | scenario | **W8.S1** |
| Phase memory decomposition; bytes per entity/edge/evidence row/unique string | cross-cutting metric | **X-MEM** |
| Concurrent readers (throughput, tail latency, RSS per reader) | cross-cutting scenario | X-CONC (needs S0) |
| Cancellation (time-to-quiescence, next result still correct) | cross-cutting runtime requirement | X-CANCEL (needs S0) |
| Long-run soak (leaks, drift, fragmentation) | cross-cutting reliability scenario | X-SOAK (needs S0) |

## 3. W4 — String interning / symbol-name representation

Defined here (approved 2026-09-29). Purpose: expose how each candidate represents,
deduplicates, owns and looks up symbol-name strings (Rust owned `String`s vs Zig
borrowed slices, ADR-0005; hash-based vs scan-based name resolution), at scale.
Name resolution today is a scan of entities comparing `strings[name_sid]`, and no
current query set exercises it at scale.

| Scenario | Question | Notes |
|---|---|---|
| W4.S1 intern-on-load | Time and memory to load and intern the string table | Includes unique-string count and dedup ratio |
| W4.S2 name -> entity resolution | Latency of resolving a name to its entities | Existing `RESOLVE` by name semantics |
| W4.S3 ambiguous-name resolution | Same, when a name maps to many entities | Results keep all matches (SEMANTICS.md) |
| W4.S4 duplication-ratio sweep | Behavior as unique/total string ratio varies | e.g. 100%, 50%, 10%, 1% unique |
| W4.S5 name-length / cardinality distribution | Behavior under skewed lengths and name-frequency distributions | Long tails, Zipf-shaped reuse |
| W4.S6 repeated resolution | Steady-state cost of many resolutions in one process | Cache/warm effects; latency percentiles |

Measurements: load/intern time; lookup p50/p95/p99; live and peak heap;
bytes/entity; bytes/unique-string; allocation count. Heap, bytes/unique-string
and allocation count are recorded **only where reliably measurable**; otherwise
`null` with a stated reason, never 0. Every scenario keeps the standing contract:
oracle-verified results, byte-identical canonical output, provenance, and the
controlled/exploratory classification. W4 needs a corpus generator extension
(controlled name-duplication, length and cardinality distributions) and, for
S4/S5/S6 in a one-shot process, in-process repeated timing; neither requires S0.

## 4. Gate #1 scope

Gate #1 asks which runtime implements the local CSL semantic kernel (ADR-0004). A
workload is Gate-relevant if it can change the Rust / Zig / Hybrid answer through a
*runtime* property (allocation, layout, ownership, boundary cost, mutability,
build), not merely because it belongs to the eventual product.

| Class | Workloads / scenarios | Reason |
|---|---|---|
| **Gate-required** | W1, W2 (incl. W2.X1-X5), W3 (S1-S2), W4 (S1-S6), W5.S1, W8.S1, W10, W11 (clean/uncached confirmation), **X-MEM** | Each stresses a runtime property the choice depends on: layout and hashing (W1/W2), result construction (W3), string ownership (W4), persisted-store and mutable-index ownership (W5/W8), the boundary (W10), reproducible builds (W11), and memory accounting everywhere |
| **Useful, non-blocking** | X-CONC, X-CANCEL, X-SOAK, W5.S2 (mmap) | Strong evidence for kernel design and for hybrid's ownership seam, but they refine a choice. Any of them that exposes a **material ranking reversal** becomes blocking |
| **Post-Gate product validation** | W6 SCIP projection, W7 Tree-sitter, W9 Planner kernel, W12 Host integration | Product subsystems. Building them now would mostly test the subsystem and its libraries and would contaminate a runtime-selection experiment with product code |

`X-MEM` is a required cross-cutting Gate metric **wherever technically
measurable**: each required workload reports host-measured peak RSS and, where the
candidate can report it reliably, live/peak heap, bytes per entity/relation/
evidence row/unique string, and allocation count, with `null` plus reason where it
cannot. X-MEM never derives bytes/entity from RSS/count (the disclaimer STATUS.md
already carries).

No representative subsystem kernels are built now. `R-trees` (many small trees,
arena vs per-node ownership, lifetime) is the only candidate, and only if later
evidence leaves an allocation/lifetime question unresolved. Rationale by item: W6's
runtime property (ingest under a large structured input) is covered by W1 load and
W4; W9's (compute over the indices) by W2; W12's (host lifecycle/ownership/boundary)
by S0 lifecycle conformance plus W10; W7's (allocation-heavy trees plus C FFI) is the
one open question, hence R-trees.

## 5. S0 — Persistent Session Protocol (prerequisite, design only)

Today's W1/W2 measure single-run, read-only processes, which cannot evaluate
persistence, incremental mutation, long-lived memory behavior, snapshot/restore,
concurrency or cancellation. S0 provides the common semantics those scenarios need.
It is an independent, additive prerequisite: the existing
`workload --id ... --corpus ... --params ...` mode stays **byte-for-byte
unchanged** so all W1/W2/W10/W11 evidence remains reproducible.

Semantics are separated from transport:

```
S0 Session Semantics  (oracle/SESSION-SEMANTICS.md)
        |
reference transport binding  (oracle/SESSION-BINDING-JSONL.md)
        |
JSONL v0
```

JSONL never defines semantics; a binding only maps them to bytes, and any other
binding must preserve every semantic outcome. Both documents are design only.

Resolved for S0 v0 (approved 2026-09-29):

* one store per session; the sequential baseline emits responses in request order;
* `generation` is monotonic and advances once per successful mutation batch;
* `snapshot_id` is an opaque, stable snapshot identifier;
* `state_digest` is mandatory: SHA-256 over canonical *logical* state;
* mutation batches are atomic; a failed mutation leaves generation and logical
  state unchanged;
* removing an entity still referenced by relations/evidence is rejected with
  `INVALID_INPUT`; there is no implicit cascade in v0, but explicit removal of the
  dependents may be included in the same atomic batch;
* baseline execution is sequential; concurrency is an optional advertised
  capability, used by X-CONC;
* cancellation is best-effort with exactly one terminal response and no partial
  semantic result;
* no pointers or shared-memory handles in S0 v0; mmap/shared view is W5.S2.

Items the approvals did not settle are listed as open in
`oracle/SESSION-SEMANTICS.md` and need review before S0 is implemented.

## 6. Execution order

1. Apply this reconciliation (this ADR) and publish the S0 preliminary
   specification. **Done with this change.**
2. Keep W1/W2/W10/W11 and all historical evidence untouched.
3. Define and run what needs **no session**: W3 (S1-S2), W2.X1-X5, W4 (S1-S6).
   Implementation does not start until the Gate-required matrix (section 8) has
   been reported.
4. Review S0 (semantics, then binding) before any S0 code.
5. After S0 is approved: implement W5.S1 against the same session semantics
   (there is **no interim one-shot W5.S1**), then W8.S1, which additionally needs
   the separate mutation-semantics ADR (planned as ADR-0008, not yet written),
   then the non-blocking X-CONC, X-CANCEL, X-SOAK.
6. W6, W7, W9, W12 stay reserved and unimplemented; revisit after Gate #1.

## 7. Hybrid stays separate (P4 / W10)

Standing hypothesis: Zig's original ~35-45% index-construction advantage shrank to
~4-7% once the hasher asymmetry was fixed (ADR-0005). Evidence since: hybrid was
never faster than pure Zig, and pure Rust was fastest, at controlled S (hybrid
+3-13% time, +22-35% RSS vs Zig) and controlled M (+1-8% time, +34-40% RSS). W10
must *try to falsify* that the residual margin justifies a Rust <-> Zig boundary.

**Falsification principle.** Session, persistence or incremental results may change
the Hybrid conclusion only if they expose a new, material, reproducible Zig
advantage under equivalent semantics. They are never introduced to rescue Hybrid.

**Decision framework (replaces the earlier `15% + non-overlapping IQR` rule).**
Neither an interquartile-range overlap test nor one universal percentage is used
as a significance or materiality criterion. Instead:

1. **Pre-registration before measuring.** Before any new measurement campaign that
   will be used to judge Hybrid, a pre-registration record is committed that fixes,
   *per dimension*, the metric definition, the comparison baseline, the minimum
   material effect, and the uncertainty method (appropriate to paired repeated
   measurements). Thresholds chosen after seeing data are not accepted.
2. **Dimensions, evaluated independently:** execution/query/update latency; memory;
   startup; artifact/build characteristics; BoundaryTax; ownership/copy cost;
   implementation/toolchain complexity. BoundaryTax, ownership/copy cost and
   toolchain complexity are costs that only Hybrid incurs.
3. **Hard requirements first** (pass/fail, no trade-offs): semantic conformance
   (byte-identical to the oracle), controlled classification of the evidence, no
   safety regression at the boundary, reproducible builds. A candidate that fails a
   hard requirement does not qualify.
4. **Pareto comparison among qualifying candidates**, not a weighted score. No
   dimension is traded against another by a formula.
5. **Survival condition.** A Hybrid architecture survives only if it provides a
   reproducible material advantage, in at least one dimension, that *neither*
   qualifying pure candidate provides, and that advantage is sufficient to justify
   the permanent cross-language boundary and a second production toolchain. The
   justification is a written argument reviewed against the pre-registered
   thresholds, since the cost dimensions are never in Hybrid's favor.
6. **Reproduction.** A claimed material Hybrid advantage requires at least two
   independent controlled reproductions.
7. Hybrid's residual costs (ownership, copies, memory, toolchain) stay in W10's
   ledger even when a scenario looks favorable.

Gate #1 stays open until a written decision rule combines all required evidence.

## 8. Gate-required workload matrix

| Workload / scenario | Kind | Needs S0 | Status |
|---|---|---|---|
| W1 store (load, lookup, scan) | workload | no | Done: SMOKE, controlled S and M |
| W2 graph (one-hop, depth 2/4/8, mixed) | workload | no | Done: SMOKE, controlled S and M |
| W2.X1 power-law degree | W2 extension | no | Planned |
| W2.X2 long chains | W2 extension | no | Planned |
| W2.X3 duplicate/parallel edges | W2 extension | no | Planned |
| W2.X4 sparse u64 IDs | W2 extension | no | Planned |
| W2.X5 cap-boundary / TRUNCATED | W2 extension | no | Planned |
| W3.S1 evidence selectivity | W3 scenario | no | Planned |
| W3.S2 polarity/lineage/duplicate mixes | W3 scenario | no | Planned |
| W4.S1 intern-on-load | W4 scenario | no | Defined, not implemented |
| W4.S2 name -> entity resolution | W4 scenario | no | Defined, not implemented |
| W4.S3 ambiguous-name resolution | W4 scenario | no | Defined, not implemented |
| W4.S4 duplication-ratio sweep | W4 scenario | no | Defined, not implemented |
| W4.S5 name-length/cardinality distribution | W4 scenario | no | Defined, not implemented |
| W4.S6 repeated resolution | W4 scenario | no | Defined, not implemented |
| W5.S1 snapshot / restore | W5 scenario | **yes** | Blocked on S0 review; no interim form |
| W8.S1 incremental 1% invalidation | W8 scenario | **yes** | Blocked on S0 review and ADR-0008 (mutation semantics) |
| W10 hybrid boundary | workload | no (A/B); S0 for persistent variants | Bootstrap A/B done; C partial; falsification framework in section 7 |
| W11 build / artifact | workload | no | Smoke done; clean/uncached confirmation pending |
| X-MEM memory accounting | cross-cutting metric | no (host RSS); `stats` from S0 for persistent heap | Required "where technically measurable" |

Non-blocking (become blocking only on a material ranking reversal): X-CONC,
X-CANCEL, X-SOAK, W5.S2. Post-Gate: W6, W7, W9, W12.

## 9. Non-goals

No production API, no on-disk compatibility promise, no crash-consistency claim,
no SCIP/Tree-sitter/Planner/Host implementation, no Glean/MLIR/Mojo work (ADR-0004
tracks those). No workload here selects a language on its own.
