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
import re
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

# Cooperative-prompt injection mode. Two cooperative modes plus one bypass
# are accepted via the PROXY_PROMPT_MODE env var:
#   "user_front" — DEFAULT. Full scaffolding (tool list + tool-call format
#                  instructions) on the last user message, with the user's
#                  request placed BEFORE the tool list rather than after
#                  it. Puts the request in primacy position; the tool list
#                  follows at recency. Established baseline.
#   "hybrid"     — Full tool definitions sit at the stable prefix
#                  (appended to the system message; with the default
#                  _CHANGE_SYSTEM_TO_USER post-pass this becomes the
#                  user-role message at index 0). A short reminder
#                  restating the JSON envelope format, the "don't invent
#                  tool results" rule, and the per-tool parameter
#                  signatures (`name(required, [optional])`) is prepended
#                  to the last user message. Tools at prefix + signature
#                  reminder at recency.
#   "passthrough" — Benchmark control. Skips every harness-side mediation:
#                  no cooperative-prompt injection, no system→user
#                  rewrite, no history translation. Forwards tools to
#                  upstream verbatim. Not a cooperative mode; used to
#                  measure what harness's mediation contributes.
_PROMPT_MODE: str = "user_front"  # set in main() before serving

# Some upstream APIs silently drop the `system` role. When set, this converts
# the system message(s) into a user message at the start of the conversation,
# with a stub assistant message between to satisfy strict role-alternation.
# Default ON because the failure mode is invisible (the model just doesn't
# follow system instructions and you may not notice). Set to "0" to disable
# for upstreams that DO support system roles.
_CHANGE_SYSTEM_TO_USER: bool = True

# Hybrid mode only. Tool names whose FULL description is echoed verbatim into
# the recency reminder, each in its own <<<BEGIN_TOOL_DETAIL>>> block. These
# are the tools whose valid argument *values* are an unguessable closed set
# that opencode documents only as prose inside the tool description — `task`
# (the valid `subagent_type` agent names) and `skill` (the valid skill
# names). The per-tool signature list carries the parameter keys but not
# those values, so the whole description is what has to reach recency. Read
# from PROXY_HYBRID_DETAIL_TOOLS (comma-separated) in main(); empty disables.
_HYBRID_DETAIL_TOOLS: List[str] = ["task", "skill"]

# opencode builds the `task` tool's description by appending a dynamic agent
# list onto a block of static boilerplate ("when to use Task", usage notes).
# That boilerplate carries no closed-set values and is already present verbatim
# at the stable prefix, so for `task` ONLY the recency TOOL_DETAIL block is
# pared to the agent-list section — everything from this header onward. The
# header is the seam in opencode's ToolRegistry.describeTask and has been
# byte-identical across releases (verified 1.14.41 and 1.15.7). If a future
# opencode renames it, the parse falls back to the full description — no
# closed-set values are ever dropped — and TestTaskDescriptionParing is the
# canary that flags the drift. `skill`'s description is short and left verbatim.
_OPENCODE_TASK_AGENTS_HEADER = (
    "Available agent types and the tools they have access to:"
)
_OPENCODE_TASK_AGENTS_RE = re.compile(
    "^" + re.escape(_OPENCODE_TASK_AGENTS_HEADER) + ".*",
    re.MULTILINE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# OUTPUT_DIR handling
# ---------------------------------------------------------------------------

def _setup_prompt_mode() -> None:
    """Read PROXY_PROMPT_MODE from the env, validate, and set the module
    global. Invalid values fall back to 'user_front' with a warning."""
    global _PROMPT_MODE
    raw = os.environ.get("PROXY_PROMPT_MODE", "user_front").strip().lower()
    valid = ("hybrid", "user_front", "passthrough")
    if raw not in valid:
        print(
            f"[!] PROXY_PROMPT_MODE='{raw}' is not one of "
            f"{'/'.join(valid)}; defaulting to 'user_front'",
            flush=True,
        )
        raw = "user_front"
    _PROMPT_MODE = raw


def _setup_change_system_to_user() -> None:
    global _CHANGE_SYSTEM_TO_USER
    raw = os.environ.get("PROXY_CHANGE_SYSTEM_PROMPT_TO_USER", "1").strip().lower()
    _CHANGE_SYSTEM_TO_USER = raw not in ("0", "false", "no", "off", "")
    print(f"[i] convert system to user: {_CHANGE_SYSTEM_TO_USER}", flush=True)


def _setup_hybrid_detail_tools() -> None:
    """Read PROXY_HYBRID_DETAIL_TOOLS (comma-separated tool names) and set the
    module global. Names are trimmed and blanks dropped. An empty value
    disables the feature (no TOOL_DETAIL blocks emitted). Only consulted in
    hybrid mode."""
    global _HYBRID_DETAIL_TOOLS
    raw = os.environ.get("PROXY_HYBRID_DETAIL_TOOLS", "task,skill")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    _HYBRID_DETAIL_TOOLS = names
    print(f"[i] hybrid detail tools: {', '.join(names) or '(none)'}", flush=True)


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
# Cooperative-prompt builders and tool-call extraction.
#
# The prompt text in these builders is tuned — change it deliberately, with
# a reason, not incidentally. It must stay AGENT-AGNOSTIC: agents present
# tool results differently, so the builders wrap and inject around incoming
# content; they never parse or depend on the shape of what the agent put
# inside a tool message.
# ---------------------------------------------------------------------------

# Tool-result turns: the translator wraps every role:"tool" message's content
# in <<<BEGIN_TOOL_RESULT>>> / <<<END_TOOL_RESULT>>> markers BEFORE any builder
# runs, so the result arrives self-delimiting. The tool-variant builders below
# only inject framing around that already-delimited block — they do not parse
# it and do not re-wrap it. This framing line states what the block is and the
# loop semantics; it opens every tool-result turn.
_TOOL_RESULT_FRAMING = (
    "[The <<<BEGIN_TOOL_RESULT>>> block(s) in this message are output from "
    "tool call(s) you made on a previous turn — they are NOT a message from "
    "the user. Review the result(s), then continue the task: emit a ```json "
    "block to call another tool, or give your final answer if the task is "
    "complete.]"
)

# Closes a tool-result turn in user_front (`tool_front`) mode so the recency
# slot is a "now act" instruction rather than raw tool schema.
_TOOL_CONTINUE_CUE = (
    "[End of tool definitions. Act on the tool result(s) above now — emit a "
    "```json tool call to continue, or give your final answer if the task is "
    "complete.]"
)

# Opens the cooperative-prompt scaffold on genuine user turns in
# user_front mode (`build_cooperative_prompt_user_front`). It does two
# things: tells the model the delimited block is the user's actual message
# for this turn, and
# keeps the model's identity anchored. The identity clause replaces an
# earlier generic persona line ("You are a helpful and intelligent AI
# assistant"): because the scaffold lands in the last user message and is
# re-read every turn, the model treated that line as the ACTIVE persona,
# overriding the real persona from the upstream conversation (e.g. opencode's
# "You are opencode") and letting its pretrained identity surface ("Gemini
# Enterprise"). This line must NOT describe the tool-call format — that
# scaffolding sits AFTER the request block, so an intro about tools would
# mislabel what follows it. It also must not contain the literal
# <<<BEGIN_USER_REQUEST>>> token, which would collide with the real marker.
_PERSONA_PRESERVE_FRAMING = (
    "[The delimited block below is the user's next message in this "
    "conversation. Continue acting as the assistant established earlier in "
    "this conversation; do not adopt a new identity.]"
)


def format_tools_to_text(tools_array):
    # Emit the full JSON Schema for each tool's parameters rather than a
    # one-level summary. The earlier flattened format dropped nested
    # object/array structure (e.g., opencode's `todowrite` with an array of
    # {content, status, priority} objects), so the upstream LLM had no idea
    # what fields to populate inside each item. JSON Schema is the lingua
    # franca here — capable models read it natively. Token
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


def build_cooperative_prompt_user_front(original_content, tools_text):
    """user_front mode: a persona-preserve intro line, then the user's
    request, then the tool definitions. The request gets primacy attention
    rather than being buried after 10-15K tokens of tool schemas, and the
    intro line keeps the model from adopting a new identity. Layout matches
    the tool-result variant: intro → delimited block → tool intro → tools.
    """
    return f"""{_PERSONA_PRESERVE_FRAMING}

<<<BEGIN_USER_REQUEST>>>
{original_content}
<<<END_USER_REQUEST>>>

---

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
    `original_content` is already wrapped in <<<BEGIN_TOOL_RESULT>>> markers
    by the translator. Layout: framing line, the delimited result, tool
    definitions, then a one-line continue cue so the recency slot is a
    'now act' instruction rather than raw tool schema. The builder injects
    framing around the result — it does not parse or re-wrap it.
    """
    return f"""{_TOOL_RESULT_FRAMING}

{original_content}

---

### Tool Usage Instructions
To call another tool, you MUST output a strictly formatted JSON object inside standard Markdown code blocks (```json ... ```). It must follow this exact structure:
{{
  "name": "<tool_name>",
  "arguments": {{
    <tool_parameters>
  }}
}}

You may explain your thought process before or after the JSON block. If NO further tool calls are needed, give your final answer normally.

### Available Tools
{tools_text}

---

{_TOOL_CONTINUE_CUE}
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

<<<BEGIN_AGENT_TOOLS>>>
The following are the only tools available for use in this conversation. Any other tool names or capabilities you may know about from other contexts or from elsewhere in this prompt do not apply here — use only the tools defined below.

{tools_text}
<<<END_AGENT_TOOLS>>>
"""


def _format_tool_signature(name, required, optional):
    """Render one tool's recency-reminder entry as
    `name(required_a, required_b, [optional_c], [optional_d])`. Required
    keys come first in their declared order; each optional key is wrapped
    in its own brackets so the model can't mistake the comma for an
    "all-or-nothing" group. When both lists are empty (no schema info, or
    a zero-param tool) we render a bare `name` rather than `name()` — the
    latter would imply we'd looked and found nothing.
    """
    parts = list(required)
    parts.extend(f"[{k}]" for k in optional)
    if not parts:
        return name
    return f"{name}({', '.join(parts)})"


def _format_tool_detail_blocks(tool_details):
    """Render the per-tool TOOL_DETAIL section appended to the hybrid
    reminder. `tool_details` is a list of `(name, description)` pairs from
    `_extract_tool_details` (which has already pared `task` down to its agent
    list). Each pair's description is echoed inside its own
    `<<<BEGIN_TOOL_DETAIL name="…">>>` block — never parsed here — so the closed
    set of valid argument values opencode documents only as prose (a `task`'s
    agent types, a `skill`'s skill names) reaches recency even when attention to
    messages[0] dilutes. The named delimiter (part of
    the same marker family as AGENT_TOOLS / TOOL_RESULT / USER_MESSAGE) keeps
    the model from conflating this recency copy with the authoritative copy at
    the stable prefix. Returns "" when there's nothing to surface.
    """
    if not tool_details:
        return ""
    blocks = "\n".join(
        f'<<<BEGIN_TOOL_DETAIL name="{name}">>>\n{desc}\n<<<END_TOOL_DETAIL>>>'
        for name, desc in tool_details
    )
    framing = (
        "[The full descriptions below are repeated for the tools most often "
        "called with invalid arguments. Consult them for the exact set of "
        "valid argument values (e.g. which agent types are valid for `task`, "
        "which skills exist for `skill`). The authoritative copy lives in the "
        "<<<BEGIN_AGENT_TOOLS>>> section.]"
    )
    return f"\n\n{framing}\n{blocks}"


def build_cooperative_prompt_hybrid_reminder(content, tool_signatures, tool_details=None):
    """In hybrid mode the full tool definitions sit at the stable prefix
    (the system message; with _CHANGE_SYSTEM_TO_USER on, the user-role
    message at index 0). This reminder is prepended to the last user
    message — recency slot — so the model is anchored to the JSON envelope
    format AND given each tool's parameter signature. The signatures are
    the recency anchor for the keys the model most often gets wrong
    (e.g. opencode's `bash` requires `description`; `read` takes `filePath`
    not `filename`). The "do not invent" sentence closes a common failure
    mode where the model emits a JSON call and then narrates an imagined
    output in the same turn. The "default to the tools above" sentence
    nudges the model to reach for a dedicated tool rather than improvising
    by hand (e.g. `webfetch` over a curl/Python script, `todowrite`/
    `todoread` over a hand-written todo file).

    `tool_signatures` is a list of `(name, required_keys, optional_keys)`
    triples produced by `_extract_tool_signatures`.

    `tool_details` is an optional list of `(name, description)` pairs from
    `_extract_tool_details` — the "detail tools" whose full description is
    echoed verbatim after the reminder in <<<BEGIN_TOOL_DETAIL>>> blocks. The
    signature line carries only parameter *keys*; a `task`'s valid agent types
    and a `skill`'s valid names are a closed set of *values* that opencode
    documents only in the description prose, so the whole description is what
    must reach recency for those tools.

    The reminder leads with a pointer back to the `<<<BEGIN_AGENT_TOOLS>>>`
    section (the stable prefix where the full tool definitions live) so that
    when attention to messages[0] dilutes on long conversations the model
    still has a precise, named target to retrieve when it needs full tool
    descriptions or parameter-value constraints.
    """
    if tool_signatures:
        rendered = ", ".join(
            _format_tool_signature(name, req, opt)
            for name, req, opt in tool_signatures
        )
        tools_clause = f" Available tools: {rendered}. Use parameter keys exactly as listed."
    else:
        tools_clause = ""
    reminder = (
        "[Reminder: The tools listed in the <<<BEGIN_AGENT_TOOLS>>> section "
        "at the start of this conversation are the only tools available — "
        "refer back to that section for full tool descriptions, parameter "
        "details, and any constraints on parameter values (e.g. which agent "
        "types are valid for a `task` tool, or which skills are listed for a "
        "`skill` tool). To use a tool, emit a ```json block with "
        "{\"name\": ..., \"arguments\": ...}. You may explain your "
        "reasoning before or after the JSON block. After emitting a tool "
        "call, do not invent or narrate the tool's result — the real "
        "result will be provided in the next turn. If no tool is needed, "
        "answer normally without any JSON. Default to the tools above for any "
        "task they cover instead of doing the work by hand — e.g. use "
        "`webfetch` to retrieve a URL instead of curl or a Python script, and "
        "use `todowrite`/`todoread` to manage your task list instead of "
        "writing a todo file."
        f"{tools_clause}]"
    )
    detail_blocks = _format_tool_detail_blocks(tool_details)
    return f"{reminder}{detail_blocks}\n\n{content}"


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

    Real upstream LLMs (Gemini Enterprise, etc.)
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
            # The ``` we found may actually be the opener of the next ```json
            # block (model emits `}\n```json\n{...` with no real closing
            # fence between consecutive tool calls). Don't consume those three
            # backticks — leave them for the next iteration to recognize as
            # the next ```json opener.
            if response_text[closing_fence_pos:closing_fence_pos + 7] == '```json':
                block_end = closing_fence_pos
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

_TOOL_NAME_PATTERN = re.compile(r"^Tool Name: `([^`]+)`", re.MULTILINE)


def _split_schema_params(parameters: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Given a JSON-Schema `parameters` dict, return
    (required_keys, optional_keys). Required keys keep their declared
    order from the schema's `required` list; optional keys are the
    `properties` keys not in `required`, in declared order.
    """
    if not isinstance(parameters, dict):
        return [], []
    props = parameters.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    required_raw = parameters.get("required") or []
    if not isinstance(required_raw, list):
        required_raw = []
    required = [k for k in required_raw if isinstance(k, str)]
    required_set = set(required)
    optional = [k for k in props.keys() if k not in required_set]
    return required, optional


def _extract_tool_signatures(
    tools: Optional[List[Dict[str, Any]]],
    tools_text: str,
) -> List[Tuple[str, List[str], List[str]]]:
    """Return per-tool `(name, required_keys, optional_keys)` triples for
    the hybrid reminder.

    Primary path: the raw `tools` array (production call site) where each
    tool's JSON-Schema `parameters` is a structured field.

    Fallback path: parse the schema blocks that `format_tools_to_text`
    embedded inside `tools_text` — split the text on `Tool Name:`
    boundaries, then read each block's ```json ... ``` payload as JSON
    Schema. If a block has no schema (e.g. tests that hand in
    `"Tool Name: \\`Foo\\`"` as bare text), the tool surfaces as
    `(name, [], [])` and renders as a bare `name` in the reminder.
    """
    if tools:
        sigs: List[Tuple[str, List[str], List[str]]] = []
        for tool in tools:
            func = tool.get("function", {}) if "function" in tool else tool
            name = func.get("name")
            if not name:
                continue
            req, opt = _split_schema_params(func.get("parameters") or {})
            sigs.append((name, req, opt))
        return sigs

    sigs = []
    chunks = re.split(r"(?=^Tool Name: `)", tools_text, flags=re.MULTILINE)
    for chunk in chunks:
        m = re.match(r"Tool Name: `([^`]+)`", chunk)
        if not m:
            continue
        name = m.group(1)
        schema_match = re.search(r"```json\s*(.*?)\s*```", chunk, re.DOTALL)
        if schema_match:
            try:
                parameters = json.loads(schema_match.group(1))
            except (ValueError, TypeError):
                parameters = {}
            req, opt = _split_schema_params(parameters)
            sigs.append((name, req, opt))
        else:
            sigs.append((name, [], []))
    return sigs


def _pare_task_description(description: str) -> str:
    """Pare opencode's `task` tool description down to its agent-list section.

    opencode assembles the description as `<static boilerplate>` followed by the
    dynamic agent list, the latter introduced by `_OPENCODE_TASK_AGENTS_HEADER`.
    The boilerplate carries no closed-set values and is already present verbatim
    at the stable prefix, so echoing it again in the recency TOOL_DETAIL block is
    pure dilution. Keep only the header line onward — the agent names and their
    one-line descriptions, the closed set models most often guess wrong.

    Fallback: if the header isn't found (a future opencode reformats the
    description) return it unchanged. Degrading to "more tokens" is safe;
    silently dropping the agent list would not be.
    """
    match = _OPENCODE_TASK_AGENTS_RE.search(description)
    if match is None:
        return description
    return match.group(0)


def _extract_tool_details(
    tools: Optional[List[Dict[str, Any]]],
    flagged: List[str],
) -> List[Tuple[str, str]]:
    """Return `(name, description)` pairs for each name in `flagged` that is
    present in `tools`, preserving the order of `flagged`. Tools with an
    empty/whitespace-only description are skipped (an empty TOOL_DETAIL block
    would be noise). Used by hybrid mode to echo a small set of "detail
    tools" descriptions into the recency reminder — see
    `_format_tool_detail_blocks`.

    Source is the raw `tools` array's `description` field. For every tool except
    `task` it is taken whole — no prose parsing. `task`'s description is pared by
    `_pare_task_description` to just its agent-list section (the static
    boilerplate is redundant at recency); see that helper. Unlike
    `_extract_tool_signatures` there is no `tools_text` fallback: a tool's
    JSON-Schema params reserialize losslessly into `tools_text`, but its
    free-form, multi-line description does not, and the production call site
    always supplies `tools`. Without `tools` (a test convenience path) no detail
    blocks are surfaced.
    """
    if not tools or not flagged:
        return []
    by_name: Dict[str, str] = {}
    for tool in tools:
        func = tool.get("function", {}) if "function" in tool else tool
        name = func.get("name")
        if not name:
            continue
        by_name[name] = func.get("description") or ""
    details: List[Tuple[str, str]] = []
    for name in flagged:
        desc = by_name.get(name)
        if not (desc and desc.strip()):
            continue
        if name == "task":
            desc = _pare_task_description(desc)
        details.append((name, desc))
    return details


def _flatten_content_to_str(content):
    """Flatten a message's `content` to a plain string. Some clients send
    content as a list of content-blocks (each a dict carrying a `text` field,
    or a bare string); their text is joined with blank-line separators. A
    string passes through unchanged; any other type is coerced via str().
    Shared by the sys→user post-pass and the hybrid AGENT_INSTRUCTIONS /
    USER_MESSAGE wraps so all three handle list-valued content identically.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n\n".join(parts)
    return str(content)


def translate_history_and_apply_prompt(
    original_messages: List[Dict[str, Any]],
    tools_text: str,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """
    Translate ollama-format messages into a flat conversation suitable for the
    upstream API. Tool calls become markdown JSON blocks embedded in assistant
    content; tool results (role:"tool" messages) are wrapped verbatim in
    <<<BEGIN_TOOL_RESULT>>> / <<<END_TOOL_RESULT>>> markers and folded into a
    user message. The tool name on each result is resolved from metadata — an
    explicit name field, else the tool_call_id correlated against the
    originating assistant tool_calls, else positional order. The
    cooperative-prompt wrapper is applied to the final user message if tools
    are available.

    Special-case 'passthrough' mode: emit `original_messages` verbatim (only
    shallow-copied so the caller can't mutate proxy state through the returned
    list). No history translation, no cooperative-prompt scaffolding, no
    system→user rewrite. This is a benchmark control — see the catch_all
    handler for the matching `tools` passthrough that completes the bypass.
    """
    if not original_messages:
        return []

    if _PROMPT_MODE == "passthrough":
        # Shallow copy each dict so a downstream mutation can't poison the
        # caller's view of the upstream payload. We intentionally don't
        # validate or coerce any fields — the whole point is "do nothing".
        return [dict(m) for m in original_messages]

    messages: List[Dict[str, str]] = []

    # Tool-call name lookup. A role:"tool" result must be labeled with the
    # name of the tool it answers, but not every agent puts that name on the
    # tool message: opencode and ollama send `tool_name`/`name` directly,
    # while some agents send only a `tool_call_id`. So we record names as
    # assistant tool_calls go by — keyed by id for exact correlation, plus an
    # ordered list as a positional fallback when no id is present anywhere.
    tool_names_by_id: Dict[str, str] = {}
    pending_tool_names: List[str] = []

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
                    # Record the call name so the matching tool result can be
                    # labeled even if it carries no name field of its own.
                    tc_id = tc.get("id")
                    if tc_id:
                        tool_names_by_id[tc_id] = name
                    pending_tool_names.append(name)
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
            # Wrap the tool result in explicit open/close markers. The content
            # is taken VERBATIM — never parsed — because agents present tool
            # output differently and harness must stay agnostic to all of
            # them. The name comes from message metadata, never from
            # inspecting the content: an explicit `tool_name`/`name` field if
            # present (opencode, ollama), else the `tool_call_id` correlated
            # against the originating assistant tool_calls, else positional
            # order, else "unknown_tool".
            tool_name = msg.get("tool_name") or msg.get("name")
            tc_id = msg.get("tool_call_id") or msg.get("id")
            # Pop one positional candidate per tool message so the fallback
            # queue stays in lockstep with the tool-result stream regardless
            # of how the other results were resolved.
            positional = pending_tool_names.pop(0) if pending_tool_names else None
            if not tool_name and tc_id:
                tool_name = tool_names_by_id.get(tc_id)
            if not tool_name:
                tool_name = positional or "unknown_tool"
            observation = (
                f'<<<BEGIN_TOOL_RESULT name="{tool_name}">>>\n'
                f"{content}\n"
                f"<<<END_TOOL_RESULT>>>"
            )
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{observation}"
            else:
                messages.append({"role": "user", "content": observation})

    # Mode-based cooperative-prompt injection. Default mode is 'user_front'.
    #   user_front — request first, then tool definitions, on the last
    #                user message. Established baseline.
    #   hybrid     — full tool definitions appended to the system message
    #                (which the _CHANGE_SYSTEM_TO_USER post-pass then folds
    #                into a user-role message at index 0); a short reminder
    #                restating the JSON envelope, the no-fabricated-results
    #                rule, and the list of available tool names is prepended
    #                to the last user message. Hybrid additionally wraps three
    #                content categories in distinctive delimiters so the model
    #                (and the reminder) can address them by name and not
    #                conflate them with the upstream gateway's own system
    #                content: the inbound agent system prompt in
    #                <<<BEGIN_AGENT_INSTRUCTIONS>>>, the harness tool block in
    #                <<<BEGIN_AGENT_TOOLS>>> (inside system_addition), and every
    #                real user turn in <<<BEGIN_USER_MESSAGE>>>. Tool-result
    #                turns keep only their <<<BEGIN_TOOL_RESULT>>> markers.
    if tools_text and messages:
        if _PROMPT_MODE == "user_front":
            if messages[-1]["role"] == "user":
                original_last_role = original_messages[-1].get("role")
                final_content = messages[-1]["content"]
                if original_last_role == "tool":
                    messages[-1]["content"] = build_cooperative_prompt_tool_front(final_content, tools_text)
                else:
                    messages[-1]["content"] = build_cooperative_prompt_user_front(final_content, tools_text)
        elif _PROMPT_MODE == "hybrid":
            # Hybrid: full tool definitions go on the system message (stable
            # prefix). Tool-result-converted role:"tool" entries already became
            # user messages wrapped in <<<BEGIN_TOOL_RESULT>>> markers above;
            # we don't tack the cooperative prompt onto those — it lives in
            # the system message instead.
            system_addition = build_cooperative_prompt_system_addition(tools_text)
            if messages[0]["role"] == "system":
                # Wrap the inbound agent (opencode) system prompt
                # in AGENT_INSTRUCTIONS markers BEFORE appending harness's own
                # tool block, so the two are individually addressable and the
                # model can't conflate either with the upstream gateway's
                # system content. Empty/whitespace-only inbound content gets no
                # wrap — emitting empty markers would just add noise. Trailing
                # newline + the "\n\n" that system_addition leads with yields a
                # one-blank-line gap before "### Tool Usage Instructions".
                existing = _flatten_content_to_str(messages[0]["content"])
                if existing.strip():
                    messages[0]["content"] = (
                        f"<<<BEGIN_AGENT_INSTRUCTIONS>>>\n"
                        f"{existing}\n"
                        f"<<<END_AGENT_INSTRUCTIONS>>>\n"
                        f"{system_addition}"
                    )
                else:
                    messages[0]["content"] = existing + system_addition
            else:
                # No system message present — insert one. Strip the leading
                # blank line that the addition starts with so the system
                # content doesn't begin with whitespace. No AGENT_INSTRUCTIONS
                # wrap: there was no inbound agent prompt to delimit.
                messages.insert(0, {"role": "system", "content": system_addition.strip()})

            # Wrap every real user-role message in USER_MESSAGE markers.
            # "Real" = original role was `user`, not a tool-result that got
            # role-converted; the latter already carry <<<BEGIN_TOOL_RESULT
            # name="…">>> markers from the universal pre-dispatch wrap, so we
            # detect and skip them by that marker prefix. The system message at
            # index 0 is still role "system" here (sys→user runs later), so the
            # role filter leaves it alone. This runs BEFORE the recency reminder
            # so the reminder lands OUTSIDE the wrap — it's proxy stage
            # direction, not part of what the user wrote.
            for m in messages:
                if m["role"] != "user":
                    continue
                flattened = _flatten_content_to_str(m["content"])
                if "<<<BEGIN_TOOL_RESULT" in flattened:
                    continue
                m["content"] = (
                    f"<<<BEGIN_USER_MESSAGE>>>\n"
                    f"{flattened}\n"
                    f"<<<END_USER_MESSAGE>>>"
                )

            # Recency reminder on the last user-role message (real user
            # turn or tool-result-converted-to-user). Same scope as the
            # original hybrid reminder; prepended after the USER_MESSAGE wrap
            # so it sits outside it.
            if messages[-1]["role"] == "user":
                signatures = _extract_tool_signatures(tools, tools_text)
                details = _extract_tool_details(tools, _HYBRID_DETAIL_TOOLS)
                messages[-1]["content"] = build_cooperative_prompt_hybrid_reminder(
                    messages[-1]["content"],
                    signatures,
                    details,
                )

    # Convert system role to user role if configured. Some upstream APIs
    # silently drop the system role; this rewrites system content as a
    # user message at the head of the conversation, with a stub assistant
    # turn between it and the actual first user message to satisfy
    # strict role-alternation requirements.
    #
    # Multiple system messages are already coalesced upstream into a
    # single system message (see the `if role == "system"` branch above).
    # By the time we reach here, there is at most ONE system message and
    # it is at index 0 (if present at all).
    if _CHANGE_SYSTEM_TO_USER and messages and messages[0]["role"] == "system":
        # Some clients emit content as a list of content-blocks; flatten
        # to a single string for the user-role rewrite.
        system_content = _flatten_content_to_str(messages[0]["content"])
        if system_content.strip():
            # Replace the system message with a user message containing
            # its content. Insert a stub assistant message after it so
            # the next user message (the actual first user turn from the
            # original conversation) doesn't violate strict alternation.
            messages[0] = {"role": "user", "content": system_content}
            if len(messages) > 1 and messages[1]["role"] == "user":
                messages.insert(1, {
                    "role": "assistant",
                    "content": "I understand the instructions above.",
                })
        else:
            # System message was empty/whitespace-only after normalization.
            # Drop it entirely rather than producing an empty user message.
            messages.pop(0)

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
    order. Each call gets a unique toolu_-prefixed id so tool_use blocks in
    the conversation history can be correlated to their results.
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

        model_name = ollama_request.get("model") or "GenAI"
        original_messages = ollama_request.get("messages") or []
        tools = ollama_request.get("tools") or []

        print(f"[{req_id}] {request.method} /{path} model={model_name} messages={len(original_messages)} tools={len(tools)}", flush=True)

        tools_text = format_tools_to_text(tools)
        translated = translate_history_and_apply_prompt(original_messages, tools_text, tools=tools)

        upstream_payload = {
            "model": PROXY_API_MODEL,
            "messages": translated,
        }
        # Passthrough mode forwards the agent's tool definitions to upstream
        # as-is so the conversation actually contains tool schemas (rather
        # than the proxy's cooperative-prompt markdown injection). The
        # benchmark control's value is measuring what harness contributes
        # by stripping the mediation; the schemas the agent provides match
        # the ollama tool format, which most non-ollama upstreams won't
        # honor — that mismatch IS the data point.
        if _PROMPT_MODE == "passthrough" and tools:
            upstream_payload["tools"] = tools
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

        # Compute prompt_tokens from the translated conversation directly.
        # Upstream's `prompt_tokens` is unreliable for context tracking against
        # this provider — observed behavior (count not growing monotonically with
        # conversation length, occasionally shrinking) suggests server-side
        # sliding-window truncation. The agent needs a count that reflects the
        # full conversation it sent, not what the upstream charged for. Always
        # estimate locally from the full translated array so the agent's context
        # bar grows monotonically with conversation length.
        joined = "\n".join(m.get("content", "") for m in translated)
        upstream_usage = target_json.get("usage") or {}
        usage = {
            "prompt_tokens": _estimate_tokens(joined),
            "completion_tokens": (
                upstream_usage.get("completion_tokens")
                or _estimate_tokens(response_text)
            ),
        }

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
    _setup_change_system_to_user()
    _setup_hybrid_detail_tools()

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
        f"   sys→user:       {_CHANGE_SYSTEM_TO_USER}\n"
        f"   detail tools:   {', '.join(_HYBRID_DETAIL_TOOLS) or '(none)'}\n"
        f"   debug dumps:    {output_status}\n"
        "============================================================",
        flush=True,
    )

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)


if __name__ == "__main__":
    main()
