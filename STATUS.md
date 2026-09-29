# Runtime evaluation status

Gate #1: **PARTIAL — no architecture decision**. This is an empirical bootstrap,
not production CSL. Most results below are local macOS/arm64 evidence; a
controlled S-scale W1/W2 campaign for pure Rust/Zig has since run on a native
GitHub Actions Linux ARM64 runner (see "Controlled S-scale campaign (hosted)"
below). Controlled M-scale and hybrid-at-scale (W1/W2) have since run on the same
runner class; the remaining Gate workloads (W3-W9, W12) have not been executed.

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

## M-scale controlled (hosted), pass 1 — 2026-09-29

Same three candidates on a native `ubuntu-24.04-arm` runner, M corpus (1M entities /
10M relations / 10M evidence rows, seed 20260928), 10 repeats, streaming oracle
([ADR-0006](adr/0006-streaming-oracle.md)) with byte-level verification. Run
[36538603932](https://github.com/blyzer/csl-runtime-eval/actions/runs/36538603932),
commit `c164f89`. **State PASS, classification controlled, 270 records (90 per
candidate), all byte-identical to the oracle, 0 condition failures,
`sdk_workaround: false`.** Evidence: [results/m-scale-hosted-pass1-20260929](results/m-scale-hosted-pass1-20260929/).
A first attempt (run 36530930295) was rejected after 32 samples by the strict swap-out
rule (34 pages, no real memory pressure); swap is now disabled on the runner before
the campaign (recorded in `hosted-context`) instead of loosening the rule.

Median elapsed / peak RSS:

| Query | Rust | Zig | Hybrid | Hybrid vs Zig |
|---|---|---|---|---|
| lookup / depth-2 / incoming / outgoing / mixed | 10.3-11.0 s / 3.9 GB | 14.5-15.1 s / 5.2 GB | 14.9-15.3 s / 7.3 GB | +1-4% time, +40% RSS |
| depth-4 | 10.8 s / 4.0 GB | 15.0 s / 5.3 GB | 15.5 s / 7.4 GB | +3%, +40% |
| depth-8 | 12.7 s / 4.5 GB | 16.7 s / 6.1 GB | 17.5 s / 8.2 GB | +5%, +34% |
| scan-type | 17.3 s / 5.6 GB | 21.0 s / 8.0 GB | 22.7 s / 10.7 GB | +8%, +34% |

Reading: the S-scale picture holds at 10x the data and is now controlled. Rust is
fastest on every query (Zig ~1.3-1.4x behind); hybrid is never faster than pure Zig,
its time cost stays small (1-8%) but it needs ~34-40% more memory, the same
direction as at S. Elapsed grew ~7-8x for 10x the data on every candidate (no scaling
cliff). The local exploratory M run agrees on ordering and time ratios; its RSS
numbers (swap active) do not agree and should be disregarded. Absolute times differ
between the Mac (faster) and this runner, so compare ratios, not seconds. Still one
runner instance and a bootstrap hybrid (whole-fixture batch), so it does not rule out
a persistent-store hybrid. Gate #1 remains **OPEN**: W3-W9 and W12 are defined in the program map but not
implemented or run; scope and ordering are proposed in [ADR-0007](adr/0007-gate1-remaining-workloads.md).

## M-scale, local exploratory (pass 5) — 2026-09-29

First M-scale evidence (1M entities / 10M relations / 10M evidence rows, 2.16 GB
fixture), run **locally on this Mac** because hosted 16 GB runners cannot hold the
old oracle. Enabled by the streaming oracle ([ADR-0006](adr/0006-streaming-oracle.md)):
the M oracle prepared all nine references in ~8 minutes with a few GB, and candidate
output is verified by byte comparison. **270 records (90 per candidate), all
byte-identical to the oracle.** Evidence: [results/local/m-local-20260929](results/local/m-local-20260929/)
(`references/` not committed; regenerable from the seed).

**Classification: exploratory, not controlled.** 232 of 270 samples recorded a
machine-condition failure (one-minute load ~10-13 on 10 CPUs; 34 also saw swap-out),
Zig runs through the local SDK overlay, and this is a laptop, not a quiet runner.
Read it as direction and rough magnitude only.

Median elapsed / peak RSS (10 samples each):

| Query | Rust | Zig | Hybrid | Hybrid vs Zig |
|---|---|---|---|---|
| lookup / depth-2 / incoming / outgoing / mixed | 6.4-6.8 s / 4.2 GB | 9.5-10.5 s / 5.2 GB | 10.4-11.2 s / 4.7-6.2 GB | +5-12% |
| depth-4 | 6.4 s / 4.2 GB | 9.8 s / 5.3 GB | 10.5 s / 6.2 GB | +7% |
| depth-8 | 8.9 s / 4.8 GB | 12.4 s / 6.0 GB | 13.1 s / 5.2 GB | +6% |
| scan-type | 12.0 s / 5.8 GB | 16.2 s / 7.4 GB | 16.7 s / 5.5 GB | +3% |

Reading: the S-scale ordering holds at 10x the data: Rust fastest on every query
(~1.4-1.5x ahead of Zig), Zig ahead of hybrid, hybrid's boundary cost a modest
3-12%. Elapsed time grew ~7-8x for 10x the data on every candidate (fixed process
costs), i.e. no candidate shows a scaling cliff. Hybrid's RSS is not consistently
higher than Zig's here (5.2-5.5 GB vs 7.4 GB on `scan-type`) unlike S; with swap
activity during samples, peak RSS is less trustworthy than at S, so I make no RSS
claim from this run. Repeat on a quiet machine or a large native runner before
treating any M number as controlled. Gate #1 remains **OPEN**; W3-W9 and W12 are
still pending (see [ADR-0007](adr/0007-gate1-remaining-workloads.md)).

## Gate #1 Track A: W2.X, W3, W4 and X-MEM — 2026-09-29

Scope and definitions: [ADR-0007](adr/0007-gate1-remaining-workloads.md). All runs are
controlled hosted `ubuntu-24.04-arm`, S scale, 10 repeats, `sdk_workaround: false`, swap
disabled, **0 condition failures**. Every accepted record is byte-identical to the streaming
oracle. Evidence (each pass archived separately, nothing overwritten):

* [results/gate1-pass1-20260929](results/gate1-pass1-20260929/): W2.X1-X5, W3.S1-S2, W4.S1-S6
  (19 scenario cells, **1,860 records, all conformant**; run 36556641803, W4.S5 completed by
  run 36558262587 after the first attempt hit the 128 KiB argv limit, kept under `attempts/`).
  Analysis: `analysis.md` / `analysis.json`.
* [results/gate1-pass2-w4-20260929](results/gate1-pass2-w4-20260929/): W4 rerun after the Rust
  name-index hasher fix below (12 cells, **240 records, all conformant**; run 36558602577).
* [results/s-scale-hosted-pass5-20260929](results/s-scale-hosted-pass5-20260929/): paired W1/W2
  S baseline with the current binaries on the *same corpus* as pass 3 (270 records; run 36558359483).

Semantic gates unchanged: normal conformance **212 PASS**, profile **159 PASS**, Python **42 tests**.
New candidate options (W3/W4 ids, lazy interned name index, in-process repeated resolution,
opt-in counting allocator) leave every existing output byte-identical when unused. The Rust
counting-allocator wrapper costs about **1.4-1.9%** of Rust elapsed even when off (local
same-machine A/B, 12 interleaved runs); that bias is against Rust and changes no ordering.

### W2.X1-X5 (elapsed medians; Zig/Rust, Hybrid/Zig)

| Scenario | Rust ms | Zig ms | Zig/Rust | Hybrid/Zig |
|---|---|---|---|---|
| X1 power-law (hub out / depth / mixed) | 1,640-1,740 | 2,240-2,340 | 1.34-1.37 | 1.11-1.13 |
| X1 power-law (incoming to hub) | 1,041 | 1,655 | 1.59 | 1.05 |
| X1 power-law (tail entity) | 780-790 | 1,410-1,430 | 1.80 | 1.02-1.04 |
| X2 single chain (depth up to 64) | 110-135 | 185-188 | 1.39-1.70 | 1.03-1.04 |
| X3 duplicates x4 | 750-880 | 1,280-1,385 | 1.58-1.70 | 1.03-1.05 |
| X4 sparse u64 IDs | 820-1,930 | 1,570-2,570 | 1.33-1.94 | 1.03-1.11 |
| X5 cap boundary | 830-1,080 | 1,390-1,650 | 1.52-1.68 | 1.02-1.06 |

Rust is faster on every query of every W2 extension (1.33-1.94x) and Hybrid is never faster
than Zig. X5 places `max_paths` at 1, 2, exact-1, exact and exact+1 of the inspected-edge
count in both directions: all three candidates match the oracle byte-for-byte at the boundary,
including the conservative TRUNCATED-at-exactly-the-cap rule.

### W3 evidence (W3.S1 selectivity; Rust | Zig ms; result phase = materialize + encode)

| Selected evidence | Rust elapsed | Zig elapsed | Zig/Rust | Rust result | Zig result | Hybrid/Zig |
|---|---|---|---|---|---|---|
| ~0.1% (epoch-4) | 812 | 1,397 | 1.72 | 32 | 27 | 1.04 |
| ~1% (epoch-3) | 840 | 1,413 | 1.68 | 40 | 34 | 1.03 |
| ~20% (min VERIFIED) | 984 | 1,568 | 1.59 | 187 | 189 | 1.06 |
| 100% | 1,800 | 2,342 | 1.30 | 923 | 883 | 1.14 |

Elapsed grows with the selected evidence and the Rust lead shrinks from 1.72x to 1.30x. On the
result phase alone the candidates are level: Rust materializes faster (168 vs 223 ms at 100%),
Zig encodes faster (659 vs 756 ms). W3.S2 (30% NEGATIVE, skewed lineage, duplicate and
conflicting rows) shows the same shape (100% selection: Rust 1,959, Zig 2,451, Hybrid/Zig 1.13).

### W4 string interning (pass 2 after the hasher fix; Rust | Zig)

| Cell | Intern ms | Lookup p50 / p99 ns | Live heap after index | Allocations |
|---|---|---|---|---|
| S1/S2 all-unique names (200k) | 35-40 \| 37-42 | 80 / 304 \| 72 / 296 | 60.3 \| 68.3 MB | 438k \| 238k |
| S3 ~100 entities per name | 7.7 \| 4.9 | 64 / 136 \| 48 / 104 | 60.3 \| 68.3 MB | 251k \| 46k |
| S4 unique 100% / 50% / 10% / 1% | 42 / 28 / 15 / 7.7 \| 48 / 28 / 10 / 5.0 | 72 / 72 / 64 / 64 \| 64 / 64 / 56 / 48 | 60.3 \| 68.3 MB | 439k-251k \| 238k-46k |
| S5 name length 8 / 64 / 512 | 21 / 28 / 68 \| 19 / 27 / 50 | 80 / 112 / 232 \| 72 / 88 / 160 | 54 / 76 / 256 \| 65 / 86 / 177 MB | 339k-339k \| 138k |
| S6 1M lookups, zipf hot names | 12 \| 9.9 | 64 / 544 \| 48 / 272 | 60.3 \| 68.3 MB | 263k \| 58k |

Load is ~2x faster in Rust (54 vs 102 ms) in every cell (512-char names: 122 vs 264 ms).
Rust makes 1.8-5.5x more allocations.

### X-MEM

Host RSS (every timed record): Zig is 1.3-1.5x Rust, Hybrid ~1.8x Rust, in all 13 scenarios.
Candidate-reported live heap (untimed `--stats` pass, requested-bytes model): Zig 68.3 vs Rust
60.3 MB after index on the 200k-entity W4 corpus (+13%); peak 108.9 vs 80.1 MB with 200k unique
names (+36%). Zig's arena additionally retains 229 MB (`retained`), which the requested-bytes
figure does not show; Rust's system allocator internals are not observable (`null`).
Derived from the W4 sweeps: name-index cost per unique string **89 B (Rust) vs 193 B (Zig)**
(peak-live delta between 100% and 1% unique, 198,000 strings, ~0.95 allocations each in both).
Heap versus name length, recorded as four separate items:

1. **Observed slope**: live heap per entity grows at 2.0 B/char in Rust vs 1.0 B/char in Zig
   (64 to 512 chars: 382 to 1,278 vs 430 to 885 B per entity).
2. **Code evidence**: Rust's `Fixture` decodes `strings` into owned `String`s
   (`prototypes/rust/src/model.rs`) while Zig's decode is in `std.json` (ADR-0005).
3. **Hypothesis**: owned decoded strings contribute to the Rust slope.
4. **Causal status**: **not isolated / not profiled.** No W4 tuning is authorized from this
   hypothesis alone. Hybrid has no
in-process counters (`null`, reason recorded; ADR-0007). Heap counters include the fixture
bytes read from disk and exclude the measurement arrays.

### Remaining Rust / Zig differences

* **Decode/load: Rust ~2.0-2.3x faster**, stable in all 13 scenarios and both hosts.
* **Index phase — status: see "Index-stability experiment" (Rust material advantage within controlled same-instance pairs; cross-instance stability unresolved; stable Zig advantage falsified).** Pass 3
  measured Zig 4-7% faster (kept as history; it is not surviving evidence); the rerun on the
  *same corpus digest* measured Rust 8-17% faster (Rust index ~400 to ~320 ms, Zig ~388 to ~360 ms),
  and in the extensions Zig ranges from 4% to 85% slower. Local same-machine A/B shows the Rust
  code changes since pass 3 cost +1-2%, so the flip is not code-attributable: hosted-instance
  variance in this phase (about +/-20%) exceeds the margin P4 depends on.
* **Result phase: level** (Rust materialize faster, Zig encode faster).
* **Memory: Zig higher** (RSS 1.3-1.5x, peak heap +36%, arena retention) but Zig makes 1.8-5.5x
  fewer allocations; string ownership differs 2x per character.
* **Lookup latency: p50 level (48-112 ns); Rust tails worse on hot-name workloads** (p95 544 vs
  184 ns len64-zipf; p99 544 vs 272 ns S6).
* W4 and W3 `elapsed` includes oracle-check bookkeeping (round-0 digest, `lookup_ns` output
  serialization; Rust builds a JSON tree for 1M numbers), so compare phases and lookup
  percentiles there, not elapsed (S6: Rust 1,101 vs Zig 526 ms elapsed with equal p50).

### Hypotheses tested

* **FALSIFIED: "the Fx-style hasher is a safe general fix".** With 8-character structured names
  it made Rust's name-index build 12x slower (282 vs 23 ms Zig) and lookup p50 ~14x slower
  (1,016 vs 72 ns); reproduced in two independent runs. Std SipHash: 12 ms / 42 ns locally,
  21 ms / 80 ns hosted (pass 2). ADR-0005's hasher result holds for u64 keys only; hasher
  choice must be made per key type. ID maps keep the Fx hasher; the name index uses SipHash.
* **NOT REPRODUCED: "Zig keeps a ~4-7% index-construction advantage"** (the P4 premise).
  Classification: `STABLE ZIG INDEX ADVANTAGE FALSIFIED`. The same-corpus rerun reversed it,
  and the preregistered same-instance paired experiment found a material Rust advantage (see
  "Index-stability experiment").
* **Held:** Rust fastest and Hybrid never faster than Zig in all 13 scenarios (Hybrid +2-14%,
  RSS +24-35% over Zig); no session/persistence result is used to rescue Hybrid.
* **Held (refined):** pass 2's "encode asymmetry gone" is confirmed at phase level.

Gate #1 remains **OPEN**; nothing here selects Rust, Zig or Hybrid.

## Index-stability experiment (preregistered, single run) — 2026-09-29

Protocol fixed before the run: [preregistration](results/preregistration/gate1-materiality-and-index-stability-20260929.md)
Part A. One hosted instance, the same S corpus as pass 3 (digest `7ea1c788...`), Rust and Zig
interleaved, W1 x 40 repeats = **120 pairs**, 240 records, all byte-identical to the oracle, 0
condition failures, `sdk_workaround: false`. Evidence: [results/index-stability-20260929](results/index-stability-20260929/)
(run 36588754955, commit `ee8be24`). No implementation was changed for it.

| Phase | Median Zig/Rust | 95% CI | Pairs with Rust faster |
|---|---|---|---|
| index (total) | **1.164** | [1.140, 1.197] | 99% |
| entities | 1.655 | [1.637, 1.671] | 100% |
| adjacency | 1.113 | [1.094, 1.149] | 90% |
| sort | 1.316 | [1.312, 1.318] | 100% |

Absolute index time varies a lot run to run inside the instance (per-repeat-block medians: Rust
262-377 ms, CV 17%; Zig 331-405 ms, CV 11.5%), but the interleaved pairs cancel most of it.

**Preregistered outcome: `Rust materially faster within this instance (cross-instance stability
not established)`.** This is not the "difference within run variance" case, so the investigation is
*not* closed as within-variance by me; nothing was tuned and nothing further will be searched.

Across hosted instances on the same corpus: pass 3 measured Zig/Rust 0.93-0.96 (Zig 4-7% faster);
pass 5 measured 1.08-1.17; this experiment 1.164. Two later instances agree on the sign and
magnitude (Rust ~13-16% faster); the earlier one disagrees. Instance and build are confounded (the
code between pass 3 and pass 5 differs only in Track A additions, none in the index path; a local
same-machine A/B showed those additions cost Rust +1-2%), so the disagreement cannot be attributed
to either.

**Classification (2026-09-29, after review):**

`RUST MATERIAL ADVANTAGE ESTABLISHED WITHIN CONTROLLED SAME-INSTANCE PAIRS; CROSS-INSTANCE STABILITY NOT ESTABLISHED.`

`STABLE ZIG INDEX ADVANTAGE FALSIFIED.` (The historical 4-7% Zig advantage of pass 3 is kept as
history and is closed as falsified.)

Kept distinct on purpose:

* within-instance material Rust advantage: **established** (120 interleaved pairs, median Zig/Rust
  1.164, 95% CI [1.140, 1.197], Rust faster in 99% of pairs, entities/adjacency/sort all in the same
  direction);
* cross-instance stability / generalization: **unresolved** (historical results changed sign; build
  and instance remain confounded);
* stable Zig advantage: **falsified**.

No further index tuning or winner-seeking experiments are run for Gate #1.

## S0 v0 implemented in Rust and Zig — 2026-09-29

Contract: [S0 semantics](oracle/SESSION-SEMANTICS.md) and [JSONL binding](oracle/SESSION-BINDING-JSONL.md)
(final v0), mutation rules in [ADR-0008](adr/0008-mutation-semantics.md). `csl-eval-{rust,zig} session
--repository DIR` implements open (fixture | empty), query, mutate, state_digest, snapshot, restore,
stats, close, and answers `cancel` with `UNSUPPORTED` (no optional capability advertised). Mutation
strategy in both is **`full-rebuild`** (explicitly reported at `open`; every derived structure is
rebuilt inside `mutate`); it is the correctness reference and a baseline, **not** an incremental
implementation, so it does not satisfy W8. Snapshots are candidate-native binary files in the
snapshot repository. **Hybrid has no S0 implementation:** it would need a persistent Zig kernel ABI
that does not exist (decision requested, see the report).

Conformance is enforced by an independent oracle-side model
([oracle/session_model.py](oracle/session_model.py), never sharing code with a candidate) and the
runner [harness/s0.py](harness/s0.py): **Rust 312 checks, Zig 312 checks, 0 failures each** (digest,
generation, error codes, byte-identical query results, restore, cold restore in a fresh process,
context mismatch, chunked responses and requests, 200 seeded fuzz batches; Zig also passes 1,000).
Existing behavior is unchanged: normal conformance 212 PASS, profile 159 PASS, one-shot outputs
byte-identical, **49 Python tests**. Minor spec gaps the two implementations resolved identically are
now fixed in SESSION-SEMANTICS section 8.1.

S-scale conformance check ([results/s0-scale-sanity-20260929](results/s0-scale-sanity-20260929/),
100k entities / 1M relations / 1M evidence rows, one run each, **local Mac, exploratory; not a W5/W8
measurement**): both candidates reproduce the oracle model's `state_digest` over 209,554,927
canonical bytes before mutation, after a small mutation and after restore, reproduce oracle query
results through chunked responses, and restore a snapshot in a fresh process. Informal figures:
`state_digest` 0.93 s (Rust) / 0.70 s (Zig); snapshot ~59 MB in both; small-batch `mutate` under
full rebuild 0.79 s / 0.58 s; cold restore total time-to-first-query 1.08 s / 0.75 s. Two harness
bugs found and fixed on the way (unbuffered pipe reads made chunked queries look 50x slower;
`bool` accepted as an id in the model); neither touched candidate behavior.

## Incremental mutation and the experimental persistent Hybrid boundary — 2026-09-29

Implemented before any W5/W8 measurement (nothing here is a result):

* **Incremental mutation** in Rust and Zig (`session --strategy incremental`): entity map with
  reference counts, sorted adjacency, evidence slot store with a row index, in-place name index,
  overlay validation, atomic apply; conforms to ADR-0008 section 14. `full-rebuild` stays the
  correctness reference and baseline and is reported as such. Both pass the S0 runner
  (845 checks per strategy, adversarial no-false-CURRENT deltas included, fuzz 1,000); injected
  faults (stale name index, missing reference check) were caught by the runner. Every response now
  carries `service_ns`; `stats` carries `derived_rebuilds_total` so a fallback rebuild is visible.
* **Experimental persistent Hybrid boundary** (W10; **not** the stable ABI v1, not a production API):
  `prototypes/hybrid/abi/csl_session_experimental.h`, a coarse C ABI with one call per S0 request
  (create, open, query, mutate, state_digest, snapshot, restore, stats, cancel, close, plus a
  request fallback). Zig owns the store (the same S0 Engine as pure Zig); Rust owns the process,
  JSONL and chunk framing and never deserializes request bodies. Requests cross by pointer (no
  copy); responses are Zig-allocated copies handed to Rust and released once. No per-entity calls,
  no shared memory. It passes the S0 runner for both strategies (845 checks each); the stable v1
  header, ABI diagnostics, hybrid conformance (53/53) and one-shot outputs are unchanged. It is
  **not** to be optimized: if it consumes a material Zig advantage or Hybrid is Pareto-dominated
  under the preregistered criteria, the result is recorded and Hybrid optimization stops.
* **BoundaryTax as instrumented** (`stats.boundary`): FFI call wall time on the Rust side minus the
  engine's `service_ns` for that call, per operation; plus bytes crossing, copies, allocations,
  releases, ownership transitions and Rust-side wrapper time (`wrapper_ns_total`, *not* tax).
  `heap_breakdown` splits `kernel_zig` and `wrapper_rust`.
* **Boundary artifacts disclosed before measuring** (described, not fixed): (1) Rust copies inline
  response lines to patch its own `service_ns`, which pure candidates do not; (2) chunk framing
  (base64 + SHA-256) runs in Rust in the hybrid but in Zig/Rust in the pure candidates; (3) every
  large result is copied once out of the Zig arena; (4) the Rust counting allocator is compiled
  into the hybrid binary (off outside `session`); (5) both Zig-engine candidates parse whole
  requests inside the engine, so there is no asymmetry there. If measurement shows one of these
  makes the comparison unfair, that is the only permitted reason to touch the boundary.

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
