#!/usr/bin/env bash
set -euo pipefail
base=${1:?usage: ci/gate1-w11-r4.sh OUTPUT_ROOT}
mkdir -p "$base"
out="$base"
export ZIG="$PWD/.venv/bin/zig"
export CARGO_HOME="$RUNNER_TEMP/gate1-cargo-home"

for candidate in rust hybrid; do
  cargo tree --locked --manifest-path "$PWD/prototypes/$candidate/Cargo.toml" -e normal \
    > "$out/$candidate-dependencies.txt"
done
zig version > "$out/zig-version.txt"

build_one() {
  local candidate=$1 root=$2 artifact
  local -a cmd
  mkdir -p "$root"
  case "$candidate" in
    rust|hybrid)
      artifact="$root/target/release/csl-eval-$candidate"
      cmd=(cargo build --locked --release --manifest-path "$PWD/prototypes/$candidate/Cargo.toml" --target-dir "$root/target")
      if [ "$candidate" = hybrid ]; then
        export CSL_ZIG_CACHE_DIR="$root/zig-local-cache"
        export CSL_ZIG_GLOBAL_CACHE_DIR="$root/zig-global-cache"
      else
        unset CSL_ZIG_CACHE_DIR CSL_ZIG_GLOBAL_CACHE_DIR
      fi
      ;;
    zig)
      artifact="$root/install/bin/csl-eval-zig"
      unset CSL_ZIG_CACHE_DIR CSL_ZIG_GLOBAL_CACHE_DIR
      cmd=("$ZIG" build -Doptimize=ReleaseFast --prefix "$root/install" --cache-dir "$root/local-cache" --global-cache-dir "$root/global-cache")
      ;;
  esac
  (cd "$PWD/prototypes/$candidate" && /usr/bin/time -v -o "$root/time.txt" "${cmd[@]}") >"$root/build.stdout" 2>"$root/build.stderr"
  test -x "$artifact"
  sha256sum "$artifact" | awk '{print $1}' > "$root/artifact.sha256"
  stat -c '%s %n' "$artifact" > "$root/artifact.size"
  "$artifact" info > "$root/artifact-info.json"
}

for candidate in rust zig hybrid; do
  build_one "$candidate" "$base/$candidate/path-a"
  cp "$base/$candidate/path-a/artifact.sha256" "$base/$candidate/path-a/first-clean-build.sha256"
  cp "$base/$candidate/path-a/time.txt" "$base/$candidate/path-a/first-clean-build.time.txt"
  cp "$base/$candidate/path-a/artifact.size" "$base/$candidate/path-a/first-clean-build.size"
  build_one "$candidate" "$base/$candidate/path-b"
  cp "$base/$candidate/path-b/artifact.sha256" "$base/$candidate/path-b/first-clean-build.sha256"
  cp "$base/$candidate/path-b/time.txt" "$base/$candidate/path-b/first-clean-build.time.txt"
  cp "$base/$candidate/path-b/artifact.size" "$base/$candidate/path-b/first-clean-build.size"
  # Rebuild at the exact path after deleting all candidate outputs/caches.
  rm -rf "$base/$candidate/path-a/target" "$base/$candidate/path-a/install" \
    "$base/$candidate/path-a/local-cache" "$base/$candidate/path-a/global-cache" \
    "$base/$candidate/path-a/zig-local-cache" "$base/$candidate/path-a/zig-global-cache"
  build_one "$candidate" "$base/$candidate/path-a"
  diff -u "$base/$candidate/path-a/first-clean-build.sha256" "$base/$candidate/path-a/artifact.sha256" \
    > "$base/$candidate/same-path-hash.diff" || true
  diff -u "$base/$candidate/path-a/first-clean-build.sha256" "$base/$candidate/path-b/first-clean-build.sha256" \
    > "$base/$candidate/cross-path-hash.diff" || true
  case "$candidate" in
    zig) artifact="$base/$candidate/path-a/install/bin/csl-eval-zig" ;;
    *) artifact="$base/$candidate/path-a/target/release/csl-eval-$candidate" ;;
  esac
  "$artifact" session --repository "$base/$candidate/repository" < /dev/null > "$base/$candidate/path-a/session.log" 2>&1
  printf '%s\n' \
    '{"binding":"csl.eval.session.jsonl/v0","id":1,"max_line_bytes":65536,"op":"open","source":{"context":{"complete":false,"epoch":1,"snapshot":"S0"},"kind":"empty"}}' \
    '{"id":2,"op":"close"}' | "$artifact" session --repository "$base/$candidate/repository" > "$base/$candidate/path-a/session-execution.log" 2>&1
done
