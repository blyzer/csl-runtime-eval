# Gate #1 controlled W5 and W11/R4 package

This package is based on source baseline `ce6a895d9ec1ec9011b0658f1ba6338f21651e13`. Apply it as one commit whose first parent is exactly that baseline; the workflow rejects branches with intervening commits. It adds only opt-in persistence phase timing, W5/W11 execution and reporting infrastructure. Candidate persistence formats, integrity behavior, ID generation, data structures and algorithms are unchanged. Hybrid remains outside W5 because the preregistered Gate #1 rule already eliminated it; W11 still qualifies all three artifacts.

## Run on the controlled runner

Publish this patch as a commit whose history contains the baseline above, then dispatch the dedicated workflow from that commit:

```sh
gh workflow run gate1-controlled.yml \
  --repo blyzer/csl-runtime-eval \
  --ref <patch-branch> \
  -f runner=ubuntu-24.04-arm \
  -f w5_repeat=30
gh run watch --repo blyzer/csl-runtime-eval
```

The workflow uses a clean checkout, Linux ARM64 `ubuntu-24.04-arm`, Rust `nightly-2026-08-08`, Zig `0.14.1`, and Python `3.12` with `jsonschema==4.23.0`. It disables Actions build caches. Cargo dependencies are downloaded into a fresh per-run `CARGO_HOME`; each build uses independent clean output/cache roots. The pinned corpus is regenerated/validated by the existing W5 harness and its digest is retained in W5 manifests.

The single workflow job runs, in order: clean-root W11/R4 builds and reproducibility checks; unit/smoke tests; normal and profile conformance; S0 conformance; then W5 against an unmodified `ce6a895` control build and the opt-in instrumented build. Each W5 arm has at least 30 repetitions on the same runner. No persistence implementation correction is applied.

## W5 phase records

Set `CSL_W5_DIAGNOSTICS=1` to emit `CSL_W5_DIAG` records on stderr. The normal response JSON and snapshot bytes are unchanged. Rust records logical-view preparation, temporary row preparation, encode, embedded integrity checksum, content-derived snapshot-ID generation, physical write/read, checksum verification, parse, logical-table aggregation, logical-state-to-fixture conversion, derived-store construction and internal total time. Zig records corresponding row preparation, encode, checksum, snapshot-ID generation, file I/O, parse, row-table materialization, derived-store construction and total time. Zig has no Rust-style fixture conversion; that phase is reported as zero/not applicable. Candidate-native phase boundaries and representations are retained.

`harness/w5.py` attaches stderr diagnostic records to preparation and restore samples. It also retains snapshot bytes, bytes processed, child peak RSS, and candidate heap/allocation counters. The existing protocol `service_ns` fields are the comparable total service times. The overhead-control arm builds unmodified candidate artifacts from the exact `ce6a895` tree; the diagnostic arm builds the same source plus opt-in timing/output hooks. Matched W5 sample IDs and paired bootstrap intervals bound instrumentation overhead, including timer and stderr costs. Heap counters remain process/session scoped rather than phase-attributed.

## W11/R4 outputs

The W11 helper creates two independent clean output roots for each candidate, records direct build elapsed time, `/usr/bin/time -v` peak RSS, artifact hashes/sizes and artifact info, then performs a clean rebuild at the first identical absolute path. It compares first-build hashes across roots and same-path rebuild hashes on Linux ARM64. It runs startup/EOF and open/close execution smokes for each artifact. Cargo dependency trees, Python package inventory, toolchain versions, runner details, swap state, source commit and corpus hash are retained.

The uploaded artifact `gate1-controlled-ubuntu-24.04-arm-<run-id>` contains `results/gate1-controlled/report.md`, raw W5 JSONL/manifest data, W11 build-root evidence, test/conformance logs, S0 results, dependency/toolchain inventory, runner metadata and swap context. A failed or blocked run still uploads available evidence.

## Review boundaries

This run does not authorize removing Rust's snapshot-ID hash, changing checksum validation, normalizing Rust maps to Zig arrays, optimizing temporary vectors, changing representations, or selecting a runtime. Diagnostic timing values are evidence only after the runner job passes its conformance gates and the report confirms that records were collected for both candidates.
