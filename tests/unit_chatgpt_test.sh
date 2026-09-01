#!/usr/bin/env bash
#
# tests/unit_chatgpt_test.sh — exercise the ChatGPT backend-api integration in
# the CLI (`harness chatgpt` / `harness chatgpt host`), docker-free.
#
# Sources `harness` with HARNESS_SOURCE_ONLY=1 so main() never runs, pointed at
# a throwaway install root so nothing touches a real install. Covers:
#   - _backend_is_chatgpt / _effective_default_model: the two-backend split
#   - write_runtime_override: PROXY_BACKEND lands on the proxy service, shares
#     ONE `proxy:` mapping with PROXY_PROMPT_MODE, and the file still
#     disappears when no override is active
#   - require_runtime_config / host_require_config: the required-var set swaps
#     to the CHATGPT_* trio, so a chatgpt-only .env launches and a missing
#     CHATGPT_* still aborts
#   - _gate_on_upstream_auth / _print_upstream_models: both no-op for chatgpt
#     (cookie auth has no bearer probe and the backend-api has no catalog)
#   - host_proxy_fingerprint: covers the backend and all three CHATGPT_*
#     values, so switching backends restarts a running host proxy
#   - _running_proxy_backend / ensure_services_up: a container proxy serving
#     the other backend is restarted rather than silently reused
#   - _config_is_secret / _config_set: the cookie is masked and setting
#     CHATGPT_BASE_URL syncs the egress allowlist
#   - cmd_chatgpt: --help prints without side effects; bare form launches an
#     agent, `host` form delegates to cmd_host, both with the backend set
#   - _config_write_key: a cookie with '; ' separators is quoted on disk, so
#     the rewritten .env still sources cleanly (harness sources it under -e)
#   - cmd_chatgpt also routes start/restart/down, and restart/upgrade adopt the
#     running proxy's dialect so a chatgpt-only install can restart at all
#   - an install with no chatgpt config skips the reconciliation probes entirely
#   - the subcommand is wired into main()'s dispatch and cmd_help
#
# Prints "CHATGPT TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " chatgpt backend (CLI) unit test"
echo "============================================================"

fail() { echo "[chatgpt-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[chatgpt-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

ALLOWLIST="$TMP_ROOT/.harness-allowlist"
: >"$ALLOWLIST"

cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://api.example.com
PROXY_API_KEY=sk-openai-key
DEFAULT_MODEL_NAME=openai-model
CHATGPT_BASE_URL=https://chat.example.com
CHATGPT_MODEL_NAME=gpt-5.6-terra
CHATGPT_COOKIE_STRING=session=abcdefghijklmnop
EOF

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
    source "$HARNESS" 2>/dev/null

# Sourcing resolves the script's self-path to THIS test file, so clone_dir would
# point at tests/. Pin it at the repo the way a real install has it, so
# host_proxy_fingerprint finds proxy/requirements.txt and the allowlist sync
# finds scripts/lib/net_helpers.sh.
clone_dir="$REPO_ROOT"

for fn in _backend_is_chatgpt _effective_default_model cmd_chatgpt \
          _running_proxy_backend _running_proxy_fp _chatgpt_config_fingerprint \
          write_runtime_override require_runtime_config \
          host_require_config host_proxy_fingerprint ensure_services_up; do
    [[ "$(type -t "$fn")" == "function" ]] || fail "$fn not sourced"
done
ok "chatgpt backend functions sourced"

# --- T1: the backend predicate and the per-backend model var ----------------
backend_override=""
_backend_is_chatgpt && fail "T1: default invocation must not be the chatgpt backend"
[[ "$(_effective_default_model)" == "openai-model" ]] \
    || fail "T1: default backend must use DEFAULT_MODEL_NAME"
backend_override="chatgpt"
_backend_is_chatgpt || fail "T1: backend_override=chatgpt not detected"
[[ "$(_effective_default_model)" == "gpt-5.6-terra" ]] \
    || fail "T1: chatgpt backend must use CHATGPT_MODEL_NAME"
backend_override=""
ok "T1: _backend_is_chatgpt / _effective_default_model split the two backends"

# --- T2: write_runtime_override injects PROXY_BACKEND -----------------------
backend_override="chatgpt"; prompt_mode_override=""
write_runtime_override
[[ -f "$runtime_override" ]] || fail "T2: override file not written"
grep -q 'PROXY_BACKEND: "chatgpt"' "$runtime_override" \
    || fail "T2: PROXY_BACKEND missing — $(cat "$runtime_override")"
grep -q '^  proxy:' "$runtime_override" || fail "T2: no proxy service block"
grep -q "HARNESS_PROXY_FP: \"$(_chatgpt_config_fingerprint)\"" "$runtime_override" \
    || fail "T2: HARNESS_PROXY_FP missing — $(cat "$runtime_override")"
# The override file lands on disk under state/; only the hash may appear in it.
grep -q "$CHATGPT_COOKIE_STRING" "$runtime_override" \
    && fail "T2: the cookie leaked into the compose override file"
ok "T2: backend_override emits PROXY_BACKEND and the config fingerprint"

# --- T3: backend + prompt-mode share ONE proxy: mapping ---------------------
# Duplicate top-level service keys are invalid compose YAML, so both ephemeral
# proxy settings must land in the same block.
prompt_mode_override="user_front"
write_runtime_override
[[ "$(grep -c '^  proxy:' "$runtime_override")" == "1" ]] \
    || fail "T3: duplicate proxy: block — $(cat "$runtime_override")"
grep -q 'PROXY_PROMPT_MODE: "user_front"' "$runtime_override" || fail "T3: prompt mode dropped"
grep -q 'PROXY_BACKEND: "chatgpt"' "$runtime_override" || fail "T3: backend dropped"
ok "T3: backend and prompt-mode share a single proxy: mapping"

# --- T4: no override active still removes the file --------------------------
backend_override=""; prompt_mode_override=""
write_runtime_override
[[ ! -f "$runtime_override" ]] || fail "T4: override file survived an empty body"
ok "T4: a launch with neither override leaves no compose override behind"

# --- T5: required-var set swaps to the CHATGPT_* trio -----------------------
# require_runtime_config exits rather than returning, so probe it in subshells.
env_file="$TMP_ROOT/.env"; allowlist_path="$ALLOWLIST"
( backend_override="chatgpt"
  PROXY_API_URL=""; PROXY_API_KEY=""; DEFAULT_MODEL_NAME=""
  require_runtime_config ) >/dev/null 2>&1 \
    || fail "T5: chatgpt backend must not require the PROXY_API_* trio"
( backend_override="chatgpt"; CHATGPT_COOKIE_STRING=""
  require_runtime_config ) >/dev/null 2>&1 \
    && fail "T5: a missing CHATGPT_COOKIE_STRING must still abort"
( backend_override=""; CHATGPT_BASE_URL=""; CHATGPT_MODEL_NAME=""; CHATGPT_COOKIE_STRING=""
  require_runtime_config ) >/dev/null 2>&1 \
    || fail "T5: default backend must not require the CHATGPT_* trio"
ok "T5: require_runtime_config swaps required vars per backend"

# --- T6: host mode's lighter sibling does the same --------------------------
( backend_override="chatgpt"
  PROXY_API_URL=""; PROXY_API_KEY=""; DEFAULT_MODEL_NAME=""
  host_require_config ) >/dev/null 2>&1 \
    || fail "T6: host chatgpt must not require the PROXY_API_* trio"
( backend_override="chatgpt"; CHATGPT_BASE_URL=""
  host_require_config ) >/dev/null 2>&1 \
    && fail "T6: a missing CHATGPT_BASE_URL must still abort host mode"
ok "T6: host_require_config swaps required vars per backend"

# --- T7: the auth probe and catalog pull are skipped for chatgpt ------------
# Both are bearer-key/OpenAI-catalog specific. The chatgpt backend has no
# bearer key and no /v1/models catalog, so reaching either one is the bug.
_probe_upstream_auth() { return 1; }
( backend_override="chatgpt"; HARNESS_SKIP_AUTH_PROBE=0
  _gate_on_upstream_auth ) >/dev/null 2>&1 \
    || fail "T7: _gate_on_upstream_auth must no-op for chatgpt"
# _print_upstream_models swallows curl failures with `|| true`, so a non-zero
# return can't detect a regression. Trip a marker instead: the early return is
# what must stop it before the catalog request goes out.
CURL_MARKER="$TMP_ROOT/t7-curl-was-called"
rm -f "$CURL_MARKER"
curl() { : >"$CURL_MARKER"; return 1; }
( backend_override="chatgpt"; HARNESS_SKIP_AUTH_PROBE=0
  _print_upstream_models ) >/dev/null 2>&1 || true
[[ ! -e "$CURL_MARKER" ]] || fail "T7: _print_upstream_models hit the catalog on the chatgpt backend"
# Control: with the openai backend and a URL+key set, it DOES reach curl, so
# the assertion above is testing the guard and not a dead code path.
( backend_override=""; HARNESS_SKIP_AUTH_PROBE=0
  PROXY_API_URL="https://api.example.com"; PROXY_API_KEY="sk-test"
  _print_upstream_models ) >/dev/null 2>&1 || true
[[ -e "$CURL_MARKER" ]] || fail "T7: the openai backend must still pull the catalog"
rm -f "$CURL_MARKER"
unset -f curl _probe_upstream_auth
ok "T7: chatgpt skips the bearer-key probe and the model-catalog pull"

# --- T8: the host fingerprint covers backend + all three CHATGPT_* ----------
backend_override=""
base_fp="$(host_proxy_fingerprint)"
backend_override="chatgpt"
cg_fp="$(host_proxy_fingerprint)"
[[ "$base_fp" != "$cg_fp" ]] \
    || fail "T8: backend switch left the fingerprint unchanged (stale proxy reuse)"
for v in CHATGPT_BASE_URL CHATGPT_MODEL_NAME CHATGPT_COOKIE_STRING; do
    prev="${!v}"
    printf -v "$v" '%s' "changed-value"
    [[ "$(host_proxy_fingerprint)" != "$cg_fp" ]] || fail "T8: $v not in the fingerprint"
    printf -v "$v" '%s' "$prev"
done
[[ "$(host_proxy_fingerprint)" == "$cg_fp" ]] || fail "T8: fingerprint not restored"
backend_override=""
ok "T8: host_proxy_fingerprint tracks the backend and every CHATGPT_* value"

# --- T9: _running_proxy_backend reads the container env ---------------------
compose() { echo "proxycid123"; }
harness_docker() { printf 'PATH=/usr/bin\nPROXY_BACKEND=chatgpt\nPROXY_PORT=8000\n'; }
[[ "$(_running_proxy_backend)" == "chatgpt" ]] || fail "T9: backend not read from container env"
harness_docker() { printf 'PATH=/usr/bin\nPROXY_PORT=8000\n'; }
[[ "$(_running_proxy_backend)" == "openai" ]] \
    || fail "T9: a proxy with no PROXY_BACKEND must report the openai default"
[[ -z "$(_running_proxy_fp)" ]] || fail "T9: a proxy with no HARNESS_PROXY_FP must report nothing"
harness_docker() { printf 'PATH=/usr/bin\nHARNESS_PROXY_FP=deadbeefcafe0001\nPROXY_PORT=8000\n'; }
[[ "$(_running_proxy_fp)" == "deadbeefcafe0001" ]] \
    || fail "T9: fingerprint not read from container env"
# The cookie sits next to the fingerprint in the real container env; the reader
# must not surface it.
harness_docker() { printf 'CHATGPT_COOKIE_STRING=leakme\nHARNESS_PROXY_FP=deadbeefcafe0001\n'; }
_running_proxy_fp | grep -q leakme && fail "T9: the cookie leaked out of the env reader"
compose() { echo ""; }
_running_proxy_backend >/dev/null 2>&1 && fail "T9: no proxy must be reported as not running"
ok "T9: _running_proxy_backend reports the running container's dialect"

# --- T10: ensure_services_up restarts on a backend mismatch -----------------
# The proxy serves one dialect at a time and ensure_services_up is otherwise a
# no-op when it is already up, so this is what stops `harness chatgpt` from
# silently answering from the previous backend.
START_LOG="$TMP_ROOT/start.log"
services_up() { return 0; }
host_mcp_start_enabled() { :; }
cmd_start() { echo "started" >>"$START_LOG"; }

: >"$START_LOG"
_running_proxy_backend() { printf 'openai'; }
backend_override="" ; ensure_services_up >/dev/null
[[ ! -s "$START_LOG" ]] || fail "T10: matching backend must not restart the stack"

: >"$START_LOG"
backend_override="chatgpt"; ensure_services_up >/dev/null
[[ -s "$START_LOG" ]] || fail "T10: mismatched backend must restart the stack"

: >"$START_LOG"
_running_proxy_backend() { printf 'chatgpt'; }
_running_proxy_fp() { _chatgpt_config_fingerprint; }
backend_override="chatgpt"; ensure_services_up >/dev/null
[[ ! -s "$START_LOG" ]] || fail "T10: matching chatgpt backend must not restart"

# Rotating an expired cookie is the routine maintenance action for this
# backend, and compose won't replace a running container over an .env edit.
: >"$START_LOG"
_running_proxy_fp() { printf 'staleeeeeeeeeeee'; }
backend_override="chatgpt"; ensure_services_up >/dev/null
[[ -s "$START_LOG" ]] || fail "T10: a rotated cookie must restart the proxy"

# The openai backend carries no fingerprint; it must not restart on its absence.
: >"$START_LOG"
_running_proxy_backend() { printf 'openai'; }
_running_proxy_fp() { printf ''; }
backend_override=""; ensure_services_up >/dev/null
[[ ! -s "$START_LOG" ]] || fail "T10: the openai backend must not restart on a missing fingerprint"
backend_override=""
ok "T10: ensure_services_up restarts on a changed backend or changed chatgpt credentials"

# --- T11: the cookie is a secret and never printed in the clear -------------
_config_is_secret CHATGPT_COOKIE_STRING || fail "T11: cookie not classified secret"
out="$(_config_get CHATGPT_COOKIE_STRING)"
grep -q 'set,' <<<"$out" || fail "T11: cookie not masked — $out"
grep -q 'abcdefghijklmnop' <<<"$out" && fail "T11: cookie value leaked — $out"
_config_editable_keys | grep -q '^CHATGPT_BASE_URL|' || fail "T11: CHATGPT_BASE_URL not in the picker"
_config_editable_keys | grep -q '^CHATGPT_MODEL_NAME|' || fail "T11: CHATGPT_MODEL_NAME not in the picker"
_config_editable_keys | grep -q '^CHATGPT_COOKIE_STRING|' || fail "T11: CHATGPT_COOKIE_STRING not in the picker"
ok "T11: the cookie is masked and all three keys are in the config picker"

# --- T12: setting CHATGPT_BASE_URL syncs the egress allowlist ---------------
: >"$ALLOWLIST"
_config_set CHATGPT_BASE_URL "https://chat.newhost.example:8443" >/dev/null \
    || fail "T12: config set returned non-zero"
[[ "$(_config_read_key CHATGPT_BASE_URL)" == "https://chat.newhost.example:8443" ]] \
    || fail "T12: URL not written"
grep -q '^chat.newhost.example' "$ALLOWLIST" \
    || fail "T12: host not added to the allowlist — $(cat "$ALLOWLIST")"
ok "T12: 'config set CHATGPT_BASE_URL' allowlists the host for the firewall"

# --- T13: cmd_chatgpt --help is side-effect free ----------------------------
backend_override=""
help_out="$(cmd_chatgpt --help)" || fail "T13: --help returned non-zero"
grep -q 'harness chatgpt host' <<<"$help_out" || fail "T13: help does not document the host form"
grep -q 'CHATGPT_COOKIE_STRING' <<<"$help_out" || fail "T13: help does not document the config keys"
[[ -z "$backend_override" ]] || fail "T13: --help must not select the backend"
ok "T13: cmd_chatgpt --help documents both forms without side effects"

# --- T14: dispatch — bare launches an agent, 'host' delegates to cmd_host ---
# Called WITHOUT command substitution: cmd_chatgpt sets globals, and a subshell
# would discard exactly the state under test. The stubs log to a file instead.
DISPATCH_LOG="$TMP_ROOT/dispatch.log"
run_agent()  { echo "run_agent:$*" >>"$DISPATCH_LOG"; }
cmd_host()   { echo "cmd_host:$*"  >>"$DISPATCH_LOG"; }

backend_override=""; agent_model=""; : >"$DISPATCH_LOG"
cmd_chatgpt --yolo
[[ "$(cat "$DISPATCH_LOG")" == "run_agent:opencode --yolo" ]] \
    || fail "T14: bare form did not launch an agent — $(cat "$DISPATCH_LOG")"
[[ "$backend_override" == "chatgpt" ]] || fail "T14: backend not selected"
[[ "$agent_model" == "gpt-5.6-terra" ]] || fail "T14: agent_model not set from CHATGPT_MODEL_NAME"

backend_override=""; : >"$DISPATCH_LOG"
cmd_chatgpt host -p 'hi'
[[ "$(cat "$DISPATCH_LOG")" == "cmd_host:-p hi" ]] \
    || fail "T14: host form did not delegate to cmd_host — $(cat "$DISPATCH_LOG")"
[[ "$backend_override" == "chatgpt" ]] || fail "T14: backend not selected for the host form"

# doctor/preflight must run with the backend selected, or they check the other
# backend's vars and report a valid chatgpt-only install as broken.
real_cmd_preflight="$(declare -f cmd_preflight)"
cmd_doctor()    { echo "cmd_doctor:$*"    >>"$DISPATCH_LOG"; }
cmd_preflight() { echo "cmd_preflight:$*" >>"$DISPATCH_LOG"; }
backend_override=""; : >"$DISPATCH_LOG"
cmd_chatgpt doctor
[[ "$(cat "$DISPATCH_LOG")" == "cmd_doctor:" ]] \
    || fail "T14: doctor did not delegate to cmd_doctor — $(cat "$DISPATCH_LOG")"
[[ "$backend_override" == "chatgpt" ]] || fail "T14: backend not selected for doctor"
backend_override=""; : >"$DISPATCH_LOG"
cmd_chatgpt preflight
[[ "$(cat "$DISPATCH_LOG")" == "cmd_preflight:" ]] \
    || fail "T14: preflight did not delegate to cmd_preflight — $(cat "$DISPATCH_LOG")"
[[ "$backend_override" == "chatgpt" ]] || fail "T14: backend not selected for preflight"

# Stack lifecycle. Without these labels the words fall through to `*)` and
# launch an agent with a stray argument, and `harness start` on a chatgpt-only
# install aborts in require_runtime_config on the openai trio.
# Saved, not unset: T19 exercises the real cmd_restart, and `unset -f` on a
# stub would take the real definition with it.
real_cmd_restart="$(declare -f cmd_restart)"
real_cmd_down="$(declare -f cmd_down)"
cmd_start()   { echo "cmd_start:$*"   >>"$DISPATCH_LOG"; }
cmd_restart() { echo "cmd_restart:$*" >>"$DISPATCH_LOG"; }
cmd_down()    { echo "cmd_down:$*"    >>"$DISPATCH_LOG"; }
for verb in start restart down; do
    backend_override=""; : >"$DISPATCH_LOG"
    cmd_chatgpt "$verb"
    [[ "$(cat "$DISPATCH_LOG")" == "cmd_${verb}:" ]] \
        || fail "T14: '$verb' did not delegate to cmd_$verb — $(cat "$DISPATCH_LOG")"
    [[ "$backend_override" == "chatgpt" ]] || fail "T14: backend not selected for $verb"
done
unset -f cmd_doctor
eval "$real_cmd_preflight"; eval "$real_cmd_restart"; eval "$real_cmd_down"
ok "T14: cmd_chatgpt dispatches every form with the backend selected"

# --- T15: wired into main()'s dispatch and the help text --------------------
grep -qE '^ *chatgpt\)  *cmd_chatgpt "\$@" ;;' "$HARNESS" \
    || fail "T15: 'chatgpt' missing from main()'s command dispatch"
grep -q 'chatgpt \[host\] \[args\]' "$HARNESS" || fail "T15: chatgpt missing from cmd_help"
ok "T15: 'harness chatgpt' is dispatched and documented in 'harness help'"

# --- T16: a real cookie survives the .env write AND re-sourcing -------------
# Cookies carry '; ' separators. harness sources .env under `set -euo
# pipefail`, so an unquoted value would truncate at the first ';' and then run
# "oai-did=..." as a command, killing every harness invocation.
cookie='__Secure-next-auth.session-token=eyJhbGci.OiJk-x_y; oai-did=9f2b-4c; _puid=abc%3D'
_config_write_key CHATGPT_COOKIE_STRING "$cookie" || fail "T16: write returned non-zero"
grep -q "^CHATGPT_COOKIE_STRING='" "$TMP_ROOT/.env" || fail "T16: cookie not quoted on disk"
[[ "$(_config_read_key CHATGPT_COOKIE_STRING)" == "$cookie" ]] \
    || fail "T16: cookie mangled: got $(_config_read_key CHATGPT_COOKIE_STRING)"
sourced=$(bash -c 'set -euo pipefail; set -a; . "$1"; set +a; printf %s "$CHATGPT_COOKIE_STRING"' \
    _ "$TMP_ROOT/.env") || fail "T16: sourcing the rewritten .env aborted"
[[ "$sourced" == "$cookie" ]] || fail "T16: sourced cookie differs: $sourced"
# Bare words stay bare — no churn for the keys that never needed quoting.
_config_write_key CHATGPT_MODEL_NAME gpt-5.6-terra
grep -q '^CHATGPT_MODEL_NAME=gpt-5.6-terra$' "$TMP_ROOT/.env" \
    || fail "T16: a bare value was needlessly quoted"
ok "T16: a semicolon-bearing cookie round-trips and .env stays sourceable"

# --- T17: the double-quote fallback is exactly inverted on read -------------
# A value containing a single quote can't use the single-quote form, so the
# writer falls back to double quotes + escapes and _config_read_key has to be
# the exact inverse. This is the path that silently corrupted PROXY_API_KEY
# when only the writer had been updated.
tricky='it'"'"'s $HOME `x` "q" \\ done'
_config_write_key CHATGPT_COOKIE_STRING "$tricky" || fail "T17: write returned non-zero"
grep -q '^CHATGPT_COOKIE_STRING="' "$TMP_ROOT/.env" || fail "T17: expected the double-quote form"
[[ "$(_config_read_key CHATGPT_COOKIE_STRING)" == "$tricky" ]] \
    || fail "T17: read is not the inverse of write: got $(_config_read_key CHATGPT_COOKIE_STRING)"
sourced=$(bash -c 'set -euo pipefail; set -a; . "$1"; set +a; printf %s "$CHATGPT_COOKIE_STRING"' \
    _ "$TMP_ROOT/.env") || fail "T17: sourcing the rewritten .env aborted"
[[ "$sourced" == "$tricky" ]] || fail "T17: sourced value differs: $sourced"
_config_write_key CHATGPT_COOKIE_STRING "$cookie"
ok "T17: a quote/dollar/backtick-bearing value round-trips through the escaped form"

# --- T18: a pure-openai install pays nothing for the chatgpt reconciliation --
# _running_proxy_backend/_running_proxy_fp each cost a `compose ps` plus a
# `docker inspect`, and `compose` regenerates the runtime override every call.
# An install that never configured CHATGPT_BASE_URL can never have started a
# chatgpt proxy, so that whole block must be skipped — this is what keeps a
# pre-existing openai launch exactly as cheap as it was before this backend.
PROBE_LOG="$TMP_ROOT/probe.log"
services_up() { return 0; }
host_mcp_start_enabled() { :; }
cmd_start() { echo "started" >>"$START_LOG"; }
_running_proxy_backend() { echo "backend" >>"$PROBE_LOG"; printf 'openai'; }
_running_proxy_fp()      { echo "fp"      >>"$PROBE_LOG"; printf ''; }

: >"$PROBE_LOG"; : >"$START_LOG"
saved_base="$CHATGPT_BASE_URL"; CHATGPT_BASE_URL=""
backend_override=""; ensure_services_up >/dev/null
[[ ! -s "$PROBE_LOG" ]] || fail "T18: an openai-only install must not probe the running proxy"
[[ ! -s "$START_LOG" ]] || fail "T18: an openai-only install must not restart"

# ... but once the backend is configured, or this launch selects it, the
# reconciliation is back on.
: >"$PROBE_LOG"
CHATGPT_BASE_URL="$saved_base"
backend_override=""; ensure_services_up >/dev/null
[[ -s "$PROBE_LOG" ]] || fail "T18: a chatgpt-configured install must still reconcile"

: >"$PROBE_LOG"; CHATGPT_BASE_URL=""
backend_override="chatgpt"; ensure_services_up >/dev/null
[[ -s "$PROBE_LOG" ]] || fail "T18: an explicit chatgpt launch must reconcile regardless"
CHATGPT_BASE_URL="$saved_base"; backend_override=""
ok "T18: reconciliation is skipped entirely on an install with no chatgpt config"

# --- T19: restart/upgrade come back up on the running backend ---------------
# cmd_restart is down + start. Without adopting the running dialect first, a
# chatgpt-only install gets its stack torn down and then cmd_start dies in
# require_runtime_config naming the three openai keys it never set.
backend_override=""; agent_model=""
_running_proxy_backend() { printf 'chatgpt'; }
_adopt_running_backend
[[ "$backend_override" == "chatgpt" ]] || fail "T19: a running chatgpt proxy was not adopted"
[[ "$agent_model" == "gpt-5.6-terra" ]] || fail "T19: agent_model not refreshed on adopt"

# openai is left as the empty default on purpose: an explicit PROXY_BACKEND
# would change write_runtime_override's output for every pre-existing user.
backend_override=""; _running_proxy_backend() { printf 'openai'; }
_adopt_running_backend
[[ -z "$backend_override" ]] || fail "T19: openai must stay the implicit default"

# An install with no chatgpt config never probes at all.
: >"$PROBE_LOG"
_running_proxy_backend() { echo "backend" >>"$PROBE_LOG"; printf 'chatgpt'; }
backend_override=""; CHATGPT_BASE_URL=""
_adopt_running_backend
[[ ! -s "$PROBE_LOG" ]] || fail "T19: an openai-only install must not probe on adopt"
CHATGPT_BASE_URL="$saved_base"

# An explicit selection is never overwritten.
backend_override="chatgpt"; _running_proxy_backend() { printf 'openai'; }
_adopt_running_backend
[[ "$backend_override" == "chatgpt" ]] || fail "T19: an explicit selection was clobbered"
backend_override=""

# Order matters: the read has to happen while the container still exists.
ORDER_LOG="$TMP_ROOT/order.log"
harness_runtime_installed() { return 0; }
require_docker() { :; }
_running_proxy_backend() { echo "read" >>"$ORDER_LOG"; printf 'chatgpt'; }
cmd_down()  { echo "down"  >>"$ORDER_LOG"; }
cmd_start() { echo "start:${backend_override}" >>"$ORDER_LOG"; }
: >"$ORDER_LOG"; backend_override=""
cmd_restart
[[ "$(cat "$ORDER_LOG")" == "read
down
start:chatgpt" ]] || fail "T19: cmd_restart order/backend wrong — $(cat "$ORDER_LOG")"
backend_override=""
unset -f harness_runtime_installed require_docker cmd_down cmd_start
ok "T19: restart adopts the running dialect before tearing the stack down"

# --- T20: bare diagnostics point at the other entry point -------------------
# A chatgpt-only install run through bare `harness preflight` fails all three
# openai keys. Without a pointer that reads as "your install is broken".
saved_env="$(cat "$TMP_ROOT/.env")"
cat >"$TMP_ROOT/.env" <<'EOF'
CHATGPT_BASE_URL=https://chat.example.com
CHATGPT_MODEL_NAME=gpt-5.6-terra
CHATGPT_COOKIE_STRING=session=abcdefghijklmnop
EOF
backend_override=""
pre_out=$( (host_only=1; cmd_preflight) 2>&1 || true )
grep -q "harness chatgpt preflight" <<<"$pre_out" \
    || fail "T20: bare preflight gave no pointer to the chatgpt backend — $pre_out"
# ... and the pointer must NOT appear when the openai keys are the ones in use.
printf '%s\n' "$saved_env" >"$TMP_ROOT/.env"
pre_out=$( (host_only=1; cmd_preflight) 2>&1 || true )
grep -q "harness chatgpt preflight" <<<"$pre_out" \
    && fail "T20: the pointer must not fire on a configured openai install"
ok "T20: bare preflight points a chatgpt-only install at 'harness chatgpt preflight'"

echo
echo "CHATGPT TEST PASSED"
