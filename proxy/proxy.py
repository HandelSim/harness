"""harness translating proxy (Phase 2).

Receives ollama-format /api/chat requests, translates them into the
non-standard upstream API shape ({"model", "messages"} only), POSTs to the
upstream, then emits NDJSON chunks back to ollama matching api.ChatResponse.

Environment variables (see README / .env.example):
    PROXY_HOST           bind address (default 0.0.0.0)
    PROXY_PORT           bind port (default 8000)
    PROXY_API_URL        upstream endpoint URL (REQUIRED)
    PROXY_API_KEY        upstream bearer token (REQUIRED)
    PROXY_API_MODEL      upstream model id (REQUIRED)
    OUTPUT_DIR           debug-dump directory (optional)
    PROXY_TIMEOUT        upstream request timeout, seconds (default 180)
"""

import datetime
import json
import os
import sys
import traceback
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, Response, request

# verify=False is required because the upstream uses a self-signed cert.
# Suppress the noisy InsecureRequestWarning at module load.
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))
PROXY_API_URL: str = os.environ.get("PROXY_API_URL", "").strip()
PROXY_API_KEY: str = os.environ.get("PROXY_API_KEY", "").strip()
PROXY_API_MODEL: str = os.environ.get("PROXY_API_MODEL", "").strip()
PROXY_TIMEOUT: int = int(os.environ.get("PROXY_TIMEOUT", "180"))
OLLAMA_CONTEXT_LENGTH: int = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "200000"))

_OUTPUT_DIR: Optional[str] = None  # set in main() before serving


# ---------------------------------------------------------------------------
# OUTPUT_DIR handling
# ---------------------------------------------------------------------------

def init_output_dir() -> Optional[str]:
    raw = os.environ.get("OUTPUT_DIR", "").strip()
    if not raw:
        return None
    try:
        os.makedirs(raw, exist_ok=True)
        test_path = os.path.join(raw, ".write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return raw
    except Exception as e:
        print(f"[!] OUTPUT_DIR '{raw}' is not writable ({e}); debug file dumps disabled", flush=True)
        return None


def save_debug_file(req_id: str, stage_prefix: str, stage_name: str, payload: Any) -> None:
    if _OUTPUT_DIR is None:
        return
    filename = f"{req_id}_{stage_prefix}_{stage_name}.json"
    filepath = os.path.join(_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"[-] {req_id} failed to save debug file {filename}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Preserved-verbatim helpers (proven correct against opencode in production).
# Do not modify these — the prompt text inside is tuned.
# ---------------------------------------------------------------------------

def format_tools_to_text(tools_array):
    # Emit the full JSON Schema for each tool's parameters rather than a
    # one-level summary. The earlier flattened format dropped nested
    # object/array structure (e.g., opencode's `todowrite` with an array of
    # {content, status, priority} objects), so the upstream LLM had no idea
    # what fields to populate inside each item. JSON Schema is the lingua
    # franca here — Claude and other capable models read it natively. Token
    # cost goes up a few KB per request; correctness wins.
    if not tools_array:
        return "No tools available."
    schema_text = ""
    for tool in tools_array:
        func = tool.get("function", {}) if "function" in tool else tool
        name = func.get("name", "unknown_tool")
        desc = func.get("description", "No description provided.")
        parameters = func.get("parameters", {})
        schema_text += f"Tool Name: `{name}`\n"
        schema_text += f"Description: {desc}\n"
        schema_text += "Parameters (JSON Schema):\n"
        schema_text += "```json\n"
        schema_text += json.dumps(parameters, indent=2)
        schema_text += "\n```\n\n"
    return schema_text.strip()


def build_cooperative_prompt_user(original_content, tools_text):
    # Marker delimiters around the user content rather than bare quotes —
    # if the user's prompt itself contains quotation marks (or code that
    # uses them), bare quotes confuse the model about where the original
    # request ends. The <<<BEGIN/END_USER_REQUEST>>> markers are unambiguous.
    return f"""You are a helpful and intelligent AI assistant.

### Tool Usage Instructions
You have access to specific tools to help answer the user's request. If you need to use a tool, you MUST output a strictly formatted JSON object inside standard Markdown code blocks (```json ... ```). It must follow this exact structure:
{{
  "name": "<tool_name>",
  "arguments": {{
    <tool_parameters>
  }}
}}
You may explain your thought process before or after the JSON block. If NO tools are needed, simply answer the user normally.

### Available Tools
{tools_text}

### User Request
<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>
"""


def build_cooperative_prompt_tool(original_content, tools_text):
    return f"""You are a helpful AI assistant executing a multi-step process.

### Tool Usage Instructions
Review the latest System Observation below. If you need to use another tool to continue, output a strictly formatted JSON object inside standard Markdown code blocks (```json ... ```) with this structure:
{{
  "name": "<tool_name>",
  "arguments": {{
    <tool_parameters>
  }}
}}
You may explain your reasoning before or after the JSON block. If the task is fully complete, answer the user normally without any JSON.

### Available Tools
{tools_text}

### Latest System Observation
<<<BEGIN_OBSERVATION>>>
{original_content}
<<<END_OBSERVATION>>>
"""


def _scan_balanced_json(text, start):
    """Scan from `start` for a complete JSON object, tracking string
    boundaries and brace depth. Returns (json_str, position_after_json)
    or (None, start) if no complete object found.

    String content (between unescaped double quotes) is opaque — braces
    and backticks inside strings do NOT count as structural. Backslash
    escapes within strings are honored. This lets the scanner walk past
    LLM-emitted tool-call arguments whose strings contain markdown code
    fences or embedded JSON examples.
    """
    if start >= len(text) or text[start] != '{':
        return None, start

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if in_string:
            if ch == '\\':
                escape_next = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1

    return None, start


def extract_tool_call_and_text(response_text):
    """Extract a tool-call JSON payload from the response.

    Searches for ```json ... ``` blocks and uses balanced-brace scanning
    (not regex) to locate the JSON object boundaries. The regex this
    replaces failed when JSON string values contained backticks or nested
    code fences — the lazy match terminated on the first inner ``` instead
    of the outer one, truncating the JSON.

    If multiple ```json blocks exist, returns the first one that parses
    to a valid {name, arguments} payload.

    Returns (payload, clean_text). payload is None when no valid tool call
    was found, in which case clean_text equals response_text.
    """
    pos = 0

    while True:
        fence_start = response_text.find('```json', pos)
        if fence_start == -1:
            break

        body_start = fence_start + len('```json')
        while body_start < len(response_text) and response_text[body_start] in ' \t\n\r':
            body_start += 1

        json_str, after_json = _scan_balanced_json(response_text, body_start)

        if json_str is None:
            pos = body_start
            continue

        rest_start = after_json
        while rest_start < len(response_text) and response_text[rest_start] in ' \t\n\r':
            rest_start += 1

        closing_fence_pos = response_text.find('```', rest_start)
        if closing_fence_pos == -1:
            block_end = after_json
        else:
            block_end = closing_fence_pos + 3

        try:
            candidate = json.loads(json_str)
        except json.JSONDecodeError:
            pos = after_json
            continue

        if not isinstance(candidate, dict):
            pos = after_json
            continue
        if 'name' not in candidate or 'arguments' not in candidate:
            pos = after_json
            continue

        block = response_text[fence_start:block_end]
        clean_text = response_text.replace(block, '', 1).strip()

        return candidate, clean_text

    return None, response_text


# ---------------------------------------------------------------------------
# Translation: ollama-format -> upstream-format
# ---------------------------------------------------------------------------

def translate_history_and_apply_prompt(original_messages: List[Dict[str, Any]], tools_text: str) -> List[Dict[str, str]]:
    """
    Translate ollama-format messages into a flat conversation suitable for the
    upstream API. Tool calls become markdown JSON blocks embedded in assistant
    content; tool results become System Observations folded into the next user
    message. The cooperative-prompt wrapper is applied to the final user message
    if tools are available.
    """
    if not original_messages:
        return []

    messages: List[Dict[str, str]] = []

    for msg in original_messages:
        role = msg.get("role")
        content = msg.get("content", "") or ""

        if role == "system":
            # Coalesce consecutive system messages into one. Some clients
            # (and our own injection paths) emit multiple system blocks back-
            # to-back; the upstream API treats those as separate turns and
            # may give them less weight than a single combined block.
            if messages and messages[-1]["role"] == "system":
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": "system", "content": content})

        elif role == "user":
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": "user", "content": content})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args = func.get("arguments", {})
                    # Ollama args is an object; render as compact JSON. Accept a
                    # string defensively in case of mixed-protocol clients.
                    if isinstance(args, str):
                        args_json_str = args
                    else:
                        args_json_str = json.dumps(args)
                    md_block = f"```json\n{{\n  \"name\": \"{name}\",\n  \"arguments\": {args_json_str}\n}}\n```"
                    content += f"\n{md_block}\n"
            messages.append({"role": "assistant", "content": content.strip()})

        elif role == "tool":
            tool_name = msg.get("tool_name") or msg.get("name") or "unknown_tool"
            observation = f"[System Observation: Tool '{tool_name}' executed. Result:]\n{content}"
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{observation}"
            else:
                messages.append({"role": "user", "content": observation})

    if tools_text and messages and messages[-1]["role"] == "user":
        original_last_role = original_messages[-1].get("role")
        final_content = messages[-1]["content"]
        if original_last_role == "tool":
            messages[-1]["content"] = build_cooperative_prompt_tool(final_content, tools_text)
        else:
            messages[-1]["content"] = build_cooperative_prompt_user(final_content, tools_text)

    return messages


# ---------------------------------------------------------------------------
# NDJSON response generation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    n = max(1, len(text) // 4)
    return min(n, OLLAMA_CONTEXT_LENGTH)


def make_chunk(
    model_name: str,
    content: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    done: bool = False,
    done_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a single ollama api.ChatResponse-shaped chunk."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    chunk: Dict[str, Any] = {
        "model": model_name,
        "created_at": now,
        "message": message,
        "done": done,
    }
    if done:
        u = usage or {}
        chunk["done_reason"] = done_reason or "stop"
        chunk["total_duration"] = 1
        chunk["load_duration"] = 1
        chunk["prompt_eval_count"] = u.get("prompt_tokens") or 1
        chunk["prompt_eval_duration"] = 1
        chunk["eval_count"] = u.get("completion_tokens") or 1
        chunk["eval_duration"] = 1
    return chunk


def generate_ndjson(
    model_name: str,
    clean_text: str,
    tool_call_payload: Optional[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
) -> Iterable[str]:
    """Yield NDJSON lines for the response."""
    if clean_text:
        yield json.dumps(make_chunk(model_name, content=clean_text)) + "\n"
    if tool_call_payload:
        # An `id` is required so claude-code can later reference the
        # tool_use block in conversation history. Without it the
        # Anthropic-compatible upstream rejects the next turn with
        # "tool_use block missing required 'id' field". The toolu_<24hex>
        # format mirrors what real Anthropic returns.
        tool_call_id = f"toolu_{uuid.uuid4().hex[:24]}"
        tc = [{
            "id": tool_call_id,
            "function": {
                "name": tool_call_payload["name"],
                "arguments": tool_call_payload["arguments"],
            },
        }]
        yield json.dumps(make_chunk(model_name, tool_calls=tc)) + "\n"
    done_reason = "tool_calls" if tool_call_payload else "stop"
    yield json.dumps(make_chunk(model_name, done=True, done_reason=done_reason, usage=usage)) + "\n"


# ---------------------------------------------------------------------------
# Upstream extraction
# ---------------------------------------------------------------------------

def extract_assistant_content(target_json: Dict[str, Any]) -> str:
    """Pull the assistant content out of the upstream response. The upstream
    follows OpenAI's chat-completion shape: choices[0].message.content."""
    choices = target_json.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or ""


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(path: str) -> Response:
    req_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    try:
        ollama_request = request.get_json(silent=True) or {}
        save_debug_file(req_id, "01", "Ollama_Request", ollama_request)

        model_name = ollama_request.get("model") or "harness"
        original_messages = ollama_request.get("messages") or []
        tools = ollama_request.get("tools") or []

        print(f"[{req_id}] {request.method} /{path} model={model_name} messages={len(original_messages)} tools={len(tools)}", flush=True)

        tools_text = format_tools_to_text(tools)
        translated = translate_history_and_apply_prompt(original_messages, tools_text)

        upstream_payload = {
            "model": PROXY_API_MODEL,
            "messages": translated,
        }
        save_debug_file(req_id, "02", "API_Request", upstream_payload)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PROXY_API_KEY}",
        }

        try:
            resp = requests.post(
                PROXY_API_URL,
                headers=headers,
                json=upstream_payload,
                verify=False,
                timeout=PROXY_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"[{req_id}] upstream request failed: {e}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"error": str(e)})
            return Response(
                json.dumps({"error": "upstream request failed", "details": str(e)}),
                status=502,
                mimetype="application/json",
            )

        if resp.status_code >= 400:
            err_body: Any
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            print(f"[{req_id}] upstream returned {resp.status_code}: {err_body}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"status": resp.status_code, "body": err_body})
            return Response(
                json.dumps({"error": "upstream non-OK", "status": resp.status_code, "body": err_body}),
                status=502,
                mimetype="application/json",
            )

        try:
            target_json = resp.json()
        except ValueError as e:
            print(f"[{req_id}] upstream returned non-JSON: {e}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"error": "non-json", "body": resp.text})
            return Response(
                json.dumps({"error": "upstream returned non-JSON", "details": str(e)}),
                status=502,
                mimetype="application/json",
            )

        save_debug_file(req_id, "03", "API_Response", target_json)

        response_text = extract_assistant_content(target_json)
        tool_call_payload, clean_text = extract_tool_call_and_text(response_text)

        usage = target_json.get("usage") or {}
        # If usage missing fields, estimate from joined inputs/outputs.
        if not usage.get("prompt_tokens"):
            joined = "\n".join(m.get("content", "") for m in translated)
            usage = dict(usage)
            usage["prompt_tokens"] = _estimate_tokens(joined)
        if not usage.get("completion_tokens"):
            usage = dict(usage)
            usage["completion_tokens"] = _estimate_tokens(response_text)

        print(f"[{req_id}] upstream OK; emitting NDJSON (tool_call={'yes' if tool_call_payload else 'no'})", flush=True)

        return app.response_class(
            generate_ndjson(model_name, clean_text, tool_call_payload, usage),
            mimetype="application/x-ndjson",
        )

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[{req_id}] FATAL: {e}\n{tb}", flush=True)
        save_debug_file(req_id, "99", "Fatal_Error", {"error": str(e), "traceback": tb})
        return Response(
            json.dumps({"error": "proxy internal error", "details": str(e)}),
            status=500,
            mimetype="application/json",
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _redact_key(key: str) -> str:
    if not key:
        return "<empty>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _validate_config() -> None:
    missing = []
    if not PROXY_API_URL:
        missing.append("PROXY_API_URL")
    if not PROXY_API_KEY:
        missing.append("PROXY_API_KEY")
    if not PROXY_API_MODEL:
        missing.append("PROXY_API_MODEL")
    if missing:
        print(f"[!] FATAL: required env vars missing or empty: {', '.join(missing)}", flush=True)
        sys.exit(1)


def main() -> None:
    global _OUTPUT_DIR

    _validate_config()
    _OUTPUT_DIR = init_output_dir()

    raw_output = os.environ.get("OUTPUT_DIR", "").strip()
    if not raw_output:
        output_status = "disabled (OUTPUT_DIR not set)"
    elif _OUTPUT_DIR is None:
        output_status = f"disabled ('{raw_output}' not writable)"
    else:
        output_status = f"enabled at '{_OUTPUT_DIR}'"

    print(
        "============================================================\n"
        " harness translating proxy (Phase 2)\n"
        f"   listening on:   {PROXY_HOST}:{PROXY_PORT}\n"
        f"   upstream URL:   {PROXY_API_URL}\n"
        f"   upstream model: {PROXY_API_MODEL}\n"
        f"   upstream key:   {_redact_key(PROXY_API_KEY)}\n"
        f"   timeout:        {PROXY_TIMEOUT}s\n"
        f"   debug dumps:    {output_status}\n"
        "============================================================",
        flush=True,
    )

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)


if __name__ == "__main__":
    main()
