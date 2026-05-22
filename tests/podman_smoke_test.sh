#!/usr/bin/env bash
#
# tests/podman_smoke_test.sh — bring the harness stack up under podman,
# verify the proxy /health endpoint responds, run a one-shot agent, tear down.
#
# Linux + rootless podman (>= 4.0 with built-in `podman compose`). This is
# the supported configuration; rootful podman should also work but is not
# the primary target.
#
# Run manually:
#   bash tests/podman_smoke_test.sh
#
# Honors:
#   HARNESS_CONTAINER_RUNTIME (always pinned to podman by this script)
#
# This test does NOT belong in the default test suite — CI doesn't have
# podman installed and the test is slow on a cold system (image builds).
# Run it on Linux hosts that have podman set up to validate podman support.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Force the runtime to podman regardless of what's on PATH first.
export HARNESS_CONTAINER_RUNTIME=podman

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

PROJECT_NAME="harness-podman-smoke"

echo "============================================================"
echo " harness podman smoke test"
echo "============================================================"

# --- preflight --------------------------------------------------------------

if ! command -v podman >/dev/null 2>&1; then
    echo "[podman-smoke] ERROR: podman not on PATH" >&2
    echo "[podman-smoke] Install via your distro: 'sudo apt install podman' / 'sudo dnf install podman'" >&2
    exit 1
fi

PODMAN_VERSION=$(podman --version 2>/dev/null | awk '{print $3}')
echo "[podman-smoke] podman version: ${PODMAN_VERSION:-unknown}"

if ! harness_docker info >/dev/null 2>&1; then
    echo "[podman-smoke] ERROR: 'podman info' failed; podman not usable" >&2
    echo "[podman-smoke] Common causes (rootless):" >&2
    echo "[podman-smoke]   - subuid/subgid not set: check /etc/subuid /etc/subgid" >&2
    echo "[podman-smoke]   - first run not initialized: try 'podman system migrate'" >&2
    exit 1
fi

if ! podman compose version >/dev/null 2>&1; then
    echo "[podman-smoke] ERROR: 'podman compose' not available" >&2
    echo "[podman-smoke] podman 4.0+ ships compose built-in; older versions need" >&2
    echo "[podman-smoke] either 'podman-compose' or 'docker-compose' on PATH." >&2
    exit 1
fi
echo "[podman-smoke] podman compose: OK"

# --- staging area -----------------------------------------------------------
#
# Same shape as harness_test.sh: a temp install root that mirrors the real
# layout. Symlink the repo as ./harness so the harness CLI's realpath logic
# finds the script next to docker-compose.yml.

TEST_ROOT="$(mktemp -d -t harness-podman.XXXXXX)"

cleanup() {
    echo "[podman-smoke] cleanup"
    if [[ -n "${TEST_ROOT:-}" && -d "${TEST_ROOT}" ]]; then
        if [[ -x "${TEST_ROOT}/harness/harness" ]]; then
            HARNESS_PROJECT_NAME="${PROJECT_NAME}" \
                HARNESS_CONTAINER_RUNTIME=podman \
                "${TEST_ROOT}/harness/harness" down >/dev/null 2>&1 || true
        fi
        # Belt-and-braces: tear down via the runtime directly.
        harness_docker compose --project-name "${PROJECT_NAME}" \
            -f "${REPO_ROOT}/docker-compose.yml" \
            down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf "${TEST_ROOT}"
    fi
}
trap cleanup EXIT INT TERM

ln -s "${REPO_ROOT}" "${TEST_ROOT}/harness"

# Use a placeholder upstream — we don't make real upstream calls in this test.
# The proxy's /health endpoint doesn't dial upstream; ollama's healthcheck
# only hits its own /api/tags. So the stack reaches a stable healthy state
# without a working API key.
cat >"${TEST_ROOT}/.env" <<'EOF'
PROXY_API_URL=http://placeholder.invalid/v1/chat/completions
PROXY_API_KEY=test-key-1234
DEFAULT_MODEL_NAME=harness
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_CONTEXT_LENGTH=200000
PUBLISH_OLLAMA_PORT=
EOF

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

HARNESS_BIN="${TEST_ROOT}/harness/harness"
export HARNESS_PROJECT_NAME="${PROJECT_NAME}"
export HARNESS_INSTALL_ROOT="${TEST_ROOT}"
export HARNESS_ALLOWLIST_PATH="${TEST_ROOT}/.harness-allowlist"
export HARNESS_SKIP_AUTH_PROBE=1   # placeholder.invalid won't probe
export HARNESS_SKIP_UPDATE_CHECK=1

# --- Test 1: harness preflight under podman ---------------------------------

echo "[podman-smoke] T1: harness preflight"
if ! "${HARNESS_BIN}" preflight >/tmp/podman-smoke-preflight.txt 2>&1; then
    echo "[podman-smoke] T1 FAIL: preflight returned non-zero" >&2
    cat /tmp/podman-smoke-preflight.txt >&2
    exit 1
fi
if ! grep -Eq 'podman\s+runtime' /tmp/podman-smoke-preflight.txt; then
    echo "[podman-smoke] T1 FAIL: preflight didn't identify podman as the runtime" >&2
    cat /tmp/podman-smoke-preflight.txt >&2
    exit 1
fi
echo "[podman-smoke] T1 OK"

# --- Test 2: harness start brings up proxy + ollama -------------------------

echo "[podman-smoke] T2: harness start (build + up under podman, takes a few minutes)"
"${HARNESS_BIN}" start >/dev/null
deadline=$(( $(date +%s) + 180 ))
proxy_ok=0
ollama_ok=0
while (( $(date +%s) < deadline )); do
    proxy_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" ps -q proxy 2>/dev/null || true)
    ollama_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" ps -q ollama 2>/dev/null || true)
    if [[ -n "${proxy_id}" && -n "${ollama_id}" ]]; then
        proxy_status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proxy_id}" 2>/dev/null || echo "none")
        ollama_status=$(harness_docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${ollama_id}" 2>/dev/null || echo "none")
        [[ "${proxy_status}" == "healthy" ]] && proxy_ok=1
        [[ "${ollama_status}" == "healthy" ]] && ollama_ok=1
        if (( proxy_ok && ollama_ok )); then break; fi
    fi
    sleep 3
done
if (( ! proxy_ok || ! ollama_ok )); then
    echo "[podman-smoke] T2 FAIL: services did not reach healthy state" >&2
    harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
    [[ -n "${proxy_id}" ]] && harness_docker logs "${proxy_id}" 2>&1 | tail -50 >&2 || true
    exit 1
fi
echo "[podman-smoke] T2 OK"

# --- Test 3: firewall init ran under rootless podman ------------------------
#
# init-firewall.sh requires NET_ADMIN/NET_RAW. Under rootless podman these
# are scoped to the container's user namespace (which is the only thing
# being firewalled), so iptables/ipset succeed inside the container netns.
# Verify by checking the proxy container's iptables policy.

echo "[podman-smoke] T3: proxy iptables policy is locked down"
proxy_id=$(harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" ps -q proxy 2>/dev/null || true)
if [[ -z "${proxy_id}" ]]; then
    echo "[podman-smoke] T3 FAIL: proxy container not found" >&2
    exit 1
fi
proxy_pol=$(harness_docker exec "${proxy_id}" iptables -S OUTPUT 2>/dev/null | head -1 || true)
if ! grep -q '\-P OUTPUT DROP' <<<"${proxy_pol}"; then
    echo "[podman-smoke] T3 FAIL: OUTPUT chain default policy is not DROP" >&2
    echo "[podman-smoke]   got: ${proxy_pol}" >&2
    exit 1
fi
echo "[podman-smoke] T3 OK"

# --- Test 4: harness list (no agents) ---------------------------------------

echo "[podman-smoke] T4: harness list with no running agents"
list_out=$("${HARNESS_BIN}" list 2>&1) || {
    echo "[podman-smoke] T4 FAIL: harness list returned non-zero" >&2
    echo "${list_out}" >&2
    exit 1
}
if [[ "${list_out}" != "no harness agents running" ]]; then
    echo "[podman-smoke] T4 FAIL: unexpected output: ${list_out}" >&2
    exit 1
fi
echo "[podman-smoke] T4 OK"

# --- Test 5: harness doctor reports podman ----------------------------------

echo "[podman-smoke] T5: harness doctor identifies podman"
doctor_out=$("${HARNESS_BIN}" doctor 2>&1) || true
if ! grep -Eq 'podman\s+runtime\s+reachable' <<<"${doctor_out}"; then
    echo "[podman-smoke] T5 FAIL: doctor didn't report podman runtime" >&2
    echo "${doctor_out}" >&2
    exit 1
fi
echo "[podman-smoke] T5 OK"

# --- Test 6: harness down tears down cleanly --------------------------------

echo "[podman-smoke] T6: harness down"
"${HARNESS_BIN}" down >/dev/null 2>&1
remaining=$(harness_docker compose --project-name "${PROJECT_NAME}" \
    -f "${REPO_ROOT}/docker-compose.yml" \
    ps -q 2>/dev/null | wc -l | tr -d ' ')
if [[ "${remaining}" != "0" ]]; then
    echo "[podman-smoke] T6 FAIL: ${remaining} containers still running after down" >&2
    harness_docker compose --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" ps >&2 || true
    exit 1
fi
echo "[podman-smoke] T6 OK"

echo "============================================================"
echo " PODMAN SMOKE TEST PASSED"
echo "============================================================"
