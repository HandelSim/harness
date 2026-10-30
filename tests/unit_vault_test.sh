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
#   - vault_opencode_config               instructions + external_directory +
#                                         the x-harness-vault header
#   - harness_opencode_overlay            merges with the --slim overlay
#   - vault_sync + scripts/vault_capture.py
#                                         copy the session's conversation into
#                                         .raw/sessions/, commit, push (rebasing
#                                         once on a moved remote), with a line
#                                         per tool call and every message from
#                                         before a compaction
#   - scripts/vault_ingest.py             against a mock upstream: pages,
#                                         index, hubs, incremental runs,
#                                         retry and failure
#   - tickets                             scripts/vault_ticket.py against a
#                                         mock tracker, --ticket per folder,
#                                         the launch's instruction files,
#                                         ticket and context in captured
#                                         sessions, the ticket hub
#   - domain documents                    md/html/docx (a bad pdf skipped),
#                                         quote-checked Terms, Rules and Keep
#                                         lines, the glossary, a ticket's
#                                         context page, the candidates
#   - tickets of your own                 vault_ticket.py --new and harness
#                                         ticket new: LOCAL-N, never pulled
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
[[ "$(jq -r '.provider.harness.options.headers["x-harness-vault"]' <<<"$cfg")" == "$dir" ]] \
    || fail "V5: the x-harness-vault header should carry the vault path — $cfg"
harness_slim=1
merged=$(harness_opencode_overlay)
jq -e --arg d "$dir" '.provider.harness.options.headers["x-harness-slim"] == "1"
        and .provider.harness.options.headers["x-harness-vault"] == $d
        and (.instructions | length == 1) and .permission.external_directory' \
    <<<"$merged" >/dev/null || fail "V5: slim + vault overlays should merge — $merged"
harness_slim=0
[[ "$(harness_opencode_overlay)" == "$cfg" ]] || fail "V5: without slim the overlay is the vault part alone"
mv "$dir/AGENTS.md" "$dir/AGENTS.away"
cfg=$(vault_opencode_config 2>/dev/null)
mv "$dir/AGENTS.away" "$dir/AGENTS.md"
jq -e 'has("instructions") | not' <<<"$cfg" >/dev/null || fail "V5: no AGENTS.md/CLAUDE.md means no instructions — $cfg"
# A Windows host launch gets C:/... paths; a container launch keeps /c/...
cygpath() { [[ "$1" == -m ]] && printf 'C:%s' "$2"; }
real_detect_os=$(declare -f harness_detect_os)
harness_detect_os() { echo windows; }
win=$(harness_native_paths=1 vault_opencode_config)
jq -e --arg d "C:$dir" '.permission.external_directory[$d + "/*"] == "allow"
        and .instructions[0] == ($d + "/AGENTS.md")
        and .provider.harness.options.headers["x-harness-vault"] == $d' \
    <<<"$win" >/dev/null || fail "V5: a Windows host launch should get C:/ paths — $win"
jq -e --arg d "$dir" '.instructions[0] == ($d + "/AGENTS.md")' <<<"$(vault_opencode_config)" >/dev/null \
    || fail "V5: a container launch should keep the vault path as is"
unset -f cygpath harness_detect_os
eval "$real_detect_os"
harness_vault_dir=""
[[ -z "$(harness_opencode_overlay)" ]] || fail "V5: no vault and no slim means no overlay"
ok "V5: overlay carries instructions + external_directory, merges with slim; C:/ paths on a Windows host"

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
for f in AGENTS.md wiki/index.md wiki/log.md .raw/.gitkeep pub/.gitkeep .gitignore .gitattributes; do
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
                                         {"type": "tool", "tool": "read", "state": {"status": "completed",
                                          "input": {"filePath": "/w/src/a.py"}, "output": "TOOLOUT"}},
                                         {"type": "tool", "tool": "grep", "state": {"input": {"pattern": "def run",
                                          "path": "/w/src", "include": "*.py"}}},
                                         {"type": "tool", "tool": "bash", "state": {"input": {"command": "pytest -q"}}},
                                         {"type": "tool", "tool": "edit", "state": {"status": "error",
                                          "input": {"filePath": "/w/a.py"}}},
                                         {"type": "tool", "tool": "task", "state": {"status": "completed",
                                          "input": {"description": "find the config", "subagent_type": "explore"},
                                          "output": "It is in conf.yaml"}},
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
grep -q '^title: "Fix the bug"$' "$cap" && grep -q 'Fixed in a.py' "$cap" \
    || fail "V8: capture lacks the title or the reply — $(cat "$cap")"
for line in '- read `src/a.py`' '- grep `def run` in `src` (*.py)' '- bash `pytest -q`' '- edit `a.py` (failed)' \
        '- task (explore): find the config' '  > It is in conf.yaml'; do
    grep -qxF -- "$line" "$cap" || fail "V8: capture lacks the tool line '$line' — $(cat "$cap")"
done
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
# A signing key (a smart card asks for its PIN) is never used for harness's
# commits; gpg.program=false stands in for the card.
git -C "$SYNC" config commit.gpgsign true; git -C "$SYNC" config gpg.program false
printf 'signed?\n' >> "$SYNC/note.md"
out=$(vault_sync "$SYNC" 1761100000000 "$DB" /w 2>&1)
grep -q "committed" <<<"$out" || fail "V8: a signing setup should not stop the vault commit — $out"
git -C "$SYNC" cat-file commit HEAD | grep -q '^gpgsig' && fail "V8: the vault commit should not be signed"
# An agent launched by harness host gets the same through the environment.
(export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.abbrev GIT_CONFIG_VALUE_0=9; harness_git_nosign_env
 [[ $GIT_CONFIG_COUNT == 3 && $GIT_CONFIG_KEY_0 == core.abbrev && $GIT_CONFIG_KEY_1 == commit.gpgsign ]] \
     || fail "V8: harness_git_nosign_env should append after the caller's entries ($GIT_CONFIG_COUNT)"
 printf 'agent\n' >> "$SYNC/note.md"
 git -C "$SYNC" -c user.name=t -c user.email=t@x commit -qam agent \
     || fail "V8: under harness_git_nosign_env a commit should not use the signing key") || exit 1
git -C "$SYNC" config --unset commit.gpgsign; git -C "$SYNC" config --unset gpg.program
vault_sync "$SYNC" 1761100000000 "$DB" /w >/dev/null 2>&1
ok "V8: vault_sync captures this project's session text (redacted), commits and pushes, rebasing once"

# --- V9: a compacted session keeps everything from before each compaction ----
DB2="$TMP_ROOT/compacted.db"
# shellcheck disable=SC2086
$PY - "$DB2" <<'PYEOF' || fail "V9: could not build the fixture database"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, time_created INT,
  time_updated INT, tokens_input INT, tokens_output INT);
CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INT, data TEXT);
""")
c.execute("INSERT INTO session VALUES ('ses_long', NULL, '/w', 'Long', 1761100000000, 1761100500000, 1, 1)")
# What opencode stores for two compactions: a user message with a compaction
# part, then an assistant message with summary: true; nothing is deleted.
msgs = [("user", {}, [{"type": "text", "text": "FIRST ASK"}]),
        ("assistant", {}, [{"type": "text", "text": "FIRST ANSWER"}]),
        ("user", {"summary": {"diffs": []}}, [{"type": "compaction", "auto": True}]),
        ("assistant", {"summary": True}, [{"type": "text", "text": "SUMMARY ONE"}]),
        ("user", {}, [{"type": "text", "text": "SECOND ASK"}]),
        ("user", {}, [{"type": "compaction", "auto": True}]),
        ("assistant", {"summary": True}, [{"type": "text", "text": "SUMMARY TWO"}]),
        ("user", {}, [{"type": "text", "text": "THIRD ASK"}])]
for i, (role, extra, parts) in enumerate(msgs):
    c.execute("INSERT INTO message VALUES (?,?,?,?)", (f"m{i}", "ses_long", i, json.dumps(dict(role=role, **extra))))
    for j, p in enumerate(parts):
        c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"m{i}p{j}", f"m{i}", "ses_long", j, json.dumps(p)))
c.commit()
PYEOF
CAP2="$TMP_ROOT/cap2"
# shellcheck disable=SC2086
$PY "$REPO_ROOT/scripts/vault_capture.py" "$DB2" "$CAP2" 0 /w >/dev/null
f2="$CAP2/.raw/sessions/2025-10-22-ses_long.md"
[[ -f "$f2" ]] || fail "V9: no capture of the compacted session"
order=$(grep -E '^(FIRST|SECOND|THIRD|SUMMARY)|^## ' "$f2" | paste -sd'|' -)
want='## User|FIRST ASK|## Assistant|FIRST ANSWER|## Compaction|## Compaction summary|SUMMARY ONE|## User|SECOND ASK|## Compaction|## Compaction summary|SUMMARY TWO|## User|THIRD ASK'
[[ "$order" == "$want" ]] || fail "V9: capture order — got '$order'"
ok "V9: a session compacted twice keeps all its earlier text, summaries under their own heading"

# --- V10: harness ingest against a mock upstream -----------------------------
# The mock answers like a model: a section naming the source's first file, an
# invented name on the first try for BADNAME, a refusal for FAILALWAYS.
MOCK="$TMP_ROOT/mock.py"
REQS="$TMP_ROOT/requests.jsonl"
cat > "$MOCK" <<'PYEOF'
import json, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self.reply({"data": [{"id": "free-m"}, {"id": "free-m-withtools"}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps({"raw": self.headers.get("x-harness-raw"), "model": req.get("model"),
                                "n": len(req["messages"]), "prompt": req["messages"][0]["content"]}) + "\n")
        src = req["messages"][0]["content"].split("<<<BEGIN_SOURCE>>>", 1)[1].split("<<<END_SOURCE>>>", 1)[0]
        m = re.search(r"=== (\S+?)(?: lines [0-9-]+)? ===", src)
        name = m.group(1) if m else "notes"
        if "FAILALWAYS" in src:
            text = "I cannot do that."
        elif "BADNAME" in src and len(req["messages"]) == 1:
            text = "summary: does things\n### Purpose\nUses `inventedOne`, `inventedTwo` and `inventedThree`.\nTopics: [[x]]"
        else:
            text = (f"```markdown\nHere you go:\nsummary: covers {name}\n# Purpose\nThe file `{name}` does the work.\n"
                    f"### Gotchas\nnone seen\nTopics: [[Shared Topic]], [[only {name}]]\n```")
        self.reply({"choices": [{"message": {"role": "assistant", "content": text}}]})
srv = HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
# shellcheck disable=SC2086
$PY "$MOCK" 0 "$REQS" > "$TMP_ROOT/mock.port" 2>/dev/null &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null; rm -rf "$TMP_ROOT"' EXIT
for _ in $(seq 50); do [[ -s "$TMP_ROOT/mock.port" ]] && break; sleep 0.1; done
PORT=$(head -n 1 "$TMP_ROOT/mock.port")
[[ -n "$PORT" ]] || fail "V10: the mock upstream did not start"
PROJ="$TMP_ROOT/proj"
mkdir -p "$PROJ/src/util" "$PROJ/docs" "$PROJ/node_modules/x"
printf 'def run_app():\n    return 1\n' > "$PROJ/src/app.py"
printf 'def helper():\n    pass\n' > "$PROJ/src/util/helpers.py"
printf '# proj\nFixes ABC-12.\n' > "$PROJ/README.md"
printf 'notes\n' > "$PROJ/docs/notes.md"
printf 'skip me\n' > "$PROJ/node_modules/x/index.js"
git -C "$PROJ" init -q && git -C "$PROJ" add -A && git -C "$PROJ" commit -qm init
V="$TMP_ROOT/ingvault"
cp -R "$REPO_ROOT/vault-template" "$V"
cat > "$V/wiki/overview.md" <<'MDEOF'
---
type: overview
summary: 'a test vault'
project: 'proj'
goal: 'a test vault, it''s for proj'
ticket_pattern: '[A-Z]+-[0-9]+'
---

# proj
MDEOF
mkdir -p "$V/.raw/notes"
printf -- '---\ntitle: Meeting\n---\n## Day one\nWe decided on ABC-12 and DEF-3.\n' > "$V/.raw/notes/meeting.md"
printf -- '---\ntype: note\nsummary: why retries back off\n---\n# Retries\n' > "$V/wiki/notes/why-retry.md"
ingest() {
    # shellcheck disable=SC2086
    $PY "$REPO_ROOT/scripts/vault_ingest.py" ingest --vault "$V" --proxy "http://127.0.0.1:$PORT/v1" \
        --model free-m-withtools "$@" 2>&1
}
nreq() { [[ -f "$REQS" ]] && wc -l < "$REQS" | tr -d ' ' || echo 0; }
out=$(ingest --src "$PROJ") || fail "V10: ingest failed — $out"
for f in wiki/code/proj/proj.md wiki/code/proj/proj--src.md wiki/code/proj/proj--src--util.md \
        wiki/code/proj/proj--docs.md wiki/sources/notes--meeting.md wiki/tickets/ABC-12.md wiki/tickets/DEF-3.md; do
    [[ -f "$V/$f" ]] || fail "V10: ingest should write $f — $(cd "$V" && find wiki -name '*.md' | sort | tr '\n' ' ') $out"
done
ls "$V/wiki/topics/" | grep -qi '^shared topic\.md$' || fail "V10: a topic used by many pages gets a hub — $(ls "$V/wiki/topics" 2>&1)"
grep -rq node_modules "$V/wiki" && fail "V10: node_modules must not be ingested"
$PY - "$REQS" <<'PYEOF' || fail "V10: requests — $(head -c 600 "$REQS")"
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert rows and all(r["raw"] == "1" and r["model"] == "free-m" for r in rows), "raw header / free model"
assert any("a test vault, it's for proj" in r["prompt"] for r in rows), "the charter goes with each request"
assert all("Reminder, the task:" in r["prompt"].split("<<<END_SOURCE>>>")[-1] for r in rows), "rules repeated after the source"
PYEOF
src_page="$V/wiki/code/proj/proj--src.md"
grep -q '^> covers src/app.py' "$src_page" || fail "V10: the section's summary line — $(cat "$src_page")"
grep -q 'Here you go\|```' "$src_page" && fail "V10: preamble and fence should be stripped — $(cat "$src_page")"
grep -q 'proj--src--util' "$src_page" || fail "V10: a directory page links its subdirectories — $(cat "$src_page")"
grep -q '\[\[proj\]\]' "$V/wiki/index.md" || fail "V10: the index lists the codebase — $(cat "$V/wiki/index.md")"
grep -q 'notes--meeting' "$V/wiki/index.md" || fail "V10: the index lists the sources — $(cat "$V/wiki/index.md")"
grep -qx -- '- \[\[why-retry\]\]: why retries back off' "$V/wiki/index.md" && grep -qx '## Notes' "$V/wiki/index.md" \
    || fail "V10: the index lists an agent's notes — $(cat "$V/wiki/index.md")"
grep -q 'notes--meeting' "$V/wiki/tickets/ABC-12.md" || fail "V10: the ticket page links its sources — $(cat "$V/wiki/tickets/ABC-12.md")"
ok "V10: ingest writes code, source, topic and ticket pages and the index, through the raw header on the free model"

# --- V11: only new or changed units are sent; gone sources lose their pages --
before=$(nreq)
ingest >/dev/null || fail "V11: rerun failed"
[[ "$(nreq)" == "$before" ]] || fail "V11: a rerun with nothing changed sent $(( $(nreq) - before )) requests"
printf 'def run_app():\n    return 2\n' > "$PROJ/src/app.py"
rm -rf "$PROJ/docs"; git -C "$PROJ" add -A; git -C "$PROJ" commit -qm change
before=$(nreq)
ingest >/dev/null || fail "V11: incremental run failed"
[[ "$(nreq)" == "$(( before + 1 ))" ]] || fail "V11: one changed directory should be one request, got $(( $(nreq) - before ))"
[[ ! -f "$V/wiki/code/proj/proj--docs.md" ]] || fail "V11: a deleted directory's page should go"
grep -q 'proj--docs' "$V/wiki/index.md" && fail "V11: the index should drop the deleted page"
ok "V11: incremental: unchanged units are skipped, changed ones resent, deleted ones removed"

# --- V12: a reply that fails its checks is retried once, then marked failed --
mkdir -p "$PROJ/src/bad" "$PROJ/src/worse"
printf '# BADNAME\nx = 1\n' > "$PROJ/src/bad/x.py"
printf '# FAILALWAYS\ny = 1\n' > "$PROJ/src/worse/y.py"
git -C "$PROJ" add -A; git -C "$PROJ" commit -qm more
ingest >/dev/null
$PY - "$V/.harness/ingest.json" "$REQS" <<'PYEOF' || fail "V12: manifest — $(cat "$V/.harness/ingest.json")"
import json, sys
units = json.load(open(sys.argv[1]))["units"]
bad = [v for k, v in units.items() if k.startswith("code:proj/src/bad#")]
worse = [v for k, v in units.items() if k.startswith("code:proj/src/worse#")]
assert bad and bad[0]["status"] == "ok", bad
assert worse and worse[0]["status"] == "failed" and worse[0]["problems"], worse
rows = [json.loads(l) for l in open(sys.argv[2])]
assert any(r["n"] == 3 and "BADNAME" in r["prompt"] for r in rows), "the retry carries the first reply and its problems"
PYEOF
[[ ! -f "$V/wiki/code/proj/proj--src--worse.md" ]] || fail "V12: a failed unit writes no page"
before=$(nreq); ingest >/dev/null
[[ "$(nreq)" == "$before" ]] || fail "V12: a failed unit is not resent without --retry-failed"
ingest --retry-failed >/dev/null
(( $(nreq) > before )) || fail "V12: --retry-failed resends the failed unit"
ok "V12: a bad reply is retried with its problems, a refusal is marked failed and resent only on request"

# --- V13: the capture never shrinks a file, and import --all takes any directory
DB3="$TMP_ROOT/shrunk.db"
cp "$DB2" "$DB3"
# shellcheck disable=SC2086
$PY - "$DB3" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM message WHERE id = 'm7'"); c.commit()
PYEOF
cp "$f2" "$TMP_ROOT/f2.before"
# shellcheck disable=SC2086
err_out=$($PY "$REPO_ROOT/scripts/vault_capture.py" "$DB3" "$CAP2" 0 /w 2>&1 >/dev/null)
cmp -s "$f2" "$TMP_ROOT/f2.before" || fail "V13: a session with fewer messages must not overwrite its capture"
grep -q 'kept' <<<"$err_out" || fail "V13: keeping the file should be reported — $err_out"
ALL="$TMP_ROOT/all"
# shellcheck disable=SC2086
$PY "$REPO_ROOT/scripts/vault_capture.py" "$DB" "$ALL" 0 '*' >/dev/null
for sid in ses_new ses_else ses_old; do
    ls "$ALL/.raw/sessions/" | grep -q "$sid" || fail "V13: --all should capture $sid — $(ls "$ALL/.raw/sessions")"
done
ls "$ALL/.raw/sessions/" | grep -q ses_sub && fail "V13: subagent sessions are never captured on their own"
ok "V13: a capture is never shrunk; import --all captures every directory's root sessions"

# --- V14: vault init edits answers in place and always writes the charter ----
# Drives _vault_charter through a pty, as a terminal user would: arrow keys
# and Home insert inside the pre-filled line (a plain read stored them as
# escape codes), an empty covers answer still writes wiki/overview.md, and an
# emptied ticket pattern stays empty on the next run. Skipped where Python has
# no pty (Windows).
if $PY -c 'import pty' 2>/dev/null; then
    CH="$TMP_ROOT/charter"
    mkdir -p "$CH/wiki"
    cp "$REPO_ROOT/vault-template/wiki/overview.md" "$CH/wiki/overview.md"
    cat > "$TMP_ROOT/charter_pty.py" <<'PYEOF2'
import os, pty, re, select, sys, time
repo, root, vdir = sys.argv[1:4]
keys = [k.encode().decode("unicode_escape") for k in sys.argv[4:]]
script = ('HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$1/install" source "$2/harness" 2>/dev/null'
          ' && clone_dir="$2" && source "$2/scripts/lib/platform.sh" && cd "$3" && _vault_charter "$3"')
pid, fd = pty.fork()
if pid == 0:
    os.environ["TERM"] = "xterm"
    os.execvp("bash", ["bash", "-c", script, "x", root, repo, vdir])
out = b""
# The readline prompt, after the bracketed-paste switch or other escapes.
PROMPT = re.compile(rb"\n(?:\x1b\[[?0-9;]*[A-Za-z])*> ")
def read_until(n, timeout=20):
    global out
    end = time.time() + timeout
    while len(PROMPT.findall(out)) < n and time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                out += os.read(fd, 4096)
            except OSError:
                break
    return len(PROMPT.findall(out)) >= n
for i, k in enumerate(keys, 1):
    if not read_until(i):
        sys.exit("no prompt %d; output so far: %r" % (i, out))
    time.sleep(0.1)
    os.write(fd, k.encode())
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
_, status = os.waitpid(pid, 0)
sys.exit(0 if os.waitstatus_to_exitcode(status) == 0 else "exit %r; output: %r" % (status, out))
PYEOF2
    charter() { $PY "$TMP_ROOT/charter_pty.py" "$REPO_ROOT" "$TMP_ROOT" "$CH" "$@"; }
    fm() { sed -n "s/^$1: //p" "$CH/wiki/overview.md"; }
    # project: clear, type, five lefts, insert; readers: Home, insert; covers empty.
    charter '\x15hello world\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dbig \r' '\r' '\x1b[Hjust \r' '\r' '\r' '\r' '\r' \
        || fail "V14: first init run failed"
    [[ "$(fm project)" == "'hello big world'" ]] || fail "V14: left arrow should insert mid-line — project=$(fm project)"
    [[ "$(fm readers)" == "'just me and my coding agents'" ]] || fail "V14: Home should insert at the start — readers=$(fm readers)"
    [[ "$(fm goal)" == "'a second brain for hello big world: how it works, what was decided and why, what was done'" ]] \
        || fail "V14: Enter keeps the suggestion — goal=$(fm goal)"
    [[ "$(fm ticket_pattern)" == "'[A-Z][A-Z0-9]+-[0-9]+'" ]] || fail "V14: a new vault suggests the ticket pattern — $(fm ticket_pattern)"
    [[ "$(fm covers)" == "''" ]] || fail "V14: covers should be empty — $(fm covers)"
    [[ ! -e "$CH/wiki/overview.md.tmp" ]] || fail "V14: the charter was left as overview.md.tmp"
    grep -q $'\x1b' "$CH/wiki/overview.md" && fail "V14: escape codes reached the charter"
    # Clear the ticket pattern, then a run of plain Enters keeps every answer.
    charter '\r' '\r' '\r' '\r' '\x15\r' '\r' '\r' || fail "V14: second init run failed"
    [[ "$(fm ticket_pattern)" == "''" ]] || fail "V14: an emptied ticket pattern should save empty — $(fm ticket_pattern)"
    charter '\r' '\r' '\r' '\r' '\r' '\r' '\r' || fail "V14: third init run failed"
    [[ "$(fm ticket_pattern)" == "''" && "$(fm project)" == "'hello big world'" ]] \
        || fail "V14: a re-run should keep the saved answers — project=$(fm project) ticket_pattern=$(fm ticket_pattern)"
    [[ "$(fm tracker_url)" == "''" ]] || fail "V14: no tracker by default — $(fm tracker_url)"
    # A tracker (its trailing slash dropped) asks for extra fields too.
    charter '\r' '\r' '\r' '\r' '\r' '\r' 'https://jira.example.com/\r' 'customfield_10500, Team\r' \
        || fail "V14: init with a tracker failed"
    [[ "$(fm tracker_url)" == "'https://jira.example.com'" && "$(fm tracker_fields)" == "'customfield_10500, Team'" ]] \
        || fail "V14: tracker_url/tracker_fields — $(fm tracker_url) $(fm tracker_fields)"
    ok "V14: vault init edits pre-filled answers in place, writes the charter (tracker included), keeps answers on a re-run"
else
    ok "V14: skipped (no pty in this Python)"
fi

# --- V15: vault_ticket.py against a mock tracker -----------------------------
# The mock is Jira Server's REST v2: ABC-7 with the token (Bearer, or Basic
# for Cloud's email:token), 401 without it, 404 for anything else; under
# /moved/ it redirects to the real path. It logs each request's path.
JIRA="$TMP_ROOT/jira.py"
cat > "$JIRA" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
ISSUE = {"key": "ABC-7",
         "names": {"customfield_10400": "Acceptance Criteria", "customfield_10111": "Story Points",
                   "customfield_10109": "Sprint", "customfield_10500": "Team", "customfield_10600": "Risk Index"},
         "fields": {"summary": "Report sets while the task runs", "status": {"name": "To Do"},
                    "issuetype": {"name": "Improvement"}, "priority": {"name": "High"}, "assignee": None,
                    "labels": ["backend"], "updated": "2026-10-20T10:00:00.000+0000",
                    "description": "Report them in-stride.\nUse key sk-abcdefghijklmnopqrstuvwxyz0123 for the demo.",
                    "customfield_10400": "GIVEN a task\nWHEN it runs\nTHEN sets are reported",
                    "customfield_10111": 8.0, "customfield_10500": {"value": "Blue"}, "customfield_10600": 3.5,
                    "customfield_10109": ["com.atlassian.greenhopper.service.sprint.Sprint@1f[id=5,rapidViewId=2,state=ACTIVE,name=Sprint 5,startDate=x]"],
                    "subtasks": [{"key": "ABC-8", "fields": {"summary": "the sub part", "status": {"name": "Done"}}}],
                    "issuelinks": [], "comment": {"comments": [
                        {"author": {"displayName": "Pat"}, "created": "2026-10-%02dT09:00:00.000+0000" % (i + 1),
                         "body": "comment %d" % i} for i in range(12)]}}}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        open(sys.argv[1], "a").write(self.path + "\n")
        if self.path.startswith("/moved/"):
            self.send_response(302); self.send_header("Location", self.path[len("/moved"):])
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if self.headers.get("Authorization") not in ("Bearer tok123", "Basic bWVAeC5pbzp0b2sxMjM="):
            code, body = 401, {"errorMessages": ["no"]}
        elif self.path.startswith("/rest/api/2/issue/ABC-7?") and "expand=names" in self.path:
            code, body = 200, ISSUE
        else:
            code, body = 404, {"errorMessages": ["Issue Does Not Exist"]}
        b = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
srv = HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
# shellcheck disable=SC2086
$PY "$JIRA" "$TMP_ROOT/jira.log" > "$TMP_ROOT/jira.port" 2>/dev/null &
JIRA_PID=$!
trap 'kill "$MOCK_PID" "$JIRA_PID" 2>/dev/null; rm -rf "$TMP_ROOT"' EXIT
for _ in $(seq 50); do [[ -s "$TMP_ROOT/jira.port" ]] && break; sleep 0.1; done
JPORT=$(head -n 1 "$TMP_ROOT/jira.port")
[[ -n "$JPORT" ]] || fail "V15: the mock tracker did not start"
TV="$TMP_ROOT/ticketvault"
git init -q "$TV"
cp -R "$REPO_ROOT/vault-template/." "$TV/"
cat > "$TV/wiki/overview.md" <<MDEOF
---
type: overview
summary: 'a test vault'
project: 'proj'
tracker_url: 'http://127.0.0.1:$JPORT/'
tracker_fields: 'Team'
---
MDEOF
git -C "$TV" add -A && git -C "$TV" commit -qm init
# tk TOKEN KEY: the token goes in the environment, as ticket_pull passes it.
tk() { TRACKER_TOKEN="$1" $PY "$REPO_ROOT/scripts/vault_ticket.py" "$TV" "$2" 2>&1; }
SNAP="$TV/.raw/tickets/ABC-7.md"
NOTE="$TV/wiki/notes/tickets/ABC-7-state.md"
out=$(tk tok123 ABC-7) || fail "V15: vault_ticket.py failed — $out"
[[ "$out" == "ABC-7 (To Do): Report sets while the task runs [new]" ]] || fail "V15: status line — $out"
[[ -f "$SNAP" && -f "$NOTE" ]] || fail "V15: the copy and the state note should be written"
for want in '^ticket: ABC-7$' '^status: "\?To Do"\?$' '^> This is the ticket as people wrote it' '^- Priority: High$' \
        '^## Acceptance criteria$' '^- THEN sets are reported$' '^## Story Points$' '^8$' '^Sprint 5$' '^## Team$' '^Blue$' \
        '^- subtask ABC-8 (Done): the sub part$' '^## Comments (the last 10 of 12)$' '^comment 11$'; do
    grep -q -- "$want" "$SNAP" || fail "V15: the copy lacks /$want/ — $(cat "$SNAP")"
done
grep -q 'sk-abcdefghij\|Risk Index\|^comment 1$' "$SNAP" && fail "V15: the copy kept a key, an unasked field or an old comment — $(cat "$SNAP")"
grep -q '^Tickets: \[\[ABC-7\]\]$' "$NOTE" || fail "V15: the state note should link the hub — $(cat "$NOTE")"
[[ "$(tk me@x.io:tok123 ABC-7)" == *"[unchanged]" ]] || fail "V15: a second pull (Basic auth, email:token) should be unchanged"
printf 'mine\n' > "$NOTE"
out=$(tk wrong ABC-7)
[[ "$out" == *"refused TRACKER_TOKEN (HTTP 401"*"using the copy from 2026-10-20"* ]] || fail "V15: a refused token — $out"
[[ "$(cat "$NOTE")" == "mine" ]] || fail "V15: an existing state note must not be touched"
out=$(tk '' ABC-7)
[[ "$out" == *"no Jira token set"*"using the copy"* ]] || fail "V15: no token — $out"
out=$(tk tok123 ABC-99)
[[ "$out" == *"HTTP 404"*"no copy of the ticket yet"* && -f "$TV/wiki/notes/tickets/ABC-99-state.md" ]] \
    || fail "V15: an unknown ticket still gets its state note — $out"
sed -i.bak "s#127.0.0.1:$JPORT#127.0.0.1:9#" "$TV/wiki/overview.md"
out=$(tk tok123 ABC-7)
[[ "$out" == *"not reachable"*"using the copy"* ]] || fail "V15: an unreachable tracker — $out"
mv "$TV/wiki/overview.md.bak" "$TV/wiki/overview.md"
# The token goes only to tracker_url: a redirect is not followed (urllib
# would send the Authorization header on), plain http only to this host.
: > "$TMP_ROOT/jira.log"
sed -i.bak "s#127.0.0.1:$JPORT/#127.0.0.1:$JPORT/moved/#" "$TV/wiki/overview.md"
out=$(tk tok123 ABC-7)
[[ "$out" == *"the tracker redirected (HTTP 302) to http://127.0.0.1:$JPORT/rest/api/2/issue/ABC-7"*"not followed"*"using the copy"* ]] \
    || fail "V15: a redirect should not be followed — $out"
[[ "$(cat "$TMP_ROOT/jira.log")" == "/moved/rest/api/2/issue/ABC-7?expand=names" ]] \
    || fail "V15: only the redirecting address should be asked — $(cat "$TMP_ROOT/jira.log")"
mv "$TV/wiki/overview.md.bak" "$TV/wiki/overview.md"
for u in "http://jira.example.com" "http://127.0.0.2:$JPORT"; do
    sed -i.bak "s#^tracker_url: .*#tracker_url: '$u'#" "$TV/wiki/overview.md"
    out=$(tk tok123 ABC-7)
    [[ "$out" == *"is http, which would send TRACKER_TOKEN unencrypted"*"using the copy"* ]] \
        || fail "V15: http to $u should be refused — $out"
    mv "$TV/wiki/overview.md.bak" "$TV/wiki/overview.md"
done
[[ "$(wc -l < "$TMP_ROOT/jira.log")" -eq 1 ]] || fail "V15: a refused URL made a request — $(cat "$TMP_ROOT/jira.log")"
for u in https://jira.example.com https://jira.example.com:8443/jira http://localhost:8080 HTTP://127.0.0.1/; do
    _tracker_url_ok "$u" 2>/dev/null || fail "V15: _tracker_url_ok should take $u"
done
for u in http://jira.example.com http://localhost.evil.io ftp://x https:// 'https://a b'; do
    _tracker_url_ok "$u" 2>/dev/null && fail "V15: _tracker_url_ok should refuse $u"
done
# harness ticket login writes the URL into the charter with _charter_set:
# replacing the key, or adding it when the charter has none.
cp "$TV/wiki/overview.md" "$TMP_ROOT/charter.bak"
_charter_set "$TV" tracker_url "https://jira.new.example/it's" || fail "V15: _charter_set failed"
[[ "$(_charter_value "$TV" tracker_url)" == "https://jira.new.example/it's" ]] \
    || fail "V15: _charter_set should replace tracker_url — $(cat "$TV/wiki/overview.md")"
[[ "$(grep -c '^tracker_url:' "$TV/wiki/overview.md")" == 1 && "$(_charter_value "$TV" tracker_fields)" == "Team" ]] \
    || fail "V15: _charter_set should leave one tracker_url and the other keys — $(cat "$TV/wiki/overview.md")"
sed -i.bak '/^tracker_url:/d' "$TV/wiki/overview.md"
_charter_set "$TV" tracker_url "https://jira.example.com" || fail "V15: _charter_set (no key yet) failed"
[[ "$(_charter_value "$TV" tracker_url)" == "https://jira.example.com" && "$(sed -n 1p "$TV/wiki/overview.md")" == "---" ]] \
    || fail "V15: _charter_set should add a missing key in the front matter — $(cat "$TV/wiki/overview.md")"
mv "$TMP_ROOT/charter.bak" "$TV/wiki/overview.md"; rm -f "$TV/wiki/overview.md.bak"
# The acceptance criteria field: TRACKER_AC_FIELDS (environment), else the
# charter's tracker_ac_fields, names more fields besides the display-name
# match; the named field goes under the heading, not under its own.
out=$(TRACKER_AC_FIELDS=Team tk tok123 ABC-7)
[[ "$out" == *"[updated]" ]] || fail "V15: TRACKER_AC_FIELDS=Team should change the copy — $out"
crit=$(sed -n '/^## Acceptance criteria$/,/^## /p' "$SNAP")
grep -q '^- Blue$' <<<"$crit" && grep -q 'THEN sets' <<<"$crit" && ! grep -q '^## Team$' "$SNAP" \
    || fail "V15: TRACKER_AC_FIELDS=Team should put Team, with the Acceptance Criteria field, under the criteria — $(cat "$SNAP")"
cp "$TV/wiki/overview.md" "$TMP_ROOT/charter.bak"
printf "tracker_ac_fields: 'customfield_10500'\n" > "$TMP_ROOT/acline"
sed -i.bak "/^tracker_fields:/r $TMP_ROOT/acline" "$TV/wiki/overview.md"
tk tok123 ABC-7 >/dev/null
sed -n '/^## Acceptance criteria$/,/^## /p' "$SNAP" | grep -q '^- Blue$' \
    || fail "V15: the charter's tracker_ac_fields should name the criteria field — $(cat "$SNAP")"
TRACKER_AC_FIELDS=customfield_10400 tk tok123 ABC-7 >/dev/null
grep -q '^- THEN sets are reported$' "$SNAP" && grep -q '^## Team$' "$SNAP" \
    || fail "V15: TRACKER_AC_FIELDS should win over the charter — $(cat "$SNAP")"
mv "$TMP_ROOT/charter.bak" "$TV/wiki/overview.md"; rm -f "$TV/wiki/overview.md.bak"
tk tok123 ABC-7 >/dev/null
# render() on the field's shapes, read back with the queue's ticket_criteria.
$PY - "$REPO_ROOT/scripts" <<'PYEOF' || fail "V15: the acceptance criteria field shapes (above)"
import sys
sys.path.insert(0, sys.argv[1])
from vault_ticket import render
from ticket_queue import ticket_criteria
names = {"customfield_1": "Acceptance criteria", "customfield_2": "AC", "customfield_3": "Team"}
para = lambda t: {"type": "paragraph", "content": [{"type": "text", "text": t}]}
item = lambda *c: {"type": "listItem", "content": list(c)}
adf = {"type": "doc", "version": 1, "content": [para("Scenarios:"), {"type": "bulletList", "content": [
    item(para("a user logs in")), item(para("a bad password"), {"type": "orderedList", "content": [item(para("shows an error"))]})]}]}
def crit(fields, ac=(), desc="Do it."):
    text = render("A-1", "https://j", {"names": names, "fields": dict(fields, summary="s", description=desc)}, [], ac)
    return text, ticket_criteria(text)
bad = []
def want(what, got, exp):
    if got != exp:
        bad.append(f"{what}: got {got!r}, want {exp!r}")
want("ADF", crit({"customfield_1": adf})[1], ["a user logs in", "a bad password", "shows an error"])
want("wiki", crit({"customfield_1": "h3. Login\n# Given a user\n## When the password is wrong\n* Then it says so"})[1],
     ["Given a user", "When the password is wrong", "Then it says so"])
want("plain text", crit({"customfield_1": "Users can log in\n\nErrors are shown"})[1], ["Users can log in", "Errors are shown"])
want("checklist", crit({"customfield_1": [{"name": "one", "checked": True}, "two", 3, None]})[1], ["one", "two"])
want("override adds", crit({"customfield_1": "* auto", "customfield_2": "* the AC field"}, ["AC"])[1], ["the AC field", "auto"])
want("AC alone is not matched", crit({"customfield_2": "* x"})[1], [])
base = crit({"customfield_3": "Blue"})[0]
want("no field", "Acceptance" in base, False)
for v in (None, "", " \n", {"type": "doc", "content": []}, 12, {"odd": 1}):
    want(f"empty or unknown {v!r}", crit({"customfield_1": v, "customfield_3": "Blue"})[0], base)
d = "Acceptance criteria:\n* x\n* y"
want("already in the description", crit({"customfield_1": "* x\n* y"}, desc=d)[0].count("cceptance criteria"), 1)
want("described, no heading", crit({"customfield_1": "* x\n* y"}, desc="* x\n* y")[1], ["x", "y"])
print("\n".join(bad), file=sys.stderr)
sys.exit(1 if bad else 0)
PYEOF
ok "V15: vault_ticket.py writes a labelled, redacted copy (the acceptance criteria field as bullets) and a state note, falls back to the copy, sends the token only to an https (or local) tracker_url and follows no redirect"

# --- V16: --ticket, the folder's ticket, the launch's instruction files ------
PROJ2="$TMP_ROOT/proj2"
mkdir -p "$PROJ2/sub/deeper"
git -C "$PROJ2" init -q
printf 'root rules\n' > "$PROJ2/AGENTS.md"
printf 'sub rules\n' > "$PROJ2/sub/AGENTS.md"
printf 'claude rules\n' > "$PROJ2/sub/CLAUDE.md"
harness_ticket_flag abc-7 2>/dev/null || fail "V16: --ticket abc-7 refused"
[[ "$harness_ticket_arg" == "ABC-7" ]] || fail "V16: --ticket should upper-case the key — $harness_ticket_arg"
harness_ticket_flag 'nope' 2>/dev/null && fail "V16: --ticket nope accepted"
harness_ticket_flag '' 2>/dev/null && fail "V16: --ticket with no key accepted"
(
    TRACKER_TOKEN=tok123
    cd "$PROJ2/sub/deeper"
    here=$(harness_abs_path "$PWD")
    harness_vault_dir=$(harness_abs_path "$TV") harness_ticket_arg="ABC-7"
    ticket_prepare 2>"$TMP_ROOT/prep.err"
    [[ "$harness_ticket" == "ABC-7" ]] || fail "V16: ticket_prepare should set the ticket — $(cat "$TMP_ROOT/prep.err")"
    grep -q '^\[harness\] ticket ABC-7 (To Do)' "$TMP_ROOT/prep.err" || fail "V16: launch line — $(cat "$TMP_ROOT/prep.err")"
    [[ "$(ticket_for_dir "$here")" == "ABC-7" ]] || fail "V16: --ticket should be remembered for the folder"
    [[ "$(cd "$PROJ2/sub" && ticket_for_dir "$(harness_abs_path "$PWD")")" == "" ]] || fail "V16: a parent folder has no ticket"
    cmd_ticket DEF-1 >/dev/null
    harness_ticket_arg=""
    (cd .. && cmd_ticket XY-2 >/dev/null)
    [[ "$(ticket_for_dir "$here")" == "DEF-1" ]] || fail "V16: the nearest folder's ticket wins"
    cmd_ticket off >/dev/null
    [[ "$(ticket_for_dir "$here")" == "XY-2" ]] || fail "V16: off forgets this folder's ticket only"
    cmd_ticket nope >/dev/null 2>&1 && fail "V16: harness ticket nope accepted"
    harness_ticket="ABC-7"
    # Host: the global file, then every AGENTS.md up to the git root (the
    # CLAUDE.md next to one is not read), then the vault's three.
    mkdir -p "$TMP_ROOT/home/.claude"
    printf 'global\n' > "$TMP_ROOT/home/.claude/CLAUDE.md"
    got=$(HOME="$TMP_ROOT/home" XDG_CONFIG_HOME="" harness_context_files "$TMP_ROOT/home" | cut -f1 | paste -sd, -)
    [[ "$got" == "global,project,project,vault,ticket,ticket state" ]] || fail "V16: host context files — $got"
    got=$(HOME="$TMP_ROOT/home" XDG_CONFIG_HOME="" harness_context_files "$TMP_ROOT/home" | sed -n 2p | cut -f2)
    [[ "$got" == "$PROJ2/sub/AGENTS.md" ]] || fail "V16: the nearest AGENTS.md comes first — $got"
    # A container sees only its folder: no project file in sub/deeper.
    got=$(harness_context_files "$TMP_ROOT/home" | cut -f1 | paste -sd, -)
    [[ "$got" == "global,vault,ticket,ticket state" ]] || fail "V16: container context files — $got"
    cfg=$(vault_opencode_config)
    jq -e --arg v "$(harness_abs_path "$TV")" '.instructions == [$v + "/AGENTS.md", $v + "/.raw/tickets/ABC-7.md",
            $v + "/wiki/notes/tickets/ABC-7-state.md"]
        and .provider.harness.options.headers["x-harness-ticket"] == "ABC-7"' <<<"$cfg" >/dev/null \
        || fail "V16: the overlay should carry the ticket files and header — $cfg"
    # x-harness-task: the title from the copy, plus a queue stage's card;
    # a card that is not a JSON object is dropped, the title kept.
    task=$(jq -r '.provider.harness.options.headers["x-harness-task"]' <<<"$cfg" | base64 -d)
    [[ "$task" == '{"title":"Report sets while the task runs"}' ]] || fail "V16: the task header — $task"
    task=$(HARNESS_TASK_CARD='{"stage":"plan","attempt":2}' vault_opencode_config \
        | jq -r '.provider.harness.options.headers["x-harness-task"]' | base64 -d)
    [[ "$task" == '{"stage":"plan","attempt":2,"title":"Report sets while the task runs"}' ]] \
        || fail "V16: the task header with a queue card — $task"
    task=$(HARNESS_TASK_CARD='["plan"]' vault_opencode_config \
        | jq -r '.provider.harness.options.headers["x-harness-task"]' | base64 -d)
    [[ "$task" == '{"title":"Report sets while the task runs"}' ]] || fail "V16: a bad queue card — $task"
    out=$(HOME="$TMP_ROOT/home" cmd_context --host)
    grep -q '^  1\. global ' <<<"$out" && grep -q '^ticket:  XY-2' <<<"$out" && grep -q '^token:   set' <<<"$out" \
        || fail "V16: harness context — $out"
    grep -q 'tok123' <<<"$out" && fail "V16: harness context printed the token"
    [[ "$(_ticket_tracker_status "$TV" | head -n 1)" == "tracker: http://127.0.0.1:$JPORT/" ]] \
        || fail "V16: the tracker shown should be the charter's — $(_ticket_tracker_status "$TV")"
    exit 0
) || exit 1
ok "V16: --ticket is checked and remembered per folder; the overlay and harness context list the ticket's files"

# --- V17: sessions record their ticket and context; the hub links them ------
DB3="$TMP_ROOT/tickets.db"
# shellcheck disable=SC2086
$PY - "$DB3" <<'PYEOF' || fail "V17: could not build the fixture database"
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, time_created INT,
  time_updated INT, tokens_input INT, tokens_output INT);
CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INT, data TEXT);
""")
for sid, upd in (("ses_before", 1761100000000), ("ses_during", 1761100900000)):
    c.execute("INSERT INTO session VALUES (?, NULL, '/w', ?, 1761100000000, ?, 1, 1)", (sid, sid, upd))
    c.execute("INSERT INTO message VALUES (?,?,?,?)", (sid + "m", sid, 0, json.dumps({"role": "user"})))
    c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (sid + "p", sid + "m", sid, 0,
              json.dumps({"type": "text", "text": "work on it"})))
c.commit()
PYEOF
printf 'project rules\n' > "$TMP_ROOT/PROJECT_AGENTS.md"
cap() { $PY "$REPO_ROOT/scripts/vault_capture.py" "$DB3" "$TV" 0 /w "$@" >/dev/null; }
cap --launch 1761100500000 --ticket ABC-7 --context "$TV/AGENTS.md" --context "$TMP_ROOT/PROJECT_AGENTS.md" \
    || fail "V17: capture failed"
during="$TV/.raw/sessions/2025-10-22-ses_during.md"
before="$TV/.raw/sessions/2025-10-22-ses_before.md"
copy=".raw/context/$($PY -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:12])' "$TMP_ROOT/PROJECT_AGENTS.md").md"
grep -q '^ticket: ABC-7$' "$during" && grep -qF "context: [\"AGENTS.md\", \"$copy\"]" "$during" \
    || fail "V17: the launch's session should record its ticket and context — $(head -12 "$during")"
grep -q '^ticket:' "$before" && fail "V17: a session not touched by the launch got its ticket"
grep -q '^project rules$' "$TV/$copy" || fail "V17: a file outside the vault is copied into .raw/context"
cap --launch 1761200000000 || fail "V17: second capture failed"
grep -q '^ticket: ABC-7$' "$during" || fail "V17: a later capture must keep the session's ticket"
$PY "$REPO_ROOT/scripts/vault_ingest.py" index --vault "$TV" >/dev/null || fail "V17: index failed"
hubf="$TV/wiki/tickets/ABC-7.md"
grep -q '^## The ticket$' "$hubf" && grep -qF '`.raw/tickets/ABC-7.md`' "$hubf" && grep -q '^## Sessions (1)$' "$hubf" \
    && grep -q 'ses_during' "$hubf" && grep -qF '[[ABC-7-state]]' "$hubf" \
    || fail "V17: the ticket hub should link the copy, the state note and the session — $(cat "$hubf")"
ok "V17: sessions record their ticket and instruction files; the ticket hub links the copy, state and sessions"

# --- V18: CRLF pages (a Windows checkout) parse like LF ones ------------------
# A two-part page whose file is CRLF: changing one part keeps the other's text.
mkdir -p "$PROJ/src/big"
# shellcheck disable=SC2086
$PY - "$PROJ/src/big" <<'PYEOF'
import sys
for n in ("a", "b"):
    open(f"{sys.argv[1]}/{n}.py", "w").write("".join(f"def {n}_{i}():\n    return {i}\n" for i in range(110)))
PYEOF
git -C "$PROJ" add -A; git -C "$PROJ" commit -qm big
ingest --unit-kb 4 >/dev/null || fail "V18: ingest of a two-part directory failed"
BIG="$V/wiki/code/proj/proj--src--big.md"
[[ "$(grep -c '^<!-- unit:code:proj/src/big#' "$BIG")" == 2 ]] || fail "V18: expected a two-part page — $(cat "$BIG")"
for f in "$BIG" "$V/wiki/overview.md" "$V/wiki/notes/why-retry.md"; do sed -i.bak 's/$/\r/' "$f" && rm -f "$f.bak"; done
grep -q $'\r$' "$BIG" || fail "V18: the page should be CRLF for this test"
printf 'def b_extra():\n    return 0\n' >> "$PROJ/src/big/b.py"
git -C "$PROJ" add -A; git -C "$PROJ" commit -qm big2
before=$(nreq)
ingest --unit-kb 4 >/dev/null || fail "V18: incremental ingest on a CRLF page failed"
[[ "$(nreq)" == "$(( before + 1 ))" ]] || fail "V18: one changed part should be one request, got $(( $(nreq) - before ))"
grep -q 'Not ingested yet' "$BIG" && fail "V18: the unchanged part of a CRLF page became a placeholder — $(cat "$BIG")"
grep -q '^> covers src/big/a.py' "$BIG" || fail "V18: part 1's section should be kept — $(cat "$BIG")"
grep -qx -- '- \[\[why-retry\]\]: why retries back off' "$V/wiki/index.md" \
    || fail "V18: a CRLF note's summary should reach the index — $(cat "$V/wiki/index.md")"
# shellcheck disable=SC2086
$PY - "$REPO_ROOT/scripts" "$V" <<'PYEOF' || fail "V18: CRLF parsers"
import sys
sys.path.insert(0, sys.argv[1])
import vault_ingest as vi
meta, body = vi.frontmatter("---\r\ntracker_url: 'https://j.example'\r\n---\r\n# x\r\n")
assert meta == {"tracker_url": "https://j.example"} and body == "# x\n", (meta, body)
m = vi.UNIT_RE.search("<!-- unit:a#1 sha:abc -->\r\n> s\r\n<!-- /unit -->\r\n")
assert m and m.group(1) == "a#1" and m.group(3) == "> s", m
_, ticket_re = vi.charter(sys.argv[2])
assert ticket_re and ticket_re.findall("see ABC-12") == ["ABC-12"], "a CRLF charter's ticket_pattern"
PYEOF
ok "V18: CRLF pages, notes and charter parse as LF ones; a CRLF two-part page keeps its other part"

# --- V19: the vault's working tree is LF (core.autocrlf=false, set once) -----
# GIT_CONFIG_GLOBAL stands in for Git for Windows' core.autocrlf=true.
printf '[core]\n\tautocrlf = true\n' > "$TMP_ROOT/crlf.gitconfig"
cmd_vault add crlf "$REMOTE" >/dev/null && cmd_vault use crlf >/dev/null
CRLFV="$state_root/vaults/crlf"
GIT_CONFIG_GLOBAL="$TMP_ROOT/crlf.gitconfig" git clone -q "$REMOTE" "$CRLFV" 2>/dev/null
grep -q $'\r$' "$CRLFV/AGENTS.md" || fail "V19: the stand-in should check out CRLF"
# A clone from long ago: files older than the index, so git trusts their stat
# data and does not look at their content (a fresh clone is "racily clean").
git -C "$CRLFV" ls-files -z | (cd "$CRLFV" && xargs -0 touch -d '2020-01-01 00:00:00')
GIT_CONFIG_GLOBAL="$TMP_ROOT/crlf.gitconfig" git -C "$CRLFV" update-index -q --refresh
GIT_CONFIG_GLOBAL="$TMP_ROOT/crlf.gitconfig" vault_prepare >/dev/null 2>&1
[[ "$(git -C "$CRLFV" config --local core.autocrlf)" == false ]] || fail "V19: prepare should set core.autocrlf=false in the vault"
grep -q $'\r$' "$CRLFV/AGENTS.md" && fail "V19: a clean CRLF checkout should be checked out again as LF"
[[ -z "$(git -C "$CRLFV" status --porcelain)" ]] || fail "V19: the re-checkout should leave the vault clean — $(git -C "$CRLFV" status --porcelain)"
git -C "$CRLFV" config core.autocrlf input
vault_prepare >/dev/null 2>&1
[[ "$(git -C "$CRLFV" config --local core.autocrlf)" == input ]] || fail "V19: a value already in the vault's config is kept"
rm -rf "$CRLFV"
GIT_CONFIG_GLOBAL="$TMP_ROOT/crlf.gitconfig" vault_prepare >/dev/null 2>&1
grep -q $'\r$' "$CRLFV/AGENTS.md" && fail "V19: a new clone should be checked out LF"
[[ "$(git -C "$CRLFV" config --local core.autocrlf)" == false ]] || fail "V19: a new clone should keep core.autocrlf=false"
cmd_vault use fresh >/dev/null && cmd_vault remove crlf >/dev/null
ok "V19: vault clones check out LF and keep core.autocrlf=false unless the vault's config says otherwise"

# --- V20: a codebase not on this host keeps its pages; --src moves it --------
# The other machine's path (C:/x on Windows, /home/x on Linux) is not a folder here.
AWAY="$TMP_ROOT/here/away"
mkdir -p "$AWAY/lib"
printf 'def far():\n    pass\n' > "$AWAY/lib/far.py"
git -C "$AWAY" init -q && git -C "$AWAY" add -A && git -C "$AWAY" commit -qm init
ingest --src "$AWAY" >/dev/null || fail "V20: ingest of a second codebase failed"
[[ -f "$V/wiki/code/away/away--lib.md" ]] || fail "V20: the codebase's page should exist"
mkdir -p "$TMP_ROOT/there" && mv "$AWAY" "$TMP_ROOT/there/away"
before=$(nreq)
out=$(ingest) || fail "V20: ingest with a codebase not on this host failed — $out"
[[ -f "$V/wiki/code/away/away--lib.md" ]] && ! grep -q 'Not ingested yet' "$V/wiki/code/away/away--lib.md" \
    || fail "V20: the pages of a codebase not on this host must be left alone — $out"
grep -q '"code:away/lib#1"' "$V/.harness/ingest.json" || fail "V20: its units must stay in the manifest"
[[ "$(grep -c 'away: .* is not on this host' <<<"$out")" == 1 ]] || fail "V20: one warning per run — $out"
[[ "$(nreq)" == "$before" ]] || fail "V20: nothing should be sent"
out=$(ingest --src "$TMP_ROOT/there/away") || fail "V20: --src with the codebase's new path should take over — $out"
[[ "$(nreq)" == "$before" ]] || fail "V20: the same files at a new path are not sent again"
grep -qF "\"away\": \"$TMP_ROOT/there/away\"" "$V/.harness/ingest.json" || fail "V20: the path should be updated — $(cat "$V/.harness/ingest.json")"
mkdir -p "$TMP_ROOT/third/away"
ingest --src "$TMP_ROOT/third/away" >/dev/null && fail "V20: a name whose path is on this host must still be refused"
ok "V20: a codebase whose folder is not on this host keeps its pages; --src with its new path updates it"

# --- V21: secret-looking files are never sent ---------------------------------
mkdir -p "$PROJ/src/conf/secrets" "$V/.raw/notes"
for f in .env .env.local id_rsa id_ed25519.pub server.pem tls.key cert.p12 .netrc credentials.json \
        secrets.yaml secrets/db.txt; do
    printf 'TOPSECRET\n' > "$PROJ/src/conf/$f"
done
printf 'KEY=EXAMPLEVALUE\n' > "$PROJ/src/conf/.env.example"
printf 'DEBUG = True\n' > "$PROJ/src/conf/settings.py"
printf 'TOPSECRET\n' > "$V/.raw/notes/credentials.md"
git -C "$PROJ" add -A; git -C "$PROJ" commit -qm conf
before=$(nreq)
ingest >/dev/null || fail "V21: ingest failed"
(( $(nreq) > before )) || fail "V21: the conf directory should be sent"
tail -n +"$(( before + 1 ))" "$REQS" | grep -q TOPSECRET && fail "V21: a secret-looking file was sent"
tail -n +"$(( before + 1 ))" "$REQS" | grep -q EXAMPLEVALUE || fail "V21: .env.example is not a secret"
grep -q 'credentials' "$V/.harness/ingest.json" && fail "V21: a .raw credentials file got a unit"
ok "V21: .env*, keys, .netrc, credentials* and secrets* files are never sent; .env.example is"

# --- V22: --watch --retry-failed sleeps on units that failed in this run -----
grep -q '"status": "failed"' "$V/.harness/ingest.json" || fail "V22: needs a failed unit"
before=$(nreq)
logs=$(grep -c ' ingest: ' "$V/wiki/log.md")
rc=0; timeout 5 "$PY" "$REPO_ROOT/scripts/vault_ingest.py" ingest --vault "$V" --proxy "http://127.0.0.1:$PORT/v1" \
    --model free-m --retry-failed --watch 1 >/dev/null 2>&1 || rc=$?
[[ "$rc" == 124 ]] || fail "V22: the watch should still be running (rc $rc)"
[[ "$(nreq)" == "$(( before + 2 ))" ]] || fail "V22: one try and one retry, then sleep; got $(( $(nreq) - before )) requests"
[[ "$(grep -c ' ingest: ' "$V/wiki/log.md")" == "$(( logs + 1 ))" ]] || fail "V22: one log line for the pass, none for the sleeps"
[[ ! -e "$V/harness-ingest.run.json" ]] || fail "V22: a run stopped with SIGTERM should remove its run file"
ok "V22: --watch --retry-failed retries a failed unit once per run and sleeps instead of looping"

# --- V23: nothing in a vault's .git runs: hooks and fsmonitor are off --------
# An agent can write the clone's .git; harness's git (bash and python) must not
# run what it put there, through a commit, a push and a rebase.
HOOKV="$TMP_ROOT/hookv"
RAN="$TMP_ROOT/hook.ran"
git clone -q "$REMOTE" "$HOOKV" 2>/dev/null
mkdir -p "$TMP_ROOT/evil-hooks"
for h in pre-commit commit-msg post-commit pre-push pre-rebase post-rewrite post-checkout post-merge reference-transaction; do
    for d in "$HOOKV/.git/hooks" "$TMP_ROOT/evil-hooks"; do
        printf '#!/bin/sh\necho %s >> "%s"\n' "$h" "$RAN" > "$d/$h"; chmod +x "$d/$h"
    done
done
printf '#!/bin/sh\necho fsmonitor >> "%s"\nexit 1\n' "$RAN" > "$TMP_ROOT/fsmon.sh"; chmod +x "$TMP_ROOT/fsmon.sh"
git -C "$HOOKV" config core.fsmonitor "$TMP_ROOT/fsmon.sh"
git -C "$HOOKV" config core.hooksPath "$TMP_ROOT/evil-hooks"
moveremote() {   # another machine pushes, so the next push is rebased first
    git -C "$OTHER" pull -q --rebase 2>/dev/null; printf '%s\n' "$1" >> "$OTHER/other.md"
    git -C "$OTHER" commit -qam "$1"; git -C "$OTHER" push -q 2>/dev/null
}
moveremote hooks1
printf 'hooked?\n' > "$HOOKV/hooked.md"
out=$(vault_sync "$HOOKV" 0 "" /w 2>&1)
grep -q "pushed (after rebasing" <<<"$out" || fail "V23: vault_sync should commit, rebase and push — $out"
[[ ! -e "$RAN" ]] || fail "V23: vault_sync ran the vault's hooks or fsmonitor: $(tr '\n' ' ' < "$RAN")"
moveremote hooks2
printf 'hooked again\n' >> "$HOOKV/hooked.md"
# shellcheck disable=SC2086
$PY -c 'import sys; sys.path.insert(0, sys.argv[1]); import vault_ingest as vi; vi.commit(sys.argv[2], "py")' \
    "$REPO_ROOT/scripts" "$HOOKV" || fail "V23: python commit failed"
[[ "$(git --git-dir="$REMOTE" log -1 --format=%s)" == py ]] || fail "V23: the python commit should be rebased and pushed"
[[ ! -e "$RAN" ]] || fail "V23: vault_ingest.py ran the vault's hooks or fsmonitor: $(tr '\n' ' ' < "$RAN")"
# The stand-ins work: plain git does run them.
git -C "$HOOKV" status >/dev/null 2>&1
[[ -s "$RAN" ]] || fail "V23: the fsmonitor stand-in should run under plain git"
ok "V23: harness's vault git (bash and python) runs no hook and no fsmonitor from the vault's .git"

# --- V24: no identity, a signing key: commits and the rebase still go through
IDV="$TMP_ROOT/idv"
git clone -q "$REMOTE" "$IDV" 2>/dev/null
printf '[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n' > "$TMP_ROOT/sign.gitconfig"
(
    unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL EMAIL
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$TMP_ROOT/sign.gitconfig"
    moveremote() {
        git -C "$OTHER" pull -q --rebase 2>/dev/null
        printf '%s\n' "$1" >> "$OTHER/other.md"
        git -c user.name=o -c user.email=o@o -c commit.gpgsign=false -C "$OTHER" commit -qam "$1"
        git -C "$OTHER" push -q 2>/dev/null
    }
    moveremote id1
    printf 'who?\n' > "$IDV/who.md"
    out=$(vault_sync "$IDV" 0 "" /w 2>&1)
    grep -q "pushed (after rebasing" <<<"$out" || fail "V24: vault_sync with no identity and a signing key — $out"
    [[ "$(git --git-dir="$REMOTE" log -1 --format='%ae %ce')" == "harness@localhost harness@localhost" ]] \
        || fail "V24: harness's identity should be used — $(git --git-dir="$REMOTE" log -1 --format='%ae %ce')"
    moveremote id2
    printf 'who again?\n' >> "$IDV/who.md"
    # shellcheck disable=SC2086
    $PY -c 'import sys; sys.path.insert(0, sys.argv[1]); import vault_ingest as vi; vi.commit(sys.argv[2], "py-id")' \
        "$REPO_ROOT/scripts" "$IDV" 2>&1
    [[ "$(git --git-dir="$REMOTE" log -1 --format='%s %ce')" == "py-id harness@localhost" ]] \
        || fail "V24: python's commit and rebase with no identity and a signing key"
    exit 0
) || exit 1
ok "V24: with no git identity and a signing key, harness commits and rebases as harness@localhost, unsigned"

# --- V25: a rebase that conflicts only on generated files takes the remote's --
GEN="$TMP_ROOT/gen.git"
git init -q --bare "$GEN"
GA="$TMP_ROOT/ga"; GB="$TMP_ROOT/gb"; GC="$TMP_ROOT/gc"
git clone -q "$GEN" "$GA" 2>/dev/null
mkdir -p "$GA/wiki/code/x" "$GA/wiki/notes" "$GA/.raw/notes"
genpage() { printf -- '---\ntype: code\nsummary: %s\ngenerated: harness\n---\n\n# x\n\n%s\n' "$2" "$2" > "$1"; }
genpage "$GA/wiki/code/x/x.md" base
printf -- '---\ntype: index\ngenerated: harness\n---\n\nbase\n' > "$GA/wiki/index.md"
printf -- '---\ntype: note\nsummary: a note\n---\n\nbase\n' > "$GA/wiki/notes/n.md"
git -C "$GA" add -A; git -C "$GA" commit -qm base; git -C "$GA" push -q origin HEAD 2>/dev/null
git clone -q "$GEN" "$GB" 2>/dev/null; git clone -q "$GEN" "$GC" 2>/dev/null
mkdir -p "$GB/.raw/notes" "$GC/.raw/notes"
genpage "$GA/wiki/code/x/x.md" "from a"; printf 'from a\n' >> "$GA/wiki/index.md"
git -C "$GA" commit -qam a1; git -C "$GA" push -q 2>/dev/null
genpage "$GB/wiki/code/x/x.md" "from b"; printf 'from b\n' >> "$GB/wiki/index.md"
printf 'b raw\n' > "$GB/.raw/notes/b.md"
out=$(vault_sync "$GB" 0 "" /w 2>&1)
grep -q "pushed (after rebasing on the remote and regenerating" <<<"$out" || fail "V25: generated conflicts should be resolved — $out"
[[ "$(git -C "$GB" rev-parse HEAD)" == "$(git --git-dir="$GEN" rev-parse HEAD)" ]] || fail "V25: not pushed"
[[ ! -d "$GB/.git/rebase-merge" && ! -d "$GB/.git/rebase-apply" ]] || fail "V25: a rebase was left stopped"
grep -q '^from a$' "$GB/wiki/code/x/x.md" || fail "V25: the remote's generated page should win — $(cat "$GB/wiki/code/x/x.md")"
git --git-dir="$GEN" cat-file -e HEAD:.raw/notes/b.md || fail "V25: the local raw file should be pushed"
grep -q '^# Index$' "$GB/wiki/index.md" && grep -q '\[\[x\]\]' "$GB/wiki/index.md" \
    || fail "V25: the index should be regenerated — $(cat "$GB/wiki/index.md")"
# Python: the same with the third clone.
git -C "$GA" pull -q 2>/dev/null
genpage "$GA/wiki/code/x/x.md" "from a again"; git -C "$GA" commit -qam a2; git -C "$GA" push -q 2>/dev/null
genpage "$GC/wiki/code/x/x.md" "from c"; printf 'c raw\n' > "$GC/.raw/notes/c.md"
# shellcheck disable=SC2086
$PY -c 'import sys; sys.path.insert(0, sys.argv[1]); import vault_ingest as vi; vi.commit(sys.argv[2], "py-gen")' \
    "$REPO_ROOT/scripts" "$GC" 2>&1
[[ "$(git -C "$GC" rev-parse HEAD)" == "$(git --git-dir="$GEN" rev-parse HEAD)" ]] || fail "V25: python should resolve and push"
grep -q '^from a again$' "$GC/wiki/code/x/x.md" && git --git-dir="$GEN" cat-file -e HEAD:.raw/notes/c.md \
    || fail "V25: python: the remote's generated page wins, the raw file goes up"
# A conflict on a page someone wrote: aborted, the commit stays local.
git -C "$GA" pull -q 2>/dev/null
printf 'a writes\n' >> "$GA/wiki/notes/n.md"; git -C "$GA" commit -qam a3; git -C "$GA" push -q 2>/dev/null
git -C "$GB" pull -q 2>/dev/null
printf 'b writes\n' >> "$GB/wiki/notes/n.md"; git -C "$GA" pull -q 2>/dev/null
printf 'a writes more\n' >> "$GA/wiki/notes/n.md"; git -C "$GA" commit -qam a4; git -C "$GA" push -q 2>/dev/null
out=$(vault_sync "$GB" 0 "" /w 2>&1)
grep -q "push failed" <<<"$out" || fail "V25: a conflict on a written page should fail the push — $out"
[[ ! -d "$GB/.git/rebase-merge" && ! -d "$GB/.git/rebase-apply" ]] || fail "V25: the rebase should be aborted"
[[ "$(git -C "$GB" rev-list --count '@{u}..')" == 1 ]] || fail "V25: the commit should stay local"
grep -q '^b writes$' "$GB/wiki/notes/n.md" || fail "V25: the local text should be kept"
ok "V25: a rebase conflicting only on generated files takes the remote's, regenerates and pushes; any other aborts"

# --- V26: status, the run file, the lock (exit 3), --unit and forget ---------
VI="$REPO_ROOT/scripts/vault_ingest.py"
RUNF="$V/harness-ingest.run.json"   # $V has no .git: the lock and the run file are in it
st() { $PY "$VI" status --vault "$V" --json "$@"; }
st --quick > "$TMP_ROOT/st.json" || fail "V26: status --quick failed"
$PY - "$TMP_ROOT/st.json" "$TMP_ROOT/there/away" <<'PYEOF' || fail "V26: status --quick — $(cat "$TMP_ROOT/st.json")"
import json, sys
s = json.load(open(sys.argv[1]))
assert set(s) == {"vault", "running", "repos", "raw", "failed", "last_run", "pages"}, sorted(s)
assert s["running"] is None, s["running"]
repos = {r["name"]: r for r in s["repos"]}
assert set(repos) == {"proj", "away"}, repos
assert repos["away"] == {"name": "away", "path": sys.argv[2], "exists": True, "units": 1, "ok": 1, "failed": 0,
                         "pending": None}, repos["away"]
assert repos["proj"]["failed"] == 1 and repos["proj"]["pending"] is None and repos["proj"]["ok"] >= 4, repos["proj"]
assert set(s["raw"]) == {"units", "ok", "failed", "pending"} and s["raw"]["ok"] >= 1 and s["raw"]["pending"] is None
f = s["failed"]
assert len(f) == 1 and f[0]["id"] == "code:proj/src/worse#1" and f[0]["problems"] and f[0]["attempts"] >= 1, f
assert set(f[0]) == {"id", "page", "attempts", "at", "problems"}, f[0]
lr = s["last_run"]
assert lr and lr["model"] == "free-m" and isinstance(lr["written"], int) and lr["at"].endswith("Z"), lr
assert s["pages"] >= 8, s["pages"]
PYEOF
printf 'def more():\n    pass\n' > "$PROJ/src/util/more.py"
st > "$TMP_ROOT/st.json" || fail "V26: status failed"
[[ "$(jq -c '[.repos[] | select(.name == "proj") | .pending, .failed][]' "$TMP_ROOT/st.json" | tr '\n' ' ')" == "1 1 " \
    && "$(jq '.raw.pending' "$TMP_ROOT/st.json")" == 0 ]] \
    || fail "V26: status counts the changed directory as pending — $(cat "$TMP_ROOT/st.json")"
before=$(nreq)
# A watch run, stopped with Ctrl-C (SIGINT) after a pass; meanwhile, look at it.
(
    for _ in $(seq 50); do [[ "$(jq -r '.step' "$RUNF" 2>/dev/null)" == waiting ]] && break; sleep 0.1; done
    cp "$RUNF" "$TMP_ROOT/run.json" 2>/dev/null
    st --quick > "$TMP_ROOT/st-running.json"
    rc=0; ingest > "$TMP_ROOT/second.out" || rc=$?; echo "$rc" > "$TMP_ROOT/second.rc"
    rc=0; $PY "$VI" forget --vault "$V" away > /dev/null 2>&1 || rc=$?; echo "$rc" > "$TMP_ROOT/forget.rc"
) &
watcher=$!
rc=0; timeout -s INT 6 "$PY" "$VI" ingest --vault "$V" --proxy "http://127.0.0.1:$PORT/v1" --model free-m-withtools \
    --watch 1 > /dev/null 2>&1 || rc=$?
wait "$watcher"
[[ "$rc" == 124 ]] || fail "V26: the watch should run until stopped (rc $rc)"
[[ "$(nreq)" == "$(( before + 1 ))" ]] || fail "V26: the watch should send the changed directory once"
$PY - "$TMP_ROOT/run.json" <<'PYEOF' || fail "V26: the run file — $(cat "$TMP_ROOT/run.json" 2>&1)"
import json, re, sys
r = json.load(open(sys.argv[1]))
assert set(r) == {"pid", "started", "beat", "model", "args", "step", "unit", "done", "failed", "left", "watch"}, sorted(r)
iso = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
assert isinstance(r["pid"], int) and re.fullmatch(iso, r["started"]) and re.fullmatch(iso, r["beat"]), r
assert r["model"] == "free-m" and r["step"] == "waiting" and r["unit"] == "" and r["watch"] is True, r
assert r["done"] == 1 and r["failed"] == 0 and r["left"] == 0, r
assert r["args"][:2] == ["ingest", "--vault"] and r["args"][-2:] == ["--watch", "1"], r["args"]
PYEOF
[[ "$(jq -c '.running | [.alive, .step]' "$TMP_ROOT/st-running.json")" == '[true,"waiting"]' ]] \
    || fail "V26: status during a run says it is running — $(cat "$TMP_ROOT/st-running.json")"
[[ "$(cat "$TMP_ROOT/second.rc")" == 3 ]] && grep -q 'another ingest is running on this vault (pid' "$TMP_ROOT/second.out" \
    || fail "V26: a second ingest exits 3 — rc $(cat "$TMP_ROOT/second.rc"): $(cat "$TMP_ROOT/second.out")"
[[ "$(cat "$TMP_ROOT/forget.rc")" == 3 ]] || fail "V26: forget during an ingest exits 3"
[[ ! -e "$RUNF" ]] || fail "V26: the run file should go when the run ends"
# A run file left by a run that is gone (killed): not alive.
printf '{"pid": 999999999, "started": "2026-01-01T00:00:00Z", "beat": "2026-01-01T00:00:05Z", "step": "ingesting"}\n' > "$RUNF"
[[ "$(st --quick | jq -c '.running | [.alive, .step]')" == '[false,"ingesting"]' ]] || fail "V26: a stale run file is not alive"
rm -f "$RUNF"
# --unit: just that unit, whatever its state.
before=$(nreq)
ingest --unit 'code:proj/src#1' >/dev/null || fail "V26: --unit of an ok unit failed"
[[ "$(nreq)" == "$(( before + 1 ))" ]] || fail "V26: --unit should send that one unit"
out=$(ingest --unit 'code:nope/x#1') && fail "V26: an unknown --unit should fail — $out"
[[ "$out" == *"no unit code:nope/x#1 here"* ]] || fail "V26: unknown --unit message — $out"
# forget: units, repo entry and generated pages go; a page someone wrote stays.
printf '# mine\n' > "$V/wiki/code/away/mine.md"
out=$($PY "$VI" forget --vault "$V" away 2>&1) || fail "V26: forget failed — $out"
[[ "$out" == *"kept wiki/code/away/mine.md"*"forgot away: 1 units, 2 pages removed"* ]] || fail "V26: forget output — $out"
[[ ! -e "$V/wiki/code/away/away--lib.md" && ! -e "$V/wiki/code/away/away.md" && -f "$V/wiki/code/away/mine.md" ]] \
    || fail "V26: forget removes the generated pages only — $(ls -R "$V/wiki/code/away")"
grep -q '"away"\|code:away/' "$V/.harness/ingest.json" && fail "V26: forget should drop the repo and its units"
grep -q 'away--lib' "$V/wiki/index.md" && fail "V26: the index should drop the forgotten pages"
grep -q ' forget: codebase away, 1 units, 2 pages removed$' "$V/wiki/log.md" || fail "V26: forget is logged"
out=$($PY "$VI" forget --vault "$V" away 2>&1) && fail "V26: forgetting an unknown codebase should fail"
[[ "$out" == *"no codebase named away (it has: proj)"* ]] || fail "V26: unknown forget message — $out"
rm -f "$V/wiki/code/away/mine.md"
# harness ingest --status / --forget: the active vault's local copy.
(
    state_root="$TMP_ROOT/st26"; mkdir -p "$state_root/vaults"; ln -s "$V" "$state_root/vaults/t26"
    vault_active_name() { echo t26; }
    vault_prepare() { echo "$state_root/vaults/t26"; }
    out=$(cmd_ingest --status --quick --json) || fail "V26: harness ingest --status failed — $out"
    [[ "$(jq -r '.repos[0].name' <<<"$out")" == proj ]] || fail "V26: harness ingest --status --json — $out"
    cmd_ingest --status | grep -q '^code:     proj (' || fail "V26: harness ingest --status text"
    cmd_ingest --json 2>/dev/null && fail "V26: --json alone should be refused"
    cmd_ingest --forget proj "$PROJ" 2>/dev/null && fail "V26: --forget with a <dir> should be refused"
    rc=0; cmd_ingest --forget nope >/dev/null 2>&1 || rc=$?
    [[ "$rc" == 1 ]] || fail "V26: harness ingest --forget of an unknown codebase (rc $rc)"
    true
) || exit 1
ok "V26: status (--json, --quick), the run file while an ingest runs, exit 3 on the lock, --unit, forget, harness ingest --status/--forget"

# --- V27: domain documents, the glossary, ticket context and candidates ------
# The mock answers in the format the prompt asks for (Terms/Rules for a
# domain document, Keep for a session), with one quote that is not in the
# source each time: that line, and only that line, must be dropped.
MOCK27="$TMP_ROOT/mock27.py"
REQS27="$TMP_ROOT/requests27.jsonl"
cat > "$MOCK27" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
DOCS = [
    ("Net Asset Value", "summary: fund valuation\n### Terms\n"
     "- Net Asset Value (NAV): fund assets minus liabilities \"value of a fund's assets minus its liabilities\"\n"
     "- Widget: a made up thing \"widgets are always blue\"\n### Rules\n"
     "- The NAV is published by 6 pm each business day \"must be published by 6 pm\"\n"
     "- Trades settle within two business days \"must settle within two business days\"\nTopics: [[valuation]]"),
    ("Settlement Date", "summary: settlement\n### Terms\n"
     "- **Settlement Date**: the day a trade is paid \"the day a trade is paid for\"\n### Rules\n- none\nTopics: [[settlement]]"),
    ("clearing house", "summary: clearing\n### Terms\n"
     "- clearing house: sits between the two sides “stands between buyer and seller”\n### Rules\n- none\nTopics: [[clearing]]"),
    ("ALLBAD", "summary: nothing\n### Terms\n- Foo: bar \"this is not in the source at all\"\n### Rules\n- none\nTopics: [[x]]"),
    ("we chose sqlite", "summary: storage choice\n### What happened\nThe storage engine was chosen for the host.\n"
     "### Keep\n- [decision] Use sqlite: the host has no database server \"the host has no server\"\n"
     "- [gotcha] something invented \"this never was said here\"\nTopics: [[storage]]"),
]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self.reply({"data": [{"id": "free-m"}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = req["messages"][0]["content"]
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps({"n": len(req["messages"]), "prompt": prompt}) + "\n")
        src = prompt.split("<<<BEGIN_SOURCE>>>", 1)[1].split("<<<END_SOURCE>>>", 1)[0]
        domain = "Full Name (ABBR)" in prompt
        text = next((t for k, t in DOCS if k in src and ("### Terms" in t) == domain),
                    "summary: covers notes\n### What happened\nThe source was read and summed up here.\nTopics: [[misc]]")
        self.reply({"choices": [{"message": {"role": "assistant", "content": text}}]})
srv = HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
# shellcheck disable=SC2086
$PY "$MOCK27" 0 "$REQS27" > "$TMP_ROOT/mock27.port" 2>/dev/null &
MOCK27_PID=$!
trap 'kill "$MOCK_PID" "$JIRA_PID" "$MOCK27_PID" 2>/dev/null; rm -rf "$TMP_ROOT"' EXIT
for _ in $(seq 50); do [[ -s "$TMP_ROOT/mock27.port" ]] && break; sleep 0.1; done
PORT27=$(head -n 1 "$TMP_ROOT/mock27.port")
[[ -n "$PORT27" ]] || fail "V27: the mock upstream did not start"
DV="$TMP_ROOT/domvault"
cp -R "$REPO_ROOT/vault-template" "$DV"
printf -- "---\ntype: overview\nsummary: 'funds'\nproject: 'funds'\nticket_pattern: '[A-Z]+-[0-9]+'\n---\n" > "$DV/wiki/overview.md"
mkdir -p "$DV/.raw/domain" "$DV/.raw/sessions" "$DV/.raw/tickets"
printf '# Funds\n\nThe Net Asset Value (NAV) is the value of a fund'"'"'s assets minus its liabilities.\n\n## Rules\n\nThe NAV must be published by 6 pm every business day. Every trade must settle within two business days.\n' \
    > "$DV/.raw/domain/funds.md"
printf '<html><body><h1>Clearing</h1><p>A clearing house stands between buyer and seller.</p><script>evilScript()</script></body></html>\n' \
    > "$DV/.raw/domain/clearing.html"
printf 'ALLBAD: nothing useful in this document at all.\n' > "$DV/.raw/domain/bad.txt"
printf 'not a pdf\n' > "$DV/.raw/domain/scan.pdf"
$PY - "$DV/.raw/domain/terms.docx" <<'PYEOF'
import sys, zipfile
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
doc = (f'<?xml version="1.0"?><w:document {W}><w:body>'
       '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Settlement</w:t></w:r></w:p>'
       '<w:p><w:r><w:t>Settlement Date: the day a trade is paid for </w:t></w:r><w:r><w:t>and delivered.</w:t></w:r></w:p>'
       '</w:body></w:document>')
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("word/document.xml", doc)
PYEOF
printf -- '---\ntype: session\ntitle: storage\n---\n## User\nWhich database?\n## Assistant\nwe chose sqlite because the host has no server to run one.\n' \
    > "$DV/.raw/sessions/2026-10-01-abc.md"
printf -- '---\ntype: ticket\nticket: ABC-9\ntitle: Fix the NAV rounding\n---\n# ABC-9: Fix the NAV rounding\n\n> This is the ticket as people wrote it in the tracker. The label mentions a Settlement Date.\n\nThe NAV is off by a cent.\n' \
    > "$DV/.raw/tickets/ABC-9.md"
printf -- '---\ntype: ticket\nticket: ABC-10\ntitle: Move the nav bar\n---\n# ABC-10: Move the nav bar\n\nThe nav bar should be on the left.\n' \
    > "$DV/.raw/tickets/ABC-10.md"
out=$($PY "$VI" ingest --vault "$DV" --proxy "http://127.0.0.1:$PORT27/v1" --model free-m 2>&1) || fail "V27: ingest failed — $out"
grep -q 'scan.pdf: .*skipped' <<<"$out" || fail "V27: an unreadable pdf is skipped out loud — $out"
[[ -z "$(find "$DV/wiki" -name '*scan*')" ]] || fail "V27: the pdf should have no page"
for f in domain--funds domain--clearing domain--terms; do
    grep -q '^type: domain$' "$DV/wiki/domain/$f.md" 2>/dev/null || fail "V27: $f should be a domain page — $(ls "$DV/wiki/domain" 2>&1) $out"
done
grep -q 'Widget' "$DV/wiki/domain/domain--funds.md" && fail "V27: a line whose quote is not in the source is dropped"
grep -q 'Net Asset Value (NAV)' "$DV/wiki/domain/domain--funds.md" || fail "V27: a quoted line is kept — $(cat "$DV/wiki/domain/domain--funds.md")"
$PY - "$REQS27" <<'PYEOF' || fail "V27: requests — $(head -c 800 "$REQS27")"
import json, sys
rows = [json.loads(l)["prompt"] for l in open(sys.argv[1])]
html = [p for p in rows if "clearing house" in p]
assert html and not any("evilScript" in p for p in html), "html: text only, no script"
assert any("## Settlement" in p.split("<<<BEGIN_SOURCE>>>")[1] for p in rows), "docx: a heading is a cut point"
assert any("### Terms" in p and "Full Name (ABBR)" in p for p in rows), "domain format and rules"
assert any("### Keep" in p for p in rows if "we chose sqlite" in p), "sessions are asked for Keep lines"
PYEOF
$PY - "$DV/.harness/ingest.json" <<'PYEOF' || fail "V27: manifest — $(cat "$DV/.harness/ingest.json")"
import json, sys
u = json.load(open(sys.argv[1]))["units"]
bad = u["raw:domain/bad.txt#1"]
assert bad["status"] == "failed" and "quote" in " ".join(bad["problems"]), bad
PYEOF
G="$DV/wiki/domain/glossary.md"
grep -qx -- '- \*\*Net Asset Value (NAV)\*\*: fund assets minus liabilities (\[\[domain--funds\]\])' "$G" \
    && grep -q '^- \*\*Settlement Date\*\*: .*\[\[domain--terms\]\]' "$G" && grep -q '^- \*\*clearing house\*\*' "$G" \
    && grep -qx -- '- Trades settle within two business days (\[\[domain--funds\]\])' "$G" \
    || fail "V27: the glossary lists every document's terms and rules — $(cat "$G" 2>&1)"
grep -q 'Widget\|Foo' "$G" && fail "V27: the glossary has a dropped line — $(cat "$G")"
C="$DV/wiki/context/ABC-9-context.md"
grep -q 'Net Asset Value (NAV)' "$C" 2>/dev/null && grep -q 'published by 6 pm' "$C" \
    || fail "V27: a ticket using NAV gets its term and the rules naming it — $(cat "$C" 2>&1)"
grep -q 'settle within' "$C" && fail "V27: a rule not naming the ticket's terms stays out — $(cat "$C")"
grep -q 'Settlement Date' "$C" && fail "V27: the label harness puts on a ticket copy is not matched — $(cat "$C")"
[[ ! -e "$DV/wiki/context/ABC-10-context.md" ]] || fail "V27: 'nav' in lower case is not the abbreviation NAV — $(cat "$DV/wiki/context/ABC-10-context.md")"
grep -q '^- \[decision\] Use sqlite: the host has no database server (\[\[sessions--2026-10-01-abc\]\], 2026-10-01)$' \
    "$DV/wiki/candidates.md" || fail "V27: candidates — $(cat "$DV/wiki/candidates.md" 2>&1)"
grep -q 'invented' "$DV/wiki/candidates.md" && fail "V27: a Keep line without its quote is dropped"
grep -qx '## Domain documents' "$DV/wiki/index.md" && grep -q '\[\[glossary\]\]' "$DV/wiki/index.md" \
    || fail "V27: the index lists the domain pages and the glossary — $(cat "$DV/wiki/index.md")"
grep -q 'candidates\|ABC-9-context' "$DV/wiki/index.md" && fail "V27: candidates and context pages are not in the index"
# The launch's ticket pull rewrites the context; status says nothing about the pdf.
rm "$C"
$PY "$REPO_ROOT/scripts/vault_ticket.py" "$DV" ABC-9 >/dev/null 2>&1
[[ -f "$C" ]] || fail "V27: vault_ticket.py writes the ticket's context"
out=$($PY "$VI" status --vault "$DV" 2>&1) || fail "V27: status failed — $out"
grep -q 'pdf' <<<"$out" && fail "V27: status prints no ingest warnings — $out"
(
    mkdir -p "$TMP_ROOT/h27" "$TMP_ROOT/w27"; cd "$TMP_ROOT/w27"
    harness_vault_dir="$DV" harness_ticket=ABC-9
    got=$(harness_context_files "$TMP_ROOT/h27" | cut -f1 | paste -sd, -)
    [[ "$got" == "vault,ticket,ticket context,ticket state" ]] || fail "V27: context files — $got"
    jq -e --arg c "$C" '.instructions | index($c) != null' <<<"$(vault_opencode_config)" >/dev/null \
        || fail "V27: the overlay hands the ticket's context to the agent"
) || exit 1
ok "V27: domain docs (md, html, docx; a bad pdf skipped) give quote-checked terms, the glossary, ticket context and candidates"

# --- V28: tickets of your own (source: local), never pulled ------------------
tn() { printf '%s\n' "$3" | $PY "$REPO_ROOT/scripts/vault_ticket.py" --new "$TV" "$1" "$2" ${4:+"$4"} 2>&1; }
[[ "$(tn - 'first thing' 'do the first thing')" == LOCAL-1 && "$(tn - 'second' '')" == LOCAL-2 ]] \
    || fail "V28: --new should mint LOCAL-1 then LOCAL-2 — $(ls "$TV/.raw/tickets")"
L1="$TV/.raw/tickets/LOCAL-1.md"
for want in '^source: local$' '^status: open$' '^# LOCAL-1: first thing$' "^> This is the owner's own ticket" '^do the first thing$'; do
    grep -q -- "$want" "$L1" || fail "V28: the copy lacks /$want/ — $(cat "$L1")"
done
grep -q '^second$' "$TV/.raw/tickets/LOCAL-2.md" || fail "V28: no text: the title is the description — $(cat "$TV/.raw/tickets/LOCAL-2.md")"
mkdir -p "$TMP_ROOT/q28/LOCAL-7"
[[ "$(tn - 'past the queue' x "$TMP_ROOT/q28")" == LOCAL-8 ]] || fail "V28: a key the queue has is not minted again"
[[ "$(TICKET_LOCAL_PREFIX=me tn - mine x)" == ME-1 ]] || fail "V28: TICKET_LOCAL_PREFIX names the prefix"
out=$(tn LOCAL-1 again x) && fail "V28: an existing copy was overwritten — $out"
out=$(tn 'not a key' t x) && fail "V28: a bad key accepted — $out"
# ABC-5 is a key the tracker knows the shape of: as your own it is never fetched.
[[ "$(tn ABC-5 'mine, not jira' 'keep it')" == ABC-5 ]] || fail "V28: --new with a key"
: > "$TMP_ROOT/jira.log"
out=$(tk tok123 ABC-5)
[[ "$out" == "ABC-5 (open): mine, not jira [your own ticket, not in the tracker]" ]] || fail "V28: the pull of your own ticket — $out"
[[ ! -s "$TMP_ROOT/jira.log" ]] || fail "V28: your own ticket was asked of the tracker — $(cat "$TMP_ROOT/jira.log")"
grep -q '^keep it$' "$TV/.raw/tickets/ABC-5.md" || fail "V28: the pull changed your own ticket"
[[ -f "$TV/wiki/notes/tickets/ABC-5-state.md" ]] || fail "V28: the pull makes your own ticket's state note"
(
    vault_prepare() { echo "$TV"; }; vault_sync() { echo "sync $1 $harness_ticket" > "$TMP_ROOT/v28.sync"; }
    out=$(printf 'from stdin\n' | cmd_ticket new 'by hand' - 2>&1) || fail "V28: harness ticket new failed — $out"
    [[ "$out" == "ticket LOCAL-9 (your own, not in the tracker): $TV/.raw/tickets/LOCAL-9.md"* ]] || fail "V28: harness ticket new — $out"
    grep -q '^from stdin$' "$TV/.raw/tickets/LOCAL-9.md" || fail "V28: - reads the text from stdin"
    [[ "$(cat "$TMP_ROOT/v28.sync")" == "sync $TV LOCAL-9" ]] || fail "V28: the vault is synced — $(cat "$TMP_ROOT/v28.sync")"
    cmd_ticket new 2>/dev/null && fail "V28: harness ticket new with no title accepted"
    cmd_ticket new t --key 'no' 2>/dev/null && fail "V28: harness ticket new with a bad key accepted"
    exit 0
) || exit 1
ok "V28: tickets of your own: LOCAL-N (past the queue's keys, TICKET_LOCAL_PREFIX), never overwritten, never fetched, harness ticket new"

echo "VAULT TEST PASSED"
