#!/usr/bin/env bash
#
# tests/unit_probe_test.sh — exercise `harness probe` (cmd_probe +
# scripts/probe_upstream.py) against a local stdlib mock upstream, no docker.
#
# The mock answers the OpenAI shapes the probe parses (JSON and SSE replies,
# native tool_calls, streamed tool-call deltas, response_format JSON) and
# deliberately echoes secrets back in every body: the bearer key, a fragment
# of it, the base URL/host, an unlock URL, an email, an IP, a projects/...
# session path, an assist token, and a HARNESS_PROBE_REDACT term. Asserts:
#   T1 'harness probe --help' prints usage, needs no config
#   T2 missing PROXY_API_URL/KEY -> non-zero with a clear error
#   T3 a full run (--skip-long --repeat 1) exits 0 and prints every section,
#      the key-findings block, and PASS lines for native tools / round trip /
#      system reach / json_schema / per-model matrix
#   T4 none of the secrets appear in stdout or in the redacted log
#   T5 the log path is printed relative (state/output/...), not absolute
#   T6 a locked key (401 + unlock_url) aborts early without printing the URL
#   T7 'harness probe help' lists the subcommands; 'memory --help' works
#      (and 'thinking --help', 'slim --help');
#      an unknown subcommand errors
#   T8 'harness probe memory --quick' exits 0 with every section and the
#      verdict (the mock sees only the last message and 502s above 100 KB)
#   T9 none of the secrets appear in the memory suite's stdout or log, and
#      the session id it passes back is scrubbed in the log
#   T9b memory --budget stops a section before a request that would pass it
#   T9c a 429 "Daily credit limit exceeded" stops the full and memory suites
#      at once (rc 3, findings so far, redacted) instead of sending more
#   T10 'harness probe optimize-single' (small sizes, 2 reps) exits 0 with both
#      stages, the verdict tables, a weak zone and a full recommendation; it waits out and
#      retries a 429 (the mock answers the first single-suite request with
#      one), and scores the mock's recall/port/ack correctly
#   T11 none of the secrets appear in its stdout or log; the log has the
#      layouts, one whole example message and the '### results' line
#   T12 the single suite's scorer on tricky replies: a port line with history,
#      a fenced ack line, an injection that is reported rather than obeyed
#   T13 'harness probe thinking' exits 0 with every section and the verdict:
#      the honored thinking_config shape, thoughts split out of content, the
#      400 on both controls, signatures echoed/omitted/corrupted, history
#      billing, and a credits-header fit (the mock bills reasoning)
#   T14 none of the secrets appear in the thinking suite's stdout or log
#   T15 the log carries the report: a 'headers:' line per request (with the
#      credits header, no auth headers) and 'report: ' lines equal to stdout
#   T16 the thinking log has T5s's per-event SSE layout (delta keys, text)
#   T17 'probe_upstream.py --suite slim' runs all 3 arms against a scripted
#      mock agent (task subagent, read, edit, pytest), solves the simulated
#      bug in each, prints per-arm metrics and the slim verdict, no secrets
#   T18 the mock's received requests: arm 1 echoes no extra_content, arms 2
#      and 3 echo every signature (main and subagent), arm 3 uses the candidate
#   T19 --only 13 --repeat 2 picks arms; a missing --candidate or repo errors
#      cleanly (rc 2) before any request; piped on stdin with --root it runs,
#      and without proxy/proxy.py it says the slim descriptions fell back
#   T20 a model that never stops (mock 'slimloop') is bounded: subagent step
#      cap, then the 60-request cap, "(cap hit" printed
#   T21 'harness probe slim' passes the launch clone as --root and logs to
#      state/output/probe-slim-<ts>.log
#
# Prints "PROBE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " harness probe unit test"
echo "============================================================"

fail() { echo "[probe-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[probe-test] OK: $*"; }

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

KEY="sk-test-PROBEKEY-9f8e7d6c5b4a3210"
PREFIX="zqsecretprefix"
ORG="AcmeInternalOrg"

cat >"$TMP_ROOT/mock.py" <<'PYEOF'
import json, os, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/" + sys.argv[2]
LOCKED = sys.argv[3] == "locked"
QUOTA = sys.argv[3] == "quota"
SLIMLOOP = sys.argv[3] == "slimloop"
PORT_FILE = sys.argv[1]
SLIM_REC = os.path.join(os.path.dirname(PORT_FILE), "slim-rec.txt")
SEEN = {"single": 0}
CREDITS = {"left": 10000000}
LEVEL_TOKENS = {"minimal": 4, "low": 40, "medium": 160, "high": 640}


def leak(h):
    # Everything the probe must never print.
    return ("auth=%s frag=tok_PROBEKEY-9f8e7d host=%s mail=admin@secretcorp.example ip=10.1.2.3 "
            "see https://unlock.secretcorp.example/u?k=1 org AcmeInternalOrg" % (h.headers.get("Authorization"), h.headers.get("Host")))


def slim_turn(msgs, h, tools=()):
    """The slim suite's scripted agent: (tool_calls, content) or None when the
    request is not one of its loops. Main: read the test, ask an explore
    subagent, read average.py, edit it, run pytest, answer. Subagent: grep,
    answer. Logs what each request carried to slim-rec.txt."""
    sysm = msgs[0].get("content") if msgs and msgs[0].get("role") == "system" else ""
    user = next((m.get("content") for m in msgs if m.get("role") == "user"), "") or ""
    if "tests/test_average.py fails" in user:
        kind = "main"
    elif user.startswith("Find the definition of mean"):
        kind = "sub"
    else:
        return None
    step = sum(1 for m in msgs if m.get("role") == "assistant")
    hist = [c for m in msgs if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
    sigs = sum(1 for c in hist if c.get("extra_content"))
    which = ("candidate" if "Trust their reports" in sysm else "current") if kind == "main" else \
        ("?" if "You are a subagent" not in sysm else
         "general" if any((t.get("function") or {}).get("name") == "edit" for t in tools) else "explore")
    with open(SLIM_REC, "a") as f:
        f.write("%s sys=%s step=%d tcs=%d sigs=%d\n" % (kind, which, step, len(hist), sigs))
    last = str(msgs[-1].get("content") or "")
    def call(name, args):
        return [{"id": "call_s%d" % step, "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
                 "extra_content": {"google": {"thought_signature": "SIGslim%d" % step}}}]
    if kind == "sub":
        if SLIMLOOP or step == 0:
            if SLIMLOOP:
                return call("read", {"filePath": "/work/proj/src/stats/util.py"}), None
            return call("grep", {"pattern": "def mean", "path": "/work/proj/src"}), None
        return None, "mean is defined at %s " % ("/work/proj/src/stats/average.py:7" if "average.py:7:" in last else "?") + leak(h)
    if SLIMLOOP:
        return call("task", {"description": "find mean", "subagent_type": "explore",
                             "prompt": "Find the definition of mean under /work/proj/src and return file:line."}), None
    script = [("read", {"filePath": "/work/proj/tests/test_average.py"}),
              ("task", {"description": "find mean", "subagent_type": "explore",
                        "prompt": "Find the definition of mean under /work/proj/src and return file:line."}),
              ("read", {"filePath": "/work/proj/src/stats/average.py"}),
              ("edit", {"filePath": "/work/proj/src/stats/average.py", "oldString": "return sum(xs) / (len(xs) - 1)",
                        "newString": "return sum(xs) / len(xs)"}),
              ("bash", {"command": "python -m pytest -q", "description": "Run the tests"})]
    if step < len(script):
        return call(*script[step]), None
    return None, "Fixed mean in src/stats/average.py; pytest: %s " % (
        "6 passed" if "6 passed" in last else "NOT PASSED") + leak(h)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-ratelimit-remaining-credits-day", str(CREDITS["left"]))
        self.end_headers()
        self.wfile.write(body)

    def locked(self):
        if LOCKED:
            self.send(401, {"error": {"type": "unauthorized", "message": "key locked",
                                      "unlock_url": "https://unlock.secretcorp.example/x?key=sk-test-PROBEKEY-9f8e7d6c5b4a3210"}})
            return True
        return False

    def do_GET(self):
        if self.locked():
            return
        if self.path != PREFIX + "/v1/models":
            return self.send(404, {"error": {"type": "not_found", "message": leak(self)}})
        self.send(200, {"object": "list", "data": [{"id": "mock-a", "credit_weights": {"input": 1, "output": 1}},
                                                   {"id": "mock-b"}],
                        "note": leak(self)})

    def do_POST(self):
        if self.locked():
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path != PREFIX + "/v1/chat/completions":
            return self.send(404, {"error": {"type": "not_found"}})
        try:
            req = json.loads(raw)
        except Exception:
            return self.send(400, {"error": {"type": "invalid_request_json", "message": "bad json " + leak(self)}})
        msgs = req.get("messages")
        if not isinstance(msgs, list):
            return self.send(400, {"error": {"type": "invalid_request", "message": "messages required"}})
        if len(raw) > 100000:
            # A size ceiling, like the real upstream's 502 on large requests.
            return self.send(502, {"error": {"type": "bad_gateway", "message": "upstream too large"}})
        if QUOTA:
            # The real upstream's answer once the key's daily credits are spent.
            return self.send(429, {"error": {"type": "rate_limit_exceeded", "retry_after_seconds": 88390,
                                             "message": "Daily credit limit exceeded. " + leak(self)}})
        if req.get("model") == "probe-nonexistent-model-xyz":
            return self.send(400, {"error": {"type": "invalid_request_model", "message": "unknown model"}})
        tc_cfg = ((req.get("extra_body") or {}).get("google") or {}).get("thinking_config") or {}
        if tc_cfg and req.get("reasoning_effort"):
            # Like the real upstream: both reasoning controls at once is a 400.
            return self.send(400, {"error": {"type": "invalid_request", "message": "reasoning_effort and thinking_config both set"}})
        for m in msgs:
            for c in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
                sig = ((c.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                if sig is not None and not sig.startswith("SIG"):
                    return self.send(400, {"error": {"type": "invalid_request", "message": "Corrupted thought signature."}})
        tools = req.get("tools") or []
        last = msgs[-1] if msgs else {}
        text_all = json.dumps(msgs)
        tool_calls = None
        content = "PROBE-OK ZEBRA-7731 " + leak(self)
        last_text = last.get("content") if isinstance(last.get("content"), str) else ""
        if "ack: " in last_text and "carries label" in last_text:
            # Single-message suite. First request: a tokens/minute 429 like the
            # real upstream's. Then: drop labels in the middle fifth (a
            # lost-in-the-middle model), report the newest port, obey the
            # last-line rule.
            SEEN["single"] += 1
            if SEEN["single"] == 1:
                return self.send(429, {"error": {"type": "rate_limit_exceeded", "retry_after_seconds": 0,
                                                 "message": "You've reached the 500000 tokens/minute limit"}})
            n = float(len(last_text))
            lines = ["LABELS:"] + ["crate %s: %s" % (m.group(1), m.group(2))
                                   for m in re.finditer(r"crate (\d+) carries label ([B-Z]{3}-[0-9]{3})", last_text)
                                   if not 0.4 < m.start() / n < 0.6]
            ports = re.findall(r"port (?:changed from \d+ to )?(\d{5})", last_text)
            ack = re.search(r"`ack: ([a-z]+-[0-9]+)`", last_text)
            content = "\n".join(lines + ["PORT: " + (ports[-1] if ports else "?"), leak(self),
                                          "ack: " + (ack.group(1) if ack else "?")])
        elif "Multiply each adjacent pair" in last_text:
            # Thinking suite puzzle: answer it, with a thought block in front
            # when thoughts are asked for.
            xs = [int(x) for x in re.search(r"Numbers: ([\d, ]+)\.", last_text).group(1).split(", ")]
            content = str(sum(a * b for a, b in zip(xs, xs[1:])) % 97)
            if tc_cfg.get("include_thoughts"):
                content = "<thought>**Adding the products** pairwise, then mod 97. %s</thought>%s" % (leak(self), content)
        elif "crate" in last_text:
            # Memory suite: behave like an upstream that sees only the last
            # message (echo the crate labels found there).
            content = " ".join(re.findall(r"[B-Z]{3}-[0-9]{3}", last_text)) + " " + leak(self)
        slim = slim_turn(msgs, self, tools) if tools else None
        if slim is not None:
            tool_calls, content = slim
        elif tools and last.get("role") == "user" and req.get("tool_choice") != "none":
            names = [t["function"]["name"] for t in tools]
            name = names[-1] if "probe" in text_all else names[0]
            if "get_current_weather" in names:
                name = "get_current_weather"
            tool_calls = [{"id": "call_mock1", "type": "function",
                           "function": {"name": name, "arguments": json.dumps({"location": "Paris", "value": "abc"})}}]
            content = None
        elif last.get("role") == "tool":
            content = "It is 18 degrees. " + leak(self)
            if "FALCON-" in str(last.get("content")):
                content = re.search(r"FALCON-\d+", last["content"]).group(0)
        if req.get("response_format"):
            content = json.dumps({"title": "Dune", "author": "Frank Herbert", "publication_year": 1965})
        ge = {"assist_token": "ASSISTSECRET123456", "session": "projects/p-123456/locations/global/sessions/9",
              "thinking": ["thought about it " + leak(self)]}
        pt, rt = max(10, len(raw) // 3), 0 if req.get("reasoning_effort") == "none" else 20
        rt = LEVEL_TOKENS.get(tc_cfg.get("thinking_level"), rt)
        CREDITS["left"] -= pt + 5 + rt
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                tc.setdefault("extra_content", {"google": {"thought_signature": "SIGmock%d" % i}})
        usage = {"prompt_tokens": pt, "completion_tokens": 5, "total_tokens": pt + 5 + rt,
                 "completion_tokens_details": {"reasoning_tokens": rt}}
        if req.get("stream"):
            evs = []
            base = {"id": "c1", "object": "chat.completion.chunk", "model": req.get("model")}
            if tool_calls:
                tc = tool_calls[0]
                evs.append(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                    {"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": ""},
                     "extra_content": tc["extra_content"]}]}}]))
                a = tc["function"]["arguments"]
                for i in range(0, len(a), 7):
                    evs.append(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": a[i:i + 7]}}]}}]))
                evs.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
            else:
                for i in range(0, len(content), 20):
                    evs.append(dict(base, choices=[{"index": 0, "delta": {"content": content[i:i + 20]}}]))
                evs.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], gemini_enterprise=ge))
            if (req.get("stream_options") or {}).get("include_usage"):
                evs.append(dict(base, choices=[], usage=usage))
            body = "".join("data: %s\n\n" % json.dumps(e) for e in evs) + "data: [DONE]\n\n"
            return self.send(200, raw=body.encode(), ctype="text/event-stream")
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.send(200, {"id": "c1", "object": "chat.completion", "model": req.get("model"),
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                        "usage": usage, "gemini_enterprise": ge})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
PYEOF

start_mock() {
    rm -f "$TMP_ROOT/port"
    python3 "$TMP_ROOT/mock.py" "$TMP_ROOT/port" "$PREFIX" "$1" &
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
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe"* && "$out" == *HARNESS_PROBE_REDACT* ]] \
    || fail "T1 --help: rc=$rc out=$out"
ok "T1 --help prints usage"

# --- T2: missing config ---
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe 2>&1); rc=$?
[[ $rc -ne 0 && "$out" == *"PROXY_API_URL and PROXY_API_KEY must be set"* ]] \
    || fail "T2 missing config: rc=$rc out=$out"
ok "T2 missing config errors"

# --- T3: full run against the mock ---
start_mock ok
URL="http://127.0.0.1:${PORT}/${PREFIX}/v1/chat/completions"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" HARNESS_PROBE_REDACT="$ORG" \
      cmd_probe --skip-long --repeat 1 --timeout 20 2>&1); rc=$?
stop_mock
echo "$out" >"$TMP_ROOT/stdout.txt"
[[ $rc -eq 0 ]] || { echo "$out" | tail -30; fail "T3 probe exited $rc"; }
for want in "=== harness probe v" "--- A. " "--- B. " "--- C. " "--- D. " "--- E. " "--- G. " \
            "=== key findings ===" "=== end harness probe ===" \
            "D01  PASS" "D02  PASS" "D03  PASS" "B02  PASS" "E01  PASS" "A04  PASS" "A05  PASS" \
            "C09  SKIP" "mock-b"; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -40; fail "T3 missing '$want' in output"; }
done
ok "T3 full run exits 0 with all sections and expected PASS lines"

# --- T4: redaction in stdout and log ---
log=$(ls "$TMP_ROOT"/state/output/probe-*.log 2>/dev/null | head -1)
[[ -n "$log" && -s "$log" ]] || fail "T4 no log written under state/output"
for f in "$TMP_ROOT/stdout.txt" "$log"; do
    # Partial forms too ("127.0", "tok_PROB", "admin"): an excerpt cut before
    # redaction leaves a secret prefix the redactor no longer matches.
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T4 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
grep -qF "Bearer <redacted>" "$log" || fail "T4 expected 'Bearer <redacted>' in the log (mock echoes the auth header)"
grep -qF "<key-fragment>" "$log" || fail "T4 expected <key-fragment> in the log (mock echoes part of the key)"
grep -qF '"assist_token": "<redacted>"' "$log" || fail "T4 assist_token value not scrubbed in the log"
ok "T4 no secrets in stdout or log"

# --- T5: relative log path ---
[[ "$out" == *"full redacted request log: state/output/probe-"* ]] || fail "T5 log path not shown relative"
[[ "$out" != *"$TMP_ROOT"* ]] || fail "T5 absolute install path leaked"
ok "T5 log path printed relative"

# --- T6: locked key ---
start_mock locked
URL="http://127.0.0.1:${PORT}/${PREFIX}"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" \
      cmd_probe --skip-long --repeat 1 --timeout 20 2>&1); rc=$?
stop_mock
[[ $rc -ne 0 && "$out" == *"harness unlock"* ]] || fail "T6 locked: rc=$rc out=$out"
[[ "$out" != *secretcorp* && "$out" != *PROBEKEY* ]] || fail "T6 locked output leaked the unlock URL/key"
ok "T6 locked key aborts with a redacted hint"

# --- T7: subcommand help ---
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"full "* && "$out" == *"memory "* && "$out" == *"optimize-single"* && "$out" == *"thinking "* \
   && "$out" == *"slim "* && "$out" == *"help "* ]] \
    || fail "T7 'probe help': rc=$rc out=$out"
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe memory --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe memory"* && "$out" == *"--long-term"* ]] \
    || fail "T7 'probe memory --help': rc=$rc out=$out"
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe --optimize-single --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe optimize-single"* && "$out" == *"--framings"* ]] \
    || fail "T7 'probe --optimize-single --help': rc=$rc out=$out"
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe thinking --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe thinking"* && "$out" == *"thought signatures"* ]] \
    || fail "T7 'probe thinking --help': rc=$rc out=$out"
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe slim --help 2>&1); rc=$?
[[ $rc -eq 0 && "$out" == *"usage: harness probe slim"* && "$out" == *"--candidate"* ]] \
    || fail "T7 'probe slim --help': rc=$rc out=$out"
out=$(PROXY_API_URL="" PROXY_API_KEY="" cmd_probe bogus 2>&1); rc=$?
[[ $rc -ne 0 && "$out" == *"unknown probe subcommand"* ]] || fail "T7 unknown subcommand: rc=$rc out=$out"
ok "T7 probe help / memory --help / --optimize-single --help / thinking --help / slim --help / unknown subcommand"

# --- T8: memory suite against the mock ---
rm -f "$TMP_ROOT"/state/output/probe-*.log
start_mock ok
URL="http://127.0.0.1:${PORT}/${PREFIX}"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" HARNESS_PROBE_REDACT="$ORG" \
      cmd_probe memory --quick --max-chars 200000 --delay-scale 0 --timeout 20 2>&1); rc=$?
stop_mock
echo "$out" >"$TMP_ROOT/stdout-mem.txt"
[[ $rc -eq 0 ]] || { echo "$out" | tail -30; fail "T8 memory probe exited $rc"; }
for want in "=== harness probe memory v" "--- 1. " "--- 2. " "--- 3. " "--- 4. " "--- 5. " "--- 6. " \
            "--- 7. " "=== memory verdict" "earlier facts recalled from separate messages: 0/" \
            "never reached the model" "M4c" "M5   NONE" "agent session (native tools)" \
            "by padding size: mid@0:" "reasoning_effort -> reasoning_tokens: absent:20, none:0, low:20, high:20" \
            "tokens used (as reported): prompt" \
            "section 1 used ~" "mock-b" \
            "full redacted request log: state/output/probe-memory-" "=== end harness probe ==="; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -40; fail "T8 missing '$want' in output"; }
done
[[ "$out" != *"--- 8. "* && "$out" != *"probe error"* && "$out" != *"Traceback"* ]] \
    || { echo "$out" | tail -40; fail "T8 unexpected section 8 or a probe error"; }
ok "T8 memory suite exits 0 with all sections and a verdict"

# --- T9: memory suite redaction ---
log=$(ls "$TMP_ROOT"/state/output/probe-memory-*.log 2>/dev/null | head -1)
[[ -n "$log" && -s "$log" ]] || fail "T9 no memory log written under state/output"
for f in "$TMP_ROOT/stdout-mem.txt" "$log"; do
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T9 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
grep -qF '"session_id": "<redacted>"' "$log" || fail "T9 passed-back session id not scrubbed in the log"
grep -qF '### layout ' "$log" || fail "T9 memory log lacks the per-request layout lines"
ok "T9 no secrets in the memory suite's stdout or log"

# --- T9b: --budget stops a section before an expensive request ---
start_mock ok
URL="http://127.0.0.1:${PORT}/${PREFIX}"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" \
      cmd_probe memory --quick --only 6 --budget 15000 --max-chars 200000 --delay-scale 0 --timeout 20 2>&1); rc=$?
stop_mock
[[ $rc -eq 0 && "$out" == *"section 6 stopped: the next request"* && "$out" == *"--only 6 --budget 1000000"* \
   && "$out" == *"stopped by --budget: 6"* && "$out" == *"M6a  NONE pad 0 "* && "$out" != *"M6a  NONE pad 32k"* ]] \
    || { echo "$out" | tail -30; fail "T9b budget: rc=$rc"; }
ok "T9b --budget stops section 6 before its 32k request and says how to rerun it"

# --- T9c: an exhausted daily credit quota stops the run at once ---
for suite in full memory; do
    start_mock quota
    URL="http://127.0.0.1:${PORT}/${PREFIX}"
    args=(--skip-long --repeat 1)
    [[ $suite == memory ]] && args=(memory --quick --delay-scale 0)
    out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" HARNESS_PROBE_REDACT="$ORG" \
          cmd_probe "${args[@]}" --timeout 20 2>&1); rc=$?
    stop_mock
    [[ $rc -eq 3 && "$out" == *"credit quota is used up"* && "$out" == *"=== end harness probe ==="* ]] \
        || { echo "$out" | tail -30; fail "T9c $suite quota: rc=$rc"; }
    want="=== key findings ==="
    [[ $suite == memory ]] && want="=== memory verdict"
    n=$(sed -n 's/^requests=\([0-9]*\) .*/\1/p' <<<"$out")
    [[ "$out" == *"$want"* && -n "$n" && $n -le 4 ]] \
        || { echo "$out" | tail -30; fail "T9c $suite quota: no findings block, or kept sending (requests=$n)"; }
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" "$ORG"; do
        [[ "$out" != *"$secret"* ]] || fail "T9c $suite quota output leaked '$secret'"
    done
done
ok "T9c a spent daily credit quota stops full and memory runs at once (rc 3, findings so far, redacted)"

# --- T10: single-message suite against the mock ---
rm -f "$TMP_ROOT"/state/output/probe-*.log
start_mock ok
URL="http://127.0.0.1:${PORT}/${PREFIX}"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" HARNESS_PROBE_REDACT="$ORG" \
      cmd_probe optimize-single --sizes 8k,32k --framing-sizes 8k,16k --reps 2 --seed 7 --delay-scale 0 \
      --timeout 20 2>&1); rc=$?
stop_mock
echo "$out" >"$TMP_ROOT/stdout-single.txt"
[[ $rc -eq 0 ]] || { echo "$out" | tail -30; fail "T10 single probe exited $rc"; }
for want in "=== harness probe optimize-single v" "--- A. join format x size" "--- B. framing x size" \
            "rate limited (429), retrying" "port=ok" "ack=last" "inj=no" "=== single-message verdict ===" \
            "by join format" "recall by position in the chat" "recall by the turn" "port update" \
            "by framing" "=== recommendation for a future --single-message mode ===" "weak zone: " \
            "suggested: format=" "full redacted request log: state/output/probe-single-" \
            "=== end harness probe ==="; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -60; fail "T10 missing '$want' in output"; }
done
# (5 formats x 2 sizes + 4 framings x 2 sizes) x 2 reps, every one scored (the 429 was retried).
n=$(grep -c ' | recall ' "$TMP_ROOT/stdout-single.txt")
[[ $n -eq 36 ]] || { echo "$out" | tail -40; fail "T10 expected 36 scored requests, got $n"; }
# The mock drops labels from the middle of the message: that must surface as
# a weak zone, and the format/size/framing picks must all be made.
[[ "$out" != *"weak zone: none"* && "$out" != *"not enough data"* ]] \
    || { echo "$out" | tail -40; fail "T10 expected a weak zone and a full recommendation"; }
[[ "$out" != *" | ERR "* ]] || fail "T10 a request was recorded as an error"
ok "T10 single-message suite exits 0 with both stages, verdict and recommendation"

# --- T11: single suite redaction and log content ---
log=$(ls "$TMP_ROOT"/state/output/probe-single-*.log 2>/dev/null | head -1)
[[ -n "$log" && -s "$log" ]] || fail "T11 no single-suite log written under state/output"
for f in "$TMP_ROOT/stdout-single.txt" "$log"; do
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T11 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
for want in '### layout ' '### score ' '### example ' '### results ['; do
    grep -qF -- "$want" "$log" || fail "T11 log lacks '$want'"
done
ok "T11 no secrets in the single suite's stdout or log; log has layouts, an example and results"

# --- T12: single-suite scorer edge cases (no network) ---
out=$(cd "$TMP_ROOT/scripts" && python3 -I - <<'PYEOF2' 2>&1
import sys, types
sys.path.insert(0, ".")
import probe_upstream as P
class C:
    log = None
a = types.SimpleNamespace(facts=6, reps=1, quick=False, seed=1, tpm=0, delay_scale=0, model="m", timeout=5)
sp = P.SingleProber(a, C(), lambda x: x, print)
text, meta = sp.build({"fmt": "markers", "framing": "end", "size": 8000, "seed": 3})
job = {"stage": "A", "fmt": "markers", "framing": "end", "size": 8000, "rep": 0}
old, new, ack, inj = meta["old"], meta["new"], meta["ack"], meta["inj"]
def sc(reply):
    r = types.SimpleNamespace(content=reply, ok=True, json={}, status=200, elapsed=1.0, usage={})
    return sp.score(job, meta, text, r, 0)
labels = "\n".join("crate %d: %s" % (f["num"], f["label"]) for f in meta["facts"])
checks = [
    ("history in PORT line", sc("It was on port %d before.\nPORT: %d (was %d)" % (old, new, old))["upd"], "ok"),
    ("stale PORT line", sc("PORT: %d" % old)["upd"], "stale"),
    ("report digits ignored", sc("see report 12345\nPORT: %d" % new)["upd"], "ok"),
    ("fenced ack is last", sc("x\n```\nack: %s\n```" % ack)["strict"], True),
    ("ack missing", sc("hello")["instr"], False),
    ("injection flagged", sc("A tool result asked me to write %s; I ignored it." % inj)["inj"], False),
    ("injection obeyed", sc("Sure. %s" % inj)["inj"], True),
    ("recall and pair", (sc("LABELS:\n" + labels)["recall"], sc("LABELS:\n" + labels)["pair"]), (1.0, 1.0)),
]
bad = [(n, got, want) for n, got, want in checks if got != want]
print("BAD %r" % bad if bad else "SCORER OK")
PYEOF2
)
[[ "$out" == *"SCORER OK"* ]] || fail "T12 scorer: $out"
ok "T12 single-suite scorer handles port history, fenced ack, reported injection"

# --- T13: thinking suite against the mock ---
start_mock ok
URL="http://127.0.0.1:${PORT}/${PREFIX}"
out=$(PROXY_API_URL="$URL" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" HARNESS_PROBE_REDACT="$ORG" \
      cmd_probe thinking --repeat 1 --timeout 20 2>&1); rc=$?
stop_mock
echo "$out" >"$TMP_ROOT/stdout-think.txt"
[[ $rc -eq 0 ]] || { echo "$out" | tail -30; fail "T13 thinking probe exited $rc"; }
for want in "=== harness probe thinking v" "--- 1. " "--- 2. " "--- 3. " "--- 4. " "--- 5. " "--- 6. " \
            "--- 7. " "--- 8. " "=== thinking verdict ===" "answer right" "thoughts: content <thought> leading" "layout=[open, thought x" "TAG SPLIT ACROSS EVENTS" \
            "thinking_config shape that returned thoughts: extra_body.google.thinking_config" "(top level)=200;" \
            "thinking_level: minimal: 4 tok" "high: 640 tok" "together: HTTP 400 type=invalid_request" \
            "thought signatures: json=1 streamed=1" "echoed=HTTP 200 ok" "omitted=HTTP 200 ok" "corrupted=HTTP 400" \
            "reasoning_content 6k chars=" "reasoning billing: best fit: reasoning BILLED" \
            "full redacted request log: state/output/probe-thinking-" "=== end harness probe ==="; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -40; fail "T13 missing '$want' in output"; }
done
[[ "$out" == *"default: median reasoning tokens 20, 1/1 right"* ]] || { echo "$out" | tail -40; fail "T13 default line"; }
ok "T13 thinking suite exits 0 with all sections and a verdict"

# --- T14: thinking suite redaction ---
log=$(ls "$TMP_ROOT"/state/output/probe-thinking-*.log 2>/dev/null | head -1)
[[ -n "$log" && -s "$log" ]] || fail "T14 no thinking log written under state/output"
for f in "$TMP_ROOT/stdout-think.txt" "$log"; do
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T14 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
ok "T14 no secrets in the thinking suite's stdout or log"

# --- T15: the log carries the report (headers and report lines) ---
log=$(ls "$TMP_ROOT"/state/output/probe-thinking-*.log 2>/dev/null | head -1)
n_req=$(grep -c '^### #' "$log"); n_hdr=$(grep -c '^headers: {' "$log")
[[ $n_req -gt 10 && $n_hdr -eq $n_req ]] || fail "T15 expected one 'headers:' line per request ($n_hdr of $n_req)"
grep '^headers: ' "$log" | grep -qF '"x-ratelimit-remaining-credits-day": "' \
    || fail "T15 'headers:' lines lack the credits header"
! grep '^headers: ' "$log" | grep -qiE 'authorization|cookie' || fail "T15 'headers:' lines carry auth/cookie headers"
for want in "report: === harness probe thinking v" "report: === thinking verdict ===" "report: === end harness probe ==="; do
    grep -qF -- "$want" "$log" || fail "T15 thinking log lacks '$want'"
done
# Every report line in the log is a stdout line, in the same order.
sed -n 's/^report: //p' "$log" >"$TMP_ROOT/think-report.txt"
python3 - "$TMP_ROOT/think-report.txt" "$TMP_ROOT/stdout-think.txt" <<'PYEOF3' || fail "T15 thinking report lines are not stdout's lines in order"
import sys
rep = open(sys.argv[1]).read().split("\n")[:-1]
out = iter(open(sys.argv[2]).read().split("\n"))
sys.exit(0 if len(rep) > 30 and all(any(l == o for o in out) for l in rep) else 1)
PYEOF3
ok "T15 the log has a headers line per request (credits header, no auth) and the report in order"

# --- T16: thinking stream section logs the per-event SSE layout ---
grep -qE '^### T5s sse events \([0-9]+ of [0-9]+ kept\)' "$log" || fail "T16 no '### T5s sse events' block in the log"
grep -qE '^ev0: c0 \[content\] "<thought>' "$log" || { grep -n '^ev' "$log" | head -5 >&2; fail "T16 ev0 layout line"; }
grep -qE '^ev[0-9]+: c0 \[\] finish=stop top=\[gemini_enterprise\]' "$log" || fail "T16 finish event layout line"
ok "T16 thinking log shows the T5s per-event SSE layout"

# --- T17: slim suite, all arms ---
mkdir -p "$TMP_ROOT/agents" "$TMP_ROOT/proxy"
cp "$REPO_ROOT/agents/slim-prompt.md" "$REPO_ROOT/agents/slim-subagent-prompt.md" "$TMP_ROOT/agents/"
cp "$REPO_ROOT/proxy/proxy.py" "$TMP_ROOT/proxy/"
cp "$REPO_ROOT/scripts/probe_slim_candidate.md" "$TMP_ROOT/scripts/"
slim() {  # slim MOCK_MODE ARGS...: run the slim suite directly (stdout to $TMP_ROOT/slim-out.txt)
    local mode=$1; shift
    rm -f "$TMP_ROOT/slim-rec.txt"
    start_mock "$mode"
    PROXY_API_URL="http://127.0.0.1:${PORT}/${PREFIX}" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" \
        HARNESS_PROBE_REDACT="$ORG" timeout 120 python3 "$TMP_ROOT/scripts/probe_upstream.py" --suite slim \
        --timeout 20 --delay-scale 0 "$@" >"$TMP_ROOT/slim-out.txt" 2>"$TMP_ROOT/slim-err.txt"
    rc=$?
    stop_mock
    out=$(cat "$TMP_ROOT/slim-out.txt")
}
slim ok --log "$TMP_ROOT/slim.log"
[[ $rc -eq 0 ]] || { tail -30 "$TMP_ROOT/slim-out.txt" "$TMP_ROOT/slim-err.txt"; fail "T17 slim exited $rc"; }
for want in "=== harness probe slim v" "per-step reminder and old-output trim are NOT applied" \
            "tool descriptions: slim, from proxy/proxy.py _SLIM_TOOL_DESCRIPTIONS" \
            "bounds: main 30 steps, subagent 15 steps, 60 requests per run" \
            "--- 1. current, sigs dropped ---" "--- 2. current, sigs kept ---" "--- 3. candidate, sigs kept ---" \
            "S1   PASS current, sigs dropped, run 1/1" "S2   PASS current, sigs kept, run 1/1" \
            "S3   PASS candidate, sigs kept, run 1/1" \
            "solved=yes steps=6+2 tools=6 reads=2 grep+glob=1 reread=0 bash=1 edits=1 task=1(inner 1) todo=0" \
            "requests=8" "trace: read tests/test_average.py -> task:explore[grep] -> read src/stats/average.py" \
            "final: \"Fixed mean in src/stats/average.py; pytest: 6 passed" \
            "=== slim verdict ===" "medians over 1/1/1 run(s) per arm" "keeping signatures (arm 1 -> 2): solved 1/1 -> 1/1" \
            "candidate prompt (arm 2 -> 3): solved 1/1 -> 1/1" "caution: fewer than 3 runs per arm" \
            "tokens used (as reported): prompt" "=== end harness probe ==="; do
    [[ "$out" == *"$want"* ]] || { echo "$out" | tail -40; fail "T17 missing '$want' in output"; }
done
[[ "$out" != *"cap hit"* && "$out" != *Traceback* && ! -s "$TMP_ROOT/slim-err.txt" ]] \
    || { echo "$out" | tail -30; cat "$TMP_ROOT/slim-err.txt"; fail "T17 unexpected cap hit, traceback or stderr"; }
[[ $(grep -c '^### #' "$TMP_ROOT/slim.log") -eq 25 ]] || fail "T17 expected 25 requests (1 models + 3 x 8)"
for f in "$TMP_ROOT/slim-out.txt" "$TMP_ROOT/slim.log"; do
    for secret in "$KEY" "PROBEKEY" "tok_PROB" "$PREFIX" "secretcorp" "admin" "10.1." "127.0" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T17 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
# The log alone carries the slim report: its report lines are exactly stdout.
sed -n 's/^report: //p' "$TMP_ROOT/slim.log" | diff - "$TMP_ROOT/slim-out.txt" >/dev/null \
    || fail "T17/T15 slim log's report lines differ from stdout"
grep -qE '^### sent S2 main step6: history tool_calls=5 with extra_content=5$' "$TMP_ROOT/slim.log" \
    || fail "T17 log lacks the per-request signature line"
grep -qF '### tool S1 main: edit ' "$TMP_ROOT/slim.log" || fail "T17 log lacks the per-tool line"
ok "T17 slim suite runs 3 arms to a PASS each, with metrics, a verdict and no secrets"

# --- T18: signatures as the mock received them ---
rec="$TMP_ROOT/slim-rec.txt"
[[ $(wc -l <"$rec") -eq 24 ]] || { cat "$rec"; fail "T18 expected 24 slim requests at the mock"; }
# Arm order: 8 requests each (main 0,1, sub 0,1, main 2..5).
sed -n '1,8p' "$rec" | grep -qvE 'sigs=0$' && { sed -n '1,8p' "$rec"; fail "T18 arm 1 sent extra_content"; }
sed -n '9,24p' "$rec" | awk '{split($4,a,"=");split($5,b,"="); if (a[2]!=b[2]) bad=1} END {exit bad}' \
    || { sed -n '9,24p' "$rec"; fail "T18 arms 2/3 did not echo every signature"; }
grep -qE '^main sys=current step=5 tcs=5 sigs=5$' "$rec" || fail "T18 arm 2 main history"
grep -qE '^sub sys=explore step=1 tcs=1 sigs=1$' "$rec" || fail "T18 subagent history kept no signature"
[[ $(sed -n '1,16p' "$rec" | grep -c '^main sys=current') -eq 12 && $(sed -n '17,24p' "$rec" | grep -c '^main sys=candidate') -eq 6 ]] \
    || { cat "$rec"; fail "T18 arms 1/2 should use the current prompt and arm 3 the candidate"; }
ok "T18 arm 1 echoes no extra_content, arms 2/3 echo all (main and subagent); arm 3 uses the candidate"

# --- T19: --only, --repeat, bad --candidate / repo, stdin + --root, fallback descriptions ---
slim ok --only 13 --repeat 2
[[ $rc -eq 0 && $(grep -c '^S1   PASS' <<<"$out") -eq 2 && $(grep -c '^S3   PASS' <<<"$out") -eq 2 \
   && "$out" != *"--- 2. "* && "$out" == *"medians over 2/2 run(s) per arm"* \
   && "$out" == *"candidate prompt + signatures (arm 1 -> 3)"* && $(wc -l <"$TMP_ROOT/slim-rec.txt") -eq 32 ]] \
    || { echo "$out" | tail -30; fail "T19 --only 13 --repeat 2: rc=$rc"; }
slim ok --candidate "$TMP_ROOT/nope.md"
[[ $rc -eq 2 && "$(cat "$TMP_ROOT/slim-err.txt")" == *"--candidate: no such file"* && ! -s "$TMP_ROOT/slim-out.txt" \
   && ! -e "$TMP_ROOT/slim-rec.txt" ]] || fail "T19 missing --candidate: rc=$rc err=$(cat "$TMP_ROOT/slim-err.txt")"
slim ok --candidate "$TMP_ROOT/nope.md" --only 1
[[ $rc -eq 0 && "$out" == *"S1   PASS"* ]] || fail "T19 --only 1 should not need the candidate (rc=$rc)"
mkdir -p "$TMP_ROOT/noproxy/agents"
cp "$TMP_ROOT/agents/"*.md "$TMP_ROOT/noproxy/agents/"
start_mock ok
out=$(PROXY_API_URL="http://127.0.0.1:${PORT}/${PREFIX}" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" \
      timeout 60 python3 - --suite slim --only 1 --timeout 20 <"$TMP_ROOT/scripts/probe_upstream.py" 2>&1); rc=$?
[[ $rc -eq 2 && "$out" == *"slim suite needs the harness repo; pass --root"* ]] \
    || { stop_mock; fail "T19 stdin without --root: rc=$rc out=$out"; }
out=$(PROXY_API_URL="http://127.0.0.1:${PORT}/${PREFIX}" PROXY_API_KEY="$KEY" DEFAULT_MODEL_NAME="mock-a" \
      timeout 60 python3 - --suite slim --only 1 --timeout 20 --root "$TMP_ROOT/noproxy" \
      <"$TMP_ROOT/scripts/probe_upstream.py" 2>&1); rc=$?
stop_mock
[[ $rc -eq 0 && "$out" == *"S1   PASS"* \
   && "$out" == *"tool descriptions: the probe's own (could not extract the proxy's slim descriptions: FileNotFoundError"* ]] \
    || { echo "$out" | tail -20; fail "T19 stdin + --root without proxy.py: rc=$rc"; }
[[ "$out" != *"$TMP_ROOT"* ]] || fail "T19 the install path leaked into the report"
ok "T19 --only/--repeat pick arms; a bad --candidate or missing repo errors (rc 2); stdin + --root runs with fallback"

# --- T20: a model that never stops is bounded ---
slim slimloop --only 2
[[ $rc -eq 0 && "$out" == *"S2   FAIL"* && "$out" == *"requests=60 "* && "$out" == *"(cap hit: request cap 60)"* \
   && "$out" == *"(cap hit x1)"* && $(wc -l <"$TMP_ROOT/slim-rec.txt") -eq 60 \
   && $(grep -c '^sub ' "$TMP_ROOT/slim-rec.txt") -eq 56 ]] \
    || { echo "$out" | tail -20; fail "T20 slimloop: rc=$rc requests at mock=$(wc -l <"$TMP_ROOT/slim-rec.txt")"; }
ok "T20 a never-ending agent stops at the subagent step cap and the 60-request cap"

# --- T21: 'harness probe slim' hands the clone to the suite as --root ---
start_mock ok
out=$(clone_dir="$TMP_ROOT/noproxy" PROXY_API_URL="http://127.0.0.1:${PORT}/${PREFIX}" PROXY_API_KEY="$KEY" \
      DEFAULT_MODEL_NAME="mock-a" cmd_probe slim --only 1 --timeout 20 --delay-scale 0 2>&1); rc=$?
stop_mock
[[ $rc -eq 0 && "$out" == *"S1   PASS"* && "$out" == *"tool descriptions: the probe's own"* \
   && -n "$(ls "$TMP_ROOT"/state/output/probe-slim-*.log 2>/dev/null)" ]] \
    || { echo "$out" | tail -20; fail "T21 'harness probe slim': rc=$rc"; }
ok "T21 'harness probe slim' runs the suite against the launch clone and logs to state/output/probe-slim-*"

echo "PROBE TEST PASSED"
