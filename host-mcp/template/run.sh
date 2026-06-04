#!/usr/bin/env bash
# Launch the host build MCP. ONE script for every host: Linux, macOS, and
# Windows Git Bash. harness itself runs under Git Bash on Windows, so there is
# no PowerShell dependency — run this from the same Git Bash you run harness in.
#
# Creates a local venv, installs requirements, and starts the server. Keep this
# terminal open while the agent works. cmake/ctest must be on PATH. On Windows,
# CMake's "Visual Studio 17 2022" generator (set in project.json) locates MSVC
# itself, so you do NOT need a Developer prompt — a normal Git Bash with `cmake`
# on PATH is enough.
#
#   ./run.sh                       # uses the port in project.json
#   HOST_MCP_PORT=9123 ./run.sh    # override the port
set -euo pipefail
cd "$(dirname "$0")"

# Pick a Python launcher. Windows Python ships as `python` (or the `py`
# launcher), not `python3`, so probe in that order. Override with PYTHON=...
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON="python3"
  elif command -v python >/dev/null 2>&1; then PYTHON="python"
  elif command -v py >/dev/null 2>&1; then PYTHON="py -3"
  else
    echo "[host-mcp] no python found (need python3, python, or py on PATH)" >&2
    exit 1
  fi
fi

if [[ ! -d .venv ]]; then
  echo "[host-mcp] creating venv..."
  # shellcheck disable=SC2086  # PYTHON may be "py -3"; word-splitting is wanted.
  $PYTHON -m venv .venv
fi

# A venv built by Windows Python keeps its programs in .venv/Scripts; POSIX
# venvs use .venv/bin. Activate whichever exists so this one script covers Git
# Bash, Linux, and macOS without branching on the OS name.
if [[ -f .venv/Scripts/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/Scripts/activate
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "[host-mcp] starting server (Ctrl+C to stop)..."
exec python server.py
