# `proxy/proxy.py` — translating proxy

A Flask app that translates between ollama's `/api/chat` wire format and
the upstream's chat-completions format, AND injects cooperative tool-use
prompts so models that don't natively support tool calls can produce them
as ```json blocks that the proxy parses back into native `tool_calls`.

Single-process, single file, ~985 lines.

## Request lifecycle

```
ollama ──/api/chat──▶ catch_all() ──translate_history_and_apply_prompt()──▶
                                  ──POST upstream──▶
                                  ──extract_tool_calls_and_text()──▶
                                  ──generate_ndjson()──▶ ollama
```

`catch_all(path)` is the single Flask route handler that owns every
non-health request. It:

1. Reads the ollama JSON body (`model`, `messages`, `tools`).
2. Builds a tools-as-text string via `format_tools_to_text`.
3. Calls `translate_history_and_apply_prompt` to flatten the ollama
   conversation into a single-role-alternating array for the upstream
   and to inject the cooperative tool-use scaffolding into whichever
   message the prompt-mode dictates.
4. POSTs `{model: PROXY_API_MODEL, messages: translated}` to
   `PROXY_API_URL` with a `Bearer PROXY_API_KEY` header,
   `verify=False` (the upstream uses a self-signed cert), and
   `timeout=PROXY_TIMEOUT`.
5. Extracts assistant content from the upstream response
   (`extract_assistant_content`).
6. Parses ```json tool-call blocks out of the assistant content
   (`extract_tool_calls_and_text`).
7. Emits NDJSON chunks matching ollama's streaming contract
   (`generate_ndjson` + `make_chunk`).

Errors return 502 with a structured body and a debug dump under
`OUTPUT_DIR`.

## Cooperative-prompt modes (`PROXY_PROMPT_MODE`)

The upstream doesn't natively support tool calls, so the proxy injects a
scaffold that tells the model to emit ```json blocks of the form
`{"name": "...", "arguments": {...}}`. Five modes live as separate
`build_cooperative_prompt_*` functions. The validator in `_setup_prompt_mode`
accepts:

- **`user_front`** (default) — full scaffolding on the last user message,
  request placed BEFORE the tool list. Avoids burying a ~10–15K-token tool
  schema between the model and the user's actual question.
- **`user_bookend`** — like `user_front`, but the request is repeated
  AFTER the tool list, wrapped in `<<<BEGIN_USER_REQUEST>>>` markers.
  Highest reliability at the cost of a duplicated payload.
- **`user`** — legacy: scaffolding + tool list re-injected, request at
  the END.
- **`system`** — scaffolding lives in the system message; user turns pass
  through unchanged. Cheapest. Some upstreams treat system content as
  background and don't reliably emit tool calls.
- **`hybrid`** — full tools in the system message + a ~50-token reminder
  on the last user message.

Invalid values fall back to `user_front` with a warning.

## Tool-result delimiting

`role:"tool"` messages are wrapped — content **verbatim**, never parsed —
in `<<<BEGIN_TOOL_RESULT name="…">>>` / `<<<END_TOOL_RESULT>>>` markers at
translation time, so a tool result is unambiguously bounded whether it is
the live turn or buried in history. The `name` is resolved from metadata,
never from the content: an explicit `tool_name` / `name` field if present
(opencode, ollama), else the `tool_call_id` correlated against the
originating assistant `tool_calls` (Claude Code sends results keyed by id,
not name), else positional order, else `unknown_tool`. harness serves both
opencode and Claude Code, which format tool output differently; wrapping
rather than parsing keeps the proxy agnostic to either shape.

On a tool-result turn the `user` / `user_front` / `user_bookend` modes use
tool-variant builders (`build_cooperative_prompt_tool*`) that inject a
framing line — "this block is tool output, not a user message; continue
the task" — around the already-delimited result, instead of the
`<<<BEGIN_USER_REQUEST>>>` wrapper used for genuine user turns. `tool_front`
additionally closes with a one-line "now act" cue so the recency slot is an
instruction rather than raw schema. The `system` / `hybrid` modes leave the
marker-wrapped result as the user message and keep the scaffold in the
system message.

## `_CHANGE_SYSTEM_TO_USER`

Default ON. Some upstreams silently drop the `system` role; the
translator converts the system message(s) into a user message at the
start, with a stub assistant message between to satisfy strict
role-alternation. Failure mode of the bug is invisible (model just
ignores system instructions), so the default has to be defensive. Set to
`0` to disable for upstreams that honor system roles.

## Tool-call extraction

`extract_tool_calls_and_text` walks the response left-to-right looking
for ```json fences and uses `_scan_balanced_json` (NOT regex) to find the
matching closing brace. The regex this replaces failed when tool-call
argument strings contained backticks or nested code fences — the lazy
match terminated on the first inner ``` instead of the outer one,
truncating the JSON. The balanced scanner tracks string boundaries and
backslash escapes so it walks past LLM-emitted argument strings
containing markdown.

Multiple `{name, arguments}` blocks per response are normal — real
upstreams (Gemini Enterprise, claude-3.5-sonnet variants) emit parallel
tool calls when the task naturally calls for them. Order is preserved.
Blocks that fail to parse, aren't dicts, or are missing `name`/`arguments`
are LEFT in the text — `clean_text` contains them as-is. (The LLM may
have been describing JSON, not asking to invoke a tool.)

## NDJSON streaming

`generate_ndjson` yields ollama-compatible streaming chunks: a sequence
of `{message: {role, content}}` chunks ending in `done: true`. The chunks
are materialized into a list first (`list(generate_ndjson(...))`) and
dumped to `OUTPUT_DIR` before being streamed back to ollama. Memory cost
is the response size — a few KB for typical tool-call responses. Latency
is unchanged: the upstream call already completed fully before NDJSON
generation began (the proxy is NOT streaming from upstream — it gets the
full response, then translates).

## Local token estimation

Upstream `prompt_tokens` is unreliable for context tracking — observed
behavior (count not growing monotonically with conversation length,
occasionally shrinking) suggests server-side sliding-window truncation.
The agent's context bar needs a count that grows monotonically with the
conversation it actually sent. `_estimate_tokens` is called on the
joined translated messages so the agent always sees a stable count.
`completion_tokens` is taken from upstream if present, else estimated.

## Config and validation

Module-level env reads at startup:

```
PROXY_HOST=0.0.0.0          PROXY_PORT=8000
PROXY_API_URL (REQUIRED)    PROXY_API_KEY (REQUIRED)    PROXY_API_MODEL (REQUIRED)
PROXY_TIMEOUT=180           OUTPUT_DIR (optional)
OLLAMA_CONTEXT_LENGTH=200000
```

`_validate_config` in `main()` enforces the three REQUIRED values are
non-empty and `PROXY_API_URL` parses; the process exits with a clear
message if not. `_redact_key` is used in startup logging so logs show
something like `sk-abc...xyz`.

`_setup_prompt_mode` and `_setup_change_system_to_user` read their env
vars from the same `main()` startup path and stash the resolved values
as module globals.

## Debug dumps

When `OUTPUT_DIR` is set (typical: `/output` inside the container,
bind-mounted to `<install-root>/state/output/` on the host), each request
writes:

- `<req_id>_01_Ollama_Request.json`
- `<req_id>_02_API_Request.json`
- `<req_id>_03_API_Response.json` or `_03_API_Error.json`
- `<req_id>_04_NDJSON_Response.json`
- `<req_id>_99_Fatal_Error.json` on unhandled exception

`req_id` is the timestamp at request start (`%Y%m%d_%H%M%S_%f`).

## Tests

`tests/proxy_test.sh` (integration via the proxy container) and
`proxy/test_proxy.py` (unit tests, run directly in the proxy container).
See [`tests.md`](tests.md).

## Iteration loop

`docker compose --project-name harness restart proxy` picks up edits in
~10–15 s without touching ollama / agents / MCPs. Faster than
`harness restart` for the proxy-iteration loop.
