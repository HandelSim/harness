# harness

## What This Is

harness is a container-runtime system that lets the opencode coding agent talk
to a third-party chat-completions API transparently, with a translating proxy in
the middle that injects cooperative tool-use prompting so models without native
tool calling still drive an agent loop. Today the agent reaches the proxy through
an intermediate ollama container; this project removes that layer so opencode
talks to the proxy directly.

## Core Value

A working coding agent (opencode) driving real tool calls against the user's
working directory through the translating proxy. If everything else fails, the
opencode -> proxy -> upstream path with cooperative tool-use mediation must keep
working.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. Inferred from existing code (brownfield). -->

- ✓ Translating proxy that converts between the agent wire format and the
  upstream chat-completions API — existing (`proxy/proxy.py`)
- ✓ Cooperative tool-use prompt injection (hybrid / user_front / passthrough
  modes) so non-tool-native upstreams emit parseable ```json tool calls — existing
- ✓ Tool-call extraction with balanced-brace JSON scanning, lenient parsing, and
  tolerant argument lift — existing
- ✓ Empty-response rescue (text + no-op `bash pwd`) to unstick upstream filters — existing
- ✓ `harness` management CLI: start/stop/restart, opencode/shell launch, doctor,
  update/upgrade, MCP lifecycle — existing
- ✓ Universal egress firewall shared by all services — existing
- ✓ MCP registry: install/enable/disable/uninstall vetted MCP services and merge
  them into the agent's client config — existing
- ✓ Model passthrough: the selected upstream model flows opencode -> proxy ->
  upstream unchanged; `/v1/models` pass-through route exists — existing
- ✓ Docker + Podman runtime support, install/upgrade flow operating on the clone — existing
- ✓ Extensive bash + python test suite (proxy unit, integration, firewall,
  persistence, mcp, scheme-contract, full-pipeline, benchmarks) — existing

### Active

<!-- Current scope: the ollama-removal milestone. -->

- [ ] opencode talks directly to the proxy (no ollama container in the data path)
- [ ] The proxy accepts an OpenAI-compatible inbound interface
      (`/v1/chat/completions`) and returns OpenAI-compatible responses, replacing
      the ollama `/api/chat` + NDJSON inbound contract
- [ ] opencode's generated config points its provider `baseURL` at the proxy and
      discovers models from the proxy's `/v1/models` (not ollama `/api/tags`)
- [ ] The ollama container, image, and entrypoint are removed from compose, the
      CLI, install/upgrade, and the firewall wiring
- [ ] All cooperative tool-use mediation (prompt modes, tool-call extraction,
      system->user conversion, empty-response rescue, token estimation) is
      preserved across the inbound-format change
- [ ] The test suite, CI, and architecture docs are updated to the new direct
      opencode -> proxy topology

### Out of Scope

<!-- Explicit boundaries. -->

- Supporting coding agents other than opencode — the original reason ollama was
  added; the maintainer no longer wants multi-agent support, which is what makes
  ollama removable. The proxy now manages the opencode interface specifically.
- Running local/offline models via ollama — harness only ever used ollama as a
  forwarding stub to the upstream API, not for local inference.
- Changing the upstream API contract or the cooperative-prompt strategy — this
  milestone is a transport/topology change, not a mediation redesign.

## Context

- **Current data flow:** opencode (container, `@ai-sdk/openai-compatible`
  provider, `baseURL: http://ollama:11434/v1`) -> ollama stub model
  (`OLLAMA_REMOTES`/registered stub forwards as ollama `/api/chat`) -> proxy
  (`proxy/proxy.py`, translates to OpenAI and calls upstream) -> upstream API.
- **Target data flow:** opencode (provider `baseURL: http://proxy:8000/v1`) ->
  proxy (`/v1/chat/completions`, OpenAI-compatible inbound and outbound) ->
  upstream API. The ollama hop is deleted.
- **Why ollama is removable:** opencode already uses the OpenAI-compatible
  adapter; ollama is acting only as an OpenAI-in / ollama-out forwarding shim.
  The proxy already speaks OpenAI outbound and already exposes a `/v1/models`
  pass-through. The remaining work is making the proxy speak OpenAI *inbound*
  instead of ollama `/api/chat`, and rewiring opencode + compose + the CLU.
- **Migration surface (verified):** ~40 files reference ollama. Heaviest:
  `harness` CLI (~55 refs), `tests/harness_test.sh` (~49),
  `tests/firewall_test.sh` (~41), `proxy/proxy.py` (~23, inbound format),
  `ollama/` (Dockerfile + entrypoint, deleted wholesale), `docker-compose.yml`
  (~19), `agents/entrypoint.sh` (opencode config + model discovery), plus tests,
  docs (`architecture/proxy.md`, `architecture/containers.md`), and
  install/upgrade. See `.planning/codebase/CONCERNS.md` for the full
  coupling-point map.
- **Largest single change:** `proxy/proxy.py` inbound — `catch_all` expects the
  ollama `{model, messages, tools}` chat shape and `generate_ndjson`/`make_chunk`
  emit ollama NDJSON. These become OpenAI request parsing + OpenAI response
  emission. The upstream call, tool-call extraction, prompt injection,
  system->user conversion, and empty-response rescue are upstream-facing/internal
  and do not depend on the ollama wire format.
- **Doc discipline:** the repo keeps architecture docs in `architecture/*.md` and
  requires updating the relevant doc in the same commit as a behavior change (see
  `CLAUDE.md` and `architecture/README.md`). The migration must carry doc updates.

## Constraints

- **Tech stack**: Python 3 / Flask proxy, large bash CLI, bash container
  entrypoints, docker-compose (docker and podman). Stay within this stack; no
  rewrite.
- **Compatibility**: opencode uses the `@ai-sdk/openai-compatible` provider —
  the proxy's new inbound contract must satisfy that adapter (OpenAI
  `/v1/chat/completions` request and response shape, including how streaming is
  handled). This is the binding interface for the whole migration.
- **Mediation preserved**: cooperative tool-use behavior (hybrid prompt mode,
  tool-call extraction, empty-response rescue, token estimation) must survive the
  inbound-format change with no regression — it is the core value.
- **Firewall**: every service runs behind the universal egress firewall; removing
  ollama must not open or break the firewall wiring.
- **Tests as the gate**: docker-based suites (proxy, harness, persistence, mcp,
  firewall, scheme_contract, full_pipeline) run in CI on every push/PR. The
  migration is "done" only when CI is green on the new topology.
- **Install/upgrade continuity**: existing installs upgrade via `git pull` +
  `scripts/upgrade-manifest.json`; the migration must bring forward or retire
  ollama state cleanly rather than stranding it.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Remove the ollama container; opencode talks to the proxy directly | ollama was added only to support multiple coding agents, which is no longer wanted; it is now a heavy, redundant forwarding layer | — Pending |
| Proxy manages an OpenAI-compatible inbound interface instead of the ollama `/api/chat` interface | opencode already uses the OpenAI-compatible adapter and the proxy already speaks OpenAI outbound; this is the smallest contract that lets ollama be deleted | — Pending |
| Exact inbound streaming contract (OpenAI SSE vs single JSON response) and the precise `/v1/models` rewiring | These are the main open design questions; resolve them in `/gsd:discuss-phase` / `/gsd:plan-phase`, not at onboarding | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? -> Move to Out of Scope with reason
2. Requirements validated? -> Move to Validated with phase reference
3. New requirements emerged? -> Add to Active
4. Decisions to log? -> Add to Key Decisions
5. "What This Is" still accurate? -> Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-02 after initialization*
