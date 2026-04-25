#!/usr/bin/env bash
#
# Phase 1 de-risk test (updated for Phase 2).
#
# Brings up ollama + the (real) proxy + a mock upstream API, then asserts
# that a chat request to ollama is forwarded through to the upstream and the
# response round-trips back to the caller. If anything fails, dumps logs and
# exits 1. Always tears down compose state on exit (success or failure).
#
# This stays the project's simplest smoke test. It uses the same mock upstream
# as scripts/proxy_test.sh so it doesn't depend on real upstream credentials.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PROJECT_NAME="harness-derisk"

echo "============================================================"
echo " harness Phase 1 de-risk test (Phase 2-aware)"
echo "   verifies: curl -> ollama -> proxy -> mock upstream -> back"
echo "============================================================"

# --- preflight ---------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[derisk] ERROR: docker daemon is not reachable" >&2
    exit 1
fi

# --- temp env file + override -----------------------------------------------

ENV_FILE="$(mktemp -t harness-derisk.XXXXXX.env)"
cat >"${ENV_FILE}" <<'EOF'
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
MOCK_SCENARIO=text
EOF

# Override that publishes ollama's port to the host and adds the mock upstream.
# The base compose file keeps ollama internal-only by default.
OVERRIDE_FILE="$(mktemp -t harness-derisk.XXXXXX.yml)"
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

COMPOSE=(docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" -f docker-compose.yml -f "${OVERRIDE_FILE}")

cleanup() {
    echo "[derisk] cleanup: tearing down compose state"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "${ENV_FILE}" "${OVERRIDE_FILE}"
}
trap cleanup EXIT INT TERM

# Defensive: if a prior run left containers/volumes around, clear them first.
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

# --- bring up ----------------------------------------------------------------

echo "[derisk] building and starting ollama + proxy + mockupstream"
"${COMPOSE[@]}" up -d --build

# --- wait for healthy --------------------------------------------------------

echo "[derisk] waiting up to 120s for all services to become healthy"

is_healthy() {
    local svc="$1"
    local cid
    cid="$("${COMPOSE[@]}" ps -q "${svc}" 2>/dev/null || true)"
    if [[ -z "${cid}" ]]; then
        return 1
    fi
    local status
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")"
    [[ "${status}" == "healthy" ]]
}

deadline=$(( $(date +%s) + 120 ))
while true; do
    if is_healthy mockupstream && is_healthy proxy && is_healthy ollama; then
        echo "[derisk] all three services healthy"
        break
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[derisk] ERROR: services did not become healthy within 120s" >&2
        echo "--- mockupstream logs ---"; "${COMPOSE[@]}" logs mockupstream || true
        echo "--- proxy logs ---";        "${COMPOSE[@]}" logs proxy        || true
        echo "--- ollama logs ---";       "${COMPOSE[@]}" logs ollama       || true
        exit 1
    fi
    sleep 2
done

# --- assertions --------------------------------------------------------------

OLLAMA_URL="http://localhost:11434"

fail() {
    local label="$1"; shift
    echo "[derisk] ASSERTION FAILED: ${label}" >&2
    if [[ $# -gt 0 ]]; then
        echo "[derisk] detail: $*" >&2
    fi
    echo "--- mockupstream logs ---" >&2; "${COMPOSE[@]}" logs mockupstream >&2 || true
    echo "--- proxy logs ---"        >&2; "${COMPOSE[@]}" logs proxy        >&2 || true
    echo "--- ollama logs ---"       >&2; "${COMPOSE[@]}" logs ollama       >&2 || true
    exit 1
}

# Test A — stub model registered.
echo "[derisk] test A: stub model registered"
TAGS_BODY="$(curl -fsS "${OLLAMA_URL}/api/tags")" || fail "A: /api/tags request failed"
echo "[derisk]   /api/tags: ${TAGS_BODY}"
echo "${TAGS_BODY}" | grep -q '"name":"harness' \
    || echo "${TAGS_BODY}" | grep -q '"model":"harness' \
    || fail "A: 'harness' model not found in /api/tags response" "${TAGS_BODY}"

# Test B — stub has remote_host configured.
echo "[derisk] test B: stub has remote_host configured"
SHOW_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/show" -H 'Content-Type: application/json' -d '{"model":"harness"}')" \
    || fail "B: /api/show request failed"
echo "[derisk]   /api/show: ${SHOW_BODY}"
echo "${SHOW_BODY}" | grep -qi 'remote_host' \
    || fail "B: /api/show response did not mention remote_host" "${SHOW_BODY}"
echo "${SHOW_BODY}" | grep -q 'proxy' \
    || fail "B: /api/show did not contain 'proxy' as the remote host" "${SHOW_BODY}"

# Test C — stub has correct context length (200000 from the temp env).
echo "[derisk] test C: stub has context length 200000"
echo "${SHOW_BODY}" | grep -q '200000' \
    || fail "C: /api/show did not include the configured context length 200000" "${SHOW_BODY}"

# Test D — end-to-end forward (non-streaming).
echo "[derisk] test D: end-to-end forward (stream=false)"
CHAT_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d '{"model":"harness","messages":[{"role":"user","content":"test"}],"stream":false}')" \
    || fail "D: /api/chat (stream=false) request failed"
echo "[derisk]   /api/chat (stream=false): ${CHAT_BODY}"
echo "${CHAT_BODY}" | grep -q "Hello from mock upstream" \
    || fail "D: chat response did not contain mock upstream text" "${CHAT_BODY}"

# Test E — proxy actually forwarded to the upstream.
echo "[derisk] test E: mock upstream logged the forwarded request"
MOCK_LOGS="$("${COMPOSE[@]}" logs mockupstream 2>&1 || true)"
echo "${MOCK_LOGS}" | grep -q "mock-upstream" \
    || fail "E: mock upstream logs did not show a forwarded request" "${MOCK_LOGS}"

# Test F — streaming forward.
echo "[derisk] test F: end-to-end forward (stream=true)"
STREAM_BODY="$(curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
    -H 'Content-Type: application/json' \
    -d '{"model":"harness","messages":[{"role":"user","content":"test"}],"stream":true}')" \
    || fail "F: /api/chat (stream=true) request failed"
echo "[derisk]   /api/chat (stream=true): ${STREAM_BODY}"
echo "${STREAM_BODY}" | grep -q "Hello from mock upstream" \
    || fail "F: streaming chat response did not contain mock upstream text" "${STREAM_BODY}"

echo "============================================================"
echo " DERISK TEST PASSED"
echo "============================================================"
exit 0
