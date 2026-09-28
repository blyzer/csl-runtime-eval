#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$ROOT"
if ! command -v clang >/dev/null 2>&1; then
  echo 'SKIP C/C++ ABI diagnostics: clang NOT AVAILABLE'
  exit 0
fi
LIBRARY="$("$PYTHON" - <<'PY'
from pathlib import Path
paths=list(Path('prototypes/hybrid/target/release').rglob('lib/libcsl_kernel.a'))
if paths:print(max(paths,key=lambda p:p.stat().st_mtime).resolve())
PY
)"
if [[ -z "$LIBRARY" ]]; then
  echo 'SKIP ABI diagnostics: build hybrid release first'
  exit 0
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
clang -std=c11 -Wall -Wextra -Werror -fsyntax-only tests/abi_header.c
clang++ -x c++ -std=c++17 -Wall -Wextra -Werror -fsyntax-only tests/abi_header.c
clang -fsanitize=address,undefined -g tests/abi_header.c "$LIBRARY" -o "$TMP/abi-test"
"$TMP/abi-test"
echo 'PASS C/C++ header and C caller ASan/UBSan; Zig library is NOT sanitizer-instrumented'
