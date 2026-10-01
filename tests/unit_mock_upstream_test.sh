#!/usr/bin/env bash
#
# tests/unit_mock_upstream_test.sh — the mock upstream's fixture dispatch
# ignores the proxy's own correction turns, docker-free.
#
# Require-tool mode answers a text-only reply with an appended user message
# (`_REQUIRE_TOOL_CORRECTION`) and re-POSTs. That message's example bash call
# says "list files", which matched 03_list_files, so `harness -p "say hello"`
# in integration_test.sh got an `ls` tool call on every round and never
# finished. The mock now skips user turns starting with "[harness" and matches
# the turn being corrected instead.
#
#   T1  a correction after "say hello" (raw, wrapped in the user-request
#       markers, and with the escalation suffix) dispatches 01_simple_text
#   T2  a correction after a tool result matches the tool result, not 03
#   T3  every user message the proxy authors starts with the prefix the mock
#       skips (drift guard, constants read from proxy.py's source)
#
# Run:  bash tests/unit_mock_upstream_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo " mock upstream: correction turns skipped by fixture dispatch"
echo "============================================================"

command -v python3 >/dev/null 2>&1 || { echo "[mock-upstream] FAIL: python3 not found" >&2; exit 1; }

REPO_ROOT="$REPO_ROOT" python3 - <<'EOF'
import ast, os, sys, types

root = os.environ["REPO_ROOT"]
os.environ["MOCK_FIXTURES_DIR"] = os.path.join(root, "tests/fixtures/responses")
try:
    import flask  # noqa: F401
except ImportError:
    # The unit job has no flask; the mock only needs it to build its app.
    class _App:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
    sys.modules["flask"] = types.SimpleNamespace(
        Flask=_App, Response=object, request=None)
sys.path.insert(0, os.path.join(root, "tests"))
import mock_upstream as m
# Literal fallback so the old mock fails on behavior, not a missing name.
PREFIX = getattr(m, "PROXY_CORRECTION_PREFIX", "[harness")

# Proxy-authored strings, read from source so the test needs no proxy deps.
consts = {}
src = open(os.path.join(root, "proxy/proxy.py"), encoding="utf-8").read()
for node in ast.parse(src).body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if isinstance(target, ast.Name) and target.id in (
            "_REQUIRE_TOOL_CORRECTION", "_REQUIRE_TOOL_ESCALATION",
            "_REQUIRE_TOOL_FINISH_TODOS_CORRECTION", "_RETRY_CORRECTION_MESSAGES",
        ):
            consts[target.id] = ast.literal_eval(node.value)

failed = 0
def check(name, cond, detail=""):
    global failed
    print(f"[mock-upstream] {'OK' if cond else 'FAIL'}: {name}" + (f" ({detail})" if not cond else ""))
    failed += 0 if cond else 1

def label(messages):
    return m.select_response({"messages": messages})[2]

corr = consts["_REQUIRE_TOOL_CORRECTION"]
esc = consts["_REQUIRE_TOOL_ESCALATION"].format(attempt=2, budget=3)
hello = [{"role": "user", "content": "say hello"},
         {"role": "assistant", "content": "Hello from mock upstream"}]

# T1
# Premise: the correction's text itself matches 03 when not skipped.
check("T1 correction text alone matches 03_list_files (premise)",
      label([{"role": "user", "content": corr[len(PREFIX):]}])
      == "fixture:03_list_files.json")
check("T1 raw correction -> 01_simple_text",
      label(hello + [{"role": "user", "content": corr}]) == "fixture:01_simple_text.json")
wrapped = "<<<BEGIN_USER_REQUEST>>>\n" + corr + esc + "\n<<<END_USER_REQUEST>>>\nReminder"
check("T1 wrapped + escalated correction -> 01_simple_text",
      label(hello + [{"role": "assistant", "content": "hi"}, {"role": "user", "content": corr},
                     {"role": "assistant", "content": "hi"}, {"role": "user", "content": wrapped}])
      == "fixture:01_simple_text.json")
check("T1 list-of-parts correction -> 01_simple_text",
      label(hello + [{"role": "user", "content": [{"type": "text", "text": corr}]}])
      == "fixture:01_simple_text.json")

# T2
tool_result = "<<<BEGIN_TOOL_RESULT name=bash>>>\nREADME.md\n<<<END_TOOL_RESULT>>>"
got = label([{"role": "user", "content": "say hello"},
             {"role": "user", "content": tool_result},
             {"role": "assistant", "content": "done"},
             {"role": "user", "content": corr}])
check("T2 correction after a tool result -> 99_default", got == "fixture:99_default.json", got)

# T3
authored = {"_REQUIRE_TOOL_CORRECTION": corr,
            "_REQUIRE_TOOL_FINISH_TODOS_CORRECTION": consts["_REQUIRE_TOOL_FINISH_TODOS_CORRECTION"]}
authored.update({f"_RETRY_CORRECTION_MESSAGES[{k}]": v
                 for k, v in consts["_RETRY_CORRECTION_MESSAGES"].items()})
for name, text in authored.items():
    check(f"T3 {name} starts with {PREFIX!r}",
          text.startswith(PREFIX), text[:40])

sys.exit(1 if failed else 0)
EOF

echo "[mock-upstream] all checks passed"
