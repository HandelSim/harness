#!/usr/bin/env bash
#
# tests/unit_zephyr_test.sh — `harness tests` and scripts/vault_tests.py
# without docker or network: a mock Zephyr on 127.0.0.1 plays Zephyr Scale
# (Data Center, /rest/atm/1.0), Zephyr Squad (Data Center, Jira search + ZAPI)
# and Zephyr Scale Cloud (API v2 + Jira's search for issue keys).
#
#   Z1  Scale DC: detected, paged (205 cases), one file per case in the
#       canonical form (HTML as text, secrets redacted), index + guide,
#       an unchanged second pull, a case deleted upstream removed, an empty
#       answer keeping the vault's copy
#   Z2  Squad DC: detected, steps from ZAPI, linked issues as covers
#   Z3  Scale Cloud: ZEPHYR_TOKEN, a 429 waited out, folder/status/priority
#       names behind ids, issue ids turned into keys
#   Z4  export: CSV (one row per step), XML (parses, steps, issues), XLSX (a
#       valid zip with the rows), HTML; a draft already in Zephyr, one with a
#       key and one with no name left out; the same bytes on a second run
#   Z5  safety: only GETs reach the mock, the token is in no vault file, an
#       http tracker_url that is not local is refused before any request
#   Z6  harness: ingest skips .raw/tests, the guide is an instruction file of
#       every launch, ZEPHYR_TOKEN is a secret var, vault_sync's export line
#
# Prints "ZEPHYR TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " zephyr tests unit test"
echo "============================================================"

fail() { echo "[zephyr-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[zephyr-test] OK: $*"; }

command -v git >/dev/null 2>&1 || fail "this test needs git"
TMP_ROOT="$(mktemp -d)"
MOCK_PID=""
trap '[[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null; rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT/install" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_tests)" == "function" ]] || fail "cmd_tests not sourced"
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"
PY=$(host_python_bin) || fail "this test needs python 3"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
TESTS="$REPO_ROOT/scripts/vault_tests.py"

# --- the mock --------------------------------------------------------------
MOCK="$TMP_ROOT/zephyr.py"
cat > "$MOCK" <<'PYEOF'
import json, os, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG, STATE = sys.argv[1], sys.argv[2]
def state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}
def scale_cases():
    n = state().get("scale_n", 205)
    out = []
    for i in range(1, n + 1):
        tc = {"key": f"PROJ-T{i}", "name": f"Case {i}", "folder": "/Auth/Login" if i % 2 else "/Reports",
              "status": "Approved", "priority": "Normal", "labels": ["smoke"] if i % 3 == 0 else [],
              "component": "Auth", "issueLinks": [f"PROJ-{100 + i % 5}"], "updatedOn": "2026-10-01T00:00:00.000Z",
              "objective": "<p>Check <b>login</b> &amp; logout</p>", "precondition": "User exists",
              "testScript": {"type": "STEP_BY_STEP", "steps": [
                  {"index": 1, "description": "Enter password<br>then submit", "testData": "pw: sk-abcdefghijklmnopqrstuvwxyz0123", "expectedResult": "<p>Home page</p>"},
                  {"index": 0, "description": "Open the login page", "testData": "", "expectedResult": "Form shown"}]}}
        if i == 7:
            tc["testScript"] = {"type": "PLAIN_TEXT", "text": "<p>Just look at it</p>"}
        if i == 8:
            del tc["testScript"]   # a search answer without the script: fetched alone
        out.append(tc)
    return out
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, headers=()):
        b = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def other(self):
        open(LOG, "a").write(f"{self.command} {self.path}\n")
        self.send(405, {})
    do_POST = do_PUT = do_DELETE = do_PATCH = other
    def do_GET(self):
        open(LOG, "a").write(f"GET {self.path}\n")
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        auth = self.headers.get("Authorization")
        if p.startswith("/cloud/"):
            if auth != "Bearer ztok":
                return self.send(401, {})
            p = p[len("/cloud/v2"):]
            st = state()
            if p == "/testcases":
                if not st.get("throttled"):
                    st["throttled"] = 1; json.dump(st, open(STATE, "w"))
                    return self.send(429, {}, [("Retry-After", "0")])
                vals = [{"key": "CL-T1", "name": "Cloud one", "folder": {"id": 2}, "status": {"id": 10},
                         "priority": {"id": 20}, "labels": ["ui"], "links": {"issues": [{"issueId": 5001}]},
                         "testScript": {"self": "http://x/v2/testcases/CL-T1/teststeps"}},
                        {"key": "CL-T2", "name": "Cloud two", "folder": None, "status": {"id": 10},
                         "priority": {"id": 20}, "labels": [], "links": {"issues": []},
                         "testScript": {"self": "http://x/v2/testcases/CL-T2/testscript"}}]
                start, n = int(q.get("startAt", 0)), int(q.get("maxResults", 1))
                return self.send(200, {"values": vals[start:start + n], "isLast": start + n >= len(vals)})
            if p == "/folders":
                return self.send(200, {"values": [{"id": 1, "parentId": None, "name": "Auth"},
                                                  {"id": 2, "parentId": 1, "name": "Login"}], "isLast": True})
            if p == "/statuses":
                return self.send(200, {"values": [{"id": 10, "name": "Draft"}], "isLast": True})
            if p == "/priorities":
                return self.send(200, {"values": [{"id": 20, "name": "High"}], "isLast": True})
            if p == "/testcases/CL-T1/teststeps":
                return self.send(200, {"values": [{"inline": {"description": "Click <i>Go</i>", "testData": None,
                                                              "expectedResult": "It goes"}}], "isLast": True})
            if p == "/testcases/CL-T2/testscript":
                return self.send(200, {"type": "plain", "text": "Plain words"})
            return self.send(404, {})
        if auth != "Bearer tok123":
            return self.send(401, {})
        if p.startswith("/guarded/jira/rest/atm/"):   # Scale that does not take the token as logged in
            self.send_response(302)
            self.send_header("Location", "/guarded/jira/login.jsp?permissionViolation=true&os_destination=x")
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if p.startswith("/guarded/jira/rest/zapi/"):
            return self.send(403, {})
        if p == "/scale/rest/atm/1.0/testcase/search":
            cases = scale_cases()
            start, n = int(q.get("startAt", 0)), int(q.get("maxResults", 200))
            n = min(n, state().get("cap", n))   # a server that caps the page size
            return self.send(200, cases[start:start + n])
        if p == "/scale/rest/atm/1.0/testcase/PROJ-T8":
            if state().get("fail_one"):
                return self.send(500, {})
            tc = [c for c in scale_cases() if c["key"] == "PROJ-T8"][0]
            tc["testScript"] = {"type": "STEP_BY_STEP", "steps": [{"index": 0, "description": "Alone", "expectedResult": "Fetched"}]}
            return self.send(200, tc)
        if p.startswith("/squad/rest/atm/"):
            return self.send(404, {})
        if p == "/squad/rest/zapi/latest/moduleInfo":
            return self.send(200, {"version": "1"})
        if p == "/squad/rest/api/2/search":
            issues = [{"id": "900", "key": "SQ-1", "fields": {"summary": "Squad case", "description": "Desc *bold*",
                       "status": {"name": "To Do"}, "priority": {"name": "Major"}, "labels": ["l1"],
                       "components": [{"name": "Core"}], "updated": "2026-10-02",
                       "issuelinks": [{"outwardIssue": {"key": "SQ-50"}}]}}]
            return self.send(200, {"issues": issues, "total": 1, "startAt": 0})
        if p == "/squad/rest/zapi/latest/teststep/900":
            if state().get("fail_one"):
                return self.send(500, {})
            return self.send(200, {"stepBeanCollection": [{"orderId": 2, "step": "Second", "data": "", "result": "R2"},
                                                          {"orderId": 1, "step": "First", "data": "d", "result": "R1"}]})
        if p == "/rest/api/3/search/jql":
            return self.send(200, {"issues": [{"id": "5001", "key": "CL-42"}]})
        return self.send(404, {})
srv = HTTPServer(("127.0.0.1", 0), H)
print(srv.server_address[1], flush=True)
srv.serve_forever()
PYEOF
# shellcheck disable=SC2086
$PY "$MOCK" "$TMP_ROOT/mock.log" "$TMP_ROOT/mock.state" > "$TMP_ROOT/mock.port" 2>/dev/null &
MOCK_PID=$!
for _ in $(seq 50); do [[ -s "$TMP_ROOT/mock.port" ]] && break; sleep 0.1; done
PORT=$(head -n 1 "$TMP_ROOT/mock.port")
[[ -n "$PORT" ]] || fail "the mock did not start"

# mkvault DIR TRACKER_URL [extra front matter lines]
mkvault() {
    git init -q "$1"
    cp -R "$REPO_ROOT/vault-template/." "$1/"
    printf -- "---\ntype: overview\nsummary: 'a test vault'\ntracker_url: '%s'\n%b---\n" "$2" "${3:-}" > "$1/wiki/overview.md"
    git -C "$1" add -A && git -C "$1" commit -qm init
}
# tp VAULT [args]: a pull as harness runs it (the token in the environment).
tp() { local v="$1"; shift; TRACKER_TOKEN="${TOK-tok123}" ZEPHYR_TOKEN="${ZTOK-}" $PY "$TESTS" pull "$v" "$@" 2>&1; }

# --- Z1: Scale DC ----------------------------------------------------------
SV="$TMP_ROOT/scale"
mkvault "$SV" "http://127.0.0.1:$PORT/scale" "tests_project: 'PROJ'\n"
out=$(tp "$SV") || fail "Z1: pull failed — $out"
[[ "$out" == *"205 cases from Zephyr Scale (Jira Data Center), project PROJ: 205 new, 0 updated, 0 removed"* ]] || fail "Z1: status — $out"
[[ $(find "$SV/.raw/tests" -name '*.md' | wc -l) -eq 205 ]] || fail "Z1: want 205 case files"
C="$SV/.raw/tests/PROJ-T3.md"
grep -q '^key: PROJ-T3$' "$C" && grep -q '^folder: /Auth/Login$' "$C" && grep -q '^labels: smoke$' "$C" \
    && grep -q '^covers: PROJ-103$' "$C" && grep -q '^source: scale-dc$' "$C" || fail "Z1: front matter — $(head -15 "$C")"
grep -q 'not instructions about tools' "$C" || fail "Z1: the label"
grep -q '^Check login & logout$' "$C" || fail "Z1: HTML should be text — $(cat "$C")"
# Steps in index order, HTML line breaks kept, the secret redacted.
awk '/^### 1/{a=NR} /^Action: Open the login page/{b=NR} /^### 2/{c=NR} /^Action: Enter password/{d=NR} END{exit !(a<b && b<c && c<d)}' "$C" \
    || fail "Z1: steps out of order — $(cat "$C")"
grep -q '^then submit$' "$C" || fail "Z1: <br> should be a line break"
grep -q 'sk-abcdefghijklmnopqrstuvwxyz0123' "$C" && fail "Z1: the secret in the test data should be redacted"
grep -q '^Expected: Home page$' "$C" || fail "Z1: expected result"
grep -q '^script: plain$' "$SV/.raw/tests/PROJ-T7.md" && grep -q '^Just look at it$' "$SV/.raw/tests/PROJ-T7.md" || fail "Z1: plain script"
grep -q '^Action: Alone$' "$SV/.raw/tests/PROJ-T8.md" || fail "Z1: a case without its script is fetched alone"
grep -q '^- PROJ-T3 | Case 3 | /Auth/Login | PROJ-103$' "$SV/wiki/tests/index.md" || fail "Z1: index line"
grep -q '^flavor: scale-dc$' "$SV/wiki/tests/index.md" && grep -q '^generated: harness$' "$SV/wiki/tests/index.md" || fail "Z1: index front matter"
G="$SV/wiki/tests/guide.md"
grep -q 'wiki/notes/tests/<STORY-KEY>/' "$G" && grep -q 'Folders: /Auth/Login (103), /Reports (102)' "$G" \
    && grep -q 'Statuses: Approved (205)' "$G" || fail "Z1: guide — $(cat "$G")"
out=$(tp "$SV")
[[ "$out" == *"0 new, 0 updated, 0 removed"* ]] || fail "Z1: a second pull should change nothing — $out"
echo '{"scale_n": 204, "throttled": 0}' > "$TMP_ROOT/mock.state"
out=$(tp "$SV")
[[ "$out" == *"204 cases"*"1 removed"* && ! -f "$SV/.raw/tests/PROJ-T205.md" ]] || fail "Z1: a case deleted upstream — $out"
echo '{"scale_n": 0}' > "$TMP_ROOT/mock.state"
out=$(tp "$SV")
[[ "$out" == *"has no cases in project PROJ"* && -f "$SV/.raw/tests/PROJ-T1.md" ]] || fail "Z1: an empty answer must keep the copy — $out"
echo '{"scale_n": 204}' > "$TMP_ROOT/mock.state"
tp "$SV" >/dev/null
# A server that answers fewer than maxResults is paged on, not taken as done
# (or the cases after the first page would be deleted).
echo '{"scale_n": 204, "cap": 50}' > "$TMP_ROOT/mock.state"
out=$(tp "$SV")
[[ "$out" == *"204 cases"*"0 removed"* && -f "$SV/.raw/tests/PROJ-T204.md" ]] || fail "Z1: a capped page size — $out"
# A case whose steps did not come keeps the vault's copy.
echo '{"scale_n": 204, "fail_one": 1}' > "$TMP_ROOT/mock.state"
out=$(tp "$SV")
[[ "$out" == *"PROJ-T8: no steps"*"the vault's copy is kept"*"0 updated, 0 removed"* ]] || fail "Z1: a failed step fetch — $out"
grep -q '^Action: Alone$' "$SV/.raw/tests/PROJ-T8.md" || fail "Z1: a failed step fetch must keep the steps — $(cat "$SV/.raw/tests/PROJ-T8.md")"
echo '{"scale_n": 204}' > "$TMP_ROOT/mock.state"
ok "Z1: Scale DC detected and paged (also when the server caps the page); canonical case files (text, redacted, steps in order), index and guide; unchanged, deleted, empty and half-failed pulls"

# --- Z2: Squad DC ----------------------------------------------------------
QV="$TMP_ROOT/squad"
mkvault "$QV" "http://127.0.0.1:$PORT/squad"
out=$(tp "$QV" --project SQ) || fail "Z2: pull failed — $out"
[[ "$out" == *"1 cases from Zephyr Squad (Jira Data Center), project SQ: 1 new"* ]] || fail "Z2: status — $out"
Q="$QV/.raw/tests/SQ-1.md"
grep -q '^covers: SQ-50$' "$Q" && grep -q '^component: Core$' "$Q" || fail "Z2: front matter — $(cat "$Q")"
awk '/^Action: First/{a=NR} /^Action: Second/{b=NR} END{exit !(a && a<b)}' "$Q" || fail "Z2: steps by orderId — $(cat "$Q")"
grep -q '^Data: d$' "$Q" && grep -q '^Expected: R1$' "$Q" || fail "Z2: data and result"
echo '{"scale_n": 204, "fail_one": 1}' > "$TMP_ROOT/mock.state"
out=$(tp "$QV" --project SQ)
[[ "$out" == *"SQ-1: no steps"*"0 updated"* ]] && grep -q '^Action: First$' "$Q" || fail "Z2: a failed step fetch must keep the copy — $out"
echo '{"scale_n": 204}' > "$TMP_ROOT/mock.state"
ok "Z2: Squad DC detected; Jira issues of type Test with ZAPI steps and linked issues"

# --- Z3: Scale Cloud -------------------------------------------------------
CV="$TMP_ROOT/cloud"
mkvault "$CV" "http://127.0.0.1:$PORT" "tests_project: 'CL'\ntests_url: 'http://127.0.0.1:$PORT/cloud/v2'\n"
out=$(TOK="" tp "$CV") && fail "Z3: no token should fail" || true
[[ "$out" == *"no token"* ]] || fail "Z3: no token — $out"
GV="$TMP_ROOT/guarded"
mkvault "$GV" "http://127.0.0.1:$PORT/guarded/jira" "tests_project: 'MISTPCD'\n"
out=$(tp "$GV") && fail "Z3: a login-page redirect should fail" || true
[[ "$out" == *"Zephyr Scale (/rest/atm/1.0) sent the request to Jira's login page (HTTP 302): the token was not accepted"* \
   && "$out" != *"set tracker_url"* && "$out" == *"Zephyr Squad (/rest/zapi) refused TRACKER_TOKEN (HTTP 403)"* ]] \
    || fail "Z3: a redirect to the login page is the token, not the address — $out"
JV="$TMP_ROOT/jcloud"
mkvault "$JV" "https://example.atlassian.net" "tests_project: 'CL'\n"
out=$(tp "$JV") && fail "Z3: Jira Cloud without ZEPHYR_TOKEN should fail" || true
[[ "$out" == *"needs its own API token: set ZEPHYR_TOKEN"* ]] || fail "Z3: the Jira Cloud hint — $out"
out=$(ZTOK=ztok tp "$CV") || fail "Z3: pull failed — $out"
[[ "$out" == *"2 cases from Zephyr Scale (Jira Cloud), project CL: 2 new"* ]] || fail "Z3: status — $out"
K="$CV/.raw/tests/CL-T1.md"
grep -q '^folder: /Auth/Login$' "$K" && grep -q '^status: Draft$' "$K" && grep -q '^priority: High$' "$K" \
    && grep -q '^covers: CL-42$' "$K" && grep -q '^Action: Click Go$' "$K" || fail "Z3: case — $(cat "$K")"
grep -q '^Plain words$' "$CV/.raw/tests/CL-T2.md" || fail "Z3: plain script"
grep -q 'GET /cloud/v2/testcases?projectKey=CL&startAt=0' "$TMP_ROOT/mock.log" || fail "Z3: the paged case list"
ok "Z3: Scale Cloud with ZEPHYR_TOKEN: a 429 waited out, names behind ids, issue ids as keys"

# --- Z4: export ------------------------------------------------------------
D="$SV/wiki/notes/tests/PROJ-9"
mkdir -p "$D"
cat > "$D/expired.md" <<'MDEOF'
---
name: Login rejects an expired password
folder: /Auth/Login
status: Approved
priority: Normal
labels: smoke
---

## Objective

An expired password is refused, "politely".

## Steps

### 1
Action: Open the login page
Expected: Form shown

### 2
Action: Enter an expired password,
then submit
Data: user=a, pw=old
Expected: Error: password expired
MDEOF
printf -- '---\nname: Case 3\ncovers: PROJ-103\n---\n## Steps\n### 1\nAction: a\nExpected: b\n' > "$D/dup.md"
printf -- '---\nkey: PROJ-T4\nname: Changed\n---\n## Steps\n### 1\nAction: a\nExpected: b\n' > "$D/keyed.md"
printf -- '---\nname: Odd one\nfolder: /Nowhere\n---\n## Steps\n### 1\nAction: a\n' > "$D/odd.md"
out=$($PY "$TESTS" export "$SV") || fail "Z4: export failed — $out"
[[ "$out" == *"2 draft case(s) exported"*"2 left out"* ]] || fail "Z4: status — $out"
[[ "$out" == *"dup.md is already in Zephyr as PROJ-T3"* && "$out" == *"keyed.md has key PROJ-T4"* ]] || fail "Z4: left out — $out"
[[ "$out" == *"odd.md step 1 has no Expected"* && "$out" == *"folder '/Nowhere' is not one the pulled cases use"* ]] || fail "Z4: checks — $out"
E="$SV/wiki/tests/export"
[[ -f "$E/zephyr-import.csv" && -f "$E/zephyr-import.xml" && -f "$E/zephyr-import.html" && ! -f "$E/zephyr-import.xlsx" ]] \
    || fail "Z4: Scale gets CSV + XML + HTML — $(ls "$E")"
$PY - "$E" <<'PYEOF' || fail "Z4: the files"
import csv, sys, xml.etree.ElementTree as ET
d = sys.argv[1]
rows = list(csv.reader(open(f"{d}/zephyr-import.csv", encoding="utf-8-sig", newline="")))
assert rows[0][0] == "Name" and len(rows) == 1 + 2 + 1, rows
first, second = rows[1], rows[2]
assert first[0] == "Login rejects an expired password" and first[3] == "/Auth/Login" and first[9] == "PROJ-9", first
assert second[0] == "" and second[11] == "Enter an expired password,\nthen submit" and second[12] == "user=a, pw=old", second
assert first[1] == 'An expired password is refused, "politely".'
assert rows[3][0] == "Odd one"
root = ET.parse(f"{d}/zephyr-import.xml").getroot()
tcs = root.findall("./testCases/testCase")
assert len(tcs) == 2 and tcs[0].findtext("name") == "Login rejects an expired password"
steps = tcs[0].findall("./testScript/steps/step")
assert [s.get("index") for s in steps] == ["0", "1"]
assert steps[1].findtext("description") == "Enter an expired password,<br />then submit", steps[1].findtext("description")
assert tcs[0].findtext("./issues/issue/key") == "PROJ-9"
h = open(f"{d}/zephyr-import.html", encoding="utf-8").read()
assert "Login rejects an expired password" in h and "already in Zephyr as PROJ-T3" in h and "(left out)" in h
PYEOF
sum1=$(cat "$E"/zephyr-import.* | cksum)
out=$($PY "$TESTS" export "$SV" --quiet)
[[ -z "$out" && "$(cat "$E"/zephyr-import.* | cksum)" == "$sum1" ]] || fail "Z4: an unchanged export is the same bytes and says nothing — $out"
# Squad: the Excel file only, a valid workbook with the test on every row.
mkdir -p "$QV/wiki/notes/tests/SQ-60"
printf -- '---\nname: New squad case\n---\n## Steps\n### 1\nAction: s1\nExpected: e1\n### 2\nAction: s2\nExpected: e2\n' > "$QV/wiki/notes/tests/SQ-60/n.md"
$PY "$TESTS" export "$QV" >/dev/null
[[ -f "$QV/wiki/tests/export/zephyr-import.xlsx" && ! -f "$QV/wiki/tests/export/zephyr-import.csv" ]] || fail "Z4: Squad gets the xlsx"
$PY - "$QV/wiki/tests/export/zephyr-import.xlsx" <<'PYEOF' || fail "Z4: the xlsx"
import re, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
assert z.testzip() is None
assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"} <= set(z.namelist())
sheet = z.read("xl/worksheets/sheet1.xml").decode()
rows = re.findall(r"<row r=\"(\d+)\">", sheet)
assert rows == ["1", "2", "3"], rows
assert sheet.count(">New squad case<") == 2 and ">SQ-60<" in sheet and ">e2<" in sheet
PYEOF
ok "Z4: export: CSV one row per step, XML with steps and issues, Squad's xlsx, HTML; duplicates, keyed and bad drafts reported; deterministic"

# --- Z5: safety ------------------------------------------------------------
if grep -v '^GET ' "$TMP_ROOT/mock.log"; then fail "Z5: only GETs may reach Zephyr or Jira"; fi
if grep -rqE 'tok123|ztok' "$SV" "$QV" "$CV" --exclude-dir=.git; then fail "Z5: a token was written to a vault"; fi
BV="$TMP_ROOT/bad"
mkvault "$BV" "http://jira.example.com" "tests_project: 'PROJ'\n"
: > "$TMP_ROOT/mock.log"
out=$(tp "$BV") && fail "Z5: an http tracker_url should be refused" || true
[[ "$out" == *"is http, which would send the token unencrypted"* ]] || fail "Z5: refusal — $out"
ok "Z5: GET only, no token in the vault, a non-local http URL refused"

# --- Z6: harness -----------------------------------------------------------
mkdir -p "$SV/.raw/notes" && printf 'a note to ingest\n' > "$SV/.raw/notes/a.md"
$PY - "$REPO_ROOT/scripts" "$SV" <<'PYEOF' || fail "Z6: ingest should skip .raw/tests"
import sys
sys.path.insert(0, sys.argv[1])
from vault_ingest import raw_units
src = [u["source"] for u in raw_units(sys.argv[2], 100000)]
assert src == [".raw/notes/a.md"], src[:5]
PYEOF
[[ " ${_harness_secret_vars[*]} " == *" ZEPHYR_TOKEN "* ]] || fail "Z6: ZEPHYR_TOKEN must be unset before opencode starts"
lines=$(cd "$TMP_ROOT" && harness_vault_dir="$SV" harness_ticket="" harness_context_files "$TMP_ROOT/nohome")
grep -q "^tests	$SV/wiki/tests/guide.md$" <<<"$lines" || fail "Z6: the guide is an instruction file — $lines"
printf -- '---\nname: Another\n---\n## Steps\n### 1\nAction: x\nExpected: y\n' > "$D/another.md"
out=$(tests_export_quiet "$SV" 2>&1)
[[ "$out" == "[harness] tests: 3 new test case(s) ready to import into Zephyr: wiki/tests/export/ (harness tests view)"* ]] \
    || fail "Z6: vault_sync's export line — $out"
cmd_tests --help | grep -q 'harness tests pull' || fail "Z6: help"
ok "Z6: ingest skips .raw/tests, the guide reaches every launch, ZEPHYR_TOKEN is a secret, the after-session export"

echo "ZEPHYR TEST PASSED"
