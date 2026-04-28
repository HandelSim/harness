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
# Interactive subcommands (claude, opencode, stop with picker) require a
# TTY and live upstream — those are validated by
# scripts/full_pipeline_test.sh (T9/T10/T11) and by the manual smoke checks
# documented in MANUAL_TEST_PROMPT.md. They are NOT covered here.
#
# Other smoke checks:
#   - harness-install.sh's PATH-rcfile append is idempotent
#
# A separate compose project name (HARNESS_PROJECT_NAME=harness-mgmt-test)
# is used so this test never collides with a real harness instance the
# developer may have running on the same daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NAME="harness-mgmt-test"

echo "============================================================"
echo " harness management script test"
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

# Firewall allowlist for the test stack. placeholder.invalid is included so
# init-firewall.sh's PROXY_API_URL guardrail accepts it (the host won't
# resolve, which the firewall logs WARN about, but the guardrail's allowlist-
# membership check is satisfied).
cat >"${TEST_ROOT}/.harness-allowlist" <<'EOF'
github.com
api.github.com
codeload.github.com
raw.githubusercontent.com
objects.githubusercontent.com
pypi.org
files.pythonhosted.org
registry.npmjs.org
placeholder.invalid
EOF

# Convenience: every invocation in this test file shares the same env vars.
# HARNESS_INSTALL_ROOT pins the install root explicitly. The symlink at
# ${TEST_ROOT}/harness would otherwise cause the script's realpath/dirname
# walk to land in the real repo's parent, where there's no .env.
HARNESS_BIN="${TEST_ROOT}/harness/harness"
export HARNESS_PROJECT_NAME="${PROJECT_NAME}"
export HARNESS_INSTALL_ROOT="${TEST_ROOT}"
export HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist"

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
for cmd in start down restart update upgrade logs claude opencode list stop net mcp doctor claude-statusline-config; do
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
#
# Phase 7a: install/uninstall/enable/disable verbs were re-cut. The Phase 6
# `enable`/`disable --force` aliases still work but emit a deprecation
# warning to stderr. We exercise both the new and the deprecated paths so a
# regression in either surfaces here.

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
# Plain 'mcp list' shows installed-only (Phase 13b); use --available so the
# empty-registry path emits the dedicated 'no MCP entries' message.
empty_list=$(HARNESS_REGISTRY_DIR="${empty_reg}" "${HARNESS_BIN}" mcp list --available)
if ! grep -qi 'no MCP entries' <<<"${empty_list}"; then
    echo "[harness-test] T5b FAIL: empty registry should report 'no MCP entries'" >&2
    echo "${empty_list}" >&2
    exit 1
fi

# 5b.2: populated registry — foo appears with state=available.
# Need --available since foo is in the registry but not yet installed.
pop_list=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp list --available)
if ! grep -Eq 'foo[[:space:]]+available' <<<"${pop_list}"; then
    echo "[harness-test] T5b FAIL: populated registry should list foo as available" >&2
    echo "${pop_list}" >&2
    exit 1
fi

# 5b.3: install unknown errors with available list.
set +e
unk_out=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp install nope 2>&1)
unk_rc=$?
set -e
if (( unk_rc == 0 )); then
    echo "[harness-test] T5b FAIL: install nope unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -qi 'unknown MCP' <<<"${unk_out}"; then
    echo "[harness-test] T5b FAIL: install error doesn't mention 'unknown MCP'" >&2
    echo "${unk_out}" >&2
    exit 1
fi

# 5b.4: install + uninstall round-trip on the host fs (no docker).
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp install foo >/dev/null
[[ -f "${TEST_ROOT}/state/mcp/foo/compose.yml" ]] \
    || { echo "[harness-test] T5b FAIL: foo not installed to active tree" >&2; exit 1; }
[[ -f "${TEST_ROOT}/state/mcp/foo/harness-meta.json" ]] \
    || { echo "[harness-test] T5b FAIL: harness-meta.json not written on install" >&2; exit 1; }
# After install, list should show installed-enabled.
inst_list=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp list)
if ! grep -Eq 'foo[[:space:]]+installed-enabled' <<<"${inst_list}"; then
    echo "[harness-test] T5b FAIL: list did not show foo as installed-enabled" >&2
    echo "${inst_list}" >&2
    exit 1
fi
# disable (state-flag flip; data and config preserved)
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp disable foo >/dev/null
[[ -f "${TEST_ROOT}/state/mcp/foo/compose.yml" ]] \
    || { echo "[harness-test] T5b FAIL: disable removed compose.yml (should be preserved)" >&2; exit 1; }
dis_list=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp list)
if ! grep -Eq 'foo[[:space:]]+installed-disabled' <<<"${dis_list}"; then
    echo "[harness-test] T5b FAIL: list did not show foo as installed-disabled" >&2
    echo "${dis_list}" >&2
    exit 1
fi
# re-enable
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp enable foo >/dev/null
re_list=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp list)
if ! grep -Eq 'foo[[:space:]]+installed-enabled' <<<"${re_list}"; then
    echo "[harness-test] T5b FAIL: re-enable did not restore installed-enabled" >&2
    echo "${re_list}" >&2
    exit 1
fi
# uninstall
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp uninstall foo --force >/dev/null
[[ -f "${TEST_ROOT}/state/mcp/foo/compose.yml" ]] \
    && { echo "[harness-test] T5b FAIL: uninstall did not remove compose.yml" >&2; exit 1; }

# 5b.5: enable on a not-yet-installed entry now refuses (Phase 13b made
# enable/disable canonical state-flag commands; the Phase 6 deprecation
# alias was removed). The user must explicitly run install first.
set +e
not_installed_out=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp enable foo 2>&1)
not_installed_rc=$?
set -e
if (( not_installed_rc == 0 )); then
    echo "[harness-test] T5b FAIL: 'mcp enable <not-yet-installed>' should refuse" >&2
    echo "${not_installed_out}" >&2
    exit 1
fi
if ! grep -qi 'not installed' <<<"${not_installed_out}"; then
    echo "[harness-test] T5b FAIL: enable refusal did not mention 'not installed'" >&2
    echo "${not_installed_out}" >&2
    exit 1
fi

# 5b.6: install foo, then disable (state flag), then enable, verifying
# enabled flag toggles without affecting installed state. Files stay.
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp install foo >/dev/null
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp disable foo >/dev/null
[[ -f "${TEST_ROOT}/state/mcp/foo/compose.yml" ]] \
    || { echo "[harness-test] T5b FAIL: disable removed files (should only flip flag)" >&2; exit 1; }
state_after_disable=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp status foo 2>&1)
grep -Eq 'state:[[:space:]]+installed-disabled' <<<"${state_after_disable}" \
    || { echo "[harness-test] T5b FAIL: disable did not flip state to installed-disabled" >&2
         echo "${state_after_disable}" >&2; exit 1; }
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp enable foo >/dev/null
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp uninstall foo --force >/dev/null
[[ -f "${TEST_ROOT}/state/mcp/foo/compose.yml" ]] \
    && { echo "[harness-test] T5b FAIL: uninstall did not remove compose.yml" >&2; exit 1; }

# 5b.7: status reports state correctly.
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp install foo >/dev/null
status_out=$(HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp status foo 2>&1)
if ! grep -Eq 'state:[[:space:]]+installed-enabled' <<<"${status_out}"; then
    echo "[harness-test] T5b FAIL: status did not report installed-enabled" >&2
    echo "${status_out}" >&2
    exit 1
fi
HARNESS_REGISTRY_DIR="${populated_reg}" "${HARNESS_BIN}" mcp uninstall foo --force >/dev/null

cleanup_mcp_dirs
trap cleanup EXIT INT TERM

echo "[harness-test] T5b OK"

# --- Test 7: harness-install.sh PATH append is idempotent ------------------
#
# Synthesize the exact append-to-rcfile branch from harness-install.sh: if
# grep finds an existing .local/bin reference we leave the file alone. We
# test it by simulating two install runs into a fake HOME.

echo "[harness-test] T7: harness-install.sh PATH-append idempotency"
fake_home=$(mktemp -d -t harness-fake-home.XXXXXX)
trap 'rm -rf "${fake_home}"' RETURN || true

rcfile="${fake_home}/.bashrc"
touch "${rcfile}"

# Emulate the harness-install.sh append logic directly.
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

# Post-13a, harness claude auto-builds the agent image on first launch
# rather than failing with "image not found". So the missing-image path
# we used to assert against is gone. Instead, we verify that `-p` is
# parsed and that the script enters the agent-launch path — we can detect
# that by stashing both the image AND blocking docker compose so the
# auto-build fails fast. The error we then see should NOT look like an
# argument parse error.
restore_agent_image() {
    # `timeout 60` may kill the harness wrapper after `docker run --rm
    # --network <project>_harness-net` has started the agent container.
    # The container survives client death and stays attached to the network,
    # which would then cause T12's restart to fail at `compose down`
    # ("network has active endpoints"). Force-remove any harness-agent
    # containers we may have left behind so subsequent tests start clean.
    local stragglers
    stragglers=$(docker ps -aq --filter "ancestor=harness-agent:latest" 2>/dev/null || true)
    if [[ -n "${stragglers}" ]]; then
        docker rm -f ${stragglers} >/dev/null 2>&1 || true
    fi
    if [[ -n "${agent_img_orig:-}" ]]; then
        docker tag "${agent_img_orig}" "harness-agent:latest" >/dev/null 2>&1 || true
        docker rmi "${agent_img_orig}" >/dev/null 2>&1 || true
    fi
}
trap 'restore_agent_image; cleanup' EXIT INT TERM

agent_img_orig=""
if docker image inspect harness-agent:latest >/dev/null 2>&1; then
    agent_img_orig="harness-agent:harness-test-stash-$$"
    docker tag harness-agent:latest "${agent_img_orig}" >/dev/null
    docker rmi harness-agent:latest >/dev/null 2>&1 || true
fi

# Build will be attempted; we don't actually want it to run to completion
# in this test (it's slow). Pre-create a sentinel file under the test's
# install root and rely on the build to either succeed or fail — we only
# care that no parse error appeared in the output. If image was already
# absent before the test, the build will run; that's fine.
set +e
p_out=$(timeout 60 "${HARNESS_BIN}" claude -p "test prompt" 2>&1)
p_rc=$?
set -e

# Restore as soon as we have the result.
restore_agent_image
agent_img_orig=""

# A successful exit (rc=0) is unexpected here because we lack a running
# ollama / mock upstream. But we DON'T require non-zero — the test is
# purely about argument parsing. The forbidden conditions are parse errors.
if grep -Eqi 'unknown command|invalid option|usage:[[:space:]]+harness' <<<"${p_out}"; then
    echo "[harness-test] T10 FAIL: -p was parsed as an unknown command/flag" >&2
    echo "${p_out}" >&2
    exit 1
fi
echo "[harness-test] T10 OK"

# --- Test 11: harness net allow / deny / list / status ----------------------
#
# Non-interactive subcommands of `harness net` round-trip the allowlist file.
# `open` / `close` need a TTY for the confirmation phrase, so we exercise
# them via the helper library directly in a separate test below.

echo "[harness-test] T11: harness net allow/deny/list/status"

# T11.1: status shows the test allowlist.
status_out=$("${HARNESS_BIN}" net status 2>&1)
if ! grep -q 'allowlist:' <<<"${status_out}"; then
    echo "[harness-test] T11 FAIL: net status missing allowlist section" >&2
    echo "${status_out}" >&2; exit 1
fi
if ! grep -Eq 'overrides:' <<<"${status_out}"; then
    echo "[harness-test] T11 FAIL: net status missing overrides section" >&2
    echo "${status_out}" >&2; exit 1
fi

# T11.2: allow a host, then list shows it.
"${HARNESS_BIN}" net allow new-host.example.com >/dev/null
list_out=$("${HARNESS_BIN}" net list)
if ! grep -q 'new-host.example.com' <<<"${list_out}"; then
    echo "[harness-test] T11 FAIL: allow did not add new-host.example.com" >&2
    echo "${list_out}" >&2; exit 1
fi

# T11.3: --git-push annotates a host as push-enabled.
"${HARNESS_BIN}" net allow my-gitlab.example.com --git-push >/dev/null
push_out=$("${HARNESS_BIN}" net list)
if ! grep -Eq 'my-gitlab\.example\.com[[:space:]]+\[git-push\]' <<<"${push_out}"; then
    echo "[harness-test] T11 FAIL: --git-push did not annotate host as push" >&2
    echo "${push_out}" >&2; exit 1
fi

# T11.4: deny removes a host.
"${HARNESS_BIN}" net deny new-host.example.com >/dev/null
denied_out=$("${HARNESS_BIN}" net list)
if grep -q 'new-host.example.com' <<<"${denied_out}"; then
    echo "[harness-test] T11 FAIL: deny did not remove new-host.example.com" >&2
    echo "${denied_out}" >&2; exit 1
fi

# T11.5: invalid host is rejected.
set +e
inv_out=$("${HARNESS_BIN}" net allow 'BAD HOST' 2>&1)
inv_rc=$?
set -e
if (( inv_rc == 0 )); then
    echo "[harness-test] T11 FAIL: net allow with invalid host succeeded" >&2; exit 1
fi
if ! grep -qi 'invalid host' <<<"${inv_out}"; then
    echo "[harness-test] T11 FAIL: net allow invalid host: missing error message" >&2
    echo "${inv_out}" >&2; exit 1
fi

# Clean up the test additions so subsequent tests start from the seed
# allowlist.
"${HARNESS_BIN}" net deny my-gitlab.example.com >/dev/null 2>&1 || true
echo "[harness-test] T11 OK"

# --- Test 12: harness restart -----------------------------------------------
#
# restart = down + start, so we just verify it leaves services healthy.

echo "[harness-test] T12: harness restart"
"${HARNESS_BIN}" restart >/dev/null
deadline=$(( $(date +%s) + 60 ))
while true; do
    proxy_id=$(docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${proxy_id}" ]]; then
        proxy_status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" ]]; then break; fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[harness-test] T12 FAIL: services not healthy after restart" >&2; exit 1
    fi
    sleep 2
done
echo "[harness-test] T12 OK"

# --- Test 13: claude-statusline-config dispatches -----------------------------
#
# Like T10, we just want to prove the dispatcher routes the verb to the
# right command — building/running the configurator interactively isn't
# something we can do in a non-TTY test. We verify it appears in `harness
# help` and that the help-only path doesn't error.

echo "[harness-test] T13: claude-statusline-config in help"
help_out=$("${HARNESS_BIN}" help)
if ! grep -q 'claude-statusline-config' <<<"${help_out}"; then
    echo "[harness-test] T13 FAIL: help text missing claude-statusline-config" >&2
    exit 1
fi
echo "[harness-test] T13 OK"

# --- Test 14: harness help mentions new B2 commands -------------------------

echo "[harness-test] T14: help mentions B2 verbs (restart, net, --net)"
for tok in restart 'net <subcmd>' '--net'; do
    if ! grep -qF -- "$tok" <<<"${help_out}"; then
        echo "[harness-test] T14 FAIL: help missing '$tok'" >&2
        exit 1
    fi
done
echo "[harness-test] T14 OK"

# --- Test 15: doctor [network] section --------------------------------------

echo "[harness-test] T15: doctor [network] section"
set +e
doctor_net_out=$("${HARNESS_BIN}" doctor 2>&1)
set -e
if ! grep -q '\[network\]' <<<"${doctor_net_out}"; then
    echo "[harness-test] T15 FAIL: doctor missing [network] section" >&2
    echo "${doctor_net_out}" >&2; exit 1
fi
if ! grep -q 'allowlist' <<<"${doctor_net_out}"; then
    echo "[harness-test] T15 FAIL: doctor [network] missing allowlist line" >&2
    exit 1
fi
echo "[harness-test] T15 OK"

# --- Test 16: harness upgrade --check (no changes) -------------------------
#
# Builds a tiny synthetic install root in a separate tmpdir, points the
# harness binary at it, and runs `harness upgrade --check`. Verifies:
#   - non-interactive: exits 0 with no prompt
#   - prints the "Upgrade actions to apply" preview
#   - mtimes of install-root files do not change

echo "[harness-test] T16: harness upgrade --check"
UPG_ROOT="$(mktemp -d -t harness-upg-check.XXXXXX)"
cleanup_upg() {
    if [[ -n "${UPG_ROOT:-}" && -d "${UPG_ROOT}" ]]; then
        rm -rf "${UPG_ROOT}"
    fi
}
trap 'cleanup_upg; restore_agent_image; cleanup' EXIT INT TERM

ln -s "${REPO_ROOT}" "${UPG_ROOT}/harness"
# Pre-fill .env with a subset of vars so envfile_merge has something to
# add. .harness-allowlist with a subset so linefile_merge has something to
# add. ccstatusline target absent so json_merge reports "create from source".
cat >"${UPG_ROOT}/.env" <<'EOF'
PROXY_API_URL=https://placeholder.invalid/v1/chat/completions
PROXY_API_KEY=test-key
PROXY_API_MODEL=test-model
EOF
cat >"${UPG_ROOT}/.harness-allowlist" <<'EOF'
github.com
api.github.com
placeholder.invalid
EOF

env_mt_before=$(stat -c '%Y' "${UPG_ROOT}/.env")
allow_mt_before=$(stat -c '%Y' "${UPG_ROOT}/.harness-allowlist")

set +e
upg_out=$(HARNESS_INSTALL_ROOT="${UPG_ROOT}" HARNESS_PROJECT_NAME="harness-upg-check" \
    "${UPG_ROOT}/harness/harness" upgrade --check 2>&1)
upg_rc=$?
set -e
if (( upg_rc != 0 )); then
    echo "[harness-test] T16 FAIL: upgrade --check exited rc=${upg_rc}" >&2
    echo "${upg_out}" >&2; exit 1
fi
if ! grep -q 'Upgrade actions to apply:' <<<"${upg_out}"; then
    echo "[harness-test] T16 FAIL: --check did not print preview" >&2
    echo "${upg_out}" >&2; exit 1
fi
if ! grep -q 'env_vars' <<<"${upg_out}"; then
    echo "[harness-test] T16 FAIL: --check did not list env_vars action" >&2
    echo "${upg_out}" >&2; exit 1
fi
if ! grep -q 'no changes will be made' <<<"${upg_out}"; then
    echo "[harness-test] T16 FAIL: --check did not announce dry-run" >&2
    echo "${upg_out}" >&2; exit 1
fi
env_mt_after=$(stat -c '%Y' "${UPG_ROOT}/.env")
allow_mt_after=$(stat -c '%Y' "${UPG_ROOT}/.harness-allowlist")
[[ "${env_mt_before}" == "${env_mt_after}" ]] \
    || { echo "[harness-test] T16 FAIL: --check modified .env mtime" >&2; exit 1; }
[[ "${allow_mt_before}" == "${allow_mt_after}" ]] \
    || { echo "[harness-test] T16 FAIL: --check modified allowlist mtime" >&2; exit 1; }
[[ ! -f "${UPG_ROOT}/state/agent/home/.config/ccstatusline/settings.json" ]] \
    || { echo "[harness-test] T16 FAIL: --check created ccstatusline settings file" >&2; exit 1; }
echo "[harness-test] T16 OK"

# --- Test 17: harness upgrade --no-prompt --no-restart -------------------
#
# Apply the same upgrade in apply mode, but skip the down/start cycle so we
# don't disturb T15's running services. Verify the install root files
# actually picked up the new vars/hosts/keys.

echo "[harness-test] T17: harness upgrade --no-prompt --no-restart"
set +e
upg_apply_out=$(HARNESS_INSTALL_ROOT="${UPG_ROOT}" HARNESS_PROJECT_NAME="harness-upg-check" \
    HARNESS_UPGRADE_SKIP_PULL=1 \
    "${UPG_ROOT}/harness/harness" upgrade --no-prompt --no-restart 2>&1)
upg_apply_rc=$?
set -e
if (( upg_apply_rc != 0 )); then
    echo "[harness-test] T17 FAIL: apply rc=${upg_apply_rc}" >&2
    echo "${upg_apply_out}" >&2; exit 1
fi
# .env should now have at least one of the new vars from .env.example.
if ! grep -q '^OLLAMA_VERSION=' "${UPG_ROOT}/.env"; then
    echo "[harness-test] T17 FAIL: OLLAMA_VERSION not added to .env after upgrade" >&2
    cat "${UPG_ROOT}/.env" >&2; exit 1
fi
# .env existing values must be preserved.
if ! grep -q '^PROXY_API_KEY=test-key$' "${UPG_ROOT}/.env"; then
    echo "[harness-test] T17 FAIL: PROXY_API_KEY user value not preserved" >&2
    cat "${UPG_ROOT}/.env" >&2; exit 1
fi
# .harness-allowlist should now have pypi.org (a new entry from the example).
if ! grep -q '^pypi.org$' "${UPG_ROOT}/.harness-allowlist"; then
    echo "[harness-test] T17 FAIL: pypi.org not added to allowlist after upgrade" >&2
    cat "${UPG_ROOT}/.harness-allowlist" >&2; exit 1
fi
# ccstatusline target was absent → should now exist.
if [[ ! -f "${UPG_ROOT}/state/agent/home/.config/ccstatusline/settings.json" ]]; then
    echo "[harness-test] T17 FAIL: ccstatusline settings file not created" >&2
    exit 1
fi
# Idempotent: re-running adds nothing.
upg_redo_out=$(HARNESS_INSTALL_ROOT="${UPG_ROOT}" HARNESS_PROJECT_NAME="harness-upg-check" \
    HARNESS_UPGRADE_SKIP_PULL=1 \
    "${UPG_ROOT}/harness/harness" upgrade --no-prompt --no-restart 2>&1)
if grep -Eq 'envfile_merge: [1-9][0-9]* change' <<<"${upg_redo_out}"; then
    echo "[harness-test] T17 FAIL: idempotent upgrade reported envfile changes on second run" >&2
    echo "${upg_redo_out}" >&2; exit 1
fi
cleanup_upg
trap 'restore_agent_image; cleanup' EXIT INT TERM
echo "[harness-test] T17 OK"

# --- Test 18: harness upgrade non-interactive without --no-prompt --------
#
# Non-interactive shells without --no-prompt MUST refuse rather than hang.

echo "[harness-test] T18: harness upgrade rejects non-interactive without --no-prompt"
UPG18_ROOT="$(mktemp -d -t harness-upg18.XXXXXX)"
ln -s "${REPO_ROOT}" "${UPG18_ROOT}/harness"
echo "PROXY_API_URL=https://placeholder.invalid/v1" >"${UPG18_ROOT}/.env"
echo "github.com" >"${UPG18_ROOT}/.harness-allowlist"
set +e
upg18_out=$(HARNESS_INSTALL_ROOT="${UPG18_ROOT}" HARNESS_PROJECT_NAME="harness-upg18" \
    HARNESS_UPGRADE_SKIP_PULL=1 \
    "${UPG18_ROOT}/harness/harness" upgrade --no-restart </dev/null 2>&1)
upg18_rc=$?
set -e
if (( upg18_rc == 0 )); then
    echo "[harness-test] T18 FAIL: non-interactive upgrade without --no-prompt unexpectedly succeeded" >&2
    echo "${upg18_out}" >&2
    rm -rf "${UPG18_ROOT}"; exit 1
fi
if ! grep -qi 'non-interactive' <<<"${upg18_out}"; then
    echo "[harness-test] T18 FAIL: error message did not flag non-interactive shell" >&2
    echo "${upg18_out}" >&2
    rm -rf "${UPG18_ROOT}"; exit 1
fi
rm -rf "${UPG18_ROOT}"
echo "[harness-test] T18 OK"

# --- Test 19: platform.sh helpers (sourced) --------------------------------

echo "[harness-test] T19: platform.sh helpers"

# Source the library directly. All helpers are pure functions that touch
# the filesystem / docker daemon at most read-only.
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# OS detection returns a known value.
os=$(harness_detect_os)
case "${os}" in
    linux|macos|windows|unknown) ;;
    *)
        echo "[harness-test] T19 FAIL: harness_detect_os returned unexpected value: ${os}" >&2
        exit 1
        ;;
esac

# realpath resolves an existing file.
tmpfile=$(mktemp)
resolved=$(harness_realpath "${tmpfile}")
if [[ -z "${resolved}" ]]; then
    echo "[harness-test] T19 FAIL: harness_realpath returned empty" >&2
    rm -f "${tmpfile}"; exit 1
fi
rm -f "${tmpfile}"

# normalize_path collapses double slashes and converts backslashes.
norm=$(harness_normalize_path "/foo//bar")
if [[ "${norm}" != "/foo/bar" ]]; then
    echo "[harness-test] T19 FAIL: normalize_path didn't collapse double slash: ${norm}" >&2
    exit 1
fi
norm_bs=$(harness_normalize_path 'C:\Users\foo')
if [[ "${norm_bs}" != "C:/Users/foo" ]]; then
    echo "[harness-test] T19 FAIL: normalize_path didn't convert backslash: ${norm_bs}" >&2
    exit 1
fi

# docker_running reflects actual state — we already know the daemon is up
# from the preflight at the top of this test file.
if ! harness_docker_running; then
    echo "[harness-test] T19 FAIL: harness_docker_running false but daemon is up" >&2
    exit 1
fi

# check_command finds bash.
if ! harness_check_command bash "bash shell" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: harness_check_command bash failed" >&2
    exit 1
fi

# check_command rejects a nonsense binary.
if harness_check_command __nonexistent_cmd_xyz__ "fake binary" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: harness_check_command should reject fake command" >&2
    exit 1
fi

# check_env_var: required-and-empty fails, optional-and-empty passes.
unset _HARNESS_TEST_VAR_PROBE
if harness_check_env_var _HARNESS_TEST_VAR_PROBE true "test" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: required-empty env var should have failed" >&2
    exit 1
fi
if ! harness_check_env_var _HARNESS_TEST_VAR_PROBE false "test" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: optional-empty env var should have passed" >&2
    exit 1
fi
export _HARNESS_TEST_VAR_PROBE=value
if ! harness_check_env_var _HARNESS_TEST_VAR_PROBE true "test" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: set required env var should have passed" >&2
    exit 1
fi
unset _HARNESS_TEST_VAR_PROBE

# check_file_exists distinguishes required vs optional and present vs absent.
exist_tmp=$(mktemp)
if ! harness_check_file_exists "${exist_tmp}" true "exists" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: required-existing file should have passed" >&2
    rm -f "${exist_tmp}"; exit 1
fi
rm -f "${exist_tmp}"
if harness_check_file_exists "${exist_tmp}" true "absent" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: required-missing file should have failed" >&2
    exit 1
fi
if ! harness_check_file_exists "${exist_tmp}" false "absent-optional" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: optional-missing file should have passed" >&2
    exit 1
fi

# check_dir_writable on a known-writable temp dir.
tmpdir=$(mktemp -d)
if ! harness_check_dir_writable "${tmpdir}" true "writable" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: writable dir should have passed" >&2
    rmdir "${tmpdir}"; exit 1
fi
rmdir "${tmpdir}"

# check_disk_space — passing 0 MB always succeeds.
if ! harness_check_disk_space "${REPO_ROOT}" 0 "any disk space" 2>/dev/null; then
    echo "[harness-test] T19 FAIL: 0MB requirement should always pass" >&2
    exit 1
fi
echo "[harness-test] T19 OK"

# --- Test 20: harness preflight command ------------------------------------
#
# Smoke the command end-to-end against the test install root. .env and
# allowlist are seeded with placeholder-but-non-empty values so all
# required-vars checks pass; the daemon is up; we expect rc=0.

echo "[harness-test] T20: harness preflight (config valid)"
set +e
preflight_out=$("${HARNESS_BIN}" preflight 2>&1)
preflight_rc=$?
set -e
if (( preflight_rc != 0 )); then
    echo "[harness-test] T20 FAIL: preflight rc=${preflight_rc} with valid config" >&2
    echo "${preflight_out}" >&2; exit 1
fi
if ! grep -q 'all checks passed' <<<"${preflight_out}"; then
    echo "[harness-test] T20 FAIL: preflight didn't print 'all checks passed'" >&2
    echo "${preflight_out}" >&2; exit 1
fi
for needle in 'PROXY_API_URL is set' 'PROXY_API_KEY is set' 'PROXY_API_MODEL is set' 'docker daemon'; do
    if ! grep -q "${needle}" <<<"${preflight_out}"; then
        echo "[harness-test] T20 FAIL: preflight missing line: ${needle}" >&2
        echo "${preflight_out}" >&2; exit 1
    fi
done
echo "[harness-test] T20 OK"

# --- Test 21: harness preflight catches missing config ---------------------
#
# Move the .env aside so preflight reports it missing and returns 1.

echo "[harness-test] T21: harness preflight detects missing .env"
mv "${TEST_ROOT}/.env" "${TEST_ROOT}/.env.stash"
set +e
pf_miss_out=$("${HARNESS_BIN}" preflight 2>&1)
pf_miss_rc=$?
set -e
mv "${TEST_ROOT}/.env.stash" "${TEST_ROOT}/.env"
if (( pf_miss_rc == 0 )); then
    echo "[harness-test] T21 FAIL: preflight unexpectedly passed with missing .env" >&2
    echo "${pf_miss_out}" >&2; exit 1
fi
if ! grep -q '\.env config file' <<<"${pf_miss_out}"; then
    echo "[harness-test] T21 FAIL: preflight didn't mention .env" >&2
    echo "${pf_miss_out}" >&2; exit 1
fi
if ! grep -q '✗' <<<"${pf_miss_out}"; then
    echo "[harness-test] T21 FAIL: preflight missing failure marker" >&2
    echo "${pf_miss_out}" >&2; exit 1
fi
echo "[harness-test] T21 OK"

# --- Test 22: harness preflight catches allowlist hostname mismatch --------
#
# Edit .env so PROXY_API_URL points at a host that's not in the allowlist.
# Preflight should report the mismatch and suggest `harness net allow`.

echo "[harness-test] T22: harness preflight detects allowlist mismatch"
cp "${TEST_ROOT}/.env" "${TEST_ROOT}/.env.stash22"
sed -i 's|^PROXY_API_URL=.*|PROXY_API_URL=https://not-in-allowlist.example.org/v1|' "${TEST_ROOT}/.env"
set +e
pf_mm_out=$("${HARNESS_BIN}" preflight 2>&1)
pf_mm_rc=$?
set -e
mv "${TEST_ROOT}/.env.stash22" "${TEST_ROOT}/.env"
if (( pf_mm_rc == 0 )); then
    echo "[harness-test] T22 FAIL: preflight unexpectedly passed with bad hostname" >&2
    echo "${pf_mm_out}" >&2; exit 1
fi
if ! grep -q 'not-in-allowlist.example.org' <<<"${pf_mm_out}"; then
    echo "[harness-test] T22 FAIL: preflight didn't flag the bad hostname" >&2
    echo "${pf_mm_out}" >&2; exit 1
fi
if ! grep -q 'harness net allow' <<<"${pf_mm_out}"; then
    echo "[harness-test] T22 FAIL: preflight didn't suggest 'net allow' fix" >&2
    echo "${pf_mm_out}" >&2; exit 1
fi
echo "[harness-test] T22 OK"

echo "============================================================"
echo " HARNESS TEST PASSED"
echo "============================================================"
exit 0
