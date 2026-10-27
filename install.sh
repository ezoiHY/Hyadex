#!/usr/bin/env bash
set -euo pipefail
umask 077
INSTALL_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for installer_python in "$INSTALL_SCRIPT_DIR/.venv/bin/python" "$(command -v python3 || true)"; do
  if [[ -n "$installer_python" && -x "$installer_python" ]] \
    && "$installer_python" -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
    exec "$installer_python" -B "$INSTALL_SCRIPT_DIR/hyadex/ops/installation.py" "$@"
  fi
done
if ! command -v uv >/dev/null 2>&1; then
  echo 'Install uv or Python 3.13 before running the Hyadex source installer.' >&2
  exit 1
fi
exec uv run --no-project --python 3.13 python -B "$INSTALL_SCRIPT_DIR/hyadex/ops/installation.py" "$@"
