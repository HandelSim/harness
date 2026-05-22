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
(passthrough) and registers an ollama stub per advertised model so the user can
switch models from opencode. See [`proxy.md`](proxy.md) → URL base + model
passthrough. (Earlier the harness pinned one model — `gemini 3.1 pro` was the
one clear best choice — which is why older docs/config spoke of a single model.)

Note: the upstream's own request/response examples use placeholder model
ids (`gemini-2.5-flash`, `gpt-4`) and example `usage` numbers that do not
correspond to a real exchange — they document JSON *shape* only, not
authoritative values.

## Contract and quirks

These are the load-bearing behaviors the proxy is built around:

- **No tool support.** A `tools` field in the request is ignored; the
  response never contains `tool_calls`. This is *why* the proxy does
  cooperative-prompt tool-use — see [`proxy.md`](proxy.md).
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
- **Unreliable `usage`.** `usage.total_tokens` is per-request (the most
  recent request + response only), not cumulative for the conversation.
  It cannot be used for context tracking — the proxy estimates tokens
  locally instead (see "Local token estimation" in [`proxy.md`](proxy.md)).

## API key lifecycle

- **Keys lock every 8 hours.** Unclear whether the 8h is measured from
  last use or from last unlock — **needs testing.**
- **Keys expire after ~1 month.**
- **Usage is effectively unlimited** — no rate-limit concern for now
  (though the API can still return `429`, see status codes below).

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
| `403` | Forbidden — key lacks permission for this endpoint |
| `404` | Not Found — resource does not exist (e.g. model not enabled) |
| `429` | Too Many Requests — rate limit exceeded |
| `500` | Internal Server Error — server error |
| `502` | Bad Gateway — upstream LLM backend failure |

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
