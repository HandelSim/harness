# `proxy/proxy.py` — translating proxy

A Flask app that translates between ollama's `/api/chat` wire format and
the upstream's chat-completions format, AND injects cooperative tool-use
prompts so models that don't natively support tool calls can produce them
as ```json blocks that the proxy parses back into native `tool_calls`.

Single-process, single file, ~1,130 lines.

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
4. POSTs `{model: <requested>, messages: translated}` to the derived chat
   endpoint `{base}/v1/chat/completions` (see [URL base + model
   passthrough](#url-base--model-passthrough)) with a `Bearer PROXY_API_KEY`
   header, `verify=False` (the upstream uses a self-signed cert), and
   `timeout=PROXY_TIMEOUT`. `<requested>` is the inbound ollama model with any
   `:latest` tag stripped — i.e. passthrough — falling back to
   `DEFAULT_MODEL_NAME` only when the request omits a model.
5. Extracts assistant content from the upstream response
   (`extract_assistant_content`).
6. Parses ```json tool-call blocks out of the assistant content
   (`extract_tool_calls_and_text`).
7. Emits NDJSON chunks matching ollama's streaming contract
   (`generate_ndjson` + `make_chunk`).

Errors return 502 with a structured body and a debug dump under
`OUTPUT_DIR`.

## URL base + model passthrough

`PROXY_API_URL` is a **base**, not a full endpoint. `_normalize_api_base`
strips a trailing `/v1/chat/completions`, `/chat/completions`, or `/v1` (so a
base, an OpenAI-style `/v1` base, or a legacy full chat URL all work), and the
module derives two endpoints once at import: `CHAT_URL` =
`{base}/v1/chat/completions` and `MODELS_URL` = `{base}/v1/models`.

`catch_all` forwards the **requested** model to upstream rather than a fixed id:
`_strip_model_tag(model_name) or DEFAULT_MODEL_NAME`. ollama registers its stubs
as `<id>:latest` and forwards that tagged name; `_strip_model_tag` removes only a
`:latest` suffix (a real upstream id may contain a colon, so an arbitrary tag is
not stripped). This is what lets a user switch between the upstream's models from
opencode — the selected model flows opencode → ollama → proxy → upstream
unchanged. `DEFAULT_MODEL_NAME` is only the fallback when a request carries no
model.

The `GET /v1/models` route is a thin pass-through: it forwards `MODELS_URL` with
the bearer key and `verify=False` and returns the upstream status + body
verbatim, so a locked-key `401` (with its `unlock_url`) reaches the caller
unchanged. It's declared as an explicit Flask route so it wins over `catch_all`.
ollama's entrypoint consumes it at startup to discover and register a stub per
upstream model (see [`containers.md`](containers.md)).

## Cooperative-prompt modes (`PROXY_PROMPT_MODE`)

The upstream doesn't natively support tool calls, so the proxy injects a
scaffold that tells the model to emit ```json blocks of the form
`{"name": "...", "arguments": {...}}`. Two cooperative modes live as
separate `build_cooperative_prompt_*` functions, plus one bypass mode.

`PROXY_PROMPT_MODE` is **not a user `.env` knob**. The proxy defaults to
`hybrid` and `docker-compose.yml` no longer interpolates the var (so a stale
`.env` can't silently override the default). It is still honored from the
proxy's *container* env so all three modes stay reachable for benchmarking and
power use: `harness start/restart --prompt-mode <mode>` injects it ephemerally
via the runtime override (see [`harness-cli.md`](harness-cli.md)), and the
benchmark stack supplies it through its own compose overrides / the same flag
(see [`tests.md`](../tests/INVENTORY.md) and `tests/benchmarks/`). The
validator in `_setup_prompt_mode` accepts:

- **`user_front`** — full scaffolding on the last user message,
  request placed BEFORE the tool list. Avoids burying a ~10–15K-token tool
  schema between the model and the user's actual question.
- **`hybrid`** (default) — full tool definitions appended to the system message
  (which `_CHANGE_SYSTEM_TO_USER` then folds into the user-role message at
  index 0, the "stable prefix" position). A short recency reminder is
  prepended to the last user message, organised as four labelled lines —
  **Tools** (JSON envelope, no-fabricated-results rule, a pointer back to
  `<<<BEGIN_AGENT_TOOLS>>>` for full descriptions), **Workflow** (prefer a
  listed tool over hand-work — e.g. `webfetch` over curl/a script — keep a
  live plan with `todowrite`/`todoread`, launch `task` agents several
  concurrently to parallelise and conserve context), **Honesty**
  (anti-fabrication: no invented names/paths/signatures/citations), and
  **Environment** (the proxy runs in a Linux container with the working
  directory mounted from the host — host OS named when known, see
  [Host-OS injection](#host-os-injection) — so reproducible setup must live
  in the working directory, not the container). The line closes with each
  tool's parameter signature (`name(required, [optional])` per tool). The
  signature list — not just bare
  names — is the recency anchor for the parameter keys models most often
  guess wrong (e.g. calling `read({"filename": ...})` instead of
  `read({"filePath": ...})`, or omitting opencode's `bash` required
  `description`). The signature list carries parameter *keys* but not their
  *values*; for a small configurable set of "detail tools" whose valid
  values are a closed set opencode documents only in description prose (a
  `task`'s agent types, a `skill`'s skill names) the reminder additionally
  echoes the tool's description (whole for `skill`; pared to the agent-list
  section for `task`) — see [Hybrid delimiters](#hybrid-delimiters). Hybrid additionally delimits
  three content categories so each is addressable by name and the model
  can't conflate them with the upstream gateway's own system prompt/tools —
  again see [Hybrid delimiters](#hybrid-delimiters) below.
- **`passthrough`** — benchmark control. Skips every harness-side
  mediation: no cooperative-prompt injection, no system→user rewrite, no
  history translation. `translate_history_and_apply_prompt` short-circuits
  to `[dict(m) for m in original_messages]`, and `catch_all` forwards
  `tools` to upstream verbatim (other modes never set `tools` on the
  upstream payload). Use this to measure what harness's mediation
  contributes — the request the model sees is the request the agent sent.
  Note that ollama-format tool schemas typically aren't honored by
  non-ollama upstreams, so this mode often results in the model not using
  tools at all; that mismatch IS the data point.

Invalid or absent values fall back to `hybrid` with a warning — including the
legacy `user_front`/`user` value a stale `.env` might still carry, since the
var is no longer fed to the proxy from `.env`. Three older modes (`user`,
`system`, `user_bookend`) were removed in the hybrid-consolidation refactor
(issue #64): `user` was dominated by `user_front` (burying the request after
tool schemas), `system` had no recency anchor and degraded on long
conversations, and `user_bookend` anchored the user's request rather than tool
attention — request primacy isn't the failure mode in an agent loop where the
live request already sits at `messages[-1]`. Any unrecognised value now falls
back to `hybrid`.

## Tool-result delimiting

`role:"tool"` messages are wrapped — content **verbatim**, never parsed —
in `<<<BEGIN_TOOL_RESULT name="…">>>` / `<<<END_TOOL_RESULT>>>` markers at
translation time, so a tool result is unambiguously bounded whether it is
the live turn or buried in history. The `name` is resolved from metadata,
never from the content: an explicit `tool_name` / `name` field if present
(opencode, ollama), else the `tool_call_id` correlated against the
originating assistant `tool_calls` (some agents send results keyed by id,
not name), else positional order, else `unknown_tool`. Agents format tool
output differently; wrapping rather than parsing keeps the proxy agnostic
to any shape.

On a tool-result turn `user_front` uses `build_cooperative_prompt_tool_front`,
which injects a framing line — "this block is tool output, not a user
message; continue the task" — around the already-delimited result, then
the tool list, then a one-line "now act" cue so the recency slot is an
instruction rather than raw schema. `hybrid` leaves the marker-wrapped
result as the user message, keeps the scaffold on the stable prefix, and
still appends the recency reminder (so the tool-result turn also sees the
JSON-envelope reminder and the per-tool signature list).

## Hybrid delimiters

`hybrid` mode (and ONLY hybrid) additionally wraps three content categories
in `<<<BEGIN_X>>>` / `<<<END_X>>>` markers so each section is addressable by
name. The failure pattern this targets: the upstream gateway injects its own
system prompt mentioning its own tools/subagents, and the model conflates
harness's injected tools with those — or, when the user says "the first
message" / "the tool definitions", can't tell which section is meant. The
markers are applied in the hybrid dispatch branch of
`translate_history_and_apply_prompt`; `user_front` and `passthrough` never
emit them, and `<<<BEGIN_USER_REQUEST>>>` stays exclusive to `user_front`.

- **`<<<BEGIN_AGENT_INSTRUCTIONS>>>`** — wraps the inbound opencode
  system prompt (`messages[0]` content as it arrives), applied
  BEFORE harness's tool block is appended. Skipped when that content is
  empty/whitespace-only, and absent entirely when there was no inbound
  system message.
- **`<<<BEGIN_AGENT_TOOLS>>>`** — wraps the harness-injected tool
  definitions inside `build_cooperative_prompt_system_addition`, replacing
  the old `### Available Tools` header. A disambiguation sentence inside the
  wrap tells the model these are the only valid tools and to ignore any
  competing tool names from elsewhere in the prompt. The format-spec block
  above it (the JSON-envelope instructions) stays outside the wrap.
- **`<<<BEGIN_USER_MESSAGE>>>`** — wraps every real user-role turn (original
  role `user`). Tool-result-converted-to-user messages are detected by their
  `<<<BEGIN_TOOL_RESULT` marker and skipped — they keep only the TOOL_RESULT
  delimiters. On the latest turn the recency reminder is prepended OUTSIDE
  this wrap (it's proxy stage-direction, not user text).

The reminder (`build_cooperative_prompt_hybrid_reminder`) Tools line points
at `<<<BEGIN_AGENT_TOOLS>>>` for **full tool descriptions only** so that when
attention to `messages[0]` dilutes on long conversations the model still has
a named target to retrieve. It deliberately does NOT claim that section is
where to find parameter-*value* constraints (a `task`'s agent types, a
`skill`'s names): those now reach recency in the TOOL_DETAIL blocks below, so
an earlier "e.g. which agent types are valid for `task`" parenthetical here
was removed as misleading. This is additive — token cost is ~150–250
tokens/turn with the labelled lines + Environment context; hybrid's
lighter-than-user_front recency profile is preserved.

- **`<<<BEGIN_TOOL_DETAIL name="…">>>`** — recency-only (it lives in the
  reminder, not at the stable prefix). For each tool in the project-managed
  `_HYBRID_DETAIL_TOOLS` constant (`["task", "skill"]`) that is present in the
  toolset, the reminder appends the tool's description in its own block. This is
  a code constant, not an env var — the closed set is tied to the opencode tools
  we ship for, so there is nothing for a user to tune. The pointer-back above is
  too weak for tools whose valid argument *values* are an unguessable closed set
  that opencode documents only as prose in the description — `task`'s
  `subagent_type` agent names and `skill`'s skill names (neither is a JSON-Schema
  `enum`). The signature list carries only keys, so those values have to reach
  recency. The block sits after the reminder and OUTSIDE the
  `<<<BEGIN_USER_MESSAGE>>>` wrap (proxy stage-direction). `_extract_tool_details`
  reads the raw `tools` array's `description` field — no `tools_text` fallback.
  Tools with an empty description, or constant-listed tools absent from the
  toolset, are skipped.

  **`task` is pared, not verbatim.** opencode builds the `task` description as
  static boilerplate ("when to use Task", usage notes) followed by the dynamic
  agent list, the latter introduced by the literal header `Available agent types
  and the tools they have access to:` (opencode's `ToolRegistry.describeTask`).
  The boilerplate carries no closed-set values and is already present verbatim
  at the stable prefix, so `_pare_task_description` (anchored on
  `_OPENCODE_TASK_AGENTS_HEADER`) keeps only that header onward — the agent
  names and their one-line descriptions. The header is byte-stable across
  opencode releases (verified 1.14.41 and 1.15.7); if a future opencode renames
  it the parse falls back to the **full** description (degrade to more tokens,
  never a silent loss of the agent list), and `proxy/test_proxy.py`
  `TestTaskDescriptionParing` is the canary that flags the drift. Every other
  detail tool, including `skill` (its description is short), is echoed whole.

## Host-OS injection

The hybrid reminder's **Environment** line tells the agent it runs in a Linux
container with the working directory bind-mounted from the host, so any setup
done only inside the container (a global/system venv, etc.) won't reproduce in
the user's environment — reproducible setup belongs in the working directory
(e.g. a project-local venv). Knowing the host OS family lets the agent give a
host-appropriate caveat (a Linux-native venv may need recreating on a
non-Linux host).

The host OS is fixed per install and the proxy is long-running, so it is read
once at startup, not threaded per-request. The harness CLI exports
`HARNESS_HOST_OS="$(harness_detect_os)"` in its `compose()` wrapper (see
`architecture/harness-cli.md`); `docker-compose.yml` passes it to the proxy
service (`HARNESS_HOST_OS: ${HARNESS_HOST_OS:-unknown}`); `_setup_host_os`
reads it into the `_HOST_OS` module global, honouring only `linux`/`macos`/
`windows` and normalising anything else (unset, empty, `unknown`) to `""`.
When `_HOST_OS` is `""` the Environment line drops only the `(host OS: …)`
parenthetical — the container/reproducibility facts still render, so the proxy
degrades gracefully when launched outside the harness CLI.

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
upstreams (Gemini Enterprise, etc.) emit parallel
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

The estimator uses `len(text) // 3`, not the prose rule-of-thumb of 4.
Agent turns are dominated by code, JSON-formatted tool-call blocks, and
verbatim tool results, which BPE tokenizers pack denser than prose
(roughly 2.5–3.5 chars/token). The joined text also omits role/chat-
template framing, which is real tokens upstream. Both effects bias a
chars/4 estimate low and delayed auto-compaction past the true limit;
chars/3 closes that gap without being aggressive enough to compact
prematurely on prose-heavy turns.

## Config and validation

Module-level env reads at startup:

```
PROXY_HOST=0.0.0.0          PROXY_PORT=8000
PROXY_API_URL (REQUIRED)    PROXY_API_KEY (REQUIRED)    DEFAULT_MODEL_NAME (REQUIRED)
PROXY_TIMEOUT=180           OUTPUT_DIR (optional)
OLLAMA_CONTEXT_LENGTH=200000
```

`DEFAULT_MODEL_NAME` (the renamed `PROXY_API_MODEL`) is the fallback model — see
[URL base + model passthrough](#url-base--model-passthrough). `_validate_config`
in `main()` enforces the three REQUIRED values are non-empty and `PROXY_API_URL`
parses; the process exits with a clear message if not. `_redact_key` is used in
startup logging so logs show something like `sk-abc...xyz`.

`_setup_prompt_mode` runs from `main()` and resolves `PROXY_PROMPT_MODE` (read
from the *container* env — see [Cooperative-prompt
modes](#cooperative-prompt-modes-proxy_prompt_mode); absent on a normal launch,
so it lands on the `hybrid` default) into a module global. The
system→user conversion (`_CHANGE_SYSTEM_TO_USER`) and the hybrid "detail tools"
(`_HYBRID_DETAIL_TOOLS`, `["task", "skill"]`) are project-managed code
constants, not env reads: the upstream takes no system prompt (so the
conversion always runs — see [`upstream-api.md`](upstream-api.md)) and the
detail-tools set is tied to the opencode tools we ship for. There are no longer
`_setup_change_system_to_user` / `_setup_hybrid_detail_tools` functions.

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
