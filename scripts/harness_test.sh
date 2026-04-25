#!/usr/bin/env bash
#
# Phase 4 harness management script test.
#
# Exercises the non-interactive subcommands of `harness`:
#   - start      (brings up services in test mode with mock upstream)
#   - list       (no agents -> prints "no harness agents running")
#   - logs       (follows service logs, killed by timeout)
#   - down       (tears services down)
#
# Interactive subcommands (claude, opencode, attach with picker, stop with
# picker) require a TTY and live upstream — those are validated by
# scripts/agent_test.sh and by the manual smoke checks documented in the
# Phase 4 commit message. They are NOT covered here.
#
# Other smoke checks:
#   - build_zip.sh produces a valid distribution zip
#   - install.sh's PATH-rcfile append is idempotent
#
# A separate compose project name (HARNESS_PROJECT_NAME=harness-mgmt-test)
# is used so this test never collides with a real harness instance the
# developer may have running on the same daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NAME="harness-mgmt-test"

echo "============================================================"
echo " harness Phase 4 management script test"
echo "============================================================"

# --- preflight ---------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[harness-test] ERROR: docker daemon not reachable" >&2
    exit 1
fi

# --- staging area ------------------------------------------------------------
#
# Lay out a fake install root that mirrors the real one:
#   $TEST_ROOT/
#     .env                    (test config, points proxy at mockupstream)
#     harness/                (symlink to the real repo so 'harness' subcommands resolve)
#     output/, agent/, ollama-data/  (created on demand by the script)
#
# We also drop a docker-compose override into $TEST_ROOT that adds the mock
# upstream service. The harness script itself doesn't know about this file;
# we add it via a wrapper that prepends -f to the compose call. To keep
# things simple, we instead invoke `harness` for `start/down/list/logs` and
# add the mock upstream by copying the override into the compose file path
# via a separate compose call. But that complicates the test...
#
# Simpler: skip the mock upstream entirely. The harness script's start path
# does `compose up -d --build`. With a working .env (even with placeholder
# upstream values) ollama and proxy will start. Proxy will fail to forward
# any real request — but we don't make any real requests in this test.
# Healthchecks may or may not pass depending on whether proxy's /health
# endpoint requires upstream; per Phase 2's proxy.py /health doesn't dial
# upstream, so it returns OK and the healthcheck succeeds.

TEST_ROOT="$(mktemp -d -t harness-mgmt-test.XXXXXX)"

cleanup() {
    echo "[harness-test] cleanup"
    if [[ -n "${TEST_ROOT:-}" && -d "${TEST_ROOT}" ]]; then
        # Tear services down via the harness script if start succeeded.
        if [[ -x "${TEST_ROOT}/harness/harness" ]]; then
            HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
                "${TEST_ROOT}/harness/harness" down >/dev/null 2>&1 || true
        fi
        # Belt-and-braces: remove the project's compose state directly.
        docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" \
            down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf "${TEST_ROOT}"
    fi
}
trap cleanup EXIT INT TERM

# Symlink so the harness script's realpath/dirname logic resolves correctly.
ln -s "${REPO_ROOT}" "${TEST_ROOT}/harness"

cat >"${TEST_ROOT}/.env" <<'EOF'
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

# Convenience: every invocation in this test file shares the same env vars.
# HARNESS_INSTALL_ROOT pins the install root explicitly. The symlink at
# ${TEST_ROOT}/harness would otherwise cause the script's realpath/dirname
# walk to land in the real repo's parent, where there's no .env.
HARNESS_BIN="${TEST_ROOT}/harness/harness"
export HARNESS_PROJECT_NAME="${PROJECT_NAME}"
export HARNESS_INSTALL_ROOT="${TEST_ROOT}"

# Defensive: clear stale state.
docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true

# --- Test 1: harness start brings services up -------------------------------

echo "[harness-test] T1: harness start"
"${HARNESS_BIN}" start >/dev/null

# Wait up to 60s for both services to become healthy.
deadline=$(( $(date +%s) + 60 ))
while true; do
    ollama_id=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q ollama 2>/dev/null || true)
    proxy_id=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${ollama_id}" && -n "${proxy_id}" ]]; then
        proxy_status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        ollama_status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${ollama_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" && "${ollama_status}" == "healthy" ]]; then
            break
        fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[harness-test] T1 FAIL: services not healthy in 60s" >&2
        docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
        exit 1
    fi
    sleep 2
done
echo "[harness-test] T1 OK: ollama + proxy healthy"

# --- Test 2: harness list with no agents ------------------------------------

echo "[harness-test] T2: harness list"
list_out=$("${HARNESS_BIN}" list)
if [[ "${list_out}" != "no harness agents running" ]]; then
    echo "[harness-test] T2 FAIL: expected 'no harness agents running', got: ${list_out}" >&2
    exit 1
fi
echo "[harness-test] T2 OK"

# --- Test 3: harness logs ---------------------------------------------------
#
# Follows logs; we kill it after 5s. timeout exits 124 on success.

echo "[harness-test] T3: harness logs ollama (timeout 5s)"
set +e
logs_out=$(timeout 5 "${HARNESS_BIN}" logs ollama 2>&1)
logs_rc=$?
set -e
if (( logs_rc != 124 && logs_rc != 0 )); then
    echo "[harness-test] T3 FAIL: harness logs exited with rc=${logs_rc}" >&2
    echo "${logs_out}" | tail -20 >&2
    exit 1
fi
if [[ -z "${logs_out}" ]]; then
    echo "[harness-test] T3 FAIL: harness logs produced no output" >&2
    exit 1
fi
echo "[harness-test] T3 OK"

# --- Test 4: harness down ---------------------------------------------------

echo "[harness-test] T4: harness down"
"${HARNESS_BIN}" down >/dev/null
# After down, no ollama/proxy containers should remain for this project.
remaining=$(docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q 2>/dev/null || true)
if [[ -n "${remaining}" ]]; then
    echo "[harness-test] T4 FAIL: containers still present after down" >&2
    docker compose --project-name "${PROJECT_NAME}" -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
    exit 1
fi
echo "[harness-test] T4 OK"

# --- Test 5: harness help -------------------------------------------------

echo "[harness-test] T5: harness help mentions all subcommands"
help_out=$("${HARNESS_BIN}" help)
for cmd in start down update upgrade logs claude opencode list attach stop mcp doctor; do
    if ! grep -q "$cmd" <<<"${help_out}"; then
        echo "[harness-test] T5 FAIL: help text missing '${cmd}'" >&2
        exit 1
    fi
done
echo "[harness-test] T5 OK"

# --- Test 5b: mcp subcommands (no docker work) ------------------------------
#
# These tests exercise argument parsing and registry/active-tree filesystem
# logic without actually bringing up MCP services — that's the job of
# scripts/mcp_test.sh. We override HARNESS_REGISTRY_DIR with an empty dir
# to assert the empty-registry path, then point it at a tmp registry to
# verify the populated path.

echo "[harness-test] T5b: harness mcp subcommands"

empty_reg=$(mktemp -d -t harness-empty-reg.XXXXXX)
populated_reg=$(mktemp -d -t harness-populated-reg.XXXXXX)
mkdir -p "${populated_reg}/foo"
cat >"${populated_reg}/foo/compose.yml" <<'EOF'
services:
  foo:
    image: alpine
    networks: [harness-net]
    profiles: [mcp]
networks:
  harness-net:
EOF
cat >"${populated_reg}/foo/client-config.json" <<'EOF'
{ "mcpServers": { "foo": { "type": "sse", "url": "http://foo:1/" } } }
EOF

cleanup_mcp_dirs() {
    rm -rf "${empty_reg}" "${populated_reg}"
}
trap 'cleanup_mcp_dirs; cleanup' EXIT INT TERM

# 5b.1: empty registry — list reports nothing.
empty_list=$(HARNESS_REGISTRY_DIR="${empty_reg}" "${HARNESS_BIN}" mcp list)
if ! grep -qi 'no MCP entries' <<<"${empty_list}"; then
    echo "[harness-test] T5b FAIL: empty registry should report 'no MCP entries'" >&2
    echo "${empty_list}" >&2
    exit 1
fi

# 5b.2: populated registry — foo appears as enabled=no.
pop_list=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp list)
if ! grep -Eq 'foo[[:space:]]+no' <<<"${pop_list}"; then
    echo "[harness-test] T5b FAIL: populated registry should list foo enabled=no" >&2
    echo "${pop_list}" >&2
    exit 1
fi

# 5b.3: enable unknown errors with available list.
set +e
unk_out=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp enable nope 2>&1)
unk_rc=$?
set -e
if (( unk_rc == 0 )); then
    echo "[harness-test] T5b FAIL: enable nope unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -qi 'unknown MCP' <<<"${unk_out}"; then
    echo "[harness-test] T5b FAIL: enable error doesn't mention 'unknown MCP'" >&2
    echo "${unk_out}" >&2
    exit 1
fi

# 5b.4: enable + disable round-trip on the host fs (no docker).
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp enable foo >/dev/null
[[ -f "${TEST_ROOT}/mcp/foo/compose.yml" ]] || { echo "[harness-test] T5b FAIL: foo not enabled to active tree" >&2; exit 1; }
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp disable foo --force >/dev/null
[[ -f "${TEST_ROOT}/mcp/foo/compose.yml" ]] && { echo "[harness-test] T5b FAIL: disable did not remove compose.yml" >&2; exit 1; }

cleanup_mcp_dirs
trap cleanup EXIT INT TERM

echo "[harness-test] T5b OK"

# --- Test 6: build_zip.sh produces a valid zip ------------------------------

echo "[harness-test] T6: build_zip.sh"
bash "${REPO_ROOT}/scripts/build_zip.sh" >/dev/null
zip_path="${REPO_ROOT}/dist/harness-distribution.zip"
if [[ ! -f "${zip_path}" ]]; then
    echo "[harness-test] T6 FAIL: zip not produced at ${zip_path}" >&2
    exit 1
fi
zip_listing=$(unzip -l "${zip_path}")
for f in install.sh .env README.md; do
    if ! grep -q "${f}" <<<"${zip_listing}"; then
        echo "[harness-test] T6 FAIL: zip missing ${f}" >&2
        echo "${zip_listing}" >&2
        exit 1
    fi
done
echo "[harness-test] T6 OK"

# --- Test 7: install.sh PATH append is idempotent ---------------------------
#
# Synthesize the exact append-to-rcfile branch from install.sh: if grep finds
# an existing .local/bin reference we leave the file alone. We test it by
# simulating two install runs into a fake HOME.

echo "[harness-test] T7: install.sh PATH-append idempotency"
fake_home=$(mktemp -d -t harness-fake-home.XXXXXX)
trap 'rm -rf "${fake_home}"' RETURN || true

rcfile="${fake_home}/.bashrc"
touch "${rcfile}"

# Emulate the install.sh append logic directly.
append_path() {
    if grep -q '\.local/bin' "${rcfile}"; then
        return 0
    fi
    {
        printf '\n# Added by harness installer\n'
        printf 'export PATH="$HOME/.local/bin:$PATH"\n'
    } >>"${rcfile}"
}

append_path
append_path
append_path

count=$(grep -c '\.local/bin' "${rcfile}")
if (( count != 1 )); then
    echo "[harness-test] T7 FAIL: expected exactly 1 .local/bin reference in rcfile, got ${count}" >&2
    cat "${rcfile}" >&2
    rm -rf "${fake_home}"
    exit 1
fi
rm -rf "${fake_home}"
echo "[harness-test] T7 OK"

# --- Test 8: harness doctor (services down) --------------------------------
#
# At this point T4 has torn services down. Doctor should run through every
# section, mark runtime checks as skipped/warned (no network, no containers),
# and exit non-zero only if a [config] / [storage] / [deps] check actually
# fails. We pre-filled .env with all required values, so we expect 0 errors
# even with services down — but we accept either 0 or non-zero exit, since
# missing images or missing PATH symlink could produce warnings or errors
# depending on the host. The shape of the report is what we validate.

echo "[harness-test] T8: harness doctor (services down)"
set +e
doctor_down_out=$("${HARNESS_BIN}" doctor 2>&1)
doctor_down_rc=$?
set -e
for section in '\[deps\]' '\[install\]' '\[config\]' '\[storage\]' '\[runtime\]' '\[images\]'; do
    if ! grep -Eq "${section}" <<<"${doctor_down_out}"; then
        echo "[harness-test] T8 FAIL: doctor output missing section ${section}" >&2
        echo "${doctor_down_out}" >&2
        exit 1
    fi
done
# [runtime] should reflect that services aren't running.
if ! grep -Eq 'services not running|not present' <<<"${doctor_down_out}"; then
    echo "[harness-test] T8 FAIL: doctor [runtime] did not report services as down" >&2
    echo "${doctor_down_out}" >&2
    exit 1
fi
# [deps] should at minimum confirm the docker daemon — otherwise the test
# couldn't have reached this point at all.
if ! grep -Eq 'docker daemon[[:space:]]+reachable' <<<"${doctor_down_out}"; then
    echo "[harness-test] T8 FAIL: doctor did not confirm docker daemon" >&2
    echo "${doctor_down_out}" >&2
    exit 1
fi
echo "[harness-test] T8 OK (rc=${doctor_down_rc})"

# --- Test 9: harness doctor (services up) ----------------------------------

echo "[harness-test] T9: harness doctor (services up)"
"${HARNESS_BIN}" start >/dev/null
deadline=$(( $(date +%s) + 60 ))
while true; do
    ollama_id=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q ollama 2>/dev/null || true)
    proxy_id=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${ollama_id}" && -n "${proxy_id}" ]]; then
        proxy_status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        ollama_status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${ollama_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" && "${ollama_status}" == "healthy" ]]; then
            break
        fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[harness-test] T9 FAIL: services not healthy in 60s" >&2
        exit 1
    fi
    sleep 2
done

set +e
doctor_up_out=$("${HARNESS_BIN}" doctor 2>&1)
doctor_up_rc=$?
set -e
echo "${doctor_up_out}" | sed 's/^/  | /'
if (( doctor_up_rc != 0 )); then
    echo "[harness-test] T9 FAIL: doctor exited non-zero (rc=${doctor_up_rc}) with services up" >&2
    exit 1
fi
if ! grep -Eq 'ollama[[:space:]]+healthy' <<<"${doctor_up_out}"; then
    echo "[harness-test] T9 FAIL: doctor did not report ollama healthy" >&2
    exit 1
fi
if ! grep -Eq 'proxy[[:space:]]+healthy' <<<"${doctor_up_out}"; then
    echo "[harness-test] T9 FAIL: doctor did not report proxy healthy" >&2
    exit 1
fi
# The storage section gained an `mcp` line in Phase 6 reporting whether
# any MCPs are enabled. With no MCPs active, it should report cleanly.
if ! grep -Eq 'mcp[[:space:]]+no entries enabled|mcp[[:space:]]+writable' <<<"${doctor_up_out}"; then
    echo "[harness-test] T9 FAIL: doctor [storage] missing mcp line" >&2
    echo "${doctor_up_out}" >&2
    exit 1
fi
echo "[harness-test] T9 OK"

# --- Test 10: -p flag is parsed by the harness script ----------------------
#
# We can't actually run a full headless agent here (the test stack uses
# placeholder upstream values, so any real LLM call would fail at upstream
# rather than at the harness layer). What we DO want to validate: the script
# parses -p without erroring, dispatches it, and the failure mode is "the
# upstream/proxy can't be reached" rather than "unknown flag" or "unknown
# command".
#
# We trigger this by invoking with an obviously-unbuilt agent image. To
# avoid building the agent images here (slow), we ensure the harness script
# REJECTS the invocation cleanly with the documented "image not found" error
# when the agent image isn't present. The error proves the -p path was
# entered, args parsed, and the script reached the image-existence check.

echo "[harness-test] T10: -p flag is parsed and dispatched"

# Stash any agent image so we can deterministically test the missing-image
# path without affecting the host's other tests. We rename rather than
# delete; restored on exit.
restore_agent_image() {
    if [[ -n "${claude_img_orig:-}" ]]; then
        docker tag "${claude_img_orig}" "harness-claude-agent:latest" >/dev/null 2>&1 || true
        docker rmi "${claude_img_orig}" >/dev/null 2>&1 || true
    fi
}
trap 'restore_agent_image; cleanup' EXIT INT TERM

claude_img_orig=""
if docker image inspect harness-claude-agent:latest >/dev/null 2>&1; then
    claude_img_orig="harness-claude-agent:harness-test-stash-$$"
    docker tag harness-claude-agent:latest "${claude_img_orig}" >/dev/null
    docker rmi harness-claude-agent:latest >/dev/null 2>&1 || true
fi

set +e
p_out=$("${HARNESS_BIN}" claude -p "test prompt" 2>&1)
p_rc=$?
set -e

# Restore as soon as we have the result.
restore_agent_image
claude_img_orig=""

if (( p_rc == 0 )); then
    echo "[harness-test] T10 FAIL: harness claude -p unexpectedly exited 0" >&2
    echo "${p_out}" >&2
    exit 1
fi
# We expect the image-not-found error from the harness script. If we got
# something that looks like a "command not found" or argument parse error,
# that's a regression in the -p plumbing.
if ! grep -Eqi 'image .* not found|not found; run .harness start' <<<"${p_out}"; then
    echo "[harness-test] T10 FAIL: -p invocation error doesn't look like the documented" >&2
    echo "                          image-missing failure path. Output:" >&2
    echo "${p_out}" >&2
    exit 1
fi
if grep -Eqi 'unknown command|invalid option|usage:' <<<"${p_out}"; then
    echo "[harness-test] T10 FAIL: -p was parsed as an unknown command/flag" >&2
    echo "${p_out}" >&2
    exit 1
fi
echo "[harness-test] T10 OK"

echo "============================================================"
echo " HARNESS TEST PASSED"
echo "============================================================"
exit 0
