#!/usr/bin/env bash
#
# scripts/integration_test.sh — comprehensive end-to-end integration test
# for harness's two flagship integrations:
#
#   - Pattern A (HTTP MCP):    Serena. Long-lived compose service reached
#                              via SSE on the harness network.
#   - Pattern B (skill CLI):   Graphify. Pure-local AST/graph extractor
#                              installed inside an agent home via pipx.
#
# Slow (~10-15 min wall clock) — first Serena image build is 5-10 minutes
# and ~2GB. Subsequent runs reuse the cached image. Gated behind the env
# var HARNESS_RUN_SLOW=1 so the default test suite stays fast.
#
# What this test exercises (that the default suite does not):
#   - The full MCP install -> restart -> reach -> tool-call -> down/up ->
#     disable/enable -> uninstall lifecycle, against a real upstream MCP.
#   - The full skill install path: pipx into the persistent agent home,
#     binary visible from the host bind mount, runs against a fixture
#     project, deterministic output, file ownership matches host UID,
#     persists across container rebuild.
#
# Run:
#   HARNESS_RUN_SLOW=1 bash scripts/integration_test.sh

set -euo pipefail

# --- gate -------------------------------------------------------------------

if [[ "${HARNESS_RUN_SLOW:-0}" != "1" ]]; then
    echo "integration_test.sh: skipped (set HARNESS_RUN_SLOW=1 to run)"
    echo "  This test takes 10-15 minutes and pulls/builds large images (Serena ~2GB)."
    echo "  Run before releases; not part of the default test suite."
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export REPO_ROOT  # test_helpers.sh expects this in scope.

# shellcheck source=lib/test_helpers.sh
source "${REPO_ROOT}/scripts/lib/test_helpers.sh"

require_docker

# --- staging ----------------------------------------------------------------

PROJECT_NAME="harness-integration-test"
NETWORK="${PROJECT_NAME}_harness-net"
MOCK_NAME="${PROJECT_NAME}-mockupstream-1"
GRAPHIFY_AGENT_NAME="harness-claude-graphify-install"
GRAPHIFY_AGENT_NAME_2="harness-claude-graphify-persist"

TEST_ROOT=$(mktemp -d -t harness-integration-root.XXXXXX)
FAKE_HOME=$(mktemp -d -t harness-integration-home.XXXXXX)
TEST_INSTALL="${TEST_ROOT}/harness"
TEST_WORKSPACE="${TEST_ROOT}/workspace"
mkdir -p "${TEST_WORKSPACE}"

cleanup() {
    local rc=$?
    echo "[integration] cleanup (rc=${rc})"

    # Kill any graphify test containers we launched directly. These
    # don't run under compose so `harness down` won't touch them.
    for c in "${GRAPHIFY_AGENT_NAME}" "${GRAPHIFY_AGENT_NAME_2}"; do
        docker rm -f "$c" >/dev/null 2>&1 || true
    done

    # Mock upstream sidecar.
    docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true

    # Best-effort: tear down the harness stack (proxy, ollama, serena) via
    # the install we built. The script may not exist if we bailed before
    # cloning, so guard the call.
    if [[ -x "${TEST_INSTALL}/harness" ]]; then
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${TEST_INSTALL}/harness" mcp uninstall serena --force >/dev/null 2>&1 || true
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${TEST_INSTALL}/harness" down >/dev/null 2>&1 || true
    fi

    # Belt-and-braces compose down in case the harness script is gone.
    docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        down -v --remove-orphans >/dev/null 2>&1 || true

    # Network may linger if we created it manually for a sidecar.
    docker network rm "${NETWORK}" >/dev/null 2>&1 || true

    # The serena container is fixed-named (container_name in its compose);
    # remove it explicitly in case compose missed it.
    docker rm -f harness-serena >/dev/null 2>&1 || true

    # ollama-data may contain root-owned blobs; wipe via privileged docker run.
    # HARNESS_INTEGRATION_KEEP=1 preserves TEST_ROOT/FAKE_HOME for postmortem.
    if [[ "${HARNESS_INTEGRATION_KEEP:-0}" == "1" ]]; then
        echo "[integration] HARNESS_INTEGRATION_KEEP=1: leaving TEST_ROOT=${TEST_ROOT} FAKE_HOME=${FAKE_HOME}"
    else
        for d in "${TEST_ROOT}" "${FAKE_HOME}"; do
            if [[ -d "$d" ]]; then
                if ! rm -rf "$d" 2>/dev/null; then
                    docker run --rm -v "$d:/target" --user 0:0 alpine \
                        sh -c 'rm -rf /target/* /target/.[!.]* 2>/dev/null || true' \
                        >/dev/null 2>&1 || true
                    rm -rf "$d" 2>/dev/null || true
                fi
            fi
        done
    fi
    exit "${rc}"
}
trap cleanup EXIT INT TERM

# Defensive: clear any stale state from a previous aborted run.
docker rm -f "${MOCK_NAME}" \
    "${GRAPHIFY_AGENT_NAME}" "${GRAPHIFY_AGENT_NAME_2}" \
    harness-serena >/dev/null 2>&1 || true
docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true

# --- install layout ---------------------------------------------------------

echo "[integration] cloning repo into ${TEST_INSTALL}"
# Local-path clone is fast and matches what harness-install.sh does in the
# pipeline test. We use the working tree (not HEAD) so uncommitted changes
# are exercised — git clone of a worktree doesn't pick those up, so we
# overlay after.
git clone --depth=1 "${REPO_ROOT}" "${TEST_INSTALL}" >/dev/null 2>&1
# Overlay the working tree on top of the clone so uncommitted changes are
# exercised. rsync is the cleanest tool for this but isn't shipped with Git
# Bash on Windows; fall back to tar-pipe, which is on every supported host.
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='.git/' \
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='.harness-allowlist' \
        --exclude='.harness-net-overrides.json' \
        --exclude='state/' \
        "${REPO_ROOT}/" "${TEST_INSTALL}/" >/dev/null
else
    tar -C "${REPO_ROOT}" \
        --exclude='./.git' \
        --exclude='./__pycache__' \
        --exclude='*.pyc' \
        --exclude='./.env' \
        --exclude='./.harness-allowlist' \
        --exclude='./.harness-net-overrides.json' \
        --exclude='./state' \
        -cf - . | tar -C "${TEST_INSTALL}" -xf -
fi

# Ensure all the runtime state dirs the harness expects exist.
mkdir -p \
    "${TEST_INSTALL}/state/output" \
    "${TEST_INSTALL}/state/agent/home" \
    "${TEST_INSTALL}/state/ollama-data" \
    "${TEST_INSTALL}/state/mcp"

# .env — point PROXY_API_URL at the mockupstream sidecar (joined to
# ${PROJECT_NAME}_harness-net later). HARNESS_PROJECTS_ROOT is what serena
# mounts read-only at /workspaces/projects/; setting it to TEST_WORKSPACE
# means Serena sees /workspaces/projects/test-project/.
test_generate_env "${TEST_INSTALL}/.env" \
    "PROXY_API_URL=http://mockupstream:9000/v1/chat/completions" \
    "PROXY_API_KEY=test-key-1234" \
    "PROXY_API_MODEL=test-model" \
    "MOCK_SCENARIO=text" \
    "HARNESS_PROJECTS_ROOT=${TEST_WORKSPACE}"

# Allowlist must include hosts pipx and graphifyy reach during install,
# plus the canonical positive-case probes init-firewall.sh validates with.
# Tree-sitter-language-pack downloads grammars from github via pip.
test_generate_allowlist "${TEST_INSTALL}/.harness-allowlist" \
    api.anthropic.com

# Harness wrapper that points at our test install and uses an isolated
# project name so we don't collide with a real harness on the same daemon.
# HARNESS_NO_BUILD=1 is the default — the test pre-builds every image up
# front (Phase 1.0) so harness's `compose up --build` never has to fetch
# the upstream serena git context or burn 8GB+ of buildkit cache mid-test.
# Tests that specifically need a build can override per-call:
#   HARNESS_NO_BUILD=0 harness_call start
harness_call() {
    HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
        HARNESS_NO_BUILD="${HARNESS_NO_BUILD:-1}" \
        "${TEST_INSTALL}/harness" "$@"
}

# Copy the test project fixture into the workspace. Serena and Graphify
# both look at /workspace/test-project/ when an agent runs there.
cp -a "${REPO_ROOT}/scripts/fixtures/test-project" "${TEST_WORKSPACE}/"

# === Phase 1: stack setup with mock upstream ================================

phase_1_stack_setup() {
    echo "[integration] Phase 1.0: pre-building all images (proxy, ollama, agent)"
    # Build everything once with cache enabled. Subsequent harness_call
    # invocations run with HARNESS_NO_BUILD=1 so compose just `up`s the
    # already-tagged images. This sidesteps two real problems:
    #   - serena's git-context build is slow and brittle (network + 8GB cache)
    #   - buildkit eagerly grows the build cache on every --build invocation,
    #     which gradually exhausts disk on a 30GB OCI volume.
    # If a real harness install is missing an image, `harness start` (without
    # HARNESS_NO_BUILD) will build it; the env var is opt-in.
    if ! docker compose --project-name "${PROJECT_NAME}" \
            --env-file "${TEST_INSTALL}/.env" \
            -f "${TEST_INSTALL}/docker-compose.yml" \
            --profile agent build \
            >"${TEST_ROOT}/all-images-build.log" 2>&1; then
        echo "[integration] Phase 1.0 FAIL: pre-build failed" >&2
        tail -120 "${TEST_ROOT}/all-images-build.log" >&2
        return 1
    fi

    echo "[integration] Phase 1.1: harness start (HARNESS_NO_BUILD=1; bring up ollama/proxy)"
    harness_call start

    echo "[integration] Phase 1.3: launching mockupstream sidecar"
    test_start_mockupstream "${PROJECT_NAME}"

    echo "[integration] Phase 1.4: waiting for healthy ollama, proxy, mockupstream"
    test_wait_for_healthy "${PROJECT_NAME}" ollama 90
    test_wait_for_healthy "${PROJECT_NAME}" proxy 60
    test_wait_for_container_healthy "${MOCK_NAME}" 90

    echo "[integration] Phase 1.5: smoke test (harness claude -p \"say hello\")"
    local out rc
    set +e
    out=$(cd "${TEST_WORKSPACE}/test-project" && timeout 90 \
        bash -c "HOME='${FAKE_HOME}' HARNESS_PROJECT_NAME='${PROJECT_NAME}' '${TEST_INSTALL}/harness' claude -p \"say hello\" 2>&1 < /dev/null")
    rc=$?
    set -e
    if (( rc != 0 )); then
        echo "[integration] Phase 1.5 FAIL: harness claude -p exited ${rc}" >&2
        echo "${out}" | tail -c 1500 >&2
        return 1
    fi
    if ! grep -q "Hello from mock upstream" <<<"${out}"; then
        echo "[integration] Phase 1.5 FAIL: didn't see mock-upstream response in output" >&2
        echo "${out}" | tail -c 1500 >&2
        return 1
    fi
    echo "[integration] Phase 1: stack works end-to-end through the proxy"
}

# === Phase 2: Serena (HTTP MCP) end-to-end ==================================

phase_2_serena() {
    echo "[integration] Phase 2.1: harness mcp install serena"
    if ! harness_call mcp install serena; then
        echo "[integration] Phase 2.1 FAIL: mcp install serena returned non-zero" >&2
        return 1
    fi
    if ! harness_call mcp list | grep -qE "^serena[[:space:]]+installed-enabled"; then
        echo "[integration] Phase 2.1 FAIL: serena not in installed-enabled state" >&2
        harness_call mcp list >&2
        return 1
    fi

    if docker image inspect harness-serena:v0.1 >/dev/null 2>&1; then
        echo "[integration] Phase 2.2: harness-serena:v0.1 already present, skipping build"
    else
        echo "[integration] Phase 2.2: building serena image (~5-10 min, ~8GB build cache)"
        # Pre-build serena outside `harness restart` so the subsequent restart
        # with HARNESS_NO_BUILD=1 just `up`s the existing image. This sidesteps
        # `harness start`'s `compose up -d --build`, which would re-fetch the
        # upstream serena git context and burn buildkit cache repeatedly.
        #
        # Match the env contract the harness `compose()` wrapper provides:
        # INSTALL_ROOT and HARNESS_ALLOWLIST_PATH are referenced as plain
        # ${VAR} in mcp-registry/serena/compose.yml. Without them, compose
        # interpolates the volume specs to empty and rejects them with
        # 'invalid spec: :/path: empty section between colons'.
        if ! INSTALL_ROOT="${TEST_INSTALL}" \
                HARNESS_ALLOWLIST_PATH="${TEST_INSTALL}/.harness-allowlist" \
                docker compose --project-name "${PROJECT_NAME}" \
                --env-file "${TEST_INSTALL}/.env" \
                -f "${TEST_INSTALL}/docker-compose.yml" \
                -f "${TEST_INSTALL}/state/mcp/serena/compose.yml" \
                --profile mcp build serena \
                >"${TEST_ROOT}/serena-build.log" 2>&1; then
            echo "[integration] Phase 2.2 FAIL: serena image build failed" >&2
            tail -120 "${TEST_ROOT}/serena-build.log" >&2
            return 1
        fi
    fi

    echo "[integration] Phase 2.2: harness restart (HARNESS_NO_BUILD=1, image already built)"
    # The mockupstream sidecar joined harness-net via plain `docker run`, so
    # compose doesn't manage it. `harness restart` calls `compose down`, which
    # tries to remove the network and fails with "has active endpoints" while
    # mockupstream is still attached. Stop it first; Phase 2.2.1 re-launches.
    docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true
    if ! HARNESS_NO_BUILD=1 harness_call restart >"${TEST_ROOT}/serena-restart.log" 2>&1; then
        echo "[integration] Phase 2.2 FAIL: harness restart failed" >&2
        tail -120 "${TEST_ROOT}/serena-restart.log" >&2
        return 1
    fi

    echo "[integration] Phase 2.2.1: re-launching mockupstream"
    test_start_mockupstream "${PROJECT_NAME}"
    test_wait_for_container_healthy "${MOCK_NAME}" 90

    echo "[integration] Phase 2.2.2: waiting for serena healthcheck (timeout 600s for first build)"
    test_wait_for_healthy "${PROJECT_NAME}" serena 600

    echo "[integration] Phase 2.3: serena reachable from proxy container at tcp://serena:9121"
    local proxy_cid
    proxy_cid=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${TEST_INSTALL}/docker-compose.yml" ps -q proxy 2>/dev/null)
    if [[ -z "${proxy_cid}" ]]; then
        echo "[integration] Phase 2.3 FAIL: cannot find proxy container id" >&2
        return 1
    fi
    if ! harness_docker exec "${proxy_cid}" timeout 5 bash -c "echo > /dev/tcp/serena/9121" 2>/dev/null; then
        echo "[integration] Phase 2.3 FAIL: serena unreachable from proxy at tcp://serena:9121" >&2
        # Same env contract as the build call above — required because this
        # invocation also pulls in the serena compose snippet.
        INSTALL_ROOT="${TEST_INSTALL}" \
            HARNESS_ALLOWLIST_PATH="${TEST_INSTALL}/.harness-allowlist" \
            docker compose --project-name "${PROJECT_NAME}" \
            -f "${TEST_INSTALL}/docker-compose.yml" \
            -f "${TEST_INSTALL}/state/mcp/serena/compose.yml" \
            --profile mcp logs --tail=30 serena >&2 2>/dev/null || true
        return 1
    fi
    echo "[integration] Phase 2.3: serena reachable on port 9121"

    echo "[integration] Phase 2.4: trigger agent launch + verify serena merged into agent MCP config"
    # The harness script writes the merged MCP config side-file
    # (.harness-mcp-servers.json) on every agent launch. Run a short headless
    # claude to refresh it, then assert serena is present.
    set +e
    cd "${TEST_WORKSPACE}/test-project" && timeout 60 \
        bash -c "HOME='${FAKE_HOME}' HARNESS_PROJECT_NAME='${PROJECT_NAME}' '${TEST_INSTALL}/harness' claude -p \"say hello\" >/dev/null 2>&1 < /dev/null"
    cd "${REPO_ROOT}"
    set -e
    local mcp_side_file="${TEST_INSTALL}/state/agent/home/.harness-mcp-servers.json"
    if [[ ! -f "${mcp_side_file}" ]]; then
        echo "[integration] Phase 2.4 FAIL: side file missing at ${mcp_side_file}" >&2
        return 1
    fi
    if ! jq -e '.mcpServers.serena' "${mcp_side_file}" >/dev/null 2>&1; then
        echo "[integration] Phase 2.4 FAIL: serena not in side file" >&2
        cat "${mcp_side_file}" >&2 2>/dev/null
        return 1
    fi
    echo "[integration] Phase 2.4: serena entry present in agent MCP config"

    echo "[integration] Phase 2.5: serena sees test project at /workspaces/projects/.../test-project/"
    # Projects live under /workspaces/projects/<host-relative-path>. The
    # workspace dir under TEST_ROOT was bind-mounted via HARNESS_PROJECTS_ROOT,
    # so test-project/ ends up at /workspaces/projects/test-project/.
    if ! harness_docker exec harness-serena ls /workspaces/projects/test-project/src/calculator/core.py >/dev/null 2>&1; then
        echo "[integration] Phase 2.5 FAIL: serena cannot see test project files" >&2
        harness_docker exec harness-serena ls /workspaces/projects 2>&1 | head -20 >&2 || true
        return 1
    fi
    echo "[integration] Phase 2.5: serena sees test project files"

    echo "[integration] Phase 2.6: TUI claude invokes serena (tool-call rendering)"
    phase_2_tui_test || return 1

    echo "[integration] Phase 2.7: serena down/up cycle"
    harness_call mcp down serena >/dev/null
    harness_call mcp up serena >/dev/null
    test_wait_for_healthy "${PROJECT_NAME}" serena 120
    if ! harness_docker exec "${proxy_cid}" timeout 5 bash -c "echo > /dev/tcp/serena/9121" 2>/dev/null; then
        echo "[integration] Phase 2.7 FAIL: serena unreachable after down/up cycle" >&2
        return 1
    fi
    echo "[integration] Phase 2.7: serena cycle works"

    echo "[integration] Phase 2.8: enable/disable state flag"
    harness_call mcp disable serena >/dev/null
    if ! harness_call mcp list | grep -qE "^serena[[:space:]]+installed-disabled"; then
        echo "[integration] Phase 2.8 FAIL: serena not in installed-disabled state" >&2
        harness_call mcp list >&2
        return 1
    fi
    harness_call mcp enable serena >/dev/null
    if ! harness_call mcp list | grep -qE "^serena[[:space:]]+installed-enabled"; then
        echo "[integration] Phase 2.8 FAIL: serena not back in installed-enabled state" >&2
        harness_call mcp list >&2
        return 1
    fi
    echo "[integration] Phase 2.8: enable/disable state flag works"

    echo "[integration] Phase 2.9: harness mcp uninstall serena --force"
    # First stop the running container so uninstall has nothing to evict.
    harness_call mcp down serena >/dev/null 2>&1 || true
    harness_call mcp uninstall serena --force >/dev/null
    local data_dir="${TEST_INSTALL}/state/mcp/serena/data"
    if [[ -d "${data_dir}" ]]; then
        echo "[integration] Phase 2.9: serena uninstall preserved data at ${data_dir}"
    else
        echo "[integration] Phase 2.9: serena uninstall complete (data dir empty/absent)"
    fi
    if [[ -f "${TEST_INSTALL}/state/mcp/serena/compose.yml" ]]; then
        echo "[integration] Phase 2.9 FAIL: compose.yml still present after uninstall" >&2
        return 1
    fi

    echo "[integration] Phase 2 (Serena): all checks passed"
}

# Phase 2.6 helper: spin up a TUI claude container against the test project,
# walk first-run dialogs, send a serena-bait prompt, and verify the pane
# shows evidence of the tool-call (the keyword "find_symbol" or
# "Calculator" — the mock returns a fixture containing both).
phase_2_tui_test() {
    # Phase 18 dropped tmux from agent launch and Phase 19 deleted the
    # tmux-based test driver, so we drive claude in print mode for this
    # check. The fixture (04_serena_find_symbol) returns a tool-call JSON
    # block referencing the Calculator class — the assertion is the same
    # regardless of whether the surface is a TUI pane or a print-mode
    # stdout stream.
    local out rc
    set +e
    out=$(cd "${TEST_WORKSPACE}/test-project" && timeout 120 \
        env HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
        "${TEST_INSTALL}/harness" claude -p \
        "Use serena to find the Calculator class symbol in this project" \
        2>&1 < /dev/null)
    rc=$?
    set -e

    if (( rc != 0 )); then
        echo "[integration] Phase 2.6 FAIL: harness claude -p exited ${rc}" >&2
        echo "--- output (last 60 lines) ---" >&2
        tail -60 <<<"${out}" >&2
        echo "--- mockupstream logs ---" >&2
        docker logs "${MOCK_NAME}" 2>&1 | tail -40 >&2 || true
        return 1
    fi

    if ! grep -qE "find_symbol|Calculator" <<<"${out}"; then
        echo "[integration] Phase 2.6 FAIL: print-mode output shows no evidence of serena tool invocation" >&2
        echo "--- output (last 60 lines) ---" >&2
        tail -60 <<<"${out}" >&2
        echo "--- mockupstream logs ---" >&2
        docker logs "${MOCK_NAME}" 2>&1 | tail -40 >&2 || true
        return 1
    fi
    echo "[integration] Phase 2.6: serena tool-call evidence present in print-mode output"
}

# === Phase 3: Graphify (skill) end-to-end ===================================

phase_3_graphify() {
    echo "[integration] Phase 3.1: launching long-lived agent container for graphify"
    docker rm -f "${GRAPHIFY_AGENT_NAME}" >/dev/null 2>&1 || true
    # We override the entrypoint with a small inline shell that:
    #   1. (as root) runs init-firewall.sh so pipx egress is gated by
    #      our test allowlist (matches what the real harness does).
    #   2. (as root) remaps the harness uid/gid to the host user so files
    #      written into bind mounts land owned by the host user — Phase
    #      3.10 asserts on this.
    #   3. (as root) seeds /home/harness from /etc/skel/harness on first
    #      run, mirroring the agent entrypoint's skel-seed step.
    #   4. exec gosu harness sleep — keeps the container alive so we can
    #      harness_docker exec into it for pipx install + graphify runs.
    local mnt_workspace mnt_home mnt_allowlist
    mnt_workspace=$(harness_docker_path "${TEST_WORKSPACE}/test-project")
    mnt_home=$(harness_docker_path "${TEST_INSTALL}/state/agent/home")
    mnt_allowlist=$(harness_docker_path "${TEST_INSTALL}/.harness-allowlist")
    harness_docker run -d \
        --name "${GRAPHIFY_AGENT_NAME}" \
        --network "${NETWORK}" \
        --user 0:0 \
        --cap-add NET_ADMIN \
        --cap-add NET_RAW \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        -v "${mnt_workspace}:/workspace" \
        -v "${mnt_home}:/home/harness" \
        -v "${mnt_allowlist}:/etc/harness/allowlist:ro" \
        -w /workspace \
        --label "harness.agent=true" \
        --label "harness.project=${PROJECT_NAME}" \
        --entrypoint /bin/bash \
        harness-agent:latest \
        -c '
            set -e
            /usr/local/bin/init-firewall.sh
            if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
                current_uid=$(id -u harness 2>/dev/null || echo "")
                current_gid=$(id -g harness 2>/dev/null || echo "")
                if [[ "${current_uid}" != "${HOST_UID}" || "${current_gid}" != "${HOST_GID}" ]]; then
                    groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
                        || groupadd -g "${HOST_GID}" -o harness
                    usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness
                    chown -R "${HOST_UID}:${HOST_GID}" /home/harness 2>/dev/null || true
                fi
            fi
            if [[ ! -f /home/harness/.harness-home-initialized ]]; then
                if [[ -d /etc/skel/harness ]]; then
                    cp -an /etc/skel/harness/. /home/harness/ 2>/dev/null || true
                fi
                touch /home/harness/.harness-home-initialized 2>/dev/null || true
                chown -R harness:harness /home/harness 2>/dev/null || true
            fi
            exec gosu harness sleep 3600
        ' \
        >/dev/null

    # Give init-firewall + remap a moment to settle, then verify ready.
    sleep 5
    if [[ -z "$(docker ps -q -f "name=^${GRAPHIFY_AGENT_NAME}$" 2>/dev/null)" ]]; then
        echo "[integration] Phase 3.1 FAIL: agent container exited before sleep" >&2
        docker logs "${GRAPHIFY_AGENT_NAME}" 2>&1 | tail -30 >&2
        return 1
    fi
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            id harness >/dev/null 2>&1; then
        echo "[integration] Phase 3.1 FAIL: harness user not ready inside container" >&2
        return 1
    fi
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            which pipx >/dev/null 2>&1; then
        echo "[integration] Phase 3.1 FAIL: pipx not available in agent image" >&2
        return 1
    fi
    echo "[integration] Phase 3.1: agent container ready, pipx available"

    echo "[integration] Phase 3.2: pipx install graphifyy"
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            pipx install graphifyy >"${TEST_ROOT}/pipx-install.log" 2>&1; then
        echo "[integration] Phase 3.2 FAIL: pipx install graphifyy failed" >&2
        cat "${TEST_ROOT}/pipx-install.log" >&2
        return 1
    fi
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            test -x /home/harness/.local/bin/graphify; then
        echo "[integration] Phase 3.2 FAIL: graphify binary not at expected path" >&2
        harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            ls -la /home/harness/.local/bin/ >&2 2>/dev/null || true
        return 1
    fi
    echo "[integration] Phase 3.2: graphifyy installed via pipx"

    echo "[integration] Phase 3.3: install persisted to host bind mount"
    # pipx writes a symlink whose target is the in-container absolute path
    # (/home/harness/.local/pipx/venvs/graphifyy/bin/graphify), so `-x`
    # would follow the dangling-on-host link and report missing. Test for
    # the symlink itself and for the venv directory underneath — both
    # together prove the bind-mount captured the install.
    local host_bin="${TEST_INSTALL}/state/agent/home/.local/bin/graphify"
    local host_venv="${TEST_INSTALL}/state/agent/home/.local/pipx/venvs/graphifyy"
    if [[ ! -L "${host_bin}" || ! -d "${host_venv}" ]]; then
        echo "[integration] Phase 3.3 FAIL: graphify not visible on host bind mount" >&2
        ls -la "${TEST_INSTALL}/state/agent/home/.local/bin/" >&2 2>/dev/null || true
        ls -la "${TEST_INSTALL}/state/agent/home/.local/pipx/venvs/" >&2 2>/dev/null || true
        return 1
    fi
    echo "[integration] Phase 3.3: install persists to host bind mount"

    echo "[integration] Phase 3.4: graphify install (skill registration)"
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            /home/harness/.local/bin/graphify install \
            >"${TEST_ROOT}/graphify-install.log" 2>&1; then
        echo "[integration] Phase 3.4 FAIL: 'graphify install' returned non-zero" >&2
        cat "${TEST_ROOT}/graphify-install.log" >&2
        return 1
    fi
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            test -f /home/harness/.claude/skills/graphify/SKILL.md; then
        echo "[integration] Phase 3.4 FAIL: SKILL.md not at expected path after install" >&2
        harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            find /home/harness/.claude -type f >&2 2>/dev/null || true
        return 1
    fi
    echo "[integration] Phase 3.4: SKILL.md registered at ~/.claude/skills/graphify/SKILL.md"

    if harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            grep -q "graphify" /home/harness/.claude/CLAUDE.md 2>/dev/null; then
        echo "[integration] Phase 3.4: CLAUDE.md registration confirmed"
    else
        echo "[integration] Phase 3.4 NOTE: CLAUDE.md does not mention graphify (graphify version may not patch CLAUDE.md)"
    fi

    echo "[integration] Phase 3.5: graphify --help (CLI smoke)"
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            /home/harness/.local/bin/graphify --help >/dev/null 2>&1; then
        echo "[integration] Phase 3.5 FAIL: graphify --help fails" >&2
        return 1
    fi
    echo "[integration] Phase 3.5: graphify CLI functional"

    echo "[integration] Phase 3.6: graphify on test-project (tree-sitter parse + graph build)"
    # `graphify update <path>` is the pure-local subcommand that re-extracts
    # code files and (re)builds graph.json/graph.html/GRAPH_REPORT.md without
    # any LLM call. The bare `graphify .` form would error with
    # "unknown command '.'".
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            timeout 180 bash -c "cd /workspace && /home/harness/.local/bin/graphify update ." \
            >"${TEST_ROOT}/graphify-run.log" 2>&1; then
        echo "[integration] Phase 3.6 FAIL: 'graphify update .' on test project failed" >&2
        tail -60 "${TEST_ROOT}/graphify-run.log" >&2
        return 1
    fi
    echo "[integration] Phase 3.6: graphify run completed"

    echo "[integration] Phase 3.7: graphify-out/ exists in workspace on host"
    local out_dir="${TEST_WORKSPACE}/test-project/graphify-out"
    if [[ ! -d "${out_dir}" ]]; then
        echo "[integration] Phase 3.7 FAIL: graphify-out/ not created at ${out_dir}" >&2
        harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME}" \
            ls -la /workspace/ >&2 2>/dev/null || true
        return 1
    fi
    echo "[integration] Phase 3.7: graphify-out/ exists on host"

    echo "[integration] Phase 3.8: graph.json present"
    local graph_json="${out_dir}/graph.json"
    if [[ ! -f "${graph_json}" ]]; then
        echo "[integration] Phase 3.8 FAIL: graph.json not in ${out_dir}" >&2
        ls -la "${out_dir}" >&2
        return 1
    fi
    echo "[integration] Phase 3.8: graph.json present"

    echo "[integration] Phase 3.9: Calculator + ScientificCalculator symbols in graph.json"
    if ! grep -qE "Calculator(\b|[^a-zA-Z])" "${graph_json}"; then
        echo "[integration] Phase 3.9 FAIL: Calculator symbol not in graph.json" >&2
        head -200 "${graph_json}" >&2
        return 1
    fi
    if ! grep -q "ScientificCalculator" "${graph_json}"; then
        echo "[integration] Phase 3.9 FAIL: ScientificCalculator symbol not in graph.json" >&2
        head -200 "${graph_json}" >&2
        return 1
    fi
    echo "[integration] Phase 3.9: both Calculator classes present in graph.json"

    echo "[integration] Phase 3.10: file ownership matches host UID"
    local owner_uid file_uid
    owner_uid=$(stat -c '%u' "${out_dir}")
    if [[ "${owner_uid}" != "$(id -u)" ]]; then
        echo "[integration] Phase 3.10 FAIL: graphify-out/ owned by uid ${owner_uid}, expected $(id -u)" >&2
        return 1
    fi
    file_uid=$(stat -c '%u' "${graph_json}")
    if [[ "${file_uid}" != "$(id -u)" ]]; then
        echo "[integration] Phase 3.10 FAIL: graph.json owned by uid ${file_uid}, expected $(id -u)" >&2
        return 1
    fi
    echo "[integration] Phase 3.10: file ownership correct (host UID, not container uid 1000)"

    echo "[integration] Phase 3.11: persistence — fresh container, graphify still works"
    docker rm -f "${GRAPHIFY_AGENT_NAME}" >/dev/null 2>&1 || true
    local mnt_workspace2 mnt_home2 mnt_allowlist2
    mnt_workspace2=$(harness_docker_path "${TEST_WORKSPACE}/test-project")
    mnt_home2=$(harness_docker_path "${TEST_INSTALL}/state/agent/home")
    mnt_allowlist2=$(harness_docker_path "${TEST_INSTALL}/.harness-allowlist")
    harness_docker run -d \
        --name "${GRAPHIFY_AGENT_NAME_2}" \
        --network "${NETWORK}" \
        --user 0:0 \
        --cap-add NET_ADMIN \
        --cap-add NET_RAW \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        -v "${mnt_workspace2}:/workspace" \
        -v "${mnt_home2}:/home/harness" \
        -v "${mnt_allowlist2}:/etc/harness/allowlist:ro" \
        -w /workspace \
        --label "harness.agent=true" \
        --label "harness.project=${PROJECT_NAME}" \
        --entrypoint /bin/bash \
        harness-agent:latest \
        -c '
            set -e
            /usr/local/bin/init-firewall.sh
            if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
                groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
                    || groupadd -g "${HOST_GID}" -o harness
                usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness 2>/dev/null || true
            fi
            exec gosu harness sleep 600
        ' \
        >/dev/null
    sleep 5
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME_2}" \
            /home/harness/.local/bin/graphify --help >/dev/null 2>&1; then
        echo "[integration] Phase 3.11 FAIL: graphify not callable in fresh container" >&2
        return 1
    fi
    if ! harness_docker exec --user harness "${GRAPHIFY_AGENT_NAME_2}" \
            test -f /home/harness/.claude/skills/graphify/SKILL.md; then
        echo "[integration] Phase 3.11 FAIL: SKILL.md not in fresh container" >&2
        return 1
    fi
    echo "[integration] Phase 3.11: graphify and skill persist across container rebuild"
    docker rm -f "${GRAPHIFY_AGENT_NAME_2}" >/dev/null 2>&1 || true

    echo "[integration] Phase 3 (Graphify): all 11 checks passed"
}

# === Phase 4: cross-test invariants =========================================

phase_4_cross_invariants() {
    echo "[integration] Phase 4.1: harness doctor returns 0"
    if ! harness_call doctor >"${TEST_ROOT}/doctor.log" 2>&1; then
        echo "[integration] Phase 4.1 FAIL: doctor returned non-zero" >&2
        tail -60 "${TEST_ROOT}/doctor.log" >&2
        return 1
    fi
    echo "[integration] Phase 4.1: doctor reports green"

    echo "[integration] Phase 4.2: state directory layout intact"
    # Phase 13a unified claude and opencode into a single agent/home tree.
    local d
    for d in output agent/home ollama-data mcp; do
        if [[ ! -d "${TEST_INSTALL}/state/${d}" ]]; then
            echo "[integration] Phase 4.2 FAIL: missing ${TEST_INSTALL}/state/${d}" >&2
            return 1
        fi
    done
    echo "[integration] Phase 4.2: state layout intact"

    echo "[integration] Phase 4: invariants pass"
}

# === Drive ==================================================================

test_section "Phase 1: stack setup with mock upstream"
phase_1_stack_setup

test_section "Phase 2: Serena (HTTP MCP) end-to-end"
phase_2_serena

test_section "Phase 3: Graphify (skill) end-to-end"
phase_3_graphify

test_section "Phase 4: cross-test invariants"
phase_4_cross_invariants

echo
echo "============================================================"
echo " INTEGRATION TEST PASSED"
echo "============================================================"
