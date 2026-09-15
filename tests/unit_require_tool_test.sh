#!/usr/bin/env bash
#
# tests/unit_require_tool_test.sh — exercise the CLI half of require-tool mode
# (`harness start/restart --require-tool`, `harness host --require-tool`,
# `harness --require-tool`),
# docker-free.
#
# Require-tool mode itself lives in the proxy (see proxy/test_proxy.py); this
# file covers only the plumbing that has to get HARNESS_REQUIRE_TOOL=1 from a
# flag to the proxy process, in both run modes:
#
#   - _parse_start_flags: the shared start/restart parser now takes a second
#     flag, still rejects unknown options, and takes both flags together
#   - write_runtime_override: HARNESS_REQUIRE_TOOL lands on the proxy service
#     and SHARES the one `proxy:` mapping with the other ephemeral overrides
#     (duplicate top-level service keys are invalid compose YAML), including
#     when the firewall loop emitted that block first
#   - _require_tool_on: the flag beats .env, and only a value the proxy itself
#     reads as truthy counts — HARNESS_REQUIRE_TOOL=0 is off
#   - host_proxy_fingerprint: folding require-tool in is what makes
#     `harness host --require-tool` restart a proxy that is already running,
#     and an off/0 install's fingerprint must stay byte-identical to the
#     pre-feature one or every host user's next launch kills a healthy proxy
#   - cmd_host: the flag is consumed, never forwarded to opencode
#   - run_agent: the same for a bare `harness --require-tool` launch, which
#     used to forward the flag to opencode (which then refused to start)
#   - _running_proxy_require_tool / ensure_services_up: the container-mode
#     reconciliation that makes the flag take effect against a proxy that is
#     already up, and leaves one alone when this launch does not ask for it
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
echo " require-tool flag (CLI) unit test"
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

for fn in _parse_start_flags _require_tool_on write_runtime_override \
          host_proxy_fingerprint seed_require_tool_reminder_file cmd_host; do
    [[ "$(type -t "$fn")" == "function" ]] || fail "$fn not sourced"
done
ok "require-tool functions sourced"

# --- T1: _parse_start_flags takes --require-tool, alone and combined ---------
t1=$(
    HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
    _parse_start_flags --require-tool
    echo "alone=${require_tool_override}"
    _parse_start_flags --require-tool --prompt-mode user_front
    echo "both=${require_tool_override}/${prompt_mode_override}"
)
grep -q 'alone=1' <<<"$t1" || fail "T1: --require-tool did not set the override — $t1"
grep -q 'both=1/user_front' <<<"$t1" \
    || fail "T1: --require-tool and --prompt-mode are not accepted together — $t1"
# The parser is still strict: an unknown option must abort non-zero.
t1_rc=0
( HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
  _parse_start_flags --require-tools ) >/dev/null 2>&1 || t1_rc=$?
(( t1_rc != 0 )) || fail "T1: a misspelled --require-tools was accepted"
ok "T1: _parse_start_flags accepts --require-tool and still rejects typos"

# --- T2: write_runtime_override injects HARNESS_REQUIRE_TOOL ----------------
require_tool_override=1; prompt_mode_override=""; backend_override=""
write_runtime_override
[[ -f "$runtime_override" ]] || fail "T2: override file not written"
grep -q 'HARNESS_REQUIRE_TOOL: "1"' "$runtime_override" \
    || fail "T2: HARNESS_REQUIRE_TOOL missing — $(cat "$runtime_override")"
grep -q '^  proxy:' "$runtime_override" || fail "T2: no proxy service block"
ok "T2: --require-tool emits HARNESS_REQUIRE_TOOL on the proxy service"

# --- T3: require-tool and prompt-mode share ONE proxy: mapping --------------
prompt_mode_override="user_front"
write_runtime_override
[[ "$(grep -c '^  proxy:' "$runtime_override")" == "1" ]] \
    || fail "T3: duplicate proxy: block — $(cat "$runtime_override")"
grep -q 'HARNESS_REQUIRE_TOOL: "1"' "$runtime_override" || fail "T3: require-tool dropped"
grep -q 'PROXY_PROMPT_MODE: "user_front"' "$runtime_override" || fail "T3: prompt mode dropped"
ok "T3: require-tool and prompt-mode share a single proxy: mapping"

# --- T4: no override active still removes the file --------------------------
require_tool_override=""; prompt_mode_override=""
write_runtime_override
[[ ! -f "$runtime_override" ]] || fail "T4: override file survived an empty body"
ok "T4: a launch with no overrides leaves no compose override behind"

# --- T5: the firewall loop's proxy: block absorbs it, no second mapping -----
# `harness net open proxy` makes write_runtime_override emit a proxy: block of
# its own first; the ephemeral overrides must be appended to THAT block.
if command -v jq >/dev/null 2>&1; then
    printf '%s\n' '{"services": {"proxy": {"firewall_disabled": true}}}' \
        >"$net_overrides_path"
    require_tool_override=1; prompt_mode_override=""
    write_runtime_override
    [[ "$(grep -c '^  proxy:' "$runtime_override")" == "1" ]] \
        || fail "T5: duplicate proxy: block with a firewall opt-out — $(cat "$runtime_override")"
    grep -q 'HARNESS_FIREWALL_DISABLED: "1"' "$runtime_override" \
        || fail "T5: firewall opt-out lost — $(cat "$runtime_override")"
    grep -q 'HARNESS_REQUIRE_TOOL: "1"' "$runtime_override" \
        || fail "T5: require-tool lost — $(cat "$runtime_override")"
    rm -f "$net_overrides_path"
    require_tool_override=""; write_runtime_override
    ok "T5: a firewall-opt-out proxy: block absorbs HARNESS_REQUIRE_TOOL"
else
    echo "[require-tool] SKIP: T5 needs jq"
fi

# --- T6: _require_tool_on — flag beats .env, and 0 means off ----------------
# The proxy reads 1/true/yes/on as on and everything else as off; the CLI must
# agree, or a user with HARNESS_REQUIRE_TOOL=0 in .env gets a changed host
# fingerprint (T7) for a feature that is not even on.
t6_case() {  # <flag> <env> <expect on|off>
    local got=off
    ( require_tool_override="$1"; HARNESS_REQUIRE_TOOL="$2"; _require_tool_on ) && got=on
    [[ "$got" == "$3" ]] \
        || fail "T6: flag='$1' env='$2' expected $3, got $got"
}
t6_case ""  ""      off
t6_case ""  "0"     off
t6_case ""  "no"    off
t6_case ""  "false" off
t6_case ""  "1"     on
t6_case ""  "true"  on
t6_case ""  "TRUE"  on
t6_case ""  "Yes"   on
t6_case ""  "on"    on
t6_case "1" ""      on
t6_case "1" "0"     on     # the one-shot flag overrides an off .env
ok "T6: _require_tool_on matches the proxy's truthy set and the flag wins"

# --- T7: host_proxy_fingerprint ---------------------------------------------
# Off and explicitly-0 installs must hash IDENTICALLY to a pre-feature harness,
# or the first launch after upgrading kills a healthy proxy and blames .env.
# Turning it on must change the hash, which is what restarts the proxy.
require_tool_override=""; unset HARNESS_REQUIRE_TOOL
fp_off=$(host_proxy_fingerprint)
fp_zero=$(HARNESS_REQUIRE_TOOL=0 host_proxy_fingerprint)
[[ "$fp_off" == "$fp_zero" ]] \
    || fail "T7: HARNESS_REQUIRE_TOOL=0 perturbed the fingerprint"
fp_flag=$(require_tool_override=1 host_proxy_fingerprint)
[[ "$fp_flag" != "$fp_off" ]] \
    || fail "T7: --require-tool did not change the fingerprint (the reuse short-circuit would swallow the flag)"
fp_env=$(HARNESS_REQUIRE_TOOL=1 host_proxy_fingerprint)
[[ "$fp_env" == "$fp_flag" ]] \
    || fail "T7: the .env key and the flag must produce the same fingerprint"
ok "T7: require-tool is folded into the host fingerprint only when it is on"

# --- T8: cmd_host consumes --require-tool and never forwards it -------------
# opencode rejects unknown flags, so the arg must be eaten by the loop; and it
# must reach host_proxy_start as require_tool_override.
t8_rc=0
t8_out=$(
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
        echo "OVERRIDE=${require_tool_override}"
        echo "PASSARGS=${pass_args[*]-}"
        exit 43
    }
    cmd_host --require-tool 2>&1
) || t8_rc=$?
(( t8_rc == 43 )) || fail "T8: cmd_host did not reach host_proxy_start (rc=$t8_rc) — $t8_out"
grep -q 'OVERRIDE=1' <<<"$t8_out" \
    || fail "T8: cmd_host --require-tool did not set require_tool_override — $t8_out"
grep -q 'PASSARGS=$' <<<"$t8_out" \
    || fail "T8: --require-tool was forwarded to opencode — $t8_out"
ok "T8: cmd_host --require-tool is consumed, not passed through to opencode"

# --- T10: run_agent consumes --require-tool and never forwards it -----------
# The bug this covers: bare `harness --require-tool` fell through run_agent's
# flag loop into pass_args, so opencode got an unknown flag and refused to
# start, and the proxy never saw the mode. The flag must be eaten here and
# land in require_tool_override, exactly as cmd_host does it (T8).
t10_rc=0
t10_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
        source "$HARNESS" >/dev/null 2>&1
    require_docker()             { :; }
    _gate_on_upstream_auth()     { return 0; }
    ensure_services_up()         { echo "ENSURE=${require_tool_override}"; }
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
) || t10_rc=$?
(( t10_rc == 45 )) || fail "T10: run_agent did not reach the launcher (rc=$t10_rc) — $t10_out"
grep -q 'ENSURE=1' <<<"$t10_out" \
    || fail "T10: --require-tool did not set require_tool_override before launch — $t10_out"
grep -q 'PASSARGS=\[\]$' <<<"$t10_out" \
    || fail "T10: --require-tool was forwarded to opencode — $t10_out"
ok "T10: bare 'harness --require-tool' is consumed, not passed through to opencode"

# --- T11: _running_proxy_require_tool reads the container env ---------------
# The running proxy captured HARNESS_REQUIRE_TOOL at `up` time; the reader has
# to agree with the proxy's truthy set, and report 0 (not an error) for a
# container built before the key existed.
# `harness_docker` lives in scripts/lib/platform.sh and is not defined in a
# sourced-only shell, so both saves have to tolerate an empty result.
t11_compose_saved=$(declare -f compose || true)
t11_docker_saved=$(declare -f harness_docker || true)
compose() { echo "proxycid123"; }
harness_docker() { printf 'PATH=/usr/bin\nHARNESS_REQUIRE_TOOL=1\nPROXY_PORT=8000\n'; }
[[ "$(_running_proxy_require_tool)" == "1" ]] || fail "T11: an on proxy must read as 1"
harness_docker() { printf 'PATH=/usr/bin\nHARNESS_REQUIRE_TOOL=TRUE\n'; }
[[ "$(_running_proxy_require_tool)" == "1" ]] || fail "T11: TRUE must read as 1"
harness_docker() { printf 'PATH=/usr/bin\nHARNESS_REQUIRE_TOOL=0\n'; }
[[ "$(_running_proxy_require_tool)" == "0" ]] || fail "T11: 0 must read as 0"
harness_docker() { printf 'PATH=/usr/bin\nPROXY_PORT=8000\n'; }
[[ "$(_running_proxy_require_tool)" == "0" ]] \
    || fail "T11: a pre-feature container with no key must read as 0"
compose() { echo ""; }
_running_proxy_require_tool >/dev/null 2>&1 && fail "T11: no proxy must report not-running"
if [[ -n "$t11_compose_saved" ]]; then eval "$t11_compose_saved"; else unset -f compose; fi
if [[ -n "$t11_docker_saved" ]]; then eval "$t11_docker_saved"; else unset -f harness_docker; fi
ok "T11: _running_proxy_require_tool reports the running container's mode"

# --- T12: ensure_services_up restarts a proxy that lacks require-tool -------
# ensure_services_up is a no-op when the proxy is already up, so without this
# `harness --require-tool` against a running proxy (another agent, or a `-p`
# run that left it behind) would silently do nothing. The reverse direction is
# deliberately NOT reconciled: a plain launch must not restart the proxy out
# from under a concurrent --require-tool agent.
START_LOG="$TMP_ROOT/start.log"
t12() {  # <flag> <env> <running> <expect restart|no-restart> <label>
    local got=no-restart out
    : >"$START_LOG"
    out=$(
        services_up()               { return 0; }
        _chatgpt_in_play()          { return 1; }
        host_mcp_start_enabled()    { :; }
        cmd_start()                 { echo started >>"$START_LOG"; }
        eval "_running_proxy_require_tool() { printf '%s' '$3'; }"
        require_tool_override="$1"
        HARNESS_REQUIRE_TOOL="$2"
        ensure_services_up 2>&1
    )
    [[ -s "$START_LOG" ]] && got=restart
    [[ "$got" == "$4" ]] || fail "T12: $5 — expected $4, got $got ($out)"
}
t12 1  ""  0 restart    "--require-tool against a proxy without it"
t12 1  ""  1 no-restart "--require-tool against a proxy that already has it"
t12 "" ""  1 no-restart "a plain launch leaves a require-tool proxy alone"
t12 "" ""  0 no-restart "a plain launch against a plain proxy"
t12 "" "1" 0 restart    "HARNESS_REQUIRE_TOOL=1 in .env reconciles a stale proxy"
t12 "" "0" 0 no-restart "HARNESS_REQUIRE_TOOL=0 never restarts"
rm -f "$START_LOG"
ok "T12: ensure_services_up brings a running proxy into require-tool mode"

# --- T9: the flag is documented in both help texts --------------------------
help_out=$(HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1; cmd_help 2>&1)
grep -q -- '--require-tool' <<<"$help_out" \
    || fail "T9: cmd_help does not mention --require-tool"
# It is an agent flag now too (bare `harness --require-tool`), so it has to be
# listed with the other ones and not only under `start`.
sed -n '/^agent flags:/,$p' <<<"$help_out" | grep -q -- '--require-tool' \
    || fail "T9: --require-tool is missing from the agent flags list"
host_help=$(HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1; cmd_host --help 2>&1)
grep -q -- '--require-tool' <<<"$host_help" \
    || fail "T9: 'harness host --help' does not mention --require-tool"
ok "T9: --require-tool is documented in 'harness help' and 'harness host --help'"

echo "------------------------------------------------------------"
echo "REQUIRE-TOOL TEST PASSED (${pass} checks)"
