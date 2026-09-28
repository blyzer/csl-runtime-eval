# ADR-0004: CSL/OMP²/Neper program roadmap and gate boundaries

Date: 2026-09-28. This ADR preserves architectural context and constraints for
a program larger than this repository's current code. It does not implement,
select, or authorize any technology it lists. Status legend used throughout
this repo from now on:

- **IMPLEMENTED** — code exists, is tested, runs in this repo or its CI.
- **IN PROGRESS** — actively being built this iteration.
- **PLANNED** — designed/sequenced, no code yet.
- **EXPERIMENTAL** — a reference/challenger track, explicitly not a production
  dependency regardless of how much code it accumulates.
- **RESEARCH** — architecture study only; no production integration implied.
- **DEFERRED** — intentionally not started; has an explicit reactivation
  trigger.

## Principle

Rust is the default implementation and harness language wherever no
experiment has demonstrated a reason to introduce another technology. Rust is
not thereby the automatically selected CSL semantic kernel, and Rust/Tokio is
not thereby the automatically selected distributed orchestration runtime.
Every non-Rust technology carries its own burden of proof: a controlled
experiment showing a material capability, correctness, operational,
performance, memory, failure-isolation, or complexity advantage large enough
to justify its permanent cost. Rejecting a technology after a fair experiment
is a successful experiment, not a failure, and must be recorded as such. Do
not optimize any candidate until it wins; do not select a technology for its
architectural elegance instead of its evidence.

The end state this program is converging toward is the smallest production
architecture that satisfies CSL + OMP² + Neper requirements with measured
evidence — not the union of every technology an experiment has touched.

## Two independent gates

**Gate #1 — CSL semantic kernel.** What implements the local CSL
semantic/data kernel. Candidates: Rust, Zig, Rust+Zig hybrid. Go, Gleam, BEAM
and OTP are explicitly out of scope for Gate #1; they solve a different
problem. **Status: OPEN.** This is the only gate with code in this repository
today (see "Current state" below).

**Gate #2 — Distributed/orchestration plane (P15).** What runtime
architecture best implements OMP²/Neper's distributed agent lifecycle,
supervision, messaging, recovery and orchestration semantics — not which
language is fastest. Candidates: Rust/Tokio, Go, Gleam/BEAM/OTP, or a hybrid
decomposition (e.g. Rust for local execution, BEAM for distributed
supervision). **Status: PLANNED, not started.** Gate #1's outcome must not
predetermine Gate #2's: `CSL=Rust+Zig` with `orchestration=BEAM/OTP` and
`CSL=Rust` with `orchestration=Go` are both valid eventual outcomes.

Gate #2 activates (P15.0 Need Gate) only once real requirements include one or
more of: remote workers, multi-node execution, A2A hosting/federation, csld
federation, distributed semantic workers, durable workflows, large persistent
agent populations, cross-node supervision, or fault-isolated long-running
agents. Until then P15 stays planned, but its runtime-neutral semantics
(AgentId/WorkerId/NodeId/TaskId, spawn/stop/restart, send/request/reply,
supervision, links, monitors, cancellation, deadline, mailbox/backpressure,
retry, lease/heartbeat, idempotency, delivery semantics, recovery, local vs.
remote execution) must never be expressed in this repo's code as
`tokio::task`, a goroutine, or an Erlang PID — those are runtime
implementations, not the Neper contract. **Nothing in this repository
currently implements or leaks orchestration semantics** (no Neper, no P15
code exists yet), so there is nothing to remediate today; this is a
constraint on P6+ work, not on Gate #1.

## Roadmap phases

```
P0  Formal requirements + interoperability policy         PLANNED
P1  Common corpus/oracle                                  IMPLEMENTED
P2  Rust semantic-kernel prototype                        IMPLEMENTED
P3  Zig semantic-kernel prototype                         IMPLEMENTED
P4  Rust<->Zig hybrid prototype                            IMPLEMENTED (ABI v1; not yet measured at S scale)
P5  A/B/C comparison                                      IN PROGRESS (SMOKE + S done; fairness pass this iteration)
    -> LANGUAGE GATE #1 (Rust / Zig / Hybrid)              OPEN
P6  Selected local kernel                                 PLANNED (blocked on Gate #1)
P7  CodeQueryIR                                            PLANNED
P8  Backend Contract                                       PLANNED
P9  Tree-sitter + SCIP/LSP                                 PLANNED (see Tree-sitter section)
P10 Semantic Planner                                       PLANNED
P11 Structured Agent API                                   PLANNED
P12 Capability/Extension Plane (AGENTS.md, Skills,          PLANNED
    Plugins, MCP, A2A, OpenAPI)
P13 Semantic Index                                         PLANNED
P14 Incremental semantics + Glean/Angle challenger          PLANNED
    -> QUERY MVP
P15 Distributed/Orchestration Plane                        PLANNED, gated (see Gate #2)
    -> ORCHESTRATION GATE
P16+ ChangeIR, VerificationIR, production hardening         PLANNED
```

This repository (`csl-runtime-eval`) is the P1-P5 empirical bootstrap only. It
is not production CSL and does not implement P6 or later.

## Capability/Interoperability Plane — layer identity

Preserve this mapping and do not conflate layers:

```
workspace/project instructions      -> AGENTS.md
reusable procedural skills          -> Agent Skills / SKILL.md
extension packaging                 -> Agent Plugins
tools/resources                     -> MCP
external agent interoperability     -> A2A
HTTP service contracts               -> OpenAPI
language intelligence               -> LSP
portable semantic indexes           -> SCIP
code-agent semantic operations      -> CSL
orchestration semantics             -> Neper
```

Explicitly: MCP != A2A; Agent Skill != A2A AgentSkill; Agent Plugin != semantic
authority; Neper != A2A; CSL != LSP; Tree-sitter != semantic truth. The
planner (P10, not yet built) must depend on a `CapabilityContract`, not a
backend name; external providers may advertise capabilities but must not
grant themselves semantic authority. A CSL backend is qualified through the
Backend Contract (P8) and conformance mechanisms, tracking capability,
quality ceiling, freshness, completeness and provenance independently.
Protocol identity (MCP/A2A/Glean/LSP) stays below the semantic contract.
None of P7-P13 exist yet, so none of this is at risk of violation today; it
constrains their future design.

## Tree-sitter — production-track, not merely a benchmark

Target pipeline once P9 starts: edit -> Tree-sitter incremental parse ->
changed ranges -> affected syntax nodes -> candidate affected symbols ->
semantic dependency lookup -> minimal invalidation -> semantic backend
refresh -> SemanticDelta -> CodeQueryIR. Syntactic change is not semantic
fact: Tree-sitter identifies *where* semantics may need recomputation; the
actual semantic evidence still comes from qualified backends (SCIP, LSP,
compiler-derived data, CSL backends). **Status: PLANNED**, no code yet.

## Glean + Angle — reference/challenger track

**Status: EXPERIMENTAL.** Glean is not a selected OMP² production dependency;
Angle does not replace CodeQueryIR. They exist to adversarially challenge
CodeIR/CodeQueryIR/Semantic Index/planner assumptions on equivalent semantic
workloads (definitions, references, callers/callees, implementations,
inheritance, dependency/transitive traversal, incremental update, relational
joins). Classify every compared query as Q1 (both express it cleanly), Q2
(both can, one at material extra complexity), or Q3 (Angle/Glean express
something CodeQueryIR currently cannot) — a Q3 case is direct architectural
evidence and must be documented with the missing abstraction, without
blindly copying Angle's syntax. No Glean/Angle code exists in this repo yet.

## MLIR — research track only

**Status: RESEARCH.** Do not add MLIR as a production dependency. The open
question is whether CSL benefits from an explicitly layered IR
(SyntaxIR -> SemanticIR -> QueryIR -> ExecutionIR, and later
ChangeIR -> semantic lowering -> language-specific transformation IR ->
concrete edit plan): do these layers have genuinely different invariants, are
explicit lowering passes useful, would dialect-like extensibility materially
simplify language-specific semantics, is reusable pass infrastructure needed,
and would actual MLIR infrastructure beat implementing the same layering
natively in Rust once its dependency/build/footprint/FFI/contributor cost is
counted? "Layered IR is useful" does not imply "use MLIR" — an MLIR-inspired
architecture without MLIR itself is a valid outcome. No code exists for this
track.

**MLIR and Mojo are independent decisions, not a package deal.** MLIR is
compiler/IR infrastructure (dialects, operations, interfaces, lowering,
rewrite patterns, pass infrastructure, verification) usable from any host
language, including Rust; it does not require adopting Mojo. Mojo is a
programming language *built on* MLIR infrastructure; adopting Mojo would pull
in MLIR as a transitive dependency, but studying or even adopting an
MLIR-inspired layered-IR architecture (SyntaxIR -> SemanticIR -> QueryIR ->
ExecutionIR) implies nothing about Mojo. Investigating MLIR concepts must not
be read as movement toward Mojo, and Mojo's status does not gate or follow
from MLIR's.

## Mojo — deferred

**Status: DEFERRED.** No dependency, no prototype. Reactivation trigger: a
concrete compute-heavy kernel (ranking, embedding processing, evidence
fusion, vector processing, ML preprocessing, numerical kernels) that Rust/Zig
cannot satisfy effectively. Mojo's burden of proof is a material
compute-kernel advantage large enough to justify another
language/runtime/toolchain.

## P15 detail (preserved for when its Need Gate activates)

Common workload the eventual P15 prototypes must all execute before any
performance comparison, so semantic behavior matches first: local agents,
remote agents, parent/subagent hierarchies, fan-out/fan-in, worker pools,
long-running agents, burst spawning, tool-heavy/model-heavy/mailbox-heavy
agents, supervision-heavy trees, A2A delegation, node-to-node routing.
Rust/Tokio is P15's baseline, not its predetermined winner; for every
candidate, classify each required feature (mailbox, supervision, monitor,
links, registry, restart strategy, node monitoring, distribution,
backpressure, cancellation) as NATIVE / STANDARD_RUNTIME / THIRD_PARTY /
CUSTOM, populated from the actual prototype rather than assumed, with LOC and
dependency cost recorded — "faster" is incomplete evidence if it requires
materially more custom fault-management infrastructure than an alternative
provides natively. The BEAM/OTP hypothesis under test is specifically better
supervision, failure containment, recovery semantics and large-agent
lifecycle management with less custom infrastructure than Rust/Tokio or Go —
not raw messages/sec — so a failure-injection suite (worker/agent/supervisor
crash, timeouts, hung tools, node loss, partitions, reconnect, duplicate/
delayed delivery, mailbox overflow, backpressure, stale lease/heartbeat,
cascading failure, restart storms) and a canonical recovery experiment
(kill a node under N supervised agents; measure detection time, blast radius,
restart time, work lost/duplicated) are first-class P15 deliverables, not
optional. A2A participates in P15 as external-agent interoperability testing,
not as Neper's orchestration semantics. Measurement dimensions (throughput,
p50/p95/p99 latency, memory per idle/active agent, spawn rate, recovery
latency, supervision overhead, blast radius, operational/implementation/
observability complexity, glue code) must not be collapsed into one weighted
score; use hard requirements plus Pareto comparison. If candidates tie on
hard requirements, prefer the simplest architecture. None of this is
implemented; it is preserved so P6+ work does not silently lock the codebase
into Tokio-specific assumptions before Gate #2 can run.

## Anti-goals

Do not build a six-or-more-language production system merely because
experiments exist for each. Do not optimize Rust until it wins, optimize Zig
until it wins, design the hybrid so it wins, or pick BEAM/Go for perceived
architectural elegance instead of evidence. Keep experimental/reference-only
dependencies (Zig, Glean, Angle, Go, Gleam, Erlang/OTP) and research-only
dependencies (MLIR) out of the production dependency graph; a technology
moves from experimental to production only through its own gate.

## Current state (this repository, 2026-09-28)

Implemented: P1-P4 (Gate #1 candidates), SMOKE and controlled S-scale W1/W2
evidence for pure Rust/Zig (see [ADR-0002](0002-matched-phase-baseline.md),
[ADR-0003](0003-s-scale-campaign.md), [STATUS.md](../STATUS.md)). Everything
else in this ADR is PLANNED, EXPERIMENTAL, RESEARCH or DEFERRED as labeled
above, with zero lines of code in this repository. Language Gate #1 remains
OPEN.
