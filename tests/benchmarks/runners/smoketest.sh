#!/usr/bin/env bash
#
# smoketest.sh — small, fast Harbor run to verify adapter wiring.
#
# Runs 1-3 of Terminal-Bench's smallest tasks against the `user_front`
# scheme with one agent. 5-15 min target on x86_64. On aarch64 with QEMU,
# expect 30-60 min for 1 task.
#
# Usage:
#   ./smoketest.sh                  # defaults: opencode, 1 task
#   ./smoketest.sh --agent opencode
#   ./smoketest.sh --task-id hello-world
#   ./smoketest.sh --n-concurrent 2

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

bench_guard_ci
bench_check_arch
bench_check_binfmt || true   # warn but don't block; user may not be on aarch64
bench_check_docker_socket || true
bench_check_disk "${BENCH_ROOT}/runs" || true

# --- Arg parsing -------------------------------------------------------------
AGENT="opencode"
TASK_IDS=""
N_CONCURRENT=""
SCHEME="user_front"
while (( $# > 0 )); do
    case "$1" in
        --agent) AGENT="$2"; shift 2 ;;
        --task-id|--task-ids) TASK_IDS="$2"; shift 2 ;;
        --n-concurrent) N_CONCURRENT="$2"; shift 2 ;;
        --scheme) SCHEME="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,15p' "$0"
            exit 0
            ;;
        *)
            echo "[smoketest] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

bench_require_harbor
bench_apply_scheme "${SCHEME}"

# Resolve adapter import path for the chosen agent.
case "$AGENT" in
    opencode)
        AGENT_IMPORT="harness_opencode_agent:HarnessOpencodeAgent"
        ADAPTER_DIR="${BENCH_ROOT}/adapters/harness_opencode"
        ;;
    *)
        echo "[smoketest] unknown agent: $AGENT (expected: opencode)" >&2
        exit 2
        ;;
esac

# Make the adapter discoverable on PYTHONPATH so Harbor's import-path
# loader can find it.
export PYTHONPATH="${ADAPTER_DIR}:${BENCH_ROOT}/adapters${PYTHONPATH:+:${PYTHONPATH}}"

CONC="$(bench_concurrency "${N_CONCURRENT}")"
RUN_NAME="smoketest-${AGENT}-${SCHEME}-$(date +%Y%m%dT%H%M%S)"
RUN_DIR="${BENCH_ROOT}/runs/${RUN_NAME}"
mkdir -p "${RUN_DIR}"

echo "[smoketest] agent=${AGENT} scheme=${SCHEME} concurrency=${CONC}" >&2
echo "[smoketest] output dir: ${RUN_DIR}" >&2
echo "[smoketest] tasks: ${TASK_IDS:-<smallest single TB task>}" >&2

# Harbor invocation. The exact flag spelling is the harbor>=2.x interface
# documented at harborframework.com/docs. If the API moves, update here.
HARBOR_ARGS=(
    run
    --jobs-dir "${RUN_DIR}"
    --job-name "${RUN_NAME}"
    --n-concurrent "${CONC}"
    --agent-import-path "${AGENT_IMPORT}"
)

if [[ -n "${TASK_IDS}" ]]; then
    # Harbor 0.6.x: -i / --include-task-name takes a single name and is
    # repeatable. Split comma-separated --task-ids into one flag per task.
    IFS=',' read -r -a _task_arr <<< "${TASK_IDS}"
    for t in "${_task_arr[@]}"; do
        [[ -n "$t" ]] && HARBOR_ARGS+=(-i "$t")
    done
else
    # Default to the vendored hello-harness task so smoketest runs
    # offline: no harbor registry call, no tarball download, nothing
    # outside the allowlist firewall. The task is trivial-by-design
    # (write /app/hello.txt) — sufficient to prove the full wiring path
    # (per-task container -> harness compose -> proxy -> upstream LLM ->
    # tool call -> verifier) without leaking the user's network footprint
    # to harborframework.com. For richer task coverage, use
    # `harness benchmark terminal-bench` (which DOES need harbor's
    # registry hosts in your allowlist — see tests/benchmarks/README.md).
    HARBOR_ARGS+=(--dataset "${BENCH_ROOT}/tasks/hello-harness" --n-tasks 1)
fi

exec "${HARBOR_BIN}" "${HARBOR_ARGS[@]}"
