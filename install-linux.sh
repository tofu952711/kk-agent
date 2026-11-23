#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
if [[ "${1:-}" != "--check" ]]; then
    exec "$PYTHON" "$ROOT/install.py"
fi
exec "$PYTHON" "$ROOT/scripts/check_linux.py"
