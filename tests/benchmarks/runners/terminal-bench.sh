#!/usr/bin/env bash
#
# terminal-bench.sh — full Terminal-Bench 2.0 run.
#
# This is the headline benchmark. Wall-clock cost:
#   - x86_64 native, --n-concurrent 4 : ~2-4 hours, ~80 tasks.
#   - aarch64 + QEMU, --n-concurrent 1: ~12-24 hours, possible failures.
#
# Always run smoketest first unless --no-smoketest is set.
#
# Usage:
#   ./terminal-bench.sh --agent claude --scheme current
#   ./terminal-bench.sh --no-smoketest
#   ./terminal-bench.sh --task-ids hello-world,fix-bug-123
#   ./terminal-bench.sh --n-concurrent 4

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

bench_guard_ci
bench_check_arch
bench_check_binfmt || true
bench_check_docker_socket || true
bench_check_disk "${BENCH_ROOT}/runs" || true

# --- Arg parsing -------------------------------------------------------------
AGENT="claude"
TASK_IDS=""
N_CONCURRENT=""
SCHEME="current"
RUN_SMOKETEST=1
while (( $# > 0 )); do
    case "$1" in
        --agent) AGENT="$2"; shift 2 ;;
        --task-id|--task-ids) TASK_IDS="$2"; shift 2 ;;
        --n-concurrent) N_CONCURRENT="$2"; shift 2 ;;
        --scheme) SCHEME="$2"; shift 2 ;;
        --no-smoketest) RUN_SMOKETEST=0; shift ;;
        -h|--help)
            sed -n '1,15p' "$0"
            exit 0
            ;;
        *)
            echo "[terminal-bench] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

bench_require_harbor

if (( RUN_SMOKETEST == 1 )); then
    echo "[terminal-bench] running smoketest first" \
         "(suppress with --no-smoketest)" >&2
    "${SCRIPT_DIR}/smoketest.sh" --agent "${AGENT}" --scheme "${SCHEME}"
fi

bench_apply_scheme "${SCHEME}"

case "$AGENT" in
    claude)
        AGENT_IMPORT="harness_claude_agent:HarnessClaudeAgent"
        ADAPTER_DIR="${BENCH_ROOT}/adapters/harness_claude"
        ;;
    opencode)
        AGENT_IMPORT="harness_opencode_agent:HarnessOpencodeAgent"
        ADAPTER_DIR="${BENCH_ROOT}/adapters/harness_opencode"
        ;;
    *)
        echo "[terminal-bench] unknown agent: $AGENT" >&2
        exit 2
        ;;
esac

export PYTHONPATH="${ADAPTER_DIR}:${BENCH_ROOT}/adapters${PYTHONPATH:+:${PYTHONPATH}}"

CONC="$(bench_concurrency "${N_CONCURRENT}")"
RUN_NAME="terminal-bench-${AGENT}-${SCHEME}-$(date +%Y%m%dT%H%M%S)"
RUN_DIR="${BENCH_ROOT}/runs/${RUN_NAME}"
mkdir -p "${RUN_DIR}"

echo "[terminal-bench] agent=${AGENT} scheme=${SCHEME} concurrency=${CONC}" >&2
echo "[terminal-bench] output dir: ${RUN_DIR}" >&2

HARBOR_ARGS=(
    run
    --jobs-dir "${RUN_DIR}"
    --job-name "${RUN_NAME}"
    --n-concurrent "${CONC}"
    --agent-import-path "${AGENT_IMPORT}"
    --dataset terminal-bench/terminal-bench-core
)

if [[ -n "${TASK_IDS}" ]]; then
    HARBOR_ARGS+=(--task-ids "${TASK_IDS}")
fi

exec harbor "${HARBOR_ARGS[@]}"
