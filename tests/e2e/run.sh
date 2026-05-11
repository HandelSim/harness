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

total=${#scenarios[@]}
failed=0
declare -a failed_names=()

for path in "${scenarios[@]}"; do
    name="$(basename "$path")"
    echo "===== $name ====="
    if HARNESS_E2E_LOG_DIR="$LOG_DIR" python3 "$RUNNER" "$path" --log-dir "$LOG_DIR"; then
        :
    else
        rc=$?
        failed=$((failed + 1))
        failed_names+=("$name (rc=$rc)")
    fi
    echo
done

echo "==========================================="
echo "Total scenarios: $total"
echo "Failed: $failed"
if [[ $failed -gt 0 ]]; then
    echo "Failures:"
    for n in "${failed_names[@]}"; do
        echo "  - $n"
    done
    exit 1
fi
exit 0
