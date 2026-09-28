#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi
if [[ -x "$ROOT/.venv/bin/zig" ]] && ! command -v zig >/dev/null 2>&1; then
  export PATH="$ROOT/.venv/bin:$PATH"
fi
