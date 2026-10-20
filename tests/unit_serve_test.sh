#!/usr/bin/env bash
#
# tests/unit_serve_test.sh — `harness serve` without docker or network:
#   - serve/test_pub.py   the pub page server (index, Host check, sandboxed
#                         /pub/ pages, no path escape, no-vault mode)
#   - cmd_serve           rejects bad options before it starts anything
#   - serve_password_file makes the opencode web password once, mode 600
#   - serve_opencode_config the prompt-mode model aliases
#   - the upstream key off curl's argv and out of opencode's env
#
# Prints "SERVE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " serve unit test"
echo "============================================================"

fail() { echo "[serve-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[serve-test] OK: $*"; }

PY=$(command -v python3 || command -v python) || fail "this test needs Python 3"

# --- S1: the pub server ------------------------------------------------------
( cd "$REPO_ROOT/serve" && "$PY" -m unittest -q test_pub ) || fail "S1: serve/test_pub.py failed"
ok "S1: pub server tests"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null
# Sourced from a test, clone_dir resolves off $0 and platform.sh is skipped;
# point both at the repo.
# shellcheck disable=SC2034  # read by the sourced harness functions
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"

# --- S2: bad options fail before anything starts -----------------------------
for args in "--port" "--port abc" "--port 0" "--port 65535" "--bogus"; do
    # shellcheck disable=SC2086
    if ( cmd_serve $args ) >/dev/null 2>&1; then fail "S2: 'serve $args' was accepted"; fi
done
( cmd_serve --help ) | grep -q 'harness serve \[--port N\]' || fail "S2: --help should print usage"
ok "S2: bad options rejected, --help works"

# --- S3: the password file is made once, private -----------------------------
f=$(serve_password_file) || fail "S3: serve_password_file failed"
[[ -s "$f" ]] || fail "S3: password file is empty"
first=$(cat "$f")
(( ${#first} >= 24 )) || fail "S3: password is too short"
perms=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
[[ "$perms" == "600" ]] || fail "S3: password file mode is $perms, want 600"
f2=$(serve_password_file)
[[ "$f2" == "$f" && "$(cat "$f2")" == "$first" ]] || fail "S3: a second call must reuse the password"
ok "S3: password made once, mode 600"

# --- S4: prompt modes as model aliases (serve_opencode_config) ---------------
mkdir -p "$(dirname "$(host_opencode_config)")"
cat >"$(host_opencode_config)" <<'JSON'
{"provider":{"harness":{"models":{
  "nat-withtools":{"name":"nat-withtools","limit":{"context":1000,"output":8192}},
  "coop":{"name":"coop","limit":{"context":1000,"output":8192}}}}},
 "model":"harness/nat-withtools"}
JSON
unset PROXY_NATIVE_TOOL_MODELS prompt_mode_override harness_serve harness_thoughts
harness_slim=1
[[ -z "$(serve_opencode_config)" ]] || fail "S4: no serve overlay outside serve"
harness_serve=1
out=$(serve_opencode_config)
[[ "$(jq -r .share <<<"$out")" == disabled ]] || fail "S4: serve must disable sharing"
keys=$(jq -c '.provider.harness.models | keys' <<<"$out")
[[ "$keys" == '["nat-withtools","nat-withtools:slim+thoughts","nat-withtools:stock","nat-withtools:thoughts"]' ]] \
    || fail "S4: native model aliases wrong (cooperative-route models get none): $keys"
[[ "$(jq -c '.provider.harness.models["nat-withtools"].headers' <<<"$out")" == '{"x-harness-slim":"1"}' ]] \
    || fail "S4: the model's own entry should carry the launch mode (slim)"
[[ "$(jq -c '.provider.harness.models["nat-withtools:stock"] | [.id, .headers, .limit.context]' <<<"$out")" == '["nat-withtools",{},1000]' ]] \
    || fail "S4: the stock alias should keep the upstream id and limits, with no harness header"
[[ "$(jq -r .agent.stock.model <<<"$out")" == "harness/nat-withtools:stock" ]] || fail "S4: stock agent should start on the stock alias"
[[ -z "$(thoughts_opencode_config)" ]] || fail "S4: serve with slim sets no provider-wide thoughts header"
harness_thoughts=1
out=$(serve_opencode_config)
[[ "$(jq -c '.provider.harness.models["nat-withtools"].headers' <<<"$out")" == '{"x-harness-slim":"1","x-harness-thoughts":"1"}' ]] \
    || fail "S4: with --thoughts the model's own entry is slim+thoughts"
jq -e '.provider.harness.models["nat-withtools:slim"]' <<<"$out" >/dev/null || fail "S4: with --thoughts a plain slim alias exists"
PROXY_NATIVE_TOOL_MODELS=off
[[ "$(serve_opencode_config)" == '{"share":"disabled"}' ]] || fail "S4: native route off means no aliases"
unset PROXY_NATIVE_TOOL_MODELS
harness_slim=0
[[ "$(serve_opencode_config)" == '{"share":"disabled"}' ]] || fail "S4: --normal means no aliases (stock opencode)"
[[ -n "$(thoughts_opencode_config)" ]] || fail "S4: --normal --thoughts keeps the provider-wide header"
harness_slim=1; unset harness_serve harness_thoughts
ok "S4: prompt-mode aliases only for native-route models, sharing off"

# --- S5: the key stays off curl's argv and out of opencode's env -------------
cfg=$(_curl_auth_config 'k"e\y')
[[ "$cfg" == 'header = "Authorization: Bearer k\"e\\y"' ]] || fail "S5: curl config not escaped: $cfg"
(
    PROXY_API_KEY=secret OAI_API_KEY=secret CHATGPT_COOKIE_STRING=secret
    export PROXY_API_KEY OAI_API_KEY CHATGPT_COOKIE_STRING
    host_opencode_env >/dev/null 2>&1 || true
    env | grep -qE '^(PROXY_API_KEY|OAI_API_KEY|CHATGPT_COOKIE_STRING)=' && exit 1
    exit 0
) || fail "S5: host_opencode_env must drop the upstream secrets"
if grep -nE 'curl[^|]*-H "Authorization: Bearer' "$HARNESS"; then
    fail "S5: a curl call puts the key on its command line; pipe _curl_auth_config to curl -K - instead"
fi
ok "S5: key via curl -K stdin, scrubbed from opencode's env"

echo "SERVE TEST PASSED"
