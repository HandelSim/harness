# Requirements: ollama removal — proxy serves opencode directly

Requirements for the current milestone: remove the ollama container from the data
path and have opencode communicate directly with the translating proxy over an
OpenAI-compatible interface. Scope, rationale, and constraints live in
`PROJECT.md`; the codebase coupling map lives in `.planning/codebase/CONCERNS.md`.

Status legend: `Active` (in scope, not yet built) · `Validated` (shipped &
confirmed) · `Deferred` (out of scope this milestone).

## Functional

### REQ-001 — Proxy accepts OpenAI-compatible inbound requests
**Status:** Active
The proxy exposes `POST /v1/chat/completions` accepting the OpenAI
chat-completions request shape (`model`, `messages`, `tools`, `stream`, sampling
params) that the `@ai-sdk/openai-compatible` provider emits. This replaces the
ollama `/api/chat` `{model, messages, tools}` inbound contract as the agent-facing
entry point.
**Acceptance:** opencode, configured against the proxy, completes a chat turn
through `/v1/chat/completions` with no ollama container running.

### REQ-002 — Proxy returns OpenAI-compatible responses
**Status:** Active
The proxy returns responses in the OpenAI chat-completions shape — both the
streaming form (SSE `data:` chunks with `choices[].delta`, terminated by
`[DONE]`) and/or the non-streaming single-JSON form, per whichever the opencode
adapter requires. This replaces the ollama NDJSON streaming
(`generate_ndjson` / `make_chunk`).
**Acceptance:** opencode renders streamed assistant text and executes tool calls
returned by the proxy; response framing validated by the proxy test suite.

### REQ-003 — Cooperative tool-use mediation preserved
**Status:** Active
All upstream-facing mediation behavior is preserved unchanged across the
inbound-format swap: prompt modes (hybrid / user_front / passthrough), tool-call
extraction (balanced-brace JSON scan, lenient parse, tolerant argument lift),
system→user message conversion, empty-response rescue (text + no-op `bash pwd`),
and token estimation.
**Acceptance:** existing proxy unit tests covering extraction, prompt modes, and
rescue pass unmodified (or are updated only for inbound framing, not mediation
logic); no behavioral regression in tool-call emission.

### REQ-004 — opencode configured to talk to the proxy directly
**Status:** Active
`agents/entrypoint.sh` `ensure_opencode_config()` generates an `opencode.json`
whose provider `baseURL` targets the proxy (`http://proxy:8000/v1`) instead of
`http://ollama:11434/v1`, keeping the `@ai-sdk/openai-compatible` provider.
**Acceptance:** a freshly started agent container produces an opencode config
pointing at the proxy and successfully drives a turn.

### REQ-005 — Model discovery sourced from the proxy
**Status:** Active
opencode model discovery reads from the proxy's `/v1/models` pass-through route
instead of ollama `/api/tags`. The configured/selected upstream model continues to
flow opencode → proxy → upstream unchanged.
**Acceptance:** the model list opencode sees is produced without any ollama call;
the selected model reaches upstream unchanged (passthrough preserved).

### REQ-006 — ollama container removed from the runtime
**Status:** Active
The ollama service is removed from `docker-compose.yml`, and the `ollama/`
image (Dockerfile + `entrypoint.sh` stub-model registration) is deleted. No
service in the compose graph depends on ollama; `harness-net` and service startup
ordering remain correct without it.
**Acceptance:** `harness start` brings up proxy + MCP services with no ollama
container; nothing references the ollama service name at runtime.

### REQ-007 — `harness` CLI de-ollama'd
**Status:** Active
The `harness` CLI (~55 ollama references) no longer starts, health-checks,
waits on, or otherwise manages an ollama container. `doctor`/preflight,
start/stop/restart, and any ollama-data handling are updated to the proxy-direct
topology.
**Acceptance:** `harness doctor` passes on the new topology; no CLI code path
references ollama.

### REQ-008 — Firewall wiring updated, egress posture preserved
**Status:** Active
Removing ollama does not weaken or break the universal egress firewall. Firewall
config and tests that referenced the ollama service are updated so every
remaining service still runs behind the firewall with the same egress posture.
**Acceptance:** firewall tests pass on the new topology; no allowlist/bypass
regression attributable to ollama removal.

## Non-Functional

### REQ-009 — Test suite updated and green on the new topology
**Status:** Active
The docker-based and unit suites that reference ollama (`harness_test.sh` ~49,
`firewall_test.sh` ~41, proxy tests, persistence, mcp, scheme_contract,
full_pipeline) are updated to the proxy-direct topology and pass in CI. CI runs
the full matrix on push/PR; the milestone is "done" only when CI is green.
**Acceptance:** CI passes on the migration branch with no ollama-dependent test
remaining.

### REQ-010 — Architecture docs updated in the same commits
**Status:** Active
Architecture docs are updated alongside the code that changes their subject:
`architecture/proxy.md` (inbound contract), `architecture/containers.md` (ollama
service gone), `architecture/README.md` (topology diagram + tree),
`architecture/harness-cli.md` (no ollama management), and any others whose
behavior changes. Per repo policy, doc updates ship in the same commit as the
code.
**Acceptance:** no architecture doc describes an ollama hop after the migration;
the README data-flow diagram shows opencode → proxy → upstream.

### REQ-011 — Install / upgrade continuity for existing clones
**Status:** Active
Existing installs upgrade cleanly via `git pull` + `scripts/upgrade-manifest.json`
without being stranded by removed ollama assets. Ollama runtime state
(`state/ollama-data/`) and any ollama-specific config are retired or migrated
rather than left dangling, and the upgrade path does not error on their absence.
**Acceptance:** an upgrade from a pre-migration clone completes; ollama state is
handled deliberately (retired/migrated), not orphaned.

## Open Questions (resolve in discuss/plan, not onboarding)

- **OQ-1 (feeds REQ-002):** Does the opencode `@ai-sdk/openai-compatible` provider
  require SSE streaming, accept a single non-streamed JSON response, or need both?
  This determines the proxy's outbound response implementation.
- **OQ-2 (feeds REQ-005):** What exact `/v1/models` payload does opencode need for
  discovery, and is the existing pass-through route sufficient or does it need a
  synthesized entry for the configured model?
- **OQ-3 (feeds REQ-001):** Which OpenAI request fields must be honored vs safely
  ignored (e.g. `stream_options`, `tool_choice`, sampling params) for opencode to
  function?
- **OQ-4 (feeds REQ-011):** Should `state/ollama-data/` be deleted on upgrade, or
  left in place and ignored? Decide the least-surprising retirement.

## Traceability

Phase → requirement mapping is filled in by `ROADMAP.md` during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| REQ-001 | TBD | Active |
| REQ-002 | TBD | Active |
| REQ-003 | TBD | Active |
| REQ-004 | TBD | Active |
| REQ-005 | TBD | Active |
| REQ-006 | TBD | Active |
| REQ-007 | TBD | Active |
| REQ-008 | TBD | Active |
| REQ-009 | TBD | Active |
| REQ-010 | TBD | Active |
| REQ-011 | TBD | Active |

---
*Last updated: 2026-06-02 after initialization*
