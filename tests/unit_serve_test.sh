#!/usr/bin/env bash
#
# tests/unit_serve_test.sh — `harness serve` without docker or network:
#   - serve/test_pub.py   the pub page server (index, Host check, sandboxed
#                         /pub/ pages, no path escape, no-vault mode)
#   - cmd_serve           rejects bad options before it starts anything
#   - serve_password_file makes the opencode web password once, mode 600
#   - serve_opencode_config the prompt-mode model aliases
#   - the upstream key off curl's argv and out of opencode's env
#   - cmd_serve           reaches its banner with no OAI configured
#   - serve/test_runs.py  the run viewer (/runs)
#   - agents/live-run.sh  headless runs that take messages (stub opencode)
#   - harness_view        the run viewer a headless launch brings up
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
( cd "$REPO_ROOT/serve" && "$PY" -m unittest -q test_runs ) || fail "S1: serve/test_runs.py failed"
ok "S1: run viewer tests (/runs)"

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
harness_slim=1; harness_serve=1
# The merged launch overlay: per-model headers only, and OAI keeps its slim header.
out=$(harness_opencode_overlay oai)
[[ "$(jq -c '.provider.harness.options.headers // {}' <<<"$out")" == '{}' ]] \
    || fail "S4: under serve the harness provider must carry no provider-wide mode header"
[[ "$(jq -r '.provider["harness-oai"].options.headers["x-harness-slim"]' <<<"$out")" == 1 ]] \
    || fail "S4: OAI models keep the slim header under serve"
jq -e '.provider.harness.models["nat-withtools:stock"] and .agent.stock and (.agent | has("build") | not) and .share == "disabled"' <<<"$out" >/dev/null \
    || fail "S4: the merged overlay lost the aliases, the stock agent or share (and sets no agent prompts)"
# The variable is trimmed first, as the proxy does.
PROXY_NATIVE_TOOL_MODELS='  '
jq -e '.provider.harness.models["nat-withtools:stock"]' <<<"$(serve_opencode_config)" >/dev/null \
    || fail "S4: a blank PROXY_NATIVE_TOOL_MODELS means the default patterns"
PROXY_NATIVE_TOOL_MODELS=' OFF '
[[ "$(serve_opencode_config)" == '{"share":"disabled"}' ]] || fail "S4: ' OFF ' turns the native route off"
unset PROXY_NATIVE_TOOL_MODELS harness_serve harness_thoughts
ok "S4: prompt-mode aliases only for native-route models, sharing off"

# --- S5: the key stays off curl's argv and out of opencode's env -------------
cfg=$(_curl_auth_config 'k"e\y')
[[ "$cfg" == 'header = "Authorization: Bearer k\"e\\y"' ]] || fail "S5: curl config not escaped: $cfg"
(
    PROXY_API_KEY=secret OAI_API_KEY=secret CHATGPT_COOKIE_STRING=secret TRACKER_TOKEN=secret
    export PROXY_API_KEY OAI_API_KEY CHATGPT_COOKIE_STRING TRACKER_TOKEN
    host_opencode_env >/dev/null 2>&1 || true
    env | grep -qE '^(PROXY_API_KEY|OAI_API_KEY|CHATGPT_COOKIE_STRING|TRACKER_TOKEN)=' && exit 1
    exit 0
) || fail "S5: host_opencode_env must drop the upstream secrets"
# The scrub must not hide the OAI upstream from the overlay: its models keep
# the slim header (host mode decides before the unset, serve via harness_oai).
for how in host serve; do
    (
        unset harness_oai harness_serve OPENCODE_CONFIG_CONTENT
        OAI_API_URL=http://127.0.0.1:9 OAI_API_KEY=secret OAI_MODELS=m
        export OAI_API_URL OAI_API_KEY OAI_MODELS
        if [[ "$how" == serve ]]; then
            # shellcheck disable=SC2034  # read by host_opencode_env
            harness_oai=$(_oai_configured && echo oai)
            unset OAI_API_KEY
        fi
        host_opencode_env >/dev/null 2>&1 || true
        jq -e '.provider["harness-oai"].options.headers["x-harness-slim"] == "1"' \
            <<<"${OPENCODE_CONFIG_CONTENT:-null}" >/dev/null
    ) || fail "S5: $how: the OAI slim header was lost with the key scrub"
done
# The only Authorization header in the CLI is the one _curl_auth_config
# writes; any other (curl -H, a wget header, a continuation line) would put
# the key on a command line.
hits=$(grep -nE 'Authorization: (Bearer|Basic)' "$HARNESS" | grep -vE '^[0-9]+:[[:space:]]*#')
[[ "$hits" == *'printf '\''header = "Authorization: Bearer %s"'* && $(wc -l <<<"$hits") -eq 1 ]] \
    || fail "S5: an Authorization header outside _curl_auth_config; pipe _curl_auth_config to curl -K - instead:
$hits"
ok "S5: key via curl -K stdin, scrubbed from opencode's env"

# --- S6: with no OAI configured, serve gets to its banner ---------------------
# Runs cmd_serve under the harness's own set -euo pipefail in a fresh bash
# (an `if`/`||` around a subshell would turn set -e off), with the gates, the
# proxy and both servers stubbed. The stub servers exit at once, so serve
# then fails, but only after printing the URLs.
cat >"$TMP_ROOT/serve_run.sh" <<'EOF'
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$1" source "$2" 2>/dev/null
clone_dir="$3"
source "$3/scripts/lib/platform.sh"
for f in host_require_python3 host_require_config host_confirm_gate ensure_dirs \
         host_ensure_toolchain host_preflight _gate_on_upstream_auth \
         _print_upstream_models host_proxy_start host_proxy_wait_ready \
         host_proxy_stop host_write_opencode_config host_opencode_env; do
    eval "$f() { :; }"
done
vault_prepare() { :; }
host_proxy_port() { echo 8000; }
host_usage_ledger() { echo "$state_root/ledger"; }
host_python_bin() { echo true; }
serve_password_file() { echo "$state_root/pw"; }
opencode() { :; }
unset OAI_API_URL OAI_API_KEY OAI_MODELS
set -euo pipefail
mkdir -p "$state_root"
cmd_serve --port 18780
EOF
bash "$TMP_ROOT/serve_run.sh" "$TMP_ROOT" "$HARNESS" "$REPO_ROOT" >"$TMP_ROOT/serve_out" 2>&1 || true
grep -q 'pub page:     http://127.0.0.1:18780/' "$TMP_ROOT/serve_out" \
    || fail "S6: serve exited before printing its URLs:
$(cat "$TMP_ROOT/serve_out")"
ok "S6: serve prints its URLs with no OAI configured"

# --- S7: agents/live-run.sh: a headless run that takes messages --------------
# A stub opencode on PATH: `serve` listens on a loopback port and logs each
# request; `run` logs its argv, names a session in its event stream, waits,
# and exits 7.
LR="$TMP_ROOT/live"
mkdir -p "$LR/bin"
cat >"$LR/bin/opencode" <<EOF
#!$PY
import http.server, json, os, sys, time
log = os.environ["STUB_LOG"]
def note(*a):
    with open(log, "a") as f:
        f.write(" ".join(a) + "\n")
if sys.argv[1] == "serve":
    if os.environ.get("STUB_SERVE_FAIL"):
        sys.exit(1)
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            note("POST", self.path, self.headers.get("Authorization", "none"), body.decode())
            self.send_response(204); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    note("serve-pid", str(os.getpid()))
    print("opencode server listening on http://127.0.0.1:%d" % srv.server_address[1], flush=True)
    srv.serve_forever()
note("run", *sys.argv[2:])
print(json.dumps({"type": "step_start", "sessionID": "ses_stub1"}), flush=True)
time.sleep(float(os.environ.get("STUB_RUN_SECS", "3")))
sys.exit(7)
EOF
chmod +x "$LR/bin/opencode"
live_run() {  # live_run NAME [env...]: runs live-run.sh in the background, sets lr_pid
    env PATH="$LR/bin:$PATH" XDG_DATA_HOME="$LR/$1" STUB_LOG="$LR/$1.log" "${@:2}" \
        bash "$REPO_ROOT/agents/live-run.sh" "$LR/$1.events" --agent yolo "say hi" &
    lr_pid=$!
}
live_run a
for _ in $(seq 1 50); do [[ -f "$LR/a/harness/live/ses_stub1.json" ]] && break; sleep 0.1; done
[[ -f "$LR/a/harness/live/ses_stub1.json" ]] || fail "S7: no live marker for the running session"
printf '%s' '{"parts":[{"type":"text","text":"more"}]}' >"$LR/a/harness/live/ses_stub1.inbox/1-x-ab.json"
rc=0; wait "$lr_pid" || rc=$?
[[ "$rc" == 7 ]] || fail "S7: live-run.sh should exit with run's status (7), got $rc"
grep -q '^run --attach http://127\.0\.0\.1:[0-9]* --format json --agent yolo say hi$' "$LR/a.log" \
    || fail "S7: run was not attached to the loopback serve: $(cat "$LR/a.log")"
grep -q '^POST /session/ses_stub1/prompt_async Basic [A-Za-z0-9+/=]* {"parts":\[{"type":"text","text":"more"}\]}$' "$LR/a.log" \
    || fail "S7: the inbox message did not reach prompt_async with a login: $(cat "$LR/a.log")"
[[ -f "$LR/a/harness/live/ses_stub1.inbox/1-x-ab.sent" ]] || fail "S7: a delivered message should be renamed .sent"
[[ ! -e "$LR/a/harness/live/ses_stub1.json" ]] || fail "S7: the live marker should go when the run ends"
grep -q '"sessionID": "ses_stub1"' "$LR/a.events" || fail "S7: the event stream should reach EVENTS_FILE"
spid=$(awk '$1=="serve-pid"{print $2}' "$LR/a.log")
sleep 0.3
! kill -0 "$spid" 2>/dev/null || fail "S7: the run's opencode serve outlived it"
for mode in fail off; do
    if [[ "$mode" == fail ]]; then live_run "$mode" STUB_SERVE_FAIL=1; else live_run "$mode" HARNESS_LIVE=0; fi
    rc=0; wait "$lr_pid" || rc=$?
    [[ "$rc" == 7 ]] || fail "S7 ($mode): want run's status 7, got $rc"
    grep -q '^run --format json --agent yolo say hi$' "$LR/$mode.log" \
        || fail "S7 ($mode): should fall back to a plain run: $(cat "$LR/$mode.log")"
done
# Killed mid-run (a stage timeout): serve and run go too, and the marker.
live_run term STUB_RUN_SECS=30
for _ in $(seq 1 50); do [[ -f "$LR/term/harness/live/ses_stub1.json" ]] && break; sleep 0.1; done
[[ -f "$LR/term/harness/live/ses_stub1.json" ]] || fail "S7 (term): no live marker"
kill -TERM "$lr_pid"
rc=0; wait "$lr_pid" || rc=$?
[[ "$rc" == 143 ]] || fail "S7 (term): want 143 after SIGTERM, got $rc"
[[ ! -e "$LR/term/harness/live/ses_stub1.json" ]] || fail "S7 (term): the marker should go with the wrapper"
spid=$(awk '$1=="serve-pid"{print $2}' "$LR/term.log")
sleep 0.3
! kill -0 "$spid" 2>/dev/null || fail "S7 (term): opencode serve outlived a killed wrapper"
! pgrep -f "$LR/bin/opencode run" >/dev/null || fail "S7 (term): opencode run outlived a killed wrapper"
ok "S7: live-run.sh attaches run to its own serve, relays the inbox, falls back to a plain run, cleans up when killed"

# --- S8: a headless launch brings up the run viewer (harness_view) -------------
state_root="$TMP_ROOT/view"
mkdir -p "$state_root"
VB="$TMP_ROOT/viewbin"; mkdir -p "$VB"
printf '#!/bin/sh\necho "$@" >>"%s/opened"\n' "$VB" >"$VB/xdg-open"; chmod +x "$VB/xdg-open"
HARNESS_VIEW_PORT=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
export HARNESS_VIEW_PORT
out=$(HARNESS_OPEN=0 harness_view "/runs/follow?since=1" 2>&1)
[[ -z "$out" && ! -e "$state_root/serve/viewer.pid" ]] || fail "S8: HARNESS_OPEN=0 should leave the viewer out: $out"
out=$(PATH="$VB:$PATH" DISPLAY=:9 harness_view "/runs/follow?$(harness_view_since)" 2>&1 </dev/null)
grep -q "watch the agent at http://127.0.0.1:$HARNESS_VIEW_PORT/runs/follow?since=[0-9]*000$" <<<"$out" \
    || fail "S8: no link printed: $out"
pid=$(cut -d' ' -f1 "$state_root/serve/viewer.pid")
curl -sf --noproxy '*' "http://127.0.0.1:$HARNESS_VIEW_PORT/runs" >/dev/null || fail "S8: the viewer is not serving /runs"
harness_view "/runs" >/dev/null 2>&1
[[ "$(cut -d' ' -f1 "$state_root/serve/viewer.pid")" == "$pid" ]] || fail "S8: a second launch should use the viewer that is up"
sleep 0.3
[[ ! -e "$VB/opened" ]] || fail "S8: opened a browser with no terminal to see it"
harness_view_stop
for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
! kill -0 "$pid" 2>/dev/null || fail "S8: harness serve stop left the viewer running"
[[ ! -e "$state_root/serve/viewer.pid" ]] || fail "S8: the pid file should go"
ok "S8: harness_view starts the run viewer once, prints the link, HARNESS_OPEN=0 skips it, stop ends it"

echo "SERVE TEST PASSED"
