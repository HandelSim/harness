#!/usr/bin/env bash
#
# tests/unit_debug_test.sh — `harness debug`, docker-free and offline.
#
#   T1: debug_scrub redacts .env values (and a URL's host, and doctor's
#       "abcd...wxyz" form of a secret), vault and ticket names, the queue's
#       tickets (key, project folder in each path form, its name, branch), the install
#       root and paths under $HOME, and leaves code references alone
#   T2: debug_opencode_config is read-only: "*" asks (denies under -p) and
#       comes first, the allowlist after it, the write patterns last, on the
#       top level and the build/general/yolo agents; report.md is editable;
#       merged over the yolo agent's allow-all it still asks
#   T3: vault_prepare and ticket_prepare attach nothing under harness_debug
#   T4: --help, through cmd_debug and main
#   T5: a run: AGENTS.md copied, --yolo dropped, the install root mounted,
#       the report redacted into state/debug/reports/ and printed, the raw
#       one removed, the agent's exit code passed on; `debug host` goes
#       through cmd_host
#   T6: a launch that fails before any report keeps the redacted snapshot
#   T7: the queue's run (--dir, --fix): its own folder, AGENTS.md with
#       debug_queue.md and the run's repairs, those allowed for that working
#       copy alone and the force/reset guards after them, the report redacted
#       in place with the ticket keys kept; no report is a needs-owner one;
#       a --dir outside state/debug/runs/ is refused, a --fix outside the
#       queue's clones in harness's own folder only reports
#
# Run:  bash tests/unit_debug_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " harness debug unit test"
echo "============================================================"

fail() { echo "[debug] FAIL: $*" >&2; exit 1; }
ok()   { echo "[debug] OK: $*"; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/state"
cat > "$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://upstream.example.org/v1
PROXY_API_KEY="sk-secretvalue123456"
PROXY_PORT=8000
PROXY_PROMPT_MODE=hybrid
DEFAULT_MODEL_NAME=my-model-x
EOF
printf 'brainvault\tgit@github.com:someone/brain.git\n' > "$TMP_ROOT/state/vaults.list"
printf '/home/tester/work/acme\tACME-12\n' > "$TMP_ROOT/state/tickets.list"
mkdir -p "$TMP_ROOT/state/queue/WIDG-7" "$TMP_ROOT/state/queue/WIN-3"
printf '{"key": "WIDG-7", "project": "/home/tester/work/widgetapp", "branch": "feature/fast-path", "base": "main", "start": "abc1234def"}' \
    > "$TMP_ROOT/state/queue/WIDG-7/state.json"
printf '{"key": "WIN-3", "project": "C:/Users/t/src/gizmo-lib", "base": "release-2"}' > "$TMP_ROOT/state/queue/WIN-3/state.json"

src() {
    # shellcheck disable=SC1090
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    clone_dir="$REPO_ROOT"
    HOME=/home/tester
}

# --- T1: debug_scrub -------------------------------------------------------------
t1=$(
    src
    debug_scrub <<EOF
key sk-secretvalue123456 and doctor's sk-s...3456
url https://upstream.example.org/v1/chat and host upstream.example.org
model my-model-x on port 8000 mode hybrid
vault brainvault from git@github.com:someone/brain.git, ticket ACME-12
project /home/tester/work/acme/src/app.py
other /home/tester/code/secret-app/main.go and ~/code/secret-app/x
opencode log /home/tester/.local/share/opencode/log/a.log
code $TMP_ROOT/proxy/proxy.py:120 and harness:2321
queue WIDG-7 in /home/tester/work/widgetapp on feature/fast-path from main at abc1234def
the widgetapp build, state/queue/WIDG-7/implement-0.out
win WIN-3 /c/Users/t/src/gizmo-lib and C:\Users\t\src\gizmo-lib\a.c on release-2
EOF
)
for bad in sk-secret 'sk-s...3456' upstream.example my-model-x brainvault someone/brain ACME-12 \
        acme secret-app "$TMP_ROOT" /home/tester WIDG-7 widgetapp fast-path WIN-3 gizmo-lib release-2; do
    [[ "$t1" != *"$bad"* ]] || fail "T1: '$bad' survived:
$t1"
done
for want in 'key [PROXY_API_KEY] and doctor'"'"'s [PROXY_API_KEY]' \
        'url [PROXY_API_URL]/chat and host [PROXY_API_URL host]' \
        'model [DEFAULT_MODEL_NAME] on port 8000 mode hybrid' \
        'vault [vault] from [vault url], ticket [ticket]' \
        'project [project folder]/src/app.py' \
        'other ~/<path> and ~/<path>' \
        'opencode log ~/.local/share/opencode/log/a.log' \
        'code <harness>/proxy/proxy.py:120 and harness:2321' \
        'queue [ticket] in [project folder] on [branch] from main at abc1234def' \
        'the [project] build, state/queue/[ticket]/implement-0.out' \
        'win [ticket] [project folder] and [project folder]\a.c on [branch]'; do
    grep -qxF -- "$want" <<<"$t1" || fail "T1: missing line '$want':
$t1"
done
ok "T1: debug_scrub redacts .env values, vault/ticket/queue names, install root and home paths"

# --- T2: permission overlay --------------------------------------------------------
t2_off=$( src; debug_opencode_config )
[[ -z "$t2_off" ]] || fail "T2: overlay without harness_debug — $t2_off"
check_perm() {   # <json> <mode> <label>
    local j="$1" m="$2" what
    for what in '.permission' '.agent.build.permission' '.agent.general.permission' '.agent.yolo.permission'; do
        harness_jq -e --arg m "$m" "$what"' as $p
            | ($p.bash | keys_unsorted) as $k
            | $k[0] == "*" and $p.bash["*"] == $m
              and $p.bash["cat *"] == "allow" and $p.bash["harness doctor*"] == "allow"
              and $p.bash["docker logs *"] == "allow"
              and $p.bash["*>*"] == $m and $p.bash["*.env"] == $m
              and ($k | index("*>*")) > ($k | index("cat *"))
              and $p.edit["*"] == $m and $p.edit["report.md"] == "allow"
              and $p.read["*.env"] == $m and $p.external_directory["*"] == "allow"' \
            <<<"$j" >/dev/null || fail "T2: $3 $what — $j"
    done
}
t2=$(
    src
    harness_debug=1
    j=$(debug_opencode_config)
    check_perm "$j" ask interactive
    harness_debug_print=1
    check_perm "$(debug_opencode_config)" deny headless
    # Over the yolo agent the entrypoint writes, "*" keeps its first place.
    harness_jq -cn --argjson o "$j" '{agent: {yolo: {permission: {edit: "allow", bash: {"*": "allow"}}}}} * $o
        | .agent.yolo.permission | (.bash | keys_unsorted[0]) == "*" and .bash["*"] == "ask" and .edit["*"] == "ask"' \
        | grep -qx true || fail "T2: merged over the yolo agent"
    echo passed
)
[[ "$t2" == passed ]] || fail "T2: $t2"
ok "T2: debug_opencode_config is read-only (ask, deny under -p) on every primary agent"

# --- T3: no vault, no ticket ---------------------------------------------------------
t3=$(
    src
    harness_debug=1
    vault_active_name() { echo CALLED; }
    ticket_remember() { echo CALLED; }
    ticket_for_dir() { echo CALLED; }
    harness_ticket_arg=KEY-1
    v=$(vault_prepare)
    ticket_prepare
    echo "vault=[$v] ticket=[$harness_ticket]"
)
[[ "$t3" == 'vault=[] ticket=[]' ]] || fail "T3: $t3"
ok "T3: vault_prepare and ticket_prepare attach nothing under harness_debug"

# --- T4: --help ------------------------------------------------------------------------
t4=$( src; cmd_debug --help ) || fail "T4: cmd_debug --help exited non-zero"
grep -q 'harness debug host' <<<"$t4" || fail "T4: cmd_debug --help — $t4"
t4=$( src; main debug -h ) || fail "T4: main debug -h exited non-zero"
grep -q 'harness debug host' <<<"$t4" || fail "T4: main debug -h — $t4"
t4=$( src; cmd_help ) || true
grep -q '^  debug \[host\]' <<<"$t4" || fail "T4: harness help has no debug line"
ok "T4: harness debug --help, and the help line"

# --- T5: a run with the launch stubbed -------------------------------------------------
rc=0
t5=$(
    src
    harness_runtime_installed() { return 0; }
    debug_snapshot() { echo "mode: $1"; }
    run_agent() {
        printf '%s\n' "$*" > "$TMP_ROOT/launch.args"
        [[ "$PWD" == "$TMP_ROOT/state/debug" ]] || echo "wrong cwd $PWD" > "$TMP_ROOT/launch.args"
        printf '# harness issue: x\nkey sk-secretvalue123456 at %s/harness:10\n' "$TMP_ROOT" > report.md
        echo "agent stdout"
        exit 3
    }
    cmd_debug -p "why it fails" --yolo 2>"$TMP_ROOT/err"
) || rc=$?
[[ "$(cat "$TMP_ROOT/launch.args")" == "opencode --mount $TMP_ROOT -p why it fails" ]] \
    || fail "T5: launch args — $(cat "$TMP_ROOT/launch.args")"
grep -q 'ignoring --yolo' "$TMP_ROOT/err" || fail "T5: no --yolo note"
cmp -s "$REPO_ROOT/scripts/debug_agent.md" "$TMP_ROOT/state/debug/AGENTS.md" || fail "T5: AGENTS.md not copied"
grep -qx 'mode: container' "$TMP_ROOT/state/debug/snapshot.txt" || fail "T5: snapshot"
grep -qx 'agent stdout' "$TMP_ROOT/state/debug/session.log" || fail "T5: -p output not in session.log"
[[ "$t5" == $'# harness issue: x\nkey [PROXY_API_KEY] at <harness>/harness:10' ]] || fail "T5: printed report — $t5"
[[ ! -e "$TMP_ROOT/state/debug/report.md" ]] || fail "T5: raw report.md left behind"
saved=$(ls "$TMP_ROOT/state/debug/reports/"*.md)
[[ "$(cat "$saved")" == "$t5" ]] || fail "T5: saved report differs"
[[ $rc -eq 3 ]] || fail "T5: the agent's exit code is not passed on (rc $rc)"
t5h=$(
    src
    cmd_host() { echo "HOST=[$*] debug=$harness_debug print=$harness_debug_print"; exit 0; }
    cmd_debug host 2>/dev/null
)
[[ "$t5h" == 'HOST=[] debug=1 print=0' ]] || fail "T5: debug host — $t5h"
ok "T5: a run redacts and prints the report; debug host goes through cmd_host"

# --- T6: a launch that fails before any report keeps the redacted snapshot -----------
rm -rf "$TMP_ROOT/state/debug"
rc=0
(
    src
    harness_runtime_installed() { return 0; }
    debug_snapshot() { echo "key sk-secretvalue123456 at $TMP_ROOT"; }
    run_agent() { exit 1; }
    cmd_debug 2>"$TMP_ROOT/err"
) || rc=$?
[[ $rc -eq 1 ]] || fail "T6: rc $rc"
saved=$(ls "$TMP_ROOT/state/debug/reports/"*-snapshot.md) || fail "T6: no snapshot kept"
[[ "$(cat "$saved")" == 'key [PROXY_API_KEY] at <harness>' ]] || fail "T6: kept snapshot — $(cat "$saved")"
grep -q 'did not run to the end' "$TMP_ROOT/err" || fail "T6: no note — $(cat "$TMP_ROOT/err")"
ok "T6: a failed launch keeps the redacted snapshot and says so"

# --- T7: the queue's debug run -------------------------------------------------------
run7="$TMP_ROOT/state/debug/runs/20260101-000000-WIDG-7"
work7="$TMP_ROOT/state/queue/WIDG-7/repo"
mkdir -p "$run7"
git init -q "$work7"
rc=0
t7=$(
    src
    harness_runtime_installed() { return 0; }
    debug_snapshot() { echo "mode: $1"; }
    run_agent() {
        printf '%s\n' "$*" > "$TMP_ROOT/launch7.args"
        [[ "$PWD" == "$run7" ]] || echo "wrong cwd $PWD" > "$TMP_ROOT/launch7.args"
        debug_opencode_config > "$TMP_ROOT/perm7.json"
        printf '# x\nWIDG-7 on feature/fast-path, key sk-secretvalue123456\nVERDICT: fixed\n' > report.md
        exit 0
    }
    cmd_debug --dir "$run7" --fix "$work7" --fix-branch feature/fast-path -p "the block" 2>"$TMP_ROOT/err"
) || rc=$?
[[ $rc -eq 0 ]] || fail "T7: rc $rc — $(cat "$TMP_ROOT/err")"
[[ "$(cat "$TMP_ROOT/launch7.args")" == "opencode --mount $TMP_ROOT -p the block" ]] \
    || fail "T7: launch args — $(cat "$TMP_ROOT/launch7.args")"
grep -q "the ticket queue's debug agent" "$run7/AGENTS.md" || fail "T7: no debug_queue.md in AGENTS.md"
grep -qx "    git -C $work7 rebase --abort" "$run7/AGENTS.md" || fail "T7: the run's repairs — $(cat "$run7/AGENTS.md")"
( src; harness_jq -e --arg w "$work7" --arg r "$run7/report.md" '.permission as $p | ($p.bash | keys_unsorted) as $k
    | $p.bash["git -C \($w) rebase --abort"] == "allow" and $p.bash["git -C \($w) switch feature/fast-path"] == "allow"
      and $p.bash["git -C \($w) switch *"] == null and $p.bash["git -C \($w) status*"] == "allow"
      and $p.bash["rm -f \($w)/.git/index.lock"] == "allow" and $p.bash["git *--force*"] == "deny"
      and $p.bash["git *--hard*"] == "deny" and ($k | index("git *--force*")) > ($k | index("git -C \($w) rebase --abort"))
      and $p.bash["git -C * rebase --abort"] == null and $p.edit[$r] == "allow"
      and ([$k[] | select(test("^[*]|^git -C [*]|^curl [*]$|compose -p [*]"))] == ["*", "*>*", "*.env", "*.env *",
           "*.env.*", "*.env/*", "*environ*", "*/.ssh/*", "*id_rsa*", "*id_ed25519*", "*.pem*"])
      and $p.bash["curl -s http* *"] == "deny" and $p.bash["curl *@*"] == "deny"' "$TMP_ROOT/perm7.json" >/dev/null ) \
    || fail "T7: permissions — $(cat "$TMP_ROOT/perm7.json")"
[[ "$(cat "$run7/report.md")" == $'# x\nWIDG-7 on [branch], key [PROXY_API_KEY]\nVERDICT: fixed' ]] \
    || fail "T7: report — $(cat "$run7/report.md")"
[[ "$t7" == "$(cat "$run7/report.md")" ]] || fail "T7: printed — $t7"
rm -f "$run7/report.md"
( src; harness_runtime_installed() { return 0; }; debug_snapshot() { :; }; run_agent() { exit 2; }
  cmd_debug --dir "$run7" -p x >/dev/null 2>&1 ) || true
grep -qx 'VERDICT: needs-owner' "$run7/report.md" || fail "T7: no-report stub — $(cat "$run7/report.md")"
rc=0; ( src; cmd_debug --dir "$TMP_ROOT/state" -p x >/dev/null 2>&1 ) || rc=$?
[[ $rc -eq 1 ]] || fail "T7: a --dir outside state/debug/runs/ was taken"
git init -q "$TMP_ROOT/state/notqueue"
t7o=$( src; harness_runtime_installed() { return 0; }; debug_snapshot() { :; }
       run_agent() { debug_opencode_config > "$TMP_ROOT/perm7o.json"; echo "VERDICT: needs-owner" > report.md; exit 0; }
       cmd_debug --dir "$run7" --fix "$TMP_ROOT/state/notqueue" -p x 2>"$TMP_ROOT/err" ) || true
grep -q "will not repair harness's own folder" "$TMP_ROOT/err" || fail "T7: harness's folder — $(cat "$TMP_ROOT/err")"
[[ -s "$TMP_ROOT/perm7o.json" ]] || fail "T7: harness's folder, no config"
grep -q 'rebase --abort' "$TMP_ROOT/perm7o.json" && fail "T7: repairs allowed in harness's own folder"
rm -f "$TMP_ROOT/perm7o.json"
# the owner's own folder (in place): nothing its own work in progress could lose
own7=$(mktemp -d); git init -q "$own7"; own7=$(cd "$own7" && pwd -P)
t7o=$( src; harness_runtime_installed() { return 0; }; debug_snapshot() { :; }
       run_agent() { debug_opencode_config > "$TMP_ROOT/perm7o.json"; echo "VERDICT: needs-owner" > report.md; exit 0; }
       cmd_debug --dir "$run7" --fix "$own7" --fix-branch main -p x 2>"$TMP_ROOT/err" ) || true
grep -q "the owner's own folder" "$run7/AGENTS.md" || fail "T7: in place, AGENTS.md"
got7=$( src; harness_jq -r '.permission.bash | to_entries[] | select(.value == "allow") | .key' "$TMP_ROOT/perm7o.json" )
grep -qx "git -C $own7 update-index -q --refresh" <<<"$got7" || fail "T7: in place, no refresh — $(cat "$TMP_ROOT/err")"
grep -E 'abort|index\.lock|switch|stash' <<<"$got7" && fail "T7: in place, a repair that can lose the owner's work"
rm -rf "$own7" "$TMP_ROOT/perm7o.json"
# a folder a pattern cannot spell (a space): the run only reports
work8="$TMP_ROOT/state/queue/W 8/repo"; git init -q "$work8"
t7o=$( src; harness_runtime_installed() { return 0; }; debug_snapshot() { :; }
       run_agent() { debug_opencode_config > "$TMP_ROOT/perm7o.json"; echo "VERDICT: needs-owner" > report.md; exit 0; }
       cmd_debug --dir "$run7" --fix "$work8" -p x 2>"$TMP_ROOT/err" ) || true
grep -q "only reports" "$TMP_ROOT/err" && [[ -s "$TMP_ROOT/perm7o.json" ]] && ! grep -q 'abort' "$TMP_ROOT/perm7o.json" \
    || fail "T7: a space in the path — $(cat "$TMP_ROOT/err")"
ok "T7: the queue's debug run: its own folder, its working copy's repairs only, the report redacted in place"

echo "[debug] all checks passed"
