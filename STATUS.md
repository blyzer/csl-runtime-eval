# Runtime evaluation status

Gate #1: **PARTIAL — no architecture decision**. This is an empirical bootstrap,
not production CSL. Most results below are local macOS/arm64 evidence; a
controlled S-scale W1/W2 campaign for pure Rust/Zig has since run on a native
GitHub Actions Linux ARM64 runner (see "Controlled S-scale campaign (hosted)"
below). Controlled M-scale, the remaining Gate workloads, and hybrid-at-scale
still have not been executed.

This repository is P1-P5 of a larger program; [ADR-0004](adr/0004-program-roadmap-and-gates.md)
preserves the full roadmap (Gate #2/P15, Tree-sitter, Glean/Angle, MLIR, Mojo)
and the status taxonomy (IMPLEMENTED / IN PROGRESS / PLANNED / EXPERIMENTAL /
RESEARCH / DEFERRED) used below. Everything in this file is IMPLEMENTED or IN
PROGRESS; nothing from ADR-0004 beyond Gate #1 exists in this repo yet.

| Area | State | Implementation and proof |
|---|---|---|
| Initial audit | DONE | [initial-audit.md](results/raw/initial-audit.md); existing staged files and unrelated web assets preserved |
| Schemas, oracle and canonical digest | DONE | `schemas/*.json`, `oracle/validation.py`, `oracle/oracle.py`, [semantic contract](oracle/SEMANTICS.md); `ci/validate-schemas.sh`, `ci/validate-oracle.sh`; original golden digest preserved |
| E0 conformance suite | DONE | `tests/cases.py`: 30 semantic + 23 malformed cases; [latest results](results/conformance/latest.json): **212 PASS, 0 FAIL, 0 SKIP** across oracle/Rust/Zig/hybrid, including reversed insertion order and JSON reload |
| Python checks | DONE | `tests/test_semantics.py`, `test_harness.py`, `test_shared_view.py`: **17 test methods pass**, including semantic and campaign subtests; [log](results/raw/campaign-python-tests.log) |
| Pure Rust executable baseline | DONE | `prototypes/rust/src/main.rs`; info/load/query/workload; 53 process-conformance cases, formatting, Clippy with warnings denied, cargo test, debug/release builds; [log](results/raw/phase-candidate-checks.log). Cargo has no separate Rust unit tests; conformance is external and process-based |
| Pure Zig executable baseline | DONE | `prototypes/zig/src/{main,semantic}.zig`, `build.zig`; same 53 process cases, fmt/build/test; [log](results/raw/phase-candidate-checks.log) |
| Stable hybrid ABI A | DONE | `prototypes/hybrid/abi/csl_kernel.h`, Zig `src/abi.zig`, `rust/build.rs`, Rust wrapper; int32 status, ptr/len, private allocation header, by-value release, C++ guards, runtime version; Rust actually links and executes Zig. [smoke log](results/raw/phase-smoke.log) |
| ABI ownership/error diagnostics | DONE | Zig ABI tests cover null/zero/normal/repeated calls, invalid semantic bytes, after-close lifetime, 16 MiB payload, empty release, B sizing/no-write behavior; testing allocator detects leaks/invalid frees. `tests/abi_header.c` adds real C/C++ linkage/layout and C caller ASan/UBSan checks; [log](results/raw/phase-abi-diagnostics.log). Zig itself is not sanitizer-instrumented |
| Hybrid semantic bootstrap / E4 | PARTIAL | Whole `{fixture,query}` JSON batch executes in Zig and passes all 53 process cases. Rust controls serialization and owns the wrapper. No persistent kernel store, mutation or cancellation API is claimed |
| W1 | PARTIAL | Load/validate, indexed ID lookup and kind scan; **90 original bootstrap records** (3 queries × 10 repeats × 3 candidates), all full results equal oracle. RSS, elapsed, artifact size, lookup counts and source/toolchain metadata in [archived W1](results/bootstrap-20260927/w1/). Timings include whole-process loading and graph validation/indexing. No isolated entity-store timing or 1% incremental update API; E0 is read-only |
| W2 | PARTIAL | Indexed outgoing/incoming, bounded depths 2/4/8, mixed relations, cycle-heavy and high-fanout graphs; **540 original bootstrap records** (6 queries × 3 shapes × 10 repeats × 3 candidates), all equal oracle; [archived W2](results/bootstrap-20260927/w2/). Pass 1 now aligns pure-candidate data structures (see below); S/M and controlled kernel throughput remain pending |
| Deterministic generation | DONE | Streaming `harness/generate.py`: explicit entities/edges/seed/output, mixed/cycle/fanout shapes, SMOKE/S/M presets; reproducibility tested. Only SMOKE generated in CI |
| benchctl | DONE | `harness/benchctl.py`, `harness/builds.py`: prepare/validate/env/build/run/compare/report/w10/w11; process-only candidates, fail closed on normalized-result mismatch, schema-validated W1/W2/W10 records, explicit unavailable metrics |
| W10 A/B | DONE (bootstrap experiment) | `csl_kernel_experimental.h` and execute_into are separate from stable v1; **200 samples**, 5 sizes, 10 repeats, A/B and independent pure Rust/Zig allocation-copy controls. [records](results/w10/records.json), [latencies/tax](results/w10/boundary.json). Echo is not a CSL workload; tax includes ownership and copy costs |
| W10 C | PARTIAL | `harness/shared_view.py` bounds/version/snapshot/integrity/lifetime seam and tests; mmap implementation and measurements NOT STARTED |
| W11 smoke builds | DONE | Debug/release builds and **6 PASS** clean-output/no-op records, including real Zig-linked hybrid; [builds](results/w11/builds.json). Dependency/global/kernel caches may be warm; not fully uncached build claims |
| CI and manifest | DONE, remote executed | Executable `ci/*.sh`, `ci/manifest.py`, `.github/workflows/{smoke,s-scale}.yml`; [smoke log](results/raw/phase-smoke.log). Source manifest uses repository-relative paths and excludes itself/results/caches. `smoke` and the controlled `s-scale` campaign have both run and passed on hosted GitHub Actions (see hosted section below) |
| Gate-scale evaluation | PARTIAL | Controlled S repeats for pure Rust/Zig W1/W2 done on hosted ARM64 (see below); M repeats and remaining Gate workloads pending; typed/hash and phase-timing groundwork complete, no winner selected |

## Typed/hash representation pass 1 — 2026-09-28

Both pure candidates now use typed records, ID hash maps, outgoing/incoming
hash maps of edge vectors, hash sets and sorted traversal seeds/results.
[ADR-0002](adr/0002-matched-phase-baseline.md) records the pass and the remaining
hash implementation, query representation and allocation-lifetime differences.

`workload --profile` separates load, validation/index, query and result
encoding/hash phases. `benchctl run/compare --profile` validates each envelope
and full oracle result, recording native phases alongside process elapsed,
whole-process peak RSS, artifact/source/corpus hashes and SDK-workaround status.
It compares only pure Rust/Zig; hybrid retains its existing whole-fixture batch.

Profile conformance: **159 PASS, 0 FAIL, 0 SKIP** (oracle/Rust/Zig), including
reversed insertion order, multi-seed traversal caps, sparse u64 IDs, enum sorting,
and invalid typed records. [Evidence](results/conformance/phases.json).
Normal conformance: **212 PASS**, including hybrid. Python: **12 methods pass**,
including rejection of invalid durations and full-result mismatches.

**420 accepted profile records** (W1: 60; W2: 360), ten samples per query and
candidate, cover mixed W1 and mixed/cycle/fanout W2. Every full result equals the
oracle. Records and per-query phase medians are stored separately in
[results/phases](results/phases/). The original 630 unprofiled bootstrap records
are preserved in [results/bootstrap-20260927](results/bootstrap-20260927/);
current unprofiled smoke records reflect the new source and are not replacements
for a controlled comparison. Remote CI and controlled S/M remain unexecuted.

## Controlled S-scale campaign (hosted), pass 1 — 2026-09-28

Superseded by the pass-2 rerun below; kept for the record. The GitHub Actions
`S-scale paired evaluation` workflow ran on a native `ubuntu-24.04-arm` runner
(Neoverse-N2, 4 vCPU, kernel 6.17 aarch64), unblocking what this Mac's
SDK/load ceiling prevented. **180 controlled records, state PASS, 0 condition
failures, `sdk_workaround: false`.** Evidence (archived): [summary](results/s-scale-hosted-pass1-20260928/summary.json),
[manifest](results/s-scale-hosted-pass1-20260928/manifest.json), [records](results/s-scale-hosted-pass1-20260928/records.jsonl).

Comparing pure Rust and pure Zig at S scale (corpus/synthetic/S-campaign-mixed-20260928.json,
mixed shape, 10 repeats) reproduced the same asymmetries visible at SMOKE
scale in the pass-1 phase evidence ([results/phases-pass1-20260928](results/phases-pass1-20260928/)),
at ~150-200x the entity count:

- **Load phase**: Rust's JSON load ~2x faster than Zig's on every query
  (SMOKE: ~2.8ms vs ~5.5ms; S: ~486ms vs ~1000ms).
- **Index phase**: Zig's hash-index construction faster than Rust's
  (S: ~355-397ms vs ~572-661ms, Zig ~35-45% faster).
- **Result/encoding phase and peak RSS on larger result sets** (`scan-type`,
  `depth-8`): Zig ~2-2.3x faster to encode and ~2-2.3x less peak RSS than
  Rust, at both scales.
- **Net effect**: Rust won point lookups and shallow traversals (load
  dominates); Zig won large-result-set queries (encoding/memory dominates) —
  no overall winner across W1/W2 at this scale.

Pass 2 below found the "result" side of this gap was substantially a Rust
implementation artifact, not a language/runtime difference.

## Representation pass 2 and S-scale re-run — 2026-09-28

[ADR-0002 pass 2](adr/0002-matched-phase-baseline.md#representation-pass-2--phase-decomposition-and-rust-encode-fairness-2026-09-28)
found Rust's `encode()` built a `serde_json::Value` tree before serializing
(double-allocating every id/enum/evidence row), while Zig's encoder already
serialized typed structs directly. Fixed by serializing typed structs directly
in Rust too (digest verified byte-identical against the oracle); `query` was
also split from `materialize` in both candidates so the phase timings
distinguish traversal from result construction (`phase_detail_ns`, additive
alongside the unchanged `phases_ns`). Conformance unaffected: 212/159 PASS
before and after.

The same `S-campaign-mixed-20260928` corpus, oracle, query set, repetition
policy (10 repeats) and canonical validation were re-run on the same runner
label (`ubuntu-24.04-arm`), producing another **180 controlled records, 0
condition failures**. Evidence: [summary](results/s-scale-hosted/summary.json),
[manifest](results/s-scale-hosted/manifest.json), [records](results/s-scale-hosted/records.jsonl).
Pass-1 evidence is archived at [results/s-scale-hosted-pass1-20260928](results/s-scale-hosted-pass1-20260928/)
for direct comparison; this was a different runner instance, not fixed
hardware, so small (single-digit percent) deltas on unrelated queries are
runner variance, not signal.

| Query (Rust only; Zig deltas were all within ±3%, i.e. unchanged) | Before (median elapsed) | After | Delta |
|---|---|---|---|
| W1 scan-type | 3166.2 ms | 1659.2 ms | **-47.6%** |
| W2 depth-8 | 4233.4 ms | 2087.6 ms | **-50.7%** |
| W2 depth-4 | 1841.0 ms | 1386.7 ms | -24.7% |
| W2 outgoing / incoming / mixed-relations | 1192-1261 ms | 1088-1152 ms | -8.6% to -8.8% |
| W1 lookup-first / lookup-missing, W2 depth-2 | 1134-1180 ms | 1178-1184 ms | +0.2% to +4.2% (noise) |

Peak RSS on the two largest-result queries: `scan-type` 1507 MB -> 547 MB
(**-64%**); `depth-8` 2143 MB -> 638 MB (**-70%**). Phase detail confirms the
source: `scan-type`'s encode phase alone is 410 ms of the 513 ms new `result`
total (materialize 103 ms); `depth-8`'s encode is 643 ms of 806 ms
(materialize 164 ms) — consistent with removing a full extra allocation pass,
not with any traversal/query-logic change (`query`+`materialize` after the
split sums to within ~3% of pass-1's combined `query` phase on both queries,
confirming the split repartitions the same work rather than changing it).

**Which differences disappeared**: the "Zig wins large-result queries"
finding from pass 1 is gone. Rust is now faster than Zig on both `scan-type`
(1659 ms vs pass-1 Zig's unaffected ~1936 ms) and `depth-8` (2088 ms vs Zig's
~2289 ms) — the encode-side asymmetry was the dominant cause of that result,
not a fundamental representation or runtime property.
**Which differences shrank**: `depth-4` and the outgoing/incoming/mixed-relations
group, more modestly (materialize+encode contribute less to their smaller
result sets).
**Which differences remained/reversed direction**: none reversed in Zig's
favor; Rust is now equal-or-ahead on every W1/W2 query at S scale.
**Which differences grew**: none.

**Load-phase asymmetry is untouched** (Rust ~2x faster to decode than Zig,
same as pass 1) and **index-phase asymmetry is untouched** (Zig ~35-45%
faster to build indices) — pass 2 did not touch either implementation's
decode or index code, so this is the expected, unaffected baseline, not a
new finding. Language Gate #1 remains **OPEN**: this closes the specific
result-encoding fairness gap pass 1 flagged, it does not by itself select a
kernel language, and W3-W12, corpus M, and hybrid-at-scale are still
outstanding (see below).

This is Rust's `serde_json::Value` + Zig's typed records/manual allocation
showing up in measurement, not a kernel/query-engine difference; it does not
by itself justify an architecture choice. Pure Rust and pure Zig only — hybrid
has no S-scale measurement yet. Corpus M, workloads W3-W12, mutation/persistence
and hybrid-at-scale remain the same open items listed below.

## Decode/index asymmetry investigation (pass 3) — 2026-09-28

[ADR-0005](adr/0005-decode-index-asymmetry-investigation.md) investigates the
two asymmetries pass 2 left unexplained: Rust's ~2x faster JSON decode and
Zig's ~35-45% faster index construction. Both `decode` and `index` were split
into measurable sub-phases (`read`/`parse`, `entities`/`adjacency`/`sort`,
exposed as `phase_subdetail_ns`) without changing either candidate's
algorithm. Reading Zig 0.14.1's vendored source directly (not assumed) ruled
out one decode-gap hypothesis (Zig's default string handling should be
*cheaper* than Rust's, since `.alloc_if_needed` borrows unescaped strings
zero-copy where Rust always copies into an owned `String`) and identified the
leading, unconfirmed candidates as arena/chunk-growth allocation strategy and
parser/scanner maturity — neither isolated without a profiler, which this
iteration did not have available (Zig still cannot build locally on this
Mac). **No change was made to decode.**

For the index gap, source reading found the likely cause: Rust's
`std::collections::HashMap` defaults to SipHash-1-3 (deliberately
DoS-resistant, not speed-optimized); Zig's `std.AutoHashMap` defaults to
Wyhash (fast, non-cryptographic) — confirmed from `std/hash_map.zig`. This is
a standard-library default, not a language property. **Experiment**: swapped
Rust's `HashMap`/`HashSet` to an inline FxHash-style hasher (no new
dependency); verified byte-identical digest against the oracle and unchanged
106/106 conformance before trusting any timing. Controlled S-scale rerun
(S-pass3 vs S-pass2-fairness, same corpus/oracle/10-repeat protocol, archived
in [results/EVIDENCE-LEDGER.md](results/EVIDENCE-LEDGER.md)):

| | Rust index (pass2 -> pass3) | Zig index (unchanged code) | Gap |
|---|---|---|---|
| W1 scan-type | 538.8 ms -> 394.2 ms (-27%) | 349.9 ms -> 377.2 ms (noise) | Zig 35% faster -> Zig 4% faster |
| W2 depth-8 | 625.8 ms -> 403.3 ms (-36%) | 382.8 ms -> 378.0 ms (flat) | Zig 39% faster -> Zig 7% faster |

The hasher explains **most but not all** of the gap: Rust is now faster on
the `entities` and `sort` sub-phases, Zig remains ~13-17% faster on
`adjacency`. Across all nine W1/W2 queries, Rust's median *elapsed* time
improved ~14% (every query 9-18% faster); Zig stayed flat (±0-4%, as
expected since its code did not change). Conformance and digests unaffected.
A SMOKE-scale single sample had suggested Rust might overtake Zig entirely —
that did not hold at controlled S-scale, illustrating the pass 1/2/3
methodological lesson: single small-scale samples overstate and can even
flip an effect size relative to the controlled measurement.

**P4 Hybrid, reframed**: the pass-1 hypothesis ("Zig wins large-result
serialization, worth a language boundary") is falsified (pass 2). The
narrowed hypothesis this pass leaves standing — can Zig's index-construction
edge survive an FFI boundary — now has only a ~4-7% margin to work with
after the hasher fix, a materially harder bar for BoundaryTax to clear than
the original 35-45%. P4 is not closed, but has no large demonstrated
advantage to carry across a boundary right now. Language Gate #1 remains
**OPEN**.

## Hybrid at S scale (pass 4) — 2026-09-29

First controlled S-scale run including hybrid (run [36518543247](https://github.com/blyzer/csl-runtime-eval/actions/runs/36518543247),
commit `75e54c5`, same corpus/oracle/10-repeat protocol as pass 3): **270 records
(90 per candidate), all conformant, 0 condition failures**. Evidence:
[results/s-scale-hosted-pass4-hybrid-20260929](results/s-scale-hosted-pass4-hybrid-20260929/).
This run follows the fix of the Value-tree defect on both the fixture (input) and
response (output) sides of hybrid's Rust wrapper; the earlier hybrid run
(~5-7.7 s, ~4.25 GB regardless of query) was invalid for comparison and is not archived.

Median elapsed / peak RSS:

| Query | Rust | Zig | Hybrid | Hybrid vs Zig |
|---|---|---|---|---|
| lookup / depth-2 / incoming / outgoing | 841-916 ms / 377 MB | 1342-1386 ms / 490 MB | 1395-1440 ms / 660 MB | +3-4% time, +35% RSS |
| depth-4 | 1076 ms / 434 MB | 1538 ms / 577 MB | 1638 ms / 747 MB | +6.5%, +29% |
| depth-8 | 1769 ms / 638 MB | 2206 ms / 919 MB | 2498 ms / 1162 MB | +13%, +26% |
| scan-type | 1462 ms / 547 MB | 1893 ms / 764 MB | 2087 ms / 934 MB | +10%, +22% |

Reading: hybrid is never faster than pure Zig, and pure Rust (post-hasher) leads
both on every query. The residual cost of the boundary (fixture/response copies,
extra buffers) grows with result size. The narrowed P4 hypothesis — Zig's
index-construction edge surviving an FFI boundary — is **not supported**: that edge
is ~4-7% and the boundary costs 3-13% plus ~25-35% more memory. This is one runner
instance and a bootstrap hybrid (whole-fixture batch, no persistent kernel store),
so it does not rule out a persistent-store hybrid design. Gate #1 remains **OPEN**;
M scale and W3-W9/W12 are still pending.

## S-scale campaign protocol — 2026-09-28

[ADR-0003](adr/0003-s-scale-campaign.md) defines the serial paired runner in
`harness/campaign.py`: seeded query order, alternating candidate positions,
separate oracle/verification workers, retained warm-ups and per-sample machine
conditions. Every accepted result must match its schema-validated oracle tree,
including JSON value types. Source, corpus, artifact and reference hashes protect
campaign provenance. Python validation now has **17 passing test methods**.

The native controlled preflight on **this Mac** remains **BLOCKED**: Zig 0.14.1
cannot link its libc probe with the original SDK, the local SDK overlay is
present, and host load exceeds the declared ceiling. [Preflight evidence](results/s-scale-preflight/controlled/manifest.json).
The paired runner passed a 36-record exploratory SMOKE campaign locally. Native
and profile conformance remain **212/159 PASS**, with zero failures/skips.

The [GitHub Actions workflow](.github/workflows/s-scale.yml) passes actionlint
and, dispatched against a native `ubuntu-24.04-arm` runner, ran the preflight
clean (`sdk_workaround: false`) and completed the controlled S-scale W1/W2
campaign — see the hosted section above. Hosted VM results are their own scope
and do not imply bare-metal qualification. Controlled M evidence and the
remaining Gate workloads (W3-W9, W12), hybrid-at-scale, and mutation/persistence
remain outstanding.

## Measurements and provenance

[results/summary.json](results/summary.json) summarizes the original bootstrap run.
[results/phases/summary.json](results/phases/summary.json) summarizes pass 1. Raw records
retain every iteration, query, digest, corpus hash, seed, source hash, compiler,
OS/architecture, process peak RSS and artifact size. The repository has no HEAD
commit yet, so `candidate_commit` is null and dirty/source hashes identify the run.
No missing measurement is represented as zero. Measured timer zeros in tiny W10
calls reflect clock resolution, not zero-cost execution.

The candidates now match broad store data structures, not byte layout or every
algorithmic detail. Query timing includes evidence scanning, allocation and
sorting; it is not isolated ID lookup or traversal throughput. Index timing also
includes semantic validation. Different allocators, hash implementations and
result encoders remain. Bytes/entity is whole-process peak RSS divided by entity
count, not entity layout size. Timings do not justify an architecture verdict.

## Remaining limitations

- Local Zig was installed in `.venv`; macOS 27 required a local SDK text-stub
  workaround and archive repacking. See [TOOLCHAINS.md](TOOLCHAINS.md) and
  [sdk.json](results/raw/sdk.json). Use a supported native compiler/SDK pair for
  controlled Gate measurements.
- `cargo miri test` was attempted: **NOT AVAILABLE** for this nightly, therefore
  SKIPPED; [miri.log](results/raw/miri.log). No Miri or Zig ASan coverage claimed.
- Native index persistence, incremental invalidation, cancellation, shared mmap,
  cross-compilation and production planning are not implemented or claimed.
- No unresolved failure remains in the executed local validation suite.
  [Resolved failures](results/raw/resolved-failures.md) records the fixes.

Next evidence-producing step: repeat the phase baseline on a supported native
compiler/SDK pair, then run controlled S-scale repeats with machine/load and
execution order recorded. Document any further tuning for both pure candidates.
