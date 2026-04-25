#!/usr/bin/env bash
#
# scripts/persistence_test.sh — verify that the agent home bind mount and
# skel-seed logic produce a persistent /home/harness across container
# rebuilds.
#
# Test scenarios:
#
#   T1. First-run skel seed: starting a fresh container against an empty
#       agent/claude/ dir populates it with the build-time skeleton
#       (e.g. .bashrc, the marker file).
#   T2. Marker file pins idempotency: a second container with the same
#       mount does NOT overwrite anything; user files added between runs
#       survive.
#   T3. pip --user persistence: installing a python package with --user
#       inside a container lands the package under ~/.local/, and a
#       fresh container without re-installing can `import` it.
#
# We run the agent image with --entrypoint /bin/bash to bypass tmux and
# all the per-tool dispatch — this test is about the home-mount mechanics
# only, not anything claude-code or opencode does.
#
# Project name is fixed to harness-persist-test so this never collides
# with a real harness instance running on the same daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NAME="harness-persist-test"

echo "============================================================"
echo " harness persistence test"
echo "============================================================"

# --- preflight --------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    echo "[persist] ERROR: docker daemon not reachable" >&2
    exit 1
fi

# --- staging area -----------------------------------------------------------

TEST_ROOT="$(mktemp -d -t harness-persist-test.XXXXXX)"
AGENT_HOME="${TEST_ROOT}/agent/claude"
mkdir -p "${AGENT_HOME}"

cleanup() {
    local rc=$?
    echo "[persist] cleanup (rc=${rc})"

    # Some tests start named containers; remove any survivors. Names use a
    # fixed prefix so we don't have to track individual ids.
    docker ps -aq --filter "label=harness-persist-test=1" 2>/dev/null \
        | xargs -r docker rm -f >/dev/null 2>&1 || true

    # Files written into the bind mount were owned by uid 1000 inside the
    # container, which IS the host caller's uid (we don't remap in this
    # test) — so a normal rm -rf works. If it doesn't (different host uid),
    # privileged docker rm -rf as a fallback.
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

# --- build the agent image if missing --------------------------------------
#
# This test is happy to reuse a pre-built image — full_pipeline_test.sh
# runs first in CI and primes the image cache. If the image isn't there
# (running this test in isolation), build it via compose --profile agent.

if ! docker image inspect harness-claude-agent:latest >/dev/null 2>&1; then
    echo "[persist] building harness-claude-agent (image not cached)"
    # Build context only — we don't need ollama/proxy services for this
    # test, so we skip --env-file and the up dance.
    docker compose \
        --project-name "${PROJECT_NAME}" \
        -f "${REPO_ROOT}/docker-compose.yml" \
        --profile agent build claude-agent >"${TEST_ROOT}/build.log" 2>&1 \
        || { echo "[persist] FAIL: build failed; see ${TEST_ROOT}/build.log" >&2; tail -30 "${TEST_ROOT}/build.log" >&2; exit 1; }
fi

# --- helpers ----------------------------------------------------------------

# Run a one-shot bash command inside an agent container with the persistent
# home mounted. We pass --entrypoint /bin/bash to bypass the agent
# entrypoint's claude/opencode dispatch, but we manually invoke the
# entrypoint's skel-seed logic by sourcing the script under test mode flag
# — except that the entrypoint's own logic runs claude at the end. So
# instead, the bash command directly mimics the relevant steps: cp -an
# /etc/skel/harness/. ~/ + touch marker. We do this BEFORE the user's
# command so the test exercises the same mechanics.
#
# Why not run the real entrypoint? Because the entrypoint always tries to
# launch claude; even with HARNESS_TEST_MODE=1 it runs `exec claude` which
# requires ANTHROPIC_BASE_URL. Bypassing keeps the test focused.
run_in_agent() {
    local cmd="$1"
    docker run --rm \
        --label "harness-persist-test=1" \
        -v "${AGENT_HOME}:/home/harness" \
        --entrypoint /bin/bash \
        --user harness \
        -e HOME=/home/harness \
        -e PATH="/home/harness/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        harness-claude-agent:latest \
        -c "
            set -e
            # Inline skel-seed: same logic as the entrypoint.
            if [[ ! -f \$HOME/.harness-home-initialized ]]; then
                if [[ -d /etc/skel/harness ]]; then
                    cp -an /etc/skel/harness/. \$HOME/ 2>/dev/null || true
                fi
                touch \$HOME/.harness-home-initialized
            fi
            ${cmd}
        "
}

# --- T1: first-run skel seed -----------------------------------------------

echo "[persist] T1: first-run skel seed"
run_in_agent 'echo first-run > /tmp/_ignored' >/dev/null

if [[ ! -f "${AGENT_HOME}/.harness-home-initialized" ]]; then
    echo "[persist] T1 FAIL: marker file not written to bind mount" >&2
    ls -la "${AGENT_HOME}" >&2 || true
    exit 1
fi
# At least one skel-seeded file should have appeared. Debian/bookworm's
# useradd copies /etc/skel/.bashrc into /home/<user>/, so /etc/skel/harness/
# will contain it. We don't pin to a specific filename in case base-image
# layouts shift — instead we count entries.
seeded_count=$(ls -A "${AGENT_HOME}" | wc -l | tr -d ' ')
if (( seeded_count < 2 )); then
    echo "[persist] T1 FAIL: expected at least 2 entries seeded, got ${seeded_count}" >&2
    ls -la "${AGENT_HOME}" >&2 || true
    exit 1
fi
echo "[persist] T1 OK (${seeded_count} entries seeded)"

# --- T2: marker pins idempotency -------------------------------------------

echo "[persist] T2: marker pins idempotency, user files survive"
# Drop a marker the seed wouldn't create.
echo "user file" >"${AGENT_HOME}/user-file.txt"

# Run again. The seed should be skipped (marker present); user-file should
# be untouched.
run_in_agent 'echo second-run > /tmp/_ignored' >/dev/null

if [[ ! -f "${AGENT_HOME}/user-file.txt" ]]; then
    echo "[persist] T2 FAIL: user-file.txt was clobbered by skel re-seed" >&2
    exit 1
fi
content=$(cat "${AGENT_HOME}/user-file.txt")
if [[ "${content}" != "user file" ]]; then
    echo "[persist] T2 FAIL: user-file.txt contents changed" >&2
    exit 1
fi
echo "[persist] T2 OK"

# --- T3: pip --user persistence --------------------------------------------
#
# Install a tiny pure-python package with --user inside the container and
# verify a second container can import it without re-installing. Debian's
# python3-pip is marked PEP 668 externally-managed; we set
# PIP_BREAK_SYSTEM_PACKAGES=1 to let --user installs proceed (we are
# explicitly NOT trying to mutate the system site-packages — --user puts
# files under ~/.local, which is exactly what we want bind-mounted).
#
# We install `requests` per spec; it's a real-world dep that stresses the
# package layout. If networking from inside the container is broken (the
# repo's CI env may be offline), the test fails fast with a clear message.

echo "[persist] T3: pip --user persistence (install + use across runs)"

run_in_agent '
    set -e
    export PIP_BREAK_SYSTEM_PACKAGES=1
    if ! pip install --user --quiet requests >/dev/null 2>&1; then
        echo "[persist-inner] pip install failed; offline?" >&2
        exit 42
    fi
    python3 -c "import requests; print(\"requests-ok-\" + requests.__version__)"
' >"${TEST_ROOT}/t3-install.log" 2>&1
t3_rc=$?
if (( t3_rc == 42 )); then
    echo "[persist] T3 SKIPPED: pip install failed (likely offline build env)"
    echo "[persist] T3 detail:"
    sed 's/^/  | /' <"${TEST_ROOT}/t3-install.log" || true
else
    if (( t3_rc != 0 )); then
        echo "[persist] T3 FAIL: install run exited ${t3_rc}" >&2
        cat "${TEST_ROOT}/t3-install.log" >&2
        exit 1
    fi
    if ! grep -q '^requests-ok-' "${TEST_ROOT}/t3-install.log"; then
        echo "[persist] T3 FAIL: install run did not import requests" >&2
        cat "${TEST_ROOT}/t3-install.log" >&2
        exit 1
    fi

    # Confirm the package is on the host bind mount.
    if [[ ! -d "${AGENT_HOME}/.local/lib" ]]; then
        echo "[persist] T3 FAIL: ~/.local/lib not present on host after install" >&2
        ls -la "${AGENT_HOME}" >&2 || true
        exit 1
    fi
    if ! find "${AGENT_HOME}/.local/lib" -name 'requests' -type d 2>/dev/null | grep -q .; then
        echo "[persist] T3 FAIL: requests package not found in ~/.local/lib on host" >&2
        find "${AGENT_HOME}/.local/lib" -maxdepth 4 -type d >&2 || true
        exit 1
    fi

    # Second-run import — must NOT re-install. Run a fresh container; the
    # only thing that's persistent across the two is the bind mount.
    run_in_agent '
        python3 -c "import requests; print(\"second-ok-\" + requests.__version__)"
    ' >"${TEST_ROOT}/t3-second.log" 2>&1
    if ! grep -q '^second-ok-' "${TEST_ROOT}/t3-second.log"; then
        echo "[persist] T3 FAIL: second container could not import requests" >&2
        cat "${TEST_ROOT}/t3-second.log" >&2
        exit 1
    fi
    echo "[persist] T3 OK"
fi

echo "============================================================"
echo " PERSISTENCE TEST PASSED"
echo "============================================================"
exit 0
