<!-- refreshed: 2026-06-02 -->
# Architecture

**Analysis Date:** 2026-06-02

## System Overview

```text
┌────────────────────────────────────────────────────────────────┐
│                     harness CLI (host)                          │
│  `harness`  (bash, ~5111 lines)                                 │
│  Manages: start/stop/update/upgrade/mcp/net/doctor/test         │
└──────────┬─────────────────────────────────────────────────────┘
           │ docker run (ephemeral, joins harness_harness-net)
           ▼
┌────────────────────────────────────────────────────────────────┐
│               agent container  (harness-agent:latest)           │
│  `agents/entrypoint.sh`  — firewall → UID remap → gosu drop    │
│  mode dispatch: opencode (TUI or -p headless) | shell (bash)    │
│  opencode  →  http://ollama:11434/v1  (via opencode.json)       │
└──────────┬─────────────────────────────────────────────────────┘
           │ /api/chat (NDJSON streaming)
           ▼
┌────────────────────────────────────────────────────────────────┐
│               ollama  (harness-ollama:latest)                   │
│  `ollama/entrypoint.sh`  — stub model registration at startup   │
│  stub models: RemoteHost = http://proxy:8000                    │
│  advertises models discovered from proxy /v1/models             │
└──────────┬─────────────────────────────────────────────────────┘
           │ POST /v1/chat/completions  (full response, not streamed)
           ▼
┌────────────────────────────────────────────────────────────────┐
│               proxy  (harness-proxy:latest)                     │
│  `proxy/proxy.py`  — Flask, ~1802 lines                         │
│  translate ollama→upstream format                               │
│  inject cooperative tool-use prompts (hybrid/user_front/pass)   │
│  parse ```json tool-call blocks → native tool_calls             │
│  emit NDJSON back to ollama                                     │
└──────────┬─────────────────────────────────────────────────────┘
           │ POST {PROXY_API_URL}/v1/chat/completions  (Bearer key)
           ▼
┌────────────────────────────────────────────────────────────────┐
│               upstream API  (Gemini Enterprise / OpenAI-shape)  │
│  No tool support. Hidden system prompt. No streaming consumed.  │
│  Keys lock every ~8h; `harness unlock` surfaces unlock URL.     │
└────────────────────────────────────────────────────────────────┘

Shared bridge: harness_harness-net
Shared egress firewall: firewall/init-firewall.sh (runs in every container)
MCP services: join harness_harness-net as external peers
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `harness` CLI | Lifecycle management, agent launch, MCP/net management, doctor/preflight | `harness` |
| agent entrypoint | UID remap, firewall setup, gosu drop, skel seed, opencode config, MCP config merge, mode dispatch | `agents/entrypoint.sh` |
| ollama stub | Register one stub model per upstream model; forward `/api/chat` to proxy via `RemoteHost` | `ollama/entrypoint.sh` |
| translating proxy | ollama↔upstream format translation, cooperative-prompt injection, tool-call extraction, NDJSON generation | `proxy/proxy.py` |
| egress firewall | Drop all egress except DNS, `PROXY_API_URL` host, and allowlist entries | `firewall/init-firewall.sh` |
| MCP registry | Vetted long-running MCP service definitions (compose + client config) | `mcp-registry/<name>/` |
| platform lib | OS/runtime detection, TTY strategy, mount validation, jq fallback | `scripts/lib/platform.sh` |
| net helpers | Allowlist + per-service firewall override JSON manipulation | `scripts/lib/net_helpers.sh` |
| upgrade actions | `envfile_merge`, `linefile_merge`, `directory_overwrite` for `harness upgrade` | `scripts/lib/upgrade_actions.sh` |

## Pattern Overview

**Overall:** Container-isolated agent runtime with a format-translating proxy chain.

**Key Characteristics:**
- The agent never touches the upstream API directly; all upstream I/O goes through ollama → proxy
- The proxy is the sole mediation point: format translation, prompt injection, tool-call extraction
- The egress firewall runs in every container entrypoint, not as a host-level rule
- The repo IS the install root — code, config (`.env`, `.harness-allowlist`), and runtime state (`state/`) coexist in the clone
- Agent containers are ephemeral (`docker run`); long-running services (`ollama`, `proxy`, MCPs) are managed by compose

## Layers

**Management layer:**
- Purpose: Operate the harness runtime from the host shell
- Location: `harness` (root script), `scripts/lib/`
- Contains: Subcommand dispatch (`cmd_*`), compose wrapper, agent launch path (`run_agent`/`run_agent_interactive`/`run_agent_print`), runtime-override generation, MCP lifecycle, net management, doctor/preflight
- Depends on: Docker/Podman daemon, `scripts/lib/platform.sh`, `scripts/lib/net_helpers.sh`
- Used by: End users, CI

**Agent container layer:**
- Purpose: Run the coding agent in an isolated, firewalled environment
- Location: `agents/Dockerfile`, `agents/entrypoint.sh`
- Contains: UID remap, firewall init, gosu privilege drop, skel seed, `ensure_opencode_config`, `merge_opencode_mcp_servers`, `run_opencode`, `run_shell`
- Depends on: `firewall/init-firewall.sh`, ollama service (for `/api/tags`), `state/agent/home/` bind mount
- Used by: `harness` CLI via `docker run`

**Translation layer:**
- Purpose: Bridge ollama's wire format to upstream's OpenAI-shaped API; inject tool-use prompts
- Location: `proxy/proxy.py`
- Contains: `catch_all` Flask route, `translate_history_and_apply_prompt`, `extract_tool_calls_and_text`, `generate_ndjson`, cooperative-prompt builders (`build_cooperative_prompt_hybrid_reminder`, `build_cooperative_prompt_system_addition`, `build_cooperative_prompt_user_front`)
- Depends on: `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME` env vars
- Used by: ollama stub (via `RemoteHost` URL)

**Stub relay layer:**
- Purpose: Present the upstream's model catalog to opencode via ollama's native API
- Location: `ollama/Dockerfile`, `ollama/entrypoint.sh`
- Contains: Model discovery (GET proxy `/v1/models`), stub registration (`/api/create` with `remote_host`), startup validation
- Depends on: proxy service (must be healthy first — `depends_on: condition: service_healthy`)
- Used by: agent containers (opencode points at `http://ollama:11434/v1`)

**Egress control layer:**
- Purpose: Restrict outbound network in every container
- Location: `firewall/init-firewall.sh`, `firewall/configure-git-credentials.sh`
- Contains: iptables/ipset rules built from `/etc/harness/allowlist`, `# git-push` annotation handling
- Depends on: `NET_ADMIN`/`NET_RAW` capabilities; allowlist bind-mounted at `/etc/harness/allowlist`
- Used by: All containers (agent, ollama, proxy)

**MCP services layer:**
- Purpose: Provide optional long-running tool servers (e.g. Serena code-intelligence)
- Location: `mcp-registry/<name>/` (registry), `state/mcp/<name>/` (active)
- Contains: `compose.yml` (references `harness_harness-net` as external), `client-config.json`, `harness-meta.json`
- Depends on: main compose stack already up; `harness_harness-net` network exists
- Used by: agent containers (MCP URLs injected into `~/.config/opencode/opencode.json`)

## Data Flow

### Primary Request Path (agent turn → upstream → agent)

1. opencode sends `/api/chat` POST to `http://ollama:11434` (`agents/entrypoint.sh:ensure_opencode_config` points opencode here)
2. ollama stub's `RemoteHost` forwards the chat request to `http://proxy:8000` (`ollama/entrypoint.sh:register_stub_model`)
3. proxy `catch_all` receives the ollama JSON body (`proxy/proxy.py:1561`)
4. `format_tools_to_text` serializes the `tools` array to markdown (`proxy/proxy.py:430`)
5. `translate_history_and_apply_prompt` flattens message history, rewrites system→user, injects cooperative-prompt scaffold (`proxy/proxy.py:1075`)
6. proxy POSTs `{model, messages}` to `{PROXY_API_URL}/v1/chat/completions` with Bearer key (`proxy/proxy.py:1604`)
7. `extract_assistant_content` pulls text from `choices[0].message.content` (`proxy/proxy.py:1427`)
8. `extract_tool_calls_and_text` parses ` ```json ` blocks into `tool_calls` using balanced-brace scanner (`proxy/proxy.py:795`)
9. Empty-response detection: if both `clean_text` and `tool_call_payloads` are empty, proxy injects rescue text + `bash pwd` tool call (`proxy/proxy.py:1444–1513`)
10. `generate_ndjson` materializes ollama-compatible streaming chunks and writes debug dumps to `state/output/` (`proxy/proxy.py:1393`)
11. NDJSON is streamed back through ollama to opencode

### Model Discovery Path (stack startup)

1. `harness start` / `harness restart` → `compose()` writes `state/.harness-runtime.yml`, invokes `docker compose up`
2. `proxy` container starts first; ollama `depends_on: proxy: condition: service_healthy`
3. ollama entrypoint: GETs `http://proxy:${PROXY_PORT}/v1/models` — proxy passes through to upstream GET `/v1/models` (`ollama/entrypoint.sh:112`)
4. For each discovered model id (plus `DEFAULT_MODEL_NAME`), ollama POSTs `/api/create` with `remote_host=http://proxy:8000` (`ollama/entrypoint.sh:65`)
5. `agents/entrypoint.sh:ensure_opencode_config` GETs `http://ollama:11434/api/tags`, builds `opencode.json` with one model entry per registered stub

### Agent Launch Path

1. `harness` (or bare `harness`) → `run_agent` (`harness:2292`)
2. Parse `--yolo`, `--net`, `--mount`, `-p` flags; validate/dedup mounts
3. `_gate_on_upstream_auth` probes `{PROXY_API_URL}/v1/chat/completions`; abort on locked/invalid key
4. `ensure_services_up` ensures ollama+proxy are running
5. `write_agent_mcp_config` writes `state/agent/home/.harness-mcp-servers.json`
6. `docker run harness-agent:latest opencode` (or `shell`) with NET_ADMIN/NET_RAW caps, allowlist mount, `--network harness_harness-net`, CWD bind-mount at same absolute path, `HOST_UID`/`HOST_GID`, `HARNESS_HOST_CWD`

**State Management:**
- Runtime state: `state/` (gitignored) — ollama blobs (`state/ollama-data/`), agent home (`state/agent/home/`), proxy debug dumps (`state/output/`), MCP active configs (`state/mcp/`)
- Runtime compose override: `state/.harness-runtime.yml` — regenerated on every `compose()` call; carries `PUBLISH_OLLAMA_PORT`, per-service firewall opt-outs, ephemeral `PROXY_PROMPT_MODE`
- Net overrides: `state/.harness-net-overrides.json` — persists `firewall_disabled` per service

## Key Abstractions

**Stub model (`ollama/entrypoint.sh`):**
- Purpose: Make ollama present the upstream's model catalog without running a local model
- Examples: `ollama/entrypoint.sh:register_stub_model`
- Pattern: Each upstream model id becomes an ollama stub whose `remote_host` routes chat to the proxy; stub name matches upstream id so the request passes through unchanged

**Cooperative-prompt injection (`proxy/proxy.py`):**
- Purpose: Give a no-tool-support upstream the ability to emit structured tool calls
- Examples: `proxy/proxy.py:build_cooperative_prompt_hybrid_reminder`, `build_cooperative_prompt_system_addition`, `build_cooperative_prompt_user_front`
- Pattern: `hybrid` mode (default) — full tool definitions appended to system/user[0] as `<<<BEGIN_AGENT_TOOLS>>>` block; recency reminder with per-tool signatures lands on every last user turn. `user_front` — scaffold on last user message. `passthrough` — no injection (benchmark control).

**Runtime override (`harness:write_runtime_override`):**
- Purpose: Compose-layer configuration that is regenerated per invocation rather than committed
- Pattern: `state/.harness-runtime.yml` carries `PUBLISH_OLLAMA_PORT`, per-service `HARNESS_FIREWALL_DISABLED`, and ephemeral `PROXY_PROMPT_MODE`; deleted when all three sources are empty

**MCP compose merge (`harness:mcp_compose_files`):**
- Purpose: Splice enabled MCP service compose snippets into every `docker compose` invocation
- Pattern: Each enabled `state/mcp/<name>/compose.yml` is added as `-f <path>` to the compose args; snippets reference `harness_harness-net` as `external`

## Entry Points

**`harness` CLI:**
- Location: `harness` (repo root)
- Triggers: User invocation from any directory; zero args or leading `-` flag launches opencode agent
- Responsibilities: Self-locate, load `.env`, dispatch to `cmd_*` functions, drive `docker compose` via `compose()` wrapper

**`proxy/proxy.py:main()`:**
- Location: `proxy/proxy.py:1764`
- Triggers: Container entrypoint (`proxy/Dockerfile` runs `python3 proxy.py`)
- Responsibilities: Validate config, init output dir, set up prompt mode + host OS, start Flask on `PROXY_HOST:PROXY_PORT`

**`ollama/entrypoint.sh`:**
- Location: `ollama/entrypoint.sh`
- Triggers: ollama container startup
- Responsibilities: Firewall init, `ollama serve` in background, model discovery, stub registration, block on ollama PID

**`agents/entrypoint.sh`:**
- Location: `agents/entrypoint.sh`
- Triggers: Agent `docker run` (via `harness` CLI)
- Responsibilities: Root-side firewall + UID remap + gosu drop; user-side git credentials, skel seed, cd to `HARNESS_HOST_CWD`, mode dispatch to `run_opencode` or `run_shell`

## Architectural Constraints

- **Network:** All inter-container communication is by service name on `harness_harness-net` (`http://ollama:11434`, `http://proxy:8000`). `OLLAMA_REMOTES: proxy` is load-bearing — ollama allowlists `RemoteHost` on the exact literal hostname; renaming the `proxy` service requires updating `OLLAMA_REMOTES`.
- **IPv6:** Disabled at container creation via `net.ipv6.conf.all.disable_ipv6=1` sysctl in every service; the firewall is IPv4-only and IPv6 would otherwise be unfiltered.
- **Firewall ordering:** `init-firewall.sh` must run before any privilege drop; `NET_ADMIN`/`NET_RAW` are not preserved by `gosu`.
- **System→user rewrite:** `_CHANGE_SYSTEM_TO_USER` is hardcoded `True` in `proxy/proxy.py` — the upstream ignores the `system` role. This is not a knob.
- **Model passthrough:** The proxy forwards whatever model name ollama sent (`:latest` tag stripped) to upstream unchanged. `DEFAULT_MODEL_NAME` is only the fallback when no model is in the request.
- **Global state in proxy:** `_PROMPT_MODE`, `_HOST_OS`, `_OUTPUT_DIR`, `_CHANGE_SYSTEM_TO_USER`, `_HYBRID_DETAIL_TOOLS` are module-level globals set once at startup in `main()`.
- **Proxy is not a streaming proxy:** It receives the full upstream response before emitting NDJSON; the response is materialized in memory, then dumped to `state/output/`, then streamed to ollama.

## Anti-Patterns

### Setting `PROXY_PROMPT_MODE` in `.env`

**What happens:** The variable sits in `.env` but `docker-compose.yml` deliberately does NOT interpolate it into the proxy service environment.
**Why it's wrong:** Before this was fixed, a stale `.env` value silently overrode the proxy's built-in `hybrid` default. The only correct path is `harness start/restart --prompt-mode <mode>`, which injects the value ephemerally via the runtime override (`state/.harness-runtime.yml`).
**Do this instead:** Use `harness start --prompt-mode user_front` for a one-off; the proxy reverts to `hybrid` on the next bare restart.

### Renaming the `proxy` service in `docker-compose.yml` without updating `OLLAMA_REMOTES`

**What happens:** ollama's `RemoteHost` allowlist check uses exact string matching on the hostname portion of the URL. If the service is renamed to (e.g.) `translator`, `OLLAMA_REMOTES` must change from `proxy` to `translator`.
**Why it's wrong:** ollama silently refuses to forward to a host not in `OLLAMA_REMOTES`, causing all agent requests to 404/hang.
**Do this instead:** Change both the service name in `docker-compose.yml` and the `OLLAMA_REMOTES` value in the same commit.

### Running docker-based tests locally from an agent invocation

**What happens:** Tests such as `harness test` (whole suite), `proxy_test.sh`, `harness_test.sh`, `full_pipeline_test.sh` require Docker and significant disk.
**Why it's wrong:** These can exhaust runner disk and hang the agent. CI runs the full matrix on every push.
**Do this instead:** Run only `bash -n` on touched shell scripts, `scripts/check_runtime_calls.sh`, advisory `shellcheck`, and `harness test unit` (the docker-free unit suite).

## Error Handling

**Strategy:** Fail fast at startup for missing required config; surface upstream errors as HTTP 502 with structured bodies; degrade gracefully on optional features (missing `jq`, unreachable Exa, absent `CWD` annotation).

**Patterns:**
- `proxy/proxy.py:_validate_config` aborts the process on startup if `PROXY_API_URL`, `PROXY_API_KEY`, or `DEFAULT_MODEL_NAME` are empty (`proxy/proxy.py:1751`)
- `catch_all` returns `502` with `{"error": ..., "details": ...}` JSON on upstream connection errors or non-2xx upstream responses (`proxy/proxy.py:1614–1632`)
- `ollama/entrypoint.sh`: locked-key `401` (with `unlock_url`) → print URL and `exit 1`; transient upstream failure → warn and fall back to `DEFAULT_MODEL_NAME` only
- `harness:_gate_on_upstream_auth`: locked key or rejected key → abort agent launch and print unlock URL; `invalid_request` error type (key valid, probe request bad) → warn and continue
- Debug dumps written to `state/output/<req_id>_*.json` for every proxy request when `OUTPUT_DIR` is set

## Cross-Cutting Concerns

**Logging:** `print()` to stdout in `proxy/proxy.py`; `echo` to stdout/stderr in bash scripts; structured debug dumps in `state/output/`. Accessed via `harness logs <service>`.
**Validation:** `harness doctor` (read-only diagnostic) and `harness preflight` (validates `.env`, allowlist, docker daemon before start).
**Authentication:** Bearer token (`PROXY_API_KEY`) on every upstream request; key lifecycle managed externally (locks every ~8h, expires ~1 month). `harness unlock` surfaces the `unlock_url` from a locked `401`.

---

*Architecture analysis: 2026-06-02*
