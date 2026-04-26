"""Mock upstream API for proxy / agent / pipeline integration tests.

Listens on port 9000 (override with MOCK_PORT) and returns a canned response
shaped like an OpenAI chat completion.

Two response-selection paths exist:

1. **Fixture dispatch** (preferred). When MOCK_FIXTURES_DIR is set, on every
   request the server reads the most recent user-message content from the
   forwarded body, matches it (case-insensitive, multiline) against each
   fixture's compiled `match` regex in lexicographic filename order, and
   returns the first hit's `response`. Fixtures are loaded once at startup
   from `*.json` files under MOCK_FIXTURES_DIR; each file has the shape:

       {
         "name": "human-readable label",
         "match": "^regex against user prompt$",
         "response": { ... full OpenAI chat completion body ... }
       }

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
# system. derisk_test/proxy_test/agent_test all run with these as the
# fallback when no fixture matches.
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
    """

    __slots__ = ("name", "filename", "pattern", "response")

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
        self.response = raw["response"]

    def matches(self, prompt: str) -> bool:
        if self.pattern is None:
            return True
        return self.pattern.search(prompt) is not None


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


def extract_user_prompt(body: Any) -> str:
    """Pull the most recent user-message content out of a forwarded body.

    Body shape is OpenAI chat-completions: `{"messages": [{"role", "content"}, ...]}`.
    The proxy's cooperative-prompt wrapper appends a tool-usage preamble to
    the final user message; we match against the entire content, so fixtures
    can target either the user's text or the wrapper's instructions.
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
            return content
        # Some clients send content as a list of {"type":"text","text":...}
        # parts. Concatenate the text parts so fixtures can match either.
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def select_response(body: Any) -> tuple[dict[str, Any], str]:
    """Return (response_dict, label) for a forwarded body.

    label is human-readable (the fixture name or 'scenario:<x>') so the
    request log makes the dispatch path obvious during debugging.
    """
    prompt = extract_user_prompt(body)
    for fx in FIXTURES:
        if fx.matches(prompt):
            return fx.response, f"fixture:{fx.filename}"
    if SCENARIO in SCENARIO_RESPONSES:
        return SCENARIO_RESPONSES[SCENARIO], f"scenario:{SCENARIO}"
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
        "fallback:none",
    )


app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(path: str) -> Response:
    body = request.get_json(silent=True)
    print(
        f"[mock-upstream] {request.method} /{path} body={json.dumps(body) if body is not None else '<none>'}",
        flush=True,
    )

    response_body, label = select_response(body)
    print(f"[mock-upstream] dispatch -> {label}", flush=True)

    return Response(
        json.dumps(response_body),
        status=200,
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
