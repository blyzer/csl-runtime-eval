#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"
failed=0
check() {
  echo "RUN $*"
  if "$@"; then echo 'PASS'; else echo 'FAIL'; failed=1; fi
}
for candidate in rust hybrid; do
  if ! command -v cargo >/dev/null 2>&1 || { [[ "$candidate" == hybrid ]] && ! command -v zig >/dev/null 2>&1; }; then
    echo "SKIP $candidate: required toolchain NOT AVAILABLE"
    continue
  fi
  check cargo fmt --manifest-path "prototypes/$candidate/Cargo.toml" --check
  check cargo clippy --manifest-path "prototypes/$candidate/Cargo.toml" --all-targets --all-features -- -D warnings
  check cargo test --manifest-path "prototypes/$candidate/Cargo.toml"
  check cargo build --manifest-path "prototypes/$candidate/Cargo.toml"
  check cargo build --release --manifest-path "prototypes/$candidate/Cargo.toml"
done
if command -v zig >/dev/null 2>&1; then
  check zig fmt --check prototypes/zig prototypes/hybrid/zig
  for dir in prototypes/zig prototypes/hybrid/zig; do
    if (cd "$dir" && zig build && zig build test); then echo "PASS $dir build/test"; else echo "FAIL $dir build/test"; failed=1; fi
  done
else
  echo 'SKIP Zig checks: zig NOT AVAILABLE'
fi
exit "$failed"
