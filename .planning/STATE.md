# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-02)

**Core value:** A working coding agent (opencode) driving real tool calls against the user's working directory through the translating proxy — the opencode → proxy → upstream path with cooperative tool-use mediation must keep working.
**Current focus:** Phase 4 — hardening (tests/docs cleanup); ollama fully removed

## Current Position

Phase: 4 of 4 (hardening — tests green, docs free of the ollama hop, install/upgrade continuity)
Plan: direct execution (headless oak worker; not using interactive /gsd:execute-phase)
Status: Phases 1–3 complete; Phase 4 code-complete from the worker side, final
green is CI-gated (docker suites run in CI, not locally per CLAUDE.md). ollama
service/image/CLI refs/firewall remotes and the proxy `/api/chat` NDJSON path
are all deleted; the whole test catalog (proxy/harness/firewall/scheme_contract/
full_pipeline/integration/persistence/mcp/podman + benchmarks) plus INVENTORY/
COVERAGE are migrated to the OpenAI-only topology.
Last activity: 2026-06-02 — Phase 3 teardown + Phase 3b/4 test+docs cleanup pushed to nollama

Progress: [█████████░] 95%

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
- **Phase 2 cutover:** opencode's provider `baseURL` now points at
  `http://proxy:${PROXY_PORT}/v1` (was `http://ollama:11434/v1`); the model
  dropdown is built from the proxy's `/v1/models` catalog (`.data[].id`,
  OpenAI list shape) instead of ollama `/api/tags`. `PROXY_PORT` is passed
  into the agent container via `-e` (added to `agent_common_env` in `harness`)
  since the agent run path uses no `--env-file`. Model discovery now depends
  on the proxy/upstream being reachable at launch; falls back to
  `DEFAULT_MODEL_NAME` alone otherwise (cosmetic dropdown; selection still
  works).
- **Phase 3 teardown:** ollama service deleted from `docker-compose.yml`,
  `ollama/` image dir removed, proxy `/api/chat` + NDJSON emit path deleted
  (proxy is OpenAI-only: `/v1/chat/completions`, `/v1/models`, `/health`),
  firewall remotes for ollama dropped. `OLLAMA_CONTEXT_LENGTH` kept ONLY as a
  legacy read-alias for the new `MODEL_CONTEXT_LENGTH` so existing `.env`
  files keep working; `OLLAMA_AGENT_MODEL`/`PROXY_API_MODEL` collapsed into
  `DEFAULT_MODEL_NAME`. Proxy port stays unpublished — in-network tests exec
  `curl` inside the proxy container.
- **Phase 3b/4 cleanup:** whole test suite + tests/INVENTORY.md +
  tests/COVERAGE.md migrated to OpenAI shape (call_-prefixed tool ids,
  `chat.completion`/`chat.completion.chunk`, `data: [DONE]`,
  `04_OpenAI_Response_*`/`04_OpenAI_SSE_Response_*` dumps). Coverage losses
  with no OpenAI analog were **deleted, not faked**, and flagged: the
  cross-service iptables isolation check and O001 firewall-ordering block
  (no second long-running firewalled service remains) and Pe005.

### Pending Todos

[From .planning/todos/pending/ — ideas captured during sessions]

None yet.

### Blockers/Concerns

[Issues that affect future work]

- **Forbidden-file stale refs (for owner, not worker-fixable):** `CLAUDE.md`
  line ~43 and `.github/workflows/ci.yml` still mention ollama. Both are under
  the CLAUDE.md "Forbidden from modifying" guard, so left untouched. CI does
  **not** break — `hashFiles('ollama/**')` tolerates the now-missing dir. Owner
  should sweep these when convenient.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| *(none)* | | | |

## Session Continuity

Last session: 2026-06-02
Stopped at: Phase 3 teardown + Phase 3b/4 test+docs cleanup complete and pushed
to `nollama`; ollama fully removed from the data path. Next: confirm CI green on
the docker suites, then proceed to the `harness mcp register` feature
(.planning/proposals/mcp-register.md).
Resume file: None
