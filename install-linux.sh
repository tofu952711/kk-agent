#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
if [[ "${1:-}" != "--check" ]]; then
    "$PYTHON" -m pip install -r "$ROOT/requirements.txt"
    "$PYTHON" "$ROOT/install.py"
fi
exec "$PYTHON" "$ROOT/scripts/check_linux.py"
