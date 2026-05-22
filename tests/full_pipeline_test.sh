#!/usr/bin/env bash
#
# tests/full_pipeline_test.sh — full installation-to-running pipeline test.
#
# This is the most comprehensive automated test we ship. It:
#
#   1. stages harness-install.sh into a clean tmpdir as a "fresh install"
#      surface (mirroring what a user gets from a manual download of the
#      installer),
#   2. runs harness-install.sh non-interactively (cloning the local repo,
#      not GitHub — via HARNESS_REPO_URL),
#   3. exercises every major harness subcommand with a mock upstream,
#   4. runs `harness -p` (bare dispatch) and `harness opencode -p` print-mode
#      round trips against the mock,
#   5. tears everything down on exit.
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

# Cross-platform helpers (harness_docker, harness_docker_path).
# shellcheck source=lib/platform.sh
source "${REPO_ROOT}/scripts/lib/platform.sh"

PROJECT_NAME="harness-pipeline-test"
NETWORK="${PROJECT_NAME}_harness-net"
MOCK_NAME="harness-pipeline-test-mockupstream"

echo "============================================================"
echo " harness full pipeline test"
echo "============================================================"

# --- preflight --------------------------------------------------------------

if ! harness_docker info >/dev/null 2>&1; then
    echo "[pipeline] ERROR: container runtime ($(harness_container_runtime)) not reachable" >&2
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

    # Tear down the mock upstream sidecar.
    harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true

    # Tear down any T16 MCP fixture that leaked.
    harness_docker rm -f "${PROJECT_NAME}_pipe_mcp" >/dev/null 2>&1 || true

    # Stop any agent containers labeled by this project.
    local stragglers
    stragglers=$(harness_docker ps -aq --filter "label=harness.agent=true" 2>/dev/null || true)
    if [[ -n "${stragglers}" ]]; then
        # We can't easily filter by project label, but harness-pipeline-test
        # agents are mounted on $TEST_WORKSPACE. The simplest belt-and-braces
        # is to remove anything with our deterministic name pattern.
        for c in ${stragglers}; do
            local mount
            mount=$(harness_docker inspect -f '{{ index .Config.Labels "harness.mount" }}' "$c" 2>/dev/null || true)
            if [[ "${mount}" == "${TEST_WORKSPACE}"* ]]; then
                harness_docker rm -f "$c" >/dev/null 2>&1 || true
            fi
        done
    fi

    # harness down is idempotent — the symlink may not exist if install
    # bailed early, in which case we fall back to compose directly.
    if [[ -x "${FAKE_HOME}/.local/bin/harness" ]]; then
        HOME="${FAKE_HOME}" HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
            "${FAKE_HOME}/.local/bin/harness" down >/dev/null 2>&1 || true
    fi
    harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        down -v --remove-orphans >/dev/null 2>&1 || true

    # Network may linger if we created it manually for the mockupstream.
    harness_docker network rm "${NETWORK}" >/dev/null 2>&1 || true

    # ollama-data may contain files owned by a uid we can't directly remove
    # (the in-container ollama runs as root, so blobs land owned by host
    # uid 0). Use a privileged docker run to wipe the path before letting
    # the host rm -rf finish the job.
    for d in "${TEST_ROOT}" "${FAKE_HOME}" "${TEST_WORKSPACE}"; do
        if [[ -d "$d" ]]; then
            if ! rm -rf "$d" 2>/dev/null; then
                harness_docker run --rm -v "$(harness_docker_path "$d"):/target" --user 0:0 alpine \
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
harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    down -v --remove-orphans >/dev/null 2>&1 || true
harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true

# --- T0: stage installer ---------------------------------------------------

echo "[pipeline] T0: stage harness-install.sh into a fresh tmpdir"
cp "${REPO_ROOT}/harness-install.sh" "${TEST_ROOT}/harness-install.sh"
chmod +x "${TEST_ROOT}/harness-install.sh"
echo "[pipeline] T0 OK: ${TEST_ROOT}/harness-install.sh"

# --- T0b: failed clone aborts a SOURCED run + beside-.env proxy (#106) -------
#
# Issue #106: a clone that fails (no network / unreachable host) used to print
# a misleading "✓ cloned" and keep going, producing a broken half-install —
# because when the installer is `source`d (the README-recommended path) strict
# mode is off and the abort `return` was buried in a helper, so it never ended
# the script. Drive that exact scenario: point HARNESS_REPO_URL at a
# non-existent path and run the installer SOURCED. It must (a) abort non-zero,
# (b) print a clone-failure message, (c) NOT print "install complete", and
# (d) leave no half-install behind. Also drop a .env beside the installer with
# HTTPS_PROXY set and assert the installer announces it uses that for the clone
# (the #106 follow-up: beside-.env proxy feeds the clone, host env is the
# fallback when unset).
echo "[pipeline] T0b: failed clone aborts sourced run + beside-.env proxy"
CLONE_FAIL_ROOT="${TEST_ROOT}/clonefail"
mkdir -p "${CLONE_FAIL_ROOT}"
cp "${REPO_ROOT}/harness-install.sh" "${CLONE_FAIL_ROOT}/harness-install.sh"
cat >"${CLONE_FAIL_ROOT}/.env" <<EOF
HTTPS_PROXY=http://t0b-beside-proxy.invalid:3128
EOF
set +e
# Source (not execute) the installer so we exercise the sourced-abort path.
# Prompts before the clone are answered n,n (no PATH, no API key). A local-path
# clone ignores the bogus proxy URL, so the failure is purely the missing repo.
( cd "${CLONE_FAIL_ROOT}" \
  && HOME="${FAKE_HOME}" HARNESS_REPO_URL="${TEST_ROOT}/does-not-exist-$$" \
       bash -c 'source ./harness-install.sh' <<<$'n\nn\n' ) \
  >"${CLONE_FAIL_ROOT}/clonefail.log" 2>&1
t0b_rc=$?
set -e
if (( t0b_rc == 0 )); then
    echo "[pipeline] T0b FAIL: sourced installer returned 0 on a failed clone" >&2
    cat "${CLONE_FAIL_ROOT}/clonefail.log" >&2
    exit 1
fi
grep -Eq 'git clone of .* failed' "${CLONE_FAIL_ROOT}/clonefail.log" \
    || { echo "[pipeline] T0b FAIL: no clone-failure message printed" >&2; cat "${CLONE_FAIL_ROOT}/clonefail.log" >&2; exit 1; }
if grep -q 'install complete' "${CLONE_FAIL_ROOT}/clonefail.log"; then
    echo "[pipeline] T0b FAIL: 'install complete' printed despite a failed clone" >&2
    cat "${CLONE_FAIL_ROOT}/clonefail.log" >&2
    exit 1
fi
grep -q 'using HTTPS_PROXY from .* for the clone' "${CLONE_FAIL_ROOT}/clonefail.log" \
    || { echo "[pipeline] T0b FAIL: beside-.env HTTPS_PROXY not used for the clone" >&2; cat "${CLONE_FAIL_ROOT}/clonefail.log" >&2; exit 1; }
if [[ -e "${CLONE_FAIL_ROOT}/harness/state" ]]; then
    echo "[pipeline] T0b FAIL: half-install left behind (state/ created after a failed clone)" >&2
    exit 1
fi
echo "[pipeline] T0b OK (rc=${t0b_rc})"

# --- T1: install flow -------------------------------------------------------

echo "[pipeline] T1: run harness-install.sh from staged dir"

# Pre-fill .env so harness-install.sh's "edit .env" prompt is unnecessary. Values
# point PROXY_API_URL at the mockupstream sidecar we'll bring up later.
cat >"${TEST_ROOT}/.env" <<EOF
PROXY_API_URL=http://mockupstream:9000/v1/chat/completions
PROXY_API_KEY=test-key-1234
DEFAULT_MODEL_NAME=harness
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_CONTEXT_LENGTH=200000
PUBLISH_OLLAMA_PORT=
MOCK_SCENARIO=text
EOF

# harness-install.sh prompts: add to PATH? [y/n], enter API key? [y/n], then
# (on y) the key itself. Send: y, y, <key>. Supplying the key here exercises
# issue #67's PROXY_API_KEY rewrite — the prompt value wins over the pre-placed
# .env's PROXY_API_KEY=test-key-1234 (mock upstream ignores the key, so the
# downstream round-trip is unaffected).
# harness-install.sh installs into $(pwd) — must cd into TEST_ROOT first.
# Issue #68: a corp proxy exported in the installing shell must be persisted
# into the seeded .env (host-side — honored for host git AND for the docker
# image builds further down). The local-path clone ignores the (bogus) proxy,
# so it's safe to set here; we assert persistence below, then scrub it so the
# docker-driven part of the pipeline doesn't route 'docker compose build'
# through the unreachable bogus URL. HTTPS_PROXY is scoped to this subshell's
# install command and does not leak to the outer test shell.
PIPE_PROXY="http://pipeline-corp-proxy.invalid:3128"
(
    cd "${TEST_ROOT}"
    HOME="${FAKE_HOME}" HARNESS_REPO_URL="${REPO_ROOT}" HTTPS_PROXY="${PIPE_PROXY}" \
        bash "${TEST_ROOT}/harness-install.sh" <<<$'y\ny\nharness-pipeline-prompt-key\n' >"${TEST_ROOT}/install.log" 2>&1
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
        --exclude='__pycache__/' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='.harness-allowlist' \
        --exclude='.harness-net-overrides.json' \
        --exclude='state/' \
        "${REPO_ROOT}/" "${TEST_ROOT}/harness/"
else
    # Fallback: tar-pipe (preserves modes; excludes via tar-style globs).
    ( cd "${REPO_ROOT}" && tar --exclude='.git' \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='./.env' --exclude='./.harness-allowlist' \
        --exclude='./.harness-net-overrides.json' \
        --exclude='./state' -cf - . ) \
        | ( cd "${TEST_ROOT}/harness" && tar -xf - )
fi

echo "[pipeline] T1 OK"

# --- T1b: corp-proxy persisted into seeded .env (issue #68) ------------------
#
# The installer ran with HTTPS_PROXY exported; it must have written that value
# into the install root's .env (filling the blank line, or appending it) so
# later 'harness' runs reuse the proxy without re-exporting. The .env is
# excluded from the working-tree overlay above, so the persisted value
# survives. After asserting, scrub the proxy lines: harness now lets the proxy
# reach 'docker compose build' (issue #68 fix), so leaving the bogus URL in
# .env would route the pipeline's build through an unreachable host and hang.
# The build passthrough itself is proven directly by tests/harness_test.sh T7b.
echo "[pipeline] T1b: corp proxy persisted into .env"
pipe_env="${TEST_ROOT}/harness/.env"
if ! grep -Eq "^[[:space:]]*HTTPS_PROXY=${PIPE_PROXY//./\\.}$" "${pipe_env}"; then
    echo "[pipeline] T1b FAIL: HTTPS_PROXY=${PIPE_PROXY} not persisted into ${pipe_env}" >&2
    grep -nE '^[[:space:]]*HTTPS?_PROXY=' "${pipe_env}" >&2 || echo "(no proxy lines present)" >&2
    exit 1
fi
# Scrub both host-proxy lines so nothing downstream inherits the bogus URL.
proxy_scrub_tmp="${pipe_env}.scrub.$$"
grep -vE '^[[:space:]]*(HTTP_PROXY|HTTPS_PROXY)=' "${pipe_env}" >"${proxy_scrub_tmp}"
mv -f "${proxy_scrub_tmp}" "${pipe_env}"
echo "[pipeline] T1b OK"

# Inventory I009: harness-install.sh defaults REPO_URL to the public GitHub URL.
# The test overrides via HARNESS_REPO_URL, but the source still must carry the
# documented default literal so an unset env yields the production behavior.
grep -Eq 'REPO_URL="\$\{HARNESS_REPO_URL:-https://github\.com/HandelSim/harness\}"' \
    "${REPO_ROOT}/harness-install.sh" \
    || { echo "[pipeline] T1 FAIL: I009 default REPO_URL literal missing in harness-install.sh" >&2; exit 1; }

# Inventory I012: after clone, the installer sources the real scripts/lib/platform.sh.
# Verify the file is present in the clone (required for sourcing) and that the
# installer source contains the sourcing line that runs post-clone.
[[ -f "${TEST_ROOT}/harness/scripts/lib/platform.sh" ]] \
    || { echo "[pipeline] T1 FAIL: I012 scripts/lib/platform.sh missing in clone" >&2; exit 1; }
grep -Eq 'source[[:space:]]+"\$install_root/scripts/lib/platform\.sh"' \
    "${REPO_ROOT}/harness-install.sh" \
    || { echo "[pipeline] T1 FAIL: I012 post-clone source of platform.sh missing in harness-install.sh" >&2; exit 1; }

# --- T2: install verification ---------------------------------------------

echo "[pipeline] T2: install layout"
[[ -x "${FAKE_HOME}/.local/bin/harness" ]]              || { echo "[pipeline] T2 FAIL: harness wrapper missing or not executable" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/.git" ]]                    || { echo "[pipeline] T2 FAIL: clone is not a git repo" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/output" ]]            || { echo "[pipeline] T2 FAIL: state/output/ missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/agent/home" ]]        || { echo "[pipeline] T2 FAIL: state/agent/home/ missing" >&2; exit 1; }
[[ -d "${TEST_ROOT}/harness/state/ollama-data" ]]       || { echo "[pipeline] T2 FAIL: state/ollama-data/ missing" >&2; exit 1; }
[[ -f "${TEST_ROOT}/harness/.env" ]]                    || { echo "[pipeline] T2 FAIL: .env missing in clone" >&2; exit 1; }
[[ -f "${TEST_ROOT}/harness/.harness-allowlist" ]]      || { echo "[pipeline] T2 FAIL: .harness-allowlist missing in clone" >&2; exit 1; }

# Issue #67: a pre-placed .env beside the installer is COPIED into the clone
# (not moved — source must survive), and a prompt-supplied API key is written
# into PROXY_API_KEY, overwriting the pre-placed value while leaving the rest
# of the file intact.
grep -q '^PROXY_API_KEY=harness-pipeline-prompt-key$' "${TEST_ROOT}/harness/.env" \
    || { echo "[pipeline] T2 FAIL: #67 installer did not write prompt-supplied PROXY_API_KEY into .env" >&2; grep '^PROXY_API_KEY=' "${TEST_ROOT}/harness/.env" >&2; exit 1; }
grep -q '^PROXY_API_URL=http://mockupstream:9000/v1/chat/completions$' "${TEST_ROOT}/harness/.env" \
    || { echo "[pipeline] T2 FAIL: #67 PROXY_API_KEY rewrite clobbered other .env vars (PROXY_API_URL lost)" >&2; exit 1; }
[[ -f "${TEST_ROOT}/.env" ]] \
    || { echo "[pipeline] T2 FAIL: #67 pre-placed source .env was removed (copy, not move, expected)" >&2; exit 1; }

# Inventory I024: wrapper hard-codes the install-root path. Grep the wrapper
# body for the literal install root we expect ("${TEST_ROOT}/harness/harness").
grep -Fq "${TEST_ROOT}/harness/harness" "${FAKE_HOME}/.local/bin/harness" \
    || { echo "[pipeline] T2 FAIL: I024 wrapper does not hard-code install-root harness path" >&2; cat "${FAKE_HOME}/.local/bin/harness" >&2; exit 1; }

# Inventory F006: real harness script ships the _self_path resolver so it
# works when invoked through a symlink. Check the function and its
# realpath/readlink fallback are present in the cloned script.
grep -q '^_self_path()' "${TEST_ROOT}/harness/harness" \
    || { echo "[pipeline] T2 FAIL: F006 _self_path function missing from installed harness script" >&2; exit 1; }
grep -Eq 'realpath|readlink' "${TEST_ROOT}/harness/harness" \
    || { echo "[pipeline] T2 FAIL: F006 _self_path lacks realpath/readlink resolver" >&2; exit 1; }

# Inventory I005: installer preflight verifies docker/podman compose works.
# The install completed, so compose must be callable; assert the runtime
# really does answer `compose version` here as a direct check.
harness_docker compose version >/dev/null 2>&1 \
    || { echo "[pipeline] T2 FAIL: I005 'docker compose version' not working" >&2; exit 1; }
echo "[pipeline] T2 OK"

# --- T3: harness help -------------------------------------------------------

echo "[pipeline] T3: harness help"
help_out=$(harness_call help)
for cmd in start down opencode doctor list stop; do
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
        cid=$(harness_docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" \
            ps -q "${svc}" 2>/dev/null || true)
        if [[ -n "${cid}" ]]; then
            local status
            status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")
            if [[ "${status}" == "healthy" ]]; then
                return 0
            fi
        fi
        if (( $(date +%s) >= deadline )); then
            echo "[pipeline] timed out waiting for ${svc}" >&2
            harness_docker compose --project-name "${PROJECT_NAME}" \
                -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
            return 1
        fi
        sleep 2
    done
}

if ! wait_healthy_compose ollama 90; then echo "[pipeline] T5 FAIL ollama" >&2; exit 1; fi
if ! wait_healthy_compose proxy 90; then echo "[pipeline] T5 FAIL proxy" >&2; exit 1; fi

# Inventory F139: warn_if_firewall_open is silent when net-overrides is
# empty or absent. start.log must not contain the firewall-disabled banner
# in this default-config run (no .harness-net-overrides.json present).
if grep -q "NETWORK FIREWALL IS DISABLED" "${TEST_ROOT}/start.log"; then
    echo "[pipeline] T5 FAIL: F139 firewall-open warning bled into start with empty overrides" >&2
    cat "${TEST_ROOT}/start.log" >&2
    exit 1
fi

# Inventory F026 + F135 + Pe010: write_runtime_override manages
# state/.harness-runtime.yml as a generated file. With this test's .env
# (empty PUBLISH_OLLAMA_PORT, no net-overrides), the override body is empty
# and the writer removes the file. Assert the documented behavior: either
# the file is absent, or — if present — it carries the generator header
# (i.e. is a generated artifact, not a stale hand-written file).
runtime_override="${TEST_ROOT}/harness/state/.harness-runtime.yml"
if [[ -f "${runtime_override}" ]]; then
    grep -q '^# Generated by harness; do not edit\.' "${runtime_override}" \
        || { echo "[pipeline] T5 FAIL: F026/F135/Pe010 runtime override file lacks generator header" >&2; cat "${runtime_override}" >&2; exit 1; }
fi

# Inventory Pe005: state/ollama-data/ holds model registration data and
# survives restarts. After a successful start (ollama is healthy), the
# directory must contain at least one persisted artifact (manifest/blob).
if ! find "${TEST_ROOT}/harness/state/ollama-data" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    echo "[pipeline] T5 FAIL: Pe005 state/ollama-data/ is empty after start (no model registration persisted)" >&2
    ls -la "${TEST_ROOT}/harness/state/ollama-data" >&2 || true
    exit 1
fi

# Build agent images (the harness script doesn't auto-build agents on start;
# the install relies on `compose --profile agent build`). The pipeline
# test needs both images present for T9–T11.
echo "[pipeline] T5: building agent images (compose --profile agent build)"
harness_docker compose --project-name "${PROJECT_NAME}" \
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
mock_py_host=$(harness_docker_path "${REPO_ROOT}/tests/mock_upstream.py")
harness_docker run -d \
    --name "${MOCK_NAME}" \
    --network "${NETWORK}" \
    --network-alias mockupstream \
    -e MOCK_SCENARIO=text \
    -v "${mock_py_host}:/app/mock_upstream.py:ro" \
    -w /app \
    python:3.12-slim \
    sh -c 'pip install --quiet --no-cache-dir flask==3.0.3 && python /app/mock_upstream.py' \
    >/dev/null

# Wait for /health from inside the network (use the ollama container as a
# probe; it's already on the network and has curl).
ollama_cid=$(harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q ollama)
deadline=$(( $(date +%s) + 60 ))
while true; do
    if harness_docker exec "${ollama_cid}" curl -sf http://mockupstream:9000/health >/dev/null 2>&1; then
        break
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[pipeline] T6 FAIL: mockupstream never became reachable" >&2
        harness_docker logs "${MOCK_NAME}" >&2 || true
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

# --- T9: bare `harness -p` (headless, dispatches to opencode) --------------
#
# Bare `harness` with only agent flags launches an opencode agent (option C
# dispatch). This exercises that path end-to-end and the gosu privilege drop.

echo "[pipeline] T9: harness -p (bare dispatch)"
cd "${TEST_WORKSPACE}"
set +e
t9_out=$(timeout 60 bash -c 'HOME='"'${FAKE_HOME}'"' HARNESS_PROJECT_NAME='"'${PROJECT_NAME}'"' '"'${FAKE_HOME}/.local/bin/harness'"' -p "say hello" 2>&1 < /dev/null')
t9_rc=$?
set -e
cd "${REPO_ROOT}"
echo "[pipeline]   T9 raw (truncated): $(echo "${t9_out}" | tail -c 800)"

# opencode's `run` may require interactive provider auth on some versions. If
# we hit that, skip the round-trip assertion but still verify the
# privilege-drop side effects below (they happen in the entrypoint before the
# agent itself runs, so they hold regardless).
t9_round_trip_ok=1
if (( t9_rc != 0 )); then
    if echo "${t9_out}" | grep -qiE 'auth|login|provider .* not (configured|found)|no .* api key'; then
        echo "[pipeline] T9: opencode run requires interactive provider auth — skipping round-trip assertion"
        t9_round_trip_ok=0
    else
        echo "[pipeline] T9 FAIL: harness -p exited ${t9_rc}" >&2
        echo "${t9_out}" >&2
        exit 1
    fi
fi
if (( t9_round_trip_ok )); then
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
fi

# Inventory A007: root-side init drops privileges to harness via gosu before
# exec'ing the agent. After T9 ran, files under the shared agent home must
# be owned by the host UID (not root). The agent-home bind mount lives at
# ${TEST_ROOT}/harness/state/agent/home; .harness-home-initialized is created
# by the user-side init AFTER the gosu drop, so its owner == host uid is
# direct evidence the drop happened.
agent_home_marker="${TEST_ROOT}/harness/state/agent/home/.harness-home-initialized"
[[ -f "${agent_home_marker}" ]] \
    || { echo "[pipeline] T9 FAIL: A007 home-initialized marker absent — gosu/user-side init did not run" >&2; exit 1; }
marker_uid=$(stat -c '%u' "${agent_home_marker}" 2>/dev/null || echo "")
[[ "${marker_uid}" == "$(id -u)" ]] \
    || { echo "[pipeline] T9 FAIL: A007 home-initialized marker owner=${marker_uid}, expected host uid=$(id -u) — agent did not drop privileges" >&2; exit 1; }

# Inventory A018: ensure_opencode_config writes ~/.config/opencode/opencode.json
# on every launch. T9 booted opencode through agents/entrypoint.sh, which calls
# ensure_opencode_config BEFORE exec'ing the agent — so the config lands in the
# bind-mounted agent home regardless of whether opencode's own run later hit the
# provider-auth skip above (t9_round_trip_ok=0). Assert the file exists in the
# shared home AND carries the harness-managed provider/model block (not just any
# non-empty file). This is the boot-smoke folded in from the former e2e
# 01-opencode-boot scenario.
opencode_cfg="${TEST_ROOT}/harness/state/agent/home/.config/opencode/opencode.json"
[[ -f "${opencode_cfg}" ]] \
    || { echo "[pipeline] T9 FAIL: A018 ensure_opencode_config did not write opencode.json on launch (${opencode_cfg})" >&2; ls -la "${TEST_ROOT}/harness/state/agent/home/.config/opencode" >&2 || true; exit 1; }
grep -q '"harness"' "${opencode_cfg}" \
    || { echo "[pipeline] T9 FAIL: A018 opencode.json missing the harness provider block" >&2; cat "${opencode_cfg}" >&2; exit 1; }
grep -q '"model": "harness/' "${opencode_cfg}" \
    || { echo "[pipeline] T9 FAIL: A018 opencode.json missing the harness model binding" >&2; cat "${opencode_cfg}" >&2; exit 1; }
# #94 A035: the provider display name is the fixed string "GenAI Harness"
# (hardcoded in the agent entrypoint; no longer user-configurable).
grep -q '"name": "GenAI Harness"' "${opencode_cfg}" \
    || { echo "[pipeline] T9 FAIL: A035 opencode.json provider name is not the fixed 'GenAI Harness'" >&2; cat "${opencode_cfg}" >&2; exit 1; }
# #94 A036: the model dropdown is built from ollama /api/tags, which discovered
# 'harness' via the proxy's /v1/models route — so a model entry named 'harness'
# (distinct from the provider whose name is 'GenAI Harness') is registered.
grep -q '"name": "harness"' "${opencode_cfg}" \
    || { echo "[pipeline] T9 FAIL: A036 opencode.json model dropdown missing the discovered 'harness' model" >&2; cat "${opencode_cfg}" >&2; exit 1; }
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

# opencode's `run` may require interactive provider auth on some opencode
# versions. If we hit that, skip with a clear note rather than failing.
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
    # Inventory A031: run_opencode strips the -p/--print flag before
    # forwarding remaining args to `opencode run`. If the flag were still
    # being passed through, opencode would error with an "unknown flag"
    # message instead of returning the mock response.
    if grep -Eqi 'unknown (option|flag).*-p|unknown (option|flag).*--print' <<<"${t10_out}"; then
        echo "[pipeline] T10 FAIL: A031 opencode saw a stray -p/--print flag — strip logic broken" >&2
        echo "${t10_out}" >&2
        exit 1
    fi
    # Inventory A031: also check the installed agents/entrypoint.sh ships
    # the strip-leading-print-flag logic (direct source assertion that the
    # behavior is in the artifact under test).
    grep -Eq '"\$arg" == "-p" \|\| "\$arg" == "--print"' "${TEST_ROOT}/harness/agents/entrypoint.sh" \
        || { echo "[pipeline] T10 FAIL: A031 entrypoint.sh missing the -p/--print strip branch" >&2; exit 1; }
    echo "[pipeline] T10 OK"
fi

# --- T11: removed --------------------------------------------------------
#
# T11 used to drive an interactive tmux session via scripts/lib/tui_driver.sh
# to walk an agent's first-run dialogs and verify a prompt round-trip in
# the pane. Phase 18 dropped tmux wrapping from agent launch in favor of
# foreground exec, and Phase 19 deleted the tmux-based test driver
# altogether. T9 (`harness -p "say hello"`) already covers the end-to-end
# mock round-trip via bare dispatch, and T10 covers explicit opencode, so
# the tmux flow had no unique coverage. The harness list + harness stop
# coverage T11 also did is provided by tests/harness_test.sh.

# --- T12 (skipped here, see MANUAL_TEST_PROMPT.md) -------------------------
#
# File-creation-with-correct-ownership requires a real upstream that emits
# tool-call JSON. The mock returns plain text only, so the agent has no way
# to actually drive an Edit/Write tool. Phase 2's proxy_test.sh covers the
# UID-translation logic at the proxy layer; the manual test covers the
# end-to-end "agent created a file with my UID" scenario.

# --- T15: persistent home marker -------------------------------------------
#
# T9 already ran an agent against the bind-mounted home, so the skel-seed
# marker should be present. Re-running `harness -p` exercises the same code
# path a second time and must not regress.

echo "[pipeline] T15: persistent home marker + idempotent re-seed"
marker="${TEST_ROOT}/harness/state/agent/home/.harness-home-initialized"
if [[ ! -f "${marker}" ]]; then
    echo "[pipeline] T15 FAIL: skel-seed marker missing at ${marker}" >&2
    ls -la "${TEST_ROOT}/harness/state/agent/home" >&2 || true
    exit 1
fi
cd "${TEST_WORKSPACE}"
set +e
t15_out=$(timeout 60 bash -c 'HOME='"'${FAKE_HOME}'"' HARNESS_PROJECT_NAME='"'${PROJECT_NAME}'"' '"'${FAKE_HOME}/.local/bin/harness'"' -p "hi" 2>&1 < /dev/null')
t15_rc=$?
set -e
cd "${REPO_ROOT}"
# Tolerate the same opencode provider-auth skip as T9: a non-zero exit whose
# output names an auth/login problem is acceptable here — the re-seed code
# path (entrypoint, before the agent) still ran. Any other failure is real.
if (( t15_rc != 0 )) && ! echo "${t15_out}" | grep -qiE 'auth|login|provider .* not (configured|found)|no .* api key'; then
    echo "[pipeline] T15 FAIL: second harness -p exited ${t15_rc}" >&2
    echo "${t15_out}" | tail -c 600 >&2
    exit 1
fi
echo "[pipeline] T15 OK"

# --- T16: MCP enable + start + disable cycle -------------------------------
#
# Build a fake MCP fixture under a tmp registry, enable + start it, verify
# the service comes up healthy on harness-net, then disable and confirm
# cleanup.  Same shape as tests/mcp_test.sh but folded into the end-to-
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
    cid=$(harness_docker ps -q --filter "name=^${PROJECT_NAME}_pipe_mcp$" 2>/dev/null || true)
    if [[ -n "${cid}" ]]; then
        status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}" 2>/dev/null || echo "none")
        if [[ "${status}" == "healthy" ]]; then break; fi
    fi
    if (( $(date +%s) >= deadline )); then
        echo "[pipeline] T16 FAIL: pipe_mcp not healthy in 60s" >&2
        harness_docker ps --filter "name=pipe_mcp" >&2 || true
        harness_docker logs "${cid}" 2>&1 | tail -30 >&2 || true
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
harness_docker rm -f "${PROJECT_NAME}_pipe_mcp" >/dev/null 2>&1 || true

echo "[pipeline] T16 OK"

# --- T13: harness down ------------------------------------------------------

echo "[pipeline] T13: harness down"
# Tear down the manually-started mockupstream sidecar first. It joined
# harness-net via `docker run -d --network` but compose has no record of
# it, so `compose down` would fail to remove the network ("active
# endpoints"). On Linux this used to succeed silently because the
# orphan didn't gate exit code; on Windows Docker Desktop the whole
# `compose down` returns non-zero when the network removal errors.
harness_docker rm -f "${MOCK_NAME}" >/dev/null 2>&1 || true
harness_call down >/dev/null

remaining=$(harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q 2>/dev/null || true)
if [[ -n "${remaining}" ]]; then
    echo "[pipeline] T13 FAIL: containers still present after down" >&2
    harness_docker compose --project-name "${PROJECT_NAME}" \
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

# Inventory F031: harness update performs `git pull --ff-only` on the
# install root. The clone is fresh from a local repo with no diverged
# commits, so git's output must reflect a ff-only / no-op outcome — never a
# merge commit, never a fast-forward conflict, never "rejected".
if grep -Eqi 'rejected|non-fast-forward|merge conflict|cannot fast-forward' <<<"${update_out}"; then
    echo "[pipeline] T14 FAIL: F031 update did not perform an ff-only / no-op pull" >&2
    echo "${update_out}" >&2
    exit 1
fi
if ! grep -Eqi 'already up.to.date|fast.forward|up to date|up-to-date' <<<"${update_out}"; then
    echo "[pipeline] T14 FAIL: F031 update output lacks ff/no-op marker (expected 'Already up to date' or 'fast-forward')" >&2
    echo "${update_out}" >&2
    exit 1
fi
# Inventory F031: also verify the installed harness script wires `update`
# to `git pull --ff-only` (source-level direct assertion).
grep -Eq 'git[[:space:]]+pull[[:space:]]+--ff-only' "${TEST_ROOT}/harness/harness" \
    || { echo "[pipeline] T14 FAIL: F031 'git pull --ff-only' literal missing from installed harness script" >&2; exit 1; }

echo "[pipeline] T14 OK"

echo "============================================================"
echo " FULL PIPELINE TEST PASSED"
echo "============================================================"
exit 0
