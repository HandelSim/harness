"""harness mock proxy (Phase 1).

A stand-in for the real translating proxy. Its only job is to prove that
ollama's RemoteHost forwarding reaches us and that our response shape is
compatible with ollama's NDJSON line scanner. It logs every request and
returns a single hardcoded api.ChatResponse line.

Phase 2 replaces this file with the real proxy.
"""

import json
import os
import sys
from typing import Any

from flask import Flask, Response, request

PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))

# api.ChatResponse-shaped final chunk. Newline-terminated because ollama's
# remote response parser uses bufio.Scanner with the default line splitter.
MOCK_RESPONSE_LINE: str = json.dumps(
    {
        "model": "harness",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "MOCK PROXY: phase 1 de-risk test passed",
        },
        "done_reason": "stop",
        "done": True,
        "total_duration": 1,
        "load_duration": 1,
        "prompt_eval_count": 1,
        "prompt_eval_duration": 1,
        "eval_count": 1,
        "eval_duration": 1,
    }
) + "\n"

app = Flask(__name__)


def _log_request(path: str) -> None:
    """Pretty-print method, path, and JSON body to stdout."""
    body: Any
    try:
        body = request.get_json(silent=True)
    except Exception:
        body = None
    if body is None:
        # Not JSON — fall back to raw bytes so we still see something.
        raw = request.get_data(as_text=True)
        body_repr = raw if raw else "<empty>"
    else:
        body_repr = json.dumps(body, indent=2, sort_keys=True)
    print(
        f"[mock-proxy] {request.method} /{path}\n{body_repr}",
        flush=True,
    )


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(
        json.dumps({"status": "ok"}),
        status=200,
        mimetype="application/json",
    )


@app.route(
    "/<path:path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
def catch_all(path: str) -> Response:
    _log_request(path)
    return Response(
        MOCK_RESPONSE_LINE,
        status=200,
        mimetype="application/x-ndjson",
    )


if __name__ == "__main__":
    print(
        "============================================================\n"
        " harness MOCK proxy (Phase 1 de-risk only)\n"
        f"   listening on {PROXY_HOST}:{PROXY_PORT}\n"
        "   returns one hardcoded api.ChatResponse line for any request\n"
        "============================================================",
        file=sys.stdout,
        flush=True,
    )
    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)
