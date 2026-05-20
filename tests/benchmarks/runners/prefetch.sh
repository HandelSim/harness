#!/usr/bin/env bash
#
# prefetch.sh — download + cache a benchmark dataset BEFORE the sealed run.
#
# Why this exists
# ---------------
# terminal-bench / swe-bench-lite need harbor to resolve `--dataset
# <org>/<name>` against harbor's hosted registry (Supabase + CDN). That is
# the ONE outbound harbor must make to its own backend. We do not want that
# backend reachable while the agent + upstream LLM are running, because then
# anything harbor observed could leak back to it.
#
# This runner splits the work into two phases so "download before, sealed
# during" is real rather than documented:
#
#   1. prefetch  (THIS script) — backend temporarily allowlisted, dataset is
#      downloaded into the PERSISTENT harbor cache (see harbor/harbor.sh:
#      /harbor-cache, default tests/benchmarks/runs/.harbor-cache).
#   2. sealed run (terminal-bench.sh / swe-bench-lite.sh) — backend REMOVED
#      from .harness-allowlist; harbor reads the dataset from the cache and
#      makes zero calls to its backend. init-firewall.sh fails closed, so if
#      the cache is missing the sealed run errors instead of phoning home.
#
# Allowlist handling is intentionally yours (the firewall is the same shared
# init-firewall.sh as every other harness container — no harbor-specific
# rules). For THIS prefetch only, add harbor's backend hosts to
# .harness-allowlist; remove them before the sealed run.
#
# NOTE: harbor 0.6.6 has no dedicated "download-only" subcommand we rely on,
# so prefetch forces the download by running ONE warmup task (`--n-tasks 1`).
# That resolves + downloads + caches the whole dataset tarball. The measured
# benchmark is the separate full sealed run, not this warmup. If a future
# harbor gains a fetch-only command, replace the `harbor run` below with it.
#
# Usage:
#   ./prefetch.sh --target terminal-bench
#   ./prefetch.sh --target swe-bench-lite --agent claude
#   ./prefetch.sh --target terminal-bench --task-id hello-world

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
TARGET=""
TASK_IDS=""
N_CONCURRENT=""
while (( $# > 0 )); do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --task-id|--task-ids) TASK_IDS="$2"; shift 2 ;;
        --n-concurrent) N_CONCURRENT="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,40p' "$0"
            exit 0
            ;;
        *)
            echo "[prefetch] unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

case "$TARGET" in
    terminal-bench)  DATASET="terminal-bench/terminal-bench-core" ;;
    swe-bench-lite)  DATASET="princeton-nlp/swe-bench-lite" ;;
    "")
        echo "[prefetch] --target is required (terminal-bench | swe-bench-lite)." >&2
        echo "[prefetch] smoketest needs no prefetch — it uses the vendored" \
             "hello-harness task and never calls harbor's backend." >&2
        exit 2
        ;;
    *)
        echo "[prefetch] unknown target: $TARGET" \
             "(expected terminal-bench | swe-bench-lite)" >&2
        exit 2
        ;;
esac

bench_require_harbor

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

# Same cache dir default as harbor/harbor.sh so we can verify it populated.
CACHE_DIR="${HARNESS_BENCH_CACHE_DIR:-${REPO_ROOT}/tests/benchmarks/runs/.harbor-cache}"
mkdir -p "${CACHE_DIR}"

CONC="$(bench_concurrency "${N_CONCURRENT}")"
RUN_NAME="prefetch-${TARGET}-${AGENT}-$(date +%Y%m%dT%H%M%S)"
RUN_DIR="${BENCH_ROOT}/runs/${RUN_NAME}"
mkdir -p "${RUN_DIR}"

cat >&2 <<EOF
[prefetch] ============================================================
[prefetch] PREFETCH PHASE — harbor's backend is expected to be reachable.
[prefetch] target=${TARGET} dataset=${DATASET} agent=${AGENT}
[prefetch] cache=${CACHE_DIR}
[prefetch]
[prefetch] For THIS run only, harbor's registry hosts must be in your
[prefetch] .harness-allowlist (e.g. harborframework.com + its CDN/Supabase
[prefetch] host). Remove them again before the sealed benchmark run.
[prefetch] ============================================================
EOF

# Snapshot cache size before so we can tell whether the download actually
# landed in the mounted dir (vs. somewhere harbor cached that we don't
# persist). `du -sk` (kilobytes) is POSIX — `du -sb` is GNU-only and would
# misreport on macOS/Docker-Desktop hosts.
cache_kb_before=$(du -sk "${CACHE_DIR}" 2>/dev/null | awk '{print $1}')
cache_kb_before=${cache_kb_before:-0}

HARBOR_ARGS=(
    run
    --jobs-dir "${RUN_DIR}"
    --job-name "${RUN_NAME}"
    --n-concurrent "${CONC}"
    --agent-import-path "${AGENT_IMPORT}"
    --dataset "${DATASET}"
    --n-tasks 1
)
if [[ -n "${TASK_IDS}" ]]; then
    IFS=',' read -r -a _task_arr <<< "${TASK_IDS}"
    for t in "${_task_arr[@]}"; do
        [[ -n "$t" ]] && HARBOR_ARGS+=(-i "$t")
    done
fi

if ! "${HARBOR_BIN}" "${HARBOR_ARGS[@]}"; then
    echo "[prefetch] FAIL: warmup harbor run exited non-zero." >&2
    echo "[prefetch] If it failed resolving the dataset, harbor's backend is" \
         "probably NOT in .harness-allowlist — add it for the prefetch and" \
         "retry. (icmp-admin-prohibited / connection refused to" \
         "harborframework.com is the tell.)" >&2
    exit 1
fi

# Verify the dataset actually persisted into the mounted cache. If it didn't
# grow, harbor cached somewhere outside /harbor-cache and the sealed run will
# fail closed (no leak, but it won't run). Surface that loudly now.
cache_kb_after=$(du -sk "${CACHE_DIR}" 2>/dev/null | awk '{print $1}')
cache_kb_after=${cache_kb_after:-0}
grew=$(( cache_kb_after - cache_kb_before ))

echo "[prefetch] cache grew by ${grew} KB (${cache_kb_before} ->" \
     "${cache_kb_after})" >&2
if (( grew <= 0 )); then
    cat >&2 <<EOF
[prefetch] WARNING: the persistent cache (${CACHE_DIR}) did not grow.
[prefetch] harbor likely cached the dataset OUTSIDE /harbor-cache (i.e. not
[prefetch] under HOME / XDG_CACHE_HOME). The sealed run will then fail closed
[prefetch] instead of reusing the cache. Point HARNESS_BENCH_CACHE_DIR at
[prefetch] harbor's real cache location, or confirm harbor 0.6.6's cache dir
[prefetch] and update harbor/harbor.sh's HOME/XDG_CACHE_HOME mapping.
EOF
    exit 1
fi

cat >&2 <<EOF
[prefetch] ------------------------------------------------------------
[prefetch] OK — dataset cached. Next:
[prefetch]   1. Remove harbor's backend hosts from .harness-allowlist.
[prefetch]   2. Run the SEALED benchmark (backend now unreachable):
[prefetch]        ./tests/benchmarks/runners/${TARGET}.sh --agent ${AGENT}
[prefetch]      It reuses ${CACHE_DIR} and makes zero backend calls; the
[prefetch]      firewall rejects any harbor outbound that isn't allowlisted.
[prefetch] ------------------------------------------------------------
EOF
