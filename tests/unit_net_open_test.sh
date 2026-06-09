#!/usr/bin/env bash
#
# unit_net_open_test.sh — docker-free regression coverage for cmd_net_open's
# service-membership validation.
#
# Guards a specific, timing-dependent bug: cmd_net_open used to validate the
# requested service with `net_known_services | grep -qx -- "$svc"`. Under the
# script's `set -euo pipefail`, grep -q exits on the first match and SIGPIPEs
# the still-writing producer. net_known_services shells out to a slow
# `docker compose config`, so for any service that is NOT the last line it
# emits (e.g. the first-listed 'proxy'), the producer died 141, pipefail
# reported the pipeline as failed, and a valid service was rejected as
# "unknown service" — `harness net open proxy` was broken on every real host.
#
# The fix captures the list once and matches with a here-string (no pipe).
# This test reproduces the failure mode with a deliberately SLOW, multi-line
# net_known_services stub (proxy first), so if the pipe pattern is ever
# reintroduced the SIGPIPE fires and this test fails.
#
# Runs without docker: `harness` is sourced with HARNESS_SOURCE_ONLY=1 so
# main() never runs, and the network helpers are replaced with stubs.
#
# Run:  bash tests/unit_net_open_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

pass=0
fail() { echo "[net-open] FAIL: $*" >&2; exit 1; }
ok()   { echo "[net-open] OK: $*"; pass=$((pass + 1)); }

echo "============================================================"
echo " net open service-validation unit test (docker-free)"
echo "============================================================"

export HARNESS_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "${REPO_ROOT}/harness"

# --- stub the network helpers cmd_net_open leans on -------------------------
# A slow, multi-line producer with the target service on the FIRST line. The
# sleep guarantees that a `producer | grep -q proxy` pipe SIGPIPEs the producer
# mid-write (reproducing the original bug); the fixed here-string path is
# timing-independent and matches regardless.
net_known_services() { printf 'proxy\n'; sleep 0.3; printf 'firewall\nagent\n'; }
load_net_helpers()       { :; }
netlib_overrides_is_open() { return 1; }   # nothing open yet
netlib_overrides_open()    { :; }
net_confirm_phrase()       { return 1; }    # abort right after membership passes

# T1: a valid first-listed service passes membership and reaches the confirm
#     gate (it must NOT be rejected as unknown).
out=$(cmd_net_open proxy 2>&1) || true
if grep -q "unknown service" <<<"$out"; then
    echo "$out" | sed 's/^/    /'
    fail "T1: 'proxy' (first-listed) wrongly rejected as unknown — pipefail/SIGPIPE regression"
fi
ok "T1: first-listed service 'proxy' passes membership validation"

# T1b: it actually reached the confirmation gate (the warning text), proving it
#      got past validation rather than erroring earlier.
if ! grep -q "disable the egress firewall" <<<"$out"; then
    echo "$out" | sed 's/^/    /'
    fail "T1b: did not reach the confirm gate after membership passed"
fi
ok "T1b: reaches the typed-phrase confirm gate"

# T2: a genuinely unknown service is still rejected.
out=$(cmd_net_open totally-bogus 2>&1) || true
if ! grep -q "unknown service 'totally-bogus'" <<<"$out"; then
    echo "$out" | sed 's/^/    /'
    fail "T2: unknown service was not rejected"
fi
ok "T2: unknown service 'totally-bogus' still rejected"

# T3: a non-first valid service ('agent', last line) also passes — this case
#     worked even with the old pipe, so it pins the behaviour both ways.
out=$(cmd_net_open agent 2>&1) || true
if grep -q "unknown service" <<<"$out"; then
    echo "$out" | sed 's/^/    /'
    fail "T3: 'agent' (last-listed) wrongly rejected as unknown"
fi
ok "T3: last-listed service 'agent' passes membership validation"

echo
echo "NET OPEN TEST PASSED (${pass} checks)"
