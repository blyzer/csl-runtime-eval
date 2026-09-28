# Paired S-scale campaigns

Date: 2026-09-28. Gate #1 remains partial; this is a measurement protocol.

S has 100,000 entities, 1,000,000 relation rows and 1,000,000 evidence rows.
Start with the mixed shape and the nine existing W1/W2 queries. Ten repetitions
per query and pure candidate produce 180 measured records. Cycle/fanout and M
remain separate campaigns, never inferred from mixed S.

## Preconditions and classification

The runner records OS/architecture, toolchains, corpus/source/artifact hashes,
UTC times, load averages, available-memory estimates, swapout counters, and
macOS power/thermal output. It compiles and runs a small libc-linked Zig probe
against the original SDK, preferring system xcrun over the local overlay.

A controlled run rejects failed native probes, presence of the project SDK
overlay, one-minute load above 0.5 per logical CPU, less than 3 GiB estimated
available memory, or missing macOS AC-power confirmation. The load and memory
thresholds can be declared explicitly before starting. Every sample records
conditions immediately before/after its native process; controlled samples fail
on condition violations or an increase in the swapout counter. A failed campaign
keeps its partial evidence but never writes a passing summary.

These checks reduce known interference; they do not prove exclusive hardware,
fixed CPU frequency, CPU affinity, a constant thermal state, or cold disk caches.
macOS available memory is an estimate from free/inactive/speculative pages.
`--exploratory` permits a run despite preflight/condition failures and permanently
labels its manifest, summary and records exploratory. It never grants controlled
status based on semantic correctness. Native probe success is executable evidence
for the pinned toolchain; it is not vendor certification of the OS/compiler pair.

## Isolation and ordering

1. Generate the corpus deterministically and record its hash.
2. In a separate Python worker, validate the entire fixture once, build the
   oracle indexes once, and compute and schema-check all expected results.
   Persist these references, then exit the worker before candidate timing.
3. Shuffle query order with a recorded seed. Alternate Rust/Zig and Zig/Rust
   pairs across the ten repetitions, giving each candidate five first positions.
   Store the full planned sequence before executing it.
4. Run one verified, unmeasured warm-up per candidate/query before that
   candidate's first measured invocation. Warm-ups remain in separate evidence.
5. For each invocation, a fresh worker captures native stdout into a temporary
   file and measures only the native process with a monotonic clock and wait4.
   It reads the reference and candidate result **after** the native process exits,
   checks profile metadata and full parsed-result equality, writes the record,
   then exits so Python verification heaps cannot accumulate between samples.

Reference schema validation plus exact full-result equality proves the result
schema for each accepted sample without revalidating hundreds of thousands of
identical evidence objects ten times. Equality includes entities, propositions,
snapshot, completeness, query ID and digest; digest-only acceptance is prohibited.
`PreparedOracle` reuses only fixture indexes; truncation and query selection state
are new on every query. The existing `execute(fixture, query)` API still validates
every fixture passed to it.

Each sample retains process elapsed and peak RSS plus the four native phases
from ADR-0002. Those phase definitions and remaining representation differences
are unchanged. The Python orchestrator's memory is not candidate RSS. Neither
oracle preparation nor verification duration is native process elapsed.

## Reproduction

Build and pass normal/profile conformance first, then activate `.venv`:

```bash
python harness/campaign.py run --preset S --repeat 10 \
  --seed 20260928 --output results/s-scale-native
```

On an unsuitable workstation, explicitly label the fallback:

```bash
python harness/campaign.py run --preset S --repeat 10 --exploratory \
  --seed 20260928 --output results/s-scale-exploratory
```

Output directories must be new, avoiding accidental replacement of a campaign.
`manifest.json` records classification and state; `order.json` the schedule;
`records.jsonl` and `warmups.jsonl` the observations; `summary.json` per-query
medians. References and worker logs are retained for inspection. A BLOCKED native
preflight is evidence of an unmet prerequisite, not a candidate failure.

## GitHub-hosted execution

`.github/workflows/s-scale.yml` is manually dispatched and defaults to Ubuntu
24.04 ARM64, with x86-64 available as an explicit alternative. It installs pinned
Rust nightly-2026-08-08 and Zig 0.14.1, runs local validation, restores release
binaries after debug checks, records the runner image/run/CPU context, and runs
one S shape at ten repeats. Artifact upload runs even after failure or a blocked
preflight, with seven-day retention.

The hosted job declares a 0.75-per-CPU load ceiling because one sustained native
or verification worker is expected on a two-vCPU private runner. It retains the
native probe, 3 GiB available-memory minimum and per-sample swapout checks. There
are no automatic retries or conversion to exploratory after a failed guard.
Hosted VMs do not establish exclusive physical hardware or bare-metal
qualification. Hosted results must be described with their exact runner context.
