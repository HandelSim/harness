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

# --- T3: a running host proxy comes back with the mode it had ---------------
# host_proxy_start records backend and prompt mode in proxy.mode; the restart
# an upgrade runs reads them back for the new proxy, and leaves this shell's
# own overrides as they were.
mkdir -p "${state_root}/host"
printf 'chatgpt\nsingle\n' >"$(host_proxy_modefile)"
host_proxy_running()    { return 0; }
host_proxy_stop()       { echo "STOPPED"; }
host_proxy_start()      { echo "STARTED backend=${backend_override:-} mode=${prompt_mode_override:-}"; }
host_proxy_wait_ready() { return 0; }
backend_override="" prompt_mode_override=""
out3=$(host_proxy_refresh 2>&1)
grep -q "STOPPED" <<<"${out3}" && grep -q "STARTED backend=chatgpt mode=single" <<<"${out3}" \
    || { echo "${out3}" | sed 's/^/    /'; fail "T3: the proxy did not restart with its recorded mode"; }
host_proxy_refresh >/dev/null 2>&1
[[ -z "${backend_override}" && -z "${prompt_mode_override}" ]] \
    || fail "T3: host_proxy_refresh changed the caller's overrides"
ok "T3: host_proxy_refresh restarts the proxy with its recorded backend and mode"

host_proxy_wait_ready() { return 1; }
out3=$(host_proxy_refresh 2>&1) || fail "T3: a proxy that does not come back fails the upgrade"
grep -q "did not come back up" <<<"${out3}" \
    || { echo "${out3}" | sed 's/^/    /'; fail "T3: no message when the proxy does not come back"; }
ok "T3: a proxy that does not come back is reported, and the upgrade goes on"

host_proxy_running() { return 1; }
out3=$(host_proxy_refresh 2>&1)
[[ -z "${out3}" ]] || fail "T3: no proxy running, but refresh did: ${out3}"
ok "T3: with no proxy running, nothing is started"

# --- T4: a container install's upgrade takes the stack down, not up -----------
# The next container launch starts it on the new code; agents still working
# keep it up (marked stale); only images this install has are rebuilt.
harness_runtime_installed() { return 0; }
require_docker()        { :; }
host_proxy_refresh()    { :; }
harness_serve_refresh() { :; }
harness_view_refresh()  { :; }
cmd_down()  { echo "MARKER_CMD_DOWN"; }
compose()   { echo "MARKER_COMPOSE $*"; }
n_agents=0 images=1
running_agent_count() { echo "${n_agents}"; }
harness_docker() { [[ "$1 $2" == "image inspect" ]] && (( images )); }
upgrade4() { HARNESS_UPGRADE_SKIP_PULL=1 cmd_upgrade --no-prompt 2>&1; }

out4=$(upgrade4) || { echo "${out4}" | sed 's/^/    /'; fail "T4: the container upgrade failed"; }
grep -q "MARKER_CMD_DOWN" <<<"${out4}" && grep -q "MARKER_COMPOSE --profile agent build" <<<"${out4}" \
    || { echo "${out4}" | sed 's/^/    /'; fail "T4: the stack was not taken down and the images rebuilt"; }
! grep -q "MARKER_CMD_START" <<<"${out4}" || fail "T4: the upgrade started the stack again"
grep -q "the next container launch starts it on the new code" <<<"${out4}" || fail "T4: no word on the next launch"
[[ ! -f "$(stack_stale_file)" ]] || fail "T4: stack marked stale with no agent working"
ok "T4: the stack is taken down and the images rebuilt; nothing is started"

n_agents=2
out4=$(upgrade4) || fail "T4: the upgrade with agents working failed"
! grep -q "MARKER_CMD_DOWN" <<<"${out4}" || { echo "${out4}" | sed "s/^/    /"; fail "T4: the upgrade took the stack down under working agents"; }
grep -q "2 agent container(s) still working" <<<"${out4}" && [[ -f "$(stack_stale_file)" ]] \
    || { echo "${out4}" | sed 's/^/    /'; fail "T4: working agents not left the stack, marked stale"; }
ok "T4: agent containers still working keep the stack up, marked stale"

n_agents=0 images=0
rm -f "$(stack_stale_file)"
out4=$(upgrade4) || fail "T4: the upgrade with no images failed"
! grep -q "MARKER_COMPOSE" <<<"${out4}" && grep -q "no container images built yet" <<<"${out4}" \
    || { echo "${out4}" | sed 's/^/    /'; fail "T4: built images an install never had"; }
ok "T4: an install with no images built (host use only) builds none"

# --- T5: the first launch after the working agents end starts it again -------
services_up() { return 0; }
_chatgpt_in_play() { return 1; }
host_mcp_start_enabled() { :; }
cmd_start() { echo "MARKER_CMD_START"; }
: >"$(stack_stale_file)"
n_agents=1
[[ -z "$(ensure_services_up)" ]] || fail "T5: a stale stack was started again under a working agent"
n_agents=0
grep -q "MARKER_CMD_START" <<<"$(ensure_services_up)" || fail "T5: a stale stack with no agent left was not started again"
rm -f "$(stack_stale_file)"
[[ -z "$(ensure_services_up)" ]] || fail "T5: a current stack was started again"
ok "T5: a stale stack starts again at the first launch with no agent working"

# --- T6: restart with no stack running has nothing to restart ----------------
services_up() { return 1; }
out6=$(cmd_restart 2>&1) || fail "T6: restart with no stack running failed"
! grep -qE "MARKER_CMD_DOWN|MARKER_CMD_START" <<<"${out6}" && grep -q "nothing to restart" <<<"${out6}" \
    || { echo "${out6}" | sed 's/^/    /'; fail "T6: restart started a stack that was not running"; }
ok "T6: restart leaves a stack that is not running alone (host mode uses none)"
grep -q "MARKER_CMD_START" <<<"$(cmd_restart --prompt-mode passthrough 2>&1)" \
    || fail "T6: restart --prompt-mode did not start the stack in that mode"
prompt_mode_override=""
ok "T6: restart --prompt-mode still starts a stack that is not running"

echo
echo "HOST UPGRADE TEST PASSED (${pass} checks)"
