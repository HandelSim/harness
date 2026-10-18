#!/usr/bin/env bash
#
# tests/unit_serve_test.sh — `harness serve` without docker or network:
#   - serve/test_pub.py   the pub page server (index, Host check, sandboxed
#                         /pub/ pages, no path escape, no-vault mode)
#   - cmd_serve           rejects bad options before it starts anything
#   - serve_password_file makes the opencode web password once, mode 600
#
# Prints "SERVE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " serve unit test"
echo "============================================================"

fail() { echo "[serve-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[serve-test] OK: $*"; }

PY=$(command -v python3 || command -v python) || fail "this test needs Python 3"

# --- S1: the pub server ------------------------------------------------------
( cd "$REPO_ROOT/serve" && "$PY" -m unittest -q test_pub ) || fail "S1: serve/test_pub.py failed"
ok "S1: pub server tests"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null
# Sourced from a test, clone_dir resolves off $0 and platform.sh is skipped;
# point both at the repo.
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"

# --- S2: bad options fail before anything starts -----------------------------
for args in "--port" "--port abc" "--port 0" "--port 65535" "--bogus"; do
    # shellcheck disable=SC2086
    if ( cmd_serve $args ) >/dev/null 2>&1; then fail "S2: 'serve $args' was accepted"; fi
done
( cmd_serve --help ) | grep -q 'harness serve \[--port N\]' || fail "S2: --help should print usage"
ok "S2: bad options rejected, --help works"

# --- S3: the password file is made once, private -----------------------------
f=$(serve_password_file) || fail "S3: serve_password_file failed"
[[ -s "$f" ]] || fail "S3: password file is empty"
first=$(cat "$f")
(( ${#first} >= 24 )) || fail "S3: password is too short"
perms=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
[[ "$perms" == "600" ]] || fail "S3: password file mode is $perms, want 600"
f2=$(serve_password_file)
[[ "$f2" == "$f" && "$(cat "$f2")" == "$first" ]] || fail "S3: a second call must reuse the password"
ok "S3: password made once, mode 600"

echo "SERVE TEST PASSED"
