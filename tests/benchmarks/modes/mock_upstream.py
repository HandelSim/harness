#!/usr/bin/env python3
"""Scripted stand-in for the upstream API, for `harness benchmark --test-modes --mock`.

Stdlib only. Serves the two endpoints the proxy calls:

  GET  /v1/models            a one-model catalog
  POST /v1/chat/completions  a scripted reply (JSON or SSE)

The script per agent request: find which benchmark task this is (its prompt
appears somewhere in the request), then look at the CURRENT turn only (the last
message, or the <current_turn> block of a single-message fold). No tool result
there yet -> reply with a ```json bash call that runs the task's mock_solution;
a tool result there -> reply with a `finish` call. A request without the
proxy's tool catalog (opencode's title request) gets plain text.

So a mock run drives the real proxy, the real opencode and the real checker
end to end, in both modes, with no network beyond loopback. It proves the
wiring, not the model.

Usage: mock_upstream.py --port N --tasks-dir DIR [--log FILE] [--delay SEC]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "mock-model"


def load_tasks(tasks_dir: str) -> list[tuple[str, str]]:
    out = []
    for name in sorted(os.listdir(tasks_dir)):
        p = os.path.join(tasks_dir, name, "task.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                t = json.load(f)
            out.append((t["prompt"], t["mock_solution"]))
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def current_turn(messages: list[dict]) -> str:
    last = _text(messages[-1].get("content")) if messages else ""
    i = last.rfind("<current_turn>")
    return last[i:] if i >= 0 else last


_CATALOG_WITH_BASH = re.compile(r"<<<BEGIN_AGENT_TOOLS>>>(?:(?!<<<END_AGENT_TOOLS>>>).)*?\bbash\b", re.DOTALL)


def tool_call(name: str, arguments: dict) -> str:
    return "```json\n" + json.dumps({"name": name, "arguments": arguments}) + "\n```"


def reply_for(messages: list[dict], tasks: list[tuple[str, str]]) -> str:
    whole = "\n".join(_text(m.get("content")) for m in messages)
    if not _CATALOG_WITH_BASH.search(whole):  # opencode's title request has no tools
        return "Benchmark task"
    if "<<<BEGIN_TOOL_RESULT" in current_turn(messages):
        return tool_call("finish", {"summary": "Done."})
    for prompt, solution in tasks:
        # A prefix, not the whole prompt: quotes later in a prompt arrive escaped.
        if prompt[:60] in whole:
            return tool_call("bash", {"command": solution, "description": "Apply the change"})
    return tool_call("finish", {"summary": "No matching task."})


class Handler(BaseHTTPRequestHandler):
    tasks: list[tuple[str, str]] = []
    log_lock = threading.Lock()
    log_path = ""
    delay = 0.0

    def log_message(self, fmt, *args):  # quiet; we keep our own log
        pass

    def _log(self, line: str) -> None:
        if not self.log_path:
            return
        with self.log_lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "mock"}]}
            self._send(200, json.dumps(body).encode(), "application/json")
        else:
            self._send(200, b'{"ok": true}', "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, b'{"error": {"type": "invalid_request"}}', "application/json")
            return
        messages = req.get("messages") or []
        content = reply_for(messages, self.tasks)
        if self.delay:
            time.sleep(self.delay)
        self._log(f"POST messages={len(messages)} -> {content[:60]!r}")
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        if req.get("stream"):
            chunk = {"id": cid, "object": "chat.completion.chunk", "model": MODEL_ID,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                                  "finish_reason": None}]}
            end = {"id": cid, "object": "chat.completion.chunk", "model": MODEL_ID,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            body = (f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n").encode()
            self._send(200, body, "text/event-stream")
            return
        body = {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self._send(200, json.dumps(body).encode(), "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to stall each chat reply (timeout tests)")
    a = ap.parse_args()
    Handler.tasks = load_tasks(a.tasks_dir)
    Handler.log_path = a.log
    Handler.delay = a.delay
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"mock upstream on 127.0.0.1:{a.port} ({len(Handler.tasks)} tasks)", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
