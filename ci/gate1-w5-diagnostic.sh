#!/usr/bin/env bash
set -euo pipefail
repeat=${1:?usage: ci/gate1-w5-diagnostic.sh REPEAT OUTPUT_DIR}
out=${2:?usage: ci/gate1-w5-diagnostic.sh REPEAT OUTPUT_DIR}
test "$repeat" -ge 30
mkdir -p "$out"
export ZIG="$PWD/.venv/bin/zig"
export CARGO_HOME="$RUNNER_TEMP/gate1-cargo-home"
export CSL_ZIG_CACHE_DIR="$RUNNER_TEMP/gate1-w5-zig-local"
export CSL_ZIG_GLOBAL_CACHE_DIR="$RUNNER_TEMP/gate1-w5-zig-global"
baseline="$RUNNER_TEMP/gate1-w5-baseline"
mkdir -p "$baseline/source"
git archive ce6a895d9ec1ec9011b0658f1ba6338f21651e13 | tar -x -C "$baseline/source"
mkdir -p "$baseline/build"
cargo build --locked --release \
  --manifest-path "$baseline/source/prototypes/rust/Cargo.toml" \
  --target-dir "$baseline/build/rust-target" 2>&1 | tee "$out/build-control-rust.log"
(cd "$baseline/source/prototypes/zig" && "$ZIG" build -Doptimize=ReleaseFast --prefix "$baseline/build/zig-install" \
  --cache-dir "$baseline/build/zig-local-cache" --global-cache-dir "$baseline/build/zig-global-cache") \
  2>&1 | tee "$out/build-control-zig.log"
control_rust="$baseline/build/rust-target/release/csl-eval-rust"
control_zig="$baseline/build/zig-install/bin/csl-eval-zig"
test -x "$control_rust" && test -x "$control_zig"
rm -rf prototypes/rust/target prototypes/zig/zig-out prototypes/zig/.zig-cache
rm -rf "$CSL_ZIG_CACHE_DIR" "$CSL_ZIG_GLOBAL_CACHE_DIR"
python harness/benchctl.py build rust | tee "$out/build-rust.json"
python harness/benchctl.py build zig | tee "$out/build-zig.json"

# The control artifacts come from the exact preregistered baseline; the diagnostic artifacts
# differ only by opt-in timing/output hooks. This paired build comparison bounds instrumentation.
CSL_BIN_RUST="$control_rust" CSL_BIN_ZIG="$control_zig" \
  CSL_W5_ARTIFACT_SOURCE="ce6a895d9ec1ec9011b0658f1ba6338f21651e13 (unmodified baseline)" \
  python harness/benchctl.py conformance --candidate rust --candidate zig --profile | tee "$out/conformance-control-profile.json"
CSL_BIN_RUST="$control_rust" CSL_BIN_ZIG="$control_zig" \
  CSL_W5_ARTIFACT_SOURCE="ce6a895d9ec1ec9011b0658f1ba6338f21651e13 (unmodified baseline)" \
  python harness/w5.py run --candidates rust zig --repeat "$repeat" \
  --prep-repeat "$repeat" --output "$out/overhead-control" | tee "$out/overhead-control.log"
CSL_W5_DIAGNOSTICS=1 CSL_W5_ARTIFACT_SOURCE="ce6a895d9ec1ec9011b0658f1ba6338f21651e13 plus opt-in W5 instrumentation" \
  python harness/benchctl.py conformance --candidate rust --candidate zig --profile | tee "$out/conformance-diagnostic-profile.json"
CSL_W5_DIAGNOSTICS=1 CSL_W5_ARTIFACT_SOURCE="ce6a895d9ec1ec9011b0658f1ba6338f21651e13 plus opt-in W5 instrumentation" \
  python harness/w5.py run --candidates rust zig --repeat "$repeat" \
  --prep-repeat "$repeat" --output "$out/phases" | tee "$out/phases.log"
