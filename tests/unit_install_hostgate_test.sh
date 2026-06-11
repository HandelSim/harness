#!/usr/bin/env bash
#
# tests/unit_install_hostgate_test.sh — exercise harness-install.sh's
# install-time host-only risk-acceptance gate (host_only_risk_gate) without
# docker, a clone, or any disk write.
#
# The installer is not structured for partial sourcing (sourcing it runs the
# whole install at top level), so we awk-extract just the host_only_risk_gate
# function and source that fragment with stubs for the helpers/colors it uses.
# This mirrors how harness_test.sh exercises installer-only branches in
# isolation.
#
# Like unit_host_test.sh's coverage of the launch-time host_confirm_gate, this
# test does NOT drive the interactive `read </dev/tty` path: there is no pty in
# CI, so exercising it would either hang (a real tty is present) or be
# environment-dependent. The deterministic paths are:
#   - T1: container install (HOST_ONLY=0) is a no-op (returns 0, prints nothing)
#   - T2: HOST_ONLY=1 + HARNESS_HOST_CONFIRM=1 proceeds, and the notice states
#         the risks (no firewall, full host user) and recommends docker
#   - T3: static-source check that the refuse-without-/dev/tty branch and the
#         HARNESS_HOST_CONFIRM bypass are present (the interactive path's
#         discipline)
#
# Prints "INSTALL HOSTGATE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALLER="${REPO_ROOT}/harness-install.sh"

echo "============================================================"
echo " install host-only risk-gate unit test"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

[[ -f "$INSTALLER" ]] || fail "installer not found at $INSTALLER"

# Extract just the host_only_risk_gate function body. The only column-0 '}' in
# the function is its closing brace (the inner blocks end with fi/esac), so the
# range match is exact.
gate_src="$(awk '/^host_only_risk_gate\(\) \{/,/^}/' "$INSTALLER")"
[[ -n "$gate_src" ]] || fail "could not extract host_only_risk_gate from installer"

# Source the fragment with stubs for everything it references at the top level
# of the installer (colors + title/fail). We define our own here; keep them
# quiet so output assertions only see the gate's own text.
build_env() {
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
    title() { printf '%s\n' "$*"; }
    fail()  { printf 'x %s\n' "$*" >&2; }   # non-aborting, like the installer's
    eval "$gate_src"
}

# --- T1: container install (HOST_ONLY=0) is a silent no-op ------------------
out="$(
    set +e
    ( build_env; HOST_ONLY=0; host_only_risk_gate ) 2>&1
    echo "rc=$?"
)"
rc="${out##*rc=}"
body="${out%rc=*}"
[[ "$rc" -eq 0 ]] || fail "T1: container install should return 0, got rc=$rc"
[[ -z "${body//[[:space:]]/}" ]] || fail "T1: container install should print nothing, got: $body"
ok "T1: container install (HOST_ONLY=0) is a no-op (rc 0, no output)"

# --- T2: HOST_ONLY=1 + HARNESS_HOST_CONFIRM=1 proceeds, with the notice ------
out="$(
    set +e
    ( build_env; HOST_ONLY=1; HARNESS_HOST_CONFIRM=1; host_only_risk_gate ) 2>&1
    echo "rc=$?"
)"
rc="${out##*rc=}"
body="${out%rc=*}"
[[ "$rc" -eq 0 ]] || fail "T2: HARNESS_HOST_CONFIRM=1 should auto-accept (rc 0), got rc=$rc"
grep -qi "auto-accepted via HARNESS_HOST_CONFIRM=1" <<<"$body" \
    || fail "T2: missing auto-accept note — $body"
grep -qi "NO egress firewall" <<<"$body" \
    || fail "T2: notice must state there is no egress firewall — $body"
grep -qi "full host user" <<<"$body" \
    || fail "T2: notice must state opencode runs as the full host user — $body"
grep -qiE "install docker( or podman)?" <<<"$body" \
    || fail "T2: notice must recommend installing docker — $body"
ok "T2: bypass proceeds and the notice states the risks + recommends docker"

# --- T3: refuse-without-/dev/tty branch + bypass exist (interactive discipline)
grep -q 'if \[\[ ! -e /dev/tty \]\]; then' <<<"$gate_src" \
    || fail "T3: gate must refuse a non-interactive install without /dev/tty"
grep -q 'HARNESS_HOST_CONFIRM:-0' <<<"$gate_src" \
    || fail "T3: gate must honor the HARNESS_HOST_CONFIRM=1 bypass"
grep -q 'read -r ans </dev/tty' <<<"$gate_src" \
    || fail "T3: gate must read the acceptance answer from /dev/tty"
ok "T3: refuse-without-tty branch and HARNESS_HOST_CONFIRM bypass are present"

echo
echo "INSTALL HOSTGATE TEST PASSED"
