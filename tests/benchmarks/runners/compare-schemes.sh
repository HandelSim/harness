#!/usr/bin/env bash
#
# compare-schemes.sh — run the same benchmark under multiple schemes
# back-to-back, then write a small comparison summary.
#
# Default: passthrough vs. current on Terminal-Bench, claude agent.
#
# Usage:
#   ./compare-schemes.sh
#   ./compare-schemes.sh --schemes current,passthrough --agent claude
#   ./compare-schemes.sh --target smoketest

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

bench_guard_ci
bench_check_arch
bench_check_binfmt || true
bench_check_docker_socket || true
bench_check_disk "${BENCH_ROOT}/runs" || true

SCHEMES="current,passthrough"
AGENT="claude"
TARGET="smoketest"   # smoketest | terminal-bench | swe-bench-lite
N_CONCURRENT=""
TASK_IDS=""
while (( $# > 0 )); do
    case "$1" in
        --schemes) SCHEMES="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        --task-id|--task-ids) TASK_IDS="$2"; shift 2 ;;
        --n-concurrent) N_CONCURRENT="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,15p' "$0"
            exit 0
            ;;
        *)
            echo "[compare-schemes] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

case "$TARGET" in
    smoketest|terminal-bench|swe-bench-lite) ;;
    *)
        echo "[compare-schemes] unknown target: $TARGET" >&2
        exit 2
        ;;
esac

bench_require_harbor

COMPARE_ID="compare-$(date +%Y%m%dT%H%M%S)"
COMPARE_DIR="${BENCH_ROOT}/runs/${COMPARE_ID}"
mkdir -p "${COMPARE_DIR}"

IFS=',' read -r -a SCHEME_LIST <<< "${SCHEMES}"
for scheme in "${SCHEME_LIST[@]}"; do
    echo "[compare-schemes] >>> running scheme=${scheme} agent=${AGENT}" \
         "target=${TARGET}" >&2
    runner_args=(--agent "${AGENT}" --scheme "${scheme}" --no-smoketest)
    [[ -n "${TASK_IDS}" ]] && runner_args+=(--task-ids "${TASK_IDS}")
    [[ -n "${N_CONCURRENT}" ]] && runner_args+=(--n-concurrent "${N_CONCURRENT}")
    # Capture this scheme's stdout/stderr in the compare dir for later diff.
    log_file="${COMPARE_DIR}/${scheme}.log"
    if ! "${SCRIPT_DIR}/${TARGET}.sh" "${runner_args[@]}" \
            > "${log_file}" 2>&1; then
        echo "[compare-schemes] scheme=${scheme} exited non-zero;" \
             "see ${log_file}" >&2
    fi
done

echo "[compare-schemes] done. Per-scheme logs in: ${COMPARE_DIR}" >&2
echo "[compare-schemes] Next: see tests/benchmarks/analyze/ for" \
     "summary tooling (TODO)." >&2
