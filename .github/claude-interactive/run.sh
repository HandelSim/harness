#!/usr/bin/env bash
# run.sh — drive an INTERACTIVE Claude Code session inside a GitHub Actions
# runner via tmux, so usage bills against the subscription's interactive pool
# rather than the `claude -p` / Agent-SDK monthly credit.
#
# The billing discriminator (`cc_entrypoint`) is computed by the claude binary
# from invocation mode + TTY presence and is server-validated, so the ONLY way
# to draw the interactive pool is to run the genuine interactive REPL. tmux
# gives the process a real PTY; we drive it with send-keys / capture-pane the
# same way oak drives its workers.
#
# Inputs (env, set by the workflow):
#   CLAUDE_CODE_OAUTH_TOKEN  subscription OAuth token (from `claude setup-token`)
#   GH_TOKEN                 token used by `gh` to read the issue and comment
#   GITHUB_REPOSITORY        owner/repo
#   ISSUE_NUMBER             issue to respond to
#   CLAUDE_MODEL             optional, default claude-opus-4-7
#   GITHUB_WORKSPACE         repo checkout dir (cwd for the agent)
set -euo pipefail

: "${CLAUDE_CODE_OAUTH_TOKEN:?missing CLAUDE_CODE_OAUTH_TOKEN}"
: "${GH_TOKEN:?missing GH_TOKEN}"
: "${GITHUB_REPOSITORY:?missing GITHUB_REPOSITORY}"
: "${ISSUE_NUMBER:?missing ISSUE_NUMBER}"

REPO="$GITHUB_REPOSITORY"
MODEL="${CLAUDE_MODEL:-claude-opus-4-7}"
WORKDIR="${GITHUB_WORKSPACE:-$PWD}"
SESSION="claude-ci"
SID="$(cat /proc/sys/kernel/random/uuid)"
DONE="/tmp/claude-done.$SID"
PANE_LOG="/tmp/claude-pane.$SID.log"
PROMPT_FILE="/tmp/claude-task.$SID.md"
RESPONSE_FILE="/tmp/claude-response.$SID.md"
READY_TIMEOUT="${READY_TIMEOUT:-180}"
RUN_TIMEOUT="${RUN_TIMEOUT:-1500}"

log() { printf '>>> %s\n' "$*"; }

dump_diag() {
  log "----- tmux pane (tail) -----"
  tmux capture-pane -p -t "$SESSION" -S -300 2>/dev/null || true
  log "----- pipe-pane log (tail) -----"
  tail -n 200 "$PANE_LOG" 2>/dev/null || true
}

# --- 1. tooling -------------------------------------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  log "installing tmux"
  sudo apt-get update -qq && sudo apt-get install -y -qq tmux
fi
if ! command -v claude >/dev/null 2>&1; then
  log "installing claude code"
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
CLAUDE_BIN="$(command -v claude)"
log "claude: $CLAUDE_BIN $("$CLAUDE_BIN" --version 2>&1 | head -1)"

# --- 2. pre-seed ~/.claude so the REPL never blocks on a dialog -------------
# A fresh runner HOME would otherwise stop at onboarding / the workspace-trust
# dialog (which --dangerously-skip-permissions does NOT bypass), hanging the job.
mkdir -p "$HOME/.claude"
cat > "$HOME/.claude/settings.json" <<EOF
{
  "hooks": {
    "Stop": [
      { "matcher": "", "hooks": [ { "type": "command", "command": "touch '$DONE'" } ] }
    ]
  }
}
EOF

CFG="$HOME/.claude.json"
[ -f "$CFG" ] || echo '{}' > "$CFG"
tmp="$(mktemp)"
jq --arg p "$WORKDIR" '
  .hasCompletedOnboarding = true
  | .projects[$p].hasTrustDialogAccepted = true
  | .projects[$p].hasCompletedProjectOnboarding = true
  | .projects[$p].hasClaudeMdExternalIncludesApproved = true
  | .projects[$p].hasClaudeMdExternalIncludesWarningShown = true
' "$CFG" > "$tmp" && mv "$tmp" "$CFG"

# --- 3. build the task prompt from the issue --------------------------------
log "reading issue #$ISSUE_NUMBER"
ISSUE_JSON="$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json title,body,author,labels)"
TITLE="$(jq -r '.title' <<<"$ISSUE_JSON")"
BODY="$(jq -r '.body' <<<"$ISSUE_JSON")"
AUTHOR="$(jq -r '.author.login' <<<"$ISSUE_JSON")"

cat > "$PROMPT_FILE" <<EOF
You are responding to GitHub issue #$ISSUE_NUMBER in the repository $REPO,
opened by @$AUTHOR.

Title: $TITLE

Body:
$BODY

Write a helpful, correct response to this issue. Your entire final message
will be posted verbatim as a comment on the issue, so write it as a direct
reply to the author in GitHub-flavored markdown. Do NOT run git or gh
commands and do NOT open a pull request; just produce the reply text.
EOF

# --- 4. launch the interactive REPL in tmux (real PTY) ----------------------
rm -f "$DONE"
export CLAUDE_CODE_OAUTH_TOKEN GH_TOKEN
LAUNCH="$CLAUDE_BIN --dangerously-skip-permissions --disallowed-tools AskUserQuestion,ExitPlanMode,EnterPlanMode --model=$MODEL --session-id $SID --add-dir $WORKDIR --add-dir /tmp"
log "launching: $LAUNCH"
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -x 220 -y 50 -c "$WORKDIR" "$LAUNCH"
tmux pipe-pane -t "$SESSION" -o "cat >> '$PANE_LOG'"

# --- 5. wait for the REPL to accept input -----------------------------------
ready=0
for _ in $(seq 1 "$READY_TIMEOUT"); do
  if tmux capture-pane -p -t "$SESSION" 2>/dev/null \
       | grep -qE '(^[>❯] *$|Welcome to Claude|bypass permissions on)'; then
    ready=1; break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  log "REPL did not become ready within ${READY_TIMEOUT}s (auth failure or a blocking prompt)"
  dump_diag
  exit 1
fi
log "REPL ready"

# --- 6. send the task and wait for turn completion (Stop hook sentinel) -----
tmux send-keys -t "$SESSION" "Read the file $PROMPT_FILE and follow its instructions."
sleep 0.5
tmux send-keys -t "$SESSION" Enter
log "prompt sent; waiting for completion"

done_ok=0
for _ in $(seq 1 "$RUN_TIMEOUT"); do
  [ -f "$DONE" ] && { done_ok=1; break; }
  sleep 1
done
if [ "$done_ok" != 1 ]; then
  log "agent did not finish within ${RUN_TIMEOUT}s"
  dump_diag
  exit 1
fi
log "turn complete"

# --- 7. extract the final assistant message ---------------------------------
transcript="$(find "$HOME/.claude/projects" -name "$SID.jsonl" 2>/dev/null | head -1)"
if [ -z "$transcript" ]; then
  log "transcript for session $SID not found"
  dump_diag
  exit 1
fi
log "transcript: $transcript"
# last assistant message's text blocks, joined
jq -rs '
  map(select(.type=="assistant")) | last
  | (.message.content // []) | map(select(.type=="text") | .text) | join("\n\n")
' "$transcript" > "$RESPONSE_FILE" || true
if [ ! -s "$RESPONSE_FILE" ]; then
  # fallback: every assistant text block across the turn
  jq -rs '
    [ .[] | select(.type=="assistant") | (.message.content // [])[]
      | select(.type=="text") | .text ] | join("\n\n")
  ' "$transcript" > "$RESPONSE_FILE" || true
fi
if [ ! -s "$RESPONSE_FILE" ]; then
  log "could not extract a response from the transcript"
  dump_diag
  exit 1
fi
log "response (${RESPONSE_FILE}):"
cat "$RESPONSE_FILE"

# --- 8. post the comment ----------------------------------------------------
gh issue comment "$ISSUE_NUMBER" --repo "$REPO" --body-file "$RESPONSE_FILE"
log "posted comment to issue #$ISSUE_NUMBER"

tmux kill-session -t "$SESSION" 2>/dev/null || true
log "OK"
