#!/usr/bin/env bash
# Launch the host build MCP on Linux/macOS.
#
# Creates a local venv, installs requirements, and starts the server. Keep this
# terminal open while the agent works. cmake/ctest must be on PATH.
#
#   ./run.sh                       # uses the port in project.json
#   HOST_MCP_PORT=9123 ./run.sh    # override the port
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [[ ! -d .venv ]]; then
  echo "[host-mcp] creating venv..."
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "[host-mcp] starting server (Ctrl+C to stop)..."
exec python server.py
