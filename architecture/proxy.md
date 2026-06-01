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
  index 0, the "stable prefix" position). A consolidated recency block lands
  on the last user message, organised so the **live user request comes FIRST**
  (wrapped in `<<<BEGIN_USER_REQUEST>>>` markers — issue #110), then a short
  reminder follows. The reminder has three labelled bullets —
  **Operating** (the merged Agency/Tools/Workflow bullet from the earlier
  format: positive assertion that the model acts through opencode and its
  ```json calls really execute against the working directory mounted from
  the user's machine with results that are real — named target for the
  ~20% reversion to the upstream's "I can't execute, here are commands for
  you to run" persona, issue #109; the JSON envelope and the
  no-fabricated-results rule; prefer-a-listed-tool guidance with
  `webfetch` over curl as the worked example; track non-trivial work with
  `todowrite`; launch `task` agents in parallel when independent; pointer
  back to `<<<BEGIN_AGENT_TOOLS>>>` for full descriptions),
  **Honesty** (anti-fabrication: no invented names/paths/signatures/
  citations; **plus** the addition from issue #110: any claim about the
  working directory, its contents, or local filesystem state must come
  from a tool result — see [Working-directory echo](#working-directory-echo)
  for why), and
  **Environment** (the proxy runs in a Linux container with the working
  directory mounted from the host — host OS named when known, see
  [Host-OS injection](#host-os-injection); the live host CWD is echoed
  inline so "this folder" / "here" resolve to the real path, see
  [Working-directory echo](#working-directory-echo) — so reproducible
  setup must live in the working directory, not the container). Below the
  bullets is **one entry per tool** — signature, one-line guidance from
  `_HYBRID_TOOL_GUIDANCE`, and (for "detail tools") the verbatim closed-set
  argument values inlined under the same entry. This is the
  consolidated-recency change from issue #110: everything the model needs
  to know about a tool sits in one place rather than being split across a
  signature list, a separate guidance section, and standalone
  `<<<BEGIN_TOOL_DETAIL>>>` blocks. Signatures take the
  `name(required, [optional])` shape — the recency anchor for the parameter
  keys models most often guess wrong (e.g. calling `read({"filename": ...})`
  instead of `read({"filePath": ...})`, or omitting opencode's `bash`
  required `description`). For the "detail tools" whose valid argument
  *values* are a closed set opencode documents only in description prose (a
  `task`'s `subagent_type` agents, a `skill`'s skill names), the tool's
  description is inlined directly under its entry (whole for `skill`; pared
  to the agent-list section for `task`) — see
  [Hybrid delimiters](#hybrid-delimiters). Hybrid additionally delimits four
  content categories so each is addressable by name and the model can't
  conflate them with the upstream gateway's own system prompt/tools — again
  see [Hybrid delimiters](#hybrid-delimiters) below.
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

`hybrid` mode (and ONLY hybrid) additionally wraps four content categories
in `<<<BEGIN_X>>>` / `<<<END_X>>>` markers so each section is addressable by
name. The failure pattern this targets: the upstream gateway injects its own
system prompt mentioning its own tools/subagents, and the model conflates
harness's injected tools with those — or, when the user says "the first
message" / "the tool definitions", can't tell which section is meant. The
markers are applied in the hybrid dispatch branch of
`translate_history_and_apply_prompt`; `user_front` and `passthrough` never
emit them. The exception is `<<<BEGIN_USER_REQUEST>>>`, which is shared with
`user_front`: hybrid uses it to delimit the live user ask on the recency
turn (placed at the FRONT of the recency block, before the reminder), and
`user_front` uses it as part of its own scaffold.

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
- **`<<<BEGIN_USER_MESSAGE>>>`** — wraps every PRIOR real user-role turn
  (original role `user`). The live (last) real user turn is NOT wrapped in
  USER_MESSAGE; the recency builder wraps it in `<<<BEGIN_USER_REQUEST>>>`
  instead — see below. Tool-result-converted-to-user messages are detected
  by their `<<<BEGIN_TOOL_RESULT` marker and skipped — they keep only the
  TOOL_RESULT delimiters.
- **`<<<BEGIN_USER_REQUEST>>>`** — wraps the LIVE user ask on the recency
  turn, placed at the FRONT of the recency block (before the reminder).
  Order swap from the prior "reminder-first" layout: the live ask now sits
  in the most-recent attention slot rather than behind a wall of operating
  rules. Tool-result-converted-to-user turns skip this wrap (the TOOL_RESULT
  markers already delimit the live content).

The reminder (`build_cooperative_prompt_hybrid_reminder`) Operating bullet
points at `<<<BEGIN_AGENT_TOOLS>>>` for **full tool descriptions only**, so
that when attention to `messages[0]` dilutes on long conversations the
model still has a named target to retrieve. It deliberately does NOT claim
that section is where to find parameter-*value* constraints (a `task`'s
agent types, a `skill`'s names): those reach recency INLINED under each
tool's own entry — see "Per-tool entries" below. This is additive — token
cost is ~150–250 tokens/turn with the three bullets + Environment context;
hybrid's lighter-than-user_front recency profile is preserved.

### Per-tool entries

Below the three reminder bullets is **one entry per tool** — the
consolidated recency format (issue #110) that puts every fact about a tool
together. Each entry is a single bullet that combines:

1. **Signature** — `name(required, [optional])` per tool, the recency
   anchor for parameter keys models most often guess wrong (e.g.
   `read({"filename": ...})` vs `read({"filePath": ...})`).
2. **Guidance** — the one-line failure-mode reminder from the project-
   managed `_HYBRID_TOOL_GUIDANCE` map (the "shortened description" the
   model reads each turn instead of the multi-KB schema at the prefix).
   Tools absent from the map render as a bare `- name(signature)` with no
   guidance, so adding a custom MCP tool degrades gracefully. The map is a
   code constant, not an env var — keyed to the opencode tools we ship for.
   The map deliberately covers the **union** of tools harness knows about,
   not just the always-shipped subset: situational/optional opencode tools
   (`websearch` — gated by `OPENCODE_ENABLE_EXA`; `lsp`, `apply_patch`,
   `question`, `repo_clone`/`repo_overview`, `plan-enter`/`plan-exit` —
   gated by user config or other opencode flags) get entries too. The
   `_HYBRID_TOOL_GUIDANCE.get(name)` lookup means a stale entry costs
   nothing (it renders only when the tool is actually in `tools`), but a
   *missing* entry the moment a tool starts shipping is a bare signature
   with no failure-mode hint at recency. `TestHybridConsolidatedRecency
   ::test_guidance_map_covers_known_opencode_tools` is the canary that
   flags accidental removal.
3. **Closed-set argument values** — for tools in the project-managed
   `_HYBRID_DETAIL_TOOLS` constant (`["task", "skill"]`), the tool's
   verbatim description is inlined as an indented block UNDER the tool's
   entry. These are the tools whose valid argument *values* are an
   unguessable closed set opencode documents only as prose in the
   description (a `task`'s `subagent_type` agents, a `skill`'s skill
   names; neither is a JSON-Schema `enum`). The signature carries only
   keys, so those values have to reach recency somewhere — they used to
   render in their own `<<<BEGIN_TOOL_DETAIL name="…">>>` blocks below the
   bullets; the consolidated format inlines them so every fact about a
   tool sits in one place. `_extract_tool_details` reads the raw `tools`
   array's `description` field; tools with an empty description, or
   constant-listed tools absent from the toolset, contribute no inlined
   block.

**`task` is pared, not verbatim.** opencode builds the `task` description as
static boilerplate ("when to use Task", usage notes) followed by the
dynamic agent list, the latter introduced by the literal header
`Available agent types and the tools they have access to:` (opencode's
`ToolRegistry.describeTask`). The boilerplate carries no closed-set values
and is already present verbatim at the stable prefix, so
`_pare_task_description` (anchored on `_OPENCODE_TASK_AGENTS_HEADER`) keeps
only that header onward — the agent names and their one-line descriptions.
The header is byte-stable across opencode releases (verified 1.14.41 and
1.15.7); if a future opencode renames it the parse falls back to the
**full** description (degrade to more tokens, never a silent loss of the
agent list), and `proxy/test_proxy.py` `TestTaskDescriptionParing` is the
canary that flags the drift. Every other detail tool, including `skill`
(its description is short), is inlined whole.

## Working-directory echo

The reminder's **Environment** line echoes the live host CWD inline (e.g.
`The working directory for this turn is \`/c/Users/.../ENC\` — when the
user says "this folder", "here", "my machine", or "the workspace", they
mean exactly that path.`). The Environment bullet without this anchor
described **where** the working directory came from (host bind-mount) but
not **which path it was**, which let the upstream's pretrained sense of
its own sandbox win — `/home/bard`, `/home/user`, `/workspace`, etc. —
when asked "what's in this folder?". Pairing the positive anchor with
Honesty's filesystem-claims rule (any claim about the working directory,
its contents, or filesystem state must come from a tool result) closes
both sides: here's the right answer, and here's what not to claim
without checking.

`_extract_working_directory(system_content)` pulls the path from
`Working directory: <path>` inside `messages[0]`'s content at request
time. The system message is still role `system` when the recency builder
runs (the `_CHANGE_SYSTEM_TO_USER` post-pass runs later), so the regex
sees the inbound agent prompt — opencode's `<env>` block carries this
line at a stable label. If the label is missing or unparsable (a future
opencode rename, a non-opencode upstream), the Environment line falls
back to the prior wording (host OS only) — graceful degradation, never
a hard fail. Honesty's filesystem-claims clause renders unconditionally
because it's load-bearing whether or not the CWD anchor was found.

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

Parsing uses `json.loads(..., strict=False)` so unescaped control
characters inside string values (literal `\n`, `\t`, `\r`) are accepted.
Models often emit a multi-line `python -c "..."` or heredoc as a
`command` argument with **real** newline bytes inside the JSON string
rather than `\\n` escapes — strict JSON forbids this, but rejecting the
block means the whole fenced payload bleeds into chat as raw text
(issue #115). The leniency only widens what counts as a valid tool call;
balanced-brace scanning, the `name`/`arguments` shape check, and the
"left in text on parse failure" behaviour are unchanged.

Same tradeoff drives the **tolerant lift on missing `arguments`** (issue
#118). Models sometimes spell args at the top level instead of nested —
e.g. `{"name": "bash", "command": "ls", "description": "list files"}`.
`catch_all` builds `available_tool_names` from the inbound `tools` array
and passes it to `extract_tool_calls_and_text`. When a parsed block has
`name` matching a current tool but no `arguments` key, the remaining
top-level keys are lifted into `arguments`. The lift is gated on **both**
conditions: a block whose `arguments` key is already present is
untouched (correctly-shaped calls aren't rewritten), and a block whose
`name` isn't a current tool still leaks into chat (instructional prose
like ` ```json\n{"name": "foo", ...}\n``` ` for a tool we never exposed
remains user-facing content, same precedent as the issue #115 leniency).
Lifting fabricates no values — only top-level keys the model already
wrote — so a misshaped call surfaces upstream as a real argument-shape
error the model can correct rather than bleeding into chat.

## Empty-response detection

Some upstreams silently short-circuit before generation — the response is
well-formed JSON with `finish_reason="stop"`, `completion_tokens=0`, no
`thinking`/`safety_ratings`/`prompt_feedback`, response time ~1 s — when
something in the **most-recent** message slot (in the observed case a
~100-line repetitive tool result) trips an internal content/safety filter
that the OpenAI-shape façade flattens to plain `stop`. Without
intervention, opencode sees "no content + done" and stops the turn; the
user types `continue` and the same content is still the recency, so the
same empty response keeps coming back (issue #117).

Confirmed scope of the trigger: only the most-recent message matters.
Once a turn no longer carries the offending content the upstream returns
to normal, even with the trigger still present further back in history.

After `extract_tool_calls_and_text` runs in `catch_all`, the proxy checks
whether `clean_text` is empty/whitespace-only AND `tool_call_payloads` is
empty. When both hold, the proxy substitutes a **two-part rescue**:

1. **Rescue text** — `_empty_response_rescue_text` returns the single
   word `"Understood."` which replaces the empty `clean_text`. The text
   alone is enough to unstick the upstream filter on the next request
   (the user's prompt displaces the trigger), but a text-only assistant
   reply makes opencode end the turn with `done_reason: stop`. The user
   then has to type something for the conversation to continue.
2. **Rescue tool call** — when the inbound tools list contains a
   `todowrite`-style tool, `_select_rescue_tool` returns a `{name,
   arguments: {"todos": []}}` payload which the proxy appends to
   `tool_call_payloads`. The NDJSON `done_reason` becomes `tool_calls`,
   opencode executes the no-op `todowrite`, and re-invokes the model
   with the tool result as the **new** recency — which displaces the
   filter-triggering content out of the hot slot, so the next model
   turn returns real content without user intervention.

The detector deliberately does NOT gate on `completion_tokens` or
`finish_reason` value — the user-facing symptom (visible stall) is the
same regardless of the upstream's exact bookkeeping, so the rule is just
"no text, no tool calls". A `print()` line at the call site logs the
`finish_reason`, the `req_id`, and which rescue mode fired
(`text+tool(<name>)` or `text-only`), so the event remains visible in
`harness logs proxy` for diagnosis.

### Why `todowrite`

`todowrite` is the rescue target because (a) it's available across the
agents harness ships for (opencode `todowrite`, Claude Code `TodoWrite`),
(b) the call has no filesystem/network side effects — only the agent's
local task list changes, (c) `{"todos": []}` is a valid call for both
schemas and produces a tiny tool result (a string like "Updated 0 todos")
that itself can't re-trigger the upstream filter, and (d) it is
exhaustively listed in `_HYBRID_TOOL_GUIDANCE` so the recency reminder
already documents the call shape — the model that sees the result on the
follow-up turn isn't surprised by it. `_select_rescue_tool` matches the
inbound tool name case-insensitively and with underscores stripped, so
`todowrite`, `TodoWrite`, and `todo_write` all match. When no
todowrite-style tool is exposed for the turn, the rescue degrades to
text-only — the upstream still unsticks on the user's next prompt; only
the auto-continuation is lost.

### Earlier iterations

The first fix emitted a verbose `[harness proxy] …` diagnostic with
"start a new session" guidance. opencode rendered that as the assistant's
own turn, which is jarring given the conversation actually resumes on the
next prompt. The minimal rescue text replaced it. That in turn proved
insufficient when the user observed (issue #117) that the turn still
ended at `"Understood."`; the tool-call leg of the rescue was added so
the agent loop continues automatically. Diagnosis still belongs in
`harness logs proxy` and `state/output/<req_id>_03_API_Response.json`.

### Conservatism

- **Tool-only turns are NOT empty.** A response with `clean_text == ""`
  but `tool_call_payloads != []` is the normal shape for any turn the
  model spent entirely on calling tools — never treated as a stall.
- **No automatic retry.** A filter trigger is deterministic on the same
  payload, so a same-payload retry would refuse again. The rescue
  occupies the assistant slot; the next request's recency naturally
  differs.
- **No truncation of the inbound tool result.** Truncating to head+tail
  is silent context mutation for a trigger we haven't proven the shape
  of, and the rescue strategy doesn't need it.

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
