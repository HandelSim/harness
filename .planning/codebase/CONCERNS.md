# Codebase Concerns

**Analysis Date:** 2026-06-02

---

## Ollama Coupling — Migration Surface Map

This section is the primary reference for the planned ollama-removal migration. Every file and coupling listed here must be addressed before opencode can talk to the proxy directly.

### Architecture of the current coupling

```
opencode (agent container)
  └── reads ~/.config/opencode/opencode.json
        └── provider.harness.options.baseURL = "http://ollama:11434/v1"
                        │
                        ▼ (OpenAI-compatible /v1/chat/completions)
              ollama stub model container
                        │  OLLAMA_REMOTES=proxy allows forwarding to:
                        ▼ (ollama's /api/chat wire format — NDJSON responses)
              proxy container (proxy/proxy.py)
                        │
                        ▼ (OpenAI /v1/chat/completions — full response, not streaming)
              upstream API (Gemini Enterprise)
```

After removal, the target is:

```
opencode → http://proxy:8000/v1/chat/completions (OpenAI-compatible)
proxy → upstream API
```

### Coupling point 1: `agents/entrypoint.sh` — opencode config hardcodes ollama URL

- **File:** `agents/entrypoint.sh:135`, `agents/entrypoint.sh:146`, `agents/entrypoint.sh:178–199`
- `ensure_opencode_config()` writes `~/.config/opencode/opencode.json` with `"baseURL": "http://ollama:11434/v1"` (line 135). This is the single line that routes opencode through ollama.
- The model list is built by querying `http://ollama:11434/api/tags` (line 146) — ollama-specific endpoint, not `/v1/models`.
- The config uses `@ai-sdk/openai-compatible` npm adapter (line 185), which is OpenAI-compatible. The adapter itself is not ollama-specific; only the `baseURL` value is.
- **Migration change:** Replace `baseURL` with `http://proxy:${PROXY_PORT:-8000}/v1`. Replace the model-list query from `ollama /api/tags` to `proxy /v1/models` (which already exists as a pass-through route).
- **Risk:** The `ensure_opencode_config` function also sets `limit.context` from `OLLAMA_CONTEXT_LENGTH` (line 136, 171). After removal, this env var name becomes misleading — should be renamed or the value sourced differently.
- **Risk:** The fallback when ollama is unreachable (line 157–160) falls back to `DEFAULT_MODEL_NAME` alone. Post-migration the equivalent fallback would be when the proxy `/v1/models` is unreachable.

### Coupling point 2: `agents/entrypoint.sh` — model discovery via ollama `/api/tags`

- **File:** `agents/entrypoint.sh:146–155`
- `tags=$(curl -fsS --max-time 10 "http://ollama:11434/api/tags")` queries ollama's stub registration state to build opencode's model dropdown.
- The `/api/tags` response has the shape `{"models":[{"name":"<id>:latest",...}]}` — `.models[].name` with `:latest` suffix stripped.
- The proxy's `/v1/models` pass-through returns OpenAI shape `{"data":[{"id":"<id>",...}]}` — `.data[].id`, no suffix to strip.
- **Migration change:** Replace the `/api/tags` curl + jq expression with a call to `http://proxy:${PROXY_PORT:-8000}/v1/models` using `.data[].id`.

### Coupling point 3: `proxy/proxy.py` — inbound wire format is ollama's `/api/chat`

- **File:** `proxy/proxy.py:1559–1565`, `proxy/proxy.py:1393–1420`
- `catch_all` reads the inbound JSON body expecting ollama's chat request shape: `{model, messages, tools}` (line 1565–1570).
- `generate_ndjson` + `make_chunk` (lines 1362–1420) emit ollama's streaming chat-response wire format: `{model, created_at, message:{role,content,[tool_calls]}, done, done_reason, ...}`.
- The mimetype returned is `application/x-ndjson` (line 1725).
- **Post-migration:** If opencode talks directly to the proxy using OpenAI-compatible format (`/v1/chat/completions`), the proxy must accept OpenAI-shaped requests and return OpenAI-shaped responses (not ollama NDJSON). This is the largest single change in `proxy.py`.
- **What stays the same:** The upstream call, tool-call extraction, cooperative-prompt injection, system→user conversion, and empty-response rescue are all upstream-facing or internal logic; they do not depend on the ollama wire format.
- **What must change:** The inbound request parser (currently reads `messages`, `tools`, `model` from a generic JSON body — this is actually already compatible with OpenAI shape), the outbound response format (NDJSON → OpenAI `choices[0].message` + `usage`), and the streaming behavior (currently materializes full response then emits NDJSON; OpenAI streaming uses SSE `data: {...}\n\n` lines).
- **Verified:** The `/api/chat` path is not an explicit Flask route — `catch_all` accepts ANY path. So the proxy will respond to `/v1/chat/completions` already, but the response format will be wrong (NDJSON instead of OpenAI).

### Coupling point 4: `proxy/proxy.py` — `_strip_model_tag` strips `:latest` suffix

- **File:** `proxy/proxy.py:84–94`, `proxy/proxy.py:1582`
- ollama registers stubs as `<id>:latest` and forwards the tagged name. `_strip_model_tag` removes the `:latest` suffix before sending to upstream.
- **Post-migration:** opencode sends the bare model id selected from `/v1/models` (no `:latest` suffix). `_strip_model_tag` becomes a no-op but is harmless if left in.

### Coupling point 5: `ollama/entrypoint.sh` — model registration via `/api/create`

- **File:** `ollama/entrypoint.sh:65–96`, `ollama/entrypoint.sh:111–133`
- Entire file exists to start ollama, register stub models via `POST /api/create` pointing `remote_host` at the proxy, and discover models via `GET proxy:/v1/models`.
- **Post-migration:** This file and the `ollama/` directory (Dockerfile + entrypoint) are entirely removed. The `state/ollama-data/` directory becomes unused.
- The model-discovery logic in `ollama/entrypoint.sh` (lines 111–133) already calls the proxy's `/v1/models` route. The same call moves to `agents/entrypoint.sh` for the model dropdown.

### Coupling point 6: `docker-compose.yml` — ollama service definition

- **File:** `docker-compose.yml:23–77`
- The `ollama` service: builds `ollama/Dockerfile`, sets `OLLAMA_HOST`, `OLLAMA_REMOTES`, `DEFAULT_MODEL_NAME`, `OLLAMA_CONTEXT_LENGTH`, `PROXY_PORT`; mounts `state/ollama-data`; has `depends_on: proxy: service_healthy`.
- **Post-migration:** Remove the `ollama` service block entirely. The `proxy` service's `depends_on` block no longer needs to exist (no ollama to gate on). The `agent` service needs no changes to its compose definition (it is launched by `docker run`, not compose).
- **Load-bearing comment** in `docker-compose.yml` line 3–7: "The hostname `proxy` in particular is load-bearing: ollama's OLLAMA_REMOTES allowlist matches on the literal hostname." This comment becomes stale and should be removed.

### Coupling point 7: `harness` CLI — ollama references throughout

- **File:** `harness` (5111 lines)
- `PUBLISH_OLLAMA_PORT` env var and runtime-override block (lines 673–693): adds a host port publish for ollama. Post-migration this var and the block generating it are dead.
- `ensure_dirs` (line 562–567): creates `state/ollama-data/`. Post-migration this directory is no longer needed.
- Doctor checks (lines 4338–4387): `ollama_status`, `doctor_check ok/warn "ollama"`, stub-model check via `docker exec` into the ollama container. All become dead code.
- `doctor_check "OLLAMA_VERSION"` (lines 4242–4245): checks the env var.
- `harness_docker exec "$ollama_cid"` (lines 4362, 4372–4376): runs commands inside the ollama container for health checks.
- Log references: `harness logs ollama` (line 3765, 3772, etc.) and the corresponding help text (line 3807) include `ollama` in the service list.
- Startup guidance on failure (line 1427): prints `harness-ollama-1` container log suggestion.
- Image presence check (line 4430): checks for `harness-ollama:${OLLAMA_VERSION}` image.

### Coupling point 8: `harness` CLI — `OLLAMA_VERSION` and `OLLAMA_CONTEXT_LENGTH` env vars

- **Files:** `harness`, `.env.example`, `docker-compose.yml`, `agents/entrypoint.sh`, multiple test files
- `OLLAMA_VERSION` in `.env.example` (line 93), `docker-compose.yml` (line 29), `harness` (line 4242), test env fixtures (every test that writes an env file). Post-migration this var is dead.
- `OLLAMA_CONTEXT_LENGTH` in `.env.example` (line 100), `docker-compose.yml`, `ollama/entrypoint.sh`, `agents/entrypoint.sh` (line 136), `proxy/proxy.py` (line 54), tests. Post-migration this var is still load-bearing for the agent's opencode config (setting context limit) and the proxy's token estimator cap — but the name is misleading. Rename to `HARNESS_CONTEXT_LENGTH` or document the retained use.

### Coupling point 9: Tests — every docker-based test suite starts the ollama service

- **Files:** `tests/proxy_test.sh`, `tests/harness_test.sh`, `tests/full_pipeline_test.sh`, `tests/integration_test.sh`, `tests/mcp_test.sh`, `tests/persistence_test.sh`, `tests/podman_smoke_test.sh`, `tests/scheme_contract_test.sh`, `tests/firewall_test.sh`, `tests/benchmarks/mock-smoketest.sh`, `tests/lib/test_helpers.sh`
- `proxy_test.sh` sends all test traffic via `http://localhost:11434/api/chat` (lines 220, 286, 312, 415, 455) — the ollama-exposed port. Post-migration tests must send to `http://localhost:${PROXY_PORT}/v1/chat/completions`.
- `scheme_contract_test.sh` similarly drives through `OLLAMA_URL="http://localhost:11434"` (line 147).
- `harness_test.sh` and `full_pipeline_test.sh` assert on ollama health state, ollama entrypoint log markers (O003, O009), and `state/ollama-data/` non-empty (Pe005).
- `firewall_test.sh` asserts specifically on the ollama container's iptables state (lines 252–276) to verify firewall ordering.
- **Migration impact:** The entire O### inventory section (16 items) and associated green coverage items become obsolete. New tests for the direct opencode→proxy connection path must be written to fill the gap.

### Coupling point 10: `proxy/proxy.py` — `OLLAMA_CONTEXT_LENGTH` env var

- **File:** `proxy/proxy.py:54, 1359`
- Read at startup: `OLLAMA_CONTEXT_LENGTH: int = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "200000"))`.
- Used only in `_estimate_tokens` as a cap: `return min(n, OLLAMA_CONTEXT_LENGTH)`.
- This is a naming debt, not a functional coupling. The cap is valid post-migration (still want to bound the token estimate). Renaming the var requires coordinated updates across `.env.example`, `docker-compose.yml`, `agents/entrypoint.sh`, and tests.

### Coupling point 11: `architecture/README.md` — diagram and description hardcode ollama

- **File:** `architecture/README.md:11–18` (system diagram), `architecture/containers.md` (entire containers doc)
- The system diagram shows the three-hop path through ollama. Both docs are factually incorrect post-migration and must be rewritten.

---

## Tech Debt

**`proxy/proxy.py` single-file 1800+ line monolith:**
- Issue: One file handles Flask routing, ollama wire-format translation, cooperative-prompt injection (three modes), tool-call extraction, NDJSON generation, upstream HTTP, token estimation, and debug dump I/O.
- Files: `proxy/proxy.py`
- Impact: High barrier to understanding; any change risks touching unrelated logic; test surface is wide. The architecture doc itself notes "Single-process, single file, ~1,130 lines" — it is now 1802 lines.
- Fix approach: Decompose into modules (e.g., `proxy/translation.py`, `proxy/prompt.py`, `proxy/streaming.py`) during the ollama-removal rewrite, since the response format change already requires touching all three layers.

**`harness` CLI bash script is 5111 lines:**
- Issue: One bash file for all subcommands, platform detection, compose orchestration, doctor, upgrade, MCP lifecycle.
- Files: `harness`
- Impact: Sourcing for tests is slow; any refactoring touching shared helpers requires reading the full file; shellcheck coverage gaps.
- Fix approach: Low priority until a specific pain point arises. The file is stable and test-covered.

**`OLLAMA_CONTEXT_LENGTH` is a misleading name for a non-ollama concern:**
- Issue: Post-migration (and even now), this env var controls the context window reported to opencode and caps the proxy's token estimator — neither of which is an ollama behavior. The name ties the concept to the layer being removed.
- Files: `proxy/proxy.py:54`, `agents/entrypoint.sh:136`, `.env.example:100`, `docker-compose.yml:44`, all test env fixtures
- Impact: Confusion about what to keep/rename during migration.
- Fix approach: Rename to `HARNESS_CONTEXT_LENGTH` in a coordinated PR; update `.env.example`, both Dockerfiles' environment sections, and all test fixtures.

**`_normalize_api_base` is duplicated between proxy and harness CLI:**
- Issue: `proxy/proxy.py:57–76` and `harness` (around line 952–960) both implement the same URL base normalization. The architecture doc notes "keep the two in sync."
- Files: `proxy/proxy.py:57–76`, `harness:952–966`
- Impact: A divergence between the two implementations causes the CLI's auth probe to hit a different endpoint than the proxy's actual request.
- Fix approach: Low risk currently since both are simple suffix-stripping. Document the sync requirement explicitly in code comments in both files (already done in `harness-cli.md`).

**Debug dump files contain full conversation history including system prompts:**
- Issue: When `OUTPUT_DIR=/output` is set, `state/output/` accumulates `<req_id>_01_Ollama_Request.json` and `<req_id>_02_API_Request.json` files containing the full translated conversation, cooperative prompts, and tool results. These files are never purged automatically.
- Files: `proxy/proxy.py:344–353`, `docker-compose.yml:116–117`
- Impact: Disk exhaustion over long sessions (no TTL or rotation); conversation content including potentially sensitive tool results stored unencrypted on disk. The `_02_API_Request.json` file does NOT contain the `Authorization` header (verified: `upstream_payload` is built without headers), so the API key is not in dumps.
- Fix approach: Add a `--max-age` or `--max-files` rotation option; or document the lack of rotation and let users manage it.

**Flask development server (`app.run`) used in production:**
- Issue: `proxy/proxy.py:1798` runs `app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)`. Flask's built-in WSGI server is single-threaded by default (Werkzeug dev server).
- Files: `proxy/proxy.py:1798`
- Impact: If opencode makes concurrent requests to the proxy (e.g., parallel tool calls trigger a re-entry into the proxy), requests queue. In practice the agent loop is sequential, so this has not caused visible failures. Post-migration, if the proxy serves OpenAI streaming (SSE), the streaming generator holds the thread for the full response duration — a second request from the same agent would queue behind it.
- Fix approach: Switch to `waitress` or `gunicorn` in `proxy/Dockerfile`. Low risk currently; becomes more important if streaming is added.

---

## Known Bugs

**`state/output/` is never rotated:**
- Symptoms: Directory grows without bound; no automatic cleanup.
- Files: `proxy/proxy.py:344–353`, `docker-compose.yml:116–117`
- Trigger: Running `harness` with `OUTPUT_DIR=/output` in `.env` over many sessions.
- Workaround: Manually delete files from `<install-root>/state/output/`.

**`opencode run` headless stdout race on fast responses (opencode 1.15.x):**
- Symptoms: `harness -p "<prompt>"` may emit empty output on fast/short responses (~10% of runs against a mock upstream), because opencode 1.15.x drops the assistant body to a non-TTY stdout when the finalize event races the idle event. Documented in `agents/Dockerfile:72–87`.
- Files: `agents/entrypoint.sh:325–357`, `agents/Dockerfile:72–87`
- Trigger: Fast upstream responses (mock upstream, cached models). The race is non-deterministic.
- Workaround: The entrypoint uses `opencode export <session>` as a fallback, and `opencode session list` as a second-level fallback when the stream carries no session id. This covers the regression but adds fragility tied to opencode's export/session-list CLI shape.

**`task` tool description paring is fragile to opencode version bumps:**
- Symptoms: If opencode renames the `"Available agent types and the tools they have access to:"` header in `ToolRegistry.describeTask`, the proxy falls back to inlining the full `task` description at recency (~100+ extra tokens per turn), and `TestTaskDescriptionParing` fails.
- Files: `proxy/proxy.py:274–279`, `proxy/test_proxy.py` (TestTaskDescriptionParing)
- Trigger: opencode version bump that renames the header string.
- Workaround: The fallback is graceful (degrades to more tokens, not missing values). The test is the canary.

---

## Security Considerations

**`verify=False` on all upstream HTTPS requests (TLS cert not verified):**
- Risk: Man-in-the-middle attack on the proxy → upstream link. Any CA-signed or self-signed cert is accepted without hostname verification.
- Files: `proxy/proxy.py:37–41`, `proxy/proxy.py:1541`, `proxy/proxy.py:1608`
- Current mitigation: The upstream uses a self-signed cert, so verification is deliberately disabled. The proxy runs inside the container on a private bridge network; the attack surface is the egress path from the container to the upstream host.
- Recommendations: Obtain or generate a cert the proxy can pin; or document the risk formally. If the upstream ever moves to a CA-signed cert, `verify=True` should be re-enabled.

**API key is not in debug dump files (verified):**
- `proxy/proxy.py:1596` saves `upstream_payload` (model + messages only, no headers). The `Authorization` header is constructed separately in `headers` and never passed to `save_debug_file`. The startup log redacts the key via `_redact_key`.
- Files: `proxy/proxy.py:1596`, `proxy/proxy.py:1743–1748`
- Current state: No key leakage into `state/output/` files. OK.

**`state/output/` debug files contain full conversation content:**
- Risk: If `OUTPUT_DIR` is enabled, every request writes conversation history (including user data, tool results, file contents the agent read) to `<install-root>/state/output/` as unencrypted JSON. These files are readable by any process with host filesystem access.
- Files: `proxy/proxy.py:344–353`
- Current mitigation: `OUTPUT_DIR` defaults to empty in `docker-compose.yml` line 98–100 (`OUTPUT_DIR: ${OUTPUT_DIR:-}`) so dumps are opt-in.
- Recommendations: Document clearly that enabling `OUTPUT_DIR` persists conversation content.

**`HARNESS_FIREWALL_DISABLED=1` can be set by untrusted env:**
- Risk: If an agent writes to the container's env (e.g., via a compromised tool), it cannot set `HARNESS_FIREWALL_DISABLED=1` for the current container — the firewall was already applied before the drop. However, if the flag is set externally (e.g., a misconfigured `.harness-net-overrides.json`), the entire egress firewall is bypassed with no per-request protection.
- Files: `agents/entrypoint.sh:42`, `firewall/init-firewall.sh`
- Current mitigation: `harness net open` requires typing "I understand the risks" on a TTY; scripts cannot bypass. The flag is only injected by the harness CLI via the runtime override.

**`harness-dummy` API key in opencode config:**
- Risk: `agents/entrypoint.sh:187` writes `"apiKey": "harness-dummy"` into the opencode provider config. This is a dummy value — the real auth happens at the proxy → upstream level.
- Files: `agents/entrypoint.sh:187`
- Current mitigation: Not a real secret; opencode uses it for the `@ai-sdk/openai-compatible` adapter header, but the proxy ignores inbound auth headers from ollama/opencode. After ollama removal, the proxy will receive a `Bearer harness-dummy` header from opencode on every request — it must continue to ignore it.

---

## Performance Bottlenecks

**Proxy does not stream from upstream (full response buffered before NDJSON):**
- Problem: The proxy POSTs to upstream and waits for the full response before emitting any NDJSON back to ollama/opencode. The user sees no output until the entire generation is complete.
- Files: `proxy/proxy.py:1603–1720`
- Cause: The upstream API is called with `stream=False` (no streaming parameter is set). The architecture doc notes: "the proxy is NOT streaming from upstream — it gets the full response, then translates."
- Improvement path: Add `"stream": True` to the upstream payload and consume the SSE stream progressively. Blocked by the need to emit NDJSON (or post-migration, OpenAI SSE) tokens as they arrive, requiring a streaming response generator in Flask — which in turn requires a production WSGI server (see Flask dev server concern above).

**ollama startup adds ~60s maximum cold-start window:**
- Problem: Each `harness start` must wait for the ollama container to: start ollama serve (several seconds), probe `/api/tags` up to 60s, query proxy `/v1/models`, POST `/api/create` per model, then confirm via `/api/tags`. This is sequential and synchronous.
- Files: `ollama/entrypoint.sh:47–58`, `docker-compose.yml:68–71`
- Cause: ollama's `depends_on: proxy: service_healthy` ensures ordering, but the overall startup chain is proxy health (~5s) + ollama model registration (~5–15s) + agent config build (query ollama `/api/tags`). The 60s timeout is a defensive bound.
- Improvement path: This concern is resolved by the ollama-removal migration — the proxy is healthy in ~5s and the agent queries `/v1/models` directly.

**`_estimate_tokens` uses a fixed divisor (3) without per-model calibration:**
- Problem: `len(text) // 3` is a heuristic tuned for code-heavy agent turns. For prose-heavy turns it over-estimates; for very dense JSON it under-estimates.
- Files: `proxy/proxy.py:1354–1359`
- Cause: Upstream `prompt_tokens` is unreliable (non-monotonic due to server-side truncation), so local estimation is required. The `//3` divisor is better than `//4` for this use case but is still approximate.
- Improvement path: Per-model calibration tables, or switching to a tiktoken-style byte-pair encoder. Low priority given the estimate's purpose is context-bar accuracy, not billing.

---

## Fragile Areas

**Agent opencode config is rewritten on every launch:**
- Files: `agents/entrypoint.sh:128–200`
- Why fragile: `ensure_opencode_config` overwrites `~/.config/opencode/opencode.json` unconditionally every launch. Any opencode-native config the user placed there is silently replaced.
- Safe modification: The function writes the full config from scratch using `jq -n`. Adding fields requires updating this one function; removing fields requires ensuring opencode does not fail on the absence. The `merge_opencode_mcp_servers` function then patches the file for MCP entries — order matters: `ensure_opencode_config` must run before `merge_opencode_mcp_servers`.

**Proxy `catch_all` treats every non-health, non-models request as a chat request:**
- Files: `proxy/proxy.py:1559–1561`
- Why fragile: A `GET /api/tags` from a test or from ollama reaches `catch_all`, which tries to parse a JSON body from a GET request and proceeds with an empty body. This resolves silently (empty `messages`, falls back to `DEFAULT_MODEL_NAME`) but may produce confusing debug dumps.
- Safe modification: Add explicit routes for known ollama management paths (`/api/tags`, `/api/show`, `/api/create`) that return appropriate 404s or stubs if they need to be handled post-migration.

**`_pare_task_description` anchors on a byte-stable opencode string:**
- Files: `proxy/proxy.py:274–279`, `proxy/test_proxy.py:TestTaskDescriptionParing`
- Why fragile: If opencode renames the header string `"Available agent types and the tools they have access to:"`, the parse silently falls back to verbatim inclusion (no information loss, but extra tokens). The test is the canary but only runs as part of `proxy/test_proxy.py` inside the proxy container.
- Safe modification: When bumping `OPENCODE_VERSION` in `agents/Dockerfile`, always run `TestTaskDescriptionParing` against the new binary and update `_OPENCODE_TASK_AGENTS_HEADER` if the header drifted.

**`OLLAMA_REMOTES=proxy` exact-string hostname matching:**
- Files: `docker-compose.yml:37–38`, `architecture/containers.md:48–55`
- Why fragile: ollama uses `slices.Contains` (exact string, no DNS) for `OLLAMA_REMOTES`. Renaming the compose service `proxy` to anything else silently breaks routing — stub model registration succeeds but all chat requests are refused by ollama.
- Safe modification: If the proxy service is renamed, `OLLAMA_REMOTES` must be updated to match. This coupling disappears entirely post-migration.

**`harness_container_workdir` `-w` escaping on Windows Git Bash:**
- Files: `scripts/lib/platform.sh:399`, `harness:2606`
- Why fragile: Issue #112 (most recent fix on `nollama` branch) addressed `docker run -w` path escaping on Windows Git Bash by prepending `//` on MSYS paths. The fix is in `harness_container_workdir`. If Docker Desktop changes path handling between versions, or if a new Windows path form appears, the escaping can silently break.
- Safe modification: The `unit_workdir_test.sh` (added on this branch) is the test gate. Always run it when touching path-handling code.

**opencode version coupling across two files:**
- Files: `agents/Dockerfile:103` (`OPENCODE_VERSION=1.15.7`), `agents/entrypoint.sh:329–356` (jq expressions against `opencode export` and `session list` JSON shapes)
- Why fragile: A version bump that changes `opencode export`'s output schema (`{messages:[{info.role,parts:[{type,text}]}]}`) or `opencode session list`'s schema (`[{id,directory,created}]`) silently breaks `harness -p` output recovery — it produces empty output without an error.
- Safe modification: The `agents/Dockerfile` has a checklist comment (lines 89–101) for what to verify on a version bump. Follow it. The `full_pipeline_test.sh` T9/T10 are the gate tests.

---

## Scaling Limits

**Single upstream API key locks every 8 hours:**
- Current capacity: One key shared across all sessions.
- Limit: When the key locks (every ~8h, measured from unlock per `architecture/upstream-api.md:58–59`), all proxy requests return 401 until manually unlocked via a browser session. The unlock cannot be automated.
- Scaling path: A key rotation pool (multiple keys, rotate on 401+unlock_url) or monitoring/alerting for key locks. Currently not implemented.

**Key expires after ~1 month:**
- Current capacity: One key.
- Limit: The key must be manually renewed.
- Scaling path: Calendar reminder or automated monitoring. No code support for key rotation.

---

## Dependencies at Risk

**ollama pinned at version 0.21.2:**
- Risk: The ollama `/api/create` stub-model registration format (`remote_host`, `from`, `info.context_length`, `parameters.num_ctx`) is a harness-specific internal API that could change in minor ollama versions. The version is pinned in `.env.example:93` and `docker-compose.yml:29` to prevent silent breakage.
- Impact: Post-migration this concern is eliminated entirely.
- Migration plan: Remove `OLLAMA_VERSION` from `.env.example`, `docker-compose.yml`, all test fixtures, and the `harness` doctor check.

**opencode pinned at version 1.15.7:**
- Risk: The headless `-p` output recovery in `agents/entrypoint.sh` parses `opencode export` and `opencode session list` JSON shapes. Any opencode release that renames these CLI subcommands or changes their output schema silently breaks `harness -p`.
- Impact: Affects all non-interactive (print mode) usage including CI test pipelines.
- Migration plan: Run the full_pipeline T9/T10 gate tests against every opencode candidate version before bumping `agents/Dockerfile:103`.

**Flask 3.0.3 + requests 2.32.3 (only two Python deps):**
- Risk: The proxy's `app.run()` uses Werkzeug's dev server. Flask 3.x deprecated some patterns from 2.x. No known incompatibility, but the proxy has no WSGI server (gunicorn, waitress) so it is not production-hardened.
- Impact: Single-threaded request handling; not suitable for concurrent requests.
- Migration plan: Add `waitress` to `proxy/requirements.txt` and switch entrypoint to `waitress-serve` as part of the ollama-removal rewrite.

---

## Missing Critical Features

**No streaming from upstream to agent:**
- Problem: The agent waits for the full upstream generation before seeing any content. On a 200k-token context with a long response, this can be 30–180 seconds of silence.
- Blocks: Responsive agent interaction on long-running completions.

**No automatic API key unlock:**
- Problem: When the upstream key locks (every ~8h), every request fails until a human visits the unlock URL in a browser.
- Blocks: Unattended / overnight agent runs.

**No session-level request rate tracking:**
- Problem: The proxy has no visibility into accumulated token usage across turns (upstream `usage` is unreliable — see `upstream-api.md:51–54`). The local `_estimate_tokens` fills in for context bar, but there is no alerting when approaching the upstream's context window.
- Blocks: Proactive context management (compact before hitting the limit vs. hitting it and having opencode truncate).

---

## Test Coverage Gaps

**O### (Ollama entrypoint) inventory: 9 of 16 items are red:**
- What's not tested: O002 (PID capture), O004 (fatal timeout), O008 (registration failure path), O019 (remote_host verification), O020/O021/O022 (signal traps), O023 (OLLAMA_REMOTES env), O025 (depends_on gate).
- Files: `ollama/entrypoint.sh`
- Risk: Regressions in the stub-model registration flow are caught only by the integration-level health check (O005, O009), not by targeted unit assertions.
- Priority: Low, because the ollama-removal migration eliminates this entire layer.

**A### (Agent entrypoint) inventory: 12 of 22 items are red:**
- What's not tested: A010 (HARNESS_HOST_CWD cd), A019 (opencode config has harness provider pointing at ollama), A028 (OPENCODE_DISABLE_AUTOUPDATE), A029 (--agent yolo).
- Files: `agents/entrypoint.sh`
- Risk: The opencode config generation (`ensure_opencode_config`) is tested only indirectly via full_pipeline_test.sh T9. The exact URL written to `baseURL` is not asserted in any current test (A019 is red). Post-migration, a test asserting `baseURL = http://proxy:...` would be the primary regression guard.
- Priority: High. A019 should be the first new test written after migration.

**N### (Firewall) inventory: 24 of 30 items are red:**
- What's not tested: Most of the fine-grained iptables assertion surface (OUTPUT policy, DNS-only allowed egress, non-allowlist hosts rejected, etc.).
- Files: `firewall/init-firewall.sh`
- Risk: Firewall regressions would only be caught by the integration-level proxy-test negative path (blocked PROXY_API_URL host aborts proxy startup).
- Priority: Medium. The existing positive/negative/bypass tests cover the critical paths.

**P### (Proxy) — no test for upstream non-JSON → 502 (P042) and no dump-file readback tests (P043, P047):**
- What's not tested: The `except ValueError` path in `catch_all` that fires when the upstream returns non-JSON; the content of `state/output/` debug dump files.
- Files: `proxy/proxy.py:1634–1643`, `proxy/proxy.py:344–353`
- Risk: A silent regression in dump-file naming or content would go unnoticed.
- Priority: Low.

---

*Concerns audit: 2026-06-02*
