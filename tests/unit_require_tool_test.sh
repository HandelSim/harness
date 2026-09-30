#!/usr/bin/env bash
#
# tests/unit_require_tool_test.sh — exercise the CLI half of require-tool mode
# and the no-docker fallback of a bare launch, docker-free.
#
# Require-tool mode itself lives in the proxy (see proxy/test_proxy.py), where
# it is always on outside the passthrough prompt mode. It used to be opt-in via
# a `--require-tool` flag and a HARNESS_REQUIRE_TOOL .env key; both are gone as
# switches. What the CLI still owes:
#
#   - _parse_start_flags: `--require-tool` is still ACCEPTED (old scripts pass
#     it) but sets nothing, and the parser still rejects typos
#   - write_runtime_override: never emits HARNESS_REQUIRE_TOOL, and a launch
#     with no other override still leaves no compose override behind
#   - host_proxy_fingerprint: carries the require-tool marker unconditionally
#     (so a host proxy started before the default flipped is restarted once),
#     and a leftover HARNESS_REQUIRE_TOOL=0 in .env cannot perturb it
#   - cmd_host / run_agent: `--require-tool` is consumed, never forwarded to
#     opencode, which aborts on an unknown flag
#   - run_agent on a box with no container runtime installed: says docker is
#     not available and hands off to cmd_host with the host-meaningful flags;
#     with a runtime installed it does NOT fall back
#   - neither help text advertises the flag any more
#
# Sources `harness` with HARNESS_SOURCE_ONLY=1 so main() never runs, pointed at
# a throwaway install root.
#
# Run:  bash tests/unit_require_tool_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " require-tool default + no-docker fallback (CLI) unit test"
echo "============================================================"

pass=0
fail() { echo "[require-tool] FAIL: $*" >&2; exit 1; }
ok()   { echo "[require-tool] OK: $*"; pass=$((pass + 1)); }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

ALLOWLIST="$TMP_ROOT/.harness-allowlist"
: >"$ALLOWLIST"
cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://api.example.com
PROXY_API_KEY=sk-test-key
DEFAULT_MODEL_NAME=test-model
EOF

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
    source "$HARNESS" 2>/dev/null

# Sourcing resolves the script's self-path to THIS test file, so clone_dir would
# point at tests/. Pin it at the repo the way a real install has it, so
# host_proxy_fingerprint finds proxy/requirements.txt.
clone_dir="$REPO_ROOT"

for fn in _parse_start_flags write_runtime_override host_proxy_fingerprint \
          cmd_host run_agent harness_runtime_installed; do
    [[ "$(type -t "$fn")" == "function" ]] || fail "$fn not sourced"
done
for fn in _require_tool_on _require_tool_truthy _running_proxy_require_tool \
          seed_require_tool_reminder_file; do
    [[ "$(type -t "$fn")" != "function" ]] || fail "$fn should be gone"
done
grep -q 'HARNESS_REQUIRE_TOOL' "$HARNESS" \
    && fail "harness still references HARNESS_REQUIRE_TOOL"
ok "functions sourced; the opt-in plumbing is gone"

# --- T1: _parse_start_flags accepts --require-tool as a no-op ---------------
t1=$(
    HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
    _parse_start_flags --require-tool
    echo "alone=[${prompt_mode_override}]"
    _parse_start_flags --require-tool --prompt-mode user_front
    echo "both=[${prompt_mode_override}]"
)
grep -q 'alone=\[\]' <<<"$t1" || fail "T1: --require-tool alone was not a no-op — $t1"
grep -q 'both=\[user_front\]' <<<"$t1" \
    || fail "T1: --require-tool and --prompt-mode are not accepted together — $t1"
t1_rc=0
( HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
  _parse_start_flags --require-tools ) >/dev/null 2>&1 || t1_rc=$?
(( t1_rc != 0 )) || fail "T1: a misspelled --require-tools was accepted"
ok "T1: _parse_start_flags accepts --require-tool as a no-op and still rejects typos"

# --- T2: write_runtime_override never emits HARNESS_REQUIRE_TOOL ------------
prompt_mode_override="user_front"; backend_override=""
write_runtime_override
[[ -f "$runtime_override" ]] || fail "T2: override file not written"
grep -q 'HARNESS_REQUIRE_TOOL' "$runtime_override" \
    && fail "T2: HARNESS_REQUIRE_TOOL emitted — $(cat "$runtime_override")"
prompt_mode_override=""
write_runtime_override
[[ ! -f "$runtime_override" ]] || fail "T2: override file survived an empty body"
ok "T2: the runtime override carries no require-tool key"

# --- T3: host_proxy_fingerprint ---------------------------------------------
# The marker is always hashed, so a host proxy left running from before the
# default flipped (its fingerprint lacks it) is restarted instead of reused.
fp=$(host_proxy_fingerprint)
fp_zero=$(HARNESS_REQUIRE_TOOL=0 host_proxy_fingerprint)
[[ "$fp" == "$fp_zero" ]] \
    || fail "T3: a leftover HARNESS_REQUIRE_TOOL=0 perturbed the fingerprint"
grep -q "requiretool=1" < <(sed -n '/^host_proxy_fingerprint()/,/^}/p' "$HARNESS") \
    || fail "T3: host_proxy_fingerprint no longer hashes the require-tool marker"
ok "T3: the host fingerprint always carries require-tool and ignores the old key"

# --- T4: cmd_host consumes --require-tool and never forwards it -------------
t4_rc=0
t4_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    host_require_python3()  { :; }
    host_require_config()   { :; }
    host_confirm_gate()     { :; }
    ensure_dirs()           { :; }
    host_ensure_toolchain() { :; }
    host_preflight()        { :; }
    _gate_on_upstream_auth() { return 0; }
    _print_upstream_models() { return 0; }
    host_proxy_start() {
        echo "PASSARGS=${pass_args[*]-}"
        exit 43
    }
    cmd_host --require-tool 2>&1
) || t4_rc=$?
(( t4_rc == 43 )) || fail "T4: cmd_host did not reach host_proxy_start (rc=$t4_rc) — $t4_out"
grep -q 'PASSARGS=$' <<<"$t4_out" \
    || fail "T4: --require-tool was forwarded to opencode — $t4_out"
ok "T4: cmd_host --require-tool is consumed, not passed through to opencode"

# --- T5: run_agent consumes --require-tool and never forwards it ------------
# With a runtime installed the launch stays in container mode (no fallback).
t5_rc=0
t5_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
        source "$HARNESS" >/dev/null 2>&1
    harness_runtime_installed()  { return 0; }
    cmd_host()                   { echo "FELL_BACK"; exit 44; }
    require_docker()             { :; }
    _gate_on_upstream_auth()     { return 0; }
    ensure_services_up()         { :; }
    ensure_dirs()                { :; }
    _update_check_and_banner()   { :; }
    _check_and_offer_config_merge() { :; }
    warn_if_firewall_open()      { return 0; }
    write_agent_mcp_config()     { :; }
    harness_abs_path()           { printf '%s' "$1"; }
    harness_docker()             { return 0; }
    agent_image()                { echo img; }
    agent_home_dir()             { echo "$TMP_ROOT/agent-home"; }
    run_agent_interactive() {
        shift 8
        echo "PASSARGS=[$*]"
        exit 45
    }
    run_agent opencode --require-tool --yolo 2>&1
) || t5_rc=$?
(( t5_rc == 45 )) || fail "T5: run_agent did not reach the container launcher (rc=$t5_rc) — $t5_out"
grep -q 'PASSARGS=\[\]$' <<<"$t5_out" \
    || fail "T5: --require-tool was forwarded to opencode — $t5_out"
ok "T5: 'harness --require-tool' is consumed, and a runtime box stays in container mode"

# --- T6: no runtime installed -> announce and fall back to cmd_host ----------
t6_launch() {
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
        source "$HARNESS" >/dev/null 2>&1
    harness_runtime_installed() { return 1; }
    require_docker()            { echo "REQUIRE_DOCKER_REACHED"; exit 1; }
    cmd_host() { echo "HOSTARGS=[$*] BACKEND=[${backend_override:-}]"; exit 46; }
    run_agent opencode "$@" 2>&1
}
t6_rc=0
t6_out=$(t6_launch --yolo --net --mount /nonexistent -p "do it" --require-tool) || t6_rc=$?
(( t6_rc == 46 )) || fail "T6: run_agent did not hand off to cmd_host (rc=$t6_rc) — $t6_out"
grep -q "docker is not available" <<<"$t6_out" \
    || fail "T6: no 'docker is not available' notice — $t6_out"
grep -q "defaulting to 'harness host'" <<<"$t6_out" \
    || fail "T6: the notice does not say it is defaulting to host mode — $t6_out"
grep -q 'HOSTARGS=\[--yolo -p do it\]' <<<"$t6_out" \
    || fail "T6: wrong args handed to cmd_host (--yolo/-p kept, --net/--mount/--require-tool dropped) — $t6_out"
grep -q "ignoring --mount" <<<"$t6_out" || fail "T6: dropped --mount not reported — $t6_out"
t6_rc=0
t6_out=$(t6_launch) || t6_rc=$?
(( t6_rc == 46 )) && grep -q 'HOSTARGS=\[\]' <<<"$t6_out" \
    || fail "T6: a bare 'harness' did not fall back with no args (rc=$t6_rc) — $t6_out"
ok "T6: with no docker/podman installed, 'harness' says so and runs 'harness host'"

# --- T7: help no longer advertises the flag ---------------------------------
help_out=$(HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1; cmd_help 2>&1)
sed -n '/^agent flags:/,$p' <<<"$help_out" | grep -q -- '--require-tool' \
    && fail "T7: --require-tool is still listed as an agent flag"
grep -q "falls back\|runs 'harness host' instead" <<<"$help_out" \
    || fail "T7: cmd_help does not mention the no-docker fallback"
host_help=$(HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1; cmd_host --help 2>&1)
grep -q -- '\[--require-tool\]' <<<"$host_help" \
    && fail "T7: 'harness host --help' still lists --require-tool in its usage"
grep -q "finish" <<<"$host_help" \
    || fail "T7: 'harness host --help' does not explain the finish-only ending"
ok "T7: help reflects require-tool as the default and documents the fallback"

echo "------------------------------------------------------------"
echo "REQUIRE-TOOL TEST PASSED (${pass} checks)"
