#!/usr/bin/env bash
#
# Phase 2 proxy integration test.
#
# Brings up ollama + the real proxy + a mock upstream, then runs four
# scenarios against /api/chat through ollama:
#   A. text     — non-streaming, verify content + usage round-trip.
#   B. tool     — verify the tool-call markdown block is parsed and re-emitted
#                 as a structured tool_calls field with done_reason=tool_calls.
#   C. forward  — verify the upstream received {"model","messages"} ONLY and
#                 the final user message contains the cooperative-prompt wrapper.
#   D. stream   — stream=true, verify multiple NDJSON lines and a final done:true.
#
# Also runs proxy/test_proxy.py inside the proxy container.
#
# Always tears down compose state on exit. Uses a dedicated project name so
# this test doesn't collide with other compose stacks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PROJECT_NAME="harness-proxy-test"

echo "============================================================"
echo " harness Phase 2 proxy integration test"
echo "============================================================"

# --- preflight ---------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[proxy-test] ERROR: docker daemon is not reachable" >&2
    exit 1
fi

# --- temp env + override -----------------------------------------------------

ENV_FILE="$(mktemp -t harness-proxy.XXXXXX.env)"
OVERRIDE_FILE="$(mktemp -t harness-proxy.XXXXXX.yml)"

write_env() {
    local scenario="$1"
    cat >"${ENV_FILE}" <<EOF
PROXY_API_URL=http://mockupstream:9000/v1/chat/completions
PROXY_API_KEY=test-key-1234
PROXY_API_MODEL=test-model
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_AGENT_MODEL=harness
OLLAMA_CONTEXT_LENGTH=200000
MOCK_SCENARIO=${scenario}
EOF
}

cat >"${OVERRIDE_FILE}" <<'EOF'
services:
  ollama:
    ports:
      - "11434:11434"
  mockupstream:
    image: python:3.12-slim
    working_dir: /app
    environment:
      MOCK_SCENARIO: ${MOCK_SCENARIO:-text}
    volumes:
      - ./scripts/mock_upstream.py:/app/mock_upstream.py:ro
    networks:
      - harness-net
    expose:
      - "9000"
    command: >
      sh -c "pip install --quiet --no-cache-dir flask==3.0.3 &&
             python /app/mock_upstream.py"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys;\nu=urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=2);\nsys.exit(0 if u.status==200 else 1)"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 20s
EOF

write_env "text"

COMPOSE=(docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" -f docker-compose.yml -f "${OVERRIDE_FILE}")

cleanup() {
    echo "[proxy-test] cleanup: tearing down compose state"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "${ENV_FILE}" "${OVERRIDE_FILE}"
}
trap cleanup EXIT INT TERM

# Defensive: clear any stale state from a prior run.
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

# --- compose config sanity check --------------------------------------------

echo "[proxy-test] validating compose config"
"${COMPOSE[@]}" config >/dev/null

# --- bring up ----------------------------------------------------------------

echo "[proxy-test] building and starting services (mockupstream + proxy + ollama)"
"${COMPOSE[@]}" up -d --build

# --- wait for healthy --------------------------------------------------------

is_healthy() {
    local svc="$1"
    local cid
    cid="$("${COMPOSE[@]}" ps -q "${svc}" 2>/dev/null || true)"
    if [[ -z "${cid}" ]]; then return 1; fi
    local status
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")"
    [[ "${status}" == "healthy" ]]
}

wait_healthy() {
    local timeout_s="$1"; shift
    local deadline=$(( $(date +%s) + timeout_s ))
    while true; do
        local all_ok=1
        for svc in "$@"; do
            if ! is_healthy "${svc}"; then all_ok=0; break; fi
        done
        if (( all_ok )); then return 0; fi
        if (( $(date +%s) >= deadline )); then return 1; fi
        sleep 2
    done
}

echo "[proxy-test] waiting up to 120s for mockupstream + proxy + ollama to be healthy"
if ! wait_healthy 120 mockupstream proxy ollama; then
    echo "[proxy-test] ERROR: services did not become healthy" >&2
    "${COMPOSE[@]}" ps >&2 || true
    echo "--- mockupstream logs ---" >&2; "${COMPOSE[@]}" logs mockupstream >&2 || true
    echo "--- proxy logs ---"        >&2; "${COMPOSE[@]}" logs proxy        >&2 || true
    echo "--- ollama logs ---"       >&2; "${COMPOSE[@]}" logs ollama       >&2 || true
    exit 1
fi
echo "[proxy-test] all services healthy"

# --- helpers -----------------------------------------------------------------

OLLAMA_URL="http://localhost:11434"

fail() {
    local label="$1"; shift
    echo "[proxy-test] FAIL: ${label}" >&2
    if [[ $# -gt 0 ]]; then echo "[proxy-test] detail: $*" >&2; fi
    echo "--- mockupstream logs ---" >&2; "${COMPOSE[@]}" logs mockupstream >&2 || true
    echo "--- proxy logs ---"        >&2; "${COMPOSE[@]}" logs proxy        >&2 || true
    echo "--- ollama logs ---"       >&2; "${COMPOSE[@]}" logs ollama       >&2 || true
    exit 1
}

restart_mock_with_scenario() {
    local scenario="$1"
    write_env "${scenario}"
    echo "[proxy-test] restarting mockupstream with MOCK_SCENARIO=${scenario}"
    "${COMPOSE[@]}" up -d --force-recreate mockupstream >/dev/null
    if ! wait_healthy 60 mockupstream; then
        fail "mockupstream did not become healthy after switching to scenario '${scenario}'"
    fi
}

# --- unit tests inside the proxy container ----------------------------------

echo "[proxy-test] running proxy unit tests inside the proxy container"
"${COMPOSE[@]}" run --rm proxy python -m unittest test_proxy.py -v \
    || fail "unit tests failed"

# --- Scenario A: text round-trip --------------------------------------------

echo "[proxy-test] scenario A: text round-trip (stream=false)"
restart_mock_with_scenario "text"

A_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d '{"model":"harness","messages":[{"role":"user","content":"hi"}],"stream":false}')" \
    || fail "A: /api/chat request failed"
echo "[proxy-test]   A response: ${A_BODY}"
echo "${A_BODY}" | grep -q "Hello from mock upstream" \
    || fail "A: response did not contain upstream content" "${A_BODY}"
echo "${A_BODY}" | grep -q '"prompt_eval_count":42' \
    || fail "A: prompt_eval_count != 42 (usage not propagated)" "${A_BODY}"
echo "${A_BODY}" | grep -q '"eval_count":7' \
    || fail "A: eval_count != 7 (usage not propagated)" "${A_BODY}"
echo "${A_BODY}" | grep -q '"done_reason":"stop"' \
    || fail "A: done_reason was not stop" "${A_BODY}"

# --- Scenario B: tool call --------------------------------------------------

echo "[proxy-test] scenario B: tool call"
restart_mock_with_scenario "tool"

B_REQ='{
  "model": "harness",
  "messages": [{"role":"user","content":"what is the weather in Atlanta?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get the weather for a city.",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type":"string","description":"city name"}},
        "required": ["city"]
      }
    }
  }],
  "stream": false
}'
B_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d "${B_REQ}")" \
    || fail "B: /api/chat request failed"
echo "[proxy-test]   B response: ${B_BODY}"
echo "${B_BODY}" | grep -q '"tool_calls"' \
    || fail "B: response had no tool_calls field" "${B_BODY}"
echo "${B_BODY}" | grep -q '"name":"get_weather"' \
    || fail "B: tool_calls did not contain get_weather" "${B_BODY}"
echo "${B_BODY}" | grep -q '"city":"Atlanta"' \
    || fail "B: tool_calls arguments did not include city=Atlanta" "${B_BODY}"
echo "${B_BODY}" | grep -q '"done_reason":"tool_calls"' \
    || fail "B: done_reason was not tool_calls" "${B_BODY}"

# --- Scenario C: forwarded request shape ------------------------------------

echo "[proxy-test] scenario C: forwarded request shape"
restart_mock_with_scenario "text"

# Send a request with tools so the cooperative prompt wrapper kicks in.
C_REQ='{
  "model": "harness",
  "messages": [{"role":"user","content":"please pick a tool"}],
  "tools": [{"type":"function","function":{"name":"get_weather","description":"weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],
  "stream": false
}'
C_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d "${C_REQ}")" \
    || fail "C: /api/chat request failed"
echo "[proxy-test]   C response (truncated): $(echo "${C_BODY}" | head -c 200)"

# Inspect the upstream's logs to confirm what the proxy forwarded.
MOCK_LOGS="$("${COMPOSE[@]}" logs --tail=200 mockupstream 2>&1 || true)"
LAST_BODY_LINE="$(echo "${MOCK_LOGS}" | grep -E '\[mock-upstream\] (POST|PUT) ' | tail -1)"
if [[ -z "${LAST_BODY_LINE}" ]]; then
    fail "C: no recent POST seen in mockupstream logs" "${MOCK_LOGS}"
fi
# Extract the body=<json> portion. The body= prefix is followed by the JSON.
FORWARDED_BODY="${LAST_BODY_LINE#*body=}"
echo "[proxy-test]   forwarded body: ${FORWARDED_BODY}"

# Verify only the two allowed top-level keys are present. We check via python
# inside the proxy container so we don't need jq on the host.
KEY_CHECK="$("${COMPOSE[@]}" exec -T proxy python -c "
import json, sys
body = json.loads(sys.stdin.read())
keys = sorted(body.keys())
if keys != ['messages', 'model']:
    print('UNEXPECTED_KEYS:' + ','.join(keys))
    sys.exit(0)
last = body['messages'][-1]
content = last.get('content','')
if '### Tool Usage Instructions' not in content:
    print('NO_WRAPPER')
    sys.exit(0)
print('OK')
" <<<"${FORWARDED_BODY}")" || fail "C: failed to inspect forwarded body" "${FORWARDED_BODY}"

case "${KEY_CHECK}" in
    *OK*) ;;
    *UNEXPECTED_KEYS*) fail "C: forwarded body had keys other than {model,messages}: ${KEY_CHECK}" "${FORWARDED_BODY}" ;;
    *NO_WRAPPER*)      fail "C: final user message is missing the cooperative-prompt wrapper" "${FORWARDED_BODY}" ;;
    *)                 fail "C: unexpected key-check output: ${KEY_CHECK}" "${FORWARDED_BODY}" ;;
esac

# --- Scenario D: streaming --------------------------------------------------

echo "[proxy-test] scenario D: stream=true returns multiple NDJSON lines"
# mockupstream still on text scenario from C
D_RAW="$(curl -fsS -N -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d '{"model":"harness","messages":[{"role":"user","content":"hi"}],"stream":true}')" \
    || fail "D: /api/chat (stream=true) request failed"
echo "[proxy-test]   D raw (first 300 chars): $(echo "${D_RAW}" | head -c 300)"

D_LINE_COUNT="$(echo "${D_RAW}" | grep -c '"model"' || true)"
if (( D_LINE_COUNT < 2 )); then
    fail "D: expected at least 2 NDJSON objects, got ${D_LINE_COUNT}" "${D_RAW}"
fi
echo "${D_RAW}" | tail -1 | grep -q '"done":true' \
    || fail "D: final line did not have done:true" "${D_RAW}"

echo "============================================================"
echo " PROXY TEST PASSED"
echo "============================================================"
exit 0
