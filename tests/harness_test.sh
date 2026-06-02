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
# Interactive subcommands (opencode, stop with picker) require a
# TTY and live upstream — those are validated by
# tests/full_pipeline_test.sh (T9/T10/T11) and by the manual smoke checks
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

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

PROJECT_NAME="harness-mgmt-test"

echo "============================================================"
echo " harness management script test"
echo "============================================================"

# --- preflight ---------------------------------------------------------------

if ! harness_docker info >/dev/null 2>&1; then
    echo "[harness-test] ERROR: container runtime ($(harness_container_runtime)) not reachable" >&2
    exit 1
fi

# --- staging area ------------------------------------------------------------
#
# Lay out a fake install root that mirrors the real one:
#   $TEST_ROOT/
#     .env                    (test config, points proxy at mockupstream)
#     harness/                (symlink to the real repo so 'harness' subcommands resolve)
#     output/, agent/           (created on demand by the script)
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
# upstream values) the proxy will start. Proxy will fail to forward
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
        harness_docker compose --project-name "${PROJECT_NAME}" \
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
DEFAULT_MODEL_NAME=harness
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
MODEL_CONTEXT_LENGTH=200000
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
harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true

# --- Test 0: _gate_on_upstream_auth + run_agent gate (no docker) ------------
#
# Regression for #16: when the proxy service was already up,
# `harness opencode` (or bare `harness`) skipped `cmd_start` (and therefore
# the upstream auth probe) and the agent surfaced a locked key as an opaque
# proxy error. The fix routes both `cmd_start` and `run_agent` through
# `_gate_on_upstream_auth`, so a locked key prints the unlock URL and
# refuses to launch the container.
#
# These cases shell-source the script (HARNESS_SOURCE_ONLY=1) and stub
# `_probe_upstream_auth` to drive each branch deterministically without any
# live upstream — matching the pattern in tests/upgrade_test.sh.

echo "[harness-test] T0: _gate_on_upstream_auth + run_agent gate"

# T0.1 — _gate_on_upstream_auth tri-state mapping. Each subshell sources the
# script fresh so prior stubs don't leak between cases.
gate_case() {
    local probe_rc="$1" expected="$2" label="$3"
    local skip_env="${4:-}"
    local rc=0
    (
        HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
        eval "_probe_upstream_auth() { return ${probe_rc}; }"
        if [[ -n "$skip_env" ]]; then
            export "${skip_env?}"
        fi
        _gate_on_upstream_auth >/dev/null 2>&1
    ) || rc=$?
    if [[ "$rc" != "$expected" ]]; then
        echo "[harness-test] T0 FAIL [$label]: probe_rc=${probe_rc} expected gate rc=${expected}, got ${rc}" >&2
        exit 1
    fi
}

gate_case 0 0 "probe rc=0 → gate proceeds"
gate_case 1 1 "probe rc=1 (locked) → gate aborts"
gate_case 2 0 "probe rc=2 (unhealthy) → gate proceeds"
gate_case 1 0 "HARNESS_SKIP_AUTH_PROBE=1 bypasses locked probe" "HARNESS_SKIP_AUTH_PROBE=1"

# T0.2 — run_agent calls the gate before any container work. Stub
# `require_docker` and `ensure_services_up`/`docker` so a gate failure must
# be the only thing that can produce a non-zero exit, and so a passing gate
# would visibly try to reach `ensure_services_up` (which we make a sentinel
# that exits non-zero with a unique code).
run_agent_locked_rc=0
locked_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    require_docker() { :; }
    ensure_services_up() { echo "ENSURE_SERVICES_UP_CALLED"; }
    docker() { echo "DOCKER_CALLED $*"; }
    _gate_on_upstream_auth() { return 1; }
    run_agent opencode 2>&1
) || run_agent_locked_rc=$?
if (( run_agent_locked_rc == 0 )); then
    echo "[harness-test] T0 FAIL: run_agent should exit non-zero when gate returns 1" >&2
    exit 1
fi
if grep -qE 'ENSURE_SERVICES_UP_CALLED|DOCKER_CALLED' <<<"$locked_out"; then
    echo "[harness-test] T0 FAIL: run_agent reached services/docker after locked gate:" >&2
    echo "$locked_out" >&2
    exit 1
fi

# T0.3 — when the gate passes, run_agent advances past it. We assert the
# next stage (ensure_services_up) is reached. We can't run the full launch
# without docker, so a sentinel that exits with a known code lets us
# distinguish "gate passed, advanced into the launch path" from "gate
# blocked the launch".
run_agent_pass_rc=0
pass_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    require_docker() { :; }
    _gate_on_upstream_auth() { return 0; }
    ensure_services_up() { echo "ENSURE_SERVICES_UP_CALLED"; exit 42; }
    run_agent opencode 2>&1
) || run_agent_pass_rc=$?
if (( run_agent_pass_rc != 42 )); then
    echo "[harness-test] T0 FAIL: expected run_agent to advance to ensure_services_up (rc=42), got ${run_agent_pass_rc}" >&2
    echo "$pass_out" >&2
    exit 1
fi
if ! grep -q 'ENSURE_SERVICES_UP_CALLED' <<<"$pass_out"; then
    echo "[harness-test] T0 FAIL: run_agent did not reach ensure_services_up after passing gate" >&2
    echo "$pass_out" >&2
    exit 1
fi

echo "[harness-test] T0 OK"

# --- Test 0.4: _probe_upstream_auth response parsing ------------------------
#
# Regression for #43 and #108. #43: the 401/403 branch over-fired (rc=1 on
# every 401) and threw away the upstream body, so a transient non-lock 401
# hard-failed every launch. #108: the over-correction then warned-and-continued
# on a genuinely rejected key, dropping the user into an agent with no models.
# Current contract for a 401/403 with no unlock URL: abort (rc=1) UNLESS the
# upstream error.type is in the invalid_request family (#43 — key is fine, the
# probe request was bad), which warns-and-proceeds (rc=2). A locked key (unlock
# URL present) still aborts (rc=1) with the unlock banner.
#
# These cases stub `curl` in a subshell so we can drive the parsing logic
# with deterministic fixtures and assert on (rc, stderr contents).

echo "[harness-test] T0.4: _probe_upstream_auth response parsing"

probe_case() {
    local label="$1" status="$2" fixture_body="$3" expected_rc="$4"
    shift 4
    local rc=0 out
    out=$(
        HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
        export PROXY_API_URL=http://probe.invalid/v1/chat/completions
        export PROXY_API_KEY=test-key
        export DEFAULT_MODEL_NAME=test-model
        # Stub curl: emit fixture body + the synthetic status marker the
        # probe parses out of curl -w. _probe_upstream_auth captures stderr
        # via 2>&1, so writing only to stdout is fine.
        curl() {
            printf '%s\n__HTTP_STATUS__%s' "$fixture_body" "$status"
        }
        _probe_upstream_auth 2>&1
    ) || rc=$?
    if [[ "$rc" != "$expected_rc" ]]; then
        echo "[harness-test] T0.4 FAIL [$label]: expected rc=${expected_rc}, got ${rc}" >&2
        echo "$out" >&2
        exit 1
    fi
    local needle
    for needle in "$@"; do
        if ! grep -qF -- "$needle" <<<"$out"; then
            echo "[harness-test] T0.4 FAIL [$label]: expected output to contain: $needle" >&2
            echo "--- captured output ---" >&2
            echo "$out" >&2
            echo "--- end ---" >&2
            exit 1
        fi
    done
}

# 200 → rc=0, no output banner.
probe_case "200 → rc=0" "200" '{"choices":[{"message":{"content":"."}}]}' "0"

# 401 with .error.unlock_url + .error.message + .error.type → rc=1, prints
# unlock banner with all three structured fields plus raw body.
LOCKED_BODY='{"error":{"type":"unauthorized","message":"API key locked - visit the unlock URL to re-enable your key","unlock_url":"https://example.test/unlock/abc123"}}'
probe_case "401 locked-key → rc=1 with URL + message + type" "401" "$LOCKED_BODY" "1" \
    "ERROR: Upstream API key is locked" \
    "https://example.test/unlock/abc123" \
    "API key locked - visit the unlock URL to re-enable your key" \
    "unauthorized" \
    "Full upstream response body:"

# 401 with no unlock URL but an invalid_request-family type → rc=2
# (warn-and-proceed): the key is fine, the probe request was bad (#43).
NOLOCK_BODY='{"error":{"type":"invalid_request","message":"bad model name"}}'
probe_case "401 invalid_request (key-is-fine) → rc=2 (warn, do not block)" "401" "$NOLOCK_BODY" "2" \
    "WARN: upstream returned 401" \
    "invalid_request" \
    "bad model name" \
    "HARNESS_SKIP_AUTH_PROBE=1"

# 401 with an auth-typed error and no unlock URL → rc=1 (abort). #108: a
# rejected key must stop the launch so the user fixes PROXY_API_KEY instead of
# landing in an agent with no models registered. Body mirrors the real #108
# report (a key with a leading '/').
REJECTED_BODY='{"error":{"type":"unauthorized","message":"Invalid token: Invalid header: Base64 error: Invalid symbol 47, offset 0."}}'
probe_case "401 unauthorized no-URL → rc=1 (abort)" "401" "$REJECTED_BODY" "1" \
    "ERROR: Upstream rejected the API key" \
    "unauthorized" \
    "Invalid symbol 47" \
    "Full upstream response body:" \
    "HARNESS_SKIP_AUTH_PROBE=1"

# 401 with no error type at all → rc=1 (abort). A bare/unknown-typed 401 is
# Unauthorized by HTTP semantics, so it must stop the launch (#108).
BARE_401_BODY='{"error":{"message":"Unauthorized"}}'
probe_case "401 empty-type no-URL → rc=1 (abort)" "401" "$BARE_401_BODY" "1" \
    "ERROR: Upstream rejected the API key" \
    "HARNESS_SKIP_AUTH_PROBE=1"

# Top-level unlock_url (alt provider shape) also triggers the rc=1 path.
TOP_LEVEL_BODY='{"unlock_url":"https://example.test/unlock/top","message":"locked"}'
probe_case "401 top-level unlock_url → rc=1" "401" "$TOP_LEVEL_BODY" "1" \
    "https://example.test/unlock/top"

# 5xx → rc=2 warn-and-proceed (unchanged by this fix, asserted for safety).
probe_case "500 → rc=2" "500" "internal server error" "2" \
    "WARN: upstream returned 500"

echo "[harness-test] T0.4 OK"

# --- Test 1: harness start brings services up -------------------------------

echo "[harness-test] T1: harness start"
"${HARNESS_BIN}" start >/dev/null

# Wait up to 60s for both services to become healthy.
deadline=$(( $(date +%s) + 60 ))
while true; do
    proxy_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${proxy_id}" ]]; then
        proxy_status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" ]]; then
            break
        fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[harness-test] T1 FAIL: services not healthy in 60s" >&2
        harness_docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
        exit 1
    fi
    sleep 2
done
echo "[harness-test] T1 OK: proxy healthy"

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

echo "[harness-test] T3: harness logs proxy (timeout 5s)"
set +e
logs_out=$(timeout 5 "${HARNESS_BIN}" logs proxy 2>&1)
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

# Inventory F042: `harness logs` (no service arg) tails ALL services.
# Without a service arg, cmd_logs invokes `compose logs -f` with no
# positional service, so output should include proxy lines (proxy is the
# only long-running service in this test stack). timeout 124 = killed by
# deadline (expected); rc 0 also acceptable if compose ended on its own.
set +e
logs_all_out=$(timeout 5 "${HARNESS_BIN}" logs 2>&1)
logs_all_rc=$?
set -e
if (( logs_all_rc != 124 && logs_all_rc != 0 )); then
    echo "[harness-test] T3 FAIL [F042]: harness logs (no arg) exited rc=${logs_all_rc}" >&2
    echo "${logs_all_out}" | tail -20 >&2
    exit 1
fi
if grep -Eqi 'unknown command|invalid option|usage:[[:space:]]+harness' <<<"${logs_all_out}"; then
    echo "[harness-test] T3 FAIL [F042]: harness logs (no arg) produced a parse error" >&2
    echo "${logs_all_out}" >&2
    exit 1
fi
# compose logs without a service prefixes each line with the service name.
# We require proxy lines to prove "tail all services".
if ! grep -Eq 'proxy' <<<"${logs_all_out}"; then
    echo "[harness-test] T3 FAIL [F042]: harness logs (no arg) missing proxy lines" >&2
    echo "${logs_all_out}" | head -40 >&2
    exit 1
fi

echo "[harness-test] T3 OK"

# --- Test 4: harness down ---------------------------------------------------

echo "[harness-test] T4: harness down"
"${HARNESS_BIN}" down >/dev/null
# After down, no proxy containers should remain for this project.
remaining=$(harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q 2>/dev/null || true)
if [[ -n "${remaining}" ]]; then
    echo "[harness-test] T4 FAIL: containers still present after down" >&2
    harness_docker compose --project-name "${PROJECT_NAME}" -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
    exit 1
fi
echo "[harness-test] T4 OK"

# --- Test 5: harness help -------------------------------------------------

echo "[harness-test] T5: harness help mentions all subcommands"
help_out=$("${HARNESS_BIN}" help)
for cmd in start down restart update upgrade logs opencode list stop net mcp doctor; do
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
# tests/mcp_test.sh. We override HARNESS_REGISTRY_DIR with an empty dir
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

# --- Test 7b: harness_docker passes host proxy through to the runtime (#68) --
#
# Host proxy vars (HTTP_PROXY/HTTPS_PROXY, upper + lower) MUST reach the
# container runtime so `docker compose build` inherits them and BuildKit routes
# base-image pulls and the image RUN steps through the corp proxy. The wrappers
# therefore do NOT scrub them — whatever is in the harness process env flows
# into the runtime call. (Running containers don't get it because compose
# declares no proxy vars in any service environment, not because these wrappers
# strip it.) Drive a fake runtime that records its environment and assert all
# four spellings are present. Subshell-scoped so the overrides don't bleed into
# later tests.
echo "[harness-test] T7b: harness_docker passes proxy through to runtime env"
(
    t7b_rt="$(mktemp -t harness-fake-rt.XXXXXX)"
    cat >"${t7b_rt}" <<'FAKE'
#!/usr/bin/env bash
env >"${HARNESS_FAKE_RT_REC}"
FAKE
    chmod +x "${t7b_rt}"

    # Force the linux branch and resolve the runtime to our recorder.
    harness_container_runtime() { printf '%s' "${t7b_rt}"; }
    harness_detect_os() { printf '%s' linux; }

    export HTTP_PROXY="http://corp.invalid:8080"  HTTPS_PROXY="http://corp.invalid:8080"
    export http_proxy="http://corp.invalid:8080"  https_proxy="http://corp.invalid:8080"

    # Guard against a vacuous test: the proxy must really be in this env.
    env | grep -q '^HTTPS_PROXY=' \
        || { echo "[harness-test] T7b FAIL: HTTPS_PROXY not set; test would be vacuous" >&2; exit 1; }

    # harness_docker (returns normally).
    rec="$(mktemp -t harness-fake-rt-rec.XXXXXX)"
    export HARNESS_FAKE_RT_REC="${rec}"
    harness_docker run --rm hello >/dev/null 2>&1
    for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
        grep -Eq "^${v}=http://corp\.invalid:8080$" "${rec}" \
            || { echo "[harness-test] T7b FAIL: ${v} did not reach harness_docker runtime env:" >&2; cat "${rec}" >&2; exit 1; }
    done

    # harness_docker_exec (execs — run in a nested subshell so only it is replaced).
    rec_exec="$(mktemp -t harness-fake-rt-rec.XXXXXX)"
    export HARNESS_FAKE_RT_REC="${rec_exec}"
    ( harness_docker_exec run --rm hello ) >/dev/null 2>&1
    for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
        grep -Eq "^${v}=http://corp\.invalid:8080$" "${rec_exec}" \
            || { echo "[harness-test] T7b FAIL: ${v} did not reach harness_docker_exec runtime env:" >&2; cat "${rec_exec}" >&2; exit 1; }
    done

    rm -f "${t7b_rt}" "${rec}" "${rec_exec}"
)
echo "[harness-test] T7b OK"

# --- Test 7c: Windows Git Bash .bash_profile -> .bashrc bridge (#68) --------
#
# Git Bash starts login shells, which read ~/.bash_profile and skip ~/.bashrc,
# so the installer's PATH line in ~/.bashrc never runs in fresh sessions. The
# installer (Windows-only) bridges ~/.bash_profile -> ~/.bashrc. This branch
# can't execute on Linux CI (it's gated on harness_detect_os == windows), so
# we (a) source-grep that the real bridge exists in the installer and (b)
# exercise the bridge logic for idempotency + ~/.profile preservation.
echo "[harness-test] T7c: Git Bash .bash_profile bridge"
grep -q 'Git Bash starts login shells' "${REPO_ROOT}/harness-install.sh" \
    || { echo "[harness-test] T7c FAIL: bridge code missing from harness-install.sh" >&2; exit 1; }
(
    t7c_home="$(mktemp -d -t harness-fake-home.XXXXXX)"

    # Mirror of the installer's bridge snippet, parameterized on $t7c_home.
    bridge() {
        local bp="${t7c_home}/.bash_profile"
        if [[ -f "$bp" ]] && grep -q '\.bashrc' "$bp"; then
            return 0
        fi
        # Capture existence BEFORE the append (>> creates the file), mirroring
        # the installer — an inline `! -f` inside the block is always false.
        local bp_new=1
        [[ -f "$bp" ]] && bp_new=0
        {
            printf '\n# Added by harness installer: bridge\n'
            if (( bp_new )) && [[ -f "${t7c_home}/.profile" ]]; then
                printf 'if [ -f ~/.profile ]; then . ~/.profile; fi\n'
            fi
            printf 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi\n'
        } >>"$bp"
    }

    # Fresh home with a pre-existing ~/.profile: bridge must source BOTH and
    # be idempotent across repeated installer runs.
    printf 'export FOO=1\n' >"${t7c_home}/.profile"
    touch "${t7c_home}/.bashrc"
    bridge; bridge; bridge
    bp="${t7c_home}/.bash_profile"
    [[ -f "$bp" ]] || { echo "[harness-test] T7c FAIL: bridge did not create .bash_profile" >&2; exit 1; }
    cnt=$(grep -c '\. ~/.bashrc' "$bp")
    (( cnt == 1 )) || { echo "[harness-test] T7c FAIL: expected 1 '.bashrc' source line, got ${cnt}" >&2; cat "$bp" >&2; exit 1; }
    grep -q '\. ~/.profile' "$bp" \
        || { echo "[harness-test] T7c FAIL: pre-existing ~/.profile not preserved by bridge" >&2; cat "$bp" >&2; exit 1; }

    rm -rf "${t7c_home}"
)
echo "[harness-test] T7c OK"

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
# [deps] should at minimum confirm the container runtime — otherwise the
# test couldn't have reached this point at all. Match either docker or
# podman (the doctor output prefixes the line with the runtime name).
if ! grep -Eq '(docker|podman)\s+runtime[[:space:]]+reachable' <<<"${doctor_down_out}"; then
    echo "[harness-test] T8 FAIL: doctor did not confirm container runtime" >&2
    echo "${doctor_down_out}" >&2
    exit 1
fi
echo "[harness-test] T8 OK (rc=${doctor_down_rc})"

# --- Test 9: harness doctor (services up) ----------------------------------

echo "[harness-test] T9: harness doctor (services up)"
"${HARNESS_BIN}" start >/dev/null
deadline=$(( $(date +%s) + 60 ))
while true; do
    proxy_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${proxy_id}" ]]; then
        proxy_status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" ]]; then
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

# Post-13a, harness auto-builds the agent image on first launch
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
    stragglers=$(harness_docker ps -aq --filter "ancestor=harness-agent:latest" 2>/dev/null || true)
    if [[ -n "${stragglers}" ]]; then
        harness_docker rm -f ${stragglers} >/dev/null 2>&1 || true
    fi
    if [[ -n "${agent_img_orig:-}" ]]; then
        harness_docker tag "${agent_img_orig}" "harness-agent:latest" >/dev/null 2>&1 || true
        harness_docker rmi "${agent_img_orig}" >/dev/null 2>&1 || true
    fi
}
trap 'restore_agent_image; cleanup' EXIT INT TERM

agent_img_orig=""
if harness_docker image inspect harness-agent:latest >/dev/null 2>&1; then
    agent_img_orig="harness-agent:harness-test-stash-$$"
    harness_docker tag harness-agent:latest "${agent_img_orig}" >/dev/null
    harness_docker rmi harness-agent:latest >/dev/null 2>&1 || true
fi

# Build will be attempted; we don't actually want it to run to completion
# in this test (it's slow). Pre-create a sentinel file under the test's
# install root and rely on the build to either succeed or fail — we only
# care that no parse error appeared in the output. If image was already
# absent before the test, the build will run; that's fine.
set +e
p_out=$(timeout 60 "${HARNESS_BIN}" -p "test prompt" 2>&1)
p_rc=$?
set -e

# Restore as soon as we have the result.
restore_agent_image
agent_img_orig=""

# A successful exit (rc=0) is unexpected here because we lack a running
# mock upstream. But we DON'T require non-zero — the test is
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

# Inventory F072: `netlib_validate_host` rejects characters outside
# `[a-z0-9.-]`. The pre-existing T11.5 only exercises the space
# character; we additionally exercise other illegal chars (underscore,
# `@`, `$`, `:`) so a regression that loosens the charclass to allow
# any of them surfaces here. Each must (a) exit non-zero and (b)
# include the 'invalid host' diagnostic.
for bad_host in 'bad_host.example.com' 'bad@host.example.com' 'bad$host.example.com' 'bad:host.example.com'; do
    set +e
    bad_out=$("${HARNESS_BIN}" net allow "${bad_host}" 2>&1)
    bad_rc=$?
    set -e
    if (( bad_rc == 0 )); then
        echo "[harness-test] T11 FAIL [F072]: net allow '${bad_host}' unexpectedly succeeded" >&2
        echo "${bad_out}" >&2; exit 1
    fi
    if ! grep -qi 'invalid host' <<<"${bad_out}"; then
        echo "[harness-test] T11 FAIL [F072]: net allow '${bad_host}' missing 'invalid host' diagnostic" >&2
        echo "${bad_out}" >&2; exit 1
    fi
    # Confirm the rejected host did NOT get added to the allowlist.
    if grep -qF "${bad_host}" "${TEST_ROOT}/.harness-allowlist"; then
        echo "[harness-test] T11 FAIL [F072]: rejected host '${bad_host}' leaked into allowlist" >&2
        cat "${TEST_ROOT}/.harness-allowlist" >&2; exit 1
    fi
done

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
    proxy_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        ps -q proxy 2>/dev/null || true)
    if [[ -n "${proxy_id}" ]]; then
        proxy_status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        if [[ "${proxy_status}" == "healthy" ]]; then break; fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[harness-test] T12 FAIL: services not healthy after restart" >&2; exit 1
    fi
    sleep 2
done
echo "[harness-test] T12 OK"

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
# add.
cat >"${UPG_ROOT}/.env" <<'EOF'
PROXY_API_URL=https://placeholder.invalid/v1/chat/completions
PROXY_API_KEY=test-key
DEFAULT_MODEL_NAME=harness
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
if ! grep -q '^MODEL_CONTEXT_LENGTH=' "${UPG_ROOT}/.env"; then
    echo "[harness-test] T17 FAIL: MODEL_CONTEXT_LENGTH not added to .env after upgrade" >&2
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

# --- Test 17b: harness upgrade --resume-after-pull skips pull -------------
#
# Issue #66: after a successful 'git pull' that advanced HEAD, cmd_upgrade
# re-execs into the freshly-pulled harness with --resume-after-pull so the
# post-pull cmd_upgrade orchestration runs from new bytes. This test
# covers the flag itself: --resume-after-pull is accepted by the parser
# and skips the pull step (the previous instance already did it). The
# UPG_ROOT here has NO git remote configured, so a real 'git pull' would
# fail — the test succeeding proves the pull was skipped.

echo "[harness-test] T17b: harness upgrade --resume-after-pull"
UPG17B_ROOT="$(mktemp -d -t harness-upg17b.XXXXXX)"
cleanup_upg17b() {
    if [[ -n "${UPG17B_ROOT:-}" && -d "${UPG17B_ROOT}" ]]; then
        rm -rf "${UPG17B_ROOT}"
    fi
}
trap 'cleanup_upg17b; restore_agent_image; cleanup' EXIT INT TERM
ln -s "${REPO_ROOT}" "${UPG17B_ROOT}/harness"
cat >"${UPG17B_ROOT}/.env" <<'EOF'
PROXY_API_URL=https://placeholder.invalid/v1/chat/completions
PROXY_API_KEY=test-key
DEFAULT_MODEL_NAME=harness
EOF
echo "github.com" >"${UPG17B_ROOT}/.harness-allowlist"

set +e
upg17b_out=$(HARNESS_INSTALL_ROOT="${UPG17B_ROOT}" HARNESS_PROJECT_NAME="harness-upg17b" \
    "${UPG17B_ROOT}/harness/harness" upgrade --resume-after-pull --no-prompt --no-restart 2>&1)
upg17b_rc=$?
set -e
if (( upg17b_rc != 0 )); then
    echo "[harness-test] T17b FAIL: --resume-after-pull rc=${upg17b_rc}" >&2
    echo "${upg17b_out}" >&2; exit 1
fi
# The skip message must come from the resume-after-pull branch, not the
# HARNESS_UPGRADE_SKIP_PULL branch — otherwise we'd be testing the env-var
# path that T17 already covers.
if ! grep -q 'continuing from re-exec after pull' <<<"${upg17b_out}"; then
    echo "[harness-test] T17b FAIL: did not see resume-after-pull skip message" >&2
    echo "${upg17b_out}" >&2; exit 1
fi
# Sanity-check: the manifest still ran (an env var should have been added).
if ! grep -q '^MODEL_CONTEXT_LENGTH=' "${UPG17B_ROOT}/.env"; then
    echo "[harness-test] T17b FAIL: MODEL_CONTEXT_LENGTH not added — manifest did not run after pull-skip" >&2
    cat "${UPG17B_ROOT}/.env" >&2; exit 1
fi
cleanup_upg17b
trap 'restore_agent_image; cleanup' EXIT INT TERM
echo "[harness-test] T17b OK"

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

# --- Test 19b: harness_jq prefers host jq when available -------------------
#
# Inventory F015: harness_jq uses the host jq binary when present (rather
# than falling back to the proxy container). We source the wrapper with
# HARNESS_SOURCE_ONLY=1 inside a subshell, stub harness_docker so any
# fallback attempt would fail loudly, and assert harness_jq still
# returns the correct result. Success proves the host-jq branch was
# taken — the fallback would have invoked harness_docker (stubbed to
# fail) and bubbled the failure out.
echo "[harness-test] T19b: harness_jq prefers host jq (F015)"
if ! command -v jq >/dev/null 2>&1; then
    echo "[harness-test] T19b SKIP: host jq not installed (cannot exercise F015)" >&2
else
    jq_host_out=$(
        HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
        # If host-jq is preferred, this stub must never be called.
        harness_docker() { echo "FALLBACK_DOCKER_INVOKED" >&2; return 1; }
        echo '{"k":"hostjq"}' | harness_jq -r '.k'
    )
    if [[ "${jq_host_out}" != "hostjq" ]]; then
        echo "[harness-test] T19b FAIL [F015]: harness_jq did not return host-jq result; got '${jq_host_out}'" >&2
        exit 1
    fi
fi
echo "[harness-test] T19b OK"

# --- Test 19c: compose() arg threading (F131, F133) ------------------------
#
# Inventory F131: compose() threads HARNESS_PROJECT_NAME through every
# compose invocation as `--project-name <project_name>`.
# Inventory F133: compose() always passes `-f <docker-compose.yml>` as
# the base compose file (plus a runtime override if present).
#
# We source the harness wrapper with HARNESS_SOURCE_ONLY=1, stub
# harness_docker to print its received args, and call compose with a
# distinctive trailing token so we can locate it in the captured args.
echo "[harness-test] T19c: compose() arg threading (F131, F133)"
compose_args_out=$(
    HARNESS_PROJECT_NAME="harness-compose-args-test" \
    HARNESS_INSTALL_ROOT="${TEST_ROOT}" \
    HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist" \
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    # Stub: avoid any real docker invocation; just echo the args we
    # would have passed.
    harness_docker() { printf 'HD_ARGS:'; printf ' %s' "$@"; printf '\n'; }
    # Stub: avoid writing the runtime override file (we don't care
    # about its content for this assertion).
    write_runtime_override() { :; }
    compose ps --sentinel-token-d19c 2>&1
)
# Inventory F131: --project-name with the configured project name appears
# in the captured compose args.
if ! grep -Eq -- '--project-name[[:space:]]+harness-compose-args-test' <<<"${compose_args_out}"; then
    echo "[harness-test] T19c FAIL [F131]: compose() did not pass --project-name" >&2
    echo "${compose_args_out}" >&2; exit 1
fi
# Inventory F133: -f <docker-compose.yml> base compose file is threaded
# into every compose invocation.
if ! grep -Eq -- '-f[[:space:]]+[^[:space:]]*docker-compose\.yml' <<<"${compose_args_out}"; then
    echo "[harness-test] T19c FAIL [F133]: compose() did not pass -f docker-compose.yml" >&2
    echo "${compose_args_out}" >&2; exit 1
fi
# Sanity: our distinctive trailing token reached harness_docker, proving
# we actually captured a compose() invocation (and not some earlier
# helper call that happens to mention --project-name).
if ! grep -q -- '--sentinel-token-d19c' <<<"${compose_args_out}"; then
    echo "[harness-test] T19c FAIL: sentinel token missing — compose() didn't reach harness_docker" >&2
    echo "${compose_args_out}" >&2; exit 1
fi
echo "[harness-test] T19c OK"

# --- Test 19d: compose() host-path conversion for Windows Git Bash (#46) --
#
# Repro for issue #46: on Git Bash for Windows, harness_docker sets
# MSYS_NO_PATHCONV=1 (so in-container args like `--entrypoint /bin/bash`
# aren't mangled) — which means host-side unix paths like
# /c/Users/handel.sim/harness/.env are NOT auto-translated and docker.exe
# treats them as drive-root paths, resolving them to C:\c\Users\... and
# failing with "couldn't find env file". The fix is to run each host
# path through harness_docker_path before passing it to docker compose;
# this test asserts the call sites actually do that.
#
# We stub harness_docker_path to a recognizable transformation (prefix
# WIN: ) and stub harness_docker to print its args. Then we call compose
# with a sentinel token and assert the captured args contain WIN:-prefixed
# values for the base compose -f, --env-file, and runtime-override -f.
echo "[harness-test] T19d: compose() host-path conversion (#46)"

# Seed a runtime override so the -f override branch is exercised.
mkdir -p "${TEST_ROOT}/state"
cat >"${TEST_ROOT}/state/.harness-runtime.yml" <<'EOF'
# stub runtime override for T19d
services: {}
EOF

t19d_out=$(
    HARNESS_PROJECT_NAME="harness-19d" \
    HARNESS_INSTALL_ROOT="${TEST_ROOT}" \
    HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist" \
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    # Stub: stand in for cygpath -m. Recognizable prefix lets us assert
    # the call site routed through this helper.
    harness_docker_path() { printf 'WIN:%s\n' "$1"; }
    # Stub: skip the real runtime-override regeneration; we seeded one above.
    write_runtime_override() { :; }
    # Stub: capture the args compose() ultimately passes to the runtime.
    harness_docker() { printf 'HD_ARGS:'; printf ' %s' "$@"; printf '\n'; }
    compose ps --sentinel-token-d19d 2>&1
)
# Sentinel reached harness_docker (we actually captured a compose call).
if ! grep -q -- '--sentinel-token-d19d' <<<"${t19d_out}"; then
    echo "[harness-test] T19d FAIL: sentinel token missing — compose() didn't reach harness_docker" >&2
    echo "${t19d_out}" >&2; exit 1
fi
# Base compose file path is converted.
if ! grep -Eq -- '-f[[:space:]]+WIN:[^[:space:]]*docker-compose\.yml' <<<"${t19d_out}"; then
    echo "[harness-test] T19d FAIL: base -f docker-compose.yml not converted via harness_docker_path" >&2
    echo "${t19d_out}" >&2; exit 1
fi
# --env-file path is converted.
if ! grep -Eq -- '--env-file[[:space:]]+WIN:[^[:space:]]*\.env' <<<"${t19d_out}"; then
    echo "[harness-test] T19d FAIL: --env-file not converted via harness_docker_path" >&2
    echo "${t19d_out}" >&2; exit 1
fi
# Runtime override -f path is converted.
if ! grep -Eq -- '-f[[:space:]]+WIN:[^[:space:]]*\.harness-runtime\.yml' <<<"${t19d_out}"; then
    echo "[harness-test] T19d FAIL: runtime override -f not converted via harness_docker_path" >&2
    echo "${t19d_out}" >&2; exit 1
fi
# Negative check: no raw, unconverted host path slipped through next to
# -f or --env-file. Anything coming from the install root or clone dir
# should be WIN:-prefixed; the only other path-looking values legitimately
# in argv are container-side paths (none in this invocation).
if grep -Eq -- '(-f|--env-file)[[:space:]]+/[^[:space:]]*\.(yml|env)' <<<"${t19d_out}"; then
    echo "[harness-test] T19d FAIL: an unconverted /path/...yml slipped past harness_docker_path" >&2
    echo "${t19d_out}" >&2; exit 1
fi
rm -f "${TEST_ROOT}/state/.harness-runtime.yml"
echo "[harness-test] T19d OK"

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
for needle in 'PROXY_API_URL is set' 'PROXY_API_KEY is set' 'DEFAULT_MODEL_NAME is set'; do
    if ! grep -q "${needle}" <<<"${preflight_out}"; then
        echo "[harness-test] T20 FAIL: preflight missing line: ${needle}" >&2
        echo "${preflight_out}" >&2; exit 1
    fi
done
# Match the runtime line for either docker or podman (preflight prefixes the
# line with the resolved runtime name).
if ! grep -Eq '(docker|podman)\s+runtime' <<<"${preflight_out}"; then
    echo "[harness-test] T20 FAIL: preflight missing container runtime line" >&2
    echo "${preflight_out}" >&2; exit 1
fi
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

# --- Test 23: update check is synchronous and tolerant of failure --------
#
# Issue #9: with the prior async-write-for-next-run design, an upstream
# advance only became visible on the SECOND invocation after the advance.
# The new helper does a bounded synchronous ls-remote so first-run drift
# surfaces immediately, with the cache demoted to an offline fallback.
# These cases exercise the helper in isolation by sourcing the harness
# script with HARNESS_SOURCE_ONLY=1 and a synthetic install root pointed
# at a real local git repo with a local-path origin (no network).

echo "[harness-test] T23: update check (sync, fallback, skip)"

UPD_ROOT="$(mktemp -d -t harness-upd-test.XXXXXX)"
UPD_REMOTE="$(mktemp -d -t harness-upd-remote.XXXXXX)"
cleanup_upd() {
    if [[ -n "${UPD_ROOT:-}" && -d "${UPD_ROOT}" ]]; then rm -rf "${UPD_ROOT}"; fi
    if [[ -n "${UPD_REMOTE:-}" && -d "${UPD_REMOTE}" ]]; then rm -rf "${UPD_REMOTE}"; fi
}
trap 'cleanup_upd; restore_agent_image; cleanup' EXIT INT TERM

# Bare remote that the local checkout will use as origin. Local file path,
# no network involved — `git ls-remote` against it succeeds in <100ms.
git init --bare --initial-branch=main "${UPD_REMOTE}" >/dev/null

# Local checkout: init, commit, push to bare remote.
git init --initial-branch=main "${UPD_ROOT}" >/dev/null
(
    cd "${UPD_ROOT}"
    git config user.email "harness-test@invalid.local"
    git config user.name  "harness test"
    echo a >a
    git add a
    git commit -q -m "initial"
    git remote add origin "${UPD_REMOTE}"
    git push -q origin main
)

# Advance the remote one commit ahead of the local checkout. Use a throw-
# away clone so UPD_ROOT's local HEAD stays where it is.
ADV="$(mktemp -d -t harness-upd-adv.XXXXXX)"
git clone -q "${UPD_REMOTE}" "${ADV}"
(
    cd "${ADV}"
    git config user.email "harness-test@invalid.local"
    git config user.name  "harness test"
    echo b >b
    git add b
    git commit -q -m "advance"
    git push -q origin main
)
rm -rf "${ADV}"

# T23.1 — first invocation (no cache) shows the banner and writes cache.
if [[ -e "${UPD_ROOT}/state/.harness-update-check" ]]; then
    echo "[harness-test] T23.1 FAIL: cache shouldn't exist before first run" >&2
    exit 1
fi
upd_out_1=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${UPD_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    _update_check_and_banner 4 2>&1
)
if ! grep -q 'update available' <<<"${upd_out_1}"; then
    echo "[harness-test] T23.1 FAIL: first run should show banner; got: ${upd_out_1}" >&2
    exit 1
fi
if [[ ! -f "${UPD_ROOT}/state/.harness-update-check" ]]; then
    echo "[harness-test] T23.1 FAIL: cache not written" >&2
    exit 1
fi
echo "[harness-test] T23.1 OK"

# T23.2 — HARNESS_SKIP_UPDATE_CHECK=1 suppresses the banner entirely.
# The env-prefix syntax `VAR=val command` only sets the var for that one
# command; we need it active when the helper runs, so `export` it inside
# the subshell (matches the pattern used by gate_case in T0).
upd_out_skip=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${UPD_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    export HARNESS_SKIP_UPDATE_CHECK=1
    _update_check_and_banner 4 2>&1
)
if grep -q 'update available' <<<"${upd_out_skip}"; then
    echo "[harness-test] T23.2 FAIL: HARNESS_SKIP_UPDATE_CHECK=1 should suppress banner" >&2
    exit 1
fi
echo "[harness-test] T23.2 OK"

# T23.3 — when origin is unreachable, fall back to cached value. Point
# origin at a nonexistent local path; ls-remote fails fast with empty
# stdout. Pre-seed the cache with a SHA that differs from local HEAD.
(
    cd "${UPD_ROOT}"
    git remote set-url origin "/nonexistent/harness-upd-bad-$$"
)
echo "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" > "${UPD_ROOT}/state/.harness-update-check"
upd_out_offline=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${UPD_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    _update_check_and_banner 4 2>&1
)
if ! grep -q 'update available' <<<"${upd_out_offline}"; then
    echo "[harness-test] T23.3 FAIL: offline path should fall back to cached banner; got: ${upd_out_offline}" >&2
    exit 1
fi
echo "[harness-test] T23.3 OK"

# T23.4 — origin still unreachable, but cache absent: no banner, no error.
# This is the case the user specifically asked us to defend: even when
# the check yields nothing, the helper must return cleanly so callers can
# continue past it.
rm -f "${UPD_ROOT}/state/.harness-update-check"
upd_rc=0
upd_out_silent=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${UPD_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    _update_check_and_banner 4 2>&1
) || upd_rc=$?
if (( upd_rc != 0 )); then
    echo "[harness-test] T23.4 FAIL: helper exited rc=${upd_rc}; should always succeed" >&2
    exit 1
fi
if grep -q 'update available' <<<"${upd_out_silent}"; then
    echo "[harness-test] T23.4 FAIL: no cache + offline should print no banner; got: ${upd_out_silent}" >&2
    exit 1
fi
echo "[harness-test] T23.4 OK"

# Inventory F022: `harness check-updates` (the explicit foreground
# command) exits non-zero on network failure when no cached value
# exists. Origin is still unreachable from T23.3 (set to a nonexistent
# local path), and we removed the cache for T23.4 — so we can drive
# the F022 path directly. We invoke `harness check-updates` (not the
# helper) to assert the user-facing command's failure mode: rc != 0
# plus the "could not reach origin/main" diagnostic on stderr.
set +e
chk_out=$(HARNESS_INSTALL_ROOT="${UPD_ROOT}" HARNESS_PROJECT_NAME="harness-upd-test" \
    "${REPO_ROOT}/harness" check-updates 2>&1)
chk_rc=$?
set -e
if (( chk_rc == 0 )); then
    echo "[harness-test] T23.4b FAIL [F022]: check-updates should exit non-zero with no cache + offline" >&2
    echo "${chk_out}" >&2
    exit 1
fi
if ! grep -qi 'could not reach origin/main' <<<"${chk_out}"; then
    echo "[harness-test] T23.4b FAIL [F022]: check-updates missing 'could not reach origin/main' diagnostic" >&2
    echo "${chk_out}" >&2
    exit 1
fi
echo "[harness-test] T23.4b OK"

# T23.5 — local HEAD == remote HEAD: no banner. Restore origin and pull
# the missing commit so the local checkout matches.
(
    cd "${UPD_ROOT}"
    git remote set-url origin "${UPD_REMOTE}"
    git pull -q origin main
)
rm -f "${UPD_ROOT}/state/.harness-update-check"
upd_out_uptodate=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${UPD_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    _update_check_and_banner 4 2>&1
)
if grep -q 'update available' <<<"${upd_out_uptodate}"; then
    echo "[harness-test] T23.5 FAIL: up-to-date checkout should not show banner; got: ${upd_out_uptodate}" >&2
    exit 1
fi
echo "[harness-test] T23.5 OK"

cleanup_upd
trap 'restore_agent_image; cleanup' EXIT INT TERM
echo "[harness-test] T23 OK"

# --- Test 24: run_agent honors the agent firewall override on a jq-less
#     host (#8) -------------------------------------------------------------
#
# Repro for #8: `harness net open agent` writes firewall_disabled=true to
# .harness-net-overrides.json, but run_agent read it with bare `jq` gated
# on `command -v jq`. On hosts without host jq (Windows Git Bash) the gate
# failed silently, the override block was skipped, and the agent launched
# with the firewall still enabled. The fix routes the read through
# harness_jq (host jq OR container fallback) with no `command -v jq` gate.
#
# We simulate a jq-less host by stubbing `command -v jq` to report jq
# missing, stub harness_jq to stand in for the container fallback, and
# assert run_agent still computes net_open=1. With the old gate in place
# the override block would be skipped and net_open would stay 0.
echo "[harness-test] T24: run_agent honors agent override on jq-less host (#8)"
t24_rc=0
t24_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    net_overrides_path="${TEST_ROOT}/t24-net-overrides.json"
    printf '%s\n' '{"services":{"agent":{"firewall_disabled":true}}}' >"$net_overrides_path"
    # Simulate a host without jq: the old `command -v jq` gate must fail.
    command() {
        if [[ "${1:-}" == "-v" && "${2:-}" == "jq" ]]; then return 1; fi
        builtin command "$@"
    }
    # Stand in for harness_jq's container fallback.
    harness_jq() { echo "true"; }
    require_docker() { :; }
    _gate_on_upstream_auth() { return 0; }
    ensure_services_up() { :; }
    ensure_dirs() { :; }
    _update_check_and_banner() { :; }
    # Capture the net_open value run_agent computed and stop the launch.
    warn_if_firewall_open() { echo "NET_OPEN=$1"; exit 77; }
    run_agent opencode 2>&1
) || t24_rc=$?
if (( t24_rc != 77 )); then
    echo "[harness-test] T24 FAIL: expected run_agent to reach warn_if_firewall_open (rc=77), got ${t24_rc}" >&2
    echo "${t24_out}" >&2; exit 1
fi
if ! grep -q 'NET_OPEN=1' <<<"${t24_out}"; then
    echo "[harness-test] T24 FAIL [#8]: run_agent ignored the agent firewall override (expected NET_OPEN=1)" >&2
    echo "${t24_out}" >&2; exit 1
fi
echo "[harness-test] T24 OK"

# --- Test 25: warn_if_firewall_open warns on a jq-less host (#8) -----------
#
# Same #8 root cause: the pre-launch firewall warning read the overrides
# file with bare `jq` gated on `command -v jq`, so on a jq-less host the
# loud "FIREWALL IS DISABLED" warning was silently suppressed. The fix
# routes the read through harness_jq with no gate. HARNESS_NET_CONFIRM=1
# short-circuits the interactive prompt so the call returns 0.
echo "[harness-test] T25: warn_if_firewall_open warns on jq-less host (#8)"
t25_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    net_overrides_path="${TEST_ROOT}/t25-net-overrides.json"
    printf '%s\n' '{"services":{"proxy":{"firewall_disabled":true}}}' >"$net_overrides_path"
    command() {
        if [[ "${1:-}" == "-v" && "${2:-}" == "jq" ]]; then return 1; fi
        builtin command "$@"
    }
    # Stand in for harness_jq's container fallback: list the open service.
    harness_jq() { echo "proxy"; }
    HARNESS_NET_CONFIRM=1 warn_if_firewall_open 0 opencode 2>&1
)
if ! grep -q 'NETWORK FIREWALL IS DISABLED' <<<"${t25_out}"; then
    echo "[harness-test] T25 FAIL [#8]: warn_if_firewall_open suppressed the warning on a jq-less host" >&2
    echo "${t25_out}" >&2; exit 1
fi
if ! grep -q 'firewall DISABLED: proxy' <<<"${t25_out}"; then
    echo "[harness-test] T25 FAIL [#8]: warn_if_firewall_open did not name the open service" >&2
    echo "${t25_out}" >&2; exit 1
fi
echo "[harness-test] T25 OK"

# --- Test 26: jq sidecar is started once per invocation and reused (#8) ----
#
# The jq-less fallback no longer spawns a container per call — it starts
# one long-lived `harness-jq-$$` sidecar and runs `docker exec` against it.
# We simulate a jq-less host, stub the runtime to fake just enough for
# _ensure_jq_sidecar (and to run the real host `jq` for the `docker exec`
# leg), call harness_jq twice, and assert exactly one `run -d` and two
# `docker exec` calls were issued.
echo "[harness-test] T26: jq sidecar is started once and reused (#8)"
t26_calls="${TEST_ROOT}/t26-docker-calls"
: > "${t26_calls}"
t26_rc=0
t26_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    install_root="${TEST_ROOT}"
    t26_state="${TEST_ROOT}/t26-sidecar-up"
    rm -f "${t26_state}"
    # Simulate a jq-less host: the host-jq probe must report jq missing.
    command() {
        if [[ "${1:-}" == "-v" && "${2:-}" == "jq" ]]; then return 1; fi
        builtin command "$@"
    }
    harness_container_runtime() { echo "docker"; }
    # Stub the runtime: record every call, fake just enough behaviour. The
    # `exec` leg runs the real host jq so harness_jq still produces output.
    harness_docker() {
        printf '%s\n' "$*" >> "${t26_calls}"
        case "$1" in
            container)  # container inspect -f '{{.State.Running}}' <name>
                if [[ -f "${t26_state}" ]]; then echo "true"; else echo ""; fi
                ;;
            info)  return 0 ;;
            image) return 0 ;;                       # image present, skip build
            ps)    return 0 ;;                       # sweep finds nothing
            run)   : > "${t26_state}"; echo "cid" ;; # run -d --name ...
            exec)  shift 3; "$@" ;;                  # exec -i <name> jq <args>
            rm)    rm -f "${t26_state}"; return 0 ;;
            *)     return 0 ;;
        esac
    }
    v1=$(echo '{"k":"v1"}' | harness_jq -r '.k')
    v2=$(echo '{"k":"v2"}' | harness_jq -r '.k')
    echo "v1=${v1} v2=${v2}"
) || t26_rc=$?
if (( t26_rc != 0 )); then
    echo "[harness-test] T26 FAIL [#8]: harness_jq sidecar path errored (rc=${t26_rc})" >&2
    cat "${t26_calls}" >&2; exit 1
fi
if [[ "${t26_out}" != "v1=v1 v2=v2" ]]; then
    echo "[harness-test] T26 FAIL [#8]: harness_jq via sidecar produced wrong output: '${t26_out}'" >&2
    cat "${t26_calls}" >&2; exit 1
fi
t26_runs=$(grep -c '^run -d --name harness-jq-' "${t26_calls}" || true)
t26_execs=$(grep -c '^exec -i harness-jq-' "${t26_calls}" || true)
if [[ "${t26_runs}" != "1" ]]; then
    echo "[harness-test] T26 FAIL [#8]: expected 1 sidecar 'run -d', got ${t26_runs}" >&2
    cat "${t26_calls}" >&2; exit 1
fi
if [[ "${t26_execs}" != "2" ]]; then
    echo "[harness-test] T26 FAIL [#8]: expected 2 'docker exec' jq calls, got ${t26_execs}" >&2
    cat "${t26_calls}" >&2; exit 1
fi
echo "[harness-test] T26 OK"

# --- Test 26pm: start/restart --prompt-mode flag (#91) ---------------------
#
# `harness start/restart --prompt-mode <mode>` is the ephemeral replacement
# for the removed PROXY_PROMPT_MODE .env knob. It must (a) validate the mode
# in _parse_prompt_mode_flag, and (b) inject PROXY_PROMPT_MODE onto the proxy
# service via write_runtime_override — folded into a single proxy: block when
# the proxy also has a firewall opt-out.
echo "[harness-test] T26pm: --prompt-mode flag (#91)"

# Case 1: _parse_prompt_mode_flag accepts both forms and rejects bad input.
t26pm_parse=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    _parse_prompt_mode_flag --prompt-mode passthrough
    echo "space=${prompt_mode_override}"
    prompt_mode_override=""
    _parse_prompt_mode_flag --prompt-mode=user_front
    echo "equals=${prompt_mode_override}"
)
if ! grep -q 'space=passthrough' <<<"${t26pm_parse}"; then
    echo "[harness-test] T26pm FAIL: '--prompt-mode passthrough' did not set the override" >&2
    echo "${t26pm_parse}" >&2; exit 1
fi
if ! grep -q 'equals=user_front' <<<"${t26pm_parse}"; then
    echo "[harness-test] T26pm FAIL: '--prompt-mode=user_front' did not set the override" >&2
    echo "${t26pm_parse}" >&2; exit 1
fi
# Invalid mode and unknown option must both abort non-zero.
t26pm_bad_rc=0
( HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
  _parse_prompt_mode_flag --prompt-mode bogus ) >/dev/null 2>&1 || t26pm_bad_rc=$?
if (( t26pm_bad_rc == 0 )); then
    echo "[harness-test] T26pm FAIL: invalid --prompt-mode value was accepted" >&2; exit 1
fi
t26pm_unknown_rc=0
( HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
  _parse_prompt_mode_flag --frobnicate ) >/dev/null 2>&1 || t26pm_unknown_rc=$?
if (( t26pm_unknown_rc == 0 )); then
    echo "[harness-test] T26pm FAIL: unknown start/restart option was accepted" >&2; exit 1
fi

# Case 2: write_runtime_override emits a standalone proxy block when only the
# prompt-mode override is set (no firewall opt-out).
t26pm_ov="${TEST_ROOT}/t26pm-standalone.yml"
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    runtime_override="${t26pm_ov}"
    net_overrides_path="${TEST_ROOT}/t26pm-absent-net-overrides.json"  # does not exist
    prompt_mode_override="user_front"
    write_runtime_override
)
if ! grep -q 'PROXY_PROMPT_MODE: "user_front"' "${t26pm_ov}" 2>/dev/null; then
    echo "[harness-test] T26pm FAIL: standalone override missing PROXY_PROMPT_MODE" >&2
    cat "${t26pm_ov}" >&2 2>/dev/null || true; exit 1
fi
if ! grep -qE '^  proxy:' "${t26pm_ov}"; then
    echo "[harness-test] T26pm FAIL: standalone override missing a proxy: service block" >&2
    cat "${t26pm_ov}" >&2; exit 1
fi

# Case 3: when the proxy ALSO has a firewall opt-out, the prompt mode folds
# into the SAME proxy: block (one mapping, both env entries).
t26pm_fold="${TEST_ROOT}/t26pm-fold.yml"
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    runtime_override="${t26pm_fold}"
    net_overrides_path="${TEST_ROOT}/t26pm-net-overrides.json"
    printf '%s\n' '{"services":{"proxy":{"firewall_disabled":true}}}' >"$net_overrides_path"
    harness_jq() { echo "proxy"; }   # stand in for the jq query → service list
    prompt_mode_override="hybrid"
    write_runtime_override
)
fold_proxy_count=$(grep -cE '^  proxy:' "${t26pm_fold}" || true)
if [[ "${fold_proxy_count}" != "1" ]]; then
    echo "[harness-test] T26pm FAIL: expected exactly one proxy: block, got ${fold_proxy_count}" >&2
    cat "${t26pm_fold}" >&2; exit 1
fi
if ! grep -q 'HARNESS_FIREWALL_DISABLED: "1"' "${t26pm_fold}"; then
    echo "[harness-test] T26pm FAIL: folded block missing the firewall opt-out entry" >&2
    cat "${t26pm_fold}" >&2; exit 1
fi
if ! grep -q 'PROXY_PROMPT_MODE: "hybrid"' "${t26pm_fold}"; then
    echo "[harness-test] T26pm FAIL: folded block missing PROXY_PROMPT_MODE" >&2
    cat "${t26pm_fold}" >&2; exit 1
fi
rm -f "${t26pm_ov}" "${t26pm_fold}" "${TEST_ROOT}/t26pm-net-overrides.json"
echo "[harness-test] T26pm OK"

# --- Test 27: _reap_jq_sidecar tears down only when a sidecar exists (#8) --
#
# _reap_jq_sidecar runs from the EXIT trap on every harness invocation,
# including ones that never touch jq or docker — so it must be a cheap
# no-op unless a sidecar was actually created. It uses a per-PID marker
# file under state/ as that signal. Case A: no marker -> no docker call.
# Case B: marker present -> `docker rm -f` the sidecar and clear the marker.
echo "[harness-test] T27: _reap_jq_sidecar reaps only when a sidecar marker exists (#8)"
t27_calls="${TEST_ROOT}/t27-docker-calls"

: > "${t27_calls}"
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    install_root="${TEST_ROOT}"
    harness_docker() { printf '%s\n' "$*" >> "${t27_calls}"; }
    rm -f "${TEST_ROOT}/state/.harness-jq-sidecar.$$"
    _reap_jq_sidecar
)
if [[ -s "${t27_calls}" ]]; then
    echo "[harness-test] T27 FAIL [#8]: _reap_jq_sidecar touched docker with no marker present" >&2
    cat "${t27_calls}" >&2; exit 1
fi

: > "${t27_calls}"
t27_rc=0
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    install_root="${TEST_ROOT}"
    harness_docker() { printf '%s\n' "$*" >> "${t27_calls}"; }
    mkdir -p "${TEST_ROOT}/state"
    : > "${TEST_ROOT}/state/.harness-jq-sidecar.$$"
    _reap_jq_sidecar
    if [[ -f "${TEST_ROOT}/state/.harness-jq-sidecar.$$" ]]; then
        echo "marker not cleared" >&2; exit 1
    fi
) || t27_rc=$?
if (( t27_rc != 0 )); then
    echo "[harness-test] T27 FAIL [#8]: _reap_jq_sidecar did not clear the marker" >&2
    exit 1
fi
if ! grep -q '^rm -f harness-jq-' "${t27_calls}"; then
    echo "[harness-test] T27 FAIL [#8]: _reap_jq_sidecar did not 'docker rm -f' the sidecar" >&2
    cat "${t27_calls}" >&2; exit 1
fi
echo "[harness-test] T27 OK"

# --- Test 28: _sweep_stale_jq_sidecars removes dead-PID sidecars only (#8) -
#
# The sweep is the self-healing safety net: it removes `harness-jq-<pid>`
# containers whose owning process is gone, but must leave alone sidecars
# owned by harness invocations that are still running. We feed it one
# dead PID and one live PID (distinct from $$) and assert only the dead
# one is removed.
echo "[harness-test] T28: _sweep_stale_jq_sidecars removes dead-PID sidecars only (#8)"
t28_calls="${TEST_ROOT}/t28-docker-calls"
: > "${t28_calls}"
t28_rc=0
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    install_root="${TEST_ROOT}"
    # A definitely-dead PID: spawn a trivial process and reap it.
    sleep 0 & dead_pid=$!
    wait "${dead_pid}" 2>/dev/null || true
    # A definitely-live PID, distinct from the harness PID ($$).
    sleep 30 & live_pid=$!
    harness_docker() {
        case "$1" in
            ps) printf '%s\n' "harness-jq-${dead_pid}" "harness-jq-${live_pid}" ;;
            rm) printf '%s\n' "$3" >> "${t28_calls}" ;;   # rm -f <name>
            *)  return 0 ;;
        esac
    }
    _sweep_stale_jq_sidecars
    kill "${live_pid}" 2>/dev/null || true
    wait "${live_pid}" 2>/dev/null || true
    if ! grep -qx "harness-jq-${dead_pid}" "${t28_calls}"; then
        echo "dead-PID sidecar was not swept" >&2; exit 1
    fi
    if grep -qx "harness-jq-${live_pid}" "${t28_calls}"; then
        echo "live-PID sidecar was wrongly swept" >&2; exit 1
    fi
) || t28_rc=$?
if (( t28_rc != 0 )); then
    echo "[harness-test] T28 FAIL [#8]: stale-sidecar sweep behaved incorrectly" >&2
    cat "${t28_calls}" >&2; exit 1
fi
echo "[harness-test] T28 OK"

# --- Test 29: same-dir agents get unique names, no refusal (#76) ------------
#
# Issue #76: relaunching an agent from a directory that already has one was
# refused because the container name was a deterministic hash of (tool, dir),
# which collided on Docker's unique-name constraint. The fix makes the name
# carry a per-launch random suffix and drops the "already running here" guard.
#
# Part A (F054): two agent_container_name calls for the same tool return
# DIFFERENT names, both prefixed harness-<tool>-. This inverts the old
# determinism assumption.
# Part B (F055): run_agent_interactive can be invoked twice for the same
# tool+dir without refusing — both reach `docker exec` with distinct --name.
echo "[harness-test] T29: same-dir agents get unique names, no refusal (#76)"

t29a_rc=0
(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    n1=$(agent_container_name opencode)
    n2=$(agent_container_name opencode)
    if [[ -z "${n1}" || -z "${n2}" ]]; then
        echo "empty container name (n1='${n1}' n2='${n2}')" >&2; exit 1
    fi
    if [[ "${n1}" != harness-opencode-* ]]; then
        echo "name missing harness-<tool>- prefix: '${n1}'" >&2; exit 1
    fi
    if [[ "${n1}" == "${n2}" ]]; then
        echo "two calls returned identical names ('${n1}'); expected per-launch uniqueness" >&2; exit 1
    fi
) || t29a_rc=$?
if (( t29a_rc != 0 )); then
    echo "[harness-test] T29 FAIL [#76,F054]: agent_container_name is not unique per launch" >&2
    exit 1
fi

t29_calls="${TEST_ROOT}/t29-run-calls"
: > "${t29_calls}"
# run_agent_interactive now runs docker as a child and `exit`s (so the issue
# footer can print after — #81); each launch must therefore run in its own
# subshell. Two same-dir launches must both reach docker run with distinct names.
t29b_rc=0
for _ in 1 2; do
    (
        HARNESS_INSTALL_ROOT="${TEST_ROOT}" \
        HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist" \
        HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
        # Stubs: keep the launch path off any real daemon/state.
        collect_agent_env() { :; }                       # emit no env lines
        harness_docker_path() { printf '%s\n' "$1"; }    # passthrough host paths
        _reap_jq_sidecar() { :; }
        # The launch path runs docker as a child now (no exec); record the run
        # args. Nothing should exec anymore.
        harness_docker_exec() { echo "UNEXPECTED_EXEC_CALL: $*" >&2; return 0; }
        harness_docker() { printf '%s\n' "$*" >> "${t29_calls}"; }
        run_agent_interactive opencode 0 0 /work/proj img /home/agent harness-net ""
    ) 2>/dev/null || t29b_rc=$?   # discard the post-run footer noise; the
                                  # run-count check below catches any regression
    (( t29b_rc != 0 )) && break
done
if (( t29b_rc != 0 )); then
    echo "[harness-test] T29 FAIL [#76]: run_agent_interactive errored on repeat same-dir launch (rc=${t29b_rc})" >&2
    cat "${t29_calls}" >&2; exit 1
fi
t29_runs=$(grep -c . "${t29_calls}" || true)
if (( t29_runs != 2 )); then
    echo "[harness-test] T29 FAIL [#76,F055]: expected 2 interactive launches to reach docker run, got ${t29_runs}" >&2
    cat "${t29_calls}" >&2; exit 1
fi
t29_names=$(grep -oE 'harness-opencode-[a-f0-9]+' "${t29_calls}" | sort -u | grep -c . || true)
if (( t29_names != 2 )); then
    echo "[harness-test] T29 FAIL [#76,F055]: same-dir launches did not get distinct --name values" >&2
    cat "${t29_calls}" >&2; exit 1
fi

# Part C (#81): an interactive launch prints the issues footer to stderr after
# the run, and propagates the container's exit code.
t29c_err="${TEST_ROOT}/t29c-stderr"
t29c_rc=0
# `|| t29c_rc=$?` is required: under `set -e` an unguarded subshell that exits
# non-zero aborts the whole script before $? can be captured.
(
    HARNESS_INSTALL_ROOT="${TEST_ROOT}" \
    HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist" \
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null
    collect_agent_env() { :; }
    harness_docker_path() { printf '%s\n' "$1"; }
    _reap_jq_sidecar() { :; }
    harness_docker() { return 7; }   # simulate the container exiting non-zero
    run_agent_interactive opencode 0 0 /work/proj img /home/agent harness-net ""
) 2>"${t29c_err}" || t29c_rc=$?
if (( t29c_rc != 7 )); then
    echo "[harness-test] T29 FAIL [#81]: interactive launch did not propagate container exit code (want 7, got ${t29c_rc})" >&2
    cat "${t29c_err}" >&2; exit 1
fi
if ! grep -q 'github.com/HandelSim/harness/issues' "${t29c_err}"; then
    echo "[harness-test] T29 FAIL [#81]: issues footer not printed to stderr after interactive run" >&2
    cat "${t29c_err}" >&2; exit 1
fi

# Part D (#81): -p / print mode must NOT emit the footer (scripted output).
t29d_err="${TEST_ROOT}/t29d-stderr"
(
    HARNESS_INSTALL_ROOT="${TEST_ROOT}" \
    HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist" \
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null
    collect_agent_env() { :; }
    harness_docker_path() { printf '%s\n' "$1"; }
    _reap_jq_sidecar() { :; }
    harness_docker_exec() { return 0; }   # stubbed: don't actually exec
    run_agent_print opencode 0 0 /work/proj img /home/agent harness-net "" -p "hi"
) 2>"${t29d_err}" || true
if grep -q 'github.com/HandelSim/harness/issues' "${t29d_err}"; then
    echo "[harness-test] T29 FAIL [#81]: -p/print mode must not emit the issues footer" >&2
    cat "${t29d_err}" >&2; exit 1
fi
echo "[harness-test] T29 OK"

# --- Test 30: option-C dispatch (bare/flag → opencode, bad word errors) -----
#
# main() routes a bare invocation, or one whose first arg is an agent flag,
# to `run_agent opencode`; a leading bare word is still a subcommand and an
# unknown one must error (typo protection). The help flags are the one flag
# exception.
echo "[harness-test] T30: option-C dispatch routing"

# Unknown bare word → non-zero exit with an 'unknown command' message.
set +e
t30_word_out=$("${HARNESS_BIN}" definitelynotacommand 2>&1)
t30_word_rc=$?
set -e
if (( t30_word_rc == 0 )); then
    echo "[harness-test] T30 FAIL: unknown bare word should exit non-zero" >&2
    exit 1
fi
if ! grep -q "unknown command" <<<"${t30_word_out}"; then
    echo "[harness-test] T30 FAIL: unknown bare word should report 'unknown command'" >&2
    echo "${t30_word_out}" >&2; exit 1
fi

# Bare invocation (no args) routes to run_agent opencode.
t30_bare_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    run_agent() { echo "RUN_AGENT_CALLED tool=$1"; }
    main
)
if ! grep -q 'RUN_AGENT_CALLED tool=opencode' <<<"${t30_bare_out}"; then
    echo "[harness-test] T30 FAIL: bare 'harness' did not route to run_agent opencode" >&2
    echo "${t30_bare_out}" >&2; exit 1
fi

# A leading agent flag routes to run_agent opencode (not an unknown-command
# error) and forwards the flag.
t30_flag_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    run_agent() { echo "RUN_AGENT_CALLED tool=$1 args=$*"; }
    main --yolo -p "hi"
)
if ! grep -q 'RUN_AGENT_CALLED tool=opencode args=opencode --yolo -p hi' <<<"${t30_flag_out}"; then
    echo "[harness-test] T30 FAIL: leading flag did not route to run_agent opencode with forwarded args" >&2
    echo "${t30_flag_out}" >&2; exit 1
fi

# The help flags stay help, not an agent launch.
t30_help_out=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    run_agent() { echo "RUN_AGENT_CALLED"; }
    cmd_help() { echo "CMD_HELP_CALLED"; }
    main --help
)
if ! grep -q 'CMD_HELP_CALLED' <<<"${t30_help_out}" || grep -q 'RUN_AGENT_CALLED' <<<"${t30_help_out}"; then
    echo "[harness-test] T30 FAIL: --help should show help, not launch an agent" >&2
    echo "${t30_help_out}" >&2; exit 1
fi
echo "[harness-test] T30 OK"

# --- Test 31: DEFAULT_MODEL_NAME CLI passthrough (#94) -----------------------
#
# DEFAULT_MODEL_NAME is the single source of truth for the default/fallback
# model id (it replaced OLLAMA_AGENT_MODEL + PROXY_API_MODEL). It is REQUIRED
# with no hardcoded default, so the CLI's agent_model is empty when the var is
# unset/blank and passes an explicit value through verbatim. The install root
# is an empty tmpdir so no real .env is sourced over the var.
echo "[harness-test] T31: DEFAULT_MODEL_NAME CLI passthrough"
T31_ROOT="$(mktemp -d -t harness-t31.XXXXXX)"
cleanup_t31() { [[ -n "${T31_ROOT:-}" && -d "${T31_ROOT}" ]] && rm -rf "${T31_ROOT}"; }
trap 'cleanup_t31; restore_agent_image; cleanup' EXIT INT TERM

t31_default=$(
    unset DEFAULT_MODEL_NAME
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${T31_ROOT}" \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    printf '%s' "${agent_model}"
)
if [[ -n "${t31_default}" ]]; then
    echo "[harness-test] T31 FAIL: unset DEFAULT_MODEL_NAME should yield empty agent_model, got '${t31_default}'" >&2
    exit 1
fi

t31_explicit=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${T31_ROOT}" DEFAULT_MODEL_NAME=mymodel \
        source "${HARNESS_BIN}" >/dev/null 2>&1
    printf '%s' "${agent_model}"
)
if [[ "${t31_explicit}" != "mymodel" ]]; then
    echo "[harness-test] T31 FAIL: explicit agent_model='${t31_explicit}' != mymodel" >&2
    exit 1
fi
cleanup_t31
trap 'restore_agent_image; cleanup' EXIT INT TERM
echo "[harness-test] T31 OK"

# --- Test 32: harness downgrade tag resolution + confirm/abort (#85) ---------
#
# `_downgrade_target_tag` resolves the previous release tag topologically
# (git describe, no version parsing — so two-number tags like v0.1/v1.0 work):
# from an untagged tip it lands on the latest tag; sitting exactly on a tag it
# steps to the prior one; on the earliest tag it errors. `cmd_downgrade` then
# does a loud-confirm `git reset --hard` (default N) — declining must leave
# HEAD untouched, accepting must move HEAD to the target. Pure git fixtures,
# no docker.
echo "[harness-test] T32: harness downgrade"
DG_ROOT="$(mktemp -d -t harness-dg.XXXXXX)"
cleanup_dg() { [[ -n "${DG_ROOT:-}" && -d "${DG_ROOT}" ]] && rm -rf "${DG_ROOT}"; }
trap 'cleanup_dg; restore_agent_image; cleanup' EXIT INT TERM
(
    cd "${DG_ROOT}"
    git init -q
    git config user.email t@t
    git config user.name t
    echo 1 >f; git add f; git commit -qm c1; git tag v0.1
    echo 2 >f; git commit -qam c2; git tag v1.0
    echo 3 >f; git commit -qam c3
)
DG_BRANCH="$(cd "${DG_ROOT}" && git rev-parse --abbrev-ref HEAD)"
DG_TIP="$(cd "${DG_ROOT}" && git rev-parse HEAD)"
DG_V10="$(cd "${DG_ROOT}" && git rev-parse v1.0^{commit})"

# T32.1 — from the untagged tip, target is the latest tag (v1.0).
t32_tip=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    clone_dir="${DG_ROOT}"; _downgrade_target_tag
)
if [[ "${t32_tip}" != "v1.0" ]]; then
    echo "[harness-test] T32.1 FAIL: tip target '${t32_tip}' != v1.0" >&2; exit 1
fi

# T32.2 — sitting exactly on v1.0, target is the prior tag (v0.1).
(cd "${DG_ROOT}" && git checkout -q v1.0)
t32_ontag=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    clone_dir="${DG_ROOT}"; _downgrade_target_tag
)
if [[ "${t32_ontag}" != "v0.1" ]]; then
    echo "[harness-test] T32.2 FAIL: on-tag target '${t32_ontag}' != v0.1" >&2; exit 1
fi

# T32.3 — sitting on the earliest tag, no earlier tag → non-zero, no output.
(cd "${DG_ROOT}" && git checkout -q v0.1)
set +e
t32_none=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    clone_dir="${DG_ROOT}"; _downgrade_target_tag
)
t32_none_rc=$?
set -e
if (( t32_none_rc == 0 )) || [[ -n "${t32_none}" ]]; then
    echo "[harness-test] T32.3 FAIL: earliest tag should yield rc!=0 + empty (rc=${t32_none_rc} out='${t32_none}')" >&2; exit 1
fi

(cd "${DG_ROOT}" && git checkout -q "${DG_BRANCH}")

# T32.4 — decline the reset: HEAD unchanged, non-zero exit.
set +e
t32_abort=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    clone_dir="${DG_ROOT}"; HARNESS_CONFIRM_FROM_STDIN=1
    require_docker() { :; }
    printf 'n\n' | cmd_downgrade --no-restart 2>&1
)
t32_abort_rc=$?
set -e
if (( t32_abort_rc == 0 )); then
    echo "[harness-test] T32.4 FAIL: declined downgrade should exit non-zero" >&2
    echo "${t32_abort}" >&2; exit 1
fi
if [[ "$(cd "${DG_ROOT}" && git rev-parse HEAD)" != "${DG_TIP}" ]]; then
    echo "[harness-test] T32.4 FAIL: declined downgrade moved HEAD" >&2; exit 1
fi

# T32.5 — accept the reset with --no-restart: HEAD moves to v1.0, exit 0,
# no rebuild attempted (no docker available here).
set +e
t32_do=$(
    HARNESS_SOURCE_ONLY=1 source "${HARNESS_BIN}" >/dev/null 2>&1
    clone_dir="${DG_ROOT}"; HARNESS_CONFIRM_FROM_STDIN=1
    require_docker() { :; }
    printf 'y\n' | cmd_downgrade --no-restart 2>&1
)
t32_do_rc=$?
set -e
if (( t32_do_rc != 0 )); then
    echo "[harness-test] T32.5 FAIL: accepted downgrade rc=${t32_do_rc}" >&2
    echo "${t32_do}" >&2; exit 1
fi
if [[ "$(cd "${DG_ROOT}" && git rev-parse HEAD)" != "${DG_V10}" ]]; then
    echo "[harness-test] T32.5 FAIL: HEAD not reset to v1.0" >&2; exit 1
fi
cleanup_dg
trap 'restore_agent_image; cleanup' EXIT INT TERM
echo "[harness-test] T32 OK"

# --- Test 33: interactive TTY resolution (#82) -----------------------------
#
# On Windows Git Bash, bash's `[[ -t 0 ]]` (an MSYS pty) and the native
# docker.exe's isTerminal() (a real Windows console) disagree, so harness must
# probe the runtime rather than trust bash, and may wrap the launch in winpty.
# The decision lives in the pure (I/O-free) helper harness_tty_strategy so it
# is testable without a real tty or daemon; the live resolver and the winpty
# wrapper are exercised with stubs. The Windows branches can't actually run on
# Linux CI (they're gated on harness_detect_os == windows), so we drive the
# logic directly with explicit inputs.
echo "[harness-test] T33: interactive TTY resolution (#82)"
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# T33.1 — pure decision matrix. Each row: os tty rt_ok winpty -> expected.
t33_check() {
    local got exp="$5"
    got=$(harness_tty_strategy "$1" "$2" "$3" "$4")
    if [[ "${got}" != "${exp}" ]]; then
        echo "[harness-test] T33.1 FAIL: tty_strategy($1,$2,$3,$4) = '${got}', want '${exp}'" >&2
        exit 1
    fi
}
t33_check linux   1 0 0 it          # linux tty → it (rt_ok/winpty irrelevant)
t33_check linux   0 0 0 i           # linux no-tty → i
t33_check macos   1 0 0 it          # macos tty → it
t33_check windows 0 1 1 i           # windows, bash has no tty → i regardless
t33_check windows 1 1 0 it          # windows, docker accepts -t → it
t33_check windows 1 0 1 it-winpty   # windows, docker rejects -t, winpty present
t33_check windows 1 0 0 i           # windows, docker rejects -t, no winpty → i

# T33.2 — resolver reflects a non-tty stdin as `i` and stays silent (no
# degraded note) because the pipe case is expected, not a Windows TTY split.
# Force the Windows branch but feed non-tty stdin; the note must not appear.
t33_res=$(
    harness_detect_os() { printf windows; }
    harness_runtime_tty_ok() { return 0; }   # must not be consulted (no tty)
    harness_winpty_available() { return 0; }
    harness_resolve_interactive_tty fake-image 2>/dev/null </dev/null
)
if [[ "${t33_res}" != "i" ]]; then
    echo "[harness-test] T33.2 FAIL: resolver with non-tty stdin = '${t33_res}', want 'i'" >&2
    exit 1
fi
t33_note=$(
    harness_detect_os() { printf windows; }
    harness_runtime_tty_ok() { return 1; }
    harness_winpty_available() { return 1; }
    harness_resolve_interactive_tty fake-image 2>&1 </dev/null >/dev/null
)
if [[ -n "${t33_note}" ]]; then
    echo "[harness-test] T33.2 FAIL: degraded note printed for a plain pipe: '${t33_note}'" >&2
    exit 1
fi

# T33.3 — harness_docker_winpty routes the runtime call through winpty. Stub
# the runtime name and shadow `winpty` with a recorder; assert ordering.
t33_winpty=$(
    harness_container_runtime() { printf 'FAKE_RT'; }
    winpty() { printf 'WINPTY:'; printf ' %s' "$@"; printf '\n'; }
    harness_docker_winpty run --rm --sentinel-t33 hello
)
if [[ "${t33_winpty}" != "WINPTY: FAKE_RT run --rm --sentinel-t33 hello" ]]; then
    echo "[harness-test] T33.3 FAIL: winpty wrapper routing wrong: '${t33_winpty}'" >&2
    exit 1
fi
echo "[harness-test] T33 OK"

echo "============================================================"
echo " HARNESS TEST PASSED"
echo "============================================================"
exit 0
