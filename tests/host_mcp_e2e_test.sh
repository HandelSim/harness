#!/usr/bin/env bash
#
# tests/host_mcp_e2e_test.sh — prove the HOST MCP workflow executes end-to-end.
#
# A "host MCP" runs as a process ON THE HOST (not a container); the
# containerized agent reaches it at http://host.docker.internal:<port>/mcp (see
# architecture/mcp.md "Host MCPs"). No other test proves a host MCP tool
# actually *executes and returns*: full_pipeline_test.sh T16 only checks the
# install/start/uninstall lifecycle against a fake `python -m http.server`, and
# integration_test.sh Phase 2.6 only greps proxy dumps for keyword evidence.
#
# This test stands up a REAL minimal host MCP exposing ONE tool (`stamp`) that
# writes a magic token to a host file. It drives `harness -p` (print mode)
# against a MOCK upstream whose fixture, on turn 1, emits an MCP tool call
# routed to the host MCP, and on turn 2 emits a final confirmation. The proof
# is the side effect: the token file — written only by the host process — must
# appear with the magic token. That can only happen if the agent reached the
# host MCP through host.docker.internal (the firewall + --add-host wiring in
# emit_host_mcp_docker_args / init-firewall.sh section 9b) AND opencode routed
# the model's tool call to the remote MCP and fed the result back.
#
# Slow: builds the agent+proxy images and installs the MCP SDK into a host
# venv (needs docker + host PyPI egress). Gated behind HARNESS_RUN_SLOW=1, so
# the default suite skips it. CI's slow matrix runs it.
#
#   HARNESS_RUN_SLOW=1 bash tests/host_mcp_e2e_test.sh
#
# !!! TOOL-NAME ASSUMPTION (verify on first failure) !!!
# The fixture emits a tool call named "${MCP_NAME}_stamp". The proxy passes the
# fixture's `name` through verbatim as tool_calls[].function.name
# (proxy/proxy.py extract_tool_calls_and_text), and opencode registers an MCP
# server's tools as <server>_<tool> — NOT the mcp__<server>__<tool> form the
# RESPONSE fixtures use, which is only ever substring-matched, never executed
# (see proxy/proxy.py extract_tool_calls_and_text and architecture/proxy.md;
# line numbers omitted deliberately so this note does not drift).
# If Phase 6 fails, inspect state/output/ for the real advertised tool name and
# update MCP_TOOL_FQN.

set -euo pipefail

# --- gate (idiom from integration_test.sh:30) -------------------------------
if [[ "${HARNESS_RUN_SLOW:-0}" != "1" ]]; then
    echo "host_mcp_e2e_test.sh: skipped (set HARNESS_RUN_SLOW=1 to run)"
    echo "  Stands up a real host MCP process + agent stack; needs docker and host pip egress."
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export REPO_ROOT  # test_helpers.sh expects this in scope.

# shellcheck source=lib/test_helpers.sh
source "${REPO_ROOT}/tests/lib/test_helpers.sh"   # pulls in harness_docker et al.

fail() { echo "[host-mcp-e2e] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-mcp-e2e] OK: $*"; }

require_docker
command -v python3 >/dev/null 2>&1 || fail "python3 required on the host to run the host MCP"

# --- identity / staging -----------------------------------------------------
PROJECT_NAME="harness-hostmcp-e2e"
NETWORK="${PROJECT_NAME}_harness-net"
MOCK_NAME="${PROJECT_NAME}-mockupstream-1"
MCP_NAME="hoststamp"                          # lowercase, no separators (opencode joins <server>_<tool>)
MCP_PORT="$(( 19000 + (RANDOM % 4000) ))"
MCP_TOOL_FQN="${MCP_NAME}_stamp"              # see TOOL-NAME ASSUMPTION above
MAGIC_TOKEN="HOSTMCP_OK_${RANDOM}${RANDOM}"

TEST_ROOT="$(mktemp -d -t harness-hostmcp-root.XXXXXX)"
FAKE_HOME="$(mktemp -d -t harness-hostmcp-home.XXXXXX)"
TEST_INSTALL="${TEST_ROOT}/harness"
TEST_WORKSPACE="${TEST_ROOT}/workspace"
TOKEN_FILE="${TEST_ROOT}/stamp.txt"           # host path; written by the HOST MCP process
HOST_MCP_PID=""

cleanup() {
    local rc=$?
    echo "[host-mcp-e2e] cleanup (rc=${rc})"
    if [[ -n "${HOST_MCP_PID}" ]]; then
        kill "${HOST_MCP_PID}" >/dev/null 2>&1 || true
        wait "${HOST_MCP_PID}" 2>/dev/null || true
    fi
    harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true
    if [[ -x "${TEST_INSTALL}/harness" ]]; then
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${TEST_INSTALL}/harness" mcp uninstall "${MCP_NAME}" --force >/dev/null 2>&1 || true
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${TEST_INSTALL}/harness" down >/dev/null 2>&1 || true
    fi
    harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
    harness_docker network rm "${NETWORK}" >/dev/null 2>&1 || true
    if [[ "${HARNESS_HOSTMCP_KEEP:-0}" == "1" ]]; then
        echo "[host-mcp-e2e] KEEP=1: artifacts under ${TEST_ROOT}"
    else
        for d in "${TEST_ROOT}" "${FAKE_HOME}"; do
            [[ -d "$d" ]] || continue
            rm -rf "$d" 2>/dev/null || \
                harness_docker run --rm -v "$d:/t" --user 0:0 alpine \
                    sh -c 'rm -rf /t/* /t/.[!.]* 2>/dev/null || true' >/dev/null 2>&1 || true
            rm -rf "$d" 2>/dev/null || true
        done
    fi
    exit "${rc}"
}
trap cleanup EXIT INT TERM

# Defensive: clear stale state from a prior aborted run.
harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true
harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true

# === Phase 0: install layout (mirrors integration_test.sh) ==================
echo "[host-mcp-e2e] Phase 0: stage install at ${TEST_INSTALL}"
mkdir -p "${TEST_INSTALL}" "${TEST_WORKSPACE}/proj"
# Copy the working tree (so local edits are exercised), excluding heavy/transient dirs.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' \
        --exclude='.env' --exclude='.harness-allowlist' --exclude='state/' \
        "${REPO_ROOT}/" "${TEST_INSTALL}/" >/dev/null
else
    tar -C "${REPO_ROOT}" --exclude='./.git' --exclude='./__pycache__' --exclude='*.pyc' \
        --exclude='./.env' --exclude='./.harness-allowlist' --exclude='./state' \
        -cf - . | tar -C "${TEST_INSTALL}" -xf -
fi
mkdir -p "${TEST_INSTALL}/state/output" "${TEST_INSTALL}/state/agent/home" "${TEST_INSTALL}/state/mcp"
echo "host MCP e2e workspace" > "${TEST_WORKSPACE}/proj/README.md"

test_generate_env "${TEST_INSTALL}/.env" \
    "PROXY_API_URL=http://mockupstream:9000/v1/chat/completions" \
    "PROXY_API_KEY=test-key-1234" \
    "DEFAULT_MODEL_NAME=harness"
test_generate_allowlist "${TEST_INSTALL}/.harness-allowlist" api.anthropic.com

harness_call() {
    HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
        "${TEST_INSTALL}/harness" "$@"
}

# === Phase 1: register the host MCP (host-init) =============================
echo "[host-mcp-e2e] Phase 1: harness mcp host-init ${MCP_NAME} --port ${MCP_PORT}"
harness_call mcp host-init "${MCP_NAME}" --port "${MCP_PORT}" \
    >"${TEST_ROOT}/host-init.log" 2>&1 \
    || fail "host-init exited non-zero — $(cat "${TEST_ROOT}/host-init.log")"
REG="${TEST_INSTALL}/state/mcp/${MCP_NAME}"
[[ -f "${REG}/client-config.json" ]] || fail "registration client-config.json missing"
grep -q "host.docker.internal:${MCP_PORT}/mcp" "${REG}/client-config.json" \
    || fail "client-config missing the host.docker.internal URL"
[[ ! -f "${REG}/compose.yml" ]] || fail "host MCP must not have a compose.yml"
[[ ! -e "${TEST_INSTALL}/host-mcp/${MCP_NAME}/run.ps1" ]] || fail "PowerShell launcher must be gone"
[[ -f "${TEST_INSTALL}/host-mcp/${MCP_NAME}/run.sh" ]] || fail "run.sh launcher missing"
ok "host MCP registered (client-config-only, run.sh launcher, no PowerShell)"

# === Phase 2: replace the scaffold server with a minimal one-tool MCP =======
INST="${TEST_INSTALL}/host-mcp/${MCP_NAME}"
cat > "${INST}/server.py" <<PYEOF
#!/usr/bin/env python3
"""Minimal host MCP for the e2e test: one tool, 'stamp', writes a token file."""
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

NAME = "${MCP_NAME}"
HOST = os.environ.get("HOST_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("HOST_MCP_PORT", "${MCP_PORT}"))

mcp = FastMCP(NAME, host=HOST, port=PORT)

@mcp.tool()
def stamp(path: str, token: str) -> str:
    """Write a token to a file on the host and return a confirmation.

    path: absolute file path to write. token: the string to write.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    print(f"[host-mcp:{NAME}] stamped {token} -> {path}", flush=True)
    return f"stamped {token} to {path}"

if __name__ == "__main__":
    print(f"[host-mcp:{NAME}] serving http://{HOST}:{PORT}/mcp", flush=True)
    mcp.run(transport="streamable-http")
PYEOF
ok "minimal one-tool server.py written"

# === Phase 3: start the REAL host MCP process on the host ===================
echo "[host-mcp-e2e] Phase 3: launch host MCP on 0.0.0.0:${MCP_PORT} (./run.sh)"
( cd "${INST}" && HOST_MCP_PORT="${MCP_PORT}" ./run.sh ) >"${TEST_ROOT}/host-mcp.log" 2>&1 &
HOST_MCP_PID=$!
mcp_up=0
for _ in $(seq 1 60); do
    if ! kill -0 "${HOST_MCP_PID}" 2>/dev/null; then
        fail "host MCP process died during startup — $(tail -40 "${TEST_ROOT}/host-mcp.log")"
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/${MCP_PORT}") 2>/dev/null; then
        exec 3>&- 3<&- 2>/dev/null || true
        mcp_up=1; break
    fi
    sleep 1
done
(( mcp_up == 1 )) || fail "host MCP did not open port ${MCP_PORT} in 60s — $(tail -40 "${TEST_ROOT}/host-mcp.log")"
ok "host MCP process is listening on ${MCP_PORT}"

# === Phase 4: bring up the stack + mock upstream (private fixtures) =========
FIX_DIR="${TEST_ROOT}/fixtures"
mkdir -p "${FIX_DIR}"
# Turn 1: emit the host MCP tool call. Turn 2 (after the tool result): final text.
# match_counter cycles the two responses; the prompt carries the trigger phrase.
cat > "${FIX_DIR}/10_host_stamp.json" <<JSONEOF
{
  "name": "host MCP stamp (two-turn)",
  "match": "stamp the token",
  "match_counter": true,
  "responses": [
    { "choices": [ { "message": { "role": "assistant",
      "content": "I'll call the host MCP to stamp the token.\n\n\`\`\`json\n{\"name\":\"${MCP_TOOL_FQN}\",\"arguments\":{\"path\":\"${TOKEN_FILE}\",\"token\":\"${MAGIC_TOKEN}\"}}\n\`\`\`\n" } } ],
      "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80} },
    { "choices": [ { "message": { "role": "assistant",
      "content": "Done. The host MCP wrote the token ${MAGIC_TOKEN}." } } ],
      "usage": {"prompt_tokens": 60, "completion_tokens": 12, "total_tokens": 72} }
  ]
}
JSONEOF
cat > "${FIX_DIR}/99_default.json" <<'JSONEOF'
{ "name": "default", "match": "", "response": {
  "choices": [ { "message": { "role": "assistant", "content": "ok" } } ],
  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2} } }
JSONEOF

echo "[host-mcp-e2e] Phase 4: harness start (builds agent+proxy) + mock upstream"
harness_call start >"${TEST_ROOT}/start.log" 2>&1 || {
    echo "--- start.log (tail) ---" >&2; tail -80 "${TEST_ROOT}/start.log" >&2
    fail "harness start failed"
}

# Mock sidecar with our PRIVATE fixtures dir (mirrors test_start_mockupstream).
mock_py_host=$(harness_docker_path "${REPO_ROOT}/tests/mock_upstream.py")
fix_host=$(harness_docker_path "${FIX_DIR}")
harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true
harness_docker run -d \
    --name "${MOCK_NAME}" \
    --network "${NETWORK}" \
    --network-alias mockupstream \
    -e "MOCK_FIXTURES_DIR=/fixtures" \
    -v "${mock_py_host}:/app/mock_upstream.py:ro" \
    -v "${fix_host}:/fixtures:ro" \
    --health-cmd "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=2).status==200 else 1)\"" \
    --health-interval 5s --health-timeout 3s --health-retries 12 --health-start-period 20s \
    python:3.12-slim \
    sh -c "pip install --quiet --no-cache-dir flask==3.0.3 && python /app/mock_upstream.py" \
    >/dev/null

test_wait_for_healthy "${PROJECT_NAME}" proxy 90
test_wait_for_container_healthy "${MOCK_NAME}" 90
# Reset turn counters so our run starts at response[0].
harness_docker exec "${MOCK_NAME}" \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/__reset_counters__', data=b'')" \
    >/dev/null 2>&1 || true
ok "stack + mock upstream healthy"

# === Phase 5: drive the agent; it must call the host MCP tool ===============
echo "[host-mcp-e2e] Phase 5: harness -p (agent calls the host MCP tool)"
rm -f "${TOKEN_FILE}"
set +e
out=$(cd "${TEST_WORKSPACE}/proj" && timeout 180 \
    env HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
    "${TEST_INSTALL}/harness" -p \
    "Use the ${MCP_NAME} MCP to stamp the token into the shared file." \
    2>&1 < /dev/null)
rc=$?
set -e
# Tolerate opencode's interactive provider-auth requirement (integration_test.sh
# pattern): if that's the failure mode, we cannot prove execution — skip.
if (( rc != 0 )) && grep -qiE 'auth|login|provider .* not (configured|found)|no .* api key' <<<"${out}"; then
    echo "[host-mcp-e2e] SKIPPED: opencode run requires interactive provider auth"; exit 0
fi
(( rc == 0 )) || {
    echo "--- harness -p output (tail) ---" >&2; tail -60 <<<"${out}" >&2
    echo "--- host mcp log ---" >&2; tail -40 "${TEST_ROOT}/host-mcp.log" >&2 || true
    harness_docker logs "${MOCK_NAME}" 2>&1 | tail -40 >&2 || true
    fail "harness -p exited ${rc}"
}
ok "harness -p completed"

# === Phase 6: PROVE the host MCP tool executed =============================
[[ -f "${TOKEN_FILE}" ]] || {
    echo "--- mock dispatch log (which fixtures fired) ---" >&2
    harness_docker logs "${MOCK_NAME}" 2>&1 | grep -i dispatch >&2 || true
    echo "--- host mcp log ---" >&2; tail -40 "${TEST_ROOT}/host-mcp.log" >&2 || true
    echo "--- proxy dumps (to find the real advertised tool name) ---" >&2
    grep -rl "stamp" "${TEST_INSTALL}/state/output/" 2>/dev/null | head >&2 || true
    fail "token file was NOT created — host MCP tool did not execute (see TOOL-NAME ASSUMPTION)"
}
got="$(cat "${TOKEN_FILE}")"
[[ "${got}" == "${MAGIC_TOKEN}" ]] || fail "token mismatch: got '${got}', want '${MAGIC_TOKEN}'"
ok "host MCP tool executed: token '${MAGIC_TOKEN}' written by the host process"
grep -q "${MAGIC_TOKEN}" "${TEST_ROOT}/host-mcp.log" 2>/dev/null \
    && ok "host MCP process log also recorded the stamp" || true

echo
echo "============================================================"
echo " HOST MCP E2E TEST PASSED"
echo "============================================================"
