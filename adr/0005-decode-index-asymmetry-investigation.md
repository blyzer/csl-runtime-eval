# ADR-0005: Decode and index asymmetry investigation (pass 3)

Date: 2026-09-28. Gate #1 remains OPEN. This does not select Rust, Zig, or
Hybrid. It investigates *why* the two asymmetries that survived
[ADR-0002 pass 2](0002-matched-phase-baseline.md#representation-pass-2--phase-decomposition-and-rust-encode-fairness-2026-09-28)
exist, per [results/EVIDENCE-LEDGER.md](../results/EVIDENCE-LEDGER.md)'s
S-pass1/S-pass2-fairness baselines:

1. Rust JSON decode ~2x faster than Zig.
2. Zig index construction ~35-45% faster than Rust.

Both languages already decode bytes directly into typed structs with no
generic `Value`/DOM intermediate (pass 1/2 already matched this). The
question here is *why*, given that, a ~2x and a ~35-45% gap remain.

## STEP 2 — Decode: Rust breakdown

`std::fs::read(path)` queries the file's metadata length and pre-sizes a
`Vec<u8>` to it before reading, so the read step is one allocation plus one
or a few `read(2)` calls. `serde_json::from_slice::<Fixture>(&bytes)` then
deserializes directly into the typed struct (`prototypes/rust/src/model.rs`):
recursive-descent, one comptime-specialized visitor per field type, no
dynamic `Value` tree. For the four array fields (`strings`, `entities`,
`relations`, `evidence`) serde's generic `Vec<T>: Deserialize` implementation
has no array-length hint from JSON syntax, so it starts from `Vec::new()` and
grows by amortized doubling as it visits each element — at S scale
(1,000,000 relations/evidence rows) this is roughly 20 reallocations per
array, each copying already-inserted `Copy` elements (cheap per element, but
real memcpy work). Every JSON string becomes an owned heap-allocated `String`
— serde_json copies string bytes (unescaping if needed) into fresh memory
regardless of whether escaping was actually required, because the target
type is `String`, not a borrowed `&str`. The global allocator is the
platform default (glibc `ptmalloc` on the CI runners used for all S-scale
evidence); no custom allocator is configured in `Cargo.toml`.

## STEP 2 — Decode: Zig breakdown

`std.fs.cwd().readFileAlloc(a, path, max)` reads the whole file into one
arena allocation. `std.json.parseFromSlice(Fixture, a, fixture, .{})` parses
directly into the typed `Fixture` (`prototypes/zig/src/semantic.zig`) with
no `std.json.Value` intermediate — read from the vendored Zig 0.14.1 source
(`.venv/lib/python3.14/site-packages/ziglang/lib/std/json/static.zig:141-147`):
when parsing from a slice (a `Scanner`, not a streaming `Reader`), the
default `allocate` option resolves to `.alloc_if_needed`, meaning **string
fields that need no escape processing are returned as slices directly into
the original input buffer — zero-copy** — a cheaper strategy than Rust's
always-copy `String` for exactly the identifier-like strings this synthetic
corpus contains. Array fields (`static.zig:480-493`) are still accumulated
via `std.ArrayList` and converted with `.toOwnedSlice()` at the end, so
array growth also has no length hint from JSON syntax, same as Rust's `Vec`.
The allocator is `std.heap.ArenaAllocator` wrapping `std.heap.page_allocator`
(`prototypes/zig/src/main.zig`): a bump allocator over chunks it requests
from `page_allocator` (a thin wrapper over raw `mmap`/`VirtualAlloc`), growing
each new chunk to `1.5x` the previous total when the current one is
exhausted (`arena_allocator.zig:164-173`). `ArrayList` growth calls the
allocator's `resize` first, and Zig's arena *can* extend the most-recent
allocation in place for free if it still fits in the current chunk
(`arena_allocator.zig:210-230`) — so growth is cheap as long as the growing
array stays the arena's most recent allocation and the chunk has room; only
crossing a chunk boundary forces a full copy into freshly `mmap`'d space,
and per-allocation memory once "reallocated away from" is never reclaimed
inside an arena (arenas only free everything at once, on `deinit`).

## STEP 2 — Comparison and hypothesis

The evidence rules out one candidate cause outright: Zig's default
`alloc_if_needed` string handling should be *cheaper* than Rust's
always-copy `String`s for this corpus's escape-free identifiers, so string
allocation is not the source of Zig's slower decode — if anything it means
the true parser/allocator cost gap is larger than the raw measured
difference, not smaller. The two remaining, code-grounded candidates, ranked
by plausibility from static reading alone:

1. **Allocator strategy** (leading candidate): Rust's `Vec`/`String` growth
   goes through a mature general-purpose allocator (glibc `ptmalloc`) with
   per-size-class free lists that can often extend or reuse memory cheaply.
   Zig's arena is a simple bump allocator whose *chunk* growth is a raw
   `mmap` call every ~1.5x, and whose *array* growth, while sometimes free
   (in-place extension), pays a real cost whenever a chunk boundary is
   crossed with no way to reclaim the abandoned space until the whole arena
   is freed. At S scale, parsing 100,000 entities plus 2,000,000 relation/
   evidence rows plausibly crosses several chunk boundaries.
2. **Parser/scanner implementation maturity**: serde_json is a long-maintained,
   heavily used, and heavily optimized crate (fast byte scanning, mature
   number parsing); Zig's `std.json` is comparatively newer and may simply
   not be as tuned per byte scanned, independent of allocation strategy.

This is a **cause analysis, not a profiler-confirmed root cause**: no
profiler (perf/Instruments) was run this iteration — Zig cannot build
locally on this Mac (pre-existing SDK issue, TOOLCHAINS.md) and CI has no
profiling step. Distinguishing (1) from (2) definitively needs either a
profiler run or a targeted experiment (e.g., pre-sizing the arena to the
file length before parsing, to isolate chunk-growth cost). Neither was done
this iteration; **no change was made to Zig's decode path**, matching the
instruction not to alter Zig's parser before the cause is measured.

## STEP 3 — Index: Rust breakdown

`Store::build_entities` builds a `HashMap<u64, Entity>` (one `insert` per
entity, one pass to validate `container` references), `Store::build_adjacency`
builds two `HashMap<u64, Vec<Edge>>` (`out`/`inc`) via `entry(...).or_default().push(...)`
per relation, plus a full pass over `evidence` for reference validation, and
`Store::sort_adjacency` sorts each adjacency `Vec<Edge>` by
`(subject, relation, object)`. Before this iteration, `HashMap`/`HashSet`
used Rust's std default hasher: `RandomState`, which is SipHash-1-3 —
explicitly documented by the Rust standard library as trading raw speed for
HashDoS resistance (a randomized, cryptographically-mixed hash, deliberately
not optimized for throughput on tiny keys like a `u64`).

## STEP 3 — Index: Zig breakdown

`Store.buildEntities`/`buildAdjacency`/`sortAdjacency` mirror the same three
steps structurally. `std.AutoHashMap`'s default hash function is **Wyhash**
(confirmed from the vendored source, `hash_map.zig:8,26-29`) — a fast,
non-cryptographic hash designed purely for throughput, with no DoS-resistance
goal. Both languages use open-addressing-style hash maps with broadly
comparable value-type storage (`Entity`/`Edge` are small `Copy`/value structs
in both languages, stored by value, no heap indirection per entry beyond the
map's own backing array); this is not primarily a data-structure difference.

## STEP 3 — Comparison, experiment, and result

The SipHash-vs-Wyhash difference is a well-established, textbook-level
Rust-vs-Zig **standard-library default choice** — not a language or runtime
limitation: Rust programs routinely swap in a fast non-cryptographic hasher
(commonly `FxHashMap`/`ahash`) for exactly this reason, with no change to the
language. This is the single most plausible, and independently verifiable
(from source, without profiling), explanation for the measured index gap.

**Experiment** (permitted once an asymmetry is demonstrated, per this
investigation's ground rules): `prototypes/rust/src/main.rs` now defines an
inline FxHash-style hasher (rotate-xor-multiply — the same algorithm `rustc`
itself uses internally; no new crate dependency) and uses it for the
`Set`/`Index`/entity-map types only. No data structure, algorithm, sort
order, or output changed; Zig was not touched. Verified before committing:
digest byte-identical to the oracle on the golden fixture, 106/106
conformance (normal + profile) unchanged, `clippy`/`fmt` clean.

**SMOKE-scale single-sample result** (directional only, superseded by the
controlled measurement below): local Mac, `scan-type` `index_ns` 779,333 ->
~451,000-546,000 ns; hosted CI single sample showed Rust *overtaking* Zig
entirely (691,420 vs 1,203,415 ns) — that reversal did not hold up at
controlled scale (below) and should be read as SMOKE-scale noise amplified
by a single sample, not as the real effect size.

**Controlled S-scale result** (S-pass3 vs S-pass2, ten repeats, same corpus,
[results/EVIDENCE-LEDGER.md](../results/EVIDENCE-LEDGER.md)): the hasher
change closed **most but not all** of the index gap — it did not reverse it
at this rigor.

| Query | Rust index (pass2 -> pass3) | Zig index (pass2 -> pass3, unchanged code) | Gap (pass2 -> pass3) |
|---|---|---|---|
| W1 scan-type | 538.8 ms -> 394.2 ms (-27%) | 349.9 ms -> 377.2 ms (+8%, noise) | Zig 35% faster -> Zig only 4% faster |
| W2 depth-8 | 625.8 ms -> 403.3 ms (-36%) | 382.8 ms -> 378.0 ms (flat) | Zig 39% faster -> Zig only 7% faster |

Sub-phase detail at pass 3 (median, ns): Rust `entities` 4.1-4.4M vs Zig
5.5-5.6M (**Rust now faster** on this sub-phase); Rust `adjacency` 324.5-333.5M
vs Zig 285.8M (Zig still ~13-17% faster here); Rust `sort` 65.2-65.9M vs Zig
86.0-86.4M (**Rust now faster**, plausibly cheaper comparator/allocation
during `sort_unstable_by_key` vs Zig's `std.mem.sort`, not investigated
further this pass). Net **elapsed** effect across all nine W1/W2 queries at
S scale: Rust improved 9-18% on every query (median across queries ~-14%);
Zig stayed flat (±0-4%, consistent with no code change). Conformance:
212/159 PASS unchanged; semantic digests remained byte-identical to the
oracle (hard gate, verified before this rerun was trusted).

**Conclusion for this step**: SipHash-vs-Wyhash explains the *majority* of
the index gap, confirmed at controlled scale, but not all of it — after the
hasher fix Rust is faster on `entities` and `sort`, Zig remains faster on
`adjacency`, and the two are within single-digit percent overall. This is a
materially different, more modest conclusion than the SMOKE-scale single
samples suggested, illustrating exactly why STEP 10's methodological lesson
matters: single-sample measurements at small scale overstate effect sizes
and can even flip direction. No further hasher tuning was attempted this
pass (STEP 6: do not optimize merely because more optimization is
available); the residual `adjacency`-phase gap is a candidate target for a
future pass if it persists.

## STEP 5 — Representation equivalence report

| Structure | Rust | Zig |
|---|---|---|
| Entity | `#[derive(Clone,Copy,Deserialize)] struct { id:u64, kind:Kind(enum), name_sid:u32, container:Option<u64> }`; value type, stored by value in the entity map; no heap indirection per entity | `struct { id:u64, kind:Kind(enum), name_sid:u32, container:?u64 }`; identical field set; value type, stored by value in `AutoHashMap`'s backing array |
| RelationEdge | `#[derive(Clone,Copy,Deserialize)] struct Edge { subject:u64, relation:Relation(enum), object:u64 }`; value type | `struct Edge { subject:u64, relation:Relation(enum), object:u64 }`; identical; value type |
| Evidence | `#[derive(Clone,Copy,Deserialize,Serialize)] struct` with 8 fields incl. 3 enums; value type; the `Serialize` derive emits fields in the struct's declared (alphabetical) order, load-bearing for the digest (ADR-0002 pass 2) | Anonymous-struct literal at encode time with the same 8 fields in the same alphabetical order (source comment: "Alphabetical field order is canonical JSON order. Never add measurement fields here."); value type |
| String/interned string | `String` (owned, heap-allocated, one allocation per string, always copied on decode); no interning — `strings: Vec<String>`, entities/evidence reference by `name_sid` index only | `[]const u8` (default `.alloc_if_needed`: borrowed slice into the input buffer when no escaping is needed, else a fresh arena allocation); no interning — same index-based referencing |
| Adjacency list | `HashMap<u64, Vec<Edge>, FxBuild>` (as of this pass; was the std `RandomState` hasher before) for `out`/`inc`; each bucket a growable `Vec<Edge>`, sorted once after construction | `std.AutoHashMap(u64, std.ArrayList(Edge))` (Wyhash) for `out`/`inc`; each bucket a growable `ArrayList(Edge)`, sorted once after construction — structurally identical shape |
| Lookup index | Same `HashMap<u64, Entity, FxBuild>` doubles as the entity lookup index | Same `AutoHashMap(u64, Entity)` doubles as the entity lookup index |
| Visited set | `HashSet<u64, FxBuild>` (`Set`), used for both `seen` and result accumulation during traversal | `std.AutoHashMap(u64, void)` (`Set`), same role |
| Result collection | `Selection { ids: Vec<u64>, props: Vec<Evidence>, truncated: bool }`; `ids`/`props` sorted then serialized directly from typed structs (post pass-2 fairness fix) | `Selection = struct { ids: []u64, props: []Evidence, truncated: bool }`; identical shape; serialized directly from typed structs (always was) |

Element sizes were not measured this iteration (would need `std::mem::size_of`
/`@sizeOf` instrumentation, not yet added); both languages use comparable
small value-type structs with no heap indirection per record, so the
representations are structurally equivalent at this level — the surviving
differences are in **allocator strategy** and **hash function choice**, not
data layout.

## STEP 4 — E2E vs kernel behavior

A dedicated `--repeat-in-process N` KERNEL mode (amortizing decode/index
once, repeating query/materialize/encode in-process) remains **designed, not
built** (unchanged from ADR-0002 pass 2). However, the existing
`phase_detail_ns`/`phase_subdetail_ns` decomposition already answers the two
questions this mode was meant to separate, from the same per-invocation
measurement:

- **"Which candidate finishes a real request fastest?"** = full `phases_ns`
  sum (`decode+index+query+materialize+encode`), i.e. E2E, already reported.
- **"Which candidate has the better semantic kernel behavior?"** =
  `index+query+materialize+encode` (excluding `decode`, which is pure I/O
  and JSON-library overhead, not kernel logic) — computable today from the
  same records without a new mode.

MATERIALIZE-only mode remains **BLOCKED** for the same reason as pass 2: no
stable precomputed-selection input format exists, and building one now would
either bypass ADR-0003's per-process isolation guarantees or require a
larger redesign out of scope for this pass.

## STEP 8 — P4 Hybrid hypothesis, reframed

Terminology correction: Zig itself requires no FFI. FFI/ABI applies only to
candidate **C, Rust+Zig Hybrid**, where Rust and Zig execute in the same
process across a language boundary. Candidates remain A (pure Rust, no
Rust<->Zig FFI), B (pure Zig, no Rust<->Zig FFI), C (Rust harness/integration
control calling a coarse/batched Zig semantic/index kernel — never
per-entity/per-edge calls, per the existing `prototypes/hybrid` ABI v1).

The pass-1 hypothesis for Hybrid — "Zig is intrinsically superior for
large-result serialization, worth crossing a language boundary to capture" —
is **falsified** by pass 2: that gap was a Rust implementation defect, fully
closed without any hybrid boundary. The pass-3 controlled S-scale result
above answers the narrowed question directly: Zig's index advantage
survives the hasher fix, but only as a single-digit-percent gap
(`adjacency` sub-phase ~13-17% faster; ~4-7% faster overall at the `index`
phase, and Rust is now *ahead* on `entities` and `sort`). A 4-7% aggregate
advantage is very unlikely to survive a Rust<->Zig FFI boundary crossing
(BoundaryTax: batching/copy overhead, ownership transitions between
allocators, serialization at the boundary) with any net benefit — P4 Hybrid
would need to demonstrate that BoundaryTax stays below roughly this same
single-digit-percent margin, which is a materially harder bar than the
35-45% margin pass 1/2 appeared to offer. **This is a valid, useful negative
signal, not a final answer**: P4 is not abandoned, but its current
best-supported hypothesis is narrow enough that Hybrid may not be worth
building unless a *new*, larger, demonstrated advantage is found (e.g. at M
scale, or in a workload family W1/W2 does not cover) — BoundaryTax,
copy/allocation, ownership-transition, and toolchain-complexity costs are
real and must clear whatever margin remains.

## S-pass3 frozen as baseline

S-pass3 is now frozen: no further Rust/Zig tuning at S scale to chase these
specific residual gaps. Established, but still open to future explanation,
not further pursued this iteration:

- Rust retains a large, unexplained decode advantage (~2x).
- Zig retains a residual index advantage: ~4-7% overall, ~13-17% specifically
  on the `adjacency` sub-phase.
- The SipHash-vs-Wyhash experiment explains most, not all, of the index gap.
- The pass-1 large-result serialization/RSS "Zig advantage" is falsified as
  a language difference (pass 2: a Rust implementation defect).
- Semantic digest equality against the oracle remains a hard gate and held
  through every change in this pass.

Work moves on to the W10 Hybrid boundary experiment (below) and, subject to
its result, M scale. This section is the reference point for "the S-scale
gaps we chose not to keep chasing, and why."

## STEP 10 — Methodological lesson

**A material performance difference must not be attributed to a
language/runtime until reasonable representation, algorithmic, serialization,
allocation and instrumentation asymmetries have been investigated.** Pass 1
measured a real, reproducible gap and initially read as "Zig is better at
large-result work"; pass 2 showed it was substantially one candidate's
implementation choice (a `Value` tree it didn't need). This pass repeats the
same discipline on the two gaps that survived pass 2, and finds the same
pattern again: the leading candidate cause for the index gap (SipHash vs
Wyhash) is a **standard-library default**, not a property of either
language. Benchmark methodology for this project going forward: before
treating any candidate's win/loss as evidence for Gate #1, identify what
library/allocator/representation defaults differ between the two
implementations and check whether the difference is attributable to one of
those defaults before concluding it reflects the language or runtime itself.

## Addendum (2026-09-29): limits of the hasher result

The FxHash-style hasher isolated here was validated on `u64` ID keys only. Gate #1 W4 later
showed it is pathological on short structured string keys: 8-character hex names made Rust's
name-index build ~12x slower and lookups ~14x slower than Zig's (reproduced twice; std SipHash
removes the effect), so the "hasher explains most of the index gap" conclusion must not be
generalized across key types. A same-corpus rerun of the S baseline also showed the residual
index difference between Rust and Zig is not stable across hosted instances. See STATUS.md
"Gate #1 Track A".

Classification of the index-phase question (2026-09-29): `NO STABLE MATERIAL INDEX ADVANTAGE
ESTABLISHED`. The 4-7% Zig index advantage recorded above is kept as history and is not
surviving evidence.
