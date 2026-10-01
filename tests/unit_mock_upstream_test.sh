#!/usr/bin/env bash
#
# tests/unit_mock_upstream_test.sh — the mock upstream's fixture dispatch
# terminates the proxy's require-tool correction loop, docker-free.
#
# Require-tool mode answers a text-only reply with an appended user message
# (`_REQUIRE_TOOL_CORRECTION`) and re-POSTs, up to a budget, then fails open
# and forwards the text. The correction's example bash call says "list files",
# which matched 03_list_files, so `harness -p "say hello"` in
# integration_test.sh got an `ls` tool call on every round and never finished.
# The mock now skips user turns starting with "[harness" (and unwraps the
# <<<BEGIN_USER_MESSAGE>>> markers earlier turns carry, so an older
# correction is recognised too) and matches the turn being corrected.
#
# Drives the proxy's real correction loop (`_serve_require_tool_correction`,
# real history translation and prompt scaffolding) in-process with the
# upstream call routed to the mock's `select_response`. flask/requests are
# stubbed when absent, since only pure functions are exercised.
#
#   T1  "say hello": every round dispatches 01_simple_text and the loop
#       exhausts its budget (fail-open), as in production
#   T2  premise: the correction text, if matched, selects 03_list_files
#   T3  a correction after a tool result matches the tool result (99_default)
#   T4  every user message the proxy authors starts with the skipped prefix
#   T5  unwrap strips <<<BEGIN_USER_MESSAGE>>> history markers
#
# Run:  bash tests/unit_mock_upstream_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo " mock upstream: require-tool correction loop terminates"
echo "============================================================"

command -v python3 >/dev/null 2>&1 || { echo "[mock-upstream] FAIL: python3 not found" >&2; exit 1; }

REPO_ROOT="$REPO_ROOT" python3 - <<'EOF'
import contextlib, io, os, sys, types

root = os.environ["REPO_ROOT"]
os.environ["MOCK_FIXTURES_DIR"] = os.path.join(root, "tests/fixtures/responses")

# The unit job has no pip installs; neither module's web layer is used here.
class _App:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return lambda *a, **k: (lambda f: f)
try:
    import flask  # noqa: F401
except ImportError:
    sys.modules["flask"] = types.SimpleNamespace(Flask=_App, Response=object, request=None)
try:
    import requests  # noqa: F401
except ImportError:
    class _RequestException(Exception): pass
    _u3 = types.SimpleNamespace(disable_warnings=lambda *a, **k: None,
                                exceptions=types.SimpleNamespace(InsecureRequestWarning=Warning))
    sys.modules["requests"] = types.SimpleNamespace(
        RequestException=_RequestException, post=None,
        packages=types.SimpleNamespace(urllib3=_u3))

sys.path[:0] = [os.path.join(root, "tests"), os.path.join(root, "proxy")]
import mock_upstream as m
import proxy

# Literal fallback so an old mock fails on behavior, not a missing name.
PREFIX = getattr(m, "PROXY_CORRECTION_PREFIX", "[harness")

failed = 0
def check(name, cond, detail=""):
    detail = str(detail)[:160]
    global failed
    print(f"[mock-upstream] {'OK' if cond else 'FAIL'}: {name}" + ("" if cond else f" ({detail})"))
    failed += 0 if cond else 1

class _Resp:
    def __init__(self, body, status):
        self.status_code, self._body, self.text = status, body, ""
    def json(self):
        return self._body

labels = []
def _post(headers, payload):
    body, status, label = m.select_response(payload)
    labels.append(label)
    return _Resp(body, status)
proxy._upstream_post = _post
proxy.save_debug_file = lambda *a, **k: None

TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "Executes a bash command.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]

def turn(messages):
    """One agent turn as catch_all runs it: first upstream call, then the
    require-tool correction loop when the reply carries no tool call."""
    labels.clear()
    with contextlib.redirect_stdout(io.StringIO()):  # proxy + mock request logs
        return _turn(messages)

def _turn(messages):
    prompt_tools = proxy._augment_tools_with_finish(TOOLS)
    tools_text = proxy.format_tools_to_text(prompt_tools)
    first = _post({}, {"messages": proxy.translate_history_and_apply_prompt(
        messages, tools_text, tools=prompt_tools)})
    served = proxy._serve_require_tool_correction(
        "unit", messages, prompt_tools, tools_text, "harness", {},
        proxy.extract_assistant_content(first.json()),
        proxy._REQUIRE_TOOL_CORRECTION, proxy._REQUIRE_TOOL_SERVE_BUDGET)
    return list(labels), served

budget = proxy._REQUIRE_TOOL_SERVE_BUDGET

# T1
got, served = turn([{"role": "user", "content": "say hello"}])
check("T1 say hello: every round -> 01_simple_text",
      got == ["fixture:01_simple_text.json"] * (1 + budget), got)
check("T1 say hello: budget exhausted, text forwarded (fail-open)", served is None, served)

# T2
corr = proxy._REQUIRE_TOOL_CORRECTION
check("T2 correction text, if matched, selects 03_list_files (premise)",
      m.select_response({"messages": [{"role": "user", "content": corr[len(PREFIX):]}]})[2]
      == "fixture:03_list_files.json")

# T3
got, served = turn([
    {"role": "user", "content": "list files"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\": \"ls -la\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "README.md"},
])
check("T3 after a tool result: every round -> 99_default",
      got == ["fixture:99_default.json"] * (1 + budget), got)
check("T3 after a tool result: budget exhausted (fail-open)", served is None, served)

# T4
authored = {"_REQUIRE_TOOL_CORRECTION": corr,
            "_REQUIRE_TOOL_FINISH_TODOS_CORRECTION": proxy._REQUIRE_TOOL_FINISH_TODOS_CORRECTION}
authored.update({f"_RETRY_CORRECTION_MESSAGES[{k}]": v
                 for k, v in proxy._RETRY_CORRECTION_MESSAGES.items()})
for name, text in authored.items():
    check(f"T4 {name} starts with {PREFIX!r}", text.startswith(PREFIX), text[:40])

# T5
check("T5 unwrap strips USER_MESSAGE markers",
      m.unwrap_proxy_scaffolding("<<<BEGIN_USER_MESSAGE>>>\nsay hi\n<<<END_USER_MESSAGE>>>") == "say hi")

sys.exit(1 if failed else 0)
EOF

echo "[mock-upstream] all checks passed"
