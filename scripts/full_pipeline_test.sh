#!/usr/bin/env bash
#
# scripts/full_pipeline_test.sh — full installation-to-running pipeline test.
#
# This is the most comprehensive automated test we ship. It:
#
#   1. builds dist/harness-distribution.zip from the current tree,
#   2. extracts it into a clean tmpdir as a "fresh install",
#   3. runs harness-install.sh non-interactively (cloning the local repo,
#      not GitHub — via HARNESS_REPO_URL),
#   4. exercises every major harness subcommand with a mock upstream,
#   5. drives a real tmux-wrapped agent session via send-keys / capture-pane,
#   6. tears everything down on exit.
#
# What this test does NOT cover (covered instead by MANUAL_TEST_PROMPT.md):
#
#   - Real LLM responses (we only have a canned mock-upstream reply).
#   - Tool-call driven file creation. The mock returns plain text; an agent
#     prompted to "create a file" will not actually create one because the
#     mock doesn't emit tool-call JSON. File-ownership semantics are still
#     tested elsewhere (proxy_test.sh); end-to-end "agent creates a file
#     with correct host UID" is a manual scenario.
#   - Subjective UX (TUI quality, latency, error message wording).
#
# Project name is fixed to harness-pipeline-test so this never collides with
# a real harness instance running on the same daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib/tui_driver.sh
source "${SCRIPT_DIR}/lib/tui_driver.sh"

PROJECT_NAME="harness-pipeline-test"
NETWORK="${PROJECT_NAME}_harness-net"
MOCK_NAME="harness-pipeline-test-mockupstream"
TMUX_AGENT_NAME="harness-claude-tmuxtest"

echo "============================================================"
echo " harness full pipeline test"
echo "============================================================"

# --- preflight --------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[pipeline] ERROR: docker daemon not reachable" >&2
    exit 1
fi
if ! command -v unzip >/dev/null 2>&1; then
    echo "[pipeline] ERROR: 'unzip' is required but not on PATH" >&2
    exit 1
fi

# --- staging ----------------------------------------------------------------

TEST_ROOT="$(mktemp -d -t harness-pipeline-root.XXXXXX)"
FAKE_HOME="$(mktemp -d -t harness-pipeline-home.XXXXXX)"
TEST_WORKSPACE="$(mktemp -d -t harness-pipeline-ws.XXXXXX)"

# Pre-seed the firewall allowlist at a stable path so docker compose's
# bind-mount resolves on the very first cleanup-pass `compose down` (run
# before T1 / harness-install.sh has had a chance to lay down the install
# root). We point HARNESS_ALLOWLIST_PATH at TEST_ROOT/.harness-allowlist so
# the compose mount works regardless of whether harness-install.sh has run
# yet — the real harness-install.sh will seed its own copy at
# <install-root>/.harness-allowlist
# from the example, but we don't depend on that step here.
cp "${REPO_ROOT}/.harness-allowlist.example" "${TEST_ROOT}/.harness-allowlist"
export HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist"

cleanup() {
    local rc=$?
    echo "[pipeline] cleanup (rc=${rc})"

    # Tear down the tmux test agent if it survived.
    docker rm -f "${TMUX_AGENT_NAME}" >/dev/null 2>&1 || true

    # Tear down the mock upstream sidecar.
    docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true

    # Tear down any T16 MCP fixture that leaked.
    docker rm -f "${PROJECT_NAME}_pipe_mcp" >/dev/null 2>&1 || true

    # Stop any agent containers labeled by this project.
    local stragglers
    stragglers=$(docker ps -aq --filter "label=harness.agent=true" 2>/dev/null || true)
    if [[ -n "${stragglers}" ]]; then
        # We can't easily filter by project label, but harness-pipeline-test
        # agents are mounted on $TEST_WORKSPACE. The simplest belt-and-braces
        # is to remove anything with our deterministic name pattern.
        for c in ${stragglers}; do
            local mount
            mount=$(docker inspect -f '{{ index .Config.Labels "harness.mount" }}' "$c" 2>/dev/null || true)
            if [[ "${mount}" == "${TEST_WORKSPACE}"* ]]; then
                docker rm -f "$c" >/dev/null 2>&1 || true
            fi
        done
    fi

    # harness down is idempotent — the symlink may not exist if install
    # bailed early, in which case we fall back to compose directly.
    if [[ -x "${FAKE_HOME}/.local/bin/harness" ]]; then
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${FAKE_HOME}/.local/bin/harness" down >/dev/null 2>&1 || true
    fi
    docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        down -v --remove-orphans >/dev/null 2>&1 || true

    # Network may linger if we created it manually for the mockupstream.
    docker network rm "${NETWORK}" >/dev/null 2>&1 || true

    # ollama-data may contain files owned by a uid we can't directly remove
    # (the in-container ollama runs as root, so blobs land owned by host
    # uid 0). Use a privileged docker run to wipe the path before letting
    # the host rm -rf finish the job.
    for d in "${TEST_ROOT}" "${FAKE_HOME}" "${TEST_WORKSPACE}"; do
        if [[ -d "$d" ]]; then
            if ! rm -rf "$d" 2>/dev/null; then
                docker run --rm -v "$d:/target" --user 0:0 alpine \
                    sh -c 'rm -rf /target/* /target/.[!.]* 2>/dev/null || true' \
                    >/dev/null 2>&1 || true
                rm -rf "$d" 2>/dev/null || true
            fi
        fi
    done
    exit "${rc}"
}
trap cleanup EXIT INT TERM

harness_call() {
    HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
        "${FAKE_HOME}/.local/bin/harness" "$@"
}

# Defensive: clear stale state from a prior run.
docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true
docker rm -f "${MOCK_NAME}" "${TMUX_AGENT_NAME}" >/dev/null 2>&1 || true

# --- T0: build the zip -----------------------------------------------------

echo "[pipeline] T0: build_zip.sh"
bash "${REPO_ROOT}/scripts/build_zip.sh" >/dev/null
ZIP="${REPO_ROOT}/dist/harness-distribution.zip"
if [[ ! -f "${ZIP}" ]]; then
    echo "[pipeline] T0 FAIL: zip not produced at ${ZIP}" >&2
    exit 1
fi
echo "[pipeline] T0 OK: ${ZIP}"

# --- T1: install flow -------------------------------------------------------

echo "[pipeline] T1: extract zip + run harness-install.sh"
( cd "${TEST_ROOT}" && unzip -q "${ZIP}" )

# Pre-fill .env so harness-install.sh's "edit .env" prompt is unnecessary. Values
# point PROXY_API_URL at the mockupstream sidecar we'll bring up later.
cat >"${TEST_ROOT}/.env" <<EOF
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
PUBLISH_OLLAMA_PORT=
MOCK_SCENARIO=text
EOF

# harness-install.sh prompts: continue? [y/N], add to PATH? [Y/n]. Send y, y.
# harness-install.sh installs into $(pwd) — must cd into TEST_ROOT first.
(
    cd "${TEST_ROOT}"
    HOME="${FAKE_HOME}" HARNESS_REPO_URL="${REPO_ROOT}" \
        bash "${TEST_ROOT}/harness-install.sh" <<<$'y\ny\n' >"${TEST_ROOT}/install.log" 2>&1
)

# harness-install.sh clones HEAD of the local repo, but the pipeline test is
# meant to validate the *current working tree* — including uncommitted
# changes (e.g. the harness script with new subcommands the test exercises).
# Overlay the working tree onto the clone, preserving the .git directory
# created by harness-install.sh so subsequent commands like `harness update`
# still work.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='.git/' \
        --exclude='dist/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='.harness-allowlist' \
        --exclude='.harness-net-overrides.json' \
        --exclude='state/' \
        "${REPO_ROOT}/" "${TEST_ROOT}/harness/"
else
    # Fallback: tar-pipe (preserves modes; excludes via tar-style globs).
    ( cd "${REPO_ROOT}" && tar --exclude='.git' --exclude='dist' \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='./.env' --exclude='./.harness-allowlist' \
        --exclude='./.harness-net-overrides.json' \
        --exclude='./state' -cf - . ) \
        | ( cd "${TEST_ROOT}/harness" && tar -xf - )
fi

echo "[pipeline] T1 OK"

# --- T2: install verification ---------------------------------------------

echo "[pipeline] T2: install layout"
[[ -L "${FAKE_HOME}/.local/bin/harness" ]]              || { echo "[pipeline] T2 FAIL: harness symlink missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/.git" ]]                    || { echo "[pipeline] T2 FAIL: clone is not a git repo" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/output" ]]            || { echo "[pipeline] T2 FAIL: state/output/ missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/agent/claude" ]]      || { echo "[pipeline] T2 FAIL: state/agent/claude/ missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/agent/opencode" ]]    || { echo "[pipeline] T2 FAIL: state/agent/opencode/ missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/ollama-data" ]]       || { echo "[pipeline] T2 FAIL: state/ollama-data/ missing" >&2; exit 1; }
[[ -f "${TEST_ROOT}/harness/.env" ]]                    || { echo "[pipeline] T2 FAIL: .env missing in clone" >&2; exit 1; }
[[ -f "${TEST_ROOT}/harness/.harness-allowlist" ]]      || { echo "[pipeline] T2 FAIL: .harness-allowlist missing in clone" >&2; exit 1; }
echo "[pipeline] T2 OK"

# --- T3: harness help -------------------------------------------------------

echo "[pipeline] T3: harness help"
help_out=$(harness_call help)
for cmd in start down claude opencode doctor list attach stop; do
    if ! grep -q "\b${cmd}\b" <<<"${help_out}"; then
        echo "[pipeline] T3 FAIL: help text missing '${cmd}'" >&2
        echo "${help_out}" >&2
        exit 1
    fi
done
echo "[pipeline] T3 OK"

# --- T4: doctor with services down -----------------------------------------

echo "[pipeline] T4: harness doctor (services down)"
set +e
doc_out=$(harness_call doctor 2>&1)
doc_rc=$?
set -e
for s in '\[deps\]' '\[install\]' '\[config\]' '\[storage\]' '\[runtime\]' '\[images\]'; do
    if ! grep -Eq "${s}" <<<"${doc_out}"; then
        echo "[pipeline] T4 FAIL: doctor missing section ${s}" >&2
        echo "${doc_out}" >&2
        exit 1
    fi
done
if ! grep -Eq 'services not running|not present' <<<"${doc_out}"; then
    echo "[pipeline] T4 FAIL: doctor [runtime] did not report services down" >&2
    echo "${doc_out}" >&2
    exit 1
fi
echo "[pipeline] T4 OK (rc=${doc_rc})"

# --- T5: harness start ------------------------------------------------------

echo "[pipeline] T5: harness start"
harness_call start >"${TEST_ROOT}/start.log" 2>&1

wait_healthy_compose() {
    local svc="$1" timeout_s="$2"
    local deadline=$(( $(date +%s) + timeout_s ))
    while true; do
        local cid
        cid=$(docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" \
            ps -q "${svc}" 2>/dev/null || true)
        if [[ -n "${cid}" ]]; then
            local status
            status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")
            if [[ "${status}" == "healthy" ]]; then
                return 0
            fi
        fi
        if (( $(date +%s) >= deadline )); then
            echo "[pipeline] timed out waiting for ${svc}" >&2
            docker compose --project-name "${PROJECT_NAME}" \
                -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
            return 1
        fi
        sleep 2
    done
}

if ! wait_healthy_compose ollama 90; then echo "[pipeline] T5 FAIL ollama" >&2; exit 1; fi
if ! wait_healthy_compose proxy 90; then echo "[pipeline] T5 FAIL proxy" >&2; exit 1; fi

# Build agent images (the harness script doesn't auto-build agents on start;
# agent_test.sh / install relies on `compose --profile agent build`). The
# pipeline test needs both images present for T9–T11.
echo "[pipeline] T5: building agent images (compose --profile agent build)"
docker compose --project-name "${PROJECT_NAME}" \
    --env-file "${TEST_ROOT}/harness/.env" \
    -f "${TEST_ROOT}/harness/docker-compose.yml" \
    --profile agent build >"${TEST_ROOT}/agent-build.log" 2>&1

echo "[pipeline] T5 OK"

# --- T6: bring up mock upstream sidecar ------------------------------------
#
# We need the mock listening as `mockupstream` on the harness-net network so
# the proxy can resolve it. We don't add it to the compose file — we just
# `docker run -d` it with --network and --network-alias.

echo "[pipeline] T6: launch mockupstream on ${NETWORK}"
docker run -d \
    --name "${MOCK_NAME}" \
    --network "${NETWORK}" \
    --network-alias mockupstream \
    -e MOCK_SCENARIO=text \
    -v "${REPO_ROOT}/scripts/mock_upstream.py:/app/mock_upstream.py:ro" \
    -w /app \
    python:3.12-slim \
    sh -c 'pip install --quiet --no-cache-dir flask==3.0.3 && python /app/mock_upstream.py' \
    >/dev/null

# Wait for /health from inside the network (use the ollama container as a
# probe; it's already on the network and has curl).
ollama_cid=$(docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q ollama)
deadline=$(( $(date +%s) + 60 ))
while true; do
    if docker exec "${ollama_cid}" curl -sf http://mockupstream:9000/health >/dev/null 2>&1; then
        break
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[pipeline] T6 FAIL: mockupstream never became reachable" >&2
        docker logs "${MOCK_NAME}" >&2 || true
        exit 1
    fi
    sleep 2
done
echo "[pipeline] T6 OK"

# --- T7: doctor with services up -------------------------------------------

echo "[pipeline] T7: harness doctor (services up)"
set +e
doc_up_out=$(harness_call doctor 2>&1)
doc_up_rc=$?
set -e
echo "${doc_up_out}" | sed 's/^/  | /'
if (( doc_up_rc != 0 )); then
    echo "[pipeline] T7 FAIL: doctor exited ${doc_up_rc}" >&2
    exit 1
fi
grep -Eq 'ollama[[:space:]]+healthy' <<<"${doc_up_out}" \
    || { echo "[pipeline] T7 FAIL: doctor did not show ollama healthy" >&2; exit 1; }
grep -Eq 'proxy[[:space:]]+healthy'  <<<"${doc_up_out}" \
    || { echo "[pipeline] T7 FAIL: doctor did not show proxy healthy"  >&2; exit 1; }
echo "[pipeline] T7 OK"

# --- T8: harness list (empty) ----------------------------------------------

echo "[pipeline] T8: harness list (empty)"
list_out=$(harness_call list)
if ! grep -Eq 'no harness agents running' <<<"${list_out}"; then
    echo "[pipeline] T8 FAIL: expected 'no harness agents running'" >&2
    echo "${list_out}" >&2
    exit 1
fi
echo "[pipeline] T8 OK"

# --- T9: harness claude -p (headless) --------------------------------------

echo "[pipeline] T9: harness claude -p"
cd "${TEST_WORKSPACE}"
set +e
t9_out=$(timeout 60 bash -c 'HOME='"'${FAKE_HOME}'"' HARNESS_PROJECT_NAME='"'${PROJECT_NAME}'"' '"'${FAKE_HOME}/.local/bin/harness'"' claude -p "say hello" 2>&1 < /dev/null')
t9_rc=$?
set -e
cd "${REPO_ROOT}"
echo "[pipeline]   T9 raw (truncated): $(echo "${t9_out}" | tail -c 800)"
if (( t9_rc != 0 )); then
    echo "[pipeline] T9 FAIL: harness claude -p exited ${t9_rc}" >&2
    echo "${t9_out}" >&2
    exit 1
fi
if ! grep -q "Hello from mock upstream" <<<"${t9_out}"; then
    echo "[pipeline] T9 FAIL: expected mock upstream response in output" >&2
    echo "${t9_out}" >&2
    exit 1
fi
# Headless agents must not appear in 'harness list'.
list_after=$(harness_call list)
if ! grep -Eq 'no harness agents running' <<<"${list_after}"; then
    echo "[pipeline] T9 FAIL: headless run leaked into 'harness list'" >&2
    echo "${list_after}" >&2
    exit 1
fi
echo "[pipeline] T9 OK"

# --- T10: harness opencode -p (headless) -----------------------------------

echo "[pipeline] T10: harness opencode -p"
cd "${TEST_WORKSPACE}"
set +e
t10_out=$(timeout 60 bash -c 'HOME='"'${FAKE_HOME}'"' HARNESS_PROJECT_NAME='"'${PROJECT_NAME}'"' '"'${FAKE_HOME}/.local/bin/harness'"' opencode -p "say hello" 2>&1 < /dev/null')
t10_rc=$?
set -e
cd "${REPO_ROOT}"
echo "[pipeline]   T10 raw (truncated): $(echo "${t10_out}" | tail -c 800)"

# As in agent_test.sh test B, opencode's `run` may require interactive
# provider auth on some opencode versions. If we hit that, skip with a
# clear note rather than failing.
if (( t10_rc != 0 )); then
    if echo "${t10_out}" | grep -qiE 'auth|login|provider .* not (configured|found)|no .* api key'; then
        echo "[pipeline] T10 SKIPPED: opencode run requires interactive provider auth"
    else
        echo "[pipeline] T10 FAIL: harness opencode -p exited ${t10_rc}" >&2
        echo "${t10_out}" >&2
        exit 1
    fi
else
    if ! grep -q "Hello from mock upstream" <<<"${t10_out}"; then
        echo "[pipeline] T10 FAIL: expected mock upstream response in output" >&2
        echo "${t10_out}" >&2
        exit 1
    fi
    echo "[pipeline] T10 OK"
fi

# --- T11: tmux interactive flow --------------------------------------------
#
# We can't directly drive `harness claude` because it execs into
# `docker exec -it tmux attach`, which would block this script. Instead we
# reproduce the docker run command (matching what run_agent_interactive
# would issue) but skip the attach step. Then we drive tmux via the
# tui_driver.sh toolkit (hex-0d Enter, ANSI stripping, busy→idle detection)
# and verify the response in capture-pane.
#
# We use a fixed container name (TMUX_AGENT_NAME) instead of the hash-based
# name so the trap can clean up unconditionally.

echo "[pipeline] T11: tmux send-keys / capture-pane (via tui_driver.sh)"
# The full agent entrypoint (root branch) runs init-firewall.sh before gosu —
# we must mirror what run_agent_interactive does in the harness CLI:
#   --cap-add NET_ADMIN --cap-add NET_RAW
#   -v <install-root>/.harness-allowlist:/etc/harness/allowlist:ro
# harness-install.sh has already seeded ${TEST_ROOT}/.harness-allowlist from the
# bundled .harness-allowlist.example (T1).
docker run -d \
    --name "${TMUX_AGENT_NAME}" \
    --network "${NETWORK}" \
    --user 0:0 \
    --cap-add NET_ADMIN \
    --cap-add NET_RAW \
    -e "HOST_UID=$(id -u)" \
    -e "HOST_GID=$(id -g)" \
    -e "OLLAMA_AGENT_MODEL=harness" \
    -e "ANTHROPIC_BASE_URL=http://ollama:11434" \
    -e "ANTHROPIC_AUTH_TOKEN=harness-dummy" \
    -e "ANTHROPIC_MODEL=harness" \
    -e "ANTHROPIC_SMALL_FAST_MODEL=harness" \
    -v "${TEST_WORKSPACE}:/workspace" \
    -v "${TEST_ROOT}/harness/state/agent/claude:/home/harness" \
    -v "${TEST_ROOT}/.harness-allowlist:/etc/harness/allowlist:ro" \
    -w /workspace \
    --label "harness.agent=true" \
    --label "harness.tool=claude" \
    --label "harness.mount=${TEST_WORKSPACE}" \
    harness-claude-agent:latest \
    >/dev/null

# Phase 1: tmux session must exist. tmux is owned by the harness user in
# the container (entrypoint exec's gosu harness after the UID remap), so
# we exec as harness rather than root. tui_driver wraps that — but here we
# need a one-shot has-session probe with a deadline, so we spell it out.
deadline=$(( $(date +%s) + 30 ))
while true; do
    if docker exec --user harness "${TMUX_AGENT_NAME}" tmux has-session -t harness-agent 2>/dev/null; then
        break
    fi
    if [[ -z "$(docker ps -q -f "name=^${TMUX_AGENT_NAME}$" 2>/dev/null)" ]]; then
        echo "[pipeline] T11 FAIL: container exited before tmux ready" >&2
        docker logs "${TMUX_AGENT_NAME}" >&2 || true
        exit 1
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[pipeline] T11 FAIL: tmux session not ready in 30s" >&2
        docker logs "${TMUX_AGENT_NAME}" >&2 || true
        exit 1
    fi
    sleep 1
done

# Helper: dump the cleaned pane on failure so we can diagnose without
# re-capturing. `tui_capture_clean` uses -J (join wrapped lines) and ANSI-
# strips, so the output is regex-friendly.
t11_dump_pane() {
    {
        echo "--- pane (ANSI stripped, joined) ---"
        tui_capture_clean "${TMUX_AGENT_NAME}" harness-agent || true
        echo "--- container logs (last 50) ---"
        docker logs "${TMUX_AGENT_NAME}" 2>&1 | tail -50 || true
    } >&2
}

# Phase 2: walk claude's first-run dialogs (theme → API-key → security
# notes → workspace-trust). The state is persisted in agent/claude/ so
# subsequent runs in production skip these; the pipeline test mounts a
# fresh agent/claude/ so we always hit them.

if ! tui_wait_for_text "${TMUX_AGENT_NAME}" harness-agent 'Choose the text style|Dark mode' 30; then
    echo "[pipeline] T11 FAIL: theme picker did not appear in 30s" >&2
    t11_dump_pane
    exit 1
fi
tui_send_key "${TMUX_AGENT_NAME}" harness-agent Enter

# After theme select, claude shows either:
#   (a) "Detected a custom API key" dialog — only when ANTHROPIC_API_KEY is set
#   (b) Security notes screen (older builds) or workspace-trust (newer)
# The harness now sets ANTHROPIC_AUTH_TOKEN (not API_KEY) so claude can skip
# its api.anthropic.com preflight probe behind our default firewall, which
# means path (a) is no longer expected. Keep the branch optional in case a
# future caller still passes API_KEY.
if tui_wait_for_text "${TMUX_AGENT_NAME}" harness-agent 'Detected a custom API key|API_KEY' 5; then
    # "1" picks "Yes". send-text avoids tmux interpreting "1" as a key name.
    tui_send_text "${TMUX_AGENT_NAME}" harness-agent "1"
    sleep 0.1
    tui_send_key "${TMUX_AGENT_NAME}" harness-agent Enter
fi

# Wait for Security notes (older builds) or workspace-trust (newer builds —
# Security notes screen was removed). Dismiss Security notes if present, then
# always Enter through workspace-trust.
if ! tui_wait_for_text "${TMUX_AGENT_NAME}" harness-agent \
        'Security notes|Press Enter|Accessing workspace|trust this folder' 20; then
    echo "[pipeline] T11 FAIL: neither Security notes nor workspace-trust dialog appeared" >&2
    t11_dump_pane
    exit 1
fi
__t11_pane=$(tui_capture_clean "${TMUX_AGENT_NAME}" harness-agent || true)
if grep -qE 'Security notes' <<<"${__t11_pane}"; then
    tui_send_key "${TMUX_AGENT_NAME}" harness-agent Enter
    if ! tui_wait_for_text "${TMUX_AGENT_NAME}" harness-agent 'Accessing workspace|trust this folder' 20; then
        echo "[pipeline] T11 FAIL: workspace-trust dialog did not follow security notes" >&2
        t11_dump_pane
        exit 1
    fi
fi
tui_send_key "${TMUX_AGENT_NAME}" harness-agent Enter

# Phase 3: wait for the main prompt to render, then send the actual user
# prompt and let tui_wait_agent_done handle the busy→idle transition.
if ! tui_wait_for_text "${TMUX_AGENT_NAME}" harness-agent 'Welcome|shortcuts|Tips for getting started' 30; then
    echo "[pipeline] T11 FAIL: main prompt did not render after dialogs" >&2
    t11_dump_pane
    exit 1
fi

if ! tui_prompt_and_wait "${TMUX_AGENT_NAME}" harness-agent 'say hello' 90; then
    echo "[pipeline] T11 FAIL: agent did not finish processing 'say hello'" >&2
    t11_dump_pane
    exit 1
fi

# Phase 4: assert the mock response landed in the pane.
if ! tui_assert_response_contains "${TMUX_AGENT_NAME}" harness-agent 'Hello from mock upstream'; then
    echo "[pipeline] T11 FAIL: did not see mock-upstream response in pane" >&2
    docker logs "${TMUX_AGENT_NAME}" 2>&1 | tail -50 >&2 || true
    exit 1
fi

# Phase 5: harness list must show the labeled agent.
list_out=$(harness_call list)
if ! grep -q "${TMUX_AGENT_NAME}" <<<"${list_out}"; then
    echo "[pipeline] T11 FAIL: harness list did not include ${TMUX_AGENT_NAME}" >&2
    echo "${list_out}" >&2
    exit 1
fi

# Phase 6: harness stop should kill it cleanly.
harness_call stop "${TMUX_AGENT_NAME}" >/dev/null
if [[ -n "$(docker ps -q -f "name=^${TMUX_AGENT_NAME}$" 2>/dev/null)" ]]; then
    echo "[pipeline] T11 FAIL: container still running after harness stop" >&2
    exit 1
fi
echo "[pipeline] T11 OK"

# --- T12 (skipped here, see MANUAL_TEST_PROMPT.md) -------------------------
#
# File-creation-with-correct-ownership requires a real upstream that emits
# tool-call JSON. The mock returns plain text only, so the agent has no way
# to actually drive an Edit/Write tool. Phase 2's proxy_test.sh covers the
# UID-translation logic at the proxy layer; the manual test covers the
# end-to-end "agent created a file with my UID" scenario.

# --- T15: persistent home marker -------------------------------------------
#
# T9/T11 already ran agents against the bind-mounted home, so the skel-seed
# marker should be present. Re-running `harness claude -p` exercises the
# same code path a second time and must not regress.

echo "[pipeline] T15: persistent home marker + idempotent re-seed"
marker="${TEST_ROOT}/harness/state/agent/claude/.harness-home-initialized"
if [[ ! -f "${marker}" ]]; then
    echo "[pipeline] T15 FAIL: skel-seed marker missing at ${marker}" >&2
    ls -la "${TEST_ROOT}/harness/state/agent/claude" >&2 || true
    exit 1
fi
cd "${TEST_WORKSPACE}"
set +e
t15_out=$(timeout 60 bash -c 'HOME='"'${FAKE_HOME}'"' HARNESS_PROJECT_NAME='"'${PROJECT_NAME}'"' '"'${FAKE_HOME}/.local/bin/harness'"' claude -p "hi" 2>&1 < /dev/null')
t15_rc=$?
set -e
cd "${REPO_ROOT}"
if (( t15_rc != 0 )); then
    echo "[pipeline] T15 FAIL: second harness claude -p exited ${t15_rc}" >&2
    echo "${t15_out}" | tail -c 600 >&2
    exit 1
fi
echo "[pipeline] T15 OK"

# --- T16: MCP enable + start + disable cycle -------------------------------
#
# Build a fake MCP fixture under a tmp registry, enable + start it, verify
# the service comes up healthy on harness-net, then disable and confirm
# cleanup.  Same shape as scripts/mcp_test.sh but folded into the end-to-
# end flow so we exercise the integration with services already running.

echo "[pipeline] T16: MCP install + start + uninstall cycle"
T16_REG="${TEST_ROOT}/t16-registry"
mkdir -p "${T16_REG}/_pipe_mcp"
cat >"${T16_REG}/_pipe_mcp/compose.yml" <<EOF
services:
  pipe_mcp:
    image: python:3.12-slim
    container_name: ${PROJECT_NAME}_pipe_mcp
    networks:
      - harness-net
    profiles:
      - mcp
    command: python -m http.server 8765 --bind 0.0.0.0
    healthcheck:
      test: ["CMD-SHELL", "python -c 'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(\"http://127.0.0.1:8765/\",timeout=2).status==200 else 1)'"]
      interval: 5s
      timeout: 3s
      retries: 6
      start_period: 5s
networks:
  harness-net:
EOF
cat >"${T16_REG}/_pipe_mcp/client-config.json" <<'EOF'
{ "mcpServers": { "pipe_mcp": { "type": "sse", "url": "http://pipe_mcp:8765/sse" } } }
EOF

t16_call() {
    HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
    HARNESS_REGISTRY_DIR="${T16_REG}" \
        "${FAKE_HOME}/.local/bin/harness" "$@"
}

t16_call mcp install _pipe_mcp >"${TEST_ROOT}/t16-install.log" 2>&1 || {
    echo "[pipeline] T16 FAIL: mcp install failed" >&2
    cat "${TEST_ROOT}/t16-install.log" >&2
    exit 1
}
if [[ ! -f "${TEST_ROOT}/harness/state/mcp/_pipe_mcp/compose.yml" ]]; then
    echo "[pipeline] T16 FAIL: install did not copy compose.yml into install root" >&2
    exit 1
fi
if [[ ! -f "${TEST_ROOT}/harness/state/mcp/_pipe_mcp/harness-meta.json" ]]; then
    echo "[pipeline] T16 FAIL: install did not write harness-meta.json" >&2
    exit 1
fi

t16_call start >"${TEST_ROOT}/t16-start.log" 2>&1 || {
    echo "[pipeline] T16 FAIL: start with MCP active failed" >&2
    tail -30 "${TEST_ROOT}/t16-start.log" >&2
    exit 1
}

deadline=$(( $(date +%s) + 60 ))
while true; do
    cid=$(docker ps -q --filter "name=^${PROJECT_NAME}_pipe_mcp$" 2>/dev/null || true)
    if [[ -n "${cid}" ]]; then
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")
        if [[ "${status}" == "healthy" ]]; then break; fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[pipeline] T16 FAIL: pipe_mcp not healthy in 60s" >&2
        docker ps --filter "name=pipe_mcp" >&2 || true
        docker logs "${cid}" 2>&1 | tail -30 >&2 || true
        exit 1
    fi
    sleep 2
done

# Doctor must now have an [mcp] section.
doc_mcp_out=$(t16_call doctor 2>&1 || true)
if ! grep -Eq '\[mcp\]' <<<"${doc_mcp_out}"; then
    echo "[pipeline] T16 FAIL: doctor missing [mcp] section after enable" >&2
    echo "${doc_mcp_out}" >&2
    exit 1
fi

t16_call mcp uninstall _pipe_mcp --force >"${TEST_ROOT}/t16-uninstall.log" 2>&1 || {
    echo "[pipeline] T16 FAIL: mcp uninstall failed" >&2
    cat "${TEST_ROOT}/t16-uninstall.log" >&2
    exit 1
}
if [[ -f "${TEST_ROOT}/harness/state/mcp/_pipe_mcp/compose.yml" ]]; then
    echo "[pipeline] T16 FAIL: compose.yml still present after uninstall" >&2
    exit 1
fi
# data/ should remain.
if [[ ! -d "${TEST_ROOT}/harness/state/mcp/_pipe_mcp/data" ]]; then
    echo "[pipeline] T16 FAIL: data/ removed by uninstall (should be preserved)" >&2
    exit 1
fi

# Re-run start; the MCP service must be torn down on next compose up.
harness_call start >"${TEST_ROOT}/t16-start2.log" 2>&1 || {
    echo "[pipeline] T16 FAIL: post-disable start failed" >&2
    tail -30 "${TEST_ROOT}/t16-start2.log" >&2
    exit 1
}
# Compose only stops services it knows about; with the MCP -f file no
# longer spliced (active tree is empty), `up -d` won't touch the dangling
# pipe_mcp container — but it should be orphaned from compose's view. The
# expected behavior is for `harness down` (in T13) to clean it up via
# --remove-orphans. Just remove it explicitly so we don't leak.
docker rm -f "${PROJECT_NAME}_pipe_mcp" >/dev/null 2>&1 || true

echo "[pipeline] T16 OK"

# --- T13: harness down ------------------------------------------------------

echo "[pipeline] T13: harness down"
harness_call down >/dev/null

remaining=$(docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q 2>/dev/null || true)
if [[ -n "${remaining}" ]]; then
    echo "[pipeline] T13 FAIL: containers still present after down" >&2
    docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
    exit 1
fi
echo "[pipeline] T13 OK"

# --- T14: harness update ----------------------------------------------------
#
# The clone is a fresh `git clone <local repo>`, so `git pull --ff-only`
# should be a clean no-op (or a fast-forward if anything changed).

echo "[pipeline] T14: harness update"
update_out=$(harness_call update 2>&1)
echo "${update_out}" | sed 's/^/  | /'
echo "[pipeline] T14 OK"

echo "============================================================"
echo " FULL PIPELINE TEST PASSED"
echo "============================================================"
exit 0
