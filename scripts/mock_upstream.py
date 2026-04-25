"""Mock upstream API for proxy integration tests.

Listens on port 9000 (override with MOCK_PORT) and returns a canned response
shaped like an OpenAI chat completion. The exact body is selected by the
MOCK_SCENARIO env var:

    text  -> simple assistant text + usage stats
    tool  -> assistant text with an embedded ```json``` tool-call block

Every request is logged to stdout (method, path, parsed JSON body) so
integration tests can scrape the logs to verify what the proxy forwarded.
"""

import json
import os
import sys

from flask import Flask, Response, request


SCENARIO = os.environ.get("MOCK_SCENARIO", "text").strip().lower()
PORT = int(os.environ.get("MOCK_PORT", "9000"))


RESPONSES = {
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


app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(path):
    body = request.get_json(silent=True)
    print(
        f"[mock-upstream] {request.method} /{path} body={json.dumps(body) if body is not None else '<none>'}",
        flush=True,
    )

    if SCENARIO not in RESPONSES:
        return Response(
            json.dumps({"error": f"unknown MOCK_SCENARIO '{SCENARIO}'"}),
            status=500,
            mimetype="application/json",
        )

    return Response(
        json.dumps(RESPONSES[SCENARIO]),
        status=200,
        mimetype="application/json",
    )


if __name__ == "__main__":
    print(
        f"[mock-upstream] starting on 0.0.0.0:{PORT} scenario={SCENARIO}",
        file=sys.stdout,
        flush=True,
    )
    app.run(host="0.0.0.0", port=PORT, debug=False)
