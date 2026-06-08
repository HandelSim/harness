#!/usr/bin/env bash
#
# tests/unit_host_test.sh — exercise the containerless `harness host` surface
# without docker, network, or a real proxy/opencode spawn.
#
# Sources the real `harness` with HARNESS_SOURCE_ONLY=1 (the same hook
# tests/upgrade_test.sh uses) so the host_* helpers are callable directly, then
# pins the install-root globals (state_root / env_file) at a throwaway tmpdir.
#
# Covers the host functions flagged as untested in
# .planning/codebase/CONCERNS.md H4, plus regressions for the fixes in this
# change set:
#   - host_require_config           rejects empty required vars (exit 1)
#   - host_confirm_gate             honors HARNESS_HOST_CONFIRM=1
#   - host_preflight                fails clearly when deps are absent
#   - host_write_opencode_config    emits valid JSON, baseURL -> 127.0.0.1,
#                                   dummy apiKey; and refuses without jq (M4)
#   - host_proxy_fingerprint        stable, and sensitive to port/key changes (M2)
#
# Prints "HOST TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " host (containerless) unit test"
echo "============================================================"

fail() { echo "[host-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v jq >/dev/null 2>&1 || fail "this test needs host jq"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# Source the host helpers without running main. HARNESS_INSTALL_ROOT pins the
# state_root / env_file / clone_dir globals the helpers read.
# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null

[[ "$(type -t host_require_config)" == "function" ]] || fail "host_require_config not sourced"
[[ "$(type -t host_write_opencode_config)" == "function" ]] || fail "host helpers not sourced"

# The helpers read $env_file; create it so the file-exists check passes.
: >"$env_file"

# --- T1: host_require_config rejects an empty required var -------------------
# Runs in a subshell because the function calls `exit 1` on failure.
if ( PROXY_API_URL=""; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) >/dev/null 2>&1; then
    fail "T1: host_require_config accepted an empty PROXY_API_URL"
fi
out=$( ( PROXY_API_URL=""; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) 2>&1 || true )
grep -q "PROXY_API_URL" <<<"$out" || fail "T1: error should name PROXY_API_URL — $out"
ok "T1: host_require_config rejects empty required vars and names them"

# --- T2: host_require_config passes when all three are set ------------------
if ! ( PROXY_API_URL="http://x"; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) >/dev/null 2>&1; then
    fail "T2: host_require_config rejected a fully-populated config"
fi
ok "T2: host_require_config passes with all required vars set"

# --- T3: host_confirm_gate honors HARNESS_HOST_CONFIRM=1 --------------------
if ! ( HARNESS_HOST_CONFIRM=1; host_confirm_gate 0 ) >/dev/null 2>&1; then
    fail "T3: host_confirm_gate did not auto-confirm with HARNESS_HOST_CONFIRM=1"
fi
out=$( ( HARNESS_HOST_CONFIRM=1; host_confirm_gate 0 ) 2>&1 || true )
grep -qi "auto-confirmed" <<<"$out" || fail "T3: expected auto-confirm notice — $out"
ok "T3: host_confirm_gate auto-confirms via HARNESS_HOST_CONFIRM=1"

# --- T4: host_preflight fails clearly when deps are absent ------------------
# Empty PATH => command -v finds none of python3/jq/node/opencode.
out=$( ( PATH="/nonexistent-dir"; host_preflight ) 2>&1 || true )
if ( PATH="/nonexistent-dir"; host_preflight ) >/dev/null 2>&1; then
    fail "T4: host_preflight passed with no deps on PATH"
fi
for dep in python3 jq node opencode; do
    grep -qi "$dep" <<<"$out" || fail "T4: preflight output should mention $dep — $out"
done
ok "T4: host_preflight fails and names every missing dep"

# --- T5: host_write_opencode_config emits valid JSON pointed at loopback ----
( PROXY_PORT=8123; DEFAULT_MODEL_NAME="test-model"; host_write_opencode_config ) \
    || fail "T5: host_write_opencode_config returned non-zero"
cfg="$(host_opencode_config)"
[[ -f "$cfg" ]] || fail "T5: config file not written at $cfg"
jq -e . "$cfg" >/dev/null 2>&1 || fail "T5: config is not valid JSON"
base=$(jq -r '.provider.harness.options.baseURL' "$cfg")
[[ "$base" == "http://127.0.0.1:8123/v1" ]] || fail "T5: baseURL should be loopback:8123/v1, got '$base'"
key=$(jq -r '.provider.harness.options.apiKey' "$cfg")
[[ "$key" == "harness-dummy" ]] || fail "T5: apiKey should be the dummy placeholder, got '$key'"
jq -e '.provider.harness.models | has("test-model")' "$cfg" >/dev/null 2>&1 \
    || fail "T5: models map should include the default model"
ok "T5: opencode config is valid JSON, loopback baseURL, dummy key, default model present"

# --- T6: host_write_opencode_config refuses when jq is absent (M4 guard) ----
if ( PATH="/nonexistent-dir"; PROXY_PORT=8123; DEFAULT_MODEL_NAME="m"; host_write_opencode_config ) >/dev/null 2>&1; then
    fail "T6: host_write_opencode_config should fail without jq, not reach the docker sidecar"
fi
ok "T6: host_write_opencode_config refuses without host jq (no docker fallback)"

# --- T7: host_proxy_fingerprint is stable and config-sensitive (M2) ---------
fp1=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
fp2=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ -n "$fp1" && "$fp1" == "$fp2" ]] || fail "T7: fingerprint not stable across identical config ($fp1 / $fp2)"
fp_port=$( PROXY_PORT=9999 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ "$fp_port" != "$fp1" ]] || fail "T7: fingerprint should change when PROXY_PORT changes"
fp_key=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=DIFFERENT DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ "$fp_key" != "$fp1" ]] || fail "T7: fingerprint should change when PROXY_API_KEY changes"
ok "T7: host_proxy_fingerprint is stable and changes on port/key edits"

echo
echo "HOST TEST PASSED"
