# Controlled S-scale campaign ledger

Maps the informal `S-pass1` / `S-pass2-fairness` / `S-pass3-investigation`
labels used in STATUS.md/ADR discussion to their exact, reproducible evidence.
Never overwritten; append a row when a new controlled S campaign runs.

| Label | Path | Run | Commit | Source digest | Corpus digest | Repeats | rustc | zig | Runner |
|---|---|---|---|---|---|---|---|---|---|
| S-pass1 (pre-fairness) | [results/s-scale-hosted-pass1-20260928/](s-scale-hosted-pass1-20260928/) | [36396614497](https://github.com/blyzer/csl-runtime-eval/actions/runs/36396614497) | `73f68c22dbca76b7b2f405e3cd9fade28deecef7` | `f7c585d4d216aad92e073acd15a18d3c24bdc285ed765942be3190d81b77198e` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass2-fairness (Rust encode fix, query/materialize split) | [results/s-scale-hosted/](s-scale-hosted/) | [36405210578](https://github.com/blyzer/csl-runtime-eval/actions/runs/36405210578) | `3febcdd442d5d4ba07269442e757625006467216` | `80ea43ca27b2578586a5c0765bc01e1f933f446feb49ec768ad14c61da3081fc` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass2-fairness (archived) | [results/s-scale-hosted-pass2-20260928/](s-scale-hosted-pass2-20260928/) | [36405210578](https://github.com/blyzer/csl-runtime-eval/actions/runs/36405210578) | `3febcdd442d5d4ba07269442e757625006467216` | `80ea43ca27b2578586a5c0765bc01e1f933f446feb49ec768ad14c61da3081fc` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass3-investigation (decode/index sub-phase instrumentation + Rust FxHash-style hasher) | [results/s-scale-hosted/](s-scale-hosted/) | [36508141357](https://github.com/blyzer/csl-runtime-eval/actions/runs/36508141357) | `b1a60cfad70df73b99a43fe95a4e071fc2109eba` | `d45577a0660a49a2fee107f8e4c49405a36acf038c799c085e6769590a5e8333` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass4-hybrid (W10 Hybrid, input/output marshaling fixed; rust+zig+hybrid) | [results/s-scale-hosted-pass4-hybrid-20260929/](s-scale-hosted-pass4-hybrid-20260929/) | [36518543247](https://github.com/blyzer/csl-runtime-eval/actions/runs/36518543247) | `75e54c561aba9d11f03cf9b6f2594909214446e8` | `ed7f6103d01afa6ff20aa7acc0eb852725d375771bdf913d349fac163fa55d8d` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| Gate1-pass1 (W2.X1-X5, W3.S1-S2, W4.S1-S6; Rust name index on Fx hasher) | [results/gate1-pass1-20260929/](gate1-pass1-20260929/) | [36556641803](https://github.com/blyzer/csl-runtime-eval/actions/runs/36556641803) + W4.S5 [36558262587](https://github.com/blyzer/csl-runtime-eval/actions/runs/36558262587) | `ab22b95` / `aa29500` | see per-cell manifest | per-cell (seed 20260929) | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| Gate1-pass2-W4 (W4.S1-S6; Rust name index on SipHash) | [results/gate1-pass2-w4-20260929/](gate1-pass2-w4-20260929/) | [36558602577](https://github.com/blyzer/csl-runtime-eval/actions/runs/36558602577) | `247822c` | see per-cell manifest | per-cell (seed 20260929) | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass5 (paired W1/W2 baseline, current binaries, same corpus as S-pass3) | [results/s-scale-hosted-pass5-20260929/](s-scale-hosted-pass5-20260929/) | [36558359483](https://github.com/blyzer/csl-runtime-eval/actions/runs/36558359483) | `aa29500` | see manifest | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| M-pass1 (controlled, hosted; rust+zig+hybrid, streaming oracle, swap disabled) | [results/m-scale-hosted-pass1-20260929/](m-scale-hosted-pass1-20260929/) | [36538603932](https://github.com/blyzer/csl-runtime-eval/actions/runs/36538603932) | `c164f89` | `bfa642aa3bd259f9e8f1bb837c7b86c1e4ea630b05db34f317f39266b469a990` | `ea1f2320e06d18da0690f930668cda68f6222c6de4d83236729b19d79f0a54dc` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| M-local-exploratory (rust+zig+hybrid, local Mac, streaming oracle; NOT controlled) | [results/local/m-local-20260929/](local/m-local-20260929/) | local | `4e7e643` | see manifest | see manifest | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | macOS arm64 16 GB (SDK overlay, load ~10) |

All four S rows share the same corpus (`corpus/synthetic/S-campaign-mixed-20260928.json`,
100,000 entities / 1,000,000 relations / 1,000,000 evidence rows, seed
`20260928`, `mixed` shape) and the same oracle/query set, so the corpus digest
column is the control: any change there would invalidate a cross-pass
comparison. Each `manifest.json` additionally records preflight machine
conditions, per-tool versions, build flags and classification
(`controlled`/`exploratory`); each `records.jsonl` row carries its own
`result_digest` and `conformance: true/false` gate. STATUS.md's "Controlled
S-scale campaign" sections carry the query-by-query comparison; this file is
only the provenance index.

The M-local row uses corpus `M-campaign-mixed-20260929` (1,000,000 entities / 10,000,000 relations / 10,000,000 evidence rows, seed `20260929`), a different corpus and seed from the S rows, so it is a scaling observation, not a paired comparison with them.

Gate1 rows use per-scenario corpora (deterministic from `harness/scenarios.py`, seed 20260929) and are not paired with the S rows; the S-pass5 row is the only one paired with S-pass3 (same corpus digest).
