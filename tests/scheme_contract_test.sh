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
#   user_bookend — request appears TWICE in last user message: once
#                  before the tool list, once after.
#   user         — legacy: full scaffolding on last user message with
#                  request at the END (after the tool list).
#   system       — full scaffolding lives in the system message; last
#                  user message passes through unchanged.
#   hybrid       — full scaffolding in system message + brief reminder
#                  prefix on last user message.
#
# Per-scheme fixture directories under tests/fixtures/responses/scheme-*/
# hold the canned upstream responses (text-only + tool-call) that the
# mock returns for each scheme variant.
#
# References: P010, P013, P014, P015, P016, P017, P018 (INVENTORY.md
# under track/B-inventory).

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
PROXY_API_MODEL=test-model
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_AGENT_MODEL=harness
OLLAMA_CONTEXT_LENGTH=200000
MOCK_SCENARIO=text
MOCK_FIXTURES_DIR=/fixtures
PROXY_PROMPT_MODE=${scheme}
SCHEME_FIXTURES_SUBDIR=scheme-${scheme}
EOF
}

# The override file bind-mounts the scheme-specific fixture subdir into
# the mock container at /fixtures and exposes a host port on ollama so
# the test driver can curl it.
cat >"${OVERRIDE_FILE}" <<'EOF'
services:
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

SCHEMES=("user_front" "user_bookend" "user" "system" "hybrid")

for scheme in "${SCHEMES[@]}"; do
    test_section "scheme contract: ${scheme}"
    restart_with_scheme "${scheme}"

    # ---- text-response probe ----
    echo "[scheme-test]   sending text probe"
    text_resp=$(send_probe_request "scheme-probe-text") \
        || fail "${scheme}/text: /api/chat failed"

    case "${scheme}" in
        user_front)  expected_content="scheme-user_front fixture" ;;
        user_bookend) expected_content="scheme-user_bookend fixture" ;;
        user)        expected_content="scheme-user fixture" ;;
        system)      expected_content="scheme-system fixture" ;;
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
            # PROXY_CHANGE_SYSTEM_PROMPT_TO_USER default is on, but the
            # incoming request has NO system message, so no conversion
            # fires; just verify keys + last-user-message structure).
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

        user_bookend)
            # P014: request appears twice in last user message, both
            # wrapped in markers; tool list appears between them.
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
msgs = body["messages"]
last = msgs[-1]
if last["role"] != "user":
    print("LAST_NOT_USER:" + last["role"]); sys.exit(0)
c = last["content"]
begin_n = c.count("<<<BEGIN_USER_REQUEST>>>")
end_n = c.count("<<<END_USER_REQUEST>>>")
if begin_n != 2:
    print(f"BEGIN_MARKER_COUNT:{begin_n}"); sys.exit(0)
if end_n != 2:
    print(f"END_MARKER_COUNT:{end_n}"); sys.exit(0)
probe_n = c.count("scheme-probe-text")
if probe_n != 2:
    print(f"PROBE_COUNT:{probe_n}"); sys.exit(0)
if "### Available Tools" not in c:
    print("NO_TOOL_LIST"); sys.exit(0)
# Tool list should sit between the two request occurrences.
first_req = c.index("<<<BEGIN_USER_REQUEST>>>")
tool_pos = c.index("Available Tools")
last_req = c.rindex("<<<BEGIN_USER_REQUEST>>>")
if not (first_req < tool_pos < last_req):
    print("TOOLS_NOT_BETWEEN_REQUESTS"); sys.exit(0)
print("OK request-bookended-around-tools")
'
            ;;

        user)
            # P015: legacy mode. Full scaffolding (including marker-
            # wrapped request) on last user message; request appears
            # AFTER the tool list, not before.
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
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
if "### Tool Usage Instructions" not in c:
    print("NO_INSTRUCTIONS_HEADER"); sys.exit(0)
# Legacy `user` mode puts the request AFTER the tool list.
req_pos = c.index("<<<BEGIN_USER_REQUEST>>>")
tool_pos = c.index("Available Tools")
if req_pos <= tool_pos:
    print("REQUEST_NOT_AFTER_TOOLS"); sys.exit(0)
if c.count("scheme-probe-text") != 1:
    print(f"PROBE_COUNT:{c.count(chr(34) + chr(34))}"); sys.exit(0)
print("OK request-after-tools")
'
            ;;

        system)
            # P016: scaffolding appended to system message. The request
            # is sent with NO system message, but the proxy inserts one
            # in that case. Last user message stays unchanged (just the
            # probe text). PROXY_CHANGE_SYSTEM_PROMPT_TO_USER defaults
            # to on, so the inserted system message gets converted to
            # a user role with a stub assistant turn after it. Assert
            # that the scaffolding lives at the HEAD of the conversation
            # (not on the last user message) and the last user message
            # is the unmodified probe.
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
msgs = body["messages"]
# Find where the tool scaffolding lives. It MUST NOT be on the last
# user message (that distinguishes system mode from user/user_front).
last = msgs[-1]
if last["role"] != "user":
    print("LAST_NOT_USER:" + last["role"]); sys.exit(0)
last_c = last["content"]
if "### Available Tools" in last_c:
    print("TOOL_LIST_ON_LAST_USER"); sys.exit(0)
if "<<<BEGIN_USER_REQUEST>>>" in last_c:
    print("REQUEST_MARKER_ON_LAST_USER"); sys.exit(0)
if "scheme-probe-text" not in last_c:
    print("PROBE_MISSING_FROM_LAST_USER"); sys.exit(0)
# Scaffolding must live in some earlier message (the converted
# system, or — if PROXY_CHANGE_SYSTEM_PROMPT_TO_USER were off — the
# system message itself). Look anywhere except the last message.
head_content = "\n".join(m.get("content", "") for m in msgs[:-1])
if "### Tool Usage Instructions" not in head_content:
    print("NO_INSTRUCTIONS_HEADER_IN_HEAD"); sys.exit(0)
if "### Available Tools" not in head_content:
    print("NO_TOOL_LIST_IN_HEAD"); sys.exit(0)
print("OK scaffolding-in-head last-user-pristine")
'
            ;;

        hybrid)
            # P017: full scaffolding in head (same as `system`) + a
            # brief "Tool reminder" prefix on the last user message.
            # The last user message must contain BOTH the reminder
            # AND the original probe text, but NOT the full tool list
            # or instruction header.
            assert_forwarded "${scheme}/text/forwarded" "${forwarded}" '
import json, sys
body = json.loads(sys.stdin.read())
msgs = body["messages"]
last = msgs[-1]
if last["role"] != "user":
    print("LAST_NOT_USER:" + last["role"]); sys.exit(0)
c = last["content"]
if "Tool reminder" not in c:
    print("NO_TOOL_REMINDER"); sys.exit(0)
if "system prompt" not in c.lower():
    print("REMINDER_DOES_NOT_REFERENCE_SYSTEM_PROMPT"); sys.exit(0)
if "scheme-probe-text" not in c:
    print("PROBE_MISSING_FROM_LAST_USER"); sys.exit(0)
# Full tool list / instructions header MUST NOT be on the last user
# message — those go in the head (system) under hybrid.
if "### Available Tools" in c:
    print("FULL_TOOL_LIST_ON_LAST_USER"); sys.exit(0)
if "### Tool Usage Instructions" in c:
    print("FULL_INSTRUCTIONS_ON_LAST_USER"); sys.exit(0)
# Verify scaffolding lives in the head.
head_content = "\n".join(m.get("content", "") for m in msgs[:-1])
if "### Tool Usage Instructions" not in head_content:
    print("NO_INSTRUCTIONS_HEADER_IN_HEAD"); sys.exit(0)
if "### Available Tools" not in head_content:
    print("NO_TOOL_LIST_IN_HEAD"); sys.exit(0)
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
