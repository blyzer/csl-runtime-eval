#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt" ziglang==0.14.1
ZIG_BIN="$("$ROOT/.venv/bin/python" -c 'import pathlib,ziglang; print(pathlib.Path(ziglang.__file__).parent/"zig")')"
ln -sf "$ZIG_BIN" "$ROOT/.venv/bin/zig"
echo "Installed locally. Run: source $ROOT/.venv/bin/activate"
echo 'For macOS arm64e-only SDKs, see TOOLCHAINS.md before applying the optional compatibility workaround.'
