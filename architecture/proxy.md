# `proxy/proxy.py` — translating proxy

A Flask app that exposes an OpenAI-compatible Chat Completions endpoint
(`POST /v1/chat/completions`) and translates to/from the upstream's
chat-completions format, AND injects cooperative tool-use prompts so models
that don't natively support tool calls can produce them as ```json blocks
that the proxy parses back into native `tool_calls`.

opencode speaks this endpoint directly via its `@ai-sdk/openai-compatible`
provider (`baseURL: http://proxy:${PROXY_PORT}/v1`). Replies are Server-Sent
Events (`text/event-stream`) when the request sets `stream: true` (the AI SDK
always does), else a single `chat.completion` JSON object.

Single-process, single file.

## Request lifecycle

```
opencode ──/v1/chat/completions──▶ catch_all() ──┬─ translate_history_and_apply_prompt()
                                                  ├─ POST upstream
                                                  ├─ extract_tool_calls_and_text()
                                                  └─ emit: generate_openai_sse() (SSE)
                                                          / build_openai_response() (JSON)
```

`catch_all(path)` is the single Flask route handler that owns every
non-health request. It:

1. Reads the JSON body (`model`, `messages`, `tools`, and OpenAI `stream`).
2. Builds a tools-as-text string via `format_tools_to_text`.
3. Calls `translate_history_and_apply_prompt` to flatten the inbound
   conversation into a single-role-alternating array for the upstream
   and to inject the cooperative tool-use scaffolding into whichever
   message the prompt-mode dictates.
4. POSTs `{model: <requested>, messages: translated}` to the derived chat
   endpoint `{base}/v1/chat/completions` (see [URL base + model
   passthrough](#url-base--model-passthrough)) with a `Bearer PROXY_API_KEY`
   header, `verify=False` (the upstream uses a self-signed cert), and
   `timeout=PROXY_TIMEOUT`. `<requested>` is the inbound model forwarded
   verbatim, falling back to `DEFAULT_MODEL_NAME` only when the request
   omits a model.
5. Extracts assistant content from the upstream response
   (`extract_assistant_content`).
6. Parses ```json tool-call blocks out of the assistant content
   (`extract_tool_calls_and_text`).
7. Emits OpenAI Chat Completions: SSE (`generate_openai_sse`) when
   `stream: true`, else a single JSON object (`build_openai_response`).

Errors return the OpenAI `{"error":{"message":…}}` envelope (which the AI
SDK surfaces to opencode) via `_client_error`, plus a debug dump under
`OUTPUT_DIR`.

**Content normalization.** A message's `content` may arrive as a plain
string or as a list of content-blocks (multimodal user turns, or an
SDK structuring an assistant turn as `parts`). `_flatten_content_to_str`
collapses either shape to a string — block `text` fields joined with
blank lines, non-text blocks (e.g. `image_url`) dropped — and
`translate_history_and_apply_prompt` calls it on every inbound message
before the role branches run, so the `.strip()`, `+=`, and
token-estimate `join` paths never see a list. (A raw list previously
500'd those paths.) `passthrough` mode returns messages verbatim, so the
flatten there lives at the token-estimate join instead.

## URL base + model passthrough

`PROXY_API_URL` is a **base**, not a full endpoint. `_normalize_api_base`
strips a trailing `/v1/chat/completions`, `/chat/completions`, or `/v1` (so a
base, an OpenAI-style `/v1` base, or a legacy full chat URL all work), and the
module derives two endpoints once at import: `CHAT_URL` =
`{base}/v1/chat/completions` and `MODELS_URL` = `{base}/v1/models`.

`catch_all` forwards the **requested** model to upstream rather than a fixed id:
`model_name or DEFAULT_MODEL_NAME`. opencode reads the bare ids the upstream
advertised on `/v1/models`, so the selected model flows opencode → proxy →
upstream unchanged — this is what lets a user switch between the upstream's
models from opencode. `DEFAULT_MODEL_NAME` is only the fallback when a request
carries no model.

The `GET /v1/models` route is a thin pass-through: it forwards `MODELS_URL` with
the bearer key and `verify=False` and returns the upstream status + body
verbatim, so a locked-key `401` (with its `unlock_url`) reaches the caller
unchanged. It's declared as an explicit Flask route so it wins over `catch_all`.
The agent entrypoint consumes it at startup to build the opencode model dropdown
(see [`containers.md`](containers.md)).

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
  reminder follows. The reminder's WORDING lives in editable files, not
  in proxy.py — see "Editable reminder data" below. It has three labelled
  bullets —
  **Operating** (the merged Agency/Tools/Workflow bullet from the earlier
  format: positive assertion that the model acts through opencode and its
  ```json calls really execute against the working directory mounted from
  the user's machine with results that are real — named target for the
  ~20% reversion to the upstream's "I can't execute, here are commands for
  you to run" persona, issue #109; the JSON envelope and the
  no-fabricated-results rule; prefer-a-listed-tool guidance with
  `webfetch` over curl as the worked example; keep a `todowrite` todo list for
  multi-step work, laid out up front and updated as you go so the plan survives
  a context compaction; launch `task` agents in parallel when independent (capped
  at 8 concurrent, each briefed in full since a sub-agent does not share
  the parent's context); pointer
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
  Note that the agent's OpenAI-format function-tool schemas typically aren't
  honored by these upstreams on this endpoint, so this mode often results in
  the model not using tools at all; that mismatch IS the data point.

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
never from the content: an explicit `tool_name` / `name` field if present,
else the `tool_call_id` correlated against the
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

### Editable reminder data (`.harness-reminder.md`, `.harness-tool-guidance.json`)

The reminder's **wording is data, not code** — all of it. It lives in two
files the user owns, so rewording the standing instructions is an edit +
`harness restart`, never a code change. This matters because the hybrid
reminder is the harness's only standing-instruction channel for the opencode
agent (no runtime AGENTS.md ships).

The split is by format, not by importance. `proxy/reminder.md` is the three
bullets' **prose** — one block, injected verbatim. `proxy/tool-guidance.json`
is the **per-tool entries** the `{{TOOL_ENTRIES}}` token expands to: the
legend, its two conditional sentences, the detail-tool list, and each tool's
one-line guidance. Those are pulled and assembled separately per turn, so they
need a structured format — a hand edit to one tool's description must not be
able to corrupt the other seventeen. Both files follow the same
tracked-default / seeded-user-copy / bind-mount / per-section-fallback
contract, described once below.

- **Tracked defaults** — `proxy/reminder.md` and `proxy/tool-guidance.json`.
  Both `COPY`d into the proxy image at `/app/`, so a container launched
  without the mounts still has a working reminder.
- **User copies** — `<install-root>/.harness-reminder.md` and
  `<install-root>/.harness-tool-guidance.json`, gitignored and seeded from
  the tracked defaults by `seed_reminder_file` / `seed_tool_guidance_file`
  (both thin wrappers over `seed_user_data_file`) on every `harness start` /
  `harness host` (no-op once the file exists). Gitignoring them is what keeps
  an edit from colliding with `harness update`'s `git pull --ff-only`.
- **How the proxy finds them** — `_reminder_template_path()` /
  `_tool_guidance_path()`: (1) `HARNESS_REMINDER_PATH` /
  `HARNESS_TOOL_GUIDANCE_PATH` if set, else (2) the file next to `proxy.py`
  (resolved off `__file__`, never a hardcoded `proxy/` — the image flattens
  the repo into `/app`). `cmd_start` exports each var when its user copy
  exists, and compose mounts it over the baked copy; `host_proxy_start`
  passes both vars directly, since host mode runs `proxy.py` straight from
  the clone with no mount to swap. The compose **defaults** are the tracked
  `./proxy/…` files, not the user copies, so a bare `docker compose up`
  (what the docker test suites do) always has a real mount source — a missing
  source makes docker create a *directory* there and the proxy would silently
  drop to its fallback. Read-only, and plain file mounts, so an edit takes
  effect on `harness restart` — no rebuild.
- **Load** — `_setup_reminder_template()` / `_setup_tool_guidance()` at
  startup. Both are also loaded without `main()` so importing `proxy.py` in
  tests works: the reminder lazy-loads in the builder, the guidance loads at
  import (eagerly, because a lazy first-use load would clobber a test that
  patched `_HYBRID_DETAIL_TOOLS`). Fixed for the life of a launch, like the
  recency map. The startup banner prints both resolved paths, the reminder's
  loaded size, and the guidance's tool count.
- **Tokens** (`reminder.md`) — `{{HOST_OS}}`, `{{CWD}}`, `{{TOOL_ENTRIES}}`, substituted by
  `str.replace`, deliberately not `str.format`/`string.Template`: the prose
  is full of braces (`{"name": ..., "arguments": {...}}`) and backslashes,
  and a user edit must never be able to raise. An unknown token is left
  literal; a deleted token just drops its clause.
- **Keys** (`tool-guidance.json`) — `legend`, `state_check_note`,
  `tool_search_note`, `detail_tools`, `tools`. Every key is optional and
  every one falls back **on its own**: a wrong type, an empty legend, or a
  non-string description drops just that piece and logs a `[!]` naming it.
  `tools` is filtered entry-by-entry, so one bad description costs one
  description — that per-key independence is the reason this data is JSON
  and not a second prose file. An empty `state_check_note` /
  `tool_search_note` is honoured (a deliberate "stop saying that"); an empty
  `legend` is not, since it would leave the tool list unexplained.
- **Self-documentation** — `reminder.md` carries a `<!-- ... -->` block at
  the very top documenting its tokens; it is stripped before injection
  (anchored at the start, so a `<!--` in the prose survives), and a trailing
  newline is stripped too so the reminder ends at `]` regardless of how the
  editor saved it. JSON has no comment syntax, so `tool-guidance.json` uses a
  `_README` key holding the same kind of block as an array of lines; the
  loader ignores every key it does not name, so `_README` costs nothing at
  runtime and cannot leak into the prompt.
- **Degradation** — a missing, unreadable, empty, or (for the JSON)
  unparsable file is not fatal; the proxy logs a loud `[!]` and carries on.
  The reminder falls back to `_REMINDER_FALLBACK`, a minimal built-in that
  keeps the tool-call envelope (and so tool calling itself) alive; the
  guidance falls back to `_HYBRID_LEGEND_FALLBACK` + the two note fallbacks
  + `_HYBRID_DETAIL_TOOLS_FALLBACK`, with **no** guidance map, so every tool
  renders as a bare signature — the same path a tool with no entry already
  takes. Both fallbacks are deliberately NOT copies of the shipped wording:
  duplicating it would let the two drift, so they carry only the
  mechanically load-bearing part (the tool-call envelope; what the signature
  syntax and the `[state-check]` marker mean). `detail_tools` is the one
  exception — it is structure, not wording, and losing it would strand
  `task`'s valid agent names. A JSON syntax error is reported with its
  **line and column**, which is the point of a hand-edited config file. The
  realistic trigger for a whole-file loss is a bad `HARNESS_*_PATH`, where
  docker mounts an empty *directory* over the file; `seed_user_data_file`
  `rmdir`s such a leftover before seeding. Treat a `[!]` in the banner as a
  real outage: with the reminder gone the workflow guidance (todo list,
  parallel `task` agents, environment reproducibility) is gone, and with the
  guidance gone the model gets signatures with no failure-mode hints.

Editing either file is unvalidated by design — the user owns the result; the
JSON loader checks types and JSON syntax, never wording. Three test suites
assert on load-bearing phrases: `proxy/test_proxy.py`
(`TestHybridConsolidatedRecency`, against both shipped defaults, plus
`TestToolGuidanceFile` for the loader contract), `tests/proxy_test.sh`
(`Tools — one entry per tool`), and `tests/scheme_contract_test.sh`
(`Reminder`, `do not invent`, and `<<<BEGIN_AGENT_TOOLS>>>` from
`reminder.md`, plus `Tools — one entry per tool` — which now comes from
`tool-guidance.json`'s `legend`, so rewording the legend breaks it too).
Rewording those phrases out is what makes them fail.

**Upgrade tradeoff.** The seeders never overwrite an existing copy, so
improvements to a shipped default do not reach an install that already has
one. For the prose that is the whole point. For the guidance it also means a
tool opencode adds later renders as a bare signature until the user takes the
new default (delete `.harness-tool-guidance.json`, then `harness restart`) or
adds the entry by hand. That is a deliberate trade of freshness for never
clobbering an edit, and it is graceful in both directions: an unknown tool and
a stale entry each cost nothing but a missing hint.

### Per-tool entries

Below the three reminder bullets is **one entry per tool** — the
consolidated recency format (issue #110) that puts every fact about a tool
together. Each entry is a single bullet that combines:

1. **Signature** — `name(required, [optional])` per tool, the recency
   anchor for parameter keys models most often guess wrong (e.g.
   `read({"filename": ...})` vs `read({"filePath": ...})`).
2. **Guidance** — the one-line failure-mode reminder from the
   `_HYBRID_TOOL_GUIDANCE` map (the "shortened description" the model reads
   each turn instead of the multi-KB schema at the prefix). Tools absent
   from the map render as a bare `- name(signature)` with no guidance, so
   deleting an entry is a supported edit and adding a custom MCP tool
   degrades gracefully. The map is the `tools` key of
   `tool-guidance.json` — user-editable data loaded at import, not a code
   constant and not an env var (see "Editable reminder data" above).
   The shipped default deliberately covers the **union** of tools harness knows about,
   not just the always-shipped subset: situational/optional opencode tools
   (`websearch` — gated by `OPENCODE_ENABLE_EXA`; `lsp`, `apply_patch`,
   `question`, `repo_clone`/`repo_overview`, `plan-enter`/`plan-exit` —
   gated by user config or other opencode flags) get entries too. The
   `_HYBRID_TOOL_GUIDANCE.get(name)` lookup means a stale entry costs
   nothing (it renders only when the tool is actually in `tools`), but a
   *missing* entry the moment a tool starts shipping is a bare signature
   with no failure-mode hint at recency. `TestHybridConsolidatedRecency
   ::test_guidance_map_covers_known_opencode_tools` is the canary that
   flags accidental removal from the shipped file (an `issubset` check, so
   MCP entries below are free to coexist).

   MCP tool guidance is **not** in this map. Both are data now, but they are
   owned by different parties: `tool-guidance.json` is opencode's own tools
   (one file, shipped with the harness, edited by the operator), while
   per-MCP tool guidance is owned by each MCP and reaches the proxy through
   a second map, `_MCP_TOOL_RECENCY`, built from every enabled MCP's own
   `recency.json` (see "MCP tool-recency injection" below). They also arrive
   by different transports — a bind-mounted file versus an env var — because
   the enabled-MCP set is assembled per launch by the CLI. `_format_tool_entries` looks up
   `_HYBRID_TOOL_GUIDANCE.get(name) or _MCP_TOOL_RECENCY.get(name)`, so a
   tool's guidance can come from either source; opencode tools win on the
   (impossible) key collision. Both maps key on the runtime tool name —
   opencode exposes an MCP server's tools as `<server>_<tool>`, so the
   bundled **serena** reference MCP's keys are `serena_*` (NOT the
   `mcp__serena__*` form the mock *response* fixtures use, which is only ever
   substring-matched in tests). MCP entries render only when that MCP is
   enabled and opencode ships the tool for the turn, so a disabled MCP adds
   nothing to recency. They are kept shorter than the opencode lines because
   the recency block is budget-constrained and an MCP can ship many tools.
3. **Closed-set argument values** — for tools in `_HYBRID_DETAIL_TOOLS`
   (`tool-guidance.json`'s `detail_tools`; shipped default
   `["task", "skill"]`), the tool's
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

## MCP tool-recency injection

Per-tool recency guidance for **MCP** tools is data the proxy receives, not
code it ships. The proxy container reads no config files and no repo state, so
the guidance arrives the same way the host OS does: an env var the harness CLI
exports and `docker-compose.yml` interpolates. The CLI builds
`HARNESS_MCP_TOOL_RECENCY` — a JSON object keyed `<server>_<tool>` — by merging
the `recency.json` of every **enabled** MCP (`mcp_tool_recency_json` in
`cmd_start`; see `architecture/mcp.md` "Tool recency descriptions"), and
`docker-compose.yml` passes it to the proxy
(`HARNESS_MCP_TOOL_RECENCY: ${HARNESS_MCP_TOOL_RECENCY:-}`).

`_setup_mcp_tool_recency` runs once at startup (alongside `_setup_host_os`),
parses the env var, and loads it into the `_MCP_TOOL_RECENCY` module global —
keeping only string→non-empty-string entries, defaulting to `{}` on missing /
empty / malformed JSON. The recency builder then consults it as the fallback
guidance source for any tool not in `_HYBRID_TOOL_GUIDANCE` (see "Per-tool
entries"). Because the value is fixed per launch (the enabled-MCP set and their
recency files don't change mid-run), reading it once mirrors the host-OS
treatment; a recency edit takes effect on the next `harness start`/`restart`.
When the var is empty (no MCP enabled, or proxy launched outside the harness
CLI) `_MCP_TOOL_RECENCY` is `{}` and MCP tools render as bare signatures —
graceful degradation, never a hard fail.

The bundled serena MCP's guidance lives in `mcp-registry/serena/recency.json`
(it used to be hard-coded in `_HYBRID_TOOL_GUIDANCE`); migrating it out is what
made the code/data split concrete. `_HYBRID_TOOL_GUIDANCE` itself later
followed, into `proxy/tool-guidance.json`, so both maps are now data and the
remaining distinction is ownership and transport rather than code-vs-data.

## State-check marker + orient-first rule

An MCP can flag a tool as **state-mutating** so the agent is told to orient
before calling it. The flag is data, like the recency line: a tool's
`recency.json` value may be an object `{"line": "...", "state_check": true}`
instead of a bare string (see `architecture/mcp.md` "Tool recency
descriptions"). The harness CLI collects the flagged tools into a JSON array
(`mcp_state_check_json` → `HARNESS_MCP_STATE_CHECK`), `docker-compose.yml`
passes it, and `_setup_state_check_tools` loads it into the
`_MCP_STATE_CHECK_TOOLS` set at startup (same once-per-launch treatment as the
recency map).

`_format_tool_entries` appends a ` [state-check]` marker to a flagged tool's
recency entry, and — **only when at least one flagged tool is in the current
turn's toolset** — adds an orient-first line to the recency legend: *"a tool
marked [state-check] mutates state — call the server's read-only
state/orientation tool first."* This is the orient-first rule the council
placed "at AGENTS.md altitude": the harness ships no runtime AGENTS.md for the
opencode agent, so its standing-instruction channel is the always-injected
recency reminder. Empty set ⇒ no markers, no extra line — graceful degradation.

## Cooperative tool-search

`HARNESS_TOOL_SEARCH=1` (default **off**) enables a hand-built analog of native
deferred-schema tool search, which is unavailable behind a non-first-party
proxy. It advertises two synthetic meta-tools the **proxy serves itself**:

- `tool_list()` — every available tool's signature + one-line purpose.
- `tool_search({"query": "..."})` — full signature + description for tools whose
  name or description matches the query.

The **registry is the inbound `tools` array, rebuilt every request** — there is
no cross-request cache, so there is nothing to go stale when an MCP restarts
mid-session (the schema-staleness gap the council flagged dissolves under
per-request indexing). `_meta_tool_list` / `_meta_tool_search` are pure
functions over that array; `_is_meta_tool_call` recognises a meta call and
**yields to a real opencode tool of the same name** (the name must be a
meta-tool AND absent from the inbound tools).

When enabled, `build_cooperative_prompt_system_addition` appends a small
`<<<BEGIN_META_TOOLS>>>` advertisement after `<<<END_AGENT_TOOLS>>>`, and the
recency legend gains a one-line pointer. When the model emits a meta call,
`catch_all` routes to `_serve_meta_tools`: it appends `[assistant(<meta call>),
user(<framed result>)]` (the result wrapped in `<<<BEGIN_TOOL_RESULT>>>`
markers, exactly like a real tool result) and re-POSTs upstream, looping until
the model emits a real response — bounded by `_META_TOOL_SERVE_BUDGET` (3). This
mirrors the malformed-tool-call retry loop. **opencode never sees a meta call**:
if a turn's calls are all-meta the loop runs and only the real follow-up is
streamed; if serving fails or the budget is exhausted the meta calls are
dropped (never forwarded — opencode has no such tool); a turn that mixes meta +
real calls keeps only the real ones. Debug dumps land at
`<req_id>_02_API_ToolSearch_Request_NN` / `_03_API_ToolSearch_Response_NN`.

**Scope (the council's "build the mechanism, gate the migration").** The full
schemas still ship at the stable prefix when this is on — no schema is migrated
behind search. The flag builds the mechanism so it is ready; the decision to
actually thin the prefix is gated on the catalog-size instrumentation below
crossing a measured threshold. Default-off means a normal launch is byte-for-
byte unchanged, and the integration (the serve loop against a live upstream) is
validated before it ever touches the daily-driver path.

## Catalog-size instrumentation

`catch_all` logs one line per request — `[req_id] catalog: tools=N
schema_tokens=M` — where `tools` ≈ the recency line count (one entry per tool)
and `schema_tokens` is the local token estimate of the full schemas at the
stable prefix. This is the measured signal that decides when migrating schemas
behind `tool_search` is worth its complexity: until the prefix is genuinely
large the migration buys little (the proxy re-injects every turn, so prefix
loss is largely self-healing for the small push layer).

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

## Malformed tool-call retry (in-proxy)

Two failure modes look like the model *tried* to call a tool but produced
output that `extract_tool_calls_and_text` could not parse, and bleeding
the raw fence into chat ends the turn with no recovery:

- **Malformed fence** — a ```json fence opener followed by a non-`{`
  character (or by EOF). The observed shape is
  ` ```json_parse_or_id:todowrite}` (issue #121): the model fabricated a
  tool-call shorthand instead of emitting a full block. Markdown renders
  the unclosed fence as a collapsed/empty code block and `done_reason:
  stop` ends the turn silently.
- **Malformed `\escape`** — a brace-balanced JSON object whose strings
  carry an invalid JSON escape (e.g. `\x1e`, `\a`, `\v` — any `\X` where
  X is not in `"\\/bfnrtu`). `json.loads(..., strict=False)` rejects
  these with a spec-defined `Invalid \escape` error, the block fails to
  extract, and the fence bleeds into chat as text. Common when the
  model lazily transcribes a Python/C source literal into JSON without
  re-escaping backslashes.

When extraction yields zero tool calls AND `_diagnose_failed_tool_call`
classifies the response as one of these two kinds, the proxy appends a
corrective `[assistant(<bad response>), user(<correction>)]` pair to the
conversation and re-POSTs upstream **once** (budget = 1). The retry goes
through the same `translate_history_and_apply_prompt` scaffolding as the
original, so the corrective lands in the hybrid recency `USER_REQUEST`
slot with the live tool list still at the stable prefix — the model sees
the full operating rules plus "your previous attempt failed for this
specific reason; emit the same call(s) again". If the retry produces a
non-malformed response (valid tool calls, prose, or empty), the retry's
result replaces the bad attempt and only the retry is streamed back —
**opencode never sees the failed attempt or any proxy-injected
correction text**. If the retry itself errors (timeout, non-2xx,
non-JSON) or produces ANOTHER malformed-tool-call attempt, the original
bad response falls through to the pre-existing bleed/empty-rescue path;
the retry is strictly additive.

The detector is conservative so that prose describing JSON doesn't
trigger a spurious retry:

- `malformed_escape` fires only when a brace-balanced JSON body
  immediately follows a ```json fence AND `json.loads` raises the
  JSON-spec `Invalid \escape` error. False-positive risk is near zero
  because (a) the body already passed balanced-brace scanning, (b) the
  fence shape was already cleared, and (c) the trigger is the JSON
  parser's own error string, not a heuristic.
- `malformed_fence` fires only when the *stripped* response **starts
  with** the broken fence (no prior prose) AND the first ```json
  opener is not followed by a balanced JSON object AND there is no
  later ```json fence in the same response. Prose like "Here's an
  example: ```json {wrong shape}" stays in clean_text as before.

Other JSON parse errors (brace-unbalanced, shape-wrong-but-parsed,
missing-key) continue to use the existing "left in text on parse
failure" path. The retry is keyed to the two failure modes that produce
turn-ending no-result outputs; everything else continues to bleed
visibly so the model (and the user, via opencode) can react.

Observability: when retry fires, `catch_all` logs
`[req_id] in-proxy retry for malformed tool call (kind=<kind>);
attempt=1, recovered=<yes|no>` and the retry's request/response dumps
land at `<req_id>_02_API_Retry_Request.json` /
`<req_id>_03_API_Retry_Response.json` (or `_03_API_Retry_Error.json` if
the retry POST failed) alongside the original `_02`/`_03` dumps.

The cooperative-prompt reminder's **Operating** bullet was tightened in
the same change: the tool-call instruction now requires a **complete**
```json...``` block (opener + body + closing fence, never an abbreviated
identifier or partial fence) and names valid JSON `\escape` sequences
(`\n`, `\x1e`, `\\`) so the bad-escape failure mode is less likely to
arise in the first place. Retry catches the cases that slip through.

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
empty. When both hold, the proxy substitutes a rescue. The shape depends on
whether a shell tool is exposed:

1. **Tool-call rescue (preferred)** — when the inbound tools list contains
   a `bash`-style shell tool, `_select_rescue_tool` returns a `{name,
   arguments: {"command": "pwd", "description": "Print working
   directory"}}` payload which becomes the sole `tool_call_payloads` entry.
   The assistant text is left **empty** — a tool-only turn is well-formed,
   so no substitute text is needed and none is emitted (a bare
   `"Understood."` would just be a content-free filler line in the
   transcript). The response `finish_reason` becomes `tool_calls`, opencode
   executes the no-op `pwd`, and re-invokes the model with the tool result
   as the **new** recency — which displaces the filter-triggering content
   out of the hot slot, so the next model turn returns real content without
   user intervention.
2. **Text-only fallback** — ONLY when no shell tool is exposed,
   `_empty_response_rescue_text` returns the single word `"Understood."` to
   keep the response non-empty. The text unsticks the upstream filter on
   the next request (the user's prompt displaces the trigger), but a
   text-only assistant reply makes opencode end the turn with `done_reason:
   stop`, so the user has to type something for the conversation to
   continue — no auto-continuation. This is the degraded mode; the
   tool-call rescue above is what runs for every coding agent in practice.

The detector deliberately does NOT gate on `completion_tokens` or
`finish_reason` value — the user-facing symptom (visible stall) is the
same regardless of the upstream's exact bookkeeping, so the rule is just
"no text, no tool calls". A `print()` line at the call site logs the
`finish_reason`, the `req_id`, and which rescue mode fired
(`tool(<name>)` or `text-only`), so the event remains visible in
`harness logs proxy` for diagnosis.

### Why `bash` running `pwd`

The bash tool is the rescue target because (a) every coding agent
harness ships for exposes a shell tool (opencode `bash`, Claude Code
`Bash`) — broader availability than `todowrite`-style tools, (b) `pwd`
is genuinely inconsequential: read-only, no filesystem/network/state
side effects, and (c) the one-line tool result (the absolute path) is
tiny and can't itself re-trigger the upstream filter.
`_select_rescue_tool` matches the inbound tool name case-insensitively,
so `bash` and `Bash` both match; the emitted call echoes the inbound
name verbatim so the agent's router can dispatch it. The arguments
shape `{"command": "pwd", "description": "Print working directory"}`
satisfies the required-fields contract of both schemas. The tool call
carries the rescue by itself — the assistant text stays empty — so when
a shell tool is present there is no `"Understood."` line at all. When no
shell tool is exposed for the turn, the rescue degrades to text-only —
the upstream still unsticks on the user's next prompt; only the
auto-continuation is lost.

### Earlier iterations

The first fix emitted a verbose `[harness proxy] …` diagnostic with
"start a new session" guidance. opencode rendered that as the assistant's
own turn, which is jarring given the conversation actually resumes on the
next prompt. The minimal rescue text replaced it. That in turn proved
insufficient when the user observed (issue #117) that the turn still
ended at `"Understood."` — text-only is `done_reason: stop` and opencode
ends the turn. A `todowrite {todos: []}` call was prototyped to force
`done_reason: tool_calls` but rejected in favor of `bash pwd`: bash is
more universally exposed than todowrite, and a read-only command is more
unambiguously inconsequential than mutating the agent's task list (even
to empty). Diagnosis still belongs in `harness logs proxy` and
`state/output/<req_id>_03_API_Response.json`.

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

## Response emission

The chunks are materialized into a list first and dumped to `OUTPUT_DIR`
before being streamed, and latency is unchanged: the upstream call completed
fully before emission began (the proxy is NOT streaming from upstream — it
gets the full response, then translates). Memory cost is the response size —
a few KB for typical tool-call responses.

### OpenAI SSE (`generate_openai_sse`)

The primary path. opencode's `@ai-sdk/openai-compatible` provider always
sends `stream: true` and parses `text/event-stream`, so the proxy emits
`data: {chat.completion.chunk}\n\n` lines terminated by `data: [DONE]`.
Because the full text is already in hand, content is sent as a small
number of deltas rather than incrementally:

- an opening `delta:{role:"assistant", content:<text>}` chunk (an empty
  `content:""` opener is still emitted for tool-only/rescued responses so
  the stream is well-formed);
- one `delta:{tool_calls:[…]}` chunk when there are tool calls — each
  entry carries `index` (always; the array key the AI SDK accumulates
  into), `id`+`function.name`, and `function.arguments` as a **JSON
  string** (the internal payload carries it as an object, so
  `_openai_tool_calls` JSON-encodes it). Emitting each call complete in
  one delta satisfies the SDK's "first fragment carries id+name, arguments
  accumulate to valid JSON" rule;
- a final `delta:{}` chunk with `finish_reason` (`tool_calls` or `stop`);
- a usage chunk (`choices:[]`, `usage:{prompt,completion,total}`) since
  opencode requests `stream_options:{include_usage:true}`;
- `data: [DONE]`.

All chunks share one `chatcmpl-…` id and `created` stamp.

### OpenAI JSON (`build_openai_response`)

When a request sets `stream: false` (manual `curl`/debug; opencode always
streams), a single `chat.completion` object is returned instead — same
content/tool_calls/usage, `tool_calls` without the streaming-only `index`
field.

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
MODEL_CONTEXT_LENGTH=200000 (legacy alias: OLLAMA_CONTEXT_LENGTH)
HARNESS_FORCE_LOOPBACK (optional; host mode sets it — see below)
PROXY_BACKEND=openai        (optional; `harness chatgpt` sets it — see below)
CHATGPT_BASE_URL / CHATGPT_MODEL_NAME / CHATGPT_COOKIE_STRING
                            (REQUIRED only when PROXY_BACKEND=chatgpt)
```

`DEFAULT_MODEL_NAME` (the renamed `PROXY_API_MODEL`) is the fallback model — see
[URL base + model passthrough](#url-base--model-passthrough). `_validate_config`
in `main()` enforces the three REQUIRED values are non-empty and `PROXY_API_URL`
parses; the process exits with a clear message if not. `_redact_key` is used in
startup logging so logs show something like `sk-abc...xyz`.

`main()` calls `_force_utf8_stdio()` **first**, before `_validate_config`, so
stdout/stderr are reconfigured to `encoding="utf-8", errors="backslashreplace"`.
The proxy prints a U+2192 arrow in its startup banner (`sys→user:`) and echoes
upstream error bodies, model names, and tracebacks (arbitrary Unicode) on the
error paths. In `harness host` mode the launcher `nohup`s the proxy with stdout
redirected to a logfile, so on Windows the stream defaults to the legacy cp1252
code page, which cannot encode `→` and much else — `print` raised
`UnicodeEncodeError` and killed the proxy at startup. UTF-8 encodes every code
point, removing the whole crash class; `backslashreplace` keeps even a stray
surrogate from raising on a log write. No-op where the stream is already UTF-8 or
predates `reconfigure` (Python < 3.7).

### Upstream backends

The proxy speaks **one** upstream dialect per process, selected by
`PROXY_BACKEND`:

| Value | Upstream | Auth | Catalog |
|---|---|---|---|
| `openai` (default) | `{PROXY_API_URL}/v1/chat/completions` | `Authorization: Bearer PROXY_API_KEY` | proxied from `{base}/v1/models` |
| `chatgpt` | `{CHATGPT_BASE_URL}/backend-api/conversation/stream` | `Cookie: CHATGPT_COOKIE_STRING` | synthesized, one entry: `CHATGPT_MODEL_NAME` |

`PROXY_BACKEND` is deliberately **not** a `.env` key. `harness chatgpt` injects
it for a single launch — container mode through the generated compose runtime
override, host mode through the proxy's launch env — so a default install is
byte-identical to before. Only the three `CHATGPT_*` values live in `.env`.

The splice point is one function, `_upstream_post(headers, payload)`, which all
three outbound chat call sites (`catch_all`, the malformed-tool-call retry, the
meta-tool loop) go through. This works because the proxy **never streams from
upstream** — it materializes the full upstream response, then translates. The
chatgpt branch therefore only has to hand back an OpenAI-shaped response object:
`_chatgpt_post` consumes the backend-api's SSE stream and returns a
`_SyntheticResponse` exposing the `.status_code` / `.text` / `.json()` trio the
call sites use. Everything downstream (error triage, tool-call extraction, the
retry and meta-tool loops, the empty-response rescue, both emitters) is
dialect-agnostic and untouched.

Three details of the chatgpt dialect are **hardcoded**, not configurable: the
stream path `/backend-api/conversation/stream`, `timezone`/`timezone_offset_min`
(`America/Chicago` / 300), and the browser `User-Agent`.

**No server-side conversation state.** The backend-api keeps history under
`conversation_id`/`parent_message_id` and expects only the newest turn. This
proxy is stateless and re-sends the whole history every request, so reusing
that state would duplicate the transcript. `_chatgpt_flatten_messages` instead
starts a fresh conversation per request and renders the translated history into
the single user message the endpoint takes: a one-message history passes
through verbatim (identical to the reference client), a longer one is joined
with `Role: text` labels. A multi-message array was rejected because if the
endpoint honored only its last entry the history loss would be silent.

`_validate_config` branches on the backend: `chatgpt` requires the three
`CHATGPT_*` values and ignores the `PROXY_API_*` / `DEFAULT_MODEL_NAME` trio;
any value other than `openai` or `chatgpt` is fatal. The startup banner reports
the backend and, for chatgpt, the cookie's length only (never a prefix, unlike
`_redact_key` on a bearer token).

`_validate_config` also honors **`HARNESS_FORCE_LOOPBACK`** (set by the
containerless `harness host` launcher — see [`harness-cli.md`](harness-cli.md) →
"Host mode"). When that env var is truthy (`1`/`true`/`yes`) and `PROXY_HOST` is
not a loopback address (`127.0.0.1`, `::1`, `localhost`), the process exits
fatally rather than binding a publicly-reachable socket. Container mode keeps the
firewall sidecar; host mode has no firewall, so this guard is the backstop that
keeps a misconfigured `.env` (`PROXY_HOST=0.0.0.0`) from exposing the host-mode
proxy off-box. The launcher sets the var; the proxy enforces it (defense in
depth).

`_setup_prompt_mode` runs from `main()` and resolves `PROXY_PROMPT_MODE` (read
from the *container* env — see [Cooperative-prompt
modes](#cooperative-prompt-modes-proxy_prompt_mode); absent on a normal launch,
so it lands on the `hybrid` default) into a module global. The
system→user conversion (`_CHANGE_SYSTEM_TO_USER`) is a project-managed code
constant, not an env read: the upstream takes no system prompt, so the
conversion always runs (see [`upstream-api.md`](upstream-api.md)). The hybrid
"detail tools" (`_HYBRID_DETAIL_TOOLS`, shipped default `["task", "skill"]`)
are no longer a constant either — `_setup_tool_guidance` loads them from
`tool-guidance.json`'s `detail_tools`. There are still no
`_setup_change_system_to_user` / `_setup_hybrid_detail_tools` functions: the
detail-tools list moved into a mounted file, not back into an env knob.

## Debug dumps

When `OUTPUT_DIR` is set (typical: `/output` inside the container,
bind-mounted to `<install-root>/state/output/` on the host), each request
writes (`harness host` has no bind mount, so it remaps the same `/output` to
`state/output/` on the host before launching the proxy — see
[`harness-cli.md`](harness-cli.md) → "Host mode"):

- `<req_id>_01_Inbound_Request.json`
- `<req_id>_02_API_Request.json`
- `<req_id>_03_API_Response.json` or `_03_API_Error.json`
- `<req_id>_04_OpenAI_SSE_Response.json` (streaming) or
  `_04_OpenAI_Response.json` (non-streaming)
- `<req_id>_99_Fatal_Error.json` on unhandled exception

`req_id` is the timestamp at request start (`%Y%m%d_%H%M%S_%f`).

## Tests

`tests/proxy_test.sh` (integration via the proxy container) and
`proxy/test_proxy.py` (unit tests, run directly in the proxy container).
The scenarios drive the OpenAI `/v1/chat/completions` path — SSE framing,
non-stream JSON, tool-call deltas whose `arguments` is a JSON string, and
the cooperative-prompt/empty-response behaviours. See [`tests.md`](tests.md).

## Iteration loop

`docker compose --project-name harness restart proxy` picks up edits in
~10–15 s without touching agents / MCPs. Faster than
`harness restart` for the proxy-iteration loop.
