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
    charter '\x15hello world\x1b[D\x1b[D\x1b[D\x1b[D\x1b[Dbig \r' '\r' '\x1b[Hjust \r' '\r' '\r' '\r' \
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
    charter '\r' '\r' '\r' '\r' '\x15\r' '\r' || fail "V14: second init run failed"
    [[ "$(fm ticket_pattern)" == "''" ]] || fail "V14: an emptied ticket pattern should save empty — $(fm ticket_pattern)"
    charter '\r' '\r' '\r' '\r' '\r' '\r' || fail "V14: third init run failed"
    [[ "$(fm ticket_pattern)" == "''" && "$(fm project)" == "'hello big world'" ]] \
        || fail "V14: a re-run should keep the saved answers — project=$(fm project) ticket_pattern=$(fm ticket_pattern)"
    ok "V14: vault init edits pre-filled answers in place, writes the charter, keeps answers on a re-run"
else
    ok "V14: skipped (no pty in this Python)"
fi

echo "VAULT TEST PASSED"
