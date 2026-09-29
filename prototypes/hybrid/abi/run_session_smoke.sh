#!/usr/bin/env bash
# Build and run the C smoke test of the experimental session ABI (C11 + C++17 syntax, ASan/UBSan caller).
# The Zig library is NOT sanitizer-instrumented. Needs `python harness/benchctl.py build hybrid` first.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
command -v clang >/dev/null 2>&1 || { echo 'SKIP session ABI smoke: clang NOT AVAILABLE'; exit 0; }
LIBRARY="$(python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path
paths = list((Path(sys.argv[1]) / 'prototypes/hybrid/target/release').rglob('lib/libcsl_kernel.a'))
if paths:
    print(max(paths, key=lambda p: p.stat().st_mtime).resolve())
PY
)"
[[ -n "$LIBRARY" ]] || { echo 'SKIP session ABI smoke: build hybrid release first'; exit 0; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
clang -std=c11 -Wall -Wextra -Werror -fsyntax-only "$HERE/session_smoke.c"
clang++ -x c++ -std=c++17 -Wall -Wextra -Werror -fsyntax-only "$HERE/session_smoke.c"
clang -fsanitize=address,undefined -g "$HERE/session_smoke.c" "$LIBRARY" -o "$TMP/session-smoke"
"$TMP/session-smoke" "$TMP"
echo 'PASS session ABI header (C11/C++17) and C caller ASan/UBSan; Zig library is NOT sanitizer-instrumented'
