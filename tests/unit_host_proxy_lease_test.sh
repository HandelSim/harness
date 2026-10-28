#!/usr/bin/env bash
#
# tests/unit_host_proxy_lease_test.sh — several harnesses share the one host
# proxy; one ending (or seeing a config change) must not stop or restart it
# under another. Docker-free: the "proxy" is a sleep, the "other harnesses"
# are background processes whose command line names harness.
#
#   T1  host_proxy_others lists live holders, drops dead ones and other programs
#   T2  host_proxy_done stops the proxy only when no other harness holds it
#   T3  host_proxy_start keeps a proxy others use on a config change, restarts
#       it when nobody does, and refuses one of another kind
#   T4  cmd_down and host_proxy_refresh (upgrade) spare a proxy others use
#
# Run:  bash tests/unit_host_proxy_lease_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " host proxy leases unit test"
echo "============================================================"

pass=0
fail() { echo "[proxy-lease] FAIL: $*" >&2; exit 1; }
ok()   { echo "[proxy-lease] OK: $*"; pass=$((pass + 1)); }

TMP_ROOT="$(mktemp -d)"
KIDS=()
cleanup() {
    local k
    for k in ${KIDS[@]+"${KIDS[@]}"}; do kill "$k" 2>/dev/null || true; done
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT
: >"$TMP_ROOT/.harness-allowlist"
cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://api.example.com
PROXY_API_KEY=sk-test-key
DEFAULT_MODEL_NAME=test-model
EOF

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$TMP_ROOT/.harness-allowlist" \
    source "$HARNESS" 2>/dev/null
clone_dir="$REPO_ROOT"
set +e
mkdir -p "$(host_state_dir)"

# A process that looks like another harness to host_proxy_others (its command
# line names harness) and lives until killed.
other_harness() {
    bash -c 'while :; do sleep 1; done' fake-harness &
    KIDS+=("$!")
    : >"$(host_proxy_usersdir)/$!"
    OTHER=$!
}
# A stand-in for the running proxy: the pidfile points at a sleep.
fake_proxy() {
    sleep 300 &
    KIDS+=("$!")
    printf '%s\n' "$!" >"$(host_proxy_pidfile)"
    PROXY=$!
}

# --- T1 ----------------------------------------------------------------------
mkdir -p "$(host_proxy_usersdir)"
host_proxy_hold
[[ -f "$(host_proxy_usersdir)/$$" ]] || fail "T1: hold left no lease"
[[ -z "$(host_proxy_others)" ]] || fail "T1: this harness counted itself"
other_harness
[[ "$(host_proxy_others)" == "$OTHER" ]] || fail "T1: live other harness not listed — $(host_proxy_others)"
sleep 300 & KIDS+=("$!"); notharness=$!
: >"$(host_proxy_usersdir)/$notharness"
: >"$(host_proxy_usersdir)/999999"
[[ "$(host_proxy_others)" == "$OTHER" ]] || fail "T1: dead or reused pids counted — $(host_proxy_others)"
if [[ -r /proc/$$/cmdline ]]; then
    [[ ! -e "$(host_proxy_usersdir)/$notharness" ]] || fail "T1: a non-harness pid's lease was kept"
fi
[[ ! -e "$(host_proxy_usersdir)/999999" ]] || fail "T1: a dead pid's lease was kept"
kill "$OTHER"; wait "$OTHER" 2>/dev/null
[[ -z "$(host_proxy_others)" ]] || fail "T1: an ended harness still counted"
ok "T1: host_proxy_others lists only live harnesses other than this one"

# --- T2 ----------------------------------------------------------------------
fake_proxy
other_harness
out=$(host_proxy_done 2>&1)
host_proxy_running || fail "T2: proxy stopped while another harness uses it"
grep -q 'left running' <<<"$out" || fail "T2: no word that it was left running — $out"
[[ ! -e "$(host_proxy_usersdir)/$$" ]] || fail "T2: done kept this harness's lease"
kill "$OTHER"; wait "$OTHER" 2>/dev/null
host_proxy_done >/dev/null 2>&1
host_proxy_running && fail "T2: last user's done left the proxy up"
ok "T2: the last harness to finish stops the proxy, the others leave it"

# --- T3 ----------------------------------------------------------------------
# Stops a (re)start right after its decision, before it launches anything.
host_proxy_ensure_venv() { echo VENV; return 1; }
fake_proxy
backend_override="" prompt_mode_override=""
printf 'stale\n' >"$(host_proxy_fpfile)"
printf '\n\n%s\n' "$(host_proxy_port)" >"$(host_proxy_modefile)"
other_harness
out=$(host_proxy_start 2>&1); rc=$?
(( rc == 0 )) || fail "T3: start failed on a shared proxy — $out"
grep -q 'keeping it' <<<"$out" || fail "T3: no keep message — $out"
grep -q VENV <<<"$out" && fail "T3: restarted the proxy under another harness — $out"
[[ "$(host_proxy_pid)" == "$PROXY" ]] || fail "T3: proxy pid changed"
[[ "$(cat "$(host_proxy_fpfile)")" == stale ]] || fail "T3: fingerprint updated though the proxy kept the old config"
backend_override=chatgpt
out=$(host_proxy_start 2>&1); rc=$?
backend_override=""
(( rc != 0 )) || fail "T3: a proxy of another backend was reused — $out"
grep -q 'another backend' <<<"$out" || fail "T3: no reason given — $out"
host_proxy_running || fail "T3: the refusal stopped the proxy"
kill "$OTHER"; wait "$OTHER" 2>/dev/null
# Nobody else: the stale config restarts it (stop, then the launch path).
out=$(host_proxy_start 2>&1)
grep -q 'restarting it' <<<"$out" || fail "T3: no restart when nobody else uses it — $out"
host_proxy_stop
ok "T3: a config change keeps a shared proxy, restarts an unshared one, refuses another kind"

# --- T4 ----------------------------------------------------------------------
fake_proxy
other_harness
out=$( (harness_runtime_installed() { return 1; }; host_mcp_stop_all() { :; }; cmd_down) 2>&1 )
host_proxy_running || fail "T4: harness down (last container agent exit) stopped a proxy in use — $out"
out=$(host_proxy_refresh 2>&1)
[[ "$(host_proxy_pid)" == "$PROXY" ]] || fail "T4: upgrade restarted a proxy in use — $out"
grep -q 'old code' <<<"$out" || fail "T4: upgrade gave no word — $out"
kill "$OTHER"; wait "$OTHER" 2>/dev/null
( harness_runtime_installed() { return 1; }; host_mcp_stop_all() { :; }; cmd_down ) >/dev/null 2>&1
host_proxy_running && fail "T4: harness down left an unused proxy up"
ok "T4: harness down and the upgrade restart spare a proxy other harnesses use"

echo "[proxy-lease] all $pass checks passed"
