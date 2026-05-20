#!/usr/bin/env bash
#
# prefetch.sh — download + cache a benchmark dataset BEFORE the sealed run.
#
# Why this exists
# ---------------
# The full-scale runners (terminal-bench.sh, swe-bench-lite.sh) pass
# `--dataset <org>/<name>` to harbor, which has to resolve that name against
# harbor's hosted registry (and, for HF-backed datasets, against
# huggingface.co). The harbor container runs behind the universal egress
# firewall with harbor's backend deliberately NOT on the allowlist, so a
# cold sealed run would be blocked from fetching the dataset — correct, but
# it means the dataset must already be on disk.
#
# This runner does that fetch as a SEPARATE phase, with harbor's backend
# temporarily added to a throwaway allowlist, into the persistent cache that
# harbor.sh bind-mounts at /harbor-cache. After it finishes you remove
# nothing — the real benchmark run reuses the cache and runs with the backend
# unreachable, so at the moment the agent is running and the upstream LLM is
# replying, the firewall has NO route back to harbor's backend.
#
# This is the "download before, sealed during" split the project requires:
# the only window in which harbor can reach its backend is this prefetch,
# which runs no agent and sends no upstream LLM traffic.
#
# Usage:
#   ./prefetch.sh                       # prefetch every real dataset
#   ./prefetch.sh --target terminal-bench
#   ./prefetch.sh --target swe-bench-lite
#   ./prefetch.sh --dataset some/other-dataset
#
# The smoketest target needs NO prefetch — it uses the vendored
# hello-harness task and never touches harbor's backend.

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
TARGET=""
DATASET=""
AGENT="claude"
N_CONCURRENT=""
while (( $# > 0 )); do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --dataset) DATASET="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --n-concurrent) N_CONCURRENT="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,30p' "$0"
            exit 0
            ;;
        *)
            echo "[prefetch] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Map a target name to its harbor dataset identifier. Keep in sync with the
# --dataset args in terminal-bench.sh / swe-bench-lite.sh.
_dataset_for_target() {
    case "$1" in
        terminal-bench) echo "terminal-bench/terminal-bench-core" ;;
        swe-bench-lite) echo "princeton-nlp/swe-bench-lite" ;;
        *) return 1 ;;
    esac
}

# Build the list of (target,dataset) pairs to prefetch.
declare -a DATASETS=()
if [[ -n "${DATASET}" ]]; then
    DATASETS+=("${DATASET}")
elif [[ -n "${TARGET}" ]]; then
    ds="$(_dataset_for_target "${TARGET}")" || {
        echo "[prefetch] unknown target: ${TARGET}" >&2
        echo "[prefetch] expected: terminal-bench | swe-bench-lite" \
             "(or pass --dataset <org>/<name>)" >&2
        exit 2
    }
    DATASETS+=("${ds}")
else
    # No target: prefetch every real dataset the runners can request.
    DATASETS+=("$(_dataset_for_target terminal-bench)")
    DATASETS+=("$(_dataset_for_target swe-bench-lite)")
fi

bench_require_harbor

# --- prefetch allowlist ------------------------------------------------------
#
# Same firewall, different allowlist file. The harbor container always runs
# init-firewall.sh; for prefetch we point it at a throwaway allowlist that is
# the base allowlist PLUS harbor's backend / huggingface hosts. Nothing here
# touches the real .harness-allowlist, so the sealed run still has the backend
# unreachable.
#
# The exact Supabase project host harbor uses can vary by harbor version; if a
# prefetch fails with a blocked host you don't recognise, add it to
# HARNESS_BENCH_PREFETCH_HOSTS and re-run. The first blocked-host error names
# the host to add.
BASE_ALLOWLIST="${HARNESS_ALLOWLIST_PATH:-${REPO_ROOT}/.harness-allowlist}"
if [[ ! -f "${BASE_ALLOWLIST}" ]]; then
    echo "[prefetch] FATAL: base allowlist not found at ${BASE_ALLOWLIST}." >&2
    echo "[prefetch] Copy .harness-allowlist.example to .harness-allowlist." >&2
    exit 1
fi

DEFAULT_PREFETCH_HOSTS="harborframework.com cdn.harborframework.com \
huggingface.co cdn-lfs.huggingface.co datasets-server.huggingface.co"
PREFETCH_HOSTS="${HARNESS_BENCH_PREFETCH_HOSTS:-${DEFAULT_PREFETCH_HOSTS}}"

PREFETCH_ALLOWLIST="$(mktemp -t harness-prefetch-allowlist.XXXXXX)"
trap 'rm -f "${PREFETCH_ALLOWLIST}"' EXIT
{
    cat "${BASE_ALLOWLIST}"
    echo "# --- prefetch-only hosts (harbor backend + huggingface) ---"
    # Split on whitespace and/or commas.
    tr ', ' '\n\n' <<< "${PREFETCH_HOSTS}" | while read -r h; do
        [[ -n "$h" ]] && echo "$h"
    done
} > "${PREFETCH_ALLOWLIST}"
export HARNESS_ALLOWLIST_PATH="${PREFETCH_ALLOWLIST}"

# Huggingface client ONLINE for the prefetch (the sealed runners set these to
# 1 so the same client uses the cache instead of going to the network).
export HF_HUB_OFFLINE=0
export HF_DATASETS_OFFLINE=0

# Resolve the adapter import path. No agent task runs during prefetch (see the
# zero-task filter below), but `harbor run` still wants a valid import path, so
# we supply the chosen agent's adapter.
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
        echo "[prefetch] unknown agent: $AGENT (expected: claude|opencode)" >&2
        exit 2
        ;;
esac
export PYTHONPATH="${ADAPTER_DIR}:${BENCH_ROOT}/adapters${PYTHONPATH:+:${PYTHONPATH}}"

CONC="$(bench_concurrency "${N_CONCURRENT}")"

# --- per-dataset prefetch ----------------------------------------------------
#
# Goal: make harbor resolve + download + cache the dataset WITHOUT executing
# any agent task (no LLM budget, no upstream traffic during the online phase).
#
# harbor 0.6.x exposes dataset prep through `harbor run`, gated by the same
# include filter (`-i`) the other runners use. We pass an include filter that
# matches NO task, so harbor enumerates the dataset (forcing the download into
# the cache) but has nothing to run. harbor must fetch the dataset to know its
# task names, so the cache is warmed as a side effect of resolving the filter.
#
# This is the one harbor-internals assumption in this file. If your harbor
# build downloads only when a task actually runs, or rejects a zero-match
# filter before fetching, override the whole harbor invocation:
#
#   HARNESS_BENCH_PREFETCH_HARBOR_ARGS="download --dataset {dataset}" \
#       ./prefetch.sh --target terminal-bench
#
# The literal token {dataset} (if present) is replaced with the dataset id.
NOOP_TASK="${HARNESS_BENCH_PREFETCH_NOOP_TASK:-__harness_prefetch_noop__}"

for ds in "${DATASETS[@]}"; do
    echo "[prefetch] >>> dataset=${ds} (firewall: backend temporarily" \
         "allowlisted; no agent task runs)" >&2
    RUN_NAME="prefetch-$(echo "${ds}" | tr '/' '-')-$(date +%Y%m%dT%H%M%S)"
    RUN_DIR="${BENCH_ROOT}/runs/${RUN_NAME}"
    mkdir -p "${RUN_DIR}"

    if [[ -n "${HARNESS_BENCH_PREFETCH_HARBOR_ARGS:-}" ]]; then
        # Operator override: split on whitespace, substitute {dataset}.
        read -r -a _override <<< "${HARNESS_BENCH_PREFETCH_HARBOR_ARGS//\{dataset\}/${ds}}"
        harbor_args=("${_override[@]}")
    else
        harbor_args=(
            run
            --dataset "${ds}"
            --jobs-dir "${RUN_DIR}"
            --job-name "${RUN_NAME}"
            --n-concurrent "${CONC}"
            --agent-import-path "${AGENT_IMPORT}"
            -i "${NOOP_TASK}"
        )
    fi

    if ! "${HARBOR_BIN}" "${harbor_args[@]}"; then
        echo "[prefetch] WARNING: harbor exited non-zero for ${ds}." >&2
        echo "[prefetch] If it was blocked reaching a host, add that host to" >&2
        echo "[prefetch] HARNESS_BENCH_PREFETCH_HOSTS and re-run. If it ran" >&2
        echo "[prefetch] tasks instead of just downloading, override the" >&2
        echo "[prefetch] harbor command with HARNESS_BENCH_PREFETCH_HARBOR_ARGS." >&2
    fi
done

echo "[prefetch] done. Dataset cache lives under" \
     "${HARNESS_BENCH_CACHE_DIR:-${BENCH_ROOT}/cache} and is reused by the" \
     "sealed runs (which never allowlist harbor's backend)." >&2
