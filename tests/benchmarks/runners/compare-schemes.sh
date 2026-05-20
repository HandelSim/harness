#!/usr/bin/env bash
#
# compare-schemes.sh — run the same benchmark under multiple schemes
# back-to-back, then write a small comparison summary.
#
# Default: user_front vs. passthrough on the smoketest target, claude agent.
# Pick a heavier target with --target terminal-bench|swe-bench-lite.
#
# Usage:
#   ./compare-schemes.sh
#   ./compare-schemes.sh --schemes user_front,passthrough --agent claude
#   ./compare-schemes.sh --target terminal-bench

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

bench_guard_ci
bench_check_arch
bench_check_binfmt || true
bench_check_docker_socket || true
bench_check_disk "${BENCH_ROOT}/runs" || true

SCHEMES="user_front,passthrough"
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
    runner_args=(--agent "${AGENT}" --scheme "${scheme}")
    # Only terminal-bench.sh / swe-bench-lite.sh accept --no-smoketest;
    # smoketest.sh has no such flag and would exit 2. Each scheme run is
    # already its own invocation here, so suppress the redundant pre-run
    # smoketest only for the targets that support the flag.
    [[ "${TARGET}" != "smoketest" ]] && runner_args+=(--no-smoketest)
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
echo "[compare-schemes] Next: diff the per-scheme harbor result JSON under" \
     "each ${COMPARE_DIR}/<scheme>.log run dir to compare pass rates." >&2
