#!/usr/bin/env bash
#
# tests/unit_agent_oai_test.sh — container-mode OAI wiring and the firewall's
# upstream auto-allow, docker-free.
#
# Covers:
#   - agent_common_env passes OAI_MODELS (names only, never OAI_API_KEY) and
#     the oai overlay (slim header on harness-oai) when an OAI upstream is set,
#     and neither when it is not
#   - agents/entrypoint.sh ensure_opencode_config (extracted, curl stubbed)
#     puts OAI_MODELS on the harness-oai provider (@ai-sdk/openai, the proxy's
#     /v1 baseURL), keeps the rest on harness, and selects harness-oai/<id>
#     for an OAI default; with no OAI_MODELS it writes harness alone
#   - firewall/init-firewall.sh section 1b (extracted) adds the hosts of
#     PROXY_API_URL and OAI_API_URL, or CHATGPT_BASE_URL alone under
#     PROXY_BACKEND=chatgpt, skips intra-cluster names, and aborts on a
#     scheme-less URL
#
# Prints "AGENT OAI TEST PASSED" on success.

# The variables this test sets are read by the sourced and extracted functions.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"
ENTRYPOINT="${REPO_ROOT}/agents/entrypoint.sh"
FIREWALL="${REPO_ROOT}/firewall/init-firewall.sh"

echo "============================================================"
echo " container OAI provider + firewall upstream auto-allow unit test"
echo "============================================================"

fail() { echo "[agent-oai-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[agent-oai-test] OK: $*"; }

command -v jq >/dev/null 2>&1 || fail "this test needs host jq"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# --- T1: agent_common_env passes OAI names and the oai overlay --------------
# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null
[[ "$(type -t agent_common_env)" == "function" ]] || fail "agent_common_env not sourced"
clone_dir="$REPO_ROOT"

env_out=$( PROXY_BACKEND=""; OAI_API_URL="https://oai.example/v1"; OAI_API_KEY="sk-oai-secret";
           OAI_MODELS=" gpt-a ,gpt-b,"; harness_slim=1; harness_vault_dir="";
           agent_common_env opencode 0 )
grep -qx 'OAI_MODELS=gpt-a,gpt-b' <<<"$env_out" || fail "T1: OAI_MODELS not passed trimmed — $env_out"
grep -q 'sk-oai-secret' <<<"$env_out" && fail "T1: the OAI key leaked into the agent env"
overlay=$(sed -n 's/^OPENCODE_CONFIG_CONTENT=//p' <<<"$env_out")
jq -e '.provider["harness-oai"].options.headers["x-harness-slim"] == "1"' <<<"$overlay" >/dev/null 2>&1 \
    || fail "T1: the container overlay must carry the slim header for harness-oai — $overlay"
env_out=$( PROXY_BACKEND=""; OAI_API_URL=""; OAI_API_KEY=""; OAI_MODELS="";
           harness_slim=1; harness_vault_dir=""; agent_common_env opencode 0 )
grep -q '^OAI_MODELS=' <<<"$env_out" && fail "T1: OAI_MODELS passed without an OAI upstream"
overlay=$(sed -n 's/^OPENCODE_CONFIG_CONTENT=//p' <<<"$env_out")
[[ "$(jq -c '.provider | keys' <<<"$overlay")" == '["harness"]' ]] \
    || fail "T1: without OAI the overlay must not name harness-oai — $overlay"
ok "T1: agent_common_env passes OAI_MODELS (names only) and the oai overlay"

# --- T2: the entrypoint builds the harness-oai provider ----------------------
fn=$(sed -n '/^ensure_opencode_config() {/,/^}/p' "$ENTRYPOINT")
[[ -n "$fn" ]] || fail "T2: ensure_opencode_config not found in $ENTRYPOINT"
cfg="$TMP_ROOT/home/.config/opencode/opencode.json"
run_entry() {
    (
        # The proxy's /v1/models lists vendor and OAI ids alike.
        curl() { printf '%s' '{"object":"list","data":[{"id":"vendor-m"},{"id":"gpt-a"},{"id":"gpt-b"}]}'; }
        HOME="$TMP_ROOT/home"; PROXY_PORT=8000; MODEL_CONTEXT_LENGTH=100000
        DEFAULT_MODEL_NAME="$1"; OAI_MODELS="$2"
        eval "$fn"
        ensure_opencode_config
    )
}
run_entry gpt-b "gpt-a, gpt-b" || fail "T2: ensure_opencode_config returned non-zero"
jq -e '(.provider.harness.models | keys) == ["vendor-m"]
       and .provider["harness-oai"].npm == "@ai-sdk/openai"
       and .provider["harness-oai"].options.baseURL == "http://proxy:8000/v1"
       and .provider["harness-oai"].options.apiKey == "harness-dummy"
       and (.provider["harness-oai"].models | keys) == ["gpt-a", "gpt-b"]
       and .provider["harness-oai"].models["gpt-a"].reasoning == true
       and .provider["harness-oai"].models["gpt-a"].limit == {context: 100000, output: 32000}
       and .model == "harness-oai/gpt-b" and .small_model == "harness-oai/gpt-b"' "$cfg" >/dev/null 2>&1 \
    || fail "T2: vendor + OAI config has the wrong shape — $(cat "$cfg")"
run_entry vendor-m "" || fail "T2: ensure_opencode_config returned non-zero"
jq -e '(.provider | keys) == ["harness"]
       and (.provider.harness.models | keys) == ["gpt-a", "gpt-b", "vendor-m"]
       and .model == "harness/vendor-m"' "$cfg" >/dev/null 2>&1 \
    || fail "T2: without OAI_MODELS the config must be harness alone — $(cat "$cfg")"
ok "T2: entrypoint puts OAI models on harness-oai and selects harness-oai/<default>"

# --- T3: firewall section 1b allows the upstream hosts from the env ----------
sec=$(sed -n '/^# --- 1b\. upstream URL hosts/,/^if (( \${#ALLOW_HOSTS\[@\]} == 0 )); then/p' "$FIREWALL" | sed '$d')
[[ -n "$sec" ]] || fail "T3: section 1b not found in $FIREWALL"
fw_hosts() {
    (
        log() { :; }
        ALLOW_HOSTS=(github.com)
        eval "$sec"
        printf '%s\n' "${ALLOW_HOSTS[@]}"
    )
}
got=$(PROXY_BACKEND="" PROXY_API_URL="https://api.vendor.example:8443/v1/chat/completions" \
      OAI_API_URL="https://oai.example/openai/v1" CHATGPT_BASE_URL="https://chat.example" fw_hosts | paste -sd' ')
[[ "$got" == "github.com api.vendor.example oai.example" ]] || fail "T3: openai backend hosts — got '$got'"
got=$(PROXY_BACKEND=chatgpt PROXY_API_URL="https://api.vendor.example/v1" \
      OAI_API_URL="https://oai.example/v1" CHATGPT_BASE_URL="https://chat.example/backend-api" fw_hosts | paste -sd' ')
[[ "$got" == "github.com chat.example" ]] || fail "T3: chatgpt backend hosts — got '$got'"
got=$(PROXY_BACKEND="" PROXY_API_URL="http://mockupstream:9000/v1" OAI_API_URL="" fw_hosts | paste -sd' ')
[[ "$got" == "github.com" ]] || fail "T3: an intra-cluster host must be skipped — got '$got'"
got=$(PROXY_BACKEND="" PROXY_API_URL="https://10.0.0.5/v1" OAI_API_URL="" fw_hosts | paste -sd' ')
[[ "$got" == "github.com 10.0.0.5" ]] || fail "T3: an IPv4 upstream must be allowed — got '$got'"
if out=$(PROXY_BACKEND="" PROXY_API_URL="" OAI_API_URL="oai.example/v1" fw_hosts 2>&1); then
    fail "T3: a scheme-less OAI_API_URL must abort"
fi
grep -q "could not parse a hostname out of OAI_API_URL" <<<"$out" || fail "T3: parse FATAL should name the var — $out"
ok "T3: firewall allows the active upstream hosts from the env; aborts on a scheme-less URL"

echo
echo "AGENT OAI TEST PASSED"
