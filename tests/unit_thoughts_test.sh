#!/usr/bin/env bash
#
# tests/unit_thoughts_test.sh — `harness probe thoughts`, docker-free and offline.
#
#   T1: the runner's own unit tests (budget guard, plan, checkouts as their
#       own repos, grading, contamination counter, report)
#   T2: cmd_probe routes `thoughts` and every argument after it to the runner,
#       before the upstream-key check the other suites need
#   T3: --help works through the real entry point
#
# Run:  bash tests/unit_thoughts_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " harness probe thoughts unit test"
echo "============================================================"

fail() { echo "[thoughts] FAIL: $*" >&2; exit 1; }
ok()   { echo "[thoughts] OK: $*"; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# --- T1: python unit tests ----------------------------------------------------
( cd "$REPO_ROOT" && python3 -m unittest tests/benchmarks/thoughts/test_thoughts.py ) \
    || fail "T1: tests/benchmarks/thoughts/test_thoughts.py"
ok "T1: runner guard, checkouts, grading, contamination, report"

src() {
    # shellcheck disable=SC1090
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    clone_dir="$REPO_ROOT"
}

# --- T2: routing ---------------------------------------------------------------
t2=$(
    src
    unset PROXY_API_URL PROXY_API_KEY
    _probe_thoughts() { echo "ROUTED=[$*]"; }
    cmd_probe thoughts --gate-only --task-ids a,b
)
[[ "$t2" == 'ROUTED=[--gate-only --task-ids a,b]' ]] || fail "T2: routing — $t2"
ok "T2: cmd_probe thoughts hands its arguments to the runner, no key needed"

# --- T3: --help through the real entry point ------------------------------------
t3=$( src; _probe_thoughts --help ) || fail "T3: --help exited non-zero — $t3"
grep -q -- '--gate-only' <<<"$t3" || fail "T3: --help — $t3"
ok "T3: harness probe thoughts --help"

echo "[thoughts] all checks passed"
