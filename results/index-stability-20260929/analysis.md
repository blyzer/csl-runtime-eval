# Index-stability experiment (run 36588754955, commit ee8be24)

Protocol: results/preregistration/gate1-materiality-and-index-stability-20260929.md (Part A), committed before this run.
One hosted ubuntu-24.04-arm instance, S corpus `S-campaign-mixed-20260928` (digest 7ea1c7884968206f...), rust and zig, W1 x 40 repeats,
120 interleaved pairs, 240 records, all conformant, 0 condition failures, sdk_workaround [False].

| phase | median R = zig/rust | 95% CI (paired bootstrap, 10,000, seed 20260929) | pairs Rust faster | Rust median ms (CV) | Zig median ms (CV) |
|---|---|---|---|---|---|
| index | 1.164 | [1.140, 1.197] | 99% | 301.7 (0.172) | 349.2 (0.115) |
| entities | 1.655 | [1.637, 1.671] | 100% | 3.9 (0.048) | 6.5 (0.031) |
| adjacency | 1.113 | [1.094, 1.149] | 90% | 235.4 (0.212) | 259.3 (0.147) |
| sort | 1.316 | [1.312, 1.318] | 100% | 63.1 (0.029) | 82.9 (0.019) |

Per-repeat-block median index ms: Rust 262-377 (IQR 27); Zig 331-405 (IQR 19).

Preregistered outcome: **Rust materially faster within this instance (cross-instance stability not established)**.
