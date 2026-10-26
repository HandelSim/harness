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
#                              refusing without one
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
         'QUEUE_MODEL' 'QUEUE_PLAN_REVIEW' 'Nothing is written to the tracker'; do
    grep -qF "$w" <<<"$out" || fail "Q2: help should mention '$w'"
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

echo "QUEUE TEST PASSED"
