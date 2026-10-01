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
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$TMP_ROOT/scripts"
cp "$REPO_ROOT/scripts/probe_upstream.py" "$TMP_ROOT/scripts/"

KEY="sk-test-PROBEKEY-9f8e7d6c5b4a3210"
PREFIX="zqsecretprefix"
ORG="AcmeInternalOrg"

cat >"$TMP_ROOT/mock.py" <<'PYEOF'
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/" + sys.argv[2]
LOCKED = sys.argv[3] == "locked"
PORT_FILE = sys.argv[1]


def leak(h):
    # Everything the probe must never print.
    return ("auth=%s frag=tok_PROBEKEY-9f8e7d host=%s mail=admin@secretcorp.example ip=10.1.2.3 "
            "see https://unlock.secretcorp.example/u?k=1 org AcmeInternalOrg" % (h.headers.get("Authorization"), h.headers.get("Host")))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
        self.send(200, {"object": "list", "data": [{"id": "mock-a"}, {"id": "mock-b"}],
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
        if req.get("model") == "probe-nonexistent-model-xyz":
            return self.send(400, {"error": {"type": "invalid_request_model", "message": "unknown model"}})
        tools = req.get("tools") or []
        last = msgs[-1] if msgs else {}
        text_all = json.dumps(msgs)
        tool_calls = None
        content = "PROBE-OK ZEBRA-7731 " + leak(self)
        if tools and last.get("role") == "user" and req.get("tool_choice") != "none":
            names = [t["function"]["name"] for t in tools]
            name = names[-1] if "probe" in text_all else names[0]
            if "get_current_weather" in names:
                name = "get_current_weather"
            tool_calls = [{"id": "call_mock1", "type": "function",
                           "function": {"name": name, "arguments": json.dumps({"location": "Paris", "value": "abc"})}}]
            content = None
        elif last.get("role") == "tool":
            content = "It is 18 degrees. " + leak(self)
        if req.get("response_format"):
            content = json.dumps({"title": "Dune", "author": "Frank Herbert", "publication_year": 1965})
        ge = {"assist_token": "ASSISTSECRET123456", "session": "projects/p-123456/locations/global/sessions/9",
              "thinking": ["thought about it " + leak(self)]}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if req.get("stream"):
            evs = []
            base = {"id": "c1", "object": "chat.completion.chunk", "model": req.get("model")}
            if tool_calls:
                tc = tool_calls[0]
                evs.append(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                    {"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": ""}}]}}]))
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
    local i
    for i in $(seq 1 50); do
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
    for secret in "$KEY" "PROBEKEY" "$PREFIX" "secretcorp" "10.1.2.3" "127.0.0.1" ":${PORT}" \
                  "p-123456" "ASSISTSECRET" "$ORG" "unlock.secret"; do
        if grep -qiF -- "$secret" "$f"; then
            grep -niF -- "$secret" "$f" | head -3 >&2
            fail "T4 secret '$secret' leaked into $(basename "$f")"
        fi
    done
done
grep -qF "<key>" "$log" || fail "T4 expected <key> placeholder in the log (mock echoes the key)"
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

echo "PROBE TEST PASSED"
