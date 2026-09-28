# Controlled S-scale campaign ledger

Maps the informal `S-pass1` / `S-pass2-fairness` / `S-pass3-investigation`
labels used in STATUS.md/ADR discussion to their exact, reproducible evidence.
Never overwritten; append a row when a new controlled S campaign runs.

| Label | Path | Run | Commit | Source digest | Corpus digest | Repeats | rustc | zig | Runner |
|---|---|---|---|---|---|---|---|---|---|
| S-pass1 (pre-fairness) | [results/s-scale-hosted-pass1-20260928/](s-scale-hosted-pass1-20260928/) | [36396614497](https://github.com/blyzer/csl-runtime-eval/actions/runs/36396614497) | `73f68c22dbca76b7b2f405e3cd9fade28deecef7` | `f7c585d4d216aad92e073acd15a18d3c24bdc285ed765942be3190d81b77198e` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass2-fairness (Rust encode fix, query/materialize split) | [results/s-scale-hosted/](s-scale-hosted/) | [36405210578](https://github.com/blyzer/csl-runtime-eval/actions/runs/36405210578) | `3febcdd442d5d4ba07269442e757625006467216` | `80ea43ca27b2578586a5c0765bc01e1f933f446feb49ec768ad14c61da3081fc` | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` | 10 | rustc 1.99.0-nightly (1a98b1e13 2026-08-07) | 0.14.1 | ubuntu-24.04-arm |
| S-pass3-investigation (decode/index sub-phase instrumentation) | TBD | TBD | TBD | TBD | `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5` (same corpus, unchanged) | 10 | TBD | TBD | ubuntu-24.04-arm |

All three share the same corpus (`corpus/synthetic/S-campaign-mixed-20260928.json`,
100,000 entities / 1,000,000 relations / 1,000,000 evidence rows, seed
`20260928`, `mixed` shape) and the same oracle/query set, so the corpus digest
column is the control: any change there would invalidate a cross-pass
comparison. Each `manifest.json` additionally records preflight machine
conditions, per-tool versions, build flags and classification
(`controlled`/`exploratory`); each `records.jsonl` row carries its own
`result_digest` and `conformance: true/false` gate. STATUS.md's "Controlled
S-scale campaign" sections carry the query-by-query comparison; this file is
only the provenance index.
