"""Mock upstream LLM API for benchmark wiring tests.

Speaks the OpenAI chat-completions shape that proxy/proxy.py expects:
POST any path; body has {"model": ..., "messages": [...]}; returns
{"choices": [{"message": {"role":"assistant","content": "<text>"}}],
 "usage": {"prompt_tokens": N, "completion_tokens": N}}.

The proxy's content-parser extracts ```json {name, arguments} ``` blocks
as tool calls; everything else is treated as the assistant's plain
reply. The mock can return either.

Behavior knobs (env vars):
    MOCK_DEFAULT_CONTENT
        Plain text returned when no script match. Default: a short
        "task complete" string that ends the agent loop.
    MOCK_SCRIPT_FILE
        Optional path to a JSON file shaped like:
            [
              {"match": "<regex against the last user message>",
               "content": "<string body, possibly with ```json fences>"},
              ...
            ]
        First matching rule wins. Falls through to MOCK_DEFAULT_CONTENT.
    MOCK_LOG_REQUESTS=1
        Print each incoming request payload to stdout.

Purely a wiring test: no real LLM behavior, no rate limits.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from flask import Flask, Response, request

app = Flask(__name__)

DEFAULT_CONTENT = os.environ.get(
    "MOCK_DEFAULT_CONTENT",
    "Task acknowledged. No further actions needed. Done.",
)
SCRIPT_FILE = os.environ.get("MOCK_SCRIPT_FILE", "").strip()
LOG_REQUESTS = os.environ.get("MOCK_LOG_REQUESTS", "0") == "1"

_script: list[dict[str, Any]] = []
if SCRIPT_FILE and os.path.exists(SCRIPT_FILE):
    with open(SCRIPT_FILE) as f:
        _script = json.load(f)
    print(f"[mock-api] loaded {len(_script)} script rules from {SCRIPT_FILE}",
          file=sys.stderr, flush=True)


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _pick_content(messages: list[dict[str, Any]]) -> str:
    haystack = _last_user_message(messages)
    for rule in _script:
        pat = rule.get("match", "")
        if pat and re.search(pat, haystack, re.IGNORECASE | re.DOTALL):
            return str(rule.get("content", DEFAULT_CONTENT))
    return DEFAULT_CONTENT


def _approx_tokens(s: str) -> int:
    # The proxy estimates locally anyway; a rough char/4 keeps the shape sane.
    return max(1, len(s) // 4)


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(json.dumps({"status": "ok"}),
                    status=200, mimetype="application/json")


@app.route("/", defaults={"path": ""}, methods=["POST"])
@app.route("/<path:path>", methods=["POST"])
def chat(path: str) -> Response:
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    model = body.get("model") or "mock-model"
    if LOG_REQUESTS:
        print(f"[mock-api] POST /{path} model={model} "
              f"msgs={len(messages)}", file=sys.stderr, flush=True)
    content = _pick_content(messages)
    prompt_text = "\n".join(str(m.get("content") or "") for m in messages)
    resp = {
        "id": f"mock-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": _approx_tokens(prompt_text),
            "completion_tokens": _approx_tokens(content),
            "total_tokens": _approx_tokens(prompt_text) + _approx_tokens(content),
        },
    }
    return Response(json.dumps(resp), status=200, mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "80"))
    host = os.environ.get("MOCK_HOST", "0.0.0.0")
    print(f"[mock-api] listening on {host}:{port} "
          f"(script_rules={len(_script)}, "
          f"default={DEFAULT_CONTENT[:60]!r})",
          file=sys.stderr, flush=True)
    app.run(host=host, port=port, threaded=True)
