#!/usr/bin/env bash
#
# mock-smoketest.sh — wiring test for the benchmark stack.
#
# What this proves (without consuming real API budget):
#   * docker-compose stack comes up (proxy + mock-api).
#   * bench_apply_scheme reads each schemes/*.json correctly.
#   * PROXY_PROMPT_MODE actually switches per scheme — the proxy
#     receives the var and rebuilds prompts accordingly.
#   * Every scheme produces a measurably different request body at the
#     mock upstream (verifies that schemes ARE comparing different
#     things, not silently collapsing to the same prompt).
#
# What it does NOT prove:
#   * Harbor adapter wiring (covered by runners/smoketest.sh with a real
#     upstream).
#   * harness-install.sh boot from scratch (we use the existing install).
#
# This script is opt-in and completely independent from
# tests/benchmarks/runners/*. Your real benchmark command stays:
#     ./tests/benchmarks/runners/smoketest.sh --agent opencode --scheme user_front
# with a real .env on the host. This file leaves that flow untouched.
#
# Usage:
#   ./tests/benchmarks/mock-smoketest.sh
#   ./tests/benchmarks/mock-smoketest.sh --keep        # don't tear down at end
#   ./tests/benchmarks/mock-smoketest.sh --schemes user_front,passthrough

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BENCH_ROOT="$SCRIPT_DIR"

# Pull in bench_jq (host jq, container fallback) so JSON parsing here
# doesn't require host python or jq.
# shellcheck source=runners/_lib.sh
source "${SCRIPT_DIR}/runners/_lib.sh"

KEEP=0
SCHEMES_CSV=""
PROBE_MODES=0
while (( $# > 0 )); do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --schemes) SCHEMES_CSV="$2"; shift 2 ;;
        --probe-modes)
            # Also test every proxy-supported PROXY_PROMPT_MODE so we can
            # confirm scheme switching produces measurably different
            # upstream requests on actually-distinct modes. Adds synthetic
            # schemes: probe-user_front, probe-hybrid.
            PROBE_MODES=1; shift ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "[mock-smoketest] unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- preflight ---------------------------------------------------------------
command -v docker >/dev/null || { echo "[mock-smoketest] docker required"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "[mock-smoketest] docker compose v2 required"; exit 1; }
# JSON parsing goes through bench_jq, which falls back to a one-shot
# proxy-image container when host jq is missing — so no separate jq /
# python3 preflight is needed here.

# Bold + colors for the summary.
if [[ -t 1 ]]; then BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else BOLD=""; DIM=""; GREEN=""; RED=""; RESET=""; fi

# Discover schemes.
mapfile -t ALL_SCHEMES < <(find "${SCRIPT_DIR}/schemes" -maxdepth 1 -name '*.json' -exec basename {} .json \;)
if [[ -n "$SCHEMES_CSV" ]]; then
    IFS=',' read -r -a SCHEMES <<< "$SCHEMES_CSV"
else
    SCHEMES=("${ALL_SCHEMES[@]}")
fi

# Synthetic probe schemes: one per proxy-supported mode.
declare -A PROBE_MODE_FOR
if (( PROBE_MODES )); then
    for m in user_front hybrid; do
        SCHEMES+=("probe-${m}")
        PROBE_MODE_FOR["probe-${m}"]="$m"
    done
fi

(( ${#SCHEMES[@]} > 0 )) || { echo "[mock-smoketest] no schemes found"; exit 1; }

# Output dir + per-scheme captures.
RUN_DIR="${BENCH_ROOT}/runs/mock-smoketest-$(date +%Y%m%dT%H%M%S)"
mkdir -p "${RUN_DIR}"
echo "[mock-smoketest] capturing into ${RUN_DIR}"

# --- env file for the mock-mode stack ---------------------------------------
#
# We do NOT touch the real .env. We write a dedicated .env.mock and pass it
# explicitly to docker compose. The host's .env (if any) is untouched.
ENV_FILE="${RUN_DIR}/.env.mock"
cat > "${ENV_FILE}" <<EOF
PROXY_API_URL=http://mock-api:80/v1/chat/completions
PROXY_API_KEY=mock-key
DEFAULT_MODEL_NAME=mock-model
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
PROXY_TIMEOUT=30
OUTPUT_DIR=/output
PROXY_PROMPT_MODE=user_front
MODEL_CONTEXT_LENGTH=200000
MOCK_LOG_REQUESTS=1
MOCK_DEFAULT_CONTENT=Task acknowledged. Done.
HARNESS_ALLOWLIST_PATH=${RUN_DIR}/.harness-allowlist
EOF

# Minimal allowlist (mock-api is bare hostname, firewall treats as
# intra-cluster anyway, but the file must exist for the proxy container).
cat > "${RUN_DIR}/.harness-allowlist" <<'EOF'
# mock-smoketest allowlist — intentionally minimal.
mock-api
EOF

PROJECT="harness-mock-$$"
COMPOSE_ARGS=(
    --project-name "${PROJECT}"
    --env-file "${ENV_FILE}"
    -f "${REPO_ROOT}/docker-compose.yml"
    -f "${REPO_ROOT}/tests/benchmarks/mock-api/docker-compose.mock.yml"
)

# Always tear down on exit unless --keep.
cleanup() {
    if (( KEEP )); then
        echo "[mock-smoketest] --keep: leaving stack up. Tear down with:"
        echo "  docker compose ${COMPOSE_ARGS[*]} down -v"
    else
        echo "[mock-smoketest] tearing down..."
        (cd "${REPO_ROOT}" && docker compose "${COMPOSE_ARGS[@]}" down -v --remove-orphans) >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# --- bring up the stack ------------------------------------------------------
echo "[mock-smoketest] starting stack (project=${PROJECT})..."
(cd "${REPO_ROOT}" && docker compose "${COMPOSE_ARGS[@]}" up -d --build mock-api proxy) >"${RUN_DIR}/compose-up.log" 2>&1 || {
    echo "[mock-smoketest] FAIL: compose up failed; see ${RUN_DIR}/compose-up.log"
    tail -40 "${RUN_DIR}/compose-up.log" >&2
    exit 1
}

# Wait for proxy + mock-api healthy.
echo -n "[mock-smoketest] waiting for proxy + mock-api healthy "
for i in $(seq 1 40); do
    proxy_h=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-proxy-1" 2>/dev/null || echo unknown)
    mock_h=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-mock-api-1" 2>/dev/null || echo unknown)
    [[ "$proxy_h" == "healthy" && "$mock_h" == "healthy" ]] && break
    echo -n "."
    sleep 2
done
echo
if [[ "$proxy_h" != "healthy" || "$mock_h" != "healthy" ]]; then
    echo "[mock-smoketest] FAIL: proxy=$proxy_h mock=$mock_h after timeout"
    docker logs "${PROJECT}-proxy-1" 2>&1 | tail -20 >&2 || true
    docker logs "${PROJECT}-mock-api-1" 2>&1 | tail -20 >&2 || true
    exit 1
fi
echo "[mock-smoketest] proxy=$proxy_h mock=$mock_h"

# --- per-scheme test ---------------------------------------------------------
#
# For each scheme:
#   1. Apply the scheme's env (rewrite .env.mock's PROXY_PROMPT_MODE).
#   2. Restart proxy so it re-reads the env.
#   3. Truncate the mock's log marker so we count only this scheme's reqs.
#   4. Send a fixed OpenAI-format request through the proxy.
#   5. Capture what the mock received — body shape proves the prompt mode.
declare -A SCHEME_OK
declare -A SCHEME_MODE
declare -A SCHEME_BYTES
declare -A SCHEME_USER_PREFIX

for scheme in "${SCHEMES[@]}"; do
    scheme_file="${SCRIPT_DIR}/schemes/${scheme}.json"
    if [[ -n "${PROBE_MODE_FOR[$scheme]:-}" ]]; then
        # Synthetic probe scheme — mode comes from the lookup, not a file.
        mode="${PROBE_MODE_FOR[$scheme]}"
    elif [[ -f "$scheme_file" ]]; then
        # Read PROXY_PROMPT_MODE from the scheme JSON via bench_jq.
        mode=$(bench_jq -r '(.env // {}).PROXY_PROMPT_MODE // ""' "$scheme_file")
    else
        echo "[mock-smoketest] skip: ${scheme}.json missing"
        continue
    fi
    SCHEME_MODE[$scheme]="${mode}"

    echo
    echo "${BOLD}[scheme] ${scheme}${RESET} (PROXY_PROMPT_MODE=${mode})"

    # Update .env.mock and restart proxy.
    sed -i.bak "s|^PROXY_PROMPT_MODE=.*|PROXY_PROMPT_MODE=${mode}|" "${ENV_FILE}" || true
    rm -f "${ENV_FILE}.bak"
    (cd "${REPO_ROOT}" && docker compose "${COMPOSE_ARGS[@]}" up -d proxy) >>"${RUN_DIR}/compose-up.log" 2>&1
    # Brief wait for proxy to come healthy again.
    for i in $(seq 1 15); do
        h=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-proxy-1" 2>/dev/null || echo unknown)
        [[ "$h" == "healthy" ]] && break
        sleep 1
    done

    # Drop a marker into the mock log (we read since-bytes after).
    log_before=$(docker logs "${PROJECT}-mock-api-1" 2>&1 | wc -c)
    # Note the latest /output dump path BEFORE this scheme runs so we can
    # identify only this scheme's new file afterward.
    before_dump=$(ls -t "${REPO_ROOT}/state/output"/*_02_API_Request.json 2>/dev/null | head -1)

    # Send a representative OpenAI /v1/chat/completions request through the proxy.
    # We exec curl from inside the proxy container (localhost:8000 is the proxy
    # itself). Saves binding a host port and keeps the test fully internal.
    # The forwarded upstream body (inspected via state/output dump + mock log)
    # is what proves prompt-mode switching; the response body is not parsed.
    req_body=$(cat <<'JSON'
{
  "model": "GenAI",
  "stream": false,
  "messages": [
    {"role": "system", "content": "You are a coding agent."},
    {"role": "user", "content": "Create /app/hello.txt with content hello-harness."}
  ],
  "tools": [
    {"type": "function",
     "function": {"name": "bash",
                  "description": "run a shell command",
                  "parameters": {"type": "object",
                                  "properties": {"command": {"type": "string"}},
                                  "required": ["command"]}}}
  ]
}
JSON
)
    resp_file="${RUN_DIR}/${scheme}.proxy-response.json"
    if docker exec "${PROJECT}-proxy-1" curl -fsS -X POST \
            -H 'content-type: application/json' \
            --data "${req_body}" \
            http://127.0.0.1:8000/v1/chat/completions \
            -o /tmp/proxy-resp.json 2>>"${RUN_DIR}/${scheme}.curl-stderr.log"; then
        docker cp "${PROJECT}-proxy-1:/tmp/proxy-resp.json" "${resp_file}" 2>/dev/null || true
        SCHEME_OK[$scheme]="yes"
        echo "  ✓ proxy returned 200"
    else
        SCHEME_OK[$scheme]="no"
        echo "  ${RED}✗ proxy call failed${RESET} (see ${RUN_DIR}/${scheme}.curl-stderr.log)"
    fi

    # Pull the new lines from the mock log to capture what it received.
    log_after=$(docker logs "${PROJECT}-mock-api-1" 2>&1 | wc -c)
    bytes_diff=$(( log_after - log_before ))
    SCHEME_BYTES[$scheme]="$bytes_diff"
    docker logs "${PROJECT}-mock-api-1" 2>&1 | tail -c "${bytes_diff}" \
        > "${RUN_DIR}/${scheme}.mock-log-delta.txt" 2>/dev/null || true

    # Grab the proxy's upstream-request dump for THIS scheme's call. The
    # proxy writes timestamped files into /output (bind-mounted on host
    # as state/output). We pick the newest _02_API_Request.json that
    # didn't exist before this scheme started.
    after_dump=$(ls -t "${REPO_ROOT}/state/output"/*_02_API_Request.json 2>/dev/null | head -1)
    if [[ -n "$after_dump" && "$after_dump" != "$before_dump" ]]; then
        cp "$after_dump" "${RUN_DIR}/${scheme}.upstream-request.json"
    fi

    # Extract a preview that highlights where the request was placed and
    # whether tool-scaffolding was injected — the discriminator across
    # PROXY_PROMPT_MODE values.
    if [[ -s "${RUN_DIR}/${scheme}.upstream-request.json" ]]; then
        preview=$(bench_jq -r '
            (.messages // []) as $m
            | "roles: " + ([$m[].role // "?"] | join(","))
            , "count: " + ($m | length | tostring)
            , ( $m | to_entries[] |
                ((.value.content // "") | tostring) as $c |
                "  [\(.key)/\(.value.role // "?")] len=\($c | length) head=\($c[0:120] | tojson)" +
                (if ($c | length) > 240
                 then "\n       tail=" + ($c[-120:] | tojson)
                 else "" end)
              )
        ' "${RUN_DIR}/${scheme}.upstream-request.json")
        SCHEME_USER_PREFIX[$scheme]="${preview}"
        echo "    upstream request captured ($(stat -c%s "${RUN_DIR}/${scheme}.upstream-request.json") bytes)"
    else
        SCHEME_USER_PREFIX[$scheme]="<no upstream request captured>"
    fi
done

# --- summary -----------------------------------------------------------------
echo
echo "${BOLD}==================== SUMMARY ====================${RESET}"
printf "%-15s %-12s %-7s %-10s\n" "SCHEME" "PROMPT_MODE" "OK" "MOCK_BYTES"
echo "----------------------------------------------------"
for scheme in "${SCHEMES[@]}"; do
    ok="${SCHEME_OK[$scheme]:-n/a}"
    okm="${ok}"
    [[ "$ok" == "yes" ]] && okm="${GREEN}yes${RESET}"
    [[ "$ok" == "no"  ]] && okm="${RED}no${RESET}"
    printf "%-15s %-12s %-15b %-10s\n" \
        "${scheme}" \
        "${SCHEME_MODE[$scheme]:-?}" \
        "${okm}" \
        "${SCHEME_BYTES[$scheme]:-0}"
done

echo
echo "${BOLD}Per-scheme upstream-request preview (proves scheme switching):${RESET}"
for scheme in "${SCHEMES[@]}"; do
    echo
    echo "${BOLD}-- ${scheme} --${RESET}"
    echo "${DIM}${SCHEME_USER_PREFIX[$scheme]}${RESET}"
done

# Cross-scheme diff: if multiple schemes produced the SAME upstream request,
# the runs are not actually testing different schemes. Flag it.
echo
echo "${BOLD}Cross-scheme uniqueness:${RESET}"
declare -A FIRST_HASH
collision=""
for scheme in "${SCHEMES[@]}"; do
    f="${RUN_DIR}/${scheme}.upstream-request.json"
    if [[ -s "$f" ]]; then
        h=$(sha256sum "$f" | cut -c1-16)
        if [[ -n "${FIRST_HASH[$h]:-}" ]]; then
            collision="${collision}${scheme} == ${FIRST_HASH[$h]} (hash ${h})\n"
        else
            FIRST_HASH[$h]="$scheme"
        fi
        printf "  %-20s sha256:%s\n" "$scheme" "$h"
    else
        printf "  %-20s ${RED}<no request captured>${RESET}\n" "$scheme"
    fi
done
if [[ -n "$collision" ]]; then
    echo
    echo "${RED}WARNING: schemes produced identical upstream requests:${RESET}"
    printf "  %b" "$collision"
    echo "  This means scheme-switching is NOT actually testing different prompts."
    echo "  Common cause: PROXY_PROMPT_MODE is invalid and proxy fell back to default."
fi

echo
echo "[mock-smoketest] artifacts in: ${RUN_DIR}"
