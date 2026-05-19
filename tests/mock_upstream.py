"""Mock upstream API for proxy / agent / pipeline integration tests.

Listens on port 9000 (override with MOCK_PORT) and returns a canned response
shaped like an OpenAI chat completion.

Two response-selection paths exist:

1. **Fixture dispatch** (preferred). When MOCK_FIXTURES_DIR is set, on every
   request the server reads the most recent user-message content from the
   forwarded body (with the proxy's cooperative-prompt scaffolding stripped —
   see `extract_user_prompt`), matches it (case-insensitive, multiline)
   against each fixture's compiled `match` regex in lexicographic filename
   order, and returns the first hit's `response`. Fixtures are loaded at startup
   from `*.json` files under MOCK_FIXTURES_DIR; each file has the shape:

       {
         "name": "human-readable label",
         "match": "^regex against user prompt$",
         "status": 200,                     # optional, default 200
         "response": { ... full OpenAI chat completion body ... }
       }

   For multi-call fixtures, set `match_counter: true` and provide a
   `responses` array; the mock tracks per-fixture call count and returns
   responses[count % len] on each match. POST /__reset_counters__ resets
   the counters for test isolation. Per-response `status` overrides the
   fixture-level default.

   A fixture whose `match` is the empty string (or missing) is treated as
   the catch-all and should be named so it sorts last (e.g. `99_default.json`).

2. **Legacy MOCK_SCENARIO env** (fallback). If no fixture matches OR
   MOCK_FIXTURES_DIR is unset, the server falls back to the env-selected
   response from the legacy SCENARIO_RESPONSES table:

       text  -> simple assistant text + usage stats
       tool  -> assistant text with an embedded ```json``` tool-call block

Every request is logged to stdout (method, path, parsed JSON body) so
integration tests can scrape the logs to verify what the proxy forwarded.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from flask import Flask, Response, request


SCENARIO = os.environ.get("MOCK_SCENARIO", "text").strip().lower()
PORT = int(os.environ.get("MOCK_PORT", "9000"))
FIXTURES_DIR = os.environ.get("MOCK_FIXTURES_DIR", "").strip()


# Legacy scenarios — preserved verbatim for tests that pre-date the fixture
# system. proxy_test and firewall_test run with these as the fallback when
# no fixture matches.
SCENARIO_RESPONSES: dict[str, dict[str, Any]] = {
    "text": {
        "choices": [
            {"message": {"role": "assistant", "content": "Hello from mock upstream"}}
        ],
        "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
    },
    "tool": {
        "choices": [
            {"message": {
                "role": "assistant",
                "content": (
                    "Let me use a tool.\n\n"
                    "```json\n"
                    "{\"name\":\"get_weather\",\"arguments\":{\"city\":\"Atlanta\"}}\n"
                    "```\n"
                ),
            }}
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
    },
}


class Fixture:
    """One fixture file loaded into memory.

    `pattern` is None for catch-alls (empty/missing match field); those
    always match and should sort last in the filename ordering.

    Two response shapes are supported:
      - single `response` (and optional top-level `status`) — same response
        every time the fixture matches.
      - `responses` array + `match_counter: true` — the mock tracks per-
        fixture call count and returns responses[count % len], advancing
        the counter on each match.
    """

    __slots__ = ("name", "filename", "pattern", "response", "responses",
                 "match_counter", "status")

    def __init__(self, filename: str, raw: dict[str, Any]) -> None:
        self.filename = filename
        self.name = raw.get("name", filename)
        match_re = raw.get("match", "")
        if match_re:
            self.pattern: re.Pattern[str] | None = re.compile(
                match_re, re.IGNORECASE | re.MULTILINE
            )
        else:
            self.pattern = None
        self.match_counter = bool(raw.get("match_counter"))
        self.responses = raw.get("responses") if self.match_counter else None
        self.response = raw.get("response", {})
        self.status = int(raw.get("status", 200))

    def matches(self, prompt: str) -> bool:
        if self.pattern is None:
            return True
        return self.pattern.search(prompt) is not None


# Per-fixture call counters for `match_counter` mode. Keyed by fixture
# filename so multiple counter-mode fixtures don't share state.
_fixture_counters: dict[str, int] = {}


def select_fixture_response(fx: "Fixture") -> tuple[dict[str, Any], int]:
    """Pick the right (body, status) for this fixture invocation.

    Counter-mode fixtures cycle through their `responses` array on each
    matching call. Per-response `status` overrides the fixture-level
    default; otherwise we fall back to fixture.status (default 200).
    """
    if fx.match_counter and fx.responses:
        count = _fixture_counters.get(fx.filename, 0)
        _fixture_counters[fx.filename] = count + 1
        body = fx.responses[count % len(fx.responses)]
        if isinstance(body, dict) and "status" in body:
            return body, int(body["status"])
        return body, fx.status
    return fx.response, fx.status


def load_fixtures(fixtures_dir: str) -> list[Fixture]:
    """Read every *.json file in fixtures_dir into a list, sorted by name.

    Skips malformed files with a warning rather than crashing — a single bad
    fixture should not take the whole mock down during a test run.
    """
    out: list[Fixture] = []
    p = Path(fixtures_dir)
    if not p.is_dir():
        print(
            f"[mock-upstream] WARN: MOCK_FIXTURES_DIR={fixtures_dir} is not a directory",
            file=sys.stderr,
            flush=True,
        )
        return out
    for path in sorted(p.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out.append(Fixture(path.name, raw))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(
                f"[mock-upstream] WARN: skipping fixture {path.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return out


FIXTURES: list[Fixture] = load_fixtures(FIXTURES_DIR) if FIXTURES_DIR else []


# Marker pairs the proxy's cooperative-prompt builders wrap real content in
# (see proxy/proxy.py). The user's actual request lands inside
# <<<BEGIN_USER_REQUEST>>> markers; a tool-result turn's output lands inside
# <<<BEGIN_TOOL_RESULT name="...">>> markers. Everything else in the message
# is proxy scaffolding — tool-schema dumps and instruction boilerplate.
_USER_REQUEST_OPEN = "<<<BEGIN_USER_REQUEST>>>"
_USER_REQUEST_CLOSE = "<<<END_USER_REQUEST>>>"
_TOOL_RESULT_OPEN = "<<<BEGIN_TOOL_RESULT"
_TOOL_RESULT_CLOSE = "<<<END_TOOL_RESULT>>>"


def unwrap_proxy_scaffolding(content: str) -> str:
    """Return just the user/tool content from a proxy-wrapped message.

    In the default `user_front` prompt mode the proxy appends the full
    tool-schema block (~10-15KB across ~24 tools for Claude Code) to the
    final user message. Matching a fixture regex against that block is a
    bug magnet: a phrase like "List files in current directory" inside the
    Bash tool's description matches the `03_list_files` fixture's
    `\\blist files\\b` regex, so first-match-wins dispatches *every* request
    to that fixture — an endless `ls -la` tool-call loop that never reaches
    the `99_default` catch-all.

    Extract the content delimited by the proxy's explicit markers instead,
    so matching targets the user's actual request (or a tool result), never
    the injected scaffolding. Fall back to the whole string when no markers
    are present (e.g. `hybrid` prompt mode leaves user turns unwrapped
    with only a short reminder prefix, or a request that never went
    through the proxy).
    """
    open_idx = content.find(_USER_REQUEST_OPEN)
    if open_idx != -1:
        body_start = open_idx + len(_USER_REQUEST_OPEN)
        # First close marker only — defensive against any future
        # bookend-style scheme that emits two request blocks with the
        # tool-schema dump *between* them. Spanning to the last close
        # marker would re-include the scaffolding.
        close_idx = content.find(_USER_REQUEST_CLOSE, body_start)
        if close_idx != -1:
            return content[body_start:close_idx].strip()
    open_idx = content.find(_TOOL_RESULT_OPEN)
    if open_idx != -1:
        # The open marker carries a name="..." attribute; content starts
        # after the '>>>' that closes the marker tag.
        tag_end = content.find(">>>", open_idx)
        if tag_end != -1:
            body_start = tag_end + len(">>>")
            close_idx = content.find(_TOOL_RESULT_CLOSE, body_start)
            if close_idx != -1:
                return content[body_start:close_idx].strip()
    return content


def extract_user_prompt(body: Any) -> str:
    """Pull the most recent user-message content out of a forwarded body.

    Body shape is OpenAI chat-completions: `{"messages": [{"role", "content"}, ...]}`.
    The proxy's cooperative-prompt wrapper pads the final user message with a
    tool-schema dump and instruction boilerplate; `unwrap_proxy_scaffolding`
    strips that so fixtures match against the user's actual request (or a
    tool result), not the injected scaffolding.
    """
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    # Walk in reverse — fixture matching against the *latest* user message
    # is intuitive and avoids matching a long-ago turn.
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return unwrap_proxy_scaffolding(content)
        # Some clients send content as a list of {"type":"text","text":...}
        # parts. Concatenate the text parts so fixtures can match either.
        if isinstance(content, list):
            joined = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            return unwrap_proxy_scaffolding(joined)
    return ""


def select_response(body: Any) -> tuple[dict[str, Any], int, str]:
    """Return (response_dict, status_code, label) for a forwarded body.

    label is human-readable (the fixture name or 'scenario:<x>') so the
    request log makes the dispatch path obvious during debugging.
    """
    prompt = extract_user_prompt(body)
    for fx in FIXTURES:
        if fx.matches(prompt):
            resp, status = select_fixture_response(fx)
            return resp, status, f"fixture:{fx.filename}"
    if SCENARIO in SCENARIO_RESPONSES:
        return SCENARIO_RESPONSES[SCENARIO], 200, f"scenario:{SCENARIO}"
    # Last-ditch: emit a stub error response (still 200 so tests don't fail
    # at HTTP level — they fail at content level with a clearer message).
    return (
        {
            "choices": [
                {"message": {
                    "role": "assistant",
                    "content": f"[mock-upstream] no fixture or scenario matched; SCENARIO={SCENARIO!r}",
                }}
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        200,
        "fallback:none",
    )


app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json")


@app.route("/__reset_counters__", methods=["POST"])
def reset_counters() -> Response:
    """Test-only endpoint that resets per-fixture call counters so a test
    re-running the same counter-mode fixture starts at index 0."""
    global _fixture_counters
    _fixture_counters = {}
    return Response(
        json.dumps({"status": "ok", "reset": True}),
        status=200,
        mimetype="application/json",
    )


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(path: str) -> Response:
    body = request.get_json(silent=True)
    print(
        f"[mock-upstream] {request.method} /{path} body={json.dumps(body) if body is not None else '<none>'}",
        flush=True,
    )

    response_body, status_code, label = select_response(body)
    print(f"[mock-upstream] dispatch -> {label} (status={status_code})", flush=True)

    return Response(
        json.dumps(response_body),
        status=status_code,
        mimetype="application/json",
    )


if __name__ == "__main__":
    print(
        f"[mock-upstream] starting on 0.0.0.0:{PORT} "
        f"scenario={SCENARIO} fixtures_dir={FIXTURES_DIR or '(unset)'} "
        f"loaded_fixtures={len(FIXTURES)}",
        file=sys.stdout,
        flush=True,
    )
    for fx in FIXTURES:
        print(
            f"[mock-upstream]   fixture: {fx.filename} "
            f"name={fx.name!r} match={'<catch-all>' if fx.pattern is None else fx.pattern.pattern!r}",
            flush=True,
        )
    app.run(host="0.0.0.0", port=PORT, debug=False)
