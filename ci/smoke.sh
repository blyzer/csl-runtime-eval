#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"
mkdir -p results/raw
"$ROOT/ci/validate-schemas.sh"
"$ROOT/ci/validate-oracle.sh"
"$PYTHON" harness/benchctl.py env
"$PYTHON" harness/benchctl.py prepare --preset SMOKE
"$PYTHON" -m unittest discover -v
failed=0
for candidate in rust zig hybrid; do
  if ! "$PYTHON" harness/benchctl.py build "$candidate"; then failed=1; fi
done
if ! "$PYTHON" harness/benchctl.py conformance --candidate rust --candidate zig --candidate hybrid; then failed=1; fi
if ! "$PYTHON" harness/benchctl.py conformance --candidate rust --candidate zig --profile; then failed=1; fi
for workload in W1 W2; do
  if ! "$PYTHON" harness/benchctl.py compare "$workload" --corpus SMOKE; then failed=1; fi
  if ! "$PYTHON" harness/benchctl.py compare "$workload" --corpus SMOKE --profile; then failed=1; fi
done
if command -v zig >/dev/null 2>&1; then
  (cd prototypes/zig && zig build test) || failed=1
  (cd prototypes/hybrid/zig && zig build test) || failed=1
else
  echo 'SKIP Zig tests: zig NOT AVAILABLE'
fi
if command -v cargo >/dev/null 2>&1 && command -v zig >/dev/null 2>&1; then
  cargo test --manifest-path prototypes/hybrid/Cargo.toml || failed=1
  cargo run --release --manifest-path prototypes/hybrid/Cargo.toml -- info || failed=1
  cargo run --release --manifest-path prototypes/hybrid/Cargo.toml -- echo 'CSL Rust + Zig' || failed=1
else
  echo 'SKIP hybrid ABI smoke: Rust/Zig toolchain NOT AVAILABLE'
fi
for shape in cycle fanout; do
  "$PYTHON" harness/generate.py --entities 1000 --edges 5000 --seed 20260927 --shape "$shape" --output "corpus/synthetic/SMOKE-$shape.json"
  if ! "$PYTHON" harness/benchctl.py compare W2 --corpus "corpus/synthetic/SMOKE-$shape.json"; then failed=1; fi
done
if ! "$PYTHON" harness/campaign.py run --preset SMOKE --repeat 2 --exploratory --output "$(mktemp -d "$ROOT/results/campaign-ci.XXXXXX")/run"; then failed=1; fi
exit "$failed"
