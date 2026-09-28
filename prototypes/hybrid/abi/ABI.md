# Hybrid ABI v1 and experimental W10 APIs

Stable `csl_kernel.h` uses opaque lowercase C names, `int32_t` status,
`{ptr,len}`, runtime version 1, and C++ guards. No capacity, C enum status,
per-edge calls, or apply-delta export. Rust bindings match it exactly.

## Strategy A: kernel-owned result

Caller owns request bytes through the synchronous call. Zig retains no request
pointer. Each nonempty result owns a separate allocation and survives kernel
close. Release it **once**, by value, with `csl_buffer_release(buffer)`.
Copies of the descriptor are borrowed aliases, not independent owners.
`{NULL,0}` release and `close(NULL)` are no-ops. Invalid arbitrary/dangling
pointers and repeated releases are caller contract violations, not supported
operations. The Rust wrapper uses a unique drop guard (not Copy/Clone), copies
bytes into Rust memory, then returns the Zig allocation through the Zig release
function even during unwinding. No cross-allocator free occurs.

Zig allocates `sizeof(usize)+payload_length` bytes. A private little-endian
header immediately preceding the payload stores the exact allocation length.
Release recovers this header and frees the original slice with that exact
length. It never guesses allocator capacity or reads public `len` to derive
allocation size. Header arithmetic is overflow checked; empty output allocates
nothing. The header is not serialized, public, or part of any digest.
Tests use `std.testing.allocator` to detect leaked allocations and invalid
frees. Kernel and buffers are separate allocations; release after close is tested.
Non-null out descriptors are cleared on failure. Do not overwrite an unreleased
result descriptor. Serialize calls on a kernel; no concurrency guarantee yet.

## Bootstrap wire protocol

- Empty request: no-op, empty success.
- Byte `0x00` followed by bytes: echo (ABI test only).
- Byte `0x01` followed by UTF-8 JSON `{fixture,query}`: one semantic batch,
  returning normalized result JSON from the same Zig semantic implementation
  as the pure Zig process. Rust controls request serialization and owns the CLI.
- Other tags/malformed semantic JSON: INVALID_QUERY. Allocation failures: OOM.

Passing the fixture per call measures cold-data serialization, not a persistent
production kernel. This intentionally coarse bootstrap has no per-edge FFI.

## Strategy B: experimental caller-owned output

`csl_kernel_experimental.h` is separate from stable v1. `execute_into` reports
required length on BUFFER_TOO_SMALL and leaves output bytes untouched. Null
output plus zero capacity is a valid sizing call; other null/length mismatches
are invalid. A sufficient caller-owned buffer receives exactly the reported
bytes. Inputs/output must not overlap. Zig never retains or frees caller bytes.
Echo needs no internal allocation. Semantic JSON execution still allocates a
per-call arena; do not extrapolate echo allocation counts to semantic batches.

## Strategy C: future immutable/shared representation

`harness/shared_view.py` and `tests/test_shared_view.py` define a versioned,
snapshot-bound, bounds-checked immutable view with content integrity and a
lifetime test. This is a test seam, not a mmap implementation or ABI promise.
A future benchmark must measure map/unmap and page-fault costs, cold/warm pages,
validation, resident/proportional memory, lease lifetime, cancellation, stale
snapshot rejection, bytes copied and serialization avoided. Readers must keep
mapping ownership alive; offsets must never become unvalidated raw pointers.

## W10 interpretation

`benchctl w10 --repeat 10` measures 0, 1 KiB, 64 KiB, 1 MiB, 16 MiB echo payloads.
A times allocation, Zig copy, Rust copy, and Zig release; B uses a preallocated
Rust output and one call. A uses two FFI calls (execute/release), B one; kernel
open/close and hashing are outside timing. Pure controls time an allocation and
copy in independent Rust/Zig executables. Input preparation and JSON output are
outside timing. Pure controls drop/free after hashing; A includes release, so
the measured tax includes A's ownership lifecycle, not just call instructions.
Output allocation counts exclude kernel creation and allocator internals.
Rust uses its system allocator; Zig uses libc allocator for the copy control
and hybrid results. All output digests must match before accepting measurements.

`BoundaryTax = median(T_hybrid) - min(median(T_pureRust), median(T_pureZig))`

These are bootstrap microbenchmarks, not CSL query performance claims. Zero-byte
measurements contain timer/call overhead and noise. Cancellation is NOT
IMPLEMENTED. Error behavior is tested separately, not latency-benchmarked.
