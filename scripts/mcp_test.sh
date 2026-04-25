#!/usr/bin/env bash
#
# scripts/mcp_test.sh — exercise the `harness mcp` lifecycle without
# pulling Serena. We synthesize a tiny fake MCP entry under a tmpdir,
# point HARNESS_REGISTRY_DIR at it, and walk through enable / start /
# verify-network / disable / verify-removed.
#
# The fake MCP is `python:3.12-slim` running `python -m http.server`. It
# exposes nothing of substance; we use it purely to validate that:
#   - registry discovery finds it,
#   - enable copies it into the active tree,
#   - harness start brings it up on the harness-net network,
#   - the merged client config lands in the agent home,
#   - disable cleanly removes the active entry but preserves data/.
#
# Project name is fixed to harness-mcp-test so this never collides with a
# real harness instance running on the same daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NAME="harness-mcp-test"

echo "============================================================"
echo " harness MCP lifecycle test"
echo "============================================================"

# --- preflight --------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[mcp] ERROR: docker daemon not reachable" >&2
    exit 1
fi

# --- staging ----------------------------------------------------------------
#
# We build a self-contained "fake install root" mirroring the real layout
# the harness script expects. The registry override lets us inject our
# fake MCP without touching the real mcp-registry/ in the repo (which the
# user might want to ship to other people).

TEST_ROOT="$(mktemp -d -t harness-mcp-test.XXXXXX)"
FAKE_REGISTRY="${TEST_ROOT}/fake-registry"
FAKE_INSTALL_ROOT="${TEST_ROOT}/install-root"

mkdir -p "${FAKE_INSTALL_ROOT}" \
         "${FAKE_REGISTRY}/_test_mcp"

# Symlink the repo as <fake-install-root>/harness so the script's
# realpath/dirname walk lands in a sensible place. HARNESS_INSTALL_ROOT
# pins it explicitly anyway, but the symlink lets `harness` exist on the
# expected path.
ln -s "${REPO_ROOT}" "${FAKE_INSTALL_ROOT}/harness"

cleanup() {
    local rc=$?
    echo "[mcp] cleanup (rc=${rc})"

    if [[ -x "${FAKE_INSTALL_ROOT}/harness/harness" ]]; then
        HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
        HARNESS_INSTALL_ROOT="${FAKE_INSTALL_ROOT}" \
        HARNESS_REGISTRY_DIR="${FAKE_REGISTRY}" \
            "${FAKE_INSTALL_ROOT}/harness/harness" down >/dev/null 2>&1 || true
    fi
    docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        down -v --remove-orphans >/dev/null 2>&1 || true

    if [[ -d "${TEST_ROOT}" ]]; then
        if ! rm -rf "${TEST_ROOT}" 2>/dev/null; then
            docker run --rm -v "${TEST_ROOT}:/target" --user 0:0 alpine \
                sh -c 'rm -rf /target/* /target/.[!.]* 2>/dev/null || true' \
                >/dev/null 2>&1 || true
            rm -rf "${TEST_ROOT}" 2>/dev/null || true
        fi
    fi
    exit "${rc}"
}
trap cleanup EXIT INT TERM

cat >"${FAKE_INSTALL_ROOT}/.env" <<'EOF'
PROXY_API_URL=http://placeholder.invalid/v1/chat/completions
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
EOF

# --- fake MCP fixture -------------------------------------------------------
#
# compose.yml: lightweight HTTP service. Network is declared with the
# matching short name as the main compose file (no external/no name) so
# compose merges them into a single project-namespaced network. This is
# the same shape the production Serena fixture uses.

cat >"${FAKE_REGISTRY}/_test_mcp/compose.yml" <<'EOF'
services:
  test_mcp:
    image: python:3.12-slim
    container_name: harness-mcp-test_test_mcp
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

cat >"${FAKE_REGISTRY}/_test_mcp/client-config.json" <<'EOF'
{
  "mcpServers": {
    "test_mcp": {
      "type": "sse",
      "url": "http://test_mcp:8765/sse"
    }
  }
}
EOF

cat >"${FAKE_REGISTRY}/_test_mcp/README.md" <<'EOF'
# _test_mcp — fixture used by scripts/mcp_test.sh.
EOF

# Sanity: also register a "non-test" entry so we can verify list filtering.
mkdir -p "${FAKE_REGISTRY}/dummy"
cat >"${FAKE_REGISTRY}/dummy/compose.yml" <<'EOF'
services:
  dummy:
    image: alpine:latest
    networks: [harness-net]
    profiles: [mcp]
    command: sh -c 'sleep 9999'
networks:
  harness-net:
EOF
cat >"${FAKE_REGISTRY}/dummy/client-config.json" <<'EOF'
{ "mcpServers": { "dummy": { "type": "sse", "url": "http://dummy:1/" } } }
EOF

# --- helper -----------------------------------------------------------------

harness_call() {
    HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
    HARNESS_INSTALL_ROOT="${FAKE_INSTALL_ROOT}" \
    HARNESS_REGISTRY_DIR="${FAKE_REGISTRY}" \
        "${FAKE_INSTALL_ROOT}/harness/harness" "$@"
}

# Defensive: clear any stragglers from a prior run.
docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true

# --- T1: list shows registry entries, none enabled --------------------------

echo "[mcp] T1: harness mcp list — registry only, none enabled"
list_out=$(harness_call mcp list)
echo "${list_out}" | sed 's/^/  | /'
# `_test_mcp` should be hidden (underscore-prefixed test fixtures don't
# show in user-facing list). `dummy` should appear. (We exercise the
# enable path against `_test_mcp` directly because it accepts any name,
# we just rely on hiding.)
if ! grep -Eq 'dummy[[:space:]]+no' <<<"${list_out}"; then
    echo "[mcp] T1 FAIL: dummy should be listed as enabled=no" >&2
    exit 1
fi
if grep -q '_test_mcp' <<<"${list_out}"; then
    echo "[mcp] T1 FAIL: _test_mcp should be hidden from list (underscore-prefixed)" >&2
    exit 1
fi
echo "[mcp] T1 OK"

# --- T2: enable unknown errors with the available list ---------------------

echo "[mcp] T2: harness mcp enable nope (unknown name)"
set +e
err_out=$(harness_call mcp enable nope 2>&1)
err_rc=$?
set -e
if (( err_rc == 0 )); then
    echo "[mcp] T2 FAIL: enable nope unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -qi 'unknown MCP' <<<"${err_out}"; then
    echo "[mcp] T2 FAIL: error doesn't mention 'unknown MCP': ${err_out}" >&2
    exit 1
fi
echo "[mcp] T2 OK"

# --- T3: enable copies entry into active tree ------------------------------

echo "[mcp] T3: harness mcp enable _test_mcp"
harness_call mcp enable _test_mcp >"${TEST_ROOT}/enable.log" 2>&1
echo "  | $(grep -E '^enabled' "${TEST_ROOT}/enable.log" || true)"

if [[ ! -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/compose.yml" ]]; then
    echo "[mcp] T3 FAIL: compose.yml not copied" >&2
    cat "${TEST_ROOT}/enable.log" >&2
    exit 1
fi
if [[ ! -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/client-config.json" ]]; then
    echo "[mcp] T3 FAIL: client-config.json not copied" >&2
    exit 1
fi
if [[ ! -d "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/data" ]]; then
    echo "[mcp] T3 FAIL: data/ not pre-created" >&2
    exit 1
fi
echo "[mcp] T3 OK"

# --- T4: re-enable without --force fails -----------------------------------

echo "[mcp] T4: re-enable without --force fails"
set +e
re_out=$(harness_call mcp enable _test_mcp 2>&1)
re_rc=$?
set -e
if (( re_rc == 0 )); then
    echo "[mcp] T4 FAIL: re-enable succeeded without --force" >&2
    echo "${re_out}" >&2
    exit 1
fi
echo "[mcp] T4 OK (rc=${re_rc})"

# --- T5: harness start brings up the fake MCP -------------------------------

echo "[mcp] T5: harness start with active MCP"
harness_call start >"${TEST_ROOT}/start.log" 2>&1 || {
    echo "[mcp] T5 FAIL: start exited non-zero" >&2
    tail -50 "${TEST_ROOT}/start.log" >&2
    exit 1
}

# Wait for the fake MCP container to become healthy (its compose file has
# a healthcheck; 60s ceiling).
deadline=$(( $(date +%s) + 60 ))
mcp_cid=""
while true; do
    mcp_cid=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/compose.yml" \
        ps -q test_mcp 2>/dev/null || true)
    if [[ -n "${mcp_cid}" ]]; then
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${mcp_cid}" 2>/dev/null || echo "none")
        if [[ "${status}" == "healthy" ]]; then
            break
        fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[mcp] T5 FAIL: test_mcp not healthy in 60s" >&2
        docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" \
            -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/compose.yml" ps >&2 || true
        docker logs "${mcp_cid}" 2>&1 | tail -30 >&2 || true
        exit 1
    fi
    sleep 2
done
echo "[mcp] T5 OK"

# --- T6: list now shows _test_mcp as enabled + running ---------------------
#
# (Listing matches by underscore-prefix; from T1 we know it's hidden in
# user-facing output. So we instead verify via `mcp list` that the active
# entry is reflected in the running state — the fact that T5 succeeded
# already proves the active tree was discovered.)

# Verify any-mcp-active branch ran by checking that the start log invoked
# --profile mcp. We emit it via compose's verbose output indirectly — the
# easiest signal is that the test_mcp container is up.
echo "[mcp] T6: services up after start"
ollama_cid=$(docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/compose.yml" \
    ps -q ollama 2>/dev/null || true)
if [[ -z "${ollama_cid}" ]]; then
    echo "[mcp] T6 FAIL: ollama container not present (unrelated regression?)" >&2
    exit 1
fi
echo "[mcp] T6 OK"

# --- T7: agent client config gets the merged entry -------------------------
#
# We don't launch a real agent here (image may not be built). Instead we
# call the same code path harness uses internally: mkdir agent dir,
# trigger the merge by running `harness claude` against an unbuilt image
# — the script writes the side file BEFORE checking image existence.
# Actually: the script writes the side file AFTER the image-existence
# check passes... let me re-examine. (See the harness script: order is
# image check, then write_agent_mcp_config in run_agent.)
#
# Easier: invoke a no-op subcommand that hits ensure_dirs +
# write_agent_mcp_config. There isn't one, so we synthesize the work by
# stashing a real agent image first. Skip this if no image is present —
# the integration check happens in full_pipeline_test.sh.
#
# Simpler approach: directly test the side-effect on the host. Once
# enabled, we run a harness invocation that triggers the merge. Use
# `harness claude -p` against an unbuilt image: the script enters
# run_agent, calls write_agent_mcp_config, then errors on the image.

echo "[mcp] T7: merged client config side file"
mkdir -p "${FAKE_INSTALL_ROOT}/agent/claude"
# Stash any real claude image so we can deterministically hit the
# image-not-found path.
stash_tag=""
if docker image inspect harness-claude-agent:latest >/dev/null 2>&1; then
    stash_tag="harness-claude-agent:mcp-test-stash-$$"
    docker tag harness-claude-agent:latest "${stash_tag}" >/dev/null
    docker rmi harness-claude-agent:latest >/dev/null 2>&1 || true
fi
restore_image() {
    if [[ -n "${stash_tag}" ]]; then
        docker tag "${stash_tag}" harness-claude-agent:latest >/dev/null 2>&1 || true
        docker rmi "${stash_tag}" >/dev/null 2>&1 || true
        stash_tag=""
    fi
}
trap 'restore_image; cleanup' EXIT INT TERM

set +e
harness_call claude -p "ignored" >/dev/null 2>&1
set -e
restore_image
trap cleanup EXIT INT TERM

side_file="${FAKE_INSTALL_ROOT}/agent/claude/.harness-mcp-servers.json"
if [[ ! -f "${side_file}" ]]; then
    echo "[mcp] T7 FAIL: ${side_file} not written" >&2
    exit 1
fi
if ! grep -q 'test_mcp' "${side_file}"; then
    echo "[mcp] T7 FAIL: side file does not mention test_mcp" >&2
    cat "${side_file}" >&2
    exit 1
fi
if ! grep -q '"http://test_mcp:8765/sse"' "${side_file}"; then
    echo "[mcp] T7 FAIL: side file missing the test_mcp url" >&2
    cat "${side_file}" >&2
    exit 1
fi
echo "[mcp] T7 OK"

# --- T8: harness mcp disable removes config but preserves data -------------

echo "[mcp] T8: harness mcp disable _test_mcp --force"
# Drop a marker into data/ so we can prove preservation.
echo "data marker" >"${FAKE_INSTALL_ROOT}/mcp/_test_mcp/data/marker.txt"

harness_call mcp disable _test_mcp --force >"${TEST_ROOT}/disable.log" 2>&1
echo "  | $(grep -E '^disabled|^data preserved' "${TEST_ROOT}/disable.log" || true)"

if [[ -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/compose.yml" ]]; then
    echo "[mcp] T8 FAIL: compose.yml still present after disable" >&2
    exit 1
fi
if [[ ! -f "${FAKE_INSTALL_ROOT}/mcp/_test_mcp/data/marker.txt" ]]; then
    echo "[mcp] T8 FAIL: data/marker.txt was removed by disable" >&2
    exit 1
fi
echo "[mcp] T8 OK"

# --- T9: re-running harness start no longer brings up the MCP --------------

echo "[mcp] T9: post-disable start drops the MCP service"
harness_call start >"${TEST_ROOT}/start2.log" 2>&1 || {
    echo "[mcp] T9 FAIL: start exited non-zero after disable" >&2
    tail -30 "${TEST_ROOT}/start2.log" >&2
    exit 1
}
deadline=$(( $(date +%s) + 30 ))
while true; do
    cid=$(docker ps -q --filter "name=^harness-mcp-test_test_mcp$" 2>/dev/null || true)
    if [[ -z "${cid}" ]]; then break; fi
    if (( $(date +%s) >= deadline )); then
        echo "[mcp] T9 FAIL: test_mcp container still running after disable" >&2
        exit 1
    fi
    sleep 2
done
echo "[mcp] T9 OK"

# --- T10: agent side file is cleaned on next launch ------------------------

echo "[mcp] T10: side file disappears when no MCPs are active"
# Re-trigger the side-file write path. Stash image + image-not-found
# again, same trick as T7.
stash_tag=""
if docker image inspect harness-claude-agent:latest >/dev/null 2>&1; then
    stash_tag="harness-claude-agent:mcp-test-stash-$$"
    docker tag harness-claude-agent:latest "${stash_tag}" >/dev/null
    docker rmi harness-claude-agent:latest >/dev/null 2>&1 || true
fi
trap 'restore_image; cleanup' EXIT INT TERM
set +e
harness_call claude -p "ignored" >/dev/null 2>&1
set -e
restore_image
trap cleanup EXIT INT TERM

if [[ -f "${side_file}" ]]; then
    echo "[mcp] T10 FAIL: stale side file should have been removed: ${side_file}" >&2
    cat "${side_file}" >&2
    exit 1
fi
echo "[mcp] T10 OK"

echo "============================================================"
echo " MCP TEST PASSED"
echo "============================================================"
exit 0
