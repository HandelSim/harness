# Upstream API — the third-party endpoint behind the proxy

What the proxy POSTs to (`PROXY_API_URL`). This doc records the upstream's
observed contract and quirks so proxy behavior can be reasoned about
without re-probing the endpoint. It is the source of truth for *what the
API does*; [`proxy.md`](proxy.md) covers *how the proxy reacts* to it.

## What it is

A **Gemini Enterprise chat product** exposed behind a
chat-completions-shaped HTTP API. It is a general chat assistant, not a
coding-agent API — there is no agent-oriented system prompt and no native
tool protocol.

**Multi-model.** The upstream serves several models and **honors the request's
`model` field**, and it exposes an OpenAI-style `GET /v1/models` catalog. The
proxy treats `PROXY_API_URL` as a base and derives `{base}/v1/chat/completions`
and `{base}/v1/models` from it; it forwards the model the agent selected
(passthrough) and the agent builds opencode's model list from the advertised
catalog so the user can switch models from opencode. See [`proxy.md`](proxy.md) → URL base + model
passthrough. (Earlier the harness pinned one model — `gemini 3.1 pro` was the
one clear best choice — which is why older docs/config spoke of a single model.)

Note: the upstream's own request/response examples use placeholder model
ids (`gemini-2.5-flash`, `gpt-4`) and example `usage` numbers that do not
correspond to a real exchange — they document JSON *shape* only, not
authoritative values.

## Contract and quirks

These are the load-bearing behaviors the proxy is built around:

- **No tool support** (historically). A `tools` field in the request was
  ignored and the response never contained `tool_calls`. This is *why* the
  proxy does cooperative-prompt tool-use; see [`proxy.md`](proxy.md).
  - As of 2026-10 the vendor documents OpenAI-style native `tools` /
    `tool_calls` / `role:"tool"` and `response_format`. `harness probe`
    (2026-10-01, all 6 catalog models) found **none of it live**: `tools`,
    `tool_choice`, `parallel_tool_calls` and legacy `functions` are ignored,
    no reply carries `tool_calls`, and `response_format` (`json_schema` or
    `json_object`) is ignored (markdown comes back). Re-run `harness probe`
    when the vendor says it is enabled (see [`harness-cli.md`](harness-cli.md)).
  - **2026-10-13: the `-withtools` models are live and standard.** See
    [Native-tools models](#native-tools-models--withtools). Everything else
    in this section describes the plain models and the cooperative path.
- **Hidden, uncontrollable system prompt.** The upstream runs its own
  system prompt that we can neither see nor override. A `system`-role
  message in the request is **quietly ignored** (no error). Its prompt is
  chat-oriented, not coding-agent-oriented. This is *why* the proxy **always**
  converts the system role to user: `_CHANGE_SYSTEM_TO_USER` is a hardcoded
  `True` constant (not a knob — the conversion must always happen since the
  upstream never honors a system prompt) — see [`proxy.md`](proxy.md).
- **No network access.** The upstream LLM cannot web-search. All web
  access must happen agent-side (e.g. opencode's own fetch tooling), not
  by asking the model.
- **Consecutive `user` messages collapse.** If two `user`-role messages
  are sent in a row, only the **last** is used. Messages must be
  concatenated, or a stub `assistant` message inserted between them, to
  preserve role alternation — see `translate_history_and_apply_prompt`
  in [`proxy.md`](proxy.md).
- **Earlier turns may be dropped entirely.** `harness probe` (2026-10-01,
  one sample each) failed every multi-message recall test even with clean
  alternation: `user, assistant, user` lost the fact from the first user
  turn (C01), a prior `assistant` turn was not treated as history (C03),
  and a `role:"tool"` result never informed the answer (D03). A fact in the
  same single user message is always seen (B03, C05). If this holds, only
  the last user message reaches the model, so the hybrid layout (tool
  definitions folded into message 0, history as alternating turns) loses
  the tool definitions and every earlier turn; what survives is the
  recency block on the last user message. Confirmed by `harness probe
  memory` (2026-10-05): 0 of 461 facts in earlier messages came back, while
  the same facts folded into the last message came back 63/63, and one
  message was recalled whole up to 700k chars. `harness probe
  optimize-single` measures which join format, size and framing work best
  for a whole chat folded into that one message.
- **Unreliable `usage`.** `usage.total_tokens` is per-request (the most
  recent request + response only), not cumulative for the conversation.
  It cannot be used for context tracking — the proxy estimates tokens
  locally instead (see "Local token estimation" in [`proxy.md`](proxy.md)).
  Responses now mark it `"is_estimated": true`.
  On the `-withtools` models `usage` is per-request and is exactly what an
  agent needs: the request's prompt size is the conversation size, which
  is what opencode compares against its compaction threshold. A stream only
  carries it when the request sets `stream_options.include_usage`, which
  the proxy now always does on the native route.
- **Sampling and shape parameters are ignored.** `max_tokens` (8 gave ~300
  words), `stop`, `temperature` (0 is not deterministic), `n` (always one
  choice) and `image_url` content parts have no effect. Array-of-text
  `content` parts are accepted.
- **Request size ceiling.** One 200k-char message (~56k upstream-estimated
  prompt tokens) worked; 600k chars returned `502 upstream_error`. The real
  ceiling is somewhere in between and unmeasured. `MODEL_CONTEXT_LENGTH`
  defaults to 200000 tokens, above that range, so a long session can hit
  502s before opencode compacts.
- **Streaming is coarse.** SSE arrives in a handful of large chunks (6
  events for a ~120-token reply) after a multi-second first byte.
- **TLS now verifies** against system CAs (the proxy still sends
  `verify=False`). `gemini_enterprise.thinking[]` came back empty on
  `gemini-3.8-flash`.

## Native-tools models (`-withtools`)

`harness probe` on `gemini-3.8-flash-withtools` (2026-10-13, one sample each)
found a standard OpenAI chat-completions contract, unlike the plain models:

- **Catalog.** `/v1/models` now lists only `*-withtools` ids (plus
  `gemini-embedding-2`), each with a `capabilities` object (it says
  `tools:false`, which is wrong), `lane`, `pool` and `credit_weights`;
  `owned_by` is `stark-proxy`. Whether the old plain ids still answer was not
  probed; a model the team cannot use returns `403 forbidden` "Your team does
  not have access to this model", which the startup auth probe now reports as
  a model problem, not a key problem.
- **Tools work.** `tools` + `tool_choice` (`auto`/`none`/`required`/named)
  return `message.tool_calls` with `finish_reason: tool_calls`, streamed as
  standard deltas. Parallel calls work (`parallel_tool_calls:false` is
  ignored). `role:"tool"` results, multi-step chains, an 11-tool opencode-like
  loop, nested/typed arguments, odd tool names (dots, dashes, 64 chars,
  `mcp__x__y`), 300-tool catalogs and the legacy `functions` API all worked.
- **System prompts and history are honored** (B01-B10, C01-C04), unlike the
  plain models.
- **Gemini response shape.** `tool_calls[].extra_content.google.thought_signature`,
  `usage.completion_tokens_details.reasoning_tokens`,
  `usage.extra_properties.google.traffic_type`, `system_fingerprint`; the
  response `model` is the base id (no `-withtools`). Round trips that omit
  the thought signature still worked.
- **Thinking** (`harness probe thinking`). Echoing a call's thought signature
  brings that step's reasoning back: step 2's `prompt_tokens` rose by step
  1's `reasoning_tokens`, so it is billed as input. A corrupted signature
  gets a bare 400 ("The request was invalid..."). Reasoning text sent back in
  history (`reasoning_content`) is ignored. Thought summaries come back only
  with `google.thinking_config.include_thoughts` (top level or in
  `extra_body`, snake or camel case) and `thinking_level` low/medium/high;
  `minimal` or a 0 budget is a 400, and so is `reasoning_effort` alongside
  `thinking_config`. Non-stream, the summary leads `content` as
  `<think>...</think>`; streamed, it arrives as plain content with no tags.
- **Parameters now apply.** `max_tokens` (truncates, `finish_reason:
  length`), `stop` and `stream_options.include_usage` work (without the
  latter a stream has no usage chunk at all, and opencode then counts 0
  tokens and never compacts; opencode does send it by default).
  `temperature:0`
  is still not deterministic and `n` still returns one choice.

The proxy forwards these models natively; see [`proxy.md`](proxy.md) →
Native tool models.

## A second upstream: an OpenAI-format API (`OAI_*`)

`OAI_API_URL` + `OAI_API_KEY` + `OAI_MODELS` add a real OpenAI-format API
(OpenAI, or Azure OpenAI's v1 surface `https://<resource>.openai.azure.us/openai/v1`)
beside the vendor upstream or instead of it. Its models take tools under their
own names (no `-withtools`). Facts the proxy is built on (Azure docs and
issue reports, 2026-10; not yet checked against the live Gov endpoint):

- **Paths** hang off the base: `{base}/chat/completions`, `{base}/responses`,
  `{base}/models`. Auth is `Authorization: Bearer <key>` (Azure v1 also takes
  `api-key`; send one, not both).
- **`/models` lists base models, not deployments**, so the ids requests use
  (Azure: deployment names) come from `OAI_MODELS`, never discovery.
- **Reasoning models reject `max_tokens`** (Chat takes
  `max_completion_tokens`, Responses `max_output_tokens`) and the sampling
  knobs (temperature, top_p, penalties).
- **gpt-5.6 on Azure rejects tools + reasoning on `/chat/completions`**:
  "Function tools with reasoning_effort are not supported ... use
  /v1/responses or set reasoning_effort to 'none'". Reasoning defaults to
  medium, so plain tools trip it. The Responses API takes both.
- **Prompt caching is automatic** from 1024 prompt tokens (prefix match; 5.6:
  30+ min retention). `prompt_cache_key` routes one session's requests to the
  same cache; `prompt_cache_retention` is deprecated on 5.6 and not sent.
  Hits are reported as `usage.prompt_tokens_details.cached_tokens` (Chat) or
  `usage.input_tokens_details.cached_tokens` (Responses).

How the proxy and opencode use it: [`proxy.md`](proxy.md) → OAI upstream and
[`harness-cli.md`](harness-cli.md) → Host mode.

## A second backend: the ChatGPT backend-api

Everything above describes the **default** upstream (`PROXY_BACKEND=openai`).
`harness chatgpt` points the same proxy at an unrelated API instead. It is not
OpenAI-compatible and shares none of the contract above.

- **Endpoint.** One path, `POST {CHATGPT_BASE_URL}/backend-api/conversation/stream`.
  Hardcoded in the proxy; only the base URL is configurable.
- **Auth.** A browser session cookie (`CHATGPT_COOKIE_STRING`), not a bearer
  key. There is no probe endpoint and no unlock URL, so `harness` skips the
  auth probe entirely for this backend. The cookie expires; the failure mode is
  a 4xx from the stream endpoint, surfaced to the agent as a 502 carrying the
  upstream body.
- **No catalog.** There is no `/v1/models`. `CHATGPT_MODEL_NAME` is both the
  requested model and the entire list the proxy synthesizes for opencode.
- **Request shape.** `{"action": "next", "model", "timezone",
  "timezone_offset_min", "messages": [{"author": {"role"}, "content":
  {"content_type": "text", "parts": [...]}}]}`.
- **Response shape.** SSE, `data: ` lines terminated by `data: [DONE]`. Two
  delta shapes are emitted and both occur: incremental
  `{"type": "message_delta", "delta": "..."}` events, and
  `message.content.parts` snapshots that are **cumulative** (each event repeats
  everything so far).
- **Server-side conversation state.** `conversation_id` / `parent_message_id`
  carry history between turns. The proxy does not use them — see
  [`proxy.md`](proxy.md) → Upstream backends.
- **No tool support**, same as the default upstream, so the cooperative-prompt
  machinery applies unchanged.

## Lanes, credits and rate limits (vendor docs, 2026-10-16)

Usage is **metered** now. The earlier "effectively unlimited" no longer holds.

**Lanes.** The model id picks a routing lane, and the lane decides billing
and behavior:

| Lane | Ids | Credits | Tools / system prompt |
|------|-----|---------|-----------------------|
| Standard | `gemini-3.5-flash`, `gemini-3.5-pro`, `gemini-2.5-*` in the capability matrix; `-withtools` aliases | metered, see weights | honored |
| Assist ("Gemini Enterprise") | `*-assist` (e.g. `gemini-3.5-pro-assist`), legacy `gemini-enterprise` | 0 (free; still TPM/RPM limited) | **system messages dropped, tools ignored** (sending tools "can lead to empty responses") |

The assist lane is the behavior the cooperative path was built for (hidden
system prompt, no tools), so the proxy's cooperative path is the way to use
it; it only costs TPM/RPM. The vendor docs contradict themselves on plain ids:
the capability matrix lists `gemini-3.5-flash` as Standard (1x/2x, tools),
while the "taxonomy" section says plain ids route to the zero-credit lane and
tool calling needs `-withtools`. Trust `/v1/models` (`lane`, `credit_weights`)
and `harness probe`, not either page.

**Credits.** `credits = prompt_tokens x W_in + completion_tokens x W_out`,
integer weights from {1, 2, 5, 10, 25, 50}, per model in the catalog's
`credit_weights`. Documented: flash 1x in / 2x out, pro 2x / 5x, embeddings
1x / 0x, assist 0x. Whether reasoning tokens bill as output is undocumented
(`harness probe` assumes yes, an upper estimate).

**Every request re-bills the whole conversation.** A chat-completions agent
resends the full history on every step, so one task costs roughly
`steps x average context`: 300 steps at ~150k tokens is ~45M. On 2026-10-16
one opencode prompt spent a 50M daily allowance in about 30 minutes; a fast
native step rate times a large re-sent context accounts for that without any
loop. The old plain models kept only the last message (above), so if they
were billed for what reached the model (unverified: the old path discarded
upstream usage) a step cost a few thousand tokens, not ~150k, which fits
hours of old-harness use costing a few million. `harness usage` totals what
the upstream reports per reply. See [`proxy.md`](proxy.md) → Native tool
models for the per-step trimming and the usage ledger.

**Limits.** Three dimensions, metered together:

- **TPM** (rolling 60 s, prompt + completion): throttling lasts seconds; honor
  `Retry-After`.
- **RPM** (rolling 60 s).
- **Daily budget** (rolling 24 h): frees up as the window rolls.

Documented tiers: Tier 0 default 2,000,000 credits/day, 200k TPM, 60 RPM;
Tier 1 5-10M credits/day, 500k TPM, 120 RPM. Rollout is phased; an account
not yet migrated keeps its old limit. Observed 2026-10-16 on a not-yet-migrated
key, a **token** allowance, not credits:

```
upstream returned status 429: You've used 50097465 of your 50000000 token
allowance for this 24-hour window. Your limit resets in 9h 19m.
```

At Tier 0 (2M credits) a metered agent session gets only a few dozen
100k-token steps a day, so the free assist lane matters more after cutover.

**Headers.** Every response carries `X-RateLimit-Limit-*` /
`X-RateLimit-Remaining-*` / `X-RateLimit-Reset-*` for `Credits-Day` and
`Tokens-Minute`, plus `x-request-id` (quote it in support tickets). A 429
carries `Retry-After` and a body with `error.type: rate_limit_exceeded`,
`error.message` and `error.retry_after_seconds`. The vendor's own example
pairs a daily-budget message with `Retry-After: 42`, so the proxy reads the
reset time from `X-RateLimit-Reset-Credits-Day` or the message, not
`Retry-After`, for a daily 429. `X-Usage-Recording-Failed: true` means
metering lagged; the request itself succeeded.

**Model alias retirement.** Capability suffixes such as `-withtools` are
deprecated on a 60-day window: during it, responses carry `Deprecation`,
`Sunset` and `Link: <...>; rel="successor-version"` headers; after it the
alias returns `410 Gone` with `error.code: model_retired` and
`error.details.replacement_model`. The vendor's example retires
`gemini-3.5-flash-withtools` on 2026-11-25 in favor of `gemini-3.5-flash`.
Until the canonical cutover is scheduled, tool calling still needs
`-withtools`. When it lands, add the canonical ids to
`PROXY_NATIVE_TOOL_MODELS` (the proxy routes by that pattern list).

## Endpoints (Phase 0)

`POST /v1/chat/completions` (unary + SSE, vision, structured output, tools),
`POST /v1/embeddings`, `GET /v1/models`, and `POST /v1/messages` (Anthropic
Messages compatibility; not used by the harness). `POST /v1/responses` is
planned (Phase 3); `POST /v1/completions` is retired. Browser-origin calls
fail CORS preflight with `401`, so clients must be server-side (the proxy is).

## API key lifecycle

- **Keys lock 14 days (336 h) after creation or the last unlock** (vendor
  docs, 2026-10-16; earlier docs and observations said 8 h). A personal key
  unlocks self-service via the `unlock_url`; a **team** key needs an
  administrator to unlock it in the portal.
- **Keys expire** on a fixed lifecycle date. An expired key is permanently
  dead (unlike a lock). Rotate before expiry: create a new key, deploy it to
  `.env`, verify traffic, revoke the old one.

### Lock / unlock flow

When a key is locked, requests return `401` with an unlock URL in the
body:

```json
{
  "error": {
    "type": "unauthorized",
    "message": "API key locked - visit the unlock URL to re-enable your key",
    "unlock_url": "https://.../unlock/<your-key-id>"
  }
}
```

Visiting `unlock_url` re-enables the key. If you are signed into the AI
account in the browser, just visiting the URL unlocks it; otherwise you
must sign in. **Unlocking cannot be automated** by the harness because it
needs a signed-in browser session. A non-committed future option: pull
the session key from a logged-in browser and use that — unclear whether
it is worth doing.

## Request / response schema

### Request body

```json
{
  "model": "gemini-2.5-flash",   // Required: model id
  "messages": [                  // Required: array of {role, content}
    { "role": "user", "content": "Hello!" }
  ],
  "temperature": 0.7,            // Optional: 0-2, default 1
  "max_tokens": 1000,            // Optional: max response tokens
  "stream": false                // Optional: enable streaming
}
```

### Response body

```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": { "content": "Hello, how are you?", "role": "assistant" }
    }
  ],
  "created": 1686935002,
  "id": "chatcmpl-abc123",
  "model": "gpt-4",
  "object": "chat.completion",
  "usage": {
    "completion_tokens": 46,
    "prompt_tokens": 8973,
    "total_tokens": 9019
  },
  "gemini_enterprise": {
    "assist_token": "xyz",
    "session": "projects/...",
    "thinking": [ "**Detecting Network Issues**\n" ]
  }
}
```

The `gemini_enterprise` block is upstream-specific: `assist_token`,
`session`, and a `thinking[]` array of the model's reasoning traces.
`usage` is per-request only (see "Unreliable `usage`" above).

### Models endpoint (`GET /v1/models`)

OpenAI-style catalog used for model discovery:

```json
{ "object": "list", "data": [ { "id": "gpt-4", "object": "model", "owned_by": "..." } ] }
```

Authenticated like the chat endpoint (Bearer key) and subject to the same
key-lock behavior — a locked key returns the `401` + `unlock_url` shape above.

## HTTP status codes

| Code | Meaning |
|------|---------|
| `400` | Bad Request — invalid request body or parameters |
| `401` | Unauthorized — invalid/missing API key, **or key locked** (see unlock flow) |
| `403` | Forbidden — key lacks permission; also `type=forbidden` for a model the team cannot use ("Your team does not have access to this model") and for an unparseable body ("restricted to specific models") |
| `404` | Not Found — resource does not exist (e.g. model not enabled) |
| `410` | Gone — a retired model alias past its 60-day window; body names `details.replacement_model`, `Link` header names the successor |
| `429` | Too Many Requests — TPM/RPM burst limit (wait `Retry-After` seconds) **or** the 24 h budget is spent (wait hours); see "Lanes, credits and rate limits" |
| `500` | Internal Server Error — server error |
| `502` | Bad Gateway — `upstream_error` catch-all: backend failure, missing `messages`, empty user content, or an oversized request |

A `401`/`403` carries an `error.type` that distinguishes *why* it failed, and
the harness auth probe keys off it (see [`harness-cli.md`](harness-cli.md) →
auth/model probes): `unauthorized` (or any non-`invalid_request` type, or no
type) means the **key** was rejected — e.g. a mis-pasted key produced
`{"error":{"type":"unauthorized","message":"Invalid token: ... Invalid symbol
47, offset 0."}}` (a leading `/` in the key), and the probe aborts the launch
(#108). `invalid_request` means the **request** was malformed but the key is
valid (e.g. a bad model id) — the probe warns and continues (#43). A locked key
is the `unauthorized` + `unlock_url` shape above and aborts with the unlock URL.
As of 2026-10-01 a model the team cannot use returns `403 type=forbidden`
instead, so a bad `DEFAULT_MODEL_NAME` now aborts the launch under the
"key rejected" banner; the dumped body shows the real reason.

## Self-reported internals (unverified)

Distilled from probing the API with direct questions in fresh chats.
**None of this is exposed through the API** — it is the chat model
describing itself, and is recorded only for context. Treat as
unverified.

- It identifies as **"Gemini Enterprise"** and is date/timezone-aware
  (knew the current date; reports a UTC default timezone).
- It **refuses to repeat its system prompt verbatim**, but summarizes it
  as: act as a first point of contact giving direct, cohesive, brief
  answers; draw on web data or its own knowledge; avoid repeating
  information; adapt to the user's tone and language; lean heavily on
  Markdown; delegate to specialized sub-agents for document generation
  or running code on uploaded files; maintain conversation history and
  remembered preferences.
- It **confirms it has no live web search.**
- It claims internal tools / sub-agents (names as reported):
  - `selfawareness_agent` — info about its own capabilities,
    operational status, activity logs, connector setup.
  - `generate_memories` — long-term cross-conversation memory (store /
    update / delete facts and preferences).
  - `transfer_to_agent` — orchestration / hand-off to sub-agents.
  - `docgen_agent` — generates formatted documents (PDF, DOCX, PPTX).
  - `file_and_coding_agent` — handles explicitly uploaded files and runs
    general code (plots, data exploration, calculations).
  - `invalid_tool_call_notifier` — internal notifier for invalid tool
    calls.
