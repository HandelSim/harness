#!/usr/bin/env bash
#
# tests/unit_platform_timer_test.sh — exercise the runtime-startup poll
# loop in scripts/lib/platform.sh:harness_start_docker_desktop.
#
# Regression for issue #50: the loop used to count `sleep 2` ticks as
# elapsed time, which under-reported wall time and also let the loop
# overrun the caller's timeout when `harness_docker_running` (a `docker
# info` call) blocked for seconds at a time during daemon boot. This
# test stubs the runtime probe to be slow + always-failing and asserts
# the loop returns within timeout + a small slack.
#
# Pure unit test — no docker, no network. Sourced from a fresh shell.
#
# Prints "PLATFORM TIMER TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo " platform timer test"
echo "============================================================"

fail() { echo "[platform-timer-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[platform-timer-test] OK: $*"; }

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# --- T1: slow-probe path respects the wall-clock timeout ---------------------
#
# Drive the macOS+docker branch (which is the only branch that both enters
# the poll loop and can succeed without a real daemon). Stubs:
#   harness_detect_os         -> "macos"
#   harness_container_runtime -> "docker"
#   open                      -> no-op (would have launched Docker.app)
#   harness_docker_running    -> sleep 1; return 1 (slow daemon, never up)

harness_detect_os()         { printf '%s' macos; }
harness_container_runtime() { printf '%s' docker; }
open()                      { return 0; }
# 2-second probe is what makes this a discriminating test: per iteration
# the wall cost is `2s probe + 2s sleep = 4s`, but the buggy counter
# would only have charged `2s` per iter (the sleep). At timeout=6 the
# buggy loop runs 3 iterations (wall ≈ 12s) while the fixed loop runs 2
# (wall ≈ 8s), so a slack ceiling at +4s catches the regression.
harness_docker_running()    { sleep 2; return 1; }
export -f harness_detect_os harness_container_runtime open harness_docker_running

timeout=6
start_ts=$(date +%s)
set +e
output="$(harness_start_docker_desktop "$timeout" 2>&1)"
rc=$?
set -e
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))

# Buggy loop wall ≈ 12s; fixed loop wall ≈ 8s. Use timeout+4 as the cap.
slack=4
max_allowed=$((timeout + slack))
if (( elapsed > max_allowed )); then
    echo "[platform-timer-test] captured output:" >&2
    echo "$output" >&2
    fail "T1: timeout=${timeout}s but loop took ${elapsed}s wall (max allowed ${max_allowed}s)"
fi
ok "T1: slow-probe loop exited in ${elapsed}s wall (<= ${max_allowed}s) for timeout=${timeout}s"

# The function must return non-zero when the probe never succeeds.
if (( rc == 0 )); then
    fail "T1: harness_start_docker_desktop returned 0 even though the daemon never became available"
fi
ok "T1: returns non-zero on timeout (rc=${rc})"

# The bucketed log line ("...still waiting (Ns elapsed, ...)") fires once
# per ~10s of elapsed; with timeout=6 it won't trigger here, so don't
# assert it. T2 below covers the counter-tracking-wall-clock property
# with a longer-timeout case.

# --- T2: the printed "Ns elapsed" counter tracks wall clock ------------------
#
# The visible bug in the user report was "if two minutes have passed, it
# might still say 40s elapsed". Verify directly: with a slow probe and
# a timeout long enough to trigger at least one bucketed log line, the
# printed elapsed value should match wall-clock duration (the bucket
# threshold) — not the count of `sleep 2` ticks.

timeout=11
start_ts=$(date +%s)
set +e
output="$(harness_start_docker_desktop "$timeout" 2>&1)"
rc=$?
set -e
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))

# Loop must still respect the timeout under T2's parameters.
if (( elapsed > timeout + 5 )); then
    echo "[platform-timer-test] captured output:" >&2
    echo "$output" >&2
    fail "T2: timeout=${timeout}s but loop took ${elapsed}s wall"
fi
ok "T2: loop exited in ${elapsed}s wall for timeout=${timeout}s"

if [[ "$output" != *"...still waiting"* ]]; then
    echo "[platform-timer-test] captured output:" >&2
    echo "$output" >&2
    fail "T2: expected at least one '...still waiting' log line with timeout=${timeout}s"
fi
counted=$(printf '%s\n' "$output" | grep -oE '\([0-9]+s elapsed' | tail -n1 | grep -oE '[0-9]+')
if [[ -z "$counted" ]]; then
    fail "T2: '...still waiting' line present but elapsed value could not be parsed"
fi
# The print happens just before the loop's final break (or partway through);
# the printed value should be close to the final wall-clock elapsed. Buggy
# counter would print ~10s while wall was ~20s. Cap the gap at 3s.
diff=$((elapsed - counted))
if (( diff < 0 )); then diff=$((-diff)); fi
if (( diff > 3 )); then
    echo "[platform-timer-test] captured output:" >&2
    echo "$output" >&2
    fail "T2: printed 'elapsed=${counted}s' but real wall was ${elapsed}s (diff ${diff}s > 3s)"
fi
ok "T2: printed elapsed (${counted}s) tracks wall clock (${elapsed}s) within ${diff}s"

# --- T3: success path returns 0 promptly once the daemon comes up -----------

probe_count=0
harness_docker_running() {
    probe_count=$((probe_count + 1))
    # First probe: not up. Second probe: up. Models the typical "Docker
    # Desktop just finished booting" handoff.
    (( probe_count >= 2 ))
}
export -f harness_docker_running

start_ts=$(date +%s)
set +e
harness_start_docker_desktop 30 >/dev/null 2>&1
rc=$?
set -e
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))

if (( rc != 0 )); then
    fail "T3: expected rc=0 when daemon comes up on second probe, got rc=${rc}"
fi
if (( elapsed > 5 )); then
    fail "T3: success path took ${elapsed}s — should return on the second probe (<5s)"
fi
ok "T3: success path returns 0 in ${elapsed}s once the daemon is up"

echo
echo "PLATFORM TIMER TEST PASSED"
