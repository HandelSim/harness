#!/usr/bin/env bash
#
# swe-bench-lite.sh — optional SWE-bench Lite run.
#
# Secondary benchmark. SWE-bench Lite is the cheap variant (~300 tasks);
# the full SWE-bench is 2294 tasks and not worth running through harness
# at this stage.
#
# Usage:
#   ./swe-bench-lite.sh --agent claude --scheme user_front
#   ./swe-bench-lite.sh --task-ids astropy__astropy-12907

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

bench_guard_ci
bench_check_arch
bench_check_binfmt || true
bench_check_docker_socket || true
bench_check_disk "${BENCH_ROOT}/runs" || true

AGENT="claude"
TASK_IDS=""
N_CONCURRENT=""
SCHEME="user_front"
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
            echo "[swe-bench-lite] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

bench_require_harbor

if (( RUN_SMOKETEST == 1 )); then
    echo "[swe-bench-lite] running smoketest first" \
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
        echo "[swe-bench-lite] unknown agent: $AGENT" >&2
        exit 2
        ;;
esac

export PYTHONPATH="${ADAPTER_DIR}:${BENCH_ROOT}/adapters${PYTHONPATH:+:${PYTHONPATH}}"

CONC="$(bench_concurrency "${N_CONCURRENT}")"
RUN_NAME="swe-bench-lite-${AGENT}-${SCHEME}-$(date +%Y%m%dT%H%M%S)"
RUN_DIR="${BENCH_ROOT}/runs/${RUN_NAME}"
mkdir -p "${RUN_DIR}"

echo "[swe-bench-lite] agent=${AGENT} scheme=${SCHEME}" \
     "concurrency=${CONC}" >&2
echo "[swe-bench-lite] output dir: ${RUN_DIR}" >&2

HARBOR_ARGS=(
    run
    --jobs-dir "${RUN_DIR}"
    --job-name "${RUN_NAME}"
    --n-concurrent "${CONC}"
    --agent-import-path "${AGENT_IMPORT}"
    --dataset princeton-nlp/swe-bench-lite
)

if [[ -n "${TASK_IDS}" ]]; then
    # Harbor 0.6.x: -i / --include-task-name takes a single name, repeatable.
    IFS=',' read -r -a _task_arr <<< "${TASK_IDS}"
    for t in "${_task_arr[@]}"; do
        [[ -n "$t" ]] && HARBOR_ARGS+=(-i "$t")
    done
fi

exec "${HARBOR_BIN}" "${HARBOR_ARGS[@]}"
