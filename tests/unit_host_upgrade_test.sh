#!/usr/bin/env bash
#
# unit_host_upgrade_test.sh — docker-free coverage for the host-only upgrade
# transition added in commit 9f4cb4e ("Make host-only installs upgrade and
# transition correctly").
#
# The behavior under test, in cmd_upgrade:
#   - When NO container runtime is present (host-only install), upgrade must
#     NOT abort on require_docker the way the container path does, and must
#     NOT try to rebuild images or restart a container stack. It pulls + merges
#     config, then prints "[harness] host-only upgrade complete." and returns 0.
#   - When a container runtime IS present, upgrade still takes the require_docker
#     path (the gate actually branches on harness_runtime_installed).
#
# Runs without docker: `harness` is sourced with HARNESS_SOURCE_ONLY=1 so
# main() never runs, the git pull is suppressed with HARNESS_UPGRADE_SKIP_PULL,
# and the container-only helpers are replaced with loud sentinels that fail the
# test if the host path ever reaches them.
#
# Run:  bash tests/unit_host_upgrade_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

pass=0
fail() { echo "[host-upgrade] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-upgrade] OK: $*"; pass=$((pass + 1)); }

echo "============================================================"
echo " host-only upgrade transition unit test (docker-free)"
echo "============================================================"

WORK="$(mktemp -d -t harness-host-upg.XXXXXX)"
cleanup() { [[ -n "${WORK:-}" && -d "${WORK}" ]] && rm -rf "${WORK}"; }
trap cleanup EXIT INT TERM

# Point the install root at a throwaway dir so nothing touches a real install.
export HARNESS_INSTALL_ROOT="${WORK}"
: > "${WORK}/.env"

export HARNESS_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "${REPO_ROOT}/harness"

# When `harness` is sourced (rather than executed) its self-resolution sees the
# test script as "$0", so clone_dir lands on tests/ instead of the repo root.
# Pin it to the real repo so cmd_upgrade finds the manifest + actions library
# (the same "tests keep clone_dir resolved to the real repo" intent noted at
# the top of `harness`).
clone_dir="${REPO_ROOT}"

# Container-only helpers the host path must never reach. Each prints a unique
# marker and exits with a unique code, so a host run that touches one fails
# loudly instead of silently passing.
require_docker()     { echo "MARKER_REQUIRE_DOCKER"; exit 91; }
cmd_down()           { echo "MARKER_CMD_DOWN"; exit 92; }
cmd_start()          { echo "MARKER_CMD_START"; exit 93; }
host_proxy_running() { return 1; }   # report no live host proxy

# --- T1: no runtime present -> host-only upgrade, no docker, returns 0 -------
# Hide jq from cmd_upgrade so it takes the documented host + no-jq sub-path:
# the merges run through a docker jq sidecar that a host-only box does not have,
# so they are skipped, the code is still updated, and the host-only completion
# block runs. `command -v` finds shell builtins/functions regardless of PATH,
# so emptying PATH makes the real `jq` binary disappear without affecting the
# pure-bash control flow this path uses (git pull is suppressed).
harness_runtime_installed() { return 1; }
set +e
out=$(HARNESS_UPGRADE_SKIP_PULL=1 PATH=/nonexistent cmd_upgrade --no-prompt 2>&1)
rc=$?
set -e

[[ "${rc}" -eq 0 ]] || { echo "${out}" | sed 's/^/    /'; fail "T1: host-only upgrade returned ${rc}, expected 0"; }
ok "T1: host-only upgrade returns 0"

grep -q "no container runtime found" <<<"${out}" \
    || { echo "${out}" | sed 's/^/    /'; fail "T1: did not announce the host-only upgrade path"; }
ok "T1: detects the no-runtime install and announces the host-only path"

grep -q "host-only upgrade complete" <<<"${out}" \
    || { echo "${out}" | sed 's/^/    /'; fail "T1: did not reach the host-only completion block"; }
ok "T1: reaches '[harness] host-only upgrade complete.'"

if grep -qE "MARKER_REQUIRE_DOCKER|MARKER_CMD_DOWN|MARKER_CMD_START" <<<"${out}"; then
    echo "${out}" | sed 's/^/    /'
    fail "T1: host path reached a container-only helper (require_docker / cmd_down / cmd_start)"
fi
ok "T1: never calls require_docker / cmd_down / cmd_start on the host path"

# --- T2: runtime present -> still goes through require_docker ----------------
# Pins the branch: with a runtime installed the gate must take the container
# path (require_docker), not the host-only path.
harness_runtime_installed() { return 0; }
set +e
out2=$(HARNESS_UPGRADE_SKIP_PULL=1 cmd_upgrade --no-prompt 2>&1)
rc2=$?
set -e

grep -q "MARKER_REQUIRE_DOCKER" <<<"${out2}" \
    || { echo "${out2}" | sed 's/^/    /'; fail "T2: container path did not call require_docker"; }
[[ "${rc2}" -eq 91 ]] \
    || { echo "${out2}" | sed 's/^/    /'; fail "T2: expected rc 91 from the require_docker sentinel, got ${rc2}"; }
ok "T2: a runtime-present install takes the require_docker path"

if grep -q "host-only upgrade complete" <<<"${out2}"; then
    echo "${out2}" | sed 's/^/    /'
    fail "T2: runtime-present install wrongly took the host-only branch"
fi
ok "T2: a runtime-present install does NOT take the host-only branch"

echo
echo "HOST UPGRADE TEST PASSED (${pass} checks)"
