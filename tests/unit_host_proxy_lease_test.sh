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
#   T4  cmd_down (last container agent) and host_proxy_refresh (upgrade) spare
#       a proxy others use; the harness down command stops it anyway
#   T5  leases say what holds them: a reused pid does not count, and a serve,
#       queue run or findings run alone does not keep a config or code change
#       from restarting the proxy
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
# line names harness) and lives until killed. With KIND, its lease is the one
# host_proxy_hold KIND writes; without, an empty one (from before KIND).
other_harness() {
    bash -c 'while :; do sleep 1; done' fake-harness &
    KIDS+=("$!")
    OTHER=$!
    if [[ -n "${1:-}" ]]; then
        printf '%s %s\n' "$1" "$(host_proxy_starttime "$OTHER")" >"$(host_proxy_usersdir)/$OTHER"
    else
        : >"$(host_proxy_usersdir)/$OTHER"
    fi
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
fake_proxy
other_harness queue; q=$OTHER
out=$( (harness_runtime_installed() { return 1; }; host_mcp_stop_all() { :; }; cmd_down_all) 2>&1 )
host_proxy_running && fail "T4: the harness down command left a proxy others use up — $out"
grep -q "harness queue run (pid $q)" <<<"$out" || fail "T4: harness down did not say whom it cut off — $out"
grep -q 'harness queue halt' <<<"$out" || fail "T4: no word on ending the background run — $out"
kill "$q"; wait "$q" 2>/dev/null
ok "T4: the last container agent's down and the upgrade restart spare a proxy others use; harness down stops it"

# --- T5 ----------------------------------------------------------------------
host_proxy_hold queue
read -r kind st <"$(host_proxy_usersdir)/$$"
[[ "$kind" == queue ]] || fail "T5: hold did not record its kind — $kind"
if [[ -r /proc/$$/stat ]]; then
    [[ "$st" =~ ^[0-9]+$ ]] || fail "T5: hold did not record a start time — $st"
    # A lease whose start time is not its pid's: the pid was reused.
    other_harness host
    printf 'host 1\n' >"$(host_proxy_usersdir)/$OTHER"
    [[ -z "$(host_proxy_others)" ]] || fail "T5: a reused pid counted as the holder"
    [[ ! -e "$(host_proxy_usersdir)/$OTHER" ]] || fail "T5: a reused pid's lease was kept"
    kill "$OTHER"; wait "$OTHER" 2>/dev/null
fi
host_proxy_release
other_harness serve; s=$OTHER
other_harness findings; f=$OTHER
other_harness host; h=$OTHER
[[ "$(host_proxy_others | sort)" == "$(printf '%s\n' "$s" "$f" "$h" | sort)" ]] || fail "T5: holders — $(host_proxy_others)"
[[ "$(host_proxy_others --sessions)" == "$h" ]] || fail "T5: only the host session counts for a restart — $(host_proxy_others --sessions)"
who=$(host_proxy_who "$s" "$h")
[[ "$who" == "harness serve (pid $s), harness host (pid $h)" ]] || fail "T5: who — $who"
# A config change with a host session holding it: kept.
fake_proxy
printf 'stale\n' >"$(host_proxy_fpfile)"
printf '\n\n%s\n' "$(host_proxy_port)" >"$(host_proxy_modefile)"
out=$(host_proxy_start 2>&1)
grep -q 'keeping it' <<<"$out" || fail "T5: restarted under a host session — $out"
grep -q "harness host (pid $h)" <<<"$out" || fail "T5: the keep message does not name the session — $out"
out=$(host_proxy_refresh 2>&1)
[[ "$(host_proxy_pid)" == "$PROXY" ]] || fail "T5: upgrade restarted under a host session — $out"
kill "$h"; wait "$h" 2>/dev/null
# Only serve and the findings run: restarted (up to the launch).
out=$(host_proxy_start 2>&1)
grep -q 'restarting it' <<<"$out" || fail "T5: background runs kept a stale proxy — $out"
host_proxy_running && fail "T5: the stale proxy was not stopped"
fake_proxy
out=$(host_proxy_refresh 2>&1)
grep -q 'restarting the host proxy' <<<"$out" || fail "T5: upgrade kept the old code for background runs — $out"
host_proxy_stop
# Another backend is still refused while anyone holds it.
fake_proxy
backend_override=chatgpt
out=$(host_proxy_start 2>&1); rc=$?
backend_override=""
(( rc != 0 )) || fail "T5: switched backend under harness serve — $out"
grep -q "harness serve (pid $s)" <<<"$out" || fail "T5: refusal does not name serve — $out"
host_proxy_stop 2>/dev/null   # harness_is_git_bash is not sourced here
kill "$s" "$f"; wait "$s" "$f" 2>/dev/null
ok "T5: leases name their holder; reused pids drop; background runs alone do not pin a stale proxy"

echo "[proxy-lease] all $pass checks passed"
