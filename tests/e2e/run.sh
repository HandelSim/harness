#!/usr/bin/env bash
# tests/e2e/run.sh
#
# e2e scenario orchestrator. Discovers scenario YAMLs under
# tests/e2e/scenarios/ and runs each via tests/e2e/lib/run_scenario.py.
#
# Environment variables (all optional):
#
#   HARNESS_E2E_SCENARIOS_DIR
#       Override the scenarios directory. Default: tests/e2e/scenarios
#       relative to this script. Used by tests and CI to point at a
#       sandbox dir.
#
#   HARNESS_E2E_PATTERN
#       Bash glob filter applied to scenario filenames (not paths). Only
#       files whose basename matches the pattern run. Example:
#           HARNESS_E2E_PATTERN='01-*' bash tests/e2e/run.sh
#
#   HARNESS_E2E_LOG_DIR
#       Directory for pipe-pane logs and transcripts. Default:
#       /tmp/harness-e2e-<epoch>. Created on demand.
#
# Exit code: 0 iff every scenario passed. 1 if any failed (or no tmux).
# Always prints a summary at the end.

set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIOS_DIR="${HARNESS_E2E_SCENARIOS_DIR:-$SCRIPT_DIR/scenarios}"
LOG_DIR="${HARNESS_E2E_LOG_DIR:-/tmp/harness-e2e-$(date +%s)}"
PATTERN="${HARNESS_E2E_PATTERN:-}"
RUNNER="$SCRIPT_DIR/lib/run_scenario.py"

# Preflight: tmux and python3 are mandatory. The driver and scenario runner
# both depend on them; failing fast here gives a clearer error than letting
# tmux invocations fail mid-scenario.
if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required for e2e tests but was not found in PATH" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required for e2e tests but was not found in PATH" >&2
    exit 1
fi

if [[ ! -d "$SCENARIOS_DIR" ]]; then
    echo "No e2e scenarios found in $SCENARIOS_DIR — nothing to run."
    exit 0
fi

# Gather candidate scenarios. nullglob makes the arrays empty rather than
# leaving the literal pattern when there are no matches.
declare -a scenarios=()
for path in "$SCENARIOS_DIR"/*.yaml "$SCENARIOS_DIR"/*.yml; do
    [[ -f "$path" ]] || continue
    if [[ -n "$PATTERN" ]]; then
        # Intentional glob match against PATTERN, not literal comparison.
        # shellcheck disable=SC2053
        if [[ "$(basename "$path")" != $PATTERN ]]; then
            continue
        fi
    fi
    scenarios+=("$path")
done

if [[ ${#scenarios[@]} -eq 0 ]]; then
    echo "No e2e scenarios found in $SCENARIOS_DIR — nothing to run."
    exit 0
fi

mkdir -p "$LOG_DIR"
echo "e2e scenarios: ${#scenarios[@]} found in $SCENARIOS_DIR"
echo "e2e logs:      $LOG_DIR"
echo

# --- optional mock upstream sidecar ----------------------------------------
#
# The e2e scenarios drive a full agent -> ollama -> proxy -> upstream round
# trip, so the proxy needs a real upstream to forward `/api/chat` to. When
# HARNESS_E2E_MOCK_UPSTREAM=1 (set by the CI e2e job), bring up the shared
# tests/mock_upstream.py as a sidecar on the harness network with the alias
# `mockupstream`, and tear it down on exit. PROXY_API_URL must already point
# at http://mockupstream:9000/... — the proxy reads it once at `harness
# start`, before this runs (mockupstream is a dotless intra-cluster name, so
# the proxy's firewall guardrail lets it through without an allowlist entry).
# Local runs leave the var unset and use whatever upstream the operator's
# .env configured.
MOCK_CONTAINER=""
cleanup_mock() {
    [[ -n "$MOCK_CONTAINER" ]] || return 0
    echo "e2e: removing mock upstream sidecar ($MOCK_CONTAINER)"
    harness_docker rm -f "$MOCK_CONTAINER" >/dev/null 2>&1 || true
}
if [[ "${HARNESS_E2E_MOCK_UPSTREAM:-0}" == "1" ]]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    export REPO_ROOT
    # shellcheck source=../lib/test_helpers.sh
    source "$REPO_ROOT/tests/lib/test_helpers.sh"
    e2e_project="${HARNESS_PROJECT_NAME:-harness}"
    MOCK_CONTAINER="${e2e_project}-mockupstream-1"
    trap cleanup_mock EXIT
    echo "e2e: starting mock upstream sidecar ($MOCK_CONTAINER)"
    test_start_mockupstream "$e2e_project"
    if ! test_wait_for_container_healthy "$MOCK_CONTAINER" 90; then
        echo "e2e: mock upstream sidecar did not become healthy within 90s" >&2
        exit 1
    fi
    echo "e2e: mock upstream sidecar healthy"
    echo
fi

total=${#scenarios[@]}
failed=0
xfailed=0
xpassed=0
declare -a failed_names=()
declare -a xfailed_names=()
declare -a xpassed_names=()

# Exit-code contract with run_scenario.py:
#   0  pass
#   1  fail
#   2  invocation / config error (treat as fail)
#   77 expected_failure: true, failed as expected (XFAIL — not a CI failure)
#   78 expected_failure: true, unexpectedly passed (XPASS — IS a CI failure;
#      the marker must be removed)
for path in "${scenarios[@]}"; do
    name="$(basename "$path")"
    echo "===== $name ====="
    rc=0
    HARNESS_E2E_LOG_DIR="$LOG_DIR" python3 "$RUNNER" "$path" --log-dir "$LOG_DIR" || rc=$?
    case "$rc" in
        0)  ;;
        77)
            xfailed=$((xfailed + 1))
            xfailed_names+=("$name")
            ;;
        78)
            xpassed=$((xpassed + 1))
            xpassed_names+=("$name")
            ;;
        *)
            failed=$((failed + 1))
            failed_names+=("$name (rc=$rc)")
            ;;
    esac
    echo
done

echo "==========================================="
echo "Total scenarios: $total"
echo "Failed:           $failed"
echo "XFailed (expected): $xfailed"
echo "XPassed (unexpected pass — fix needed): $xpassed"
if [[ $xfailed -gt 0 ]]; then
    echo "Expected failures (counted as success):"
    for n in "${xfailed_names[@]}"; do
        echo "  - $n"
    done
fi
if [[ $xpassed -gt 0 ]]; then
    echo "Unexpected passes (remove expected_failure marker on these):"
    for n in "${xpassed_names[@]}"; do
        echo "  - $n"
    done
fi
if [[ $failed -gt 0 ]]; then
    echo "Failures:"
    for n in "${failed_names[@]}"; do
        echo "  - $n"
    done
fi
if [[ $failed -gt 0 || $xpassed -gt 0 ]]; then
    exit 1
fi
exit 0
