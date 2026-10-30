#!/usr/bin/env bash
#
# tests/unit_queue_test.sh — exercise `harness queue` without docker, a model
# or a tracker.
#
#   - tests/queue_test.py      scripts/ticket_queue.py end to end: a fake
#                              harness acts as each stage's agent, a fake proxy
#                              as the verifier; plan -> implement -> review ->
#                              fix -> push to a local bare remote, blocking
#                              questions and answers, retry then blocked, a
#                              made-up quote, a .github/ change, the upstream
#                              down (exit 3, the ticket keeps its place), plan
#                              review, the gates and the prompts
#   - cmd_queue                the CLI around it: help, add/list/remove through
#                              the sourced harness, the internal _sync/_pull
#                              doing nothing without a vault, run and answer
#                              refusing without one; the owner's controls
#                              (status --json, note, move, stop, continue, retry --stage,
#                              halt, add --stages/--ref/--note) passed through
#
# Prints "QUEUE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " queue unit test"
echo "============================================================"

fail() { echo "[queue-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[queue-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v git >/dev/null 2>&1 || fail "this test needs git"
command -v python3 >/dev/null 2>&1 || fail "this test needs python3"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# --- Q1: the queue itself, against a fake agent and a fake verifier ----------
out=$(python3 "$SCRIPT_DIR/queue_test.py" 2>&1) || { echo "$out" | grep -v '^20[0-9][0-9]-' >&2; fail "Q1: tests/queue_test.py"; }
grep -q '^OK' <<<"$out" || fail "Q1: no OK from tests/queue_test.py — $(tail -5 <<<"$out")"
ok "Q1: ticket_queue.py: $(grep -o 'Ran [0-9]* tests' <<<"$out")"

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT/install" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_queue)" == "function" ]] || fail "cmd_queue not sourced"
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"
mkdir -p "$state_root"

# --- Q2: help names the stages and the settings ------------------------------
out=$(cmd_queue --help)
for w in 'harness queue add' 'harness queue run' 'harness queue answer' 'needs-answers' \
         'QUEUE_MODEL' 'QUEUE_PLAN_REVIEW' 'Nothing is written to the tracker' \
         'status [--json]' '--stages LIST' '--ref REF' 'harness queue note' 'retry KEY [--stage S]' \
         'harness queue stop' 'harness queue halt' 'harness queue move' 'harness queue continue KEY'; do
    grep -qF -- "$w" <<<"$out" || fail "Q2: help should mention '$w'"
done
grep -q 'harness queue' < <(bash "$HARNESS" --help 2>/dev/null) || fail "Q2: harness --help should list queue"
ok "Q2: help covers the commands, stages and settings"

# --- Q3: add / list / remove through the CLI ---------------------------------
PROJ="$TMP_ROOT/proj"
git init -q -b main "$PROJ"
printf 'x\n' > "$PROJ/README"
git -C "$PROJ" add README
git -C "$PROJ" commit -qm init
cmd_queue add abc-7 --dir "$PROJ" >/dev/null || fail "Q3: add failed"
[[ -f "$state_root/queue/ABC-7/state.json" ]] || fail "Q3: add should write state/queue/ABC-7/state.json"
[[ "$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["project"], s["base"], s["status"])' \
      "$state_root/queue/ABC-7/state.json")" == "$PROJ main queued" ]] || fail "Q3: wrong state for ABC-7"
out=$(cmd_queue)
grep -Eq '^ABC-7 +queued +plan' <<<"$out" || fail "Q3: list should show ABC-7 queued — $out"
if cmd_queue add 2>/dev/null; then fail "Q3: add without a key accepted"; fi
if cmd_queue add ABC-8 --frob 2>/dev/null; then fail "Q3: unknown option accepted"; fi
cmd_queue remove ABC-7 >/dev/null
[[ ! -d "$state_root/queue/ABC-7" ]] || fail "Q3: remove should drop the ticket's directory"
grep -q 'the queue is empty' < <(cmd_queue list) || fail "Q3: the queue should be empty"
ok "Q3: add / list / remove"

# --- Q4: without a vault: internals do nothing, run and answer refuse --------
cmd_queue _sync ABC-7 "$PROJ" 0 || fail "Q4: _sync without a vault should succeed quietly"
cmd_queue _pull 'not a key' || fail "Q4: _pull with a bad key should succeed quietly"
err_out=$(cmd_queue run 2>&1) && fail "Q4: run without a vault accepted"
grep -q 'needs an active vault' <<<"$err_out" || fail "Q4: run should say it needs a vault — $err_out"
if cmd_queue answer ABC-7 x 2>/dev/null; then fail "Q4: answer without a vault accepted"; fi
if cmd_queue run --frob 2>/dev/null; then fail "Q4: run with an unknown option accepted"; fi
if cmd_queue frob 2>/dev/null; then fail "Q4: an unknown subcommand accepted"; fi
ok "Q4: no vault: _sync/_pull are no-ops, run/answer refuse"

# --- Q5: the owner's controls through the CLI --------------------------------
qs() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(*(s.get(k) for k in sys.argv[2:]))' \
       "$state_root/queue/$1/state.json" "${@:2}"; }
cmd_queue add ABC-1 ABC-2 --dir "$PROJ" --stages review,push --ref main --note 'keep it small' >/dev/null \
    || fail "Q5: add with --stages/--ref/--note failed"
[[ "$(qs ABC-1 stages stage start)" == "['review', 'push'] review main" ]] || fail "Q5: add options not stored — $(qs ABC-1 stages stage start)"
cmd_queue add ABC-3 --dir "$PROJ" --start main --branch feat-x --in-place --review-plan >/dev/null \
    || fail "Q5: add with --start/--branch/--in-place/--review-plan failed"
[[ "$(qs ABC-3 start branch in_place plan_review)" == "main feat-x True True" ]] \
    || fail "Q5: --start/--branch/--in-place/--review-plan not stored — $(qs ABC-3 start branch in_place plan_review)"
if cmd_queue add ABC-4 --dir "$PROJ" --branch 2>/dev/null; then fail "Q5: --branch without a value accepted"; fi
cmd_queue remove ABC-3 >/dev/null
if cmd_queue add ABC-3 --dir "$PROJ" --stages 2>/dev/null; then fail "Q5: --stages without a value accepted"; fi
if cmd_queue add ABC-3 --dir "$PROJ" --stages frob >/dev/null 2>&1; then fail "Q5: an unknown stage accepted"; fi
cmd_queue note abc-1 'second note' >/dev/null || fail "Q5: note failed"
[[ "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["notes"]))' "$state_root/queue/ABC-1/state.json")" == 2 ]] \
    || fail "Q5: the note should be in state.json"
EDITOR="sh -c 'printf \"from the editor\\n\" >> \"\$1\"' sh" cmd_queue note ABC-1 >/dev/null || fail "Q5: note from \$EDITOR failed"
grep -q 'from the editor' "$state_root/queue/ABC-1/state.json" || fail "Q5: the editor's note is missing"
cmd_queue move ABC-2 top >/dev/null || fail "Q5: move failed"
out=$(cmd_queue status --json)
[[ "$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["runner"]["running"], *(t["key"] for t in d["tickets"]))' <<<"$out")" == "False ABC-2 ABC-1" ]] \
    || fail "Q5: status --json — $out"
if cmd_queue status --frob 2>/dev/null; then fail "Q5: status with an unknown option accepted"; fi
if cmd_queue move ABC-2 sideways 2>/dev/null; then fail "Q5: move sideways accepted"; fi
cmd_queue stop ABC-1 >/dev/null || fail "Q5: stop failed"
[[ "$(qs ABC-1 status)" == stopped ]] || fail "Q5: ABC-1 should be stopped"
# continue reaches the queue (stopped with no work yet: refused with why); one key, one message
grep -q 'work is gone (or was never set up)' < <(cmd_queue continue ABC-1 'it is back' 2>&1) \
    || fail "Q5: continue should pass through to the queue and be refused"
if cmd_queue continue 2>/dev/null; then fail "Q5: continue without a key accepted"; fi
if cmd_queue continue ABC-1 a b 2>/dev/null; then fail "Q5: continue with two messages accepted"; fi
# a stage with nothing to work from is refused: no review ran, so no Must fix items
if cmd_queue retry ABC-1 --stage fix 2>/dev/null; then fail "Q5: retry --stage fix with no review accepted"; fi
cmd_queue retry ABC-1 --stage plan >/dev/null || fail "Q5: retry --stage failed"
[[ "$(qs ABC-1 status stage stages)" == "queued plan ['plan', 'review', 'push']" ]] || fail "Q5: retry --stage — $(qs ABC-1 status stage stages)"
if cmd_queue retry ABC-1 --stage 2>/dev/null; then fail "Q5: retry --stage without a stage accepted"; fi
grep -q 'no queue run is going' < <(cmd_queue halt) || fail "Q5: halt with no run should say so"
grep -q 'no queue run is going' < <(cmd_queue halt --force) || fail "Q5: halt --force with no run should say so"
cmd_queue halt --now >/dev/null 2>&1 && fail "Q5: halt takes only --force"
cmd_queue remove ABC-1 ABC-2 >/dev/null
ok "Q5: status --json, note, move, stop, continue, retry --stage, halt [--force], add --stages/--ref/--note/--start/--branch/--in-place/--review-plan"

# --- Q6: run's launch setup, with everything around it stubbed ---------------
# A container run answers the firewall question once (stages have no tty) and
# hands the script HARNESS_NET_CONFIRM=1; --single-message is the stages' own
# (a per-request header), so the shared proxy starts as it is; the script
# always gets PYTHONUTF8=1.
STUB_PY="$TMP_ROOT/py.sh"
cat > "$STUB_PY" <<'EOF2'
#!/usr/bin/env bash
printf 'utf8=%s net=%s args=%s\n' "${PYTHONUTF8:-}" "${HARNESS_NET_CONFIRM:-}" "$*" >> "$Q6_LOG"
EOF2
chmod +x "$STUB_PY"
export Q6_LOG="$TMP_ROOT/q6.log"
q6() {
    (
        vault_active_name() { echo v; }
        harness_runtime_installed() { return "$Q6_CONTAINER"; }
        host_require_config() { :; }; ensure_dirs() { :; }; host_ensure_toolchain() { :; }
        host_preflight() { :; }; _gate_on_upstream_auth() { :; }; host_proxy_running() { return 0; }
        host_confirm_gate() { echo "confirm" >> "$Q6_LOG"; }
        warn_if_firewall_open() { echo "firewall $*" >> "$Q6_LOG"; }
        host_proxy_start() { echo "proxy mode=${prompt_mode_override:-}" >> "$Q6_LOG"; }
        host_proxy_wait_ready() { :; }; host_proxy_stop() { :; }; vault_prepare() { echo "$TMP_ROOT"; }
        harness_view() { :; }; harness_view_since() { :; }; host_proxy_port() { echo 1; }
        _effective_default_model() { echo m; }; host_require_python3() { :; }
        host_python_bin() { echo "$STUB_PY"; }
        unset HARNESS_NET_CONFIRM
        cmd_queue run "$@"
    )
}
: > "$Q6_LOG"; Q6_CONTAINER=0 q6 || fail "Q6: container run failed — $(cat "$Q6_LOG")"
[[ "$(cat "$Q6_LOG")" == $'firewall 0 queue\nproxy mode=\n'"utf8=1 net=1 args="* ]] || fail "Q6: container run — $(cat "$Q6_LOG")"
: > "$Q6_LOG"; Q6_CONTAINER=0 q6 --single-message || fail "Q6: single-message run failed"
[[ "$(cat "$Q6_LOG")" == $'confirm\nproxy mode=\n'"utf8=1 net= args="*--single-message* ]] || fail "Q6: single run — $(cat "$Q6_LOG")"
: > "$Q6_LOG"; Q6_CONTAINER=1 q6 || fail "Q6: host run failed"
[[ "$(cat "$Q6_LOG")" == $'confirm\nproxy mode=\n'"utf8=1 net= args="*--host* ]] || fail "Q6: host run — $(cat "$Q6_LOG")"
ok "Q6: run: firewall gate once for containers, single mode per stage launch, PYTHONUTF8 for the script"

# --- Q7: _stop_agents stops the containers mounting a ticket's clone ---------
# Only through harness_docker, filtered by the agent and mount labels; prints
# how many it stopped, and fails (the script then warns) when docker does.
export Q7_LOG="$TMP_ROOT/q7.log"
q7() {
    (
        vault_active_name() { :; }
        harness_abs_path() { echo "/abs$1"; }
        harness_docker() {
            echo "docker $*" >> "$Q7_LOG"
            case "$1" in
                ps) [[ "$Q7_PS" == fail ]] && return 1; printf '%s\n' $Q7_PS ;;
                stop) [[ "$2" != bad ]] ;;
            esac
        }
        cmd_queue _stop_agents "$@"
    )
}
: > "$Q7_LOG"; out=$(Q7_PS="c1 bad c2" q7 /q/ABC-1/repo) || fail "Q7: _stop_agents failed"
[[ "$out" == 2 ]] || fail "Q7: expected 2 stopped, got '$out'"
grep -qF -- "docker ps -q --filter label=harness.agent=true --filter label=harness.mount=/abs/q/ABC-1/repo" "$Q7_LOG" \
    || fail "Q7: ps filter — $(cat "$Q7_LOG")"
[[ "$(grep -c '^docker stop' "$Q7_LOG")" == 3 ]] || fail "Q7: stops — $(cat "$Q7_LOG")"
out=$(Q7_PS="" q7 /q/ABC-1/repo) && [[ "$out" == 0 ]] || fail "Q7: none running gave '$out'"
if Q7_PS=fail q7 /q/ABC-1/repo >/dev/null; then fail "Q7: a docker failure must fail"; fi
# With a launch id (an in-place ticket shares the folder with the owner's agents): only that launch's.
: > "$Q7_LOG"; Q7_PS="c1" q7 /proj abc123 >/dev/null || fail "Q7: _stop_agents with a launch id failed"
grep -qF -- "docker ps -q --filter label=harness.agent=true --filter label=harness.mount=/abs/proj --filter label=harness.queue-launch=abc123" "$Q7_LOG" \
    || fail "Q7: launch filter — $(cat "$Q7_LOG")"
ok "Q7: _stop_agents: by the clone's mount label (and launch id), through harness_docker, counts what stopped"

# --- Q8: a run harness upgrade ended (75) starts again on the new code -------
# harness queue run execs the clone's harness with the same options; a proxy
# this run started stays its own (HARNESS_QUEUE_OWNS_PROXY), so the new run
# still stops it at the end, and nothing stops it in between.
Q8="$TMP_ROOT/q8"; mkdir -p "$Q8/clone"
export Q8_LOG="$Q8/log"
printf '#!/usr/bin/env bash\necho "75" >> "$Q8_LOG"; exit 75\n' > "$Q8/py.sh"
printf '#!/usr/bin/env bash\necho "exec $* owns=${HARNESS_QUEUE_OWNS_PROXY:-}" >> "$Q8_LOG"\n' > "$Q8/clone/harness"
chmod +x "$Q8/py.sh" "$Q8/clone/harness"
q8() {
    (
        clone_dir="$Q8/clone"
        vault_active_name() { echo v; }
        harness_runtime_installed() { return 1; }
        host_require_config() { :; }; ensure_dirs() { :; }; host_ensure_toolchain() { :; }
        host_preflight() { :; }; _gate_on_upstream_auth() { :; }; host_confirm_gate() { :; }
        host_proxy_running() { return "$Q8_UP"; }
        host_proxy_start() { :; }; host_proxy_wait_ready() { :; }
        host_proxy_stop() { echo "proxy stopped" >> "$Q8_LOG"; }
        vault_prepare() { echo "$TMP_ROOT"; }
        harness_view() { :; }; harness_view_since() { :; }; host_proxy_port() { echo 1; }
        _effective_default_model() { echo m; }; host_require_python3() { :; }
        host_python_bin() { echo "$Q8/py.sh"; }
        cmd_queue run "$@"
    )
}
: > "$Q8_LOG"; Q8_UP=1 q8 --host || fail "Q8: the restarted run failed — $(cat "$Q8_LOG")"
[[ "$(cat "$Q8_LOG")" == $'75\nexec queue run --host owns=1' ]] || fail "Q8: own proxy — $(cat "$Q8_LOG")"
: > "$Q8_LOG"; Q8_UP=0 HARNESS_QUEUE_OWNS_PROXY=1 q8 || fail "Q8: the restarted run failed — $(cat "$Q8_LOG")"
[[ "$(cat "$Q8_LOG")" == $'75\nexec queue run owns=1' ]] || fail "Q8: a restarted run keeps owning its proxy — $(cat "$Q8_LOG")"
: > "$Q8_LOG"; Q8_UP=0 q8 || fail "Q8: the restarted run failed — $(cat "$Q8_LOG")"
[[ "$(cat "$Q8_LOG")" == $'75\nexec queue run owns=' ]] || fail "Q8: a proxy it did not start — $(cat "$Q8_LOG")"
ok "Q8: a run ended for an upgrade (75) execs harness queue run again, its proxy kept and still its own"

# --- Q9: add --new: a ticket of your own is made in the vault and queued -----
Q9V="$state_root/vaults/q9vault"
mkdir -p "$Q9V/.raw/tickets" "$state_root/queue/LOCAL-2"
(
    vault_active_name() { echo q9vault; }
    cmd_queue add --new $'fix the flaky login test\nit fails one run in ten' --new 'tidy the readme' --dir "$PROJ" >/dev/null \
        || fail "Q9: add --new failed"
    # LOCAL-2 is a folder in the queue already: the two made are LOCAL-1 and LOCAL-3.
    for k in LOCAL-1 LOCAL-3; do
        [[ -f "$state_root/queue/$k/state.json" && -f "$Q9V/.raw/tickets/$k.md" ]] || fail "Q9: $k should be queued and in the vault"
    done
    grep -q '^source: local$' "$Q9V/.raw/tickets/LOCAL-1.md" && grep -q '^# LOCAL-1: fix the flaky login test$' "$Q9V/.raw/tickets/LOCAL-1.md" \
        && grep -q '^it fails one run in ten$' "$Q9V/.raw/tickets/LOCAL-1.md" || fail "Q9: the copy — $(cat "$Q9V/.raw/tickets/LOCAL-1.md")"
    # An add that fails takes its new ticket out of the vault again.
    if cmd_queue add --new 'never queued' --dir "$PROJ" --stages frob >/dev/null 2>&1; then fail "Q9: an unknown stage accepted"; fi
    [[ ! -e "$Q9V/.raw/tickets/LOCAL-4.md" && ! -d "$state_root/queue/LOCAL-4" ]] || fail "Q9: a failed add should leave no LOCAL-4"
    if cmd_queue add --new ' ' --dir "$PROJ" 2>/dev/null; then fail "Q9: --new with no text accepted"; fi
    vault_active_name() { :; }
    if cmd_queue add --new 'x' --dir "$PROJ" 2>/dev/null; then fail "Q9: --new with no vault accepted"; fi
) || exit 1
ok "Q9: add --new makes LOCAL-N tickets (skipping keys the queue has) and queues them; a failed add takes them out"

echo "QUEUE TEST PASSED"
