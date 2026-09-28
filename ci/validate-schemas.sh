#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
"$PYTHON" "$ROOT/harness/benchctl.py" validate --only schemas
