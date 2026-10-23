#!/usr/bin/env bash
#
# tests/unit_vault_test.sh — exercise `harness vault` and the launch-time vault
# helpers without docker or network: the "remote" is a local bare git repo.
#
#   - cmd_vault add/list/use/off/remove   registry + active-vault bookkeeping
#   - cmd_vault new                       a new vault from vault-template/ or a
#                                         template repo, with a fresh history
#   - vault_prepare                       clones on first use, fast-forwards
#                                         after, warns and keeps the local copy
#                                         when the remote is unreachable
#   - vault_opencode_config               instructions + external_directory
#   - harness_opencode_overlay            merges with the --slim overlay
#   - vault_sync + scripts/vault_capture.py
#                                         copy the session's conversation into
#                                         .raw/sessions/, commit, push (rebasing
#                                         once on a moved remote)
#
# Prints "VAULT TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " vault unit test"
echo "============================================================"

fail() { echo "[vault-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[vault-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v jq >/dev/null 2>&1 || fail "this test needs host jq"
command -v git >/dev/null 2>&1 || fail "this test needs git"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT/install" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_vault)" == "function" ]] || fail "cmd_vault not sourced"
# Sourced from a test, clone_dir resolves off $0 and platform.sh is skipped;
# point both at the repo.
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"
mkdir -p "$state_root"

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# A "remote" with one commit carrying AGENTS.md.
REMOTE="$TMP_ROOT/brain.git"
WORK="$TMP_ROOT/work"
git init -q --bare "$REMOTE"
git clone -q "$REMOTE" "$WORK" 2>/dev/null
printf 'vault rules\n' > "$WORK/AGENTS.md"
git -C "$WORK" add AGENTS.md
git -C "$WORK" commit -qm one
git -C "$WORK" push -q origin HEAD 2>/dev/null

# --- V1: add registers and the first vault becomes active --------------------
cmd_vault add brain "$REMOTE" >/dev/null || fail "V1: add failed"
[[ "$(vault_active_name)" == "brain" ]] || fail "V1: first vault should be active"
out=$(cmd_vault)
grep -q '^\* brain  .*(not cloned yet)' <<<"$out" || fail "V1: list should mark brain active and uncloned — $out"
ok "V1: add registers and activates the first vault"

# --- V2: duplicate name and bad names are refused ----------------------------
if cmd_vault add brain "$REMOTE" >/dev/null 2>&1; then fail "V2: duplicate add accepted"; fi
if cmd_vault add ../x "$REMOTE" >/dev/null 2>&1; then fail "V2: path-like name accepted"; fi
if cmd_vault add .hidden "$REMOTE" >/dev/null 2>&1; then fail "V2: dot-leading name accepted"; fi
cmd_vault add other "$REMOTE" >/dev/null || fail "V2: second add failed"
[[ "$(vault_active_name)" == "brain" ]] || fail "V2: a second add must not change the active vault"
ok "V2: duplicate/bad names refused; later adds keep the active vault"

# --- V3: prepare clones on first use, then fast-forwards ---------------------
dir=$(vault_prepare 2>/dev/null)
[[ "$dir" == "$(harness_abs_path "$state_root/vaults/brain")" ]] || fail "V3: prepare should echo the clone path — '$dir'"
[[ -f "$dir/AGENTS.md" ]] || fail "V3: clone is missing AGENTS.md"
printf 'more\n' > "$WORK/notes.md"
git -C "$WORK" add notes.md
git -C "$WORK" commit -qm two
git -C "$WORK" push -q origin HEAD 2>/dev/null
dir=$(vault_prepare 2>/dev/null)
[[ -f "$dir/notes.md" ]] || fail "V3: second prepare should fast-forward the clone"
ok "V3: prepare clones, then fast-forwards"

# --- V4: unreachable remote warns and keeps the local copy -------------------
mv "$REMOTE" "$REMOTE.away"
err_out=$(vault_prepare 2>&1 >/dev/null)
dir=$(vault_prepare 2>/dev/null)
mv "$REMOTE.away" "$REMOTE"
[[ -n "$dir" ]] || fail "V4: a failed pull must still return the local clone"
grep -q 'using the local copy' <<<"$err_out" || fail "V4: failed pull should warn — $err_out"
ok "V4: unreachable remote keeps the local copy"

# --- V5: the overlay points opencode at the vault ----------------------------
harness_vault_dir="$dir"
cfg=$(vault_opencode_config)
[[ "$(jq -r '.instructions[0]' <<<"$cfg")" == "$dir/AGENTS.md" ]] || fail "V5: instructions should name AGENTS.md — $cfg"
[[ "$(jq -r --arg k "$dir/*" '.permission.external_directory[$k]' <<<"$cfg")" == "allow" ]] \
    || fail "V5: external_directory should allow the vault — $cfg"
harness_slim=1
merged=$(harness_opencode_overlay)
jq -e '.provider.harness.options.headers["x-harness-slim"] == "1" and (.instructions | length == 1) and .permission.external_directory' \
    <<<"$merged" >/dev/null || fail "V5: slim + vault overlays should merge — $merged"
harness_slim=0
[[ "$(harness_opencode_overlay)" == "$cfg" ]] || fail "V5: without slim the overlay is the vault part alone"
mv "$dir/AGENTS.md" "$dir/AGENTS.away"
cfg=$(vault_opencode_config 2>/dev/null)
mv "$dir/AGENTS.away" "$dir/AGENTS.md"
jq -e 'has("instructions") | not' <<<"$cfg" >/dev/null || fail "V5: no AGENTS.md/CLAUDE.md means no instructions — $cfg"
harness_vault_dir=""
[[ -z "$(harness_opencode_overlay)" ]] || fail "V5: no vault and no slim means no overlay"
ok "V5: overlay carries instructions + external_directory, merges with slim"

# --- V6: use / off / remove --------------------------------------------------
cmd_vault use other >/dev/null || fail "V6: use failed"
[[ "$(vault_active_name)" == "other" ]] || fail "V6: use should switch the active vault"
if cmd_vault use nope >/dev/null 2>&1; then fail "V6: use of an unknown vault accepted"; fi
cmd_vault off >/dev/null
[[ -z "$(vault_active_name)" ]] || fail "V6: off should clear the active vault"
[[ -z "$(vault_prepare 2>/dev/null)" ]] || fail "V6: prepare with no active vault should print nothing"
cmd_vault use brain >/dev/null
cmd_vault remove brain >/dev/null || fail "V6: remove failed"
[[ -z "$(vault_active_name)" ]] || fail "V6: removing the active vault should clear it"
[[ -d "$state_root/vaults/brain/.git" ]] || fail "V6: remove must keep the clone"
_vault_url brain >/dev/null && fail "V6: brain still registered"
_vault_url other >/dev/null || fail "V6: remove dropped the wrong vault"
ok "V6: use/off/remove keep the registry consistent and never delete a clone"

# --- V7: new makes a vault from vault-template/ or a template repo -----------
cmd_vault use other >/dev/null
out=$(cmd_vault new fresh) || fail "V7: new failed — $out"
fresh="$state_root/vaults/fresh"
for f in AGENTS.md wiki/index.md wiki/log.md .raw/.gitkeep pub/.gitkeep .gitignore; do
    [[ -f "$fresh/$f" ]] || fail "V7: new vault is missing $f"
done
[[ "$(git -C "$fresh" rev-list --count HEAD)" == "1" ]] || fail "V7: new vault should have one commit"
[[ -z "$(git -C "$fresh" status --porcelain)" ]] || fail "V7: new vault should be committed clean"
[[ "$(_vault_url fresh)" == "local" ]] || fail "V7: new vault should be registered as local"
[[ "$(vault_active_name)" == "other" ]] || fail "V7: new must not change an active vault"
grep -q '^  fresh  local, no remote  ' <<<"$(cmd_vault)" || fail "V7: list should show a local vault with no remote — $(cmd_vault)"
cmd_vault use fresh >/dev/null
err_out=$(vault_prepare 2>&1 >/dev/null)
[[ -z "$err_out" ]] || fail "V7: prepare of a vault with no upstream should not warn — $err_out"
[[ "$(vault_prepare 2>/dev/null)" == "$(harness_abs_path "$fresh")" ]] || fail "V7: prepare should return the new vault"
git -C "$fresh" remote add origin "$REMOTE"
grep -q "^\* fresh  $REMOTE  " <<<"$(cmd_vault)" || fail "V7: list should show the origin once one is set — $(cmd_vault)"
out=$(cmd_vault new tpl --from "file://$REMOTE") || fail "V7: new --from failed — $out"
tpl="$state_root/vaults/tpl"
[[ -f "$tpl/AGENTS.md" && -f "$tpl/notes.md" ]] || fail "V7: --from should copy the template's files"
[[ "$(git -C "$tpl" rev-list --count HEAD)" == "1" ]] || fail "V7: --from should start a fresh history"
git -C "$tpl" remote get-url origin >/dev/null 2>&1 && fail "V7: --from must not keep the template as origin"
if cmd_vault new fresh >/dev/null 2>&1; then fail "V7: new over a registered name accepted"; fi
cmd_vault remove tpl >/dev/null
if cmd_vault new tpl >/dev/null 2>&1; then fail "V7: new over an existing folder accepted"; fi
if cmd_vault new gone --from "$TMP_ROOT/nope.git" >/dev/null 2>&1; then fail "V7: new from a bad url accepted"; fi
[[ ! -e "$state_root/vaults/gone" ]] || fail "V7: a failed new should leave no folder"
_vault_url gone >/dev/null && fail "V7: a failed new should not register"
if cmd_vault new x --bogus >/dev/null 2>&1; then fail "V7: unknown option accepted"; fi
if cmd_vault add y local >/dev/null 2>&1; then fail "V7: add with the url 'local' accepted"; fi
mv "$fresh" "$fresh.away"
err_out=$(vault_prepare 2>&1 >/dev/null)
[[ -z "$(vault_prepare 2>/dev/null)" ]] || fail "V7: a local vault whose folder is gone should launch without it"
mv "$fresh.away" "$fresh"
grep -q "is gone" <<<"$err_out" || fail "V7: a missing local vault should say so — $err_out"
ok "V7: new makes a committed vault from the template or a repo, and launches use it"

# --- V8: vault_sync captures the session, commits and pushes -----------------
PY=$(host_python_bin) || fail "V8: this test needs python 3"
DB="$TMP_ROOT/opencode.db"
# shellcheck disable=SC2086
$PY - "$DB" <<'PYEOF' || fail "V8: could not build the fixture database"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, time_created INT,
  time_updated INT, tokens_input INT, tokens_output INT);
CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INT, data TEXT);
""")
c.execute("INSERT INTO session VALUES ('ses_new', NULL, '/w', 'Fix the bug', 1761100000000, 1761100500000, 900, 40)")
c.execute("INSERT INTO session VALUES ('ses_sub', 'ses_new', '/w', 'subagent', 1761100100000, 1761100500000, 1, 1)")
c.execute("INSERT INTO session VALUES ('ses_old', NULL, '/w', 'Old', 1, 2, 1, 1)")
# Another project's session, same time, same database: never captured.
c.execute("INSERT INTO session VALUES ('ses_else', NULL, '/wx', 'Other', 1761100000000, 1761100500000, 1, 1)")
msgs = [("m1", "ses_new", "user", [{"type": "text", "text": "fix it, key sk-abcdefghijklmnopqrstuvwx"},
                                    {"type": "text", "text": "INJECTED", "synthetic": True}]),
        ("m2", "ses_new", "assistant", [{"type": "reasoning", "text": "THINKING"},
                                         {"type": "tool", "tool": "read", "state": {"output": "TOOLOUT"}},
                                         {"type": "tool", "tool": "read"}, {"type": "tool", "tool": "edit"},
                                         {"type": "text", "text": "Fixed in a.py"}]),
        ("m3", "ses_sub", "user", [{"type": "text", "text": "SUBAGENT"}]),
        ("m4", "ses_old", "user", [{"type": "text", "text": "OLD"}]),
        ("m5", "ses_else", "user", [{"type": "text", "text": "ELSEWHERE"}])]
for i, (mid, sid, role, parts) in enumerate(msgs):
    c.execute("INSERT INTO message VALUES (?,?,?,?)", (mid, sid, i, json.dumps({"role": role})))
    for j, p in enumerate(parts):
        c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"{mid}p{j}", mid, sid, j, json.dumps(p)))
c.commit()
PYEOF
SYNC="$TMP_ROOT/sync"
git clone -q "$REMOTE" "$SYNC" 2>/dev/null
printf 'agent note\n' > "$SYNC/note.md"
out=$(vault_sync "$SYNC" 1761100000000 "$DB" /w 2>&1) || fail "V8: vault_sync failed — $out"
cap="$SYNC/.raw/sessions/2025-10-22-ses_new.md"
[[ -f "$cap" ]] || fail "V8: no capture at $cap — $(ls -R "$SYNC/.raw" 2>&1)"
[[ "$(ls "$SYNC/.raw/sessions")" == "2025-10-22-ses_new.md" ]] || fail "V8: only this project's new root session belongs there — $(ls "$SYNC/.raw/sessions")"
grep -q '^title: "Fix the bug"$' "$cap" && grep -q 'Fixed in a.py' "$cap" && grep -q '_tools: read x2, edit_' "$cap" \
    || fail "V8: capture lacks the title, the reply or the tool list — $(cat "$cap")"
grep -q 'sk-abcdefghij\|INJECTED\|THINKING\|TOOLOUT' "$cap" && fail "V8: capture kept a key, injected text, reasoning or tool output — $(cat "$cap")"
[[ -z "$(git -C "$SYNC" status --porcelain)" ]] || fail "V8: vault left uncommitted changes"
[[ "$(git -C "$SYNC" rev-parse HEAD)" == "$(git --git-dir="$REMOTE" rev-parse HEAD)" ]] || fail "V8: commit was not pushed — $out"
grep -q "pushed" <<<"$out" || fail "V8: should say it pushed — $out"
# The remote moved on meanwhile: the push is rebased once and goes through.
OTHER="$TMP_ROOT/other"
git clone -q "$REMOTE" "$OTHER" 2>/dev/null
printf 'x\n' > "$OTHER/other.md"; git -C "$OTHER" add other.md; git -C "$OTHER" commit -qm other
git -C "$OTHER" push -q 2>/dev/null
printf 'more\n' >> "$SYNC/note.md"
out=$(vault_sync "$SYNC" 1761100000000 "$DB" /w 2>&1)
grep -q "after rebasing" <<<"$out" || fail "V8: a moved remote should be rebased on — $out"
[[ "$(git -C "$SYNC" rev-parse HEAD)" == "$(git --git-dir="$REMOTE" rev-parse HEAD)" ]] || fail "V8: rebased commit not pushed"
# Nothing changed: no commit. No vault: a no-op.
before=$(git -C "$SYNC" rev-parse HEAD)
vault_sync "$SYNC" 1761100000000 "$DB" /w >/dev/null 2>&1
[[ "$(git -C "$SYNC" rev-parse HEAD)" == "$before" ]] || fail "V8: a rerun with nothing new should not commit"
vault_sync "" 0 "$DB" /w || fail "V8: no vault should be a no-op"
ok "V8: vault_sync captures this project's session text (redacted), commits and pushes, rebasing once"

echo "VAULT TEST PASSED"
