# Roadmap: harness — ollama removal (proxy serves opencode directly)

## Overview

This milestone removes the ollama container from the data path so opencode talks
to the translating proxy directly over an OpenAI-compatible interface, while
preserving all upstream-facing mediation (prompt modes, tool-call extraction,
system→user conversion, empty-response rescue, token estimation). Because this is
a migration of a working system, the new path is built and proven end-to-end
before the old one is torn out: first the proxy learns to speak OpenAI inbound
and outbound (ollama still present as a fallback), then opencode is rewired to the
proxy and the direct path is demonstrated working, then the ollama
container/image/CLI/firewall wiring is deleted, and finally the full test matrix
and install/upgrade continuity are verified green on the new topology. Per repo
policy, architecture-doc and test updates ship in the same commit as the code
that changes their subject; the final phase owns only the cross-cutting closure
gate (full CI green + upgrade continuity) that can only be checked once everything
else lands.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: OpenAI-compatible proxy inbound** - Proxy accepts `/v1/chat/completions` and returns OpenAI-shaped responses with mediation preserved, ollama still in place as fallback
- [ ] **Phase 2: opencode cutover to the proxy** - opencode's config and model discovery point at the proxy; the direct opencode→proxy path is demonstrably working
- [ ] **Phase 3: ollama teardown** - Remove the ollama container, image, CLI management, and firewall wiring from the runtime
- [ ] **Phase 4: Topology hardening** - Full CI green on the new topology, docs free of the ollama hop, and clean upgrade continuity for existing clones

## Phase Details

### Phase 1: OpenAI-compatible proxy inbound
**Goal**: The proxy accepts OpenAI-compatible `POST /v1/chat/completions` requests and returns OpenAI-compatible responses (streaming and/or non-streaming, per what the opencode adapter requires), with every piece of upstream-facing mediation preserved unchanged. ollama remains in place so the working `/api/chat` path is an unbroken fallback throughout this phase. Resolve OQ-1 (SSE vs single-JSON outbound) and OQ-3 (which OpenAI request fields to honor vs ignore, including the inbound `Bearer harness-dummy` header) during discuss/plan.
**Depends on**: Nothing (first phase)
**Requirements**: REQ-001, REQ-002, REQ-003
**Success Criteria** (what must be TRUE):
  1. A request sent to the proxy's `/v1/chat/completions` in OpenAI request shape returns an OpenAI-shaped response that the `@ai-sdk/openai-compatible` provider can consume.
  2. Tool calls, prompt-mode injection, system→user conversion, empty-response rescue, and token estimation behave identically to before — the existing proxy mediation unit tests pass (updated only for inbound/outbound framing, not mediation logic).
  3. The proxy's existing ollama `/api/chat` NDJSON path still works, so the current opencode→ollama→proxy flow is unbroken while the new path is being validated.
  4. `architecture/proxy.md` describes the OpenAI inbound/outbound contract in the same commit as the code change.
**Plans**: TBD

Plans:
- [ ] 01-01: TBD

### Phase 2: opencode cutover to the proxy
**Goal**: opencode is rewired to talk to the proxy directly: its generated `opencode.json` provider `baseURL` targets `http://proxy:8000/v1` and model discovery reads the proxy's `/v1/models` pass-through instead of ollama `/api/tags`, with the selected model still flowing through to upstream unchanged. After this phase the direct opencode→proxy→upstream path is demonstrably driving real turns and tool calls with ollama out of the data path, even though the ollama container still physically exists (deleted in Phase 3). Resolve OQ-2 (exact `/v1/models` payload opencode needs; whether the pass-through suffices or a synthesized entry is required) during discuss/plan.
**Depends on**: Phase 1
**Requirements**: REQ-004, REQ-005
**Success Criteria** (what must be TRUE):
  1. A freshly started agent container produces an `opencode.json` whose provider `baseURL` points at the proxy, not ollama.
  2. opencode's model list is built from the proxy's `/v1/models` with no ollama `/api/tags` call, and the selected model reaches upstream unchanged (passthrough preserved).
  3. A real opencode turn completes and executes tool calls through the proxy with no ollama container in the data path.
  4. `architecture/containers.md` (agent config + model discovery) is updated in the same commit as the entrypoint change.
**Plans**: TBD
**UI hint**: no

Plans:
- [ ] 02-01: TBD

### Phase 3: ollama teardown
**Goal**: With the direct path proven, remove the ollama layer from the runtime: delete the `ollama` service from `docker-compose.yml` and the `ollama/` image (Dockerfile + stub-registration entrypoint), strip the ~55 ollama references from the `harness` CLI (start/stop/restart, doctor/preflight, port publish, data dirs, log service list), and update the firewall wiring so every remaining service stays behind the egress firewall with the same posture. No runtime artifact references the ollama service name after this phase.
**Depends on**: Phase 2
**Requirements**: REQ-006, REQ-007, REQ-008
**Success Criteria** (what must be TRUE):
  1. `harness start` brings up proxy + MCP services with no ollama container, and nothing in the compose graph references the ollama service name or `harness-net` ordering depends on it.
  2. `harness doctor` passes on the new topology and no CLI code path references ollama (management, health checks, data dirs, log service list).
  3. The egress firewall still constrains every remaining service with the same posture — no allowlist or bypass regression attributable to ollama removal.
  4. `architecture/containers.md`, `architecture/harness-cli.md`, and the `architecture/README.md` topology diagram are updated in the same commits as the corresponding deletions.
**Plans**: TBD

Plans:
- [ ] 03-01: TBD

### Phase 4: Topology hardening
**Goal**: Close out the migration as a whole: bring the docker-based and unit suites that referenced ollama to green on the proxy-direct topology in CI, ensure no architecture doc still describes an ollama hop, and make existing clones upgrade cleanly via `git pull` + `scripts/upgrade-manifest.json` without being stranded by removed ollama assets. This is the cross-cutting closure gate — doc and test updates ship with their code in earlier phases, but full-matrix CI green and upgrade continuity are only fully checkable once everything else has landed. Resolve OQ-4 (delete `state/ollama-data/` on upgrade vs leave-and-ignore — choose the least-surprising retirement) during discuss/plan.
**Depends on**: Phase 3
**Requirements**: REQ-009, REQ-010, REQ-011
**Success Criteria** (what must be TRUE):
  1. CI passes the full matrix on the migration branch with no ollama-dependent test remaining (the obsolete ollama-entrypoint coverage is retired and the new direct opencode→proxy path is covered).
  2. No architecture doc describes an ollama hop, and the README data-flow diagram shows opencode → proxy → upstream.
  3. An upgrade from a pre-migration clone completes without erroring on removed ollama assets, and ollama runtime state (`state/ollama-data/`) is handled deliberately (retired or migrated), not orphaned.
**Plans**: TBD

Plans:
- [ ] 04-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. OpenAI-compatible proxy inbound | 0/TBD | Not started | - |
| 2. opencode cutover to the proxy | 0/TBD | Not started | - |
| 3. ollama teardown | 0/TBD | Not started | - |
| 4. Topology hardening | 0/TBD | Not started | - |
