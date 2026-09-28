# Toolchains and local setup

Initial commands found Python 3.14.7, Rust 1.99.0-nightly
(1a98b1e13 2026-08-07), Cargo 1.99.0-nightly (c79e8f894 2026-08-04), Git 2.55.0.
`zig version` was unavailable; system Python also lacked jsonschema.

`./ci/setup-local.sh` installs jsonschema and **Zig 0.14.1** into `.venv` and creates
`.venv/bin/zig`. Activate with `source .venv/bin/activate`, then `zig version`.
No global package manager or system files are modified. Rust requires edition
2024 support and rustfmt/clippy. Cargo.lock files pin resolved crate versions.
CI configures stable Rust and Zig 0.14.1. Local environment/paths are recorded by
`benchctl env` in `results/raw/environment.json`; this is not an E5 toolchain freeze.

## macOS 27 / arm64 SDK compatibility

This machine's SDK has arm64e-only libSystem text stubs. Zig 0.14.1 failed to
resolve libc symbols when compiling its build runner. The optional workaround:

```bash
.venv/bin/python ci/macos-zig-sdk.py --sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
source .venv/bin/activate
zig version
```

The recorded run used the installed macOS 26.5 SDK named above; omit `--sdk`
to use xcrun’s selected SDK. The command creates `.venv/zig-sdk`, copying only libSystem link metadata with arm64 target
aliases and symlinking the remaining SDK. A narrow `.venv/bin/xcrun` shim redirects
only Zig's SDK-path query; all other xcrun commands go to `/usr/bin/xcrun`.
No executable libraries are copied/changed and no system SDK is edited. This is
a local compatibility workaround, not a supported SDK replacement. Local results
record `sdk_workaround=true`; controlled Gate measurements should use a natively
supported compiler/SDK combination. Linux CI does not need it.

Hybrid also bundles Zig compiler_rt (needed for typed JSON number parsing).
On macOS, build.rs extracts and repacks the static library using Apple ar/libtool
to satisfy newer linkers' eight-byte archive alignment. Extracted object modes
are normalized because Zig archives use zero modes. Object code is unchanged.
Cargo cannot silently build a Rust-only hybrid: missing Zig or link failure fails
the build. Zig kernels currently use ReleaseFast in debug and release Cargo builds.

C caller ASan/UBSan diagnostics are available through `ci/abi-diagnostics.sh`.
They do not instrument the Zig library. Zig ABI tests use the testing allocator
for leak/invalid-free diagnostics. `cargo miri test` was attempted but cargo-miri
is NOT AVAILABLE for the installed nightly; no Miri coverage is claimed.

## S-scale preflight — 2026-09-28

The original-SDK libc-linked probe still fails for pinned Zig 0.14.1 on this
macOS 27 host. Xcode's macOS 27 SDK and the installed CLT macOS 26.5 SDK both
advertise arm64e libSystem targets. The actual probe diagnostic is retained in
`results/s-scale-preflight/controlled/manifest.json` and
`results/s-scale-preflight/native-sdk-probe.log`.

The machine also exceeds the controlled campaign's declared load ceiling.
`harness/campaign.py` records the native preflight as BLOCKED. A passing local
exploratory correctness run does not close either prerequisite. No compiler
upgrade, global SDK replacement, VM startup, or unrelated-process termination
was performed for this campaign.
