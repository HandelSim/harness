#!/usr/bin/env bash
#
# tests/unit_probe_oai_test.sh — exercise `harness probe oai` (cmd_probe +
# OaiProber in scripts/probe_upstream.py) against a local stdlib mock of an
# OpenAI-format upstream, no docker.
#
# The mock serves /responses (JSON and SSE), /responses/compact, DELETE
# /responses/{id} and /chat/completions under a secret path prefix. It
# reports cached_tokens as an exact-prefix cache would (the longest common
# prefix with an earlier request under the same prompt_cache_key, in
# 128-token steps from 1024), rejects prompt_cache_retention (as GPT-5.6
# does), summary=concise and reasoning_effort=none, bills earlier turns again
# behind previous_response_id, bills replayed encrypted reasoning unless
# reasoning.context=current_turn, answers the first chat request with a 429,
# and echoes secrets in every reply. Asserts:
#   T1 'harness probe oai --help' prints the suite's usage, needs no config
#   T2 missing OAI_API_URL/KEY -> non-zero with a clear error
#   T3 a full run exits 0 with every section and a verdict that reads the
#      mock right: cache hits per step, retention rejected, options taken,
#      earlier turns billed again, summaries (streamed, detailed, on a tool
#      step), concise rejected, current_turn cheaper, the chat route falling
#      back to reasoning_effort=low after a waited-out 429, compaction
#   T4 none of the secrets appear in stdout or in the redacted log
#   T5 what the mock received: store off except section 3, no cache key in
#      the no-key arm, both stored responses deleted
#   T6 --only 4 runs the preflight and section 4 only
#
# Prints "PROBE OAI TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " harness probe oai unit test"
echo "============================================================"

fail() { echo "[probe-oai-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[probe-oai-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v python3 >/dev/null 2>&1 || fail "this test needs host python3"

TMP_ROOT="$(mktemp -d)"
MOCK_PID=""
cleanup() {
    [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null || true
    [[ -n "${KEEP_TMP:-}" ]] || rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$TMP_ROOT/scripts"
cp "$REPO_ROOT/scripts/probe_upstream.py" "$TMP_ROOT/scripts/"

KEY="sk-test-OAIPROBEKEY-1a2b3c4d5e6f7788"
PREFIX="zqoaisecretprefix"
ORG="AcmeInternalOrg"

cat >"$TMP_ROOT/mock.py" <<'PYEOF'
import json, os, re, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE = sys.argv[1]
PREFIX = "/" + sys.argv[2] + "/openai/v1"
REC = os.path.join(os.path.dirname(PORT_FILE), "oai-rec.txt")
LOCK = threading.Lock()
SEEN = {}     # cache key -> earlier serialized prefixes
STORED = {}   # response id -> (input tokens, text of its input)
STATE = {"n": 0, "chat": 0}


def leak(h):
    return ("auth=%s frag=tok_OAIPROBEKEY-1a2b host=%s mail=admin@secretcorp.example ip=10.1.2.3 "
            "see https://unlock.secretcorp.example/u?k=1 org AcmeInternalOrg"
            % (h.headers.get("Authorization"), h.headers.get("Host")))


def toks(s):
    return len(s) // 4


def cached(key, s):
    """An exact-prefix cache: the longest prefix shared with an earlier
    request under the same key, counted from 1024 tokens in 128s."""
    with LOCK:
        prev = SEEN.setdefault(key, [])
        best = max([len(os.path.commonprefix([p, s])) for p in prev] or [0])
        prev.append(s)
    t = toks(" " * best)
    return t // 128 * 128 if t >= 1024 else 0


def puzzle(text):
    m = re.search(r"Numbers: ([\d, ]+)\.", text or "")
    if not m:
        return None
    xs = [int(x) for x in m.group(1).split(",")]
    return sum(a * b for a, b in zip(xs, xs[1:])) % 97


def text_of(inp):
    if isinstance(inp, str):
        return inp
    return " ".join(str(it.get("content") or it.get("output") or "") for it in inp if isinstance(it, dict))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json", headers=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def bad(self, msg):
        self.send(400, {"error": {"type": "invalid_request_error", "message": msg + " " + leak(self)}})

    def rec(self, line):
        with LOCK, open(REC, "a") as f:
            f.write(line + "\n")

    def do_DELETE(self):
        rid = self.path.rsplit("/", 1)[-1]
        self.rec("DELETE %s %s" % (rid, rid in STORED))
        STORED.pop(rid, None)
        self.send(200, {"id": rid, "object": "response.deleted", "deleted": True})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + os.environ["MOCK_KEY"]:
            return self.send(401, {"error": {"message": "bad key"}})
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else None
        self.rec("%s key=%s store=%s" % (path, "prompt_cache_key" in req, req.get("store")))
        if path == "/chat/completions":
            return self.chat(req)
        if path == "/responses/compact":
            return self.send(200, {"object": "response.compaction", "output": [
                {"type": "compaction", "encrypted_content": "Z" * 400}],
                "usage": {"input_tokens": toks(json.dumps(req["input"])), "output_tokens": 100}})
        if path != "/responses":
            return self.send(404, {"error": {"message": "no route"}})
        if "prompt_cache_retention" in req:
            return self.bad("Unsupported parameter: 'prompt_cache_retention'.")
        rs = req.get("reasoning") or {}
        if rs.get("summary") == "concise":
            return self.bad("Unsupported value: 'concise' for reasoning.summary.")
        inp = req.get("input")
        items = inp if isinstance(inp, list) else [{"role": "user", "content": inp}]
        ctx = rs.get("context") or "all_turns"
        # current_turn drops replayed reasoning from what is billed
        billed_items = [it for it in items if not (ctx == "current_turn" and it.get("type") == "reasoning")]
        s = json.dumps([req.get("tools"), req.get("instructions"), billed_items])
        n_in = toks(s)
        prev = req.get("previous_response_id")
        hist = text_of(items)
        if prev:
            if prev not in STORED:
                return self.bad("Previous response not found.")
            n_in += STORED[prev][0]
            hist = STORED[prev][1] + " " + hist
        c = cached(req.get("prompt_cache_key"), s)
        write = (n_in - c) if req.get("prompt_cache_options") else 0
        with LOCK:
            STATE["n"] += 1
            rid = "resp_%d" % STATE["n"]
        ans = puzzle(hist)
        user = text_of([it for it in items if it.get("role") == "user"][-1:])
        out, summary = [], None
        if rs.get("summary"):
            summary = "**Working it out**\n\nI multiply the adjacent pairs and add them. " + leak(self)
        if summary:
            out.append({"type": "reasoning", "id": "rs_%d" % STATE["n"], "encrypted_content": "E" * 800,
                        "summary": [{"type": "summary_text", "text": summary}]})
        elif rs:
            out.append({"type": "reasoning", "id": "rs_%d" % STATE["n"], "encrypted_content": "E" * 800, "summary": []})
        if req.get("tool_choice") == "auto" and req.get("tools"):
            out.append({"type": "function_call", "call_id": "call_g1", "name": "grep",
                        "arguments": json.dumps({"pattern": "def handle_cache"})})
        else:
            m = re.search(r"code word ([a-z]+-[a-z]+)", hist)
            if "Add 7" in user and ans is not None:
                txt = str(ans + 7)
            elif ans is not None:
                txt = str(ans)
            elif "What was the code word" in user and m:
                txt = m.group(1)
            else:
                txt = "OK " + leak(self)
            out.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": txt}]})
        if req.get("context_management"):
            out.insert(0, {"type": "compaction", "encrypted_content": "C" * 300})
        usage = {"input_tokens": n_in, "input_tokens_details": {"cached_tokens": c, "cache_write_tokens": write},
                 "output_tokens": 20 + (60 if rs else 0), "output_tokens_details": {"reasoning_tokens": 60 if rs else 0}}
        resp = {"id": rid, "object": "response", "model": req.get("model"), "status": "completed",
                "output": out, "usage": usage, "reasoning": {"context": ctx} if rs else None}
        if req.get("store"):
            STORED[rid] = (n_in, hist)
        if not req.get("stream"):
            return self.send(200, resp)
        evs = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}}]
        if summary:
            evs.append({"type": "response.reasoning_summary_part.added", "summary_index": 0})
            for i in range(0, len(summary), 40):
                evs.append({"type": "response.reasoning_summary_text.delta", "delta": summary[i:i + 40]})
            evs.append({"type": "response.reasoning_summary_text.done", "text": summary})
        for it in out:
            if it["type"] == "message":
                evs.append({"type": "response.output_text.delta", "delta": it["content"][0]["text"]})
        evs.append({"type": "response.completed", "response": resp})
        body = "".join("event: %s\ndata: %s\n\n" % (e["type"], json.dumps(e)) for e in evs)
        self.send(200, raw=body.encode(), ctype="text/event-stream")

    def chat(self, req):
        with LOCK:
            STATE["chat"] += 1
            first = STATE["chat"] == 1
        if first:
            return self.send(429, {"error": {"message": "Rate limit reached; retry after 1 second."}},
                             headers={"retry-after": "1"})
        if req.get("reasoning_effort") == "none":
            return self.bad("Unsupported value: 'none' for reasoning_effort.")
        s = json.dumps([req.get("tools"), req.get("messages")])
        c = cached(req.get("prompt_cache_key"), s)
        self.send(200, {"id": "c1", "object": "chat.completion", "model": req.get("model"),
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "OK " + leak(self)}}],
                        "usage": {"prompt_tokens": toks(s), "completion_tokens": 3,
                                  "prompt_tokens_details": {"cached_tokens": c}}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
PYEOF

start_mock() {
    rm -f "$TMP_ROOT/port" "$TMP_ROOT/oai-rec.txt"
    MOCK_KEY="$KEY" python3 "$TMP_ROOT/mock.py" "$TMP_ROOT/port" "$PREFIX" &
    MOCK_PID=$!
    local _
    for _ in $(seq 1 50); do
        [[ -s "$TMP_ROOT/port" ]] && break
        sleep 0.1
    done
    [[ -s "$TMP_ROOT/port" ]] || fail "mock upstream did not start"
    PORT=$(cat "$TMP_ROOT/port")
}
stop_mock() { kill "$MOCK_PID" 2>/dev/null || true; wait "$MOCK_PID" 2>/dev/null || true; MOCK_PID=""; }

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_probe)" == "function" ]] || fail "cmd_probe not sourced"
set +e

# --- T1: --help ---
out=$(OAI_API_URL="" OAI_API_KEY="" cmd_probe oai --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe oai"* && "$out" == *"reasoning.context"* ]] \
    || fail "T1 oai --help: rc=$rc out=$out"
out=$(cmd_probe help 2>&1)
[[ "$out" == *"  oai  "* ]] || fail "T1 'harness probe help' does not list oai"
ok "T1 'harness probe oai --help' prints usage; 'probe help' lists it"

# --- T2: missing config ---
out=$(OAI_API_URL="" OAI_API_KEY="" PROXY_API_URL="http://x" PROXY_API_KEY="k" cmd_probe oai 2>&1); rc=$?
[[ $rc -ne 0 && "$out" == *"OAI_API_URL and OAI_API_KEY must be set"* ]] \
    || fail "T2 missing config: rc=$rc out=$out"
ok "T2 missing OAI config errors"

# --- T3: a full run ---
start_mock
URL="http://127.0.0.1:${PORT}/${PREFIX}/openai/v1/responses"
out=$(OAI_API_URL="$URL" OAI_API_KEY="$KEY" OAI_MODELS="probe-deploy, gpt-5.6-terra" PROXY_API_URL="" \
      PROXY_API_KEY="" HARNESS_PROBE_REDACT="$ORG" cmd_probe oai --timeout 20 --delay-scale 0 2>&1); rc=$?
stop_mock
echo "$out" >"$TMP_ROOT/stdout.txt"
[[ $rc -eq 0 ]] || { echo "$out" | tail -30; fail "T3 probe exited $rc"; }
for want in "=== harness probe oai v" "--- 0. preflight" "--- 1. " "--- 2. " "--- 3. " "--- 4. " "--- 5. " "--- 6. " \
            "=== oai verdict ===" "=== end harness probe ===" "model=probe-deploy" \
            "opencode asks probe-deploy for reasoning summaries on its own: NO" \
            "opencode asks gpt-5.6-terra for reasoning summaries on its own: yes" \
            "model takes reasoning: yes; prompt_cache_key: yes" \
            "prompt_cache_retention=24h: rejected (HTTP 400" \
            "prompt_cache_options ttl=12h: taken; write " \
            "earlier turns billed again" "state kept" \
            "no summary asked: 0 chars" "summary=concise: HTTP 400" \
            "1 call(s)" "server says context=current_turn" \
            "/chat/completions (reasoning_effort=low)" "reasoning_effort=none rejected; using low" "429, waiting" \
            "compact: taken; next request input" "context_management: taken; compaction item returned" \
            "full redacted request log: state/output/probe-oai-"; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -60; fail "T3 missing '$want' in output"; }
done
# Steps 2+ of a session hit the cache for most of their input.
pct=$(sed -n 's/^session with prompt_cache_key: steps 2-6 had .* cached (\([0-9]*\)%)$/\1/p' <<<"$out")
[[ -n "$pct" && "$pct" -ge 50 ]] || { echo "$out" | grep -n "session" >&2; fail "T3 session cache share '$pct'"; }
# Summaries came back, streamed and not, and the answers were scored right.
grep -qE '^reasoning summary: .*summary=auto, streamed: [1-9][0-9]* chars; summary=detailed: [1-9][0-9]* chars' <<<"$out" \
    || fail "T3 summaries not reported: $(grep '^reasoning summary' <<<"$out")"
grep -q "answer right" <<<"$out" || fail "T3 no puzzle scored right"
grep -q "summary_text.delta x" <<<"$out" || fail "T3 streamed summary events not counted"
# current_turn bills less than the default (all_turns replays reasoning).
ctx=$(grep '^reasoning.context' <<<"$out")
dflt=$(sed -n 's/.*default: input \([0-9]*\) .*/\1/p' <<<"$ctx")
cur=$(sed -n 's/.*current_turn: input \([0-9]*\) .*/\1/p' <<<"$ctx")
[[ -n "$dflt" && -n "$cur" && "$cur" -lt "$dflt" ]] || fail "T3 reasoning.context: $ctx"
ok "T3 full run: every section and a verdict that matches the mock"

# --- T4: redaction ---
log=$(ls "$TMP_ROOT"/state/output/probe-oai-*.log 2>/dev/null | head -1)
[[ -n "$log" && -s "$log" ]] || fail "T4 no log written under state/output"
for f in "$TMP_ROOT/stdout.txt" "$log"; do
    for secret in "$KEY" "OAIPROBEKEY" "tok_OAIP" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T4 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
grep -qF "Bearer <redacted>" "$log" || fail "T4 expected 'Bearer <redacted>' in the log"
ok "T4 no secrets in stdout or log"

# --- T5: what the mock received ---
recf="$TMP_ROOT/oai-rec.txt"
[[ $(grep -c '^DELETE resp_[0-9]* True$' "$recf") -eq 2 ]] || { cat "$recf" >&2; fail "T5 stored responses not both deleted"; }
[[ $(grep -c '^/responses key=True store=True$' "$recf") -eq 2 ]] || fail "T5 expected exactly 2 stored requests"
[[ $(grep -c '^/responses key=False ' "$recf") -eq 6 ]] || fail "T5 the no-key arm should send 6 requests without a key"
! grep -q '^/responses key=True store=None' "$recf" || fail "T5 a /responses request went without store=false"
ok "T5 store off except section 3, no key in the no-key arm, stored responses deleted"

# --- T6: --only ---
start_mock
out=$(OAI_API_URL="http://127.0.0.1:${PORT}/${PREFIX}/openai/v1" OAI_API_KEY="$KEY" OAI_MODELS="probe-deploy" \
      timeout 60 python3 "$TMP_ROOT/scripts/probe_upstream.py" --suite oai --only 4 --timeout 20 2>&1); rc=$?
stop_mock
[[ $rc -eq 0 && "$out" == *"--- 0. preflight"* && "$out" == *"--- 4. "* && "$out" != *"--- 1. "* \
   && "$out" != *"--- 6. "* ]] || { echo "$out" | tail -20; fail "T6 --only 4: rc=$rc"; }
ok "T6 --only 4 runs the preflight and section 4 only"

echo "PROBE OAI TEST PASSED"
