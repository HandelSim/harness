#!/usr/bin/env bash
#
# tests/unit_findings_test.sh — exercise `harness findings` without docker or a
# model.
#
#   - tests/findings_agent_test.py         scripts/findings_agent.py against
#                                          throwaway git repos, a fake proxy and
#                                          a stub harness for the sessions:
#                                          parse, evidence checks, ids and
#                                          dedupe, areas and their order, the
#                                          snapshot, stale marking, the
#                                          free-model guard, a full pass, a
#                                          pass that fails or goes silent, the
#                                          lock, wanted and stop, a left pass
#   - serve/test_findings_agent_view.py    the queue page's card and its
#                                          start / stop / notes actions
#   - cmd_findings                         the CLI: help, dispatch, notes and
#                                          status passed through, run's proxy
#                                          lease and its restart after exit 76
#
# Prints "FINDINGS TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " findings unit test"
echo "============================================================"

fail() { echo "[findings-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[findings-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v git >/dev/null 2>&1 || fail "this test needs git"
PY=$(command -v python3 || command -v python) || fail "this test needs Python 3"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# --- F1: the agent, against a fake proxy -------------------------------------
out=$("$PY" "$SCRIPT_DIR/findings_agent_test.py" 2>&1) || { echo "$out" | grep -v '^20[0-9][0-9]-' >&2; fail "F1: tests/findings_agent_test.py"; }
grep -q '^OK' <<<"$out" || fail "F1: no OK from tests/findings_agent_test.py — $(tail -5 <<<"$out")"
ok "F1: findings_agent.py: $(grep -o 'Ran [0-9]* tests' <<<"$out")"

# --- F2: the queue page's card -------------------------------------------------
out=$(cd "$REPO_ROOT/serve" && "$PY" -m unittest -q test_findings_agent_view 2>&1) || { echo "$out" >&2; fail "F2: serve/test_findings_agent_view.py"; }
ok "F2: findings_agent_view.py: $(grep -o 'Ran [0-9]* tests' <<<"$out")"

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT/install" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_findings)" == "function" ]] || fail "cmd_findings not sourced"
# shellcheck disable=SC2034  # read by the sourced harness functions
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"
mkdir -p "$state_root"

# --- F3: help, dispatch, bad input ----------------------------------------------
out=$(cmd_findings --help)
for w in 'harness findings start' 'harness findings stop' 'harness findings status' 'harness findings notes' \
         FINDINGS_MODEL FINDINGS_GAP FINDINGS_PASS_TIMEOUT FINDINGS_IDLE_TIMEOUT /runs/findings/notes \
         FINDINGS_OPEN_MAX FINDINGS_SINGLE_MESSAGE --read-only; do
    grep -qF -- "$w" <<<"$out" || fail "F3: --help does not mention $w"
done
cmd_help 2>/dev/null | grep -q "findings \[start|stop|status\]" || fail "F3: harness help has no findings entry"
grep -q 'findings) *cmd_findings "\$@"' "$HARNESS" || fail "F3: findings is not dispatched"
if ( cmd_findings bogus ) >/dev/null 2>&1; then fail "F3: an unknown command was accepted"; fi
if ( cmd_findings start --nope ) >/dev/null 2>&1; then fail "F3: an unknown option was accepted"; fi
if ( cmd_findings run "$TMP_ROOT/missing" ) >/dev/null 2>&1; then fail "F3: a missing folder was accepted"; fi
mkdir -p "$TMP_ROOT/plain"
if ( cmd_findings run "$TMP_ROOT/plain" ) >/dev/null 2>&1; then fail "F3: a folder outside git was accepted"; fi
ok "F3: help, dispatch, bad commands, options and folders refused"

# --- F4: status and notes go through to the state folder ------------------------
out=$(cmd_findings status) || fail "F4: status failed"
grep -q "never started" <<<"$out" || fail "F4: status — $out"
grep -qF "$state_root/findings" <<<"$out" || fail "F4: status names another folder — $out"
mkdir -p "$state_root/findings"
echo "leave tests alone" > "$state_root/findings/notes.md"
[[ "$(cmd_findings notes </dev/null)" == "leave tests alone" ]] || fail "F4: notes without a tty should print them"
out=$(cmd_findings stop) || fail "F4: stop failed"
grep -q "no findings agent is running" <<<"$out" || fail "F4: stop — $out"
ok "F4: status, notes and stop use $state_root/findings"

# --- F5: run holds the proxy, passes the repo, starts the proxy again after 76 ---
REPO="$TMP_ROOT/repo"
git init -q "$REPO" && git -C "$REPO" commit -q --allow-empty -m init
STUB_PY="$TMP_ROOT/py.sh"
cat > "$STUB_PY" <<'EOF2'
#!/usr/bin/env bash
printf 'utf8=%s args=%s\n' "${PYTHONUTF8:-}" "$*" >> "$F5_LOG"
n=$(cat "$F5_COUNT" 2>/dev/null || echo 0); echo $((n + 1)) > "$F5_COUNT"
(( n == 0 )) && exit 76
exit 0
EOF2
chmod +x "$STUB_PY"
export F5_LOG="$TMP_ROOT/f5.log" F5_COUNT="$TMP_ROOT/f5.count"
f5() {
    (
        host_require_config() { :; }; ensure_dirs() { :; }; host_ensure_toolchain() { :; }
        host_preflight() { :; }; _gate_on_upstream_auth() { :; }; host_proxy_running() { return 1; }
        host_proxy_hold() { echo "hold" >> "$F5_LOG"; }
        host_proxy_start() { echo "proxy start" >> "$F5_LOG"; }
        host_proxy_wait_ready() { :; }
        host_proxy_done() { echo "done" >> "$F5_LOG"; }
        host_proxy_release() { echo "release" >> "$F5_LOG"; }
        host_proxy_port() { echo 4321; }
        host_require_python3() { :; }; host_python_bin() { echo "$STUB_PY"; }
        cd "$REPO/.." && FINDINGS_MODEL=m1 cmd_findings run "$REPO"
    )
}
: > "$F5_LOG"; rm -f "$F5_COUNT"
f5 || fail "F5: run failed — $(cat "$F5_LOG")"
top=$(git -C "$REPO" rev-parse --show-toplevel)
exp=$'hold\nproxy start\n'"utf8=1 args=$REPO_ROOT/scripts/findings_agent.py run --state $state_root/findings --proxy http://127.0.0.1:4321/v1 --model m1 --harness $REPO_ROOT/harness --repo $top"$'\nproxy start\n'"utf8=1 args=$REPO_ROOT/scripts/findings_agent.py run --state $state_root/findings --proxy http://127.0.0.1:4321/v1 --model m1 --harness $REPO_ROOT/harness --repo $top"$'\ndone'
[[ "$(cat "$F5_LOG")" == "$exp" ]] || fail "F5: run — got:
$(cat "$F5_LOG")
want:
$exp"
[[ ! -e "$state_root/findings/wrapper.pid" ]] || fail "F5: wrapper.pid left behind"
ok "F5: run holds the proxy, passes --repo, starts the proxy and the agent again after exit 76, then lets go"

# --- F6: host --read-only: the sessions' permissions -----------------------------
[[ -z "$(harness_read_only=0 read_only_opencode_config)" ]] || fail "F6: an overlay without --read-only"
ro=$(harness_read_only=1 read_only_opencode_config) || fail "F6: read_only_opencode_config failed"
RO="$ro" python3 - <<'PYEOF' || fail "F6: the read-only overlay is wrong"
import fnmatch, json, os, sys
c = json.loads(os.environ["RO"])
for p in [c["permission"]] + [c["agent"][a]["permission"] for a in ("build", "general", "yolo")]:
    assert p["edit"] == "deny" and p["webfetch"] == "deny" and p["websearch"] == "deny", p
    assert p["external_directory"] == "deny", p
    assert p["read"]["*.env"] == "deny" and p["read"]["*"] == "allow", p["read"]
rules = list(c["permission"]["bash"].items())
assert rules[0] == ("*", "deny"), rules[0]
def verdict(cmd):   # opencode: the last matching rule wins
    got = None
    for pat, v in rules:
        if fnmatch.fnmatchcase(cmd, pat):
            got = v
    return got
for cmd in ("cat serve/runs.py", "grep -rn def scripts", "ls", "find . -name '*.py'", "wc -l harness"):
    assert verdict(cmd) == "allow", cmd
for cmd in ("rm -rf x", "cat a > b", "grep x | sh", "find . -delete", "find . -exec rm {} +", "cat ../x",
            "cat /etc/passwd", "cat .env", "ls ~", "sort -o x y", "curl http://x", "python3 -c 1",
            "cat x; rm y", "echo $(id)", "git push", "cat $HOME/.claude/.credentials.json",
            "grep -r token ${HOME}/.config", "sort --compress-program=sh x", "uniq a b"):
    assert verdict(cmd) == "deny", cmd
PYEOF
ok "F6: host --read-only denies edits, writes, the web, other folders and secret files; allows reading commands"

echo "FINDINGS TEST PASSED"
