#!/usr/bin/env bash
#
# tests/unit_single_message_test.sh — the CLI half of the opt-in single-message
# prompt mode (`--single-message` = `--prompt-mode single`), docker-free.
#
# The fold itself lives in the proxy (proxy/test_proxy.py TestSinglePromptMode).
# What the CLI owes:
#
#   - _parse_start_flags: --single-message and --prompt-mode single both set
#     prompt_mode_override=single; nothing passed leaves it empty (hybrid);
#     an unknown mode is still rejected
#   - write_runtime_override: emits PROXY_PROMPT_MODE: "single" for the proxy
#   - host_proxy_fingerprint: unchanged for a default launch, different for a
#     single launch (so switching modes restarts the host proxy)
#   - cmd_host: consumes --single-message / --prompt-mode M (never forwarded to
#     opencode), rejects a bad mode before starting anything, and leaves the
#     override empty without the flag
#   - host_proxy_start hands the mode to the proxy as PROXY_PROMPT_MODE
#
# Run:  bash tests/unit_single_message_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " single-message prompt mode (CLI) unit test"
echo "============================================================"

pass=0
fail() { echo "[single-message] FAIL: $*" >&2; exit 1; }
ok()   { echo "[single-message] OK: $*"; pass=$((pass + 1)); }

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
clone_dir="$REPO_ROOT"

# --- T1: _parse_start_flags ---------------------------------------------------
t1=$(
    HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
    _parse_start_flags
    echo "none=[${prompt_mode_override}]"
    _parse_start_flags --single-message
    echo "flag=[${prompt_mode_override}]"
    prompt_mode_override=""
    _parse_start_flags --prompt-mode single
    echo "mode=[${prompt_mode_override}]"
    prompt_mode_override=""
    _parse_start_flags --prompt-mode=single
    echo "eq=[${prompt_mode_override}]"
)
grep -q 'none=\[\]' <<<"$t1" || fail "T1: no flag did not leave the default — $t1"
grep -q 'flag=\[single\]' <<<"$t1" || fail "T1: --single-message — $t1"
grep -q 'mode=\[single\]' <<<"$t1" || fail "T1: --prompt-mode single — $t1"
grep -q 'eq=\[single\]' <<<"$t1" || fail "T1: --prompt-mode=single — $t1"
t1_rc=0
( HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
  _parse_start_flags --prompt-mode singel ) >/dev/null 2>&1 || t1_rc=$?
(( t1_rc != 0 )) || fail "T1: a misspelled mode was accepted"
ok "T1: start/restart accept --single-message and --prompt-mode single; typos rejected"

# --- T2: write_runtime_override ----------------------------------------------
prompt_mode_override="single"; backend_override=""
write_runtime_override
grep -q 'PROXY_PROMPT_MODE: "single"' "$runtime_override" \
    || fail "T2: override lacks PROXY_PROMPT_MODE single — $(cat "$runtime_override")"
prompt_mode_override=""
write_runtime_override
[[ ! -f "$runtime_override" ]] || fail "T2: default launch left an override behind"
ok "T2: the container override carries the single mode only when asked"

# --- T3: host_proxy_fingerprint ---------------------------------------------
prompt_mode_override=""
fp_default=$(host_proxy_fingerprint)
fp_default2=$(host_proxy_fingerprint)
prompt_mode_override="single"
fp_single=$(host_proxy_fingerprint)
prompt_mode_override=""
[[ "$fp_default" == "$fp_default2" ]] || fail "T3: default fingerprint is unstable"
[[ "$fp_default" != "$fp_single" ]] || fail "T3: single mode does not change the fingerprint"
# The default fingerprint must not hash any promptmode line (byte-identical to
# before the feature, so existing host proxies are not restarted on upgrade).
grep -q 'if \[\[ -n "${prompt_mode_override:-}" \]\]; then' \
    < <(sed -n '/^host_proxy_fingerprint()/,/^}/p' "$HARNESS") \
    || fail "T3: promptmode is not appended conditionally"
ok "T3: the host fingerprint changes with --single-message and not otherwise"

# --- T4: cmd_host flag handling ---------------------------------------------
run_host() {
    (
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
            echo "MODE=[${prompt_mode_override}] PASSARGS=[${pass_args[*]-}]"
            exit 43
        }
        cmd_host "$@" 2>&1
    )
}
rc=0; out=$(run_host --single-message -p hi) || rc=$?
(( rc == 43 )) || fail "T4: cmd_host --single-message did not reach the proxy start (rc=$rc) — $out"
grep -q 'MODE=\[single\] PASSARGS=\[-p hi\]' <<<"$out" || fail "T4: --single-message — $out"
rc=0; out=$(run_host --prompt-mode single) || rc=$?
grep -q 'MODE=\[single\] PASSARGS=\[\]' <<<"$out" || fail "T4: --prompt-mode single — $out"
rc=0; out=$(run_host --prompt-mode=user_front) || rc=$?
grep -q 'MODE=\[user_front\]' <<<"$out" || fail "T4: --prompt-mode=user_front — $out"
rc=0; out=$(run_host -p hi) || rc=$?
grep -q 'MODE=\[\] PASSARGS=\[-p hi\]' <<<"$out" || fail "T4: plain host is not default — $out"
rc=0; out=$(run_host --prompt-mode bogus) || rc=$?
(( rc != 43 && rc != 0 )) || fail "T4: a bad mode reached the proxy start — $out"
rc=0; out=$(run_host --prompt-mode) || rc=$?
(( rc != 43 && rc != 0 )) || fail "T4: a missing mode value reached the proxy start — $out"
ok "T4: cmd_host consumes the mode flags, validates them, and defaults to hybrid"

# --- T5: host_proxy_start passes the mode to the proxy -----------------------
grep -q 'PROXY_PROMPT_MODE="${prompt_mode_override:-}"' \
    < <(sed -n '/^host_proxy_start()/,/^}/p' "$HARNESS") \
    || fail "T5: host_proxy_start does not export PROXY_PROMPT_MODE"
ok "T5: host_proxy_start exports PROXY_PROMPT_MODE"

echo "[single-message] all $pass checks passed"
