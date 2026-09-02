"""harness translating proxy.

Exposes an OpenAI-compatible Chat Completions endpoint
(`POST /v1/chat/completions`) — the interface opencode speaks via the
`@ai-sdk/openai-compatible` provider — and translates to/from the upstream
API's chat-completions format. Responses are emitted as Server-Sent Events
(`text/event-stream`, `data: {chunk}\\n\\n` ... `data: [DONE]`) when the
request sets `stream: true`, or a single `chat.completion` JSON object
otherwise.

Injects cooperative tool-use prompts so models that don't natively support
tool calls can produce them as ```json blocks that the proxy then parses and
re-emits as native tool_calls.

Environment variables (see README / .env.example):
    PROXY_HOST           bind address (default 0.0.0.0)
    PROXY_PORT           bind port (default 8000)
    HARNESS_FORCE_LOOPBACK  when truthy, refuse to bind a non-loopback
                         PROXY_HOST (set by `harness host`; defense-in-depth
                         for the firewall-less host mode)
    PROXY_API_URL        upstream base URL (REQUIRED). The proxy derives the
                         chat endpoint ({base}/v1/chat/completions) and the
                         models endpoint ({base}/v1/models) from it; a trailing
                         /v1/chat/completions, /chat/completions, or /v1 is
                         stripped first so either a base or a full chat URL works.
    PROXY_API_KEY        upstream bearer token (REQUIRED)
    DEFAULT_MODEL_NAME   fallback upstream model id (REQUIRED). The proxy
                         forwards whatever model the request asked for and only
                         falls back to this when the request omits a model.
    MODEL_CONTEXT_LENGTH context-window cap for the local token estimate
                         (default 200000; legacy alias OLLAMA_CONTEXT_LENGTH).
    PROXY_BACKEND        which upstream dialect to speak: "openai" (default)
                         or "chatgpt". NOT a .env key -- `harness chatgpt`
                         injects it for one launch. With "chatgpt" the
                         PROXY_API_* / DEFAULT_MODEL_NAME trio is unused and
                         the three CHATGPT_* vars below are REQUIRED instead.
    CHATGPT_BASE_URL     ChatGPT backend-api base URL (REQUIRED for chatgpt).
                         The stream path, timezone and user agent are
                         hardcoded; only this base is configurable.
    CHATGPT_MODEL_NAME   model id the chatgpt backend serves (REQUIRED for
                         chatgpt). Also the whole synthesized /v1/models list.
    CHATGPT_COOKIE_STRING  session cookie the chatgpt backend authenticates
                         with (REQUIRED for chatgpt; secret).
    OUTPUT_DIR           debug-dump directory (optional)
    PROXY_TIMEOUT        upstream request timeout, seconds (default 180)
"""

import datetime
import json
import os
import re
import sys
import traceback
import urllib.parse
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, Response, request

# verify=False is required because the upstream uses a self-signed cert.
# Suppress the noisy InsecureRequestWarning at module load.
#
# Security boundary: every upstream call below uses verify=False, so this assumes
# the network path to PROXY_API_URL is trusted. In container mode the egress
# firewall allowlists the upstream host, constraining that hop; in host mode
# there is no firewall, so a network-level MITM on the route to PROXY_API_URL
# could intercept or alter upstream responses. Cert pinning would require
# shipping the upstream CA as a runtime artifact and is not done today.
#
# This covers CHATGPT_BASE_URL too, and there the stake is higher: that request
# carries a full browser session cookie, not an API grant, so a MITM on the
# route takes the account rather than a key. It is deliberate — the reference
# client this backend was ported from disables verification because the base
# URL points at a private mirror with its own cert, and turning verification on
# would break the configuration that is known to work. Point CHATGPT_BASE_URL
# only at a host whose route you trust.
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
DEFAULT_MODEL_NAME: str = os.environ.get("DEFAULT_MODEL_NAME", "").strip()
PROXY_TIMEOUT: int = int(os.environ.get("PROXY_TIMEOUT", "180"))
# Context-window cap used to bound the local token estimate. Reads the new
# MODEL_CONTEXT_LENGTH env var, falling back to the legacy OLLAMA_CONTEXT_LENGTH
# name so existing .env files keep working across the ollama removal.
MODEL_CONTEXT_LENGTH: int = int(
    os.environ.get("MODEL_CONTEXT_LENGTH")
    or os.environ.get("OLLAMA_CONTEXT_LENGTH")
    or "200000"
)


def _normalize_api_base(url: str) -> str:
    """Reduce PROXY_API_URL to a scheme://host[/prefix] base.

    The user sets PROXY_API_URL to a base; the proxy appends the standard
    OpenAI-style paths (/v1/chat/completions, /v1/models). To stay forgiving
    of the older "full chat URL" form and of the OpenAI base-includes-/v1
    convention, strip a trailing /v1/chat/completions, /chat/completions, or
    /v1 (plus surrounding slashes) before re-deriving the endpoints. The
    harness CLI mirrors this normalization in bash (see _api_base in
    `harness`); keep the two in sync.
    """
    base = url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base.rstrip("/")


_API_BASE: str = _normalize_api_base(PROXY_API_URL)
CHAT_URL: str = f"{_API_BASE}/v1/chat/completions"
MODELS_URL: str = f"{_API_BASE}/v1/models"


# ---------------------------------------------------------------------------
# Upstream backend selection
# ---------------------------------------------------------------------------
#
# The proxy speaks ONE upstream dialect at a time, chosen by PROXY_BACKEND:
#
#   openai   (default) the OpenAI-compatible contract in
#                      architecture/upstream-api.md: bearer key, POST
#                      {base}/v1/chat/completions, GET {base}/v1/models.
#   chatgpt            the ChatGPT web backend-api: cookie auth, one SSE
#                      stream endpoint, no model catalog.
#
# PROXY_BACKEND is NOT a .env key. `harness chatgpt` injects it for a single
# launch (container mode via the generated compose runtime override, host mode
# via the proxy's launch env); only the three CHATGPT_* values below live in
# .env. That keeps the default install byte-identical to before.
#
# The client-facing surface is identical either way. proxy.py never streams
# FROM upstream -- it materializes the whole upstream response, then translates
# -- so the chatgpt branch only has to hand `catch_all` an OpenAI-shaped dict.
# Everything downstream (error triage, tool-call extraction, the malformed
# tool-call retry, the meta-tool loop, the empty-response rescue, both
# emitters) is dialect-agnostic and untouched.
PROXY_BACKEND: str = os.environ.get("PROXY_BACKEND", "").strip().lower() or "openai"

CHATGPT_BASE_URL: str = os.environ.get("CHATGPT_BASE_URL", "").strip().rstrip("/")
CHATGPT_MODEL_NAME: str = os.environ.get("CHATGPT_MODEL_NAME", "").strip()
CHATGPT_COOKIE_STRING: str = os.environ.get("CHATGPT_COOKIE_STRING", "").strip()

# Hardcoded on purpose: properties of the backend-api dialect, not user
# configuration. Values match the reference client this port was derived from.
CHATGPT_STREAM_ENDPOINT: str = "/backend-api/conversation/stream"
CHATGPT_TIMEZONE: str = "America/Chicago"
CHATGPT_TIMEZONE_OFFSET_MIN: int = 300
CHATGPT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
CHATGPT_STREAM_URL: str = f"{CHATGPT_BASE_URL}{CHATGPT_STREAM_ENDPOINT}"

# DEFAULT_MODEL_NAME is the openai-backend knob and is neither required nor
# set on a chatgpt-only install; CHATGPT_MODEL_NAME stands in for it so the
# request-omits-a-model fallback and the /v1/models catalog agree.
if PROXY_BACKEND == "chatgpt" and CHATGPT_MODEL_NAME:
    DEFAULT_MODEL_NAME = CHATGPT_MODEL_NAME


class _SyntheticResponse:
    """Minimal `requests.Response` stand-in for a non-OpenAI backend.

    Every upstream POST site consumes exactly `.status_code`, `.text` and
    `.json()`. Handing them one of these lets a backend with a different wire
    format reuse the entire downstream pipeline unchanged, so adding a dialect
    costs one function instead of a second copy of `catch_all`.
    """

    def __init__(self, status_code: int, body: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode("utf-8")
        self.ok = status_code < 400
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}

    def json(self) -> Dict[str, Any]:
        return self._body

    def close(self) -> None:  # parity with requests.Response
        return None


_CHATGPT_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
}


def _chatgpt_flatten_messages(messages: List[Dict[str, Any]]) -> str:
    """Render the translated OpenAI history as the single user turn the
    backend-api takes.

    The backend-api keeps conversation state server-side (conversation_id /
    parent_message_id) and expects only the newest turn. This proxy is
    stateless and re-sends the full history on every request, so reusing that
    state would duplicate the transcript. Starting a fresh conversation per
    request and carrying the whole history in one user message is exactly the
    shape the reference client verified; a multi-message array is not, and if
    the endpoint honored only its last entry the history loss would be silent.
    A single-message history is passed through verbatim. Note this is the
    bare-`curl` case only: opencode's first turn already translates to several
    messages, so the labelled form is what the agent actually sends.
    """
    parts: List[Tuple[str, str]] = []
    for m in messages:
        text = _flatten_content_to_str(m.get("content", "") or "")
        if not text.strip():
            continue
        role = str(m.get("role", "user")).strip().lower()
        parts.append((_CHATGPT_ROLE_LABELS.get(role, role.title() or "User"), text))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][1]
    return "\n\n".join(f"{label}: {text}" for label, text in parts)


def _chatgpt_origin() -> str:
    """Origin/Referer value for the backend-api call.

    An Origin is scheme://host[:port]; CHATGPT_BASE_URL may legitimately carry
    a path prefix, which is not valid there. Computed per call rather than at
    import so the value tracks CHATGPT_BASE_URL.
    """
    if not CHATGPT_BASE_URL:
        return ""
    split = urllib.parse.urlsplit(CHATGPT_BASE_URL)
    if not split.scheme or not split.netloc:
        return CHATGPT_BASE_URL
    return f"{split.scheme}://{split.netloc}"


def _chatgpt_collect_stream(resp: Any) -> Tuple[str, int, str]:
    """Consume the backend-api SSE stream.

    Returns `(text, events, prefix)`: the assistant text, how many `data:`
    events were seen, and the first bytes of the raw body. The event count is
    what separates "the model answered with nothing" from "this 200 was not an
    event stream at all" (a login page, an HTML error, a redirect landing) —
    without it both look like an empty completion, and an empty completion
    sends the agent into the empty-response rescue on every turn forever.

    Two delta shapes are emitted and both are handled, mirroring the reference
    client: incremental `{"type": "message_delta", "delta": "..."}` events, and
    `message.content.parts` snapshots that are CUMULATIVE (each event repeats
    everything so far), from which only the newly added suffix is taken.
    Snapshots of anything that is not the assistant's text answer (reasoning /
    `thoughts`, tool or system messages) are skipped: the cumulative rule is
    "longest wins", so one long non-answer message would otherwise replace the
    answer outright. A message that states no role or content_type is kept —
    the reference client filtered on neither, and dropping unlabelled events
    would break the shape it verified.
    """
    text = ""
    events = 0
    prefix = ""
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if len(prefix) < 500:
            prefix += line[:500] + "\n"
        if not line.startswith("data:"):
            continue
        events += 1
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "message_delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                text += delta
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if isinstance(author, dict):
            role = author.get("role")
            if isinstance(role, str) and role != "assistant":
                continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        ctype = content.get("content_type")
        if isinstance(ctype, str) and ctype != "text":
            continue
        chunk_parts = content.get("parts") or []
        if not isinstance(chunk_parts, list):
            continue
        snapshot = "".join(p for p in chunk_parts if isinstance(p, str))
        if len(snapshot) > len(text):
            text = snapshot
    return text, events, prefix


def _chatgpt_post(payload: Dict[str, Any]) -> _SyntheticResponse:
    """Run one chat turn against the ChatGPT backend-api and return it in the
    OpenAI chat-completion shape the rest of the proxy expects."""
    model = CHATGPT_MODEL_NAME or payload.get("model") or ""
    body = {
        "action": "next",
        "model": model,
        "timezone_offset_min": CHATGPT_TIMEZONE_OFFSET_MIN,
        "timezone": CHATGPT_TIMEZONE,
        "messages": [
            {
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": [_chatgpt_flatten_messages(payload.get("messages") or [])],
                },
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": CHATGPT_USER_AGENT,
        "Origin": _chatgpt_origin(),
        "Referer": f"{_chatgpt_origin()}/",
        "Cookie": CHATGPT_COOKIE_STRING,
    }
    # allow_redirects=False on purpose. requests drops the Cookie header on
    # every redirect hop and turns a 302 POST into a GET, so a followed
    # redirect arrives unauthenticated and comes back 200 with a login page —
    # indistinguishable, downstream, from the model answering with nothing.
    # Fail on the 3xx instead and say so.
    resp = requests.post(
        CHATGPT_STREAM_URL,
        headers=headers,
        json=body,
        verify=False,
        timeout=PROXY_TIMEOUT,
        stream=True,
        allow_redirects=False,
    )
    try:
        if resp.status_code >= 300:
            # Surface the upstream body so a stale cookie (the expected
            # failure) reaches the caller's error path instead of a bare code.
            try:
                detail = resp.text[:2000]
            except Exception:
                detail = ""
            if resp.status_code < 400:
                detail = (f"chatgpt backend redirected ({resp.status_code}) to "
                          f"{resp.headers.get('Location', '?')} — the session cookie is "
                          f"probably expired. {detail}")
            return _SyntheticResponse(
                resp.status_code if resp.status_code >= 400 else 502,
                {"error": {"message": detail or f"chatgpt backend returned {resp.status_code}",
                           "type": "chatgpt_backend_error"}},
            )
        text, events, prefix = _chatgpt_collect_stream(resp)
    finally:
        resp.close()
    if events == 0:
        return _SyntheticResponse(
            502,
            {"error": {"message": "chatgpt backend returned 200 but no SSE events; the "
                                  "session cookie is probably expired. First bytes: "
                                  + prefix[:500],
                       "type": "chatgpt_backend_error"}},
        )
    return _SyntheticResponse(
        200,
        {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(datetime.datetime.now().timestamp()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        },
    )


def _upstream_post(headers: Dict[str, str], payload: Dict[str, Any]) -> Any:
    """The single outbound chat call, routed to the configured backend.

    Returns a `requests.Response` for the openai backend and a
    `_SyntheticResponse` for chatgpt; callers only touch `.status_code`,
    `.text` and `.json()`, which both provide.
    """
    if PROXY_BACKEND == "chatgpt":
        return _chatgpt_post(payload)
    return requests.post(
        CHAT_URL,
        headers=headers,
        json=payload,
        verify=False,
        timeout=PROXY_TIMEOUT,
    )


_OUTPUT_DIR: Optional[str] = None  # set in main() before serving

# Cooperative-prompt injection mode. Two cooperative modes plus one bypass
# are accepted. This is no longer a user-facing .env knob: it defaults to
# "hybrid" and is overridden only via `harness start/restart --prompt-mode
# <mode>`, which injects PROXY_PROMPT_MODE into the proxy container env for
# that launch (ephemeral). Keeping the var honored is what keeps all three
# modes reachable for benchmarking and power use.
#   "hybrid"     — DEFAULT. Full tool definitions sit at the stable prefix
#                  (appended to the system message; with the default
#                  _CHANGE_SYSTEM_TO_USER post-pass this becomes the
#                  user-role message at index 0). A short reminder
#                  restating the JSON envelope format, the "don't invent
#                  tool results" rule, and the per-tool parameter
#                  signatures (`name(required, [optional])`) is prepended
#                  to the last user message. Tools at prefix + signature
#                  reminder at recency.
#   "user_front" — Full scaffolding (tool list + tool-call format
#                  instructions) on the last user message, with the user's
#                  request placed BEFORE the tool list rather than after
#                  it. Puts the request in primacy position; the tool list
#                  follows at recency. Established baseline; reachable as a
#                  benchmark/power override.
#   "passthrough" — Benchmark control. Skips every harness-side mediation:
#                  no cooperative-prompt injection, no system→user
#                  rewrite, no history translation. Forwards tools to
#                  upstream verbatim. Not a cooperative mode; used to
#                  measure what harness's mediation contributes.
_PROMPT_MODE: str = "hybrid"  # set in main() before serving

# Hybrid mode only. Per-tool recency guidance for MCP tools, loaded at startup
# from the HARNESS_MCP_TOOL_RECENCY env var (a JSON object keyed `<server>_<tool>`
# -> one-line description). The harness CLI builds this by merging every enabled
# MCP's `recency.json` (see architecture/mcp.md "Tool recency descriptions"), so
# MCP tool guidance is DATA owned per-MCP, not code: the bundled serena reference
# MCP ships `mcp-registry/serena/recency.json`, a custom host MCP's setup agent
# writes one, and `harness mcp setup <name>` authors one for any other registry
# MCP. Consulted as a fallback to `_HYBRID_TOOL_GUIDANCE` (opencode's own tools
# stay code, the shipped harness<->opencode contract). Empty when no MCP is
# enabled or the var is unset/unparsable — a tool with no entry renders as a bare
# signature, exactly as before.
_MCP_TOOL_RECENCY: Dict[str, str] = {}  # set in main() from the env var

# Tools (runtime names, `<server>_<tool>`) an enabled MCP marked `state_check`
# in its recency.json — state-mutating tools the agent should orient before
# calling. Loaded once at startup from HARNESS_MCP_STATE_CHECK (a JSON array the
# harness CLI builds alongside HARNESS_MCP_TOOL_RECENCY). Drives the
# `[state-check]` marker on a tool's recency entry and the orient-first line in
# the reminder. Empty (the default) renders neither — graceful degradation.
_MCP_STATE_CHECK_TOOLS: set = set()

# Cooperative tool-search (the hand-built analog of native deferred-schema tool
# search, which is unavailable behind a non-first-party proxy). Default OFF:
# HARNESS_TOOL_SEARCH=1 advertises two synthetic meta-tools — `tool_search` and
# `tool_list` — that the PROXY serves from the current request's tool array
# (never forwarded to opencode), so the model can retrieve a tool's signature on
# demand. The full schemas still ship at the stable prefix (no migration); this
# is the mechanism the schema-migration decision is gated on once the catalog-
# size instrumentation shows the prefix is large enough to be worth thinning.
# See architecture/proxy.md "Cooperative tool-search".
_TOOL_SEARCH_ENABLED: bool = False
_META_TOOL_NAMES = ("tool_search", "tool_list")
# Max upstream round-trips the proxy will spend serving back-to-back meta-tool
# calls before giving up the loop (a runaway model that only ever searches).
_META_TOOL_SERVE_BUDGET = 3

# The confirmed upstream silently drops the `system` role, so its content
# must always be converted into a user message at the start of the
# conversation, with a stub assistant message between to satisfy strict
# role-alternation. This is a project-managed constant, not a user knob: the
# upstream takes no system prompt (see architecture/upstream-api.md), so the
# conversion always has to happen. The `if _CHANGE_SYSTEM_TO_USER:` guard and
# constant are kept (rather than inlined) to preserve the non-conversion code
# path for tests.
_CHANGE_SYSTEM_TO_USER: bool = True

# Hybrid mode only. The per-tool entries block — the legend, each tool's
# one-line guidance, and the list of "detail" tools whose full description is
# echoed — is DATA loaded from a file, not literals here, exactly like the
# reminder prose in reminder.md: edit the file, `harness restart`, done. The
# module globals below hold the shipped defaults' loaded values; see
# `_tool_guidance_path` / `_load_tool_guidance` for the file contract.
#
# Tool names whose FULL description is echoed verbatim under the tool's own
# entry. These are the tools whose valid argument *values* are an unguessable
# closed set that opencode documents only as prose inside the tool description
# — `task` (the valid `subagent_type` agent names) and `skill` (the valid skill
# names). The per-tool signature carries the parameter keys but not those
# values, so the whole description has to reach recency. The descriptions are
# large, so this list is worth keeping short.
_HYBRID_DETAIL_TOOLS: List[str] = []

# One-line guidance per tool — the failure mode the upstream description warns
# about that the model still misses with the full schema in context (e.g.
# `todowrite` being called repeatedly with the same item in progress, `edit`
# called without a prior `read`). The string is appended after the tool's
# signature on its recency entry — the "shortened description" that replaces
# the multi-KB schema for the agent's read-at-each-turn budget. Tools absent
# from this map render as bare `name(signature)` with no guidance, so deleting
# an entry is a supported edit and adding a custom MCP tool degrades
# gracefully — and equivalently, a tool with an entry here renders only when
# opencode actually passes it for the turn. That is why the shipped default
# covers the union of opencode tools we know about (including
# situational/optional ones like `websearch`, `lsp`, `apply_patch`,
# `repo_clone`/`repo_overview`, `question`, `plan-enter`/`plan-exit`) even
# though many turns won't ship most of them: the cost of a stale entry is zero
# (`get` returns `None`, the line never renders), and the cost of missing an
# entry the moment a tool does ship is a bare signature with no failure-mode
# hint.
#
# NOTE: MCP tool guidance is NOT here. opencode exposes an MCP server's tools
# as `<server>_<tool>`, and those one-liners are DATA owned per-MCP — they load
# at startup into `_MCP_TOOL_RECENCY` from the HARNESS_MCP_TOOL_RECENCY env var
# (the harness CLI merges each enabled MCP's `recency.json`). The bundled
# serena reference MCP ships `mcp-registry/serena/recency.json`;
# `_format_tool_entries` consults `_MCP_TOOL_RECENCY` as a fallback to this
# map. This map stays opencode's OWN tools, which is why it ships as one file
# with the harness rather than per-MCP. See architecture/mcp.md.
_HYBRID_TOOL_GUIDANCE: Dict[str, str] = {}

# The legend rendered above the per-tool entries, plus the two sentences
# appended to it conditionally: `_HYBRID_STATE_CHECK_NOTE` only when a tool in
# this turn's toolset carries a `[state-check]` marker, and
# `_HYBRID_TOOL_SEARCH_NOTE` only when cooperative tool-search is enabled.
# Loaded from the same file; each falls back on its own.
_HYBRID_LEGEND: str = ""
_HYBRID_STATE_CHECK_NOTE: str = ""
_HYBRID_TOOL_SEARCH_NOTE: str = ""

# opencode builds the `task` tool's description by appending a dynamic agent
# list onto a block of static boilerplate ("when to use Task", usage notes).
# That boilerplate carries no closed-set values and is already present verbatim
# at the stable prefix, so for `task` ONLY the recency description inlined
# under the tool's entry is pared to the agent-list section — everything from
# this header onward. The header is the seam in opencode's
# ToolRegistry.describeTask and has been byte-identical across releases
# (verified 1.14.41 and 1.15.7). If a future opencode renames it, the parse
# falls back to the full description — no closed-set values are ever dropped —
# and TestTaskDescriptionParing is the canary that flags the drift. `skill`'s
# description is short and left verbatim.
_OPENCODE_TASK_AGENTS_HEADER = (
    "Available agent types and the tools they have access to:"
)
_OPENCODE_TASK_AGENTS_RE = re.compile(
    "^" + re.escape(_OPENCODE_TASK_AGENTS_HEADER) + ".*",
    re.MULTILINE | re.DOTALL,
)

# Host OS family (linux/macos/windows), injected by the harness CLI via
# HARNESS_HOST_OS (from harness_detect_os). The proxy always runs in a Linux
# container; the host the user actually reproduces work on may differ. The
# recency reminder surfaces this so the agent gives reproducible setup advice
# (e.g. a project-local venv) instead of relying on the container's own
# environment. Empty/unrecognised (including "unknown") suppresses only the
# host-OS parenthetical — the rest of the Environment line is host-independent.
_HOST_OS: str = ""
_RUN_MODE: str = "container"

# Hybrid mode only. Both files the reminder is built from — its prose and the
# per-tool entries' data — live in FILES, not in string literals here, so they
# are editable without a code change: edit the file, `harness restart`, done.
#
# Two rungs, both resolving to the user's copy in a normal install:
#   1. $INSTALL_ROOT/<name> — the install root is where `harness start` seeds
#      both user copies, next to .env. Only `harness host` sets the variable
#      for the proxy: host mode runs proxy.py straight from the clone, so
#      there is no bind-mount to override anything. It wins outright when set,
#      rather than falling through on a missing file, so a copy that is
#      missing or shadowed by a directory degrades LOUDLY (`[!]`) instead of
#      quietly reading a different file than the one the user edits.
#   2. <name> next to proxy.py — /app/<name> in the container, which
#      docker-compose bind-mounts the user's copy over. The Dockerfile also
#      COPYs the tracked default there, so a container with no mount still
#      has a working file.
# Neither file gets a path variable of its own: both copies keep their tracked
# basenames under the install root, so INSTALL_ROOT (which harness exports for
# compose anyway) already says everything a per-file variable would. Rung 2 is
# resolved off `__file__`, never a hardcoded `proxy/`: the image flattens the
# repo into /app.
def _user_data_path(basename: str) -> str:
    install_root = os.environ.get("INSTALL_ROOT", "").strip()
    if install_root:
        return os.path.join(install_root, basename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), basename)


# Loaded once at startup like the recency map: the file is fixed for a launch.
def _reminder_template_path() -> str:
    return _user_data_path("reminder.md")

# Substituted into the template per turn. Deliberately `{{NAME}}` + str.replace
# rather than str.format/Template: the reminder prose is full of braces
# (`{"name": ..., "arguments": {...}}`) and backslashes, and a user-edited file
# must never be able to raise on a stray character. An unknown token is simply
# left alone.
_REMINDER_TOKEN_HOST_OS = "{{HOST_OS}}"
_REMINDER_TOKEN_CWD = "{{CWD}}"
_REMINDER_TOKEN_TOOL_ENTRIES = "{{TOOL_ENTRIES}}"
_REMINDER_TOKEN_ENVIRONMENT = "{{ENVIRONMENT}}"
_REMINDER_TOKEN_TODOS = "{{TODOS}}"

# Only a comment block at the very TOP of the file is stripped (that is where
# the file documents its own tokens). Anchored at \A so a `<!--` appearing in
# the prose is left alone.
_REMINDER_HEADER_RE = re.compile(r"\A\s*<!--.*?-->[ \t]*\n?", re.DOTALL)

# Emergency fallback, used only when reminder.md is missing or unreadable (a
# bad bind-mount path — docker silently mounts a directory there). Deliberately
# NOT a copy of the file: duplicating the prose would let the two drift. It
# carries only the mechanically load-bearing part, the tool-call envelope,
# without which tool calling breaks outright, plus the tool entries.
_REMINDER_FALLBACK = (
    "[Reminder — operating rules for this turn.\n"
    "- Operating: you act through opencode. Call a tool by emitting a "
    "COMPLETE ```json...``` block whose body is `{\"name\": \"<tool>\", "
    "\"arguments\": {...}}`. After a tool call, do not invent or narrate its "
    "result — the real result arrives next turn.\n"
    "- Honesty: never fabricate; no invented paths, signatures, or results."
    "{{TOOL_ENTRIES}}]"
)

# Set by _setup_reminder_template(). None means "not loaded yet"; the builder
# lazy-loads so importing proxy.py in tests works without calling main().
_REMINDER_TEMPLATE: Optional[str] = None


# Hybrid mode only. The per-tool entries block's data (legend, per-tool
# guidance, detail-tool list) lives in a FILE for the same reason the reminder
# prose does: it is wording, not logic, and an operator must be able to retune
# a tool's one-liner without a code change. Same two rungs, same directory —
# see `_user_data_path`.
def _tool_guidance_path() -> str:
    return _user_data_path("tool-guidance.json")


# Emergency fallbacks, used per-section when the file is missing, unreadable,
# not a JSON object, or that one key is missing/the wrong type. Deliberately
# NOT copies of the shipped file: duplicating the wording would let the two
# drift, and the whole point of the file is that it is the single place the
# wording lives. They carry only the mechanically load-bearing part — what the
# signature syntax means, and what the two conditional markers mean — because
# without those the entries are an unexplained list. There is deliberately NO
# fallback guidance map: with the file gone, tools render as bare signatures,
# which `_format_tool_entries` already handles as its normal
# unknown-tool path.
_HYBRID_LEGEND_FALLBACK = (
    "Tools — signature format: name(required, [optional]); bracketed = "
    "optional; names not listed are unavailable; parameter names must match "
    "exactly. Full schemas are at <<<BEGIN_AGENT_TOOLS>>>."
)
_HYBRID_STATE_CHECK_NOTE_FALLBACK = (
    " A tool marked [state-check] mutates state — orient with the server's "
    "read-only state tool before calling it."
)
_HYBRID_TOOL_SEARCH_NOTE_FALLBACK = (
    " You can also call tool_list() or tool_search({\"query\": \"...\"}) to "
    "fetch a tool's full signature on demand."
)
# The detail-tool list is structure, not wording, so unlike the guidance map it
# does get a built-in fallback: losing it would strand `task`'s valid agent
# names and `skill`'s valid skill names, which the signature alone cannot carry.
_HYBRID_DETAIL_TOOLS_FALLBACK: List[str] = ["task", "skill"]


# ---------------------------------------------------------------------------
# OUTPUT_DIR handling
# ---------------------------------------------------------------------------

def _setup_prompt_mode() -> None:
    """Read PROXY_PROMPT_MODE from the container env, validate, and set the
    module global. The var is no longer a user .env knob: it is absent for
    normal launches (the proxy then uses the 'hybrid' default) and is set only
    when `harness start/restart --prompt-mode <mode>` injects it for a
    benchmark/power launch. Invalid or absent values fall back to 'hybrid'."""
    global _PROMPT_MODE
    raw = os.environ.get("PROXY_PROMPT_MODE", "hybrid").strip().lower()
    valid = ("hybrid", "user_front", "passthrough")
    if raw not in valid:
        print(
            f"[!] PROXY_PROMPT_MODE='{raw}' is not one of "
            f"{'/'.join(valid)}; defaulting to 'hybrid'",
            flush=True,
        )
        raw = "hybrid"
    _PROMPT_MODE = raw


def _setup_host_os() -> None:
    """Read HARNESS_HOST_OS (set by the harness CLI from harness_detect_os) into
    the module global. Recognised values are linux/macos/windows; anything else
    — unset, empty, or the literal "unknown" — becomes "" so the reminder omits
    the host-OS parenthetical while still stating the container/reproducibility
    facts. Host OS is fixed per install, so reading it once at startup is
    correct; no per-request threading."""
    global _HOST_OS
    raw = os.environ.get("HARNESS_HOST_OS", "").strip().lower()
    _HOST_OS = raw if raw in ("linux", "macos", "windows") else ""
    print(f"[i] host OS: {_HOST_OS or '(unknown)'}", flush=True)


def _setup_run_mode() -> None:
    """Read HARNESS_RUN_MODE into the module global. Only `harness host` sets
    it (to "host"); container mode leaves it unset, so anything unrecognised —
    including an older compose file that predates this var — falls back to
    "container", which is what every deployment did before this existed.

    The reminder's Environment bullet is the only consumer. In container mode
    the agent runs in a Linux image with the working directory bind-mounted; in
    host mode opencode runs directly on the user's own machine, which is often
    NOT Linux, so the container/reproducibility prose is actively wrong there.
    Run mode is fixed for a launch, so reading it once at startup is correct."""
    global _RUN_MODE
    raw = os.environ.get("HARNESS_RUN_MODE", "").strip().lower()
    _RUN_MODE = raw if raw in ("host", "container") else "container"
    print(f"[i] run mode: {_RUN_MODE}", flush=True)


def _setup_mcp_tool_recency() -> None:
    """Read HARNESS_MCP_TOOL_RECENCY (a JSON object keyed `<server>_<tool>` ->
    one-line description, built by the harness CLI from each enabled MCP's
    `recency.json`) into the module global. Only str->non-empty-str entries are
    kept; an unset, empty, or unparsable value yields an empty map (MCP tools
    then render as bare signatures, the same graceful degradation as a tool
    with no entry). MCP membership is fixed per launch, so reading once at
    startup is correct — no per-request threading."""
    global _MCP_TOOL_RECENCY
    raw = os.environ.get("HARNESS_MCP_TOOL_RECENCY", "").strip()
    parsed: Dict[str, str] = {}
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            parsed = {
                k: v
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
    _MCP_TOOL_RECENCY = parsed
    print(f"[i] MCP tool recency: {len(_MCP_TOOL_RECENCY)} entries", flush=True)


def _setup_state_check_tools() -> None:
    """Read HARNESS_MCP_STATE_CHECK (a JSON array of `<server>_<tool>` names the
    harness CLI collected from enabled MCPs' recency.json `state_check` flags)
    into the module global. Non-string / empty entries are dropped; an unset,
    empty, or unparsable value yields an empty set (no `[state-check]` markers
    and no orient-first line — the same graceful degradation as a tool with no
    recency entry). Fixed per launch, like the recency map."""
    global _MCP_STATE_CHECK_TOOLS
    raw = os.environ.get("HARNESS_MCP_STATE_CHECK", "").strip()
    names: set = set()
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, list):
            names = {x for x in data if isinstance(x, str) and x.strip()}
    _MCP_STATE_CHECK_TOOLS = names
    print(f"[i] MCP state-check tools: {len(_MCP_STATE_CHECK_TOOLS)}", flush=True)


def _setup_reminder_template() -> None:
    """Load the hybrid recency reminder's prose from the file
    `_reminder_template_path()` resolves to, stripping the leading comment
    block the file uses to document its own tokens and the trailing newline a
    text editor adds (the reminder ends at `]`).

    An unreadable or empty file is NOT fatal: the proxy logs a loud `[!]` and
    falls back to `_REMINDER_FALLBACK`, which keeps the tool-call envelope (and
    so tool calling itself) working while making the misconfiguration obvious
    in the startup banner. The realistic cause is a bad bind-mount source,
    where docker mounts a directory over the file."""
    global _REMINDER_TEMPLATE
    path = _reminder_template_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        print(
            f"[!] reminder template unreadable at {path} ({exc}); "
            "using the built-in fallback",
            flush=True,
        )
        _REMINDER_TEMPLATE = _REMINDER_FALLBACK
        return
    body = _REMINDER_HEADER_RE.sub("", raw).rstrip("\n")
    if not body.strip():
        print(
            f"[!] reminder template at {path} is empty; using the built-in "
            "fallback",
            flush=True,
        )
        _REMINDER_TEMPLATE = _REMINDER_FALLBACK
        return
    _REMINDER_TEMPLATE = body
    print(f"[i] reminder template: {len(body)} chars from {path}", flush=True)


def _load_tool_guidance(path: str):
    """Parse the per-tool entries file and return
    `(values, warnings)` — a pure function so tests can exercise every
    degradation path without patching stdout.

    `values` always has all five keys (`legend`, `state_check_note`,
    `tool_search_note`, `detail_tools`, `tools`) with usable types.
    `warnings` is a list of human-readable strings the caller logs with `[!]`.

    Every failure degrades PER SECTION rather than losing the file: the point
    of moving this data out of proxy.py is that an operator hand-edits it, and
    a typo in one tool's description must not silently blank the legend and
    the other seventeen. Only a failure that prevents parsing at all (missing
    file, malformed JSON, non-object top level) costs every section — and even
    then each one falls back to a built-in, so the reminder still renders.
    Keys the file does not define are simply not overridden; keys starting
    with `_` are ignored, which is what lets the shipped file carry its own
    `_README`.
    """
    warnings: List[str] = []
    values = {
        "legend": _HYBRID_LEGEND_FALLBACK,
        "state_check_note": _HYBRID_STATE_CHECK_NOTE_FALLBACK,
        "tool_search_note": _HYBRID_TOOL_SEARCH_NOTE_FALLBACK,
        "detail_tools": list(_HYBRID_DETAIL_TOOLS_FALLBACK),
        "tools": {},
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        warnings.append(
            f"tool guidance unreadable at {path} ({exc}); using built-in "
            "defaults (tools render as bare signatures)"
        )
        return values, warnings
    try:
        data = json.loads(raw)
    except ValueError as exc:
        # json.JSONDecodeError carries lineno/colno; naming them is the whole
        # ergonomic point of a hand-edited config file.
        where = ""
        if getattr(exc, "lineno", None) is not None:
            where = f" at line {exc.lineno} column {exc.colno}"
        warnings.append(
            f"tool guidance at {path} is not valid JSON{where}: {exc}; using "
            "built-in defaults (tools render as bare signatures)"
        )
        return values, warnings
    if not isinstance(data, dict):
        warnings.append(
            f"tool guidance at {path} must be a JSON object, got "
            f"{type(data).__name__}; using built-in defaults"
        )
        return values, warnings

    for key in ("legend", "state_check_note", "tool_search_note"):
        if key not in data:
            continue
        val = data[key]
        if not isinstance(val, str):
            warnings.append(
                f"tool guidance: '{key}' must be a string, got "
                f"{type(val).__name__}; keeping the built-in default"
            )
            continue
        # An empty string is a deliberate edit ("suppress this sentence"), so
        # it is honoured for the two optional notes. An empty legend would
        # leave the tool list unexplained, so that one falls back.
        if key == "legend" and not val.strip():
            warnings.append(
                "tool guidance: 'legend' is empty; keeping the built-in default"
            )
            continue
        values[key] = val

    if "detail_tools" in data:
        val = data["detail_tools"]
        if isinstance(val, list):
            kept = [x for x in val if isinstance(x, str) and x.strip()]
            dropped = len(val) - len(kept)
            if dropped:
                warnings.append(
                    f"tool guidance: dropped {dropped} non-string/empty entry"
                    f"{'' if dropped == 1 else 'ies'} from 'detail_tools'"
                )
            # An explicit empty list is a valid edit: echo no full descriptions.
            values["detail_tools"] = kept
        else:
            warnings.append(
                "tool guidance: 'detail_tools' must be a list, got "
                f"{type(val).__name__}; keeping the built-in default"
            )

    if "tools" in data:
        val = data["tools"]
        if isinstance(val, dict):
            kept = {}
            bad = []
            for k, v in val.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    kept[k] = v
                else:
                    bad.append(str(k))
            if bad:
                # Name the offending keys: with ~18 entries, "one of them is
                # wrong" is not an actionable message.
                warnings.append(
                    "tool guidance: dropped non-string/empty description(s) "
                    f"for {', '.join(sorted(bad))}"
                )
            values["tools"] = kept
        else:
            warnings.append(
                "tool guidance: 'tools' must be a JSON object, got "
                f"{type(val).__name__}; no per-tool guidance will render"
            )
    return values, warnings


def _setup_tool_guidance() -> None:
    """Load the per-tool entries file into the module globals and log the
    result. Called once from `main()`; also called at import (silently, via
    `_load_tool_guidance`) so importing proxy.py in tests needs no `main()`.

    Every problem is logged with a loud `[!]` naming the file and, for a JSON
    syntax error, the line and column — but nothing here is fatal. The
    guidance is a prompt-quality aid; losing it costs the model its
    failure-mode hints, not its ability to call tools."""
    global _HYBRID_LEGEND, _HYBRID_STATE_CHECK_NOTE, _HYBRID_TOOL_SEARCH_NOTE
    global _HYBRID_DETAIL_TOOLS, _HYBRID_TOOL_GUIDANCE
    path = _tool_guidance_path()
    values, warnings = _load_tool_guidance(path)
    for msg in warnings:
        print(f"[!] {msg}", flush=True)
    _HYBRID_LEGEND = values["legend"]
    _HYBRID_STATE_CHECK_NOTE = values["state_check_note"]
    _HYBRID_TOOL_SEARCH_NOTE = values["tool_search_note"]
    _HYBRID_DETAIL_TOOLS = values["detail_tools"]
    _HYBRID_TOOL_GUIDANCE = values["tools"]
    print(
        f"[i] tool guidance: {len(_HYBRID_TOOL_GUIDANCE)} tool(s), "
        f"{len(_HYBRID_DETAIL_TOOLS)} detail tool(s) from {path}",
        flush=True,
    )


# Populate the globals at import so `_format_tool_entries` works in a test that
# never calls main(). Silent by design — main() re-runs `_setup_tool_guidance`
# and it is that call that prints the banner line and any `[!]`. Loading here
# rather than lazily on first render also keeps the globals patchable: a test
# that patches `_HYBRID_DETAIL_TOOLS` is never clobbered mid-request by a
# first-use load.
(
    _tg_values,
    _tg_warnings,
) = _load_tool_guidance(_tool_guidance_path())
_HYBRID_LEGEND = _tg_values["legend"]
_HYBRID_STATE_CHECK_NOTE = _tg_values["state_check_note"]
_HYBRID_TOOL_SEARCH_NOTE = _tg_values["tool_search_note"]
_HYBRID_DETAIL_TOOLS = _tg_values["detail_tools"]
_HYBRID_TOOL_GUIDANCE = _tg_values["tools"]
del _tg_values, _tg_warnings


def _setup_tool_search() -> None:
    """Read HARNESS_TOOL_SEARCH into the module global. Truthy values
    (1/true/yes/on, case-insensitive) enable the cooperative tool-search
    meta-tools; anything else (the default) leaves them off so the prompt and
    the dispatch path are byte-for-byte unchanged from before the feature
    existed."""
    global _TOOL_SEARCH_ENABLED
    raw = os.environ.get("HARNESS_TOOL_SEARCH", "").strip().lower()
    _TOOL_SEARCH_ENABLED = raw in ("1", "true", "yes", "on")
    print(
        f"[i] cooperative tool-search: {'on' if _TOOL_SEARCH_ENABLED else 'off'}",
        flush=True,
    )


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


def _collect_tool_names(tools_array):
    """Return the set of tool names from an OpenAI-format tools array.

    Used to gate the tolerant missing-`arguments` lift inside
    `extract_tool_calls_and_text` (issue #118). Tolerates both the
    `{"function": {"name": ...}}` and bare `{"name": ...}` shapes the
    upstream agents may send. Non-string / empty names are dropped.
    """
    names: set = set()
    for tool in tools_array or []:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") if "function" in tool else tool
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


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


_META_TOOLS_PROMPT_BLOCK = """
<<<BEGIN_META_TOOLS>>>
Two extra tools let you look up tool details on demand. They are answered here, in the proxy, from the live tool list — calling one returns information only, never a side effect on the workspace, and the result comes back the same way a normal tool result does:
- tool_list() — list every available tool name with a one-line purpose.
- tool_search({"query": "..."}) — return the full signature and description of the tools whose name or description matches the query.
Call them with the same ```json envelope as any tool. Use them when you are unsure whether a tool exists or need a tool's exact parameters; the full schemas in <<<BEGIN_AGENT_TOOLS>>> above remain authoritative.
<<<END_META_TOOLS>>>
"""


def build_cooperative_prompt_system_addition(tools_text):
    """Returns the cooperative-prompt scaffolding to APPEND to the system
    message in modes 'system' and 'hybrid'. Static across all turns; safe
    to set once on the system message rather than re-sending per turn.

    When cooperative tool-search is enabled (_TOOL_SEARCH_ENABLED) a small
    <<<BEGIN_META_TOOLS>>> block is appended advertising the proxy-served
    tool_search/tool_list meta-tools; off by default, so the prefix is
    unchanged for a normal launch.
    """
    meta_block = _META_TOOLS_PROMPT_BLOCK if _TOOL_SEARCH_ENABLED else ""
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
{meta_block}"""


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


# --- Reminder token expansions composed in code -----------------------------
#
# Two of the reminder's tokens expand to whole sentences rather than a word,
# because what they say depends on runtime state the prose file cannot see.
# `{{CWD}}` already worked this way; `{{ENVIRONMENT}}` and `{{TODOS}}` follow
# the same precedent. A user who wants literal control over either can simply
# delete the token from their reminder.md and write their own text — unknown
# and absent tokens are both non-events.

# How many todo items the replay may carry, and how wide one line may get.
# The reminder is a per-turn tax on every request, so the list is bounded even
# when the model writes a fifty-step plan; completed items are the first to go
# because they are the ones the model no longer needs to act on.
_TODOS_MAX_ITEMS = 15
_TODOS_MAX_CONTENT = 140

_TODO_STATUS_MARK = {
    "completed": "x",
    "in_progress": "~",
    "cancelled": "-",
    "pending": " ",
}


def _format_environment_clause() -> str:
    """The Environment bullet's body, chosen by run mode.

    Container mode keeps the long-standing wording: a Linux image with the
    working directory bind-mounted, and the reproducibility caveat that
    follows from the agent's filesystem not being the user's. Host mode is the
    opposite situation — opencode runs directly on the user's machine, which
    is frequently macOS or Windows — so claiming a Linux container there is a
    false statement the model then reasons from (wrong path separators, wrong
    shell, advice to rebuild a venv that never needed rebuilding)."""
    host_os = f" (host OS: {_HOST_OS})" if _HOST_OS else ""
    if _RUN_MODE == "host":
        return (
            "you run directly on the user's own machine" + host_os + ", in "
            "their real filesystem — there is no container between you and "
            "their system. Do not assume Linux: paths, shell, and installed "
            "toolchain are theirs, and if you need to know one, check with a "
            "tool instead of guessing. Anything you install lands on their "
            "machine, so keep setup project-local (e.g. a venv inside the "
            "working directory + requirements.txt) rather than installing "
            "globally."
        )
    return (
        "you run in a Linux container with the current working directory "
        "mounted from the host" + host_os + ". Your work must reproduce in "
        "the user's environment, not this container — anything installed only "
        "here (e.g. a global/system venv) the user cannot run. Put "
        "reproducible setup in the working directory (e.g. a project-local "
        "venv + requirements.txt); a venv built here is Linux-native, so on a "
        "non-Linux host the user may need to recreate it (python -m venv "
        ".venv && pip install -r requirements.txt)."
    )


def _extract_latest_todos(messages):
    """Return the `todos` array from the LAST `todowrite` call in the inbound
    history, or None if the model has not written one this session.

    opencode has no `todoread` — it was deleted upstream — so the model cannot
    look its own list up. The list only exists in two places: opencode's local
    SQLite, which the proxy has no access to, and the `todowrite` tool_calls
    sitting in the history opencode sends on every request. The second is the
    one harness can reach, so the list is re-derived from scratch each turn
    rather than cached: the proxy is stateless per request, and a cache keyed
    on anything available here would be wrong across concurrent sessions.

    This is what makes the replay survive the upstream forgetting: opencode
    keeps sending the full history, so even when the upstream has dropped
    everything but the last message, that last message still carries the list.
    """
    latest = None
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function") or {}
            if func.get("name") != "todowrite":
                continue
            args = func.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    continue
            if not isinstance(args, dict):
                continue
            todos = args.get("todos")
            # An explicit empty list is a real state (the model cleared the
            # list), so it wins over an earlier non-empty one.
            if isinstance(todos, list):
                latest = todos
    return latest


def _format_todos_block(todos) -> str:
    """Render `{{TODOS}}`: either the live list, or the instruction to create
    one. Never returns "" — an empty expansion is precisely the case where the
    model most needs to be told what to do."""
    items = []
    for todo in todos or []:
        if not isinstance(todo, dict):
            continue
        content = str(todo.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > _TODOS_MAX_CONTENT:
            content = content[: _TODOS_MAX_CONTENT - 1].rstrip() + "\u2026"
        status = str(todo.get("status") or "pending").strip().lower()
        items.append((status, content))

    if not items:
        return (
            "\n  YOU HAVE NO TODO LIST. Before you do anything else this turn, "
            "call `todowrite` with a detailed, step-by-step breakdown of the "
            "user's request — every step you can foresee, not a three-line "
            "sketch. That list is the only memory you get."
        )

    # Over the cap, completed items are dropped first (oldest first): they are
    # history, and the pending ones are what the model still has to act on.
    dropped = 0
    while len(items) > _TODOS_MAX_ITEMS:
        for i, (status, _) in enumerate(items):
            if status in ("completed", "cancelled"):
                del items[i]
                dropped += 1
                break
        else:
            del items[0]
            dropped += 1

    lines = "\n".join(
        f"    [{_TODO_STATUS_MARK.get(status, '?')}] {content}"
        for status, content in items
    )
    elided = f"\n    (+{dropped} finished item(s) not shown)" if dropped else ""
    return (
        "\n  Your todo list, replayed from your last `todowrite` "
        "([ ]=pending, [~]=in_progress, [x]=completed, [-]=cancelled). This "
        "is a copy for reading, not the live list — to change anything, call "
        "`todowrite` with the FULL updated list:\n"
        + lines
        + elided
    )


def _format_tool_entries(tool_signatures, tool_details=None):
    """Render the per-tool block of the hybrid recency reminder. One entry per
    tool — signature + the one-line guidance from `_HYBRID_TOOL_GUIDANCE`
    (loaded from tool-guidance.json) + (for detail tools) the verbatim
    description from
    `_extract_tool_details`. Consolidates information that used to be split
    across three places (signature line, no per-tool guidance, separate
    `<<<BEGIN_TOOL_DETAIL>>>` blocks) into a single entry per tool so the
    agent sees every fact about a tool together.

    `tool_signatures` is the `(name, required, optional)` triples from
    `_extract_tool_signatures`. `tool_details` is the optional list of
    `(name, description)` pairs from `_extract_tool_details` — these
    descriptions used to render as their own `<<<BEGIN_TOOL_DETAIL>>>` blocks
    after the reminder; the consolidated format inlines them under the
    matching tool's entry. Returns "" when there are no signatures (no tools).
    """
    if not tool_signatures:
        return ""
    details_by_name: Dict[str, str] = dict(tool_details or [])
    lines = []
    for name, req, opt in tool_signatures:
        signature = _format_tool_signature(name, req, opt)
        # opencode's own tools are keyed in the code map; MCP tools (`<server>_
        # <tool>`) load from each enabled MCP's recency.json into _MCP_TOOL_RECENCY.
        guidance = _HYBRID_TOOL_GUIDANCE.get(name) or _MCP_TOOL_RECENCY.get(name)
        head = f"- {signature}"
        if name in _MCP_STATE_CHECK_TOOLS:
            # Marks a state-mutating tool; the legend below tells the model to
            # orient (call the server's read-only state tool) before calling it.
            head = f"{head} [state-check]"
        if guidance:
            head = f"{head} — {guidance}"
        detail = details_by_name.get(name)
        if detail and detail.strip():
            # Indent the multi-line description so it visibly belongs to the
            # tool entry above. Two spaces per line; preserve internal
            # blank lines.
            indented = "\n".join(
                f"  {ln}" if ln else "" for ln in detail.strip().splitlines()
            )
            lines.append(f"{head}\n{indented}")
        else:
            lines.append(head)
    # Legend and its two conditional sentences are user-editable data loaded
    # from tool-guidance.json, same as the per-tool guidance above.
    legend = _HYBRID_LEGEND
    if any(name in _MCP_STATE_CHECK_TOOLS for name, _, _ in tool_signatures):
        # Orient-first rule, surfaced only when a state-check tool is actually in
        # this turn's toolset. This is the standing "call state first" policy at
        # the always-injected altitude (the harness ships no runtime AGENTS.md;
        # the recency reminder is its standing-instruction channel).
        legend += _HYBRID_STATE_CHECK_NOTE
    if _TOOL_SEARCH_ENABLED:
        legend += _HYBRID_TOOL_SEARCH_NOTE
    return f"\n\n{legend}\n" + "\n".join(lines)


_WORKING_DIR_RE = re.compile(r"Working directory:[ \t]*(\S[^\n]*)")


def _extract_working_directory(system_content):
    """Pull `Working directory: <path>` out of the opencode `<env>` block in
    a system/agent-instructions message. Returns the trimmed path string or
    None if the marker isn't present (non-opencode upstream, or upstream
    changes the label).

    The path is echoed at recency in the Environment bullet so the model
    answers questions like "what's in this folder?" against the host's
    bind-mounted CWD rather than from a pretrained sense of its own
    sandbox path (e.g. `/home/bard`, `/workspace`). Missing or unparsable
    => recency degrades gracefully to the prior wording (host OS only).
    """
    if not system_content:
        return None
    m = _WORKING_DIR_RE.search(system_content)
    if not m:
        return None
    return m.group(1).strip() or None


def build_cooperative_prompt_hybrid_reminder(content, tool_signatures, tool_details=None, working_directory=None, todos=None):
    """In hybrid mode the full tool definitions sit at the stable prefix
    (the system message; with _CHANGE_SYSTEM_TO_USER on, the user-role
    message at index 0). The recency message that lands on the LAST user
    turn is laid out as:

      <<<BEGIN_USER_REQUEST>>>
      {content}
      <<<END_USER_REQUEST>>>

      [Reminder — operating rules for this turn.
       - Operating: ... (merged Agency/Tools/Workflow)
       - Honesty:   ...
       - Environment: ...

       Tools — one entry per tool ...
       - tool1(...) — guidance line
       - tool2(...) — guidance line
         <verbatim description if tool2 is a detail tool>
       ...]

    The live user request is placed FIRST, before the reminder, so the
    model's most-recent attention isn't on a wall of operating rules but
    on what the user actually asked. Tool-result turns (content already
    delimited by `<<<BEGIN_TOOL_RESULT>>>` markers) skip the
    USER_REQUEST wrap — the TOOL_RESULT markers already delimit the live
    "ask" of the turn.

    The three bullets' PROSE is not here: it is user-owned data, loaded from
    `<install root>/reminder.md` by `_setup_reminder_template`. This function only
    substitutes the three tokens the file documents — `{{HOST_OS}}`,
    `{{CWD}}`, `{{TOOL_ENTRIES}}` — into it. Read the shipped default at
    `proxy/reminder.md` for the wording and why each rule is there (the
    Operating bullet's "you act through opencode, calls really execute" is
    issue #109's anchor against the upstream's "I can't execute, here are
    commands for you to run" persona reversion). A user who rewrites the file
    owns the result; nothing here validates the prose.

    The per-tool entries below the bullets carry all three things that
    used to be split across the prior recency block — signature, shortened
    guidance, and the closed-set argument values (a `task`'s
    `subagent_type` agents, a `skill`'s valid names) — together under the
    tool's own line. The detail descriptions (`tool_details`) used to
    render as separate `<<<BEGIN_TOOL_DETAIL>>>` blocks; they are now
    inlined under the matching tool's entry, so the agent reads
    everything it needs about a tool in one place.

    `tool_signatures` is a list of `(name, required_keys, optional_keys)`
    triples produced by `_extract_tool_signatures`.

    `tool_details` is an optional list of `(name, description)` pairs from
    `_extract_tool_details` — the "detail tools" whose description is inlined
    under the matching tool's entry.
    """
    is_tool_result = "<<<BEGIN_TOOL_RESULT" in content
    if is_tool_result:
        # Tool-result turn: content is already delimited by TOOL_RESULT
        # markers; place it first as the "ask" of the turn, reminder
        # behind it. Do not wrap in USER_REQUEST (TOOL_RESULT already
        # delimits; USER_REQUEST is for live user asks).
        wrapped = content
    else:
        wrapped = (
            f"<<<BEGIN_USER_REQUEST>>>\n{content}\n<<<END_USER_REQUEST>>>"
        )
    host_os_clause = f" (host OS: {_HOST_OS})" if _HOST_OS else ""
    if working_directory:
        cwd_clause = (
            f" The working directory for this turn is `{working_directory}` — "
            "when the user says \"this folder\", \"here\", \"my machine\", or "
            "\"the workspace\", they mean exactly that path."
        )
    else:
        cwd_clause = ""
    tool_entries = _format_tool_entries(tool_signatures, tool_details)
    if _REMINDER_TEMPLATE is None:
        # Lazy load so importing proxy.py (tests) needs no main() call.
        _setup_reminder_template()
    reminder = (
        (_REMINDER_TEMPLATE or "")
        # {{HOST_OS}} predates {{ENVIRONMENT}} and keeps working on its own:
        # a reminder.md seeded before this change still carries only the old
        # token, and seeding never overwrites a user's copy.
        .replace(_REMINDER_TOKEN_HOST_OS, host_os_clause)
        .replace(_REMINDER_TOKEN_CWD, cwd_clause)
        .replace(_REMINDER_TOKEN_ENVIRONMENT, _format_environment_clause())
        .replace(_REMINDER_TOKEN_TODOS, _format_todos_block(todos))
        .replace(_REMINDER_TOKEN_TOOL_ENTRIES, tool_entries)
    )
    return f"{wrapped}\n\n{reminder}"


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


def _diagnose_failed_tool_call(response_text: str) -> Optional[str]:
    """When `extract_tool_calls_and_text` returns zero tool calls, classify
    whether the response looks like a botched tool-call attempt that
    in-proxy retry can recover (issue #121).

    Returns one of:
      - ``None`` — no recoverable signal. The response is prose, an
        unknown failure mode, or a JSON example the model was deliberately
        describing.
      - ``"malformed_escape"`` — the response carries a ```json fence with
        a brace-balanced JSON body that ``json.loads(..., strict=False)``
        rejected with an "Invalid \\escape" error. High-confidence: the
        model clearly attempted a tool call and only the backslash
        escaping is wrong. Fires even if there is prose around the fence.
      - ``"malformed_fence"`` — the response is *effectively* a ```json
        fence with no parseable JSON body — i.e. the stripped response
        STARTS with ```json (no prior prose), and the fence opener is
        followed by either EOF, whitespace-to-EOF, or a non-``{`` character
        before any other recognisable JSON object. Conservative gating:
        only fires when the model's whole emission is the broken fence
        (the reported ``\\`\\`\\`json_parse_or_id:todowrite}`` shape), so
        prose describing JSON examples is NOT mistaken for a failed call.

    The caller is responsible for verifying that no tool calls were
    extracted before consulting this helper — recovery only makes sense
    when extraction yielded nothing.
    """
    if not response_text:
        return None
    fence_pos = response_text.find("```json")
    if fence_pos == -1:
        return None
    body_start = fence_pos + len("```json")
    while body_start < len(response_text) and response_text[body_start] in " \t\n\r":
        body_start += 1

    if body_start < len(response_text) and response_text[body_start] == "{":
        json_str, _ = _scan_balanced_json(response_text, body_start)
        if json_str is not None:
            try:
                json.loads(json_str, strict=False)
            except json.JSONDecodeError as e:
                msg = e.msg or ""
                # Python's json module raises with msg == "Invalid \\escape"
                # for a `\X` where X is not in `"\\/bfnrtu`. That is the
                # JSON spec itself flagging the bad escape, not a fuzzy
                # heuristic — false-positive risk on a brace-balanced body
                # after a ```json fence is near zero.
                if "Invalid \\escape" in msg:
                    return "malformed_escape"
        # Brace-balanced but shape-wrong, OR brace-unbalanced after `{`:
        # leave it to bleed (it may be prose). Don't retry.
        return None

    # Body does not begin with `{`. Only treat as a failed tool call when
    # the stripped response IS the malformed fence — anything else (prose
    # before the fence, or recoverable JSON later in the text) means the
    # model isn't simply trying to call a tool.
    if response_text.lstrip().startswith("```json"):
        # Make sure there is no LATER balanced `{...}` after a ```json
        # fence that we would have extracted but for a shape mismatch —
        # if there is, the response is "model emitted some prose plus a
        # describable JSON example", not a botched tool call.
        next_fence = response_text.find("```json", fence_pos + 1)
        if next_fence == -1:
            return "malformed_fence"
    return None


def extract_tool_calls_and_text(response_text, available_tool_names=None):
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

    Parsing uses `strict=False` so unescaped control characters inside
    string values are accepted — LLMs commonly emit a multi-line
    `python -c "..."` or heredoc as the `command` value with real `\n`
    bytes instead of `\\n` escapes. Strict mode (the json.loads default)
    rejects those and the block bleeds into chat as raw fenced text;
    issue #115 was exactly that.

    `available_tool_names`: iterable of tool names currently exposed to the
    model for this turn. When set, a block whose `name` matches an entry
    but is missing the `arguments` key has its remaining top-level keys
    lifted into `arguments` — the failure mode in issue #118 where models
    emit `{"name": "bash", "command": "...", "description": "..."}` with
    args spelled at the top level instead of nested. The lift fires ONLY
    when (a) `arguments` is absent and (b) `name` is a current tool, so
    instructional prose using an unknown tool name still leaks intact, and
    a correctly-shaped call with `arguments` is untouched.

    Returns (payloads, clean_text). payloads is a list (possibly empty)
    of {name, arguments} dicts in the order they appeared. clean_text is
    response_text with all VALID extracted blocks removed.
    """
    known_names = set(available_tool_names) if available_tool_names else set()
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
            candidate = json.loads(json_str, strict=False)
        except json.JSONDecodeError:
            pos = after_json
            continue

        if not isinstance(candidate, dict):
            pos = after_json
            continue
        if 'name' not in candidate:
            pos = after_json
            continue
        if 'arguments' not in candidate:
            # Tolerant lift: a block like `{"name": "bash", "command": "ls",
            # "description": "..."}` is a tool call whose args were spelled
            # at the top level. We only lift when the name matches a tool
            # we actually exposed for this turn, otherwise instructional
            # prose like ` ```json\n{"name": "no_such_tool", ...}\n``` `
            # would be silently promoted to a (failing) tool invocation.
            if candidate['name'] not in known_names:
                pos = after_json
                continue
            lifted_args = {k: v for k, v in candidate.items() if k != 'name'}
            candidate = {'name': candidate['name'], 'arguments': lifted_args}

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
# Translation: inbound OpenAI-format -> upstream-format
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
    Translate the inbound OpenAI-format messages into a flat conversation
    suitable for the upstream API. Tool calls become markdown JSON blocks embedded in assistant
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
    # tool message: opencode sends a `tool_call_id` (OpenAI shape), while some
    # clients send `tool_name`/`name` directly. So we record names as
    # assistant tool_calls go by — keyed by id for exact correlation, plus an
    # ordered list as a positional fallback when no id is present anywhere.
    tool_names_by_id: Dict[str, str] = {}
    pending_tool_names: List[str] = []

    for msg in original_messages:
        role = msg.get("role")
        # Normalize content to a plain string up front. Some clients send
        # content as a list of content-blocks (multimodal user turns, or the
        # AI SDK structuring an assistant turn as parts). Left as a list it
        # crashes the downstream `.strip()` / `+=` / join paths with a 500;
        # flatten once here so every role branch can treat content as a str.
        content = _flatten_content_to_str(msg.get("content", "") or "")

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
                    # OpenAI args is a JSON string; pass it through verbatim.
                    # Some clients send an object instead — render it as
                    # compact JSON in that case.
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
            # present, else the `tool_call_id` correlated
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

            # Wrap every real user-role message in USER_MESSAGE markers —
            # EXCEPT the last one. The last real user turn is the live ask;
            # the recency builder wraps it in <<<BEGIN_USER_REQUEST>>>
            # instead and places it at the FRONT of the recency block
            # (before the reminder), so the model's most-recent attention
            # lands on the user's actual question rather than on a wall of
            # operating rules. Prior user turns keep USER_MESSAGE — they're
            # historical context, not the live ask.
            #
            # "Real" = original role was `user`, not a tool-result that got
            # role-converted; the latter already carry <<<BEGIN_TOOL_RESULT
            # name="…">>> markers from the universal pre-dispatch wrap, so we
            # detect and skip them by that marker prefix. The system message
            # at index 0 is still role "system" here (sys→user runs later),
            # so the role filter leaves it alone.
            last_user_idx = -1
            for i, m in enumerate(messages):
                if m["role"] == "user":
                    last_user_idx = i
            for i, m in enumerate(messages):
                if m["role"] != "user":
                    continue
                if i == last_user_idx:
                    # The builder handles delimiting for the live turn
                    # (USER_REQUEST for a real user turn, none for a tool
                    # result — the TOOL_RESULT markers already delimit).
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
            # turn or tool-result-converted-to-user). The builder places
            # the live user content at the FRONT (wrapped in USER_REQUEST
            # for a real user turn, left as-is for a tool result) and
            # appends the reminder behind it.
            if messages[-1]["role"] == "user":
                signatures = _extract_tool_signatures(tools, tools_text)
                details = _extract_tool_details(tools, _HYBRID_DETAIL_TOOLS)
                # The live user content lands in USER_REQUEST in the builder,
                # so the message comes in here as plain content (no
                # USER_MESSAGE wrap to strip). Tool-result-converted user
                # messages carry TOOL_RESULT markers already; the builder
                # passes those through.
                live_content = _flatten_content_to_str(messages[-1]["content"])
                # Pull the host CWD from the inbound opencode `<env>` block
                # at messages[0] so the recency Environment line can echo
                # exactly where "this folder" lives — without that anchor,
                # the model has answered from a pretrained sandbox path
                # (e.g. `/home/bard`) when the upstream's prior wins out
                # over harness's "you act through opencode" framing.
                system_content = (
                    _flatten_content_to_str(messages[0]["content"])
                    if messages and messages[0]["role"] == "system"
                    else ""
                )
                working_directory = _extract_working_directory(system_content)
                # Replayed from the raw inbound history (tool_calls are
                # flattened into prose in `messages` by this point), so the
                # list reaches recency even when the upstream has dropped
                # every earlier turn.
                todos = _extract_latest_todos(original_messages)
                messages[-1]["content"] = build_cooperative_prompt_hybrid_reminder(
                    live_content,
                    signatures,
                    details,
                    working_directory=working_directory,
                    todos=todos,
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
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    # Divisor of 3 (not the prose rule-of-thumb 4) reflects that agent
    # turns are dominated by code, JSON tool-call blocks, and verbatim
    # tool results, which BPE tokenizers pack denser than prose.
    n = max(1, len(text) // 3)
    return min(n, MODEL_CONTEXT_LENGTH)


# ---------------------------------------------------------------------------
# OpenAI-compatible response emission
# ---------------------------------------------------------------------------

def _openai_tool_calls(tool_call_payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert internal {name, arguments} payloads to OpenAI tool_calls.

    OpenAI's `function.arguments` is a JSON *string*, whereas the internal
    payload carries it as an object — so encode any non-string arguments.
    Each call gets a stable index (the array key
    the AI SDK accumulates streamed deltas into) and a unique id so tool_use
    blocks in the conversation history can be correlated to their results.
    """
    tcs: List[Dict[str, Any]] = []
    for i, payload in enumerate(tool_call_payloads):
        args = payload["arguments"]
        if not isinstance(args, str):
            args = json.dumps(args)
        tcs.append({
            "index": i,
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": payload["name"], "arguments": args},
        })
    return tcs


def generate_openai_sse(
    model_name: str,
    clean_text: str,
    tool_call_payloads: List[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
) -> Iterable[str]:
    """Yield OpenAI-compatible SSE lines (`data: {chunk}\\n\\n` ... `data: [DONE]`).

    The proxy has the full upstream response in hand before emission (it must,
    for balanced-brace tool-call extraction over the complete text), so the
    content is sent as a small number of chat.completion.chunk deltas rather
    than incrementally. The AI SDK's openai-compatible provider reassembles
    these: the first tool-call delta must carry `id` + `function.name` and the
    accumulated `function.arguments` must parse as JSON, both satisfied here by
    emitting each call complete in a single delta.
    """
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    def emit(delta: Optional[Dict[str, Any]] = None,
             finish_reason: Optional[str] = None,
             usage_obj: Optional[Dict[str, Any]] = None) -> str:
        chunk: Dict[str, Any] = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
        }
        if usage_obj is not None:
            chunk["choices"] = []
            chunk["usage"] = usage_obj
        else:
            chunk["choices"] = [{
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }]
        return "data: " + json.dumps(chunk) + "\n\n"

    # Text delta. Always emit an opening assistant delta so the stream is
    # well-formed even when there's no text (tool-only or rescued responses).
    if clean_text:
        yield emit(delta={"role": "assistant", "content": clean_text})
    elif not tool_call_payloads:
        yield emit(delta={"role": "assistant", "content": ""})

    if tool_call_payloads:
        delta: Dict[str, Any] = {"tool_calls": _openai_tool_calls(tool_call_payloads)}
        if not clean_text:
            delta["role"] = "assistant"
        yield emit(delta=delta)

    finish_reason = "tool_calls" if tool_call_payloads else "stop"
    yield emit(delta={}, finish_reason=finish_reason)

    if usage:
        yield emit(usage_obj=_openai_usage(usage))

    yield "data: [DONE]\n\n"


def build_openai_response(
    model_name: str,
    clean_text: str,
    tool_call_payloads: List[Dict[str, Any]],
    usage: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a single non-streamed OpenAI `chat.completion` object.

    Used when an OpenAI-path request sets `stream: false`. opencode always
    streams, so this serves manual `curl`/debug callers; it's cheap to support
    and keeps the inbound contract complete.
    """
    message: Dict[str, Any] = {"role": "assistant", "content": clean_text or ""}
    if tool_call_payloads:
        message["tool_calls"] = [
            {k: v for k, v in tc.items() if k != "index"}
            for tc in _openai_tool_calls(tool_call_payloads)
        ]
        message["content"] = clean_text or None
    finish_reason = "tool_calls" if tool_call_payloads else "stop"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _openai_usage(usage or {}),
    }


def _openai_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


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


def _extract_finish_reason(target_json: Dict[str, Any]) -> str:
    choices = target_json.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("finish_reason") or "").strip()


def _empty_response_rescue_text() -> str:
    """Minimal assistant text substituted when the upstream returns a
    well-formed response with no content and no tool calls. See
    architecture/proxy.md → "Empty-response detection" (issue #117).

    Used ONLY as the no-shell-tool fallback. When a shell tool is exposed
    the rescue is the `pwd` tool call from `_select_rescue_tool` alone (no
    substitute text); this text is what keeps the response non-empty when
    there is no tool to call.

    The trigger for the empty response is confined to the **most-recent**
    message slot — once a turn no longer carries the offending content the
    upstream returns to normal, even with the trigger still present further
    back in history. The text alone unsticks the upstream filter, but a
    text-only response makes opencode end the turn (`finish_reason: stop`),
    so it cannot auto-continue the loop — which is why the tool-call rescue
    is preferred whenever a shell tool is available."""
    return "Understood."


# Names of a shell-execution tool we can drive across agents (opencode
# `bash`, Claude Code `Bash`). Matched case-insensitively. We deliberately
# pick `bash` over `todowrite` for the rescue: `bash` is exposed for every
# coding agent in practice (broader availability than `todowrite`), and a
# read-only command like `pwd` is genuinely inconsequential — no
# filesystem/network/state changes, just prints the working directory.
_RESCUE_TOOL_NAME_PATTERNS = ("bash",)

# The command + description we send when invoking the rescue bash tool.
# `pwd` is the right choice because it (a) exists on every POSIX shell and
# in git-bash on Windows, (b) is read-only, (c) produces a tiny single-line
# tool result that can't itself re-trigger the upstream filter, and (d) is
# obviously inconsequential to anyone reading the conversation later.
_RESCUE_BASH_COMMAND = "pwd"
_RESCUE_BASH_DESCRIPTION = "Print working directory"


# Corrective user-role messages emitted into the augmented history when an
# in-proxy retry fires (issue #121). The retry conversation looks like
# `[…original history…, assistant(<bad response>), user(<correction>)]` and
# goes through the same `translate_history_and_apply_prompt` scaffolding —
# so the correction lands in the recency USER_REQUEST slot in hybrid mode,
# and the model's next turn is read against the SAME tool definitions and
# operating rules as the first attempt. The only thing the model sees that
# is new is "your previous attempt failed for this specific reason; emit
# the same call(s) again". Phrasing notes: (a) name the failure mode so
# the model knows what to change, (b) include the JSON-escape examples
# verbatim since the model will pattern-match on them, (c) tell it NOT to
# apologise/narrate so the retry response is the corrected tool call and
# not a prose explanation that ends the turn at `done_reason: stop`.
_RETRY_CORRECTION_MESSAGES: Dict[str, str] = {
    "malformed_escape": (
        "[harness proxy — automatic correction]\n"
        "Your previous response contained a ```json tool-call block whose "
        "JSON had an invalid `\\escape` sequence (e.g. `\\x1e`, `\\a`, "
        "`\\v` — any `\\X` where X is not one of `\"\\/bfnrtu`). The JSON "
        "parser rejected it, so your tool call did NOT execute and no "
        "result will follow. Re-emit the SAME tool call(s) now with every "
        "backslash JSON-escaped: use `\\\\n` for a literal newline inside "
        "a string, `\\\\x1e` for that byte, `\\\\\\\\` for a single "
        "backslash. Do not apologise, do not explain — emit only the "
        "corrected ```json...``` block(s)."
    ),
    "malformed_fence": (
        "[harness proxy — automatic correction]\n"
        "Your previous response started a ```json tool-call fence but the "
        "body that followed was not a valid JSON object — it was rejected "
        "and your tool call did NOT execute. Re-emit your tool call now "
        "as a COMPLETE ```json...``` block whose body is "
        "`{\"name\": \"<tool>\", \"arguments\": {...}}` — fence opener, "
        "full JSON body, closing fence. Do not apologise, do not explain "
        "— emit only the corrected ```json...``` block(s)."
    ),
}


def _build_retry_correction_message(kind: str) -> str:
    return _RETRY_CORRECTION_MESSAGES.get(
        kind, _RETRY_CORRECTION_MESSAGES["malformed_fence"]
    )


def _retry_upstream_with_correction(
    req_id: str,
    original_messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tools_text: str,
    upstream_model: str,
    headers: Dict[str, str],
    bad_response_text: str,
    kind: str,
) -> Optional[Tuple[Dict[str, Any], str, List[Dict[str, Any]], str]]:
    """Append `[assistant(<bad>), user(<correction>)]` to the conversation
    and re-POST upstream once (issue #121, retry budget = 1). The retry is
    invisible to opencode: if it succeeds, the caller swaps the retry's
    target_json / response_text / payloads / clean_text in place of the
    bad attempt and only the retry result is streamed back.

    Returns ``(target_json, response_text, payloads, clean_text)`` on a
    completed retry (even if the retry itself produced no recoverable
    output — that's the caller's decision), or ``None`` if the retry call
    itself failed (timeout, non-2xx, non-JSON body). When ``None``, the
    caller falls back to the pre-existing bleed-into-chat behaviour for
    the original response.

    Debug dumps for the retry land under `<req_id>_02_API_Retry_Request`
    / `<req_id>_03_API_Retry_Response` so the original `_02`/`_03` dumps
    are preserved.
    """
    correction = _build_retry_correction_message(kind)
    augmented = list(original_messages) + [
        {"role": "assistant", "content": bad_response_text},
        {"role": "user", "content": correction},
    ]
    translated = translate_history_and_apply_prompt(
        augmented, tools_text, tools=tools
    )
    retry_payload: Dict[str, Any] = {
        "model": upstream_model,
        "messages": translated,
    }
    if _PROMPT_MODE == "passthrough" and tools:
        retry_payload["tools"] = tools
    save_debug_file(req_id, "02", "API_Retry_Request", retry_payload)

    try:
        resp = _upstream_post(headers, retry_payload)
    except requests.RequestException as e:
        print(f"[{req_id}] retry upstream request failed: {e}", flush=True)
        save_debug_file(req_id, "03", "API_Retry_Error", {"error": str(e)})
        return None
    if resp.status_code >= 400:
        try:
            err_body: Any = resp.json()
        except Exception:
            err_body = resp.text
        print(
            f"[{req_id}] retry upstream returned {resp.status_code}: "
            f"{err_body}",
            flush=True,
        )
        save_debug_file(
            req_id,
            "03",
            "API_Retry_Error",
            {"status": resp.status_code, "body": err_body},
        )
        return None
    try:
        retry_json = resp.json()
    except ValueError as e:
        print(f"[{req_id}] retry upstream returned non-JSON: {e}", flush=True)
        save_debug_file(
            req_id, "03", "API_Retry_Error", {"error": "non-json", "body": resp.text}
        )
        return None

    save_debug_file(req_id, "03", "API_Retry_Response", retry_json)
    retry_text = extract_assistant_content(retry_json)
    retry_payloads, retry_clean = extract_tool_calls_and_text(
        retry_text, available_tool_names=_collect_tool_names(tools)
    )
    return retry_json, retry_text, retry_payloads, retry_clean


# ---------------------------------------------------------------------------
# Cooperative tool-search (default off; HARNESS_TOOL_SEARCH=1)
# ---------------------------------------------------------------------------
#
# The proxy advertises two synthetic meta-tools — tool_list / tool_search — and
# serves them itself from the CURRENT request's tool array. The registry is the
# inbound `tools` array, rebuilt every request, so there is no cache to go stale
# when an MCP restarts mid-session (the schema-staleness gap the council flagged
# dissolves under per-request indexing). opencode never sees a meta call: when
# the model emits one, the proxy answers it and re-POSTs upstream until a real
# response, exactly like the malformed-tool-call retry loop above.


def _meta_tool_list(tools: Optional[List[Dict[str, Any]]]) -> str:
    """Render tool_list() output: one line per available tool — its signature
    plus the first line of its description. Pure function of the inbound tools
    array (the per-request registry)."""
    sigs = _extract_tool_signatures(tools, "")
    if not sigs:
        return "No tools available."
    by_desc: Dict[str, str] = {}
    for tool in tools or []:
        func = tool.get("function", {}) if "function" in tool else tool
        nm = func.get("name")
        if nm:
            by_desc[nm] = (func.get("description") or "").strip()
    lines = []
    for name, req, opt in sigs:
        sig = _format_tool_signature(name, req, opt)
        first = by_desc.get(name, "").split("\n", 1)[0].strip()
        lines.append(f"- {sig}" + (f" — {first}" if first else ""))
    return "Available tools:\n" + "\n".join(lines)


def _meta_tool_search(query: str, tools: Optional[List[Dict[str, Any]]]) -> str:
    """Render tool_search(query) output: full signature + description for tools
    whose name or description contains the query (case-insensitive). An empty
    query matches nothing (so a bare call doesn't dump the whole catalog —
    tool_list() is the explicit way to do that). Pure function of the inbound
    tools array."""
    q = (query or "").strip().lower()
    if not q:
        return "Provide a non-empty query, or call tool_list() to see all tools."
    matches = []
    for tool in tools or []:
        func = tool.get("function", {}) if "function" in tool else tool
        name = func.get("name")
        if not name:
            continue
        desc = func.get("description") or ""
        if q in name.lower() or q in desc.lower():
            req, opt = _split_schema_params(func.get("parameters") or {})
            block = f"Tool: {_format_tool_signature(name, req, opt)}"
            if desc.strip():
                block += f"\n{desc.strip()}"
            matches.append(block)
    if not matches:
        return f'No tools match "{query}". Call tool_list() to see all tools.'
    return "\n\n".join(matches)


def _is_meta_tool_call(payload: Any, real_tool_names: Iterable[str]) -> bool:
    """True when a parsed tool call is a synthetic meta-tool the proxy serves
    itself: its name is tool_search/tool_list AND that name is NOT a real tool
    this turn. The second clause is the safety yield — if opencode ever ships a
    real tool by one of these names, the real one wins and the proxy forwards
    it untouched."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("name")
    return name in _META_TOOL_NAMES and name not in set(real_tool_names or ())


def _run_meta_tool(payload: Dict[str, Any], tools: Optional[List[Dict[str, Any]]]) -> str:
    name = payload.get("name")
    args = payload.get("arguments")
    if name == "tool_list":
        return _meta_tool_list(tools)
    if name == "tool_search":
        query = args.get("query", "") if isinstance(args, dict) else ""
        return _meta_tool_search(query, tools)
    return ""


def _serve_meta_tools(
    req_id: str,
    original_messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tools_text: str,
    upstream_model: str,
    headers: Dict[str, str],
    assistant_text: str,
    meta_payloads: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], str, List[Dict[str, Any]], str]]:
    """Serve one or more synthetic meta-tool calls and continue the upstream
    conversation until the model emits a real (non-meta) response. The loop runs
    entirely in the proxy — opencode never sees the meta call or its result.

    Each round appends `[assistant(<meta call text>), user(<framed result>)]`
    to the conversation (the result wrapped in the same <<<BEGIN_TOOL_RESULT>>>
    markers a real tool result carries) and re-POSTs upstream. Bounded by
    `_META_TOOL_SERVE_BUDGET` so a model that only ever searches can't loop
    forever.

    Returns `(target_json, response_text, payloads, clean_text)` for the first
    non-meta response (real tool calls, prose, or empty — any stray meta call
    mixed into it is stripped), or `None` if an upstream call failed or the
    budget was exhausted still on meta calls. On `None` the caller drops the
    meta calls rather than forwarding an unrunnable tool to opencode."""
    real_names = _collect_tool_names(tools)
    convo = list(original_messages)
    text = assistant_text
    payloads = meta_payloads
    for attempt in range(1, _META_TOOL_SERVE_BUDGET + 1):
        results = []
        for p in payloads:
            out = _run_meta_tool(p, tools)
            results.append(
                f'<<<BEGIN_TOOL_RESULT name="{p.get("name")}">>>\n'
                f"{out}\n<<<END_TOOL_RESULT>>>"
            )
        convo = convo + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "\n\n".join(results)},
        ]
        translated = translate_history_and_apply_prompt(
            convo, tools_text, tools=tools
        )
        payload: Dict[str, Any] = {"model": upstream_model, "messages": translated}
        if _PROMPT_MODE == "passthrough" and tools:
            payload["tools"] = tools
        save_debug_file(req_id, "02", f"API_ToolSearch_Request_{attempt:02d}", payload)
        try:
            resp = _upstream_post(headers, payload)
        except requests.RequestException as e:
            print(f"[{req_id}] tool-search upstream request failed: {e}", flush=True)
            save_debug_file(req_id, "03", f"API_ToolSearch_Error_{attempt:02d}", {"error": str(e)})
            return None
        if resp.status_code >= 400:
            try:
                err_body: Any = resp.json()
            except Exception:
                err_body = resp.text
            print(f"[{req_id}] tool-search upstream returned {resp.status_code}: {err_body}", flush=True)
            save_debug_file(req_id, "03", f"API_ToolSearch_Error_{attempt:02d}", {"status": resp.status_code, "body": err_body})
            return None
        try:
            target_json = resp.json()
        except ValueError as e:
            print(f"[{req_id}] tool-search upstream returned non-JSON: {e}", flush=True)
            save_debug_file(req_id, "03", f"API_ToolSearch_Error_{attempt:02d}", {"error": "non-json", "body": resp.text})
            return None
        save_debug_file(req_id, "03", f"API_ToolSearch_Response_{attempt:02d}", target_json)
        text = extract_assistant_content(target_json)
        new_payloads, new_clean = extract_tool_calls_and_text(
            text, available_tool_names=real_names
        )
        meta = [p for p in new_payloads if _is_meta_tool_call(p, real_names)]
        if meta and len(meta) == len(new_payloads):
            # Still all meta — serve again next round (bounded by the budget).
            payloads = meta
            continue
        final_payloads = [
            p for p in new_payloads if not _is_meta_tool_call(p, real_names)
        ]
        return target_json, text, final_payloads, new_clean
    return None


def _select_rescue_tool(
    available_tool_names: Iterable[str],
) -> Optional[Dict[str, Any]]:
    """Pick a "dumb tool" call to emit alongside the empty-response rescue
    text so opencode treats the assistant turn as continuing (it sets
    `finish_reason: tool_calls`, executes the tool, and re-invokes the model
    with the tool result as the new recency — which displaces the filter-
    triggering content out of the hot slot). See architecture/proxy.md →
    "Empty-response detection" (issue #117).

    Searches the inbound tools for a `bash`-style name and, if found,
    returns a `{name, arguments}` payload that runs `pwd`. `bash`/`pwd` is
    the right rescue because (a) the shell tool is exposed for every
    coding agent, (b) `pwd` is read-only with no filesystem/network/state
    side effects, and (c) the one-line tool result is tiny and can't
    re-trigger the upstream filter. Returns `None` when no shell tool is
    available — caller falls back to the text-only rescue (the upstream
    still unsticks on the user's next prompt; just no auto-continuation).
    """
    for name in available_tool_names or ():
        if not isinstance(name, str):
            continue
        if name.lower() in _RESCUE_TOOL_NAME_PATTERNS:
            return {
                "name": name,
                "arguments": {
                    "command": _RESCUE_BASH_COMMAND,
                    "description": _RESCUE_BASH_DESCRIPTION,
                },
            }
    return None


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Response:
    return Response(json.dumps({"status": "ok"}), status=200, mimetype="application/json")


@app.route("/v1/models", methods=["GET"])
def list_models() -> Response:
    """Proxy the upstream's model catalog so opencode can discover and list
    every available model (the agent entrypoint reads this to build the
    opencode model dropdown).

    This is a thin pass-through: forward GET {base}/v1/models upstream with the
    bearer key and the same verify=False the chat path uses, then return the
    upstream status and body verbatim. Returning the upstream body (not just a
    parsed list) means a locked-key 401 — with its unlock_url — reaches the
    caller unchanged, so the same unlock flow the chat path triggers also
    applies here. Declared as an explicit route so it wins over catch_all,
    which would otherwise treat the GET as a (body-less) chat request.
    """
    req_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # The chatgpt backend-api has no catalog endpoint. Synthesize a one-entry
    # list from CHATGPT_MODEL_NAME so opencode's model dropdown (built from
    # this route) offers exactly the model that backend will actually serve.
    if PROXY_BACKEND == "chatgpt":
        body = {
            "object": "list",
            "data": [{"id": CHATGPT_MODEL_NAME, "object": "model", "owned_by": "chatgpt"}],
        }
        print(f"[{req_id}] GET /v1/models -> synthesized chatgpt catalog", flush=True)
        return Response(json.dumps(body), status=200, mimetype="application/json")
    headers = {"Authorization": f"Bearer {PROXY_API_KEY}"}
    try:
        resp = requests.get(
            MODELS_URL,
            headers=headers,
            verify=False,
            timeout=PROXY_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[{req_id}] upstream models request failed: {e}", flush=True)
        return Response(
            json.dumps({"error": "upstream models request failed", "details": str(e)}),
            status=502,
            mimetype="application/json",
        )
    print(f"[{req_id}] GET /v1/models -> upstream {resp.status_code}", flush=True)
    return Response(
        resp.text,
        status=resp.status_code,
        mimetype=resp.headers.get("Content-Type", "application/json"),
    )


def _client_error(message: str, status: int) -> Response:
    """Error response in the `{"error": {"message": ...}}` envelope the AI SDK
    parses (only `message` is required; it is surfaced to opencode)."""
    body: Dict[str, Any] = {"error": {"message": message, "type": "proxy_error"}}
    return Response(json.dumps(body), status=status, mimetype="application/json")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def catch_all(path: str) -> Response:
    req_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # opencode (and any OpenAI-compatible client) hits /v1/chat/completions.

    try:
        inbound_request = request.get_json(silent=True) or {}
        save_debug_file(req_id, "01", "Inbound_Request", inbound_request)

        model_name = inbound_request.get("model") or DEFAULT_MODEL_NAME
        original_messages = inbound_request.get("messages") or []
        tools = inbound_request.get("tools") or []
        # OpenAI semantics: default non-streaming when `stream` is absent. The
        # AI SDK always sends stream:true, so opencode streams; a bare curl gets
        # a single JSON object.
        want_stream = bool(inbound_request.get("stream", False))

        print(f"[{req_id}] {request.method} /{path} model={model_name} messages={len(original_messages)} tools={len(tools)}", flush=True)

        tools_text = format_tools_to_text(tools)
        translated = translate_history_and_apply_prompt(original_messages, tools_text, tools=tools)

        # Catalog-size instrumentation (council gate-trigger). `tools` ≈ recency
        # line count (one entry per tool); `schema_tokens` is what the full
        # schemas at the stable prefix cost. Logged each request so the decision
        # to migrate schemas behind tool-search is a measured number, not a
        # guess. See architecture/proxy.md "Catalog-size instrumentation".
        print(
            f"[{req_id}] catalog: tools={len(_collect_tool_names(tools))} "
            f"schema_tokens={_estimate_tokens(tools_text)}",
            flush=True,
        )

        # Forward whatever model the request asked for so the user can switch
        # between the upstream's models from opencode. opencode reads the bare
        # ids the upstream advertised on /v1/models, so the id passes through
        # verbatim; fall back to DEFAULT_MODEL_NAME only when the request omits
        # a model entirely.
        upstream_model = model_name or DEFAULT_MODEL_NAME
        upstream_payload = {
            "model": upstream_model,
            "messages": translated,
        }
        # Passthrough mode forwards the agent's tool definitions to upstream
        # as-is so the conversation actually contains tool schemas (rather
        # than the proxy's cooperative-prompt markdown injection). The
        # benchmark control's value is measuring what harness contributes
        # by stripping the mediation; the schemas the agent provides are
        # OpenAI-format function tools, which many upstreams won't honor on
        # this endpoint — that mismatch IS the data point.
        if _PROMPT_MODE == "passthrough" and tools:
            upstream_payload["tools"] = tools
        save_debug_file(req_id, "02", "API_Request", upstream_payload)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {PROXY_API_KEY}",
        }

        try:
            resp = _upstream_post(headers, upstream_payload)
        except requests.RequestException as e:
            print(f"[{req_id}] upstream request failed: {e}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"error": str(e)})
            return _client_error(f"upstream request failed: {e}", 502)

        if resp.status_code >= 400:
            err_body: Any
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            print(f"[{req_id}] upstream returned {resp.status_code}: {err_body}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"status": resp.status_code, "body": err_body})
            return _client_error(f"upstream returned status {resp.status_code}", 502)

        try:
            target_json = resp.json()
        except ValueError as e:
            print(f"[{req_id}] upstream returned non-JSON: {e}", flush=True)
            save_debug_file(req_id, "03", "API_Error", {"error": "non-json", "body": resp.text})
            return _client_error(f"upstream returned non-JSON: {e}", 502)

        save_debug_file(req_id, "03", "API_Response", target_json)

        response_text = extract_assistant_content(target_json)
        # Names currently exposed to the model — used to gate the
        # missing-`arguments` lift inside `extract_tool_calls_and_text`
        # (see its docstring; issue #118).
        tool_call_payloads, clean_text = extract_tool_calls_and_text(
            response_text,
            available_tool_names=_collect_tool_names(tools),
        )

        # In-proxy retry for malformed tool-call attempts (issue #121).
        # When extraction yielded zero tool calls AND the response looks
        # like a botched tool-call attempt — either a ```json fence
        # opener with no parseable body (the `\`\`\`json_parse_or_id:
        # todowrite}` shape that ended the turn silently), or a complete
        # JSON object whose strings carry invalid `\escape` characters
        # that `json.loads(..., strict=False)` rejects — append a
        # corrective `[assistant(<bad>), user(<correction>)]` pair to the
        # conversation and re-POST upstream ONCE. The retry is invisible
        # to opencode: if it produces a valid tool call (or any other
        # non-malformed response), the retry result replaces the bad
        # attempt and only the retry result is streamed back. If the
        # retry fails or produces ANOTHER malformed attempt, the original
        # bad response falls through to the bleed/empty-rescue path
        # below — the retry is strictly additive.
        if not tool_call_payloads:
            kind = _diagnose_failed_tool_call(response_text)
            if kind is not None:
                retry_result = _retry_upstream_with_correction(
                    req_id,
                    original_messages,
                    tools,
                    tools_text,
                    upstream_model,
                    headers,
                    response_text,
                    kind,
                )
                if retry_result is not None:
                    retry_json, retry_text, retry_payloads, retry_clean = retry_result
                    # "Recovered" = the retry produced valid tool calls,
                    # OR a response that does NOT itself diagnose as a
                    # botched tool-call attempt. Pure prose, empty content
                    # (handed off to the empty-response rescue below), or
                    # a healthy tool call all count.
                    if retry_payloads or _diagnose_failed_tool_call(retry_text) is None:
                        target_json = retry_json
                        response_text = retry_text
                        tool_call_payloads = retry_payloads
                        clean_text = retry_clean
                        recovered = True
                    else:
                        recovered = False
                else:
                    recovered = False
                print(
                    f"[{req_id}] in-proxy retry for malformed tool call "
                    f"(kind={kind}); attempt=1, "
                    f"recovered={'yes' if recovered else 'no'}",
                    flush=True,
                )

        # Cooperative tool-search (default off; HARNESS_TOOL_SEARCH=1). When the
        # model calls a synthetic meta-tool (tool_search/tool_list), the proxy
        # serves it from the per-request tool registry and continues upstream
        # until a real response — opencode never sees the meta call. A meta call
        # is NEVER forwarded to opencode (it has no such tool): if serving fails
        # it is dropped, and a turn that mixed meta + real calls keeps only the
        # real ones. See architecture/proxy.md "Cooperative tool-search".
        if _TOOL_SEARCH_ENABLED and tool_call_payloads:
            real_names = _collect_tool_names(tools)
            meta_payloads = [
                p for p in tool_call_payloads if _is_meta_tool_call(p, real_names)
            ]
            if meta_payloads and len(meta_payloads) == len(tool_call_payloads):
                served = _serve_meta_tools(
                    req_id, original_messages, tools, tools_text,
                    upstream_model, headers, response_text, meta_payloads,
                )
                if served is not None:
                    target_json, response_text, tool_call_payloads, clean_text = served
                    print(
                        f"[{req_id}] served cooperative tool-search; continued "
                        f"to real response (tool_calls={len(tool_call_payloads)})",
                        flush=True,
                    )
                else:
                    tool_call_payloads = []
                    print(
                        f"[{req_id}] cooperative tool-search did not resolve; "
                        f"dropped meta call(s)",
                        flush=True,
                    )
            elif meta_payloads:
                tool_call_payloads = [
                    p for p in tool_call_payloads if p not in meta_payloads
                ]

        # Empty-response rescue (issue #117). Some upstreams silently
        # short-circuit before generation — well-formed JSON, finish_reason
        # "stop", zero completion tokens, no thinking, no safety fields —
        # when something in the **most-recent** message slot (typically a
        # large/repetitive tool result) trips an internal content filter.
        # Without intervention, opencode sees "no content + done" and stops
        # the turn; the user types "continue" and the same content is still
        # the recency, so the same empty response keeps coming back.
        #
        # The fix: if a shell tool is available in the inbound tools
        # (`bash`/`Bash`), emit a no-op call to it (running `pwd`) — that
        # forces `finish_reason: tool_calls`, so opencode executes the tool
        # and re-invokes the model with the tool result as the new recency,
        # which displaces the filter-triggering content out of the hot slot
        # and the next turn proceeds normally. The tool call alone carries
        # the rescue: the assistant text is left empty (a tool-only turn is
        # well-formed), so no content-free filler line lands in the
        # transcript. ONLY when no shell tool is exposed do we fall back to a
        # minimal assistant text ("Understood.") so the response isn't empty
        # — that still unsticks the upstream on the user's next prompt, just
        # without the auto-continuation (the stall the user saw in #117).
        if not clean_text.strip() and not tool_call_payloads:
            finish_reason = _extract_finish_reason(target_json)
            rescue_payload = _select_rescue_tool(_collect_tool_names(tools))
            if rescue_payload is not None:
                tool_call_payloads = [rescue_payload]
                rescue_mode = f"tool({rescue_payload['name']})"
            else:
                rescue_mode = "text-only"
                clean_text = _empty_response_rescue_text()
            print(
                f"[{req_id}] upstream returned empty content "
                f"(finish_reason={finish_reason or 'unknown'}); "
                f"substituting rescue [{rescue_mode}]",
                flush=True,
            )

        # Compute prompt_tokens from the translated conversation directly.
        # Upstream's `prompt_tokens` is unreliable for context tracking against
        # this provider — observed behavior (count not growing monotonically with
        # conversation length, occasionally shrinking) suggests server-side
        # sliding-window truncation. The agent needs a count that reflects the
        # full conversation it sent, not what the upstream charged for. Always
        # estimate locally from the full translated array so the agent's context
        # bar grows monotonically with conversation length.
        # Flatten per message: in passthrough mode `translated` is the inbound
        # array verbatim, so a multimodal/list content block would otherwise
        # blow up this join (TypeError) and 500 the turn.
        joined = "\n".join(
            _flatten_content_to_str(m.get("content", "") or "") for m in translated
        )
        upstream_usage = target_json.get("usage") or {}
        usage = {
            "prompt_tokens": _estimate_tokens(joined),
            "completion_tokens": (
                upstream_usage.get("completion_tokens")
                or _estimate_tokens(response_text)
            ),
        }

        # Emit OpenAI chat-completions. The upstream call already completed
        # fully before emission (the proxy isn't streaming from upstream; it
        # gets the full response, then translates), so materializing-then-
        # yielding doesn't change latency. Chunks are materialized so they can
        # be dumped to debug output before streaming; memory cost is the
        # response size — a few KB for typical responses.
        if want_stream:
            sse_chunks = list(generate_openai_sse(model_name, clean_text, tool_call_payloads, usage))
            save_debug_file(req_id, "04", "OpenAI_SSE_Response", {"chunks": sse_chunks})
            print(f"[{req_id}] upstream OK; emitting OpenAI SSE (tool_calls={len(tool_call_payloads)})", flush=True)
            return app.response_class(iter(sse_chunks), mimetype="text/event-stream")
        body = build_openai_response(model_name, clean_text, tool_call_payloads, usage)
        save_debug_file(req_id, "04", "OpenAI_Response", body)
        print(f"[{req_id}] upstream OK; emitting OpenAI JSON (tool_calls={len(tool_call_payloads)})", flush=True)
        return Response(json.dumps(body), status=200, mimetype="application/json")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[{req_id}] FATAL: {e}\n{tb}", flush=True)
        save_debug_file(req_id, "99", "Fatal_Error", {"error": str(e), "traceback": tb})
        return _client_error(f"proxy internal error: {e}", 500)


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
    if PROXY_BACKEND not in ("openai", "chatgpt"):
        print(
            f"[!] FATAL: unknown PROXY_BACKEND '{PROXY_BACKEND}' "
            "(expected 'openai' or 'chatgpt')",
            flush=True,
        )
        sys.exit(1)

    missing = []
    if PROXY_BACKEND == "chatgpt":
        # Cookie auth against a fixed stream endpoint; the openai-backend
        # PROXY_API_* / DEFAULT_MODEL_NAME trio is unused and not required.
        if not CHATGPT_BASE_URL:
            missing.append("CHATGPT_BASE_URL")
        if not CHATGPT_MODEL_NAME:
            missing.append("CHATGPT_MODEL_NAME")
        if not CHATGPT_COOKIE_STRING:
            missing.append("CHATGPT_COOKIE_STRING")
        # passthrough forwards the raw `tools` array and leaves the history
        # structured. The backend-api takes neither: it has no tools field and
        # one text part, so every tool schema and every tool result would be
        # dropped on the floor and the run would look like it worked. Refuse
        # instead of producing quietly wrong output.
        if _PROMPT_MODE == "passthrough":
            print(
                "[!] FATAL: PROXY_PROMPT_MODE=passthrough is not supported on the chatgpt "
                "backend (it has no tools field; tool schemas and tool results would be "
                "silently dropped)",
                flush=True,
            )
            sys.exit(1)
    else:
        if not PROXY_API_URL:
            missing.append("PROXY_API_URL")
        if not PROXY_API_KEY:
            missing.append("PROXY_API_KEY")
        if not DEFAULT_MODEL_NAME:
            missing.append("DEFAULT_MODEL_NAME")
    if missing:
        print(f"[!] FATAL: required env vars missing or empty: {', '.join(missing)}", flush=True)
        sys.exit(1)

    # Defense-in-depth for containerless host mode, which has no egress firewall
    # and fronts the upstream API key with no auth of its own. `harness host`
    # sets HARNESS_FORCE_LOOPBACK=1; when set, refuse to bind anything but a
    # loopback address, so a regression in the launch path can never expose the
    # keyed proxy on the LAN. Container mode does not set it (it binds 0.0.0.0
    # behind the firewall on purpose).
    force_loopback = os.environ.get("HARNESS_FORCE_LOOPBACK", "").strip().lower() in ("1", "true", "yes")
    if force_loopback and PROXY_HOST not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"[!] FATAL: HARNESS_FORCE_LOOPBACK is set but PROXY_HOST='{PROXY_HOST}' is not a "
            "loopback address; refusing to expose the host-mode proxy off-box",
            flush=True,
        )
        sys.exit(1)


def _force_utf8_stdio() -> None:
    """Make stdout/stderr encode as UTF-8 so the proxy never dies printing a
    non-ASCII character.

    The proxy prints a U+2192 arrow in its startup banner (`sys→user:`) and, on
    the error paths, echoes upstream error bodies, model names, and tracebacks
    that can hold arbitrary Unicode. On Windows a redirected/piped stdout (host
    mode `nohup`s the proxy to a logfile, so it is not a console) defaults to the
    legacy cp1252 code page, which cannot encode `→` and much else; `print`
    raised UnicodeEncodeError and killed the proxy at startup. UTF-8 encodes
    every code point, so this removes the whole crash class; `backslashreplace`
    keeps even a stray surrogate from ever raising on a log write. No-op where
    the stream is already UTF-8 or predates `reconfigure` (Python < 3.7)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass


def main() -> None:
    global _OUTPUT_DIR

    _force_utf8_stdio()
    _validate_config()
    _OUTPUT_DIR = init_output_dir()
    _setup_prompt_mode()
    _setup_host_os()
    _setup_run_mode()
    _setup_mcp_tool_recency()
    _setup_state_check_tools()
    _setup_tool_search()
    _setup_tool_guidance()
    _setup_reminder_template()

    raw_output = os.environ.get("OUTPUT_DIR", "").strip()
    if not raw_output:
        output_status = "disabled (OUTPUT_DIR not set)"
    elif _OUTPUT_DIR is None:
        output_status = f"disabled ('{raw_output}' not writable)"
    else:
        output_status = f"enabled at '{_OUTPUT_DIR}'"

    # Backend-aware banner lines. The chatgpt cookie is never partially
    # printed the way _redact_key prints a bearer key: a cookie's leading
    # bytes carry session structure, so only its length is reported.
    if PROXY_BACKEND == "chatgpt":
        banner_chat_url = CHATGPT_STREAM_URL
        banner_models_url = "(synthesized from CHATGPT_MODEL_NAME)"
        banner_auth = f"   upstream cookie: (set, {len(CHATGPT_COOKIE_STRING)} chars)\n"
    else:
        banner_chat_url = CHAT_URL
        banner_models_url = MODELS_URL
        # Label kept verbatim: tests/proxy_test.sh greps for "upstream key:"
        # both to confirm the redaction AND, negatively, to catch a raw key
        # leak. Renaming it silently disarms the second grep.
        banner_auth = f"   upstream key:   {_redact_key(PROXY_API_KEY)}\n"

    print(
        "============================================================\n"
        " harness translating proxy\n"
        f"   listening on:   {PROXY_HOST}:{PROXY_PORT}\n"
        f"   backend:        {PROXY_BACKEND}\n"
        f"   chat URL:       {banner_chat_url}\n"
        f"   models URL:     {banner_models_url}\n"
        f"   default model:  {DEFAULT_MODEL_NAME}\n"
        f"{banner_auth}"
        f"   timeout:        {PROXY_TIMEOUT}s\n"
        f"   prompt mode:    {_PROMPT_MODE}\n"
        f"   sys→user:       {_CHANGE_SYSTEM_TO_USER}\n"
        f"   detail tools:   {', '.join(_HYBRID_DETAIL_TOOLS) or '(none)'}\n"
        f"   mcp recency:    {len(_MCP_TOOL_RECENCY)} tool(s)\n"
        f"   state-check:    {len(_MCP_STATE_CHECK_TOOLS)} tool(s)\n"
        f"   tool-search:    {'on' if _TOOL_SEARCH_ENABLED else 'off'}\n"
        f"   host OS:        {_HOST_OS or '(unknown)'}\n"
        f"   reminder:       {_reminder_template_path()}\n"
        f"   tool guidance:  {_tool_guidance_path()}\n"
        f"   debug dumps:    {output_status}\n"
        "============================================================",
        flush=True,
    )

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)


if __name__ == "__main__":
    main()
