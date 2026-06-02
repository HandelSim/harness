# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-02)

**Core value:** A working coding agent (opencode) driving real tool calls against the user's working directory through the translating proxy — the opencode → proxy → upstream path with cooperative tool-use mediation must keep working.
**Current focus:** Phase 2 — opencode cutover to the proxy

## Current Position

Phase: 2 of 4 (opencode points at the proxy, model discovery off /v1/models)
Plan: direct execution (headless oak worker; not using interactive /gsd:execute-phase)
Status: Phase 1 complete — proxy speaks OpenAI inbound; Phase 2 next
Last activity: 2026-06-02 — Phase 1 implemented (proxy OpenAI-compatible inbound)

Progress: [██░░░░░░░░] 25%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: - min
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: -

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- **OQ-1 resolved (Phase 1):** opencode's `@ai-sdk/openai-compatible` provider
  drives `streamText`→`doStream`, always sends `stream: true`, and parses
  `text/event-stream`. SSE is **mandatory**; single-JSON is not on opencode's
  hot path (kept only for `stream:false` curl/debug). Verified against the
  provider source + opencode `session/llm.ts`.
- **OQ-2 resolved (Phase 1):** the AI SDK never calls `/v1/models`; opencode
  reads its model list from config. The existing `/v1/models` passthrough is
  kept for manual discovery/debug but is not on opencode's path.
- **OQ-3 resolved (Phase 1):** streamed tool-call deltas need `index` (always),
  `id`+`function.name` on the first fragment, and `function.arguments` as a
  JSON **string** that accumulates to valid JSON. Errors use the
  `{"error":{"message":…}}` envelope. opencode sends
  `stream_options:{include_usage:true}`, so a usage chunk is emitted.
- **Phase 1 kept the ollama `/api/chat` path** alongside the new
  `/v1/chat/completions` path (dispatch on request path in `catch_all`) so each
  checkpoint runs end-to-end; the ollama path is deleted in Phase 3.

### Pending Todos

[From .planning/todos/pending/ — ideas captured during sessions]

None yet.

### Blockers/Concerns

[Issues that affect future work]

None yet.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| *(none)* | | | |

## Session Continuity

Last session: 2026-06-02 00:00
Stopped at: GSD onboarding — roadmap and state created
Resume file: None
