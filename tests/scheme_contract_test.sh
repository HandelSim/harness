#!/usr/bin/env bash
#
# scheme_contract_test.sh — per-scheme proxy contract assertions.
#
# Brings up ollama + the real proxy + a mock upstream, then for each
# supported value of PROXY_PROMPT_MODE drives a request through ollama
# and inspects what the proxy forwarded upstream. The mock_upstream
# server logs every request body to stdout; that's our payload capture.
#
# Schemes covered:
#   user_front   — request first (in <<<BEGIN_USER_REQUEST>>> markers),
#                  then tool list, in last user message.
#   hybrid       — full scaffolding at stable prefix (head of conversation
#                  after the _CHANGE_SYSTEM_TO_USER post-pass) + a
#                  "[Reminder — …]" prefix listing available tool
#                  names prepended to the last user message.
#
# Per-scheme fixture directories under tests/fixtures/responses/scheme-*/
# hold the canned upstream responses (text-only + tool-call) that the
# mock returns for each scheme variant.
#
# References: P010, P013, P017, P018 (INVENTORY.md under track/B-inventory).
# The user / system / user_bookend modes were removed in the hybrid
# consolidation refactor (issue #64).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/test_helpers.sh"

PROJECT_NAME="harness-scheme-contracts"

echo "============================================================"
echo " per-scheme proxy contract test"
echo "============================================================"

require_docker

# --- temp env + override -----------------------------------------------------

ENV_FILE="$(mktemp -t harness-scheme.XXXXXX.env)"
OVERRIDE_FILE="$(mktemp -t harness-scheme.XXXXXX.yml)"
ALLOWLIST_FILE="$(mktemp -t harness-scheme.XXXXXX.allow)"
test_generate_allowlist "${ALLOWLIST_FILE}"
export HARNESS_ALLOWLIST_PATH="${ALLOWLIST_FILE}"

# Initial env: user_front mode + scheme-user_front fixtures. We'll
# rewrite this file and force-recreate the proxy + mockupstream
# between schemes.
write_scheme_env() {
    local scheme="$1"
    cat >"${ENV_FILE}" <<EOF
PROXY_API_URL=http://mockupstream:9000/v1/chat/completions
PROXY_API_KEY=test-key-1234
DEFAULT_MODEL_NAME=harness
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_CONTEXT_LENGTH=200000
MOCK_SCENARIO=text
MOCK_FIXTURES_DIR=/fixtures
PROXY_PROMPT_MODE=${scheme}
SCHEME_FIXTURES_SUBDIR=scheme-${scheme}
EOF
}

# The override file bind-mounts the scheme-specific fixture subdir into
# the mock container at /fixtures and exposes a host port on ollama so
# the test driver can curl it. It also re-injects PROXY_PROMPT_MODE onto
# the proxy: production docker-compose.yml no longer interpolates that var
# (removed so a stale user .env can't override the hybrid default), so this
# test supplies it through its own override, interpolated from the per-scheme
# value write_scheme_env writes into ENV_FILE.
cat >"${OVERRIDE_FILE}" <<'EOF'
services:
  proxy:
    environment:
      PROXY_PROMPT_MODE: ${PROXY_PROMPT_MODE}
  ollama:
    ports:
      - "11434:11434"
  mockupstream:
    image: python:3.12-slim
    working_dir: /app
    environment:
      MOCK_FIXTURES_DIR: /fixtures
    volumes:
      - ./tests/mock_upstream.py:/app/mock_upstream.py:ro
      - ./tests/fixtures/responses/${SCHEME_FIXTURES_SUBDIR}:/fixtures:ro
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

write_scheme_env "user_front"

COMPOSE=(harness_docker compose --project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}" -f docker-compose.yml -f "${OVERRIDE_FILE}")

cleanup() {
    echo "[scheme-test] cleanup: tearing down compose state"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "${ENV_FILE}" "${OVERRIDE_FILE}" "${ALLOWLIST_FILE}"
}
trap cleanup EXIT INT TERM

# Defensive: clear any stale state from a prior run.
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

# --- compose sanity ----------------------------------------------------------

echo "[scheme-test] validating compose config"
"${COMPOSE[@]}" config >/dev/null

# --- initial bring-up --------------------------------------------------------

echo "[scheme-test] building and starting initial stack (proxy_mode=user_front)"
"${COMPOSE[@]}" up -d --build

if ! test_wait_for_healthy "${PROJECT_NAME}" mockupstream proxy ollama 120; then
    echo "[scheme-test] ERROR: services did not become healthy" >&2
    "${COMPOSE[@]}" ps >&2 || true
    "${COMPOSE[@]}" logs mockupstream >&2 || true
    "${COMPOSE[@]}" logs proxy >&2 || true
    "${COMPOSE[@]}" logs ollama >&2 || true
    exit 1
fi
echo "[scheme-test] initial stack healthy"

# --- helpers -----------------------------------------------------------------

OLLAMA_URL="http://localhost:11434"

fail() {
    local label="$1"; shift
    echo "[scheme-test] FAIL: ${label}" >&2
    if [[ $# -gt 0 ]]; then echo "[scheme-test] detail: $*" >&2; fi
    echo "--- mockupstream logs (tail) ---" >&2
    "${COMPOSE[@]}" logs --tail=100 mockupstream >&2 || true
    echo "--- proxy logs (tail) ---" >&2
    "${COMPOSE[@]}" logs --tail=50 proxy >&2 || true
    exit 1
}

# Restart proxy + mockupstream with a new scheme. PROXY_PROMPT_MODE is
# only read at proxy startup, so we must force-recreate it. We restart
# mockupstream too so its fixture dispatch points at the scheme-specific
# directory and its per-fixture counters / log history are fresh.
restart_with_scheme() {
    local scheme="$1"
    write_scheme_env "${scheme}"
    echo "[scheme-test] restarting proxy+mockupstream with PROXY_PROMPT_MODE=${scheme}"
    # The two services don't depend on each other; recreate them
    # together so a single healthcheck wait covers both.
    "${COMPOSE[@]}" up -d --force-recreate --no-deps proxy mockupstream >/dev/null
    if ! test_wait_for_healthy "${PROJECT_NAME}" mockupstream proxy 60; then
        fail "services did not become healthy after switching to scheme '${scheme}'"
    fi
}

# Send a /api/chat request through ollama with a tools array; returns
# the ollama response body. The user content is deliberately a fixed
# probe string so the scheme-specific fixture matches it.
send_probe_request() {
    local probe_label="$1"
    local body
    body=$(cat <<EOF
{
  "model": "harness",
  "messages": [{"role":"user","content":"${probe_label}"}],
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
}
EOF
)
    curl -fsS -X POST "${OLLAMA_URL}/api/chat" \
        -H 'Content-Type: application/json' \
        -d "${body}"
}

# Capture the most recent forwarded body from mockupstream logs and
# return it on stdout. The mock logs every request as
# `[mock-upstream] POST /v1/chat/completions body=<json>`; we grep for
# that and pull the body= prefix off.
capture_forwarded_body() {
    local logs last
    logs="$("${COMPOSE[@]}" logs --tail=500 mockupstream 2>&1 || true)"
    last="$(echo "${logs}" | grep -E '\[mock-upstream\] (POST|PUT) /v1/chat/completions ' | tail -1)"
    if [[ -z "${last}" ]]; then
        echo "__NO_FORWARDED_BODY__"
        return 1
    fi
    # Trim everything up to and including `body=`.
    echo "${last#*body=}"
}

# Run a python check inside the proxy container against the forwarded
# body. The check script is passed on stdin; the body is piped through
# its stdin too (via the heredoc). Echoes 'OK' on success, anything
# else on failure.
assert_forwarded() {
    local label="$1"
    local body="$2"
    local check_script="$3"
    local out
    out="$("${COMPOSE[@]}" exec -T proxy python -c "${check_script}" <<<"${body}")" \
        || fail "${label}: python check exited non-zero" "${body}"
    case "${out}" in
        OK*) echo "[scheme-test]   ${label} ${out}" ;;
        *) fail "${label}: ${out}" "${body}" ;;
    esac
}

# --- per-scheme test loop ----------------------------------------------------

SCHEMES=("user_front" "hybrid")

for scheme in "${SCHEMES[@]}"; do
    test_section "scheme contract: ${scheme}"
    restart_with_scheme "${scheme}"

    # ---- text-response probe ----
    echo "[scheme-test]   sending text probe"
    text_resp=$(send_probe_request "scheme-probe-text") \
        || fail "${scheme}/text: /api/chat failed"

    case "${scheme}" in
        user_front)  expected_content="scheme-user_front fixture" ;;
        hybrid)      expected_content="scheme-hybrid fixture" ;;
    esac
    echo "${text_resp}" | grep -q "${expected_content}" \
        || fail "${scheme}/text: response did not contain '${expected_content}'" "${text_resp}"

    forwarded=$(capture_forwarded_body) \
        || fail "${scheme}/text: no forwarded body in mockupstream logs"

    # Per-scheme structural assertions on the forwarded body.
    case "${scheme}" in
        user_front)
            # P013: request appears in last user message wrapped in
            # markers, BEFORE the tool list. System role intact (the
            # system→user conversion is always on, but the incoming request
            # has NO system message, so no conversion fires; just verify
            # keys + last-user-message structure).
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
keys = sorted(body.keys())
if keys != ["messages", "model"]:
    print("UNEXPECTED_KEYS:" + ",".join(keys)); sys.exit(0)
msgs = body["messages"]
last = msgs[-1]
if last["role"] != "user":
    print("LAST_NOT_USER:" + last["role"]); sys.exit(0)
c = last["content"]
if "<<<BEGIN_USER_REQUEST>>>" not in c:
    print("NO_BEGIN_MARKER"); sys.exit(0)
if "<<<END_USER_REQUEST>>>" not in c:
    print("NO_END_MARKER"); sys.exit(0)
if "### Available Tools" not in c:
    print("NO_TOOL_LIST"); sys.exit(0)
if c.count("scheme-probe-text") != 1:
    print(f"REQUEST_COUNT:{c.count(chr(34) + chr(34))}"); sys.exit(0)
req_pos = c.index("<<<BEGIN_USER_REQUEST>>>")
tool_pos = c.index("Available Tools")
if req_pos >= tool_pos:
    print("REQUEST_NOT_BEFORE_TOOLS"); sys.exit(0)
print("OK request-before-tools")
'
            ;;

        hybrid)
            # P017: full scaffolding at the stable prefix (head of
            # conversation after _CHANGE_SYSTEM_TO_USER) + a
            # "[Reminder — …]"
            # block on the last user message that carries the per-tool
            # legend ("Tools — one entry per tool …") and the "do not
            # invent" sentence. The last user message must contain the
            # live probe (wrapped in <<<BEGIN_USER_REQUEST>>> markers,
            # placed FIRST) AND the reminder (placed AFTER the wrap),
            # but NOT the full tool list or instruction header.
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
msgs = body["messages"]
last = msgs[-1]
if last["role"] != "user":
    print("LAST_NOT_USER:" + last["role"]); sys.exit(0)
c = last["content"]
if "Reminder" not in c:
    print("NO_REMINDER_PREFIX"); sys.exit(0)
if "do not invent" not in c:
    print("REMINDER_MISSING_DO_NOT_INVENT"); sys.exit(0)
if "Tools — one entry per tool" not in c:
    print("REMINDER_MISSING_TOOLS_LEGEND"); sys.exit(0)
# The reminder points the model back at the AGENT_TOOLS section by name.
if "<<<BEGIN_AGENT_TOOLS>>>" not in c:
    print("REMINDER_MISSING_AGENT_TOOLS_POINTER"); sys.exit(0)
if "scheme-probe-text" not in c:
    print("PROBE_MISSING_FROM_LAST_USER"); sys.exit(0)
# The probe (live user request) is wrapped in USER_REQUEST markers and
# placed FIRST under hybrid; the reminder sits AFTER the wrap so the
# model'\''s most-recent attention lands on the question, not the rules.
if "<<<BEGIN_USER_REQUEST>>>" not in c:
    print("NO_USER_REQUEST_WRAP"); sys.exit(0)
if c.index("Reminder") <= c.index("<<<END_USER_REQUEST>>>"):
    print("REMINDER_NOT_AFTER_USER_REQUEST"); sys.exit(0)
# The full tool list / instructions header MUST NOT be on the last user
# message — those go in the head (system) under hybrid. (The reminder
# only *references* the AGENT_TOOLS marker; the block itself is in head.)
if "<<<END_AGENT_TOOLS>>>" in c:
    print("FULL_TOOL_LIST_ON_LAST_USER"); sys.exit(0)
if "### Tool Usage Instructions" in c:
    print("FULL_INSTRUCTIONS_ON_LAST_USER"); sys.exit(0)
# Verify scaffolding lives in the head.
head_content = "\n".join(m.get("content", "") for m in msgs[:-1])
if "### Tool Usage Instructions" not in head_content:
    print("NO_INSTRUCTIONS_HEADER_IN_HEAD"); sys.exit(0)
if "<<<BEGIN_AGENT_TOOLS>>>" not in head_content:
    print("NO_AGENT_TOOLS_IN_HEAD"); sys.exit(0)
print("OK hybrid reminder+head-scaffolding")
'
            ;;
    esac

    # ---- tool-call probe (cross-scheme: verifies the proxy still
    # parses ```json tool-call blocks out of the upstream response
    # under each mode). The scheme-specific fixture for the "tool"
    # probe returns a markdown-fenced JSON tool call.
    echo "[scheme-test]   sending tool-call probe"
    tool_resp=$(send_probe_request "scheme-probe-tool") \
        || fail "${scheme}/tool: /api/chat failed"

    echo "${tool_resp}" | grep -q '"tool_calls"' \
        || fail "${scheme}/tool: response had no tool_calls field" "${tool_resp}"
    echo "${tool_resp}" | grep -q '"name":"get_weather"' \
        || fail "${scheme}/tool: tool_calls did not contain get_weather" "${tool_resp}"
    echo "${tool_resp}" | grep -q '"done_reason":"tool_calls"' \
        || fail "${scheme}/tool: done_reason was not tool_calls" "${tool_resp}"
    echo "[scheme-test]   ${scheme}/tool OK (tool_calls parsed)"
done

echo "============================================================"
echo " PER-SCHEME PROXY CONTRACT TEST PASSED"
echo "============================================================"
exit 0
