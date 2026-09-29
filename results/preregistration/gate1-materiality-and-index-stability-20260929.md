# Preregistration: Hybrid materiality thresholds and the index-stability experiment

Committed 2026-09-29, **before** any measurement it governs. Applies only to
measurements taken after this file's commit; earlier evidence (S, M, W2.X, W3, W4 passes)
is descriptive and is neither reinterpreted nor re-judged by these thresholds. Framework:
[ADR-0007](../../adr/0007-gate1-remaining-workloads.md) section 7. Thresholds below were
chosen without knowledge of any W5/W8 result. Changing one after seeing data invalidates
the measurement it would judge.

Integrity clause: no workload, scenario or optimization is introduced for the purpose of
rescuing Hybrid. W5/W8 can change the Hybrid conclusion only by naturally exposing a new,
material, reproducible Zig property under equivalent semantics that survives the
Rust<->Zig boundary.

## Part A. Index-stability experiment (exactly one; not an optimization experiment)

Question: is there a *stable material* Rust/Zig difference in the index phase? Current
status: `NO STABLE MATERIAL INDEX ADVANTAGE ESTABLISHED`. This single experiment
characterizes run-to-run variance; nothing is tuned and no search for a winner follows.

Design, all fixed in advance:

* **One runner instance**: one hosted `ubuntu-24.04-arm` job, one build of each binary.
* **Identical inputs**: corpus `S-campaign-mixed-20260928` (digest
  `7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5`, the S-pass3 and
  S-pass5 corpus), candidates `rust` and `zig` only, W1 queries `lookup-first`,
  `lookup-missing`, `scan-type` (the index phase does not depend on the query).
* **Paired and interleaved**: `harness/campaign.py` schedule, seed 20260928: seeded query
  order, and within every (query, repeat) the two candidates run back to back, the first
  position alternating each repeat. **40 repeats**, so 120 pairs. One validated unmeasured
  warm-up per candidate/query. Every accepted sample must equal the oracle byte-for-byte.
* **Measurements**: `phases_ns.index`, and its sub-phases `entities`, `adjacency`, `sort`;
  `phases_ns.load` and elapsed as context; host peak RSS.
* **Unit of analysis**: per pair, `R = zig_index / rust_index`.

Reported: median `R` with a 95% bootstrap confidence interval (paired resampling of the 120
pairs, 10,000 resamples, seed 20260929); the fraction of pairs with `R > 1`; per candidate,
the median, IQR and coefficient of variation of the index phase across repeats, and the
spread of per-repeat-block medians (run-to-run variance); the same statistics for the three
sub-phases.

Classification rule:

* **Rust materially faster within this instance** only if the CI lower bound of `R` is at
  least 1.10 **and** at least 90% of pairs have `R > 1`.
* **Zig materially faster within this instance** only if the CI upper bound of `R` is at
  most 1/1.10 (0.909) **and** at least 90% of pairs have `R < 1`.
* Otherwise: **`NO STABLE MATERIAL INDEX ADVANTAGE ESTABLISHED`**; the investigation is
  closed.

If the first or second outcome occurs, it is reported as "material within one instance;
cross-instance stability not established", is not treated as a stable property, and is not
followed by tuning or repeated searching.

## Part B. Materiality thresholds by dimension (Hybrid decision framework)

Comparator for every dimension and workload cell: the **best qualifying pure candidate**
in that cell. Statistic: median of the paired ratio `hybrid / best_pure` over at least 30
pairs (fewer is "insufficient evidence"), with a 95% paired bootstrap CI (10,000
resamples, fixed seed recorded with the result). Cells are the Gate-required cells of
ADR-0007 section 8 that Hybrid can execute; the **primary cell** of a workload is named in
the workload's own protocol (W5.S1: total time-to-first-query; W8.S1: 1% mixed; W10: the
boundary probes).

"Materially better" and "materially worse" mean the CI lies entirely beyond the threshold;
anything else is "equivalent".

| Dimension | Metric | Materially better than best pure | Materially worse than best pure |
|---|---|---|---|
| Execution latency | query, mutate, restore and W5/W8 phase latencies, per cell | CI upper bound of ratio <= 0.90 | CI lower bound >= 1.10 |
| Memory | host peak RSS; candidate live/peak heap where reported | ratio <= 0.85 | ratio >= 1.15 |
| Startup | process start -> open response; cold total time-to-first-query | ratio <= 0.90 | ratio >= 1.10 |
| Artifact / build | clean-build wall time; release artifact bytes (each separately) | ratio <= 0.80 | ratio >= 1.25 |
| BoundaryTax | boundary time as a fraction of end-to-end latency (W10) | (Hybrid-only cost) | fraction >= 5% in any Gate-required cell |
| Ownership / copy cost | bytes copied and allocations attributable to the boundary, as a fraction of the store's live heap | (Hybrid-only cost) | fraction >= 10% |
| Implementation / toolchain complexity | count of toolchains, exported boundary functions/types, boundary lines of code, build steps | (Hybrid-only cost) | any second toolchain or a non-empty boundary counts as materially worse |

The last three dimensions are costs pure candidates do not incur, so Hybrid is always
"materially worse" or at best "equivalent" there; a Hybrid advantage elsewhere must
outweigh them in the written justification.

### Hard requirements (pass/fail, no trade-offs; failing candidates do not qualify)

* R1: every accepted record byte-identical to the oracle, including S0 state-digest
  equality after mutation and restore where the workload uses them.
* R2: the evidence is classified `controlled` with 0 condition failures.
* R3: boundary safety: the C caller ASan/UBSan checks pass and ownership rules hold.
* R4: reproducible clean builds on the hosted architecture (W11).
* R5: the candidate implements the S0 baseline semantics for the workloads it enters
  (a `full-rebuild` mutation strategy is allowed but reported; it is not an incremental
  implementation for W8).

### Decision procedure (Pareto, no weighted score)

1. Discard candidates failing a hard requirement. The rest are *qualifying*.
2. For each pair (X, Y) of qualifying candidates, X **dominates** Y if X is not materially
   worse than Y in any dimension and materially better in at least one.
3. Hybrid **survives** only if all hold: (a) no qualifying pure candidate dominates it;
   (b) in at least one of latency, memory, startup or artifact/build it is materially
   better than **both** qualifying pure candidates; (c) that advantage is reproduced in at
   least **two independent controlled reproductions** (different runner instances, run
   separately); (d) a written argument shows the advantage is sufficient to justify the
   permanent cross-language boundary and a second production toolchain, reviewed against
   these thresholds.
4. Otherwise Hybrid does not survive. No dimension is traded against another by a formula.

## Part C. Scope note

These thresholds are proposals fixed before measuring, offered for the record of this
commit. They can be revised only by a new preregistration committed before the
measurements it would judge, never retroactively.
