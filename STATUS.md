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

## Controlled S-scale campaign (hosted) — 2026-09-28

The GitHub Actions `S-scale paired evaluation` workflow ran on a native
`ubuntu-24.04-arm` runner (Neoverse-N2, 4 vCPU, kernel 6.17 aarch64), unblocking
what this Mac's SDK/load ceiling prevented. **180 controlled records, state
PASS, 0 condition failures, `sdk_workaround: false`.** Evidence: [summary](results/s-scale-hosted/summary.json),
[manifest](results/s-scale-hosted/manifest.json), [records](results/s-scale-hosted/records.jsonl),
[runner context](results/hosted-context/run.json). Oversized per-query reference
dumps (up to 122 MiB) are reproducible from this run and are not stored in git;
see `results/s-scale-hosted/references/` in the workflow's uploaded artifact.

Comparing pure Rust and pure Zig at S scale (corpus/synthetic/S-campaign-mixed-20260928.json,
mixed shape, 10 repeats) reproduces the same asymmetries already visible at
SMOKE scale in [results/phases](results/phases/), now at ~150-200x the entity
count, which is evidence the pattern is structural rather than noise:

- **Load phase**: Rust's JSON load is consistently ~2x faster than Zig's across
  every query (SMOKE: ~2.8ms vs ~5.5ms; S: ~486ms vs ~1000ms).
- **Index phase**: Zig's hash-index construction is consistently faster than
  Rust's (SMOKE: ~0.8ms vs ~0.8-0.9ms roughly tied; S: ~355-397ms vs ~572-661ms,
  Zig ~35-45% faster).
- **Result/encoding phase and peak RSS on larger result sets** (`scan-type`,
  `depth-8`): Zig is ~2-2.3x faster to encode and uses ~2-2.3x less peak RSS
  than Rust, at both scales.
- **Net effect**: which candidate has the lower median *elapsed* time depends on
  the query shape (Rust wins point lookups and shallow traversals where load
  dominates; Zig wins large-result-set queries where encoding/memory dominates)
  — there is no overall winner across W1/W2 at this scale.

This is Rust's `serde_json::Value` + Zig's typed records/manual allocation
showing up in measurement, not a kernel/query-engine difference; it does not
by itself justify an architecture choice. Pure Rust and pure Zig only — hybrid
has no S-scale measurement yet. Corpus M, workloads W3-W12, mutation/persistence
and hybrid-at-scale remain the same open items listed below.

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
