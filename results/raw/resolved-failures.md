# Failures encountered and resolved

- Initial oracle script emitted no JSON; replaced CLI and compare complete golden results.
- Initial generator rejected CI's --entities/--edges/--output; added streaming generator and aliases.
- Missing jsonschema/Zig: installed in repository-local .venv (Zig 0.14.1).
- Zig build runner unresolved libc on macOS 27: optional local SDK metadata overlay;
  recorded as a measurement limitation, no system SDK modification.
- Rust wrapper method named `into` collided with Into trait resolution: renamed execute_into.
- Hybrid undefined roundq: bundle Zig compiler_rt.
- Apple linker rejected unaligned Zig archive member: extract/repack with Apple ar/libtool.
  Extraction preserved zero archive modes; normalize generated object permissions before repacking.
- Rust Clippy collapsible-if warning: simplify the relation validation predicate, rerun Clippy.
- Initial manifest paths included csl-runtime-eval/ prefix: regenerate with repository-relative paths.

These intermediate failures are superseded by candidate-checks.log and smoke.log.
Miri remains NOT AVAILABLE; the attempted command is in miri.log.
C caller ASan/UBSan passed; Zig allocation checking passed, but Zig ASan is not claimed.

## Phase baseline continuation — 2026-09-28

The first direct Zig build was launched without `.venv/bin` on PATH and hit the
already-documented macOS SDK/libSystem symbol failure. Activating `.venv` restored
the existing xcrun shim; the release rebuild and full smoke suite passed. No
system SDK changes or new workaround were needed.
