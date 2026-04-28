"""harness translating proxy.

Translates between ollama's /api/chat wire format and the upstream API's
chat-completions format. Injects cooperative tool-use prompts so models
that don't natively support tool calls can produce them as ```json blocks
that the proxy then parses and re-emits as native tool_calls.

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

# Cooperative-prompt injection mode. Five values are accepted via the
# PROXY_PROMPT_MODE env var:
#   "user_front"   — DEFAULT. Same active-instruction structure as `user`
#                    mode (full scaffolding on the last user message), but
#                    with the user's request placed BEFORE the tool list
#                    rather than after it. Avoids burying the question
#                    under 10-15K tokens of tool schemas while still
#                    giving the model a clear active-turn instruction to
#                    emit tool calls when needed.
#   "user_bookend" — Like `user_front`, but the request is repeated AFTER
#                    the tool list as well. Both occurrences are wrapped
#                    in <<<BEGIN_USER_REQUEST>>> markers. Highest
#                    instruction-following reliability at the cost of a
#                    duplicated request payload.
#   "user"         — legacy: full scaffolding + tool list re-injected into
#                    the last user message, with the request at the END.
#                    Reliable for tool use but buries the user's actual
#                    question and causes conversation-context loss across
#                    turns.
#   "system"       — full scaffolding lives in the system message; user
#                    turns pass through unchanged. Cheapest. Some
#                    upstreams treat system content as background and
#                    don't reliably emit tool calls.
#   "hybrid"       — full tools in the system message + a brief ~50-token
#                    reminder wrapping the last user message. Same caveat
#                    as `system` for tool reliability.
_PROMPT_MODE: str = "user_front"  # set in main() before serving


# ---------------------------------------------------------------------------
# OUTPUT_DIR handling
# ---------------------------------------------------------------------------

def _setup_prompt_mode() -> None:
    """Read PROXY_PROMPT_MODE from the env, validate, and set the module
    global. Invalid values fall back to 'user_front' with a warning."""
    global _PROMPT_MODE
    raw = os.environ.get("PROXY_PROMPT_MODE", "user_front").strip().lower()
    valid = ("user", "system", "hybrid", "user_front", "user_bookend")
    if raw not in valid:
        print(
            f"[!] PROXY_PROMPT_MODE='{raw}' is not one of "
            f"{'/'.join(valid)}; defaulting to 'user_front'",
            flush=True,
        )
        raw = "user_front"
    _PROMPT_MODE = raw


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


def build_cooperative_prompt_user_front(original_content, tools_text):
    """user_front mode: user's request appears FIRST, then tool definitions.
    Same instruction text and markers as legacy `user` mode — only position
    differs. The request gets primacy attention rather than being buried
    after 10-15K tokens of tool schemas, so the model retains conversation
    context across turns better than the legacy mode.
    """
    return f"""<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>

---

You are a helpful and intelligent AI assistant.

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
"""


def build_cooperative_prompt_tool_front(original_content, tools_text):
    """tool_front mode (the user_front variant for tool-result turns).
    The tool result observation goes first, then the tool definitions.
    """
    return f"""<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>

---

You are a helpful and intelligent AI assistant.

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
"""


def build_cooperative_prompt_user_bookend(original_content, tools_text):
    """user_bookend mode: request first, then tool definitions, then
    request again. Both occurrences wrapped in <<<BEGIN_USER_REQUEST>>>
    markers. Maximizes attention on the user's actual question via both
    primacy and recency at the cost of duplicating the request text.
    """
    return f"""<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>

---

You are a helpful and intelligent AI assistant.

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

---

<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>
"""


def build_cooperative_prompt_tool_bookend(original_content, tools_text):
    """tool_bookend mode (the user_bookend variant for tool-result turns).
    Tool result observation appears at both ends with the tool definitions
    in the middle.
    """
    return f"""<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>

---

You are a helpful and intelligent AI assistant.

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

---

<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>
"""


def build_cooperative_prompt_system_addition(tools_text):
    """Returns the cooperative-prompt scaffolding to APPEND to the system
    message in modes 'system' and 'hybrid'. Static across all turns; safe
    to set once on the system message rather than re-sending per turn.
    """
    return f"""

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
"""


def build_cooperative_prompt_hybrid_reminder(content):
    """In hybrid mode, the full tool list lives in the system message.
    User turns get a brief reminder so the model doesn't lose tool
    awareness in long conversations. The reminder is ~50 tokens — far
    smaller than the full schemas — and is placed BEFORE the user's
    actual content so the user's content remains at the end (recency
    bias matters for instruction-following models).
    """
    reminder = (
        "[Tool reminder: tool definitions are in the system prompt above. "
        "To use a tool, emit a ```json block with {name, arguments}. "
        "Otherwise answer the user normally.]"
    )
    return f"{reminder}\n\n{content}"


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


def extract_tool_calls_and_text(response_text):
    """Extract ALL tool-call JSON payloads from the response, in order.

    Searches for ```json ... ``` blocks and uses balanced-brace scanning
    (not regex) to locate the JSON object boundaries. The regex this
    replaces failed when JSON string values contained backticks or nested
    code fences — the lazy match terminated on the first inner ``` instead
    of the outer one, truncating the JSON.

    Real upstream LLMs (Gemini Enterprise, claude-3.5-sonnet variants, etc.)
    frequently emit multiple tool calls per response when the agent's task
    naturally calls for parallel work — reading multiple files, calling
    multiple APIs, etc. Each ```json block with valid {name, arguments}
    becomes a separate tool call; their order is preserved.

    A block that fails to parse, doesn't have the expected shape, or is
    missing required keys is left in the text — clean_text will contain
    those invalid blocks intact (the agent then sees them as content,
    which is correct: the LLM may have been describing JSON, not asking
    to invoke a tool).

    Returns (payloads, clean_text). payloads is a list (possibly empty)
    of {name, arguments} dicts in the order they appeared. clean_text is
    response_text with all VALID extracted blocks removed.
    """
    payloads = []
    consumed_ranges = []  # (fence_start, block_end) tuples for blocks we extracted
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

        # Valid tool call. Record it and the byte range to remove later.
        payloads.append(candidate)
        consumed_ranges.append((fence_start, block_end))
        pos = block_end

    # Build clean_text by removing all consumed ranges. Process in REVERSE
    # so earlier indices stay valid as we slice. (Forward-order removal
    # would shift the offsets of later ranges.)
    clean_chars = list(response_text)
    for start, end in sorted(consumed_ranges, reverse=True):
        del clean_chars[start:end]
    clean_text = ''.join(clean_chars).strip()

    return payloads, clean_text


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

    # Mode-based cooperative-prompt injection. Default mode is 'user_front'.
    #   user_front   — request first, then tool definitions, on the last
    #                  user message. Best balance of tool reliability +
    #                  conversation-context retention (default).
    #   user_bookend — request first, tool definitions, request again. Both
    #                  occurrences wrapped in markers. Highest reliability.
    #   user         — legacy: full scaffolding on the last user message
    #                  with the request at the END.
    #   system       — full scaffolding appended to the system message;
    #                  user turns pass through unchanged.
    #   hybrid       — full scaffolding in the system message + brief
    #                  reminder wrapping the last user message.
    if tools_text and messages:
        if _PROMPT_MODE == "user":
            if messages[-1]["role"] == "user":
                original_last_role = original_messages[-1].get("role")
                final_content = messages[-1]["content"]
                if original_last_role == "tool":
                    messages[-1]["content"] = build_cooperative_prompt_tool(final_content, tools_text)
                else:
                    messages[-1]["content"] = build_cooperative_prompt_user(final_content, tools_text)
        elif _PROMPT_MODE == "user_front":
            if messages[-1]["role"] == "user":
                original_last_role = original_messages[-1].get("role")
                final_content = messages[-1]["content"]
                if original_last_role == "tool":
                    messages[-1]["content"] = build_cooperative_prompt_tool_front(final_content, tools_text)
                else:
                    messages[-1]["content"] = build_cooperative_prompt_user_front(final_content, tools_text)
        elif _PROMPT_MODE == "user_bookend":
            if messages[-1]["role"] == "user":
                original_last_role = original_messages[-1].get("role")
                final_content = messages[-1]["content"]
                if original_last_role == "tool":
                    messages[-1]["content"] = build_cooperative_prompt_tool_bookend(final_content, tools_text)
                else:
                    messages[-1]["content"] = build_cooperative_prompt_user_bookend(final_content, tools_text)
        else:
            # 'system' or 'hybrid' — both append scaffolding to the system
            # message. The tool-result handling above already converted
            # role:"tool" entries into user messages with a [System
            # Observation] wrapper; we don't tack the cooperative prompt
            # onto those (it lives in the system message instead).
            system_addition = build_cooperative_prompt_system_addition(tools_text)
            if messages[0]["role"] == "system":
                existing = messages[0]["content"]
                if isinstance(existing, str):
                    messages[0]["content"] = existing + system_addition
                elif isinstance(existing, list):
                    # Some clients send system as a list of content blocks.
                    # Append a text block rather than concatenating strings.
                    messages[0]["content"] = existing + [{"type": "text", "text": system_addition}]
                else:
                    messages[0]["content"] = str(existing) + system_addition
            else:
                # No system message present — insert one. Strip the leading
                # blank line that the addition starts with so the system
                # content doesn't begin with whitespace.
                messages.insert(0, {"role": "system", "content": system_addition.strip()})

            # Hybrid additionally drops a brief reminder on the last user
            # turn so the model doesn't lose tool awareness in long
            # conversations. Applies regardless of how the user message
            # was formed (real user turn vs. tool-result-converted) — the
            # reminder is short and the model should be reminded tools
            # are available either way.
            if _PROMPT_MODE == "hybrid" and messages[-1]["role"] == "user":
                messages[-1]["content"] = build_cooperative_prompt_hybrid_reminder(
                    messages[-1]["content"]
                )

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
    tool_call_payloads: List[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
) -> Iterable[str]:
    """Yield NDJSON lines for the response.

    Multiple tool calls (when the upstream produced multiple ```json blocks)
    are emitted as a single tool_calls array in one chunk, preserving their
    order. Each call gets a unique toolu_-prefixed id since claude-code's
    Anthropic-format conversation history requires the id field per
    tool_use block.
    """
    if clean_text:
        yield json.dumps(make_chunk(model_name, content=clean_text)) + "\n"
    if tool_call_payloads:
        tcs = []
        for payload in tool_call_payloads:
            tcs.append({
                "id": f"toolu_{uuid.uuid4().hex[:24]}",
                "function": {
                    "name": payload["name"],
                    "arguments": payload["arguments"],
                },
            })
        yield json.dumps(make_chunk(model_name, tool_calls=tcs)) + "\n"
    done_reason = "tool_calls" if tool_call_payloads else "stop"
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
        tool_call_payloads, clean_text = extract_tool_calls_and_text(response_text)

        usage = target_json.get("usage") or {}
        # If usage missing fields, estimate from joined inputs/outputs.
        if not usage.get("prompt_tokens"):
            joined = "\n".join(m.get("content", "") for m in translated)
            usage = dict(usage)
            usage["prompt_tokens"] = _estimate_tokens(joined)
        if not usage.get("completion_tokens"):
            usage = dict(usage)
            usage["completion_tokens"] = _estimate_tokens(response_text)

        print(f"[{req_id}] upstream OK; emitting NDJSON (tool_calls={len(tool_call_payloads)})", flush=True)

        # Materialize the NDJSON chunks so we can dump them to debug output
        # before streaming. Memory cost is the response size — at most a few
        # KB for typical tool-call responses; not a concern. Avoids needing
        # a write-around-while-yielding mechanism. The upstream API call
        # already completed fully before NDJSON generation began (the proxy
        # isn't streaming from upstream — it gets the full response, then
        # translates), so materializing-then-yielding doesn't change latency:
        # ollama gets the first NDJSON chunk at the same moment it would
        # have under the streaming generator.
        ndjson_chunks = list(generate_ndjson(model_name, clean_text, tool_call_payloads, usage))
        save_debug_file(req_id, "04", "NDJSON_Response", {"chunks": ndjson_chunks})

        return app.response_class(
            iter(ndjson_chunks),
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
    _setup_prompt_mode()

    raw_output = os.environ.get("OUTPUT_DIR", "").strip()
    if not raw_output:
        output_status = "disabled (OUTPUT_DIR not set)"
    elif _OUTPUT_DIR is None:
        output_status = f"disabled ('{raw_output}' not writable)"
    else:
        output_status = f"enabled at '{_OUTPUT_DIR}'"

    print(
        "============================================================\n"
        " harness translating proxy\n"
        f"   listening on:   {PROXY_HOST}:{PROXY_PORT}\n"
        f"   upstream URL:   {PROXY_API_URL}\n"
        f"   upstream model: {PROXY_API_MODEL}\n"
        f"   upstream key:   {_redact_key(PROXY_API_KEY)}\n"
        f"   timeout:        {PROXY_TIMEOUT}s\n"
        f"   prompt mode:    {_PROMPT_MODE}\n"
        f"   debug dumps:    {output_status}\n"
        "============================================================",
        flush=True,
    )

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)


if __name__ == "__main__":
    main()
