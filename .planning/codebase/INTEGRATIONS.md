---
last_mapped_commit: 4ebbb2c
---

# External Integrations

**Analysis Date:** 2026-06-10

> Terminology note: two unrelated "host" concepts exist.
> - **`harness host`** = the containerless full-stack runtime (proxy + opencode as host processes).
> - **host MCP** = an MCP server run as a supervised process on the host machine and wired into *container-mode* agents over the docker bridge.

## Upstream LLM API (Core Integration)

**Service:**
- **Non-Anthropic OpenAI-compatible chat-completions API** (REQUIRED)
  - Base URL: `PROXY_API_URL` environment variable (`.env.example:27`). Proxy normalizes by stripping trailing `/v1/chat/completions`, `/chat/completions`, or `/v1` (allows both base URLs and full chat URLs).
  - Auth: `PROXY_API_KEY` (bearer token, `.env.example:30`). Sent in `Authorization: Bearer {key}` header on all upstream requests.
  - **Endpoints exposed by proxy:**
    - `POST /v1/chat/completions` — Main chat endpoint. Receives OpenAI-format requests from opencode, translates to upstream, parses responses. Implementation: `proxy/proxy.py:2178-2438` (catch_all handler).
    - `GET /v1/models` — Model catalog endpoint. Proxied pass-through so opencode can build model selection dropdown. Implementation: `proxy/proxy.py:2133-2168`.
    - `GET /health` — Health check for Docker healthcheck. Implementation: `proxy/proxy.py:2128-2131`.
  - **Model passthrough:** Proxy forwards whatever model opencode requests; only falls back to `DEFAULT_MODEL_NAME` when request omits model (`proxy/proxy.py:2217`).
  - **Timeout:** `PROXY_TIMEOUT` (default 180s, `.env.example:42`).
  - **TLS:** Proxy uses `verify=False` for self-signed certs (`proxy/proxy.py:48-51`).
  - **Response format:** Streaming (SSE `text/event-stream`) when `stream: true`, else single JSON response.
  - **Error handling:** Non-2xx responses are logged and returned as 502 to opencode.
  - **Key lifecycle:** Locked keys return 401 with `unlock_url` from upstream; proxy passes through verbatim so unlock flow reaches opencode unchanged.

**Cooperative tool use (proxy mediation):**
- Proxy injects tool-use prompts into messages (hybrid mode, default) because upstream may not natively support tool calls.
- Modes: `hybrid` (full schemas at stable prefix, signature reminder at recency), `user_front` (scaffold on last user message), `passthrough` (no mediation, schemas forwarded as-is).
- Mode is ephemeral (per-launch `--prompt-mode` flag; see `proxy/proxy.py:104-130`), not persisted in `.env`.
- Upstream `system` role is converted to `user` role because upstream silently drops `system` (`proxy/proxy.py:168-176`).
- Tool calls extracted from markdown `\`\`\`json` blocks when upstream doesn't emit native tool_calls.
- In-proxy retry for malformed tool calls: if extraction yields zero calls and response looks like a failed JSON block, proxy appends correction and re-POSTs once (`proxy/proxy.py:1835-1913`).

**Verified at:**
- `proxy/proxy.py:61-64` (config load)
- `proxy/proxy.py:97-99` (endpoint derivation)
- `proxy/proxy.py:2238-2268` (chat request/response)
- `proxy/proxy.py:2150-2168` (models endpoint)

## Agent & Tool Execution

**Local Tools (opencode-ai 1.15.7):**
- `read` — Read file contents
- `edit` — Modify files (proxy guidance: "read first")
- `bash` — Execute shell commands
- `apply_patch` — Apply unified diffs
- `todowrite` — Persistent task list (guidance: don't re-call within same task)
- `task` — Launch sub-agent (dynamic description pulled from opencode, pared to agent list)
- `skill` — Load named skill (description includes valid skill names)
- `websearch` — Live web search via Exa (optional, enabled by default via `OPENCODE_ENABLE_EXA`)
- Synthetic meta-tools (opt-in, `HARNESS_TOOL_SEARCH=1`): `tool_search`, `tool_list` — let model retrieve tool schemas on demand

**Tool Guidance (Recency System):**
- Core tools have guidance in `proxy/tool-guidance.json`, loaded into the `_HYBRID_TOOL_GUIDANCE` map by `proxy/proxy.py:_setup_tool_guidance`.
- MCP tools have guidance from `recency.json` files in `mcp-registry/<name>/` and `state/mcp/<name>/` (host MCPs).
- Guidance merged by harness CLI at startup into `HARNESS_MCP_TOOL_RECENCY` JSON object (env var), injected into proxy.
- State-check (mutating) tools flagged in `recency.json` `state_check` field, merged into `HARNESS_MCP_STATE_CHECK` array, injected into proxy.

**Verified at:**
- `proxy/proxy.py:200-291` (tool guidance)
- `mcp-registry/serena/recency.json` (sample MCP recency)
- `agents/Dockerfile:103-104` (opencode + @ai-sdk/openai-compatible versions)

## MCP Services & Registry

**Container-based MCPs (Docker services):**
- Registry: `mcp-registry/` (tracked in git, vetted definitions)
- Current entry: **Serena** (semantic code analysis)
  - Compose snippet: `mcp-registry/serena/compose.yml` (merged via `-f` flag into docker compose)
  - Network: `harness-net`; agents reach at `http://serena:9121/sse`
  - Build: Local clone at `state/mcp/serena/repo` (cloned on `harness mcp install serena`)
  - Mounts: Project root at `HARNESS_PROJECTS_ROOT` (optional, default: host `$HOME`), read-only
  - Dashboard: Optional publish via `SERENA_DASHBOARD_PORT`
  - State: `state/mcp/serena/harness-meta.json` (enabled flag, managed by `harness mcp enable|disable`)
  - Client config: `mcp-registry/serena/client-config.json` (merged into agent's MCP config)
  - Recency: `mcp-registry/serena/recency.json` (tool guidance, state-check flags)

**Host-based MCPs (supervised host processes):**
- Template: `host-mcp/template/` (user-authored MCPs that run as host processes)
- SDK: MCP Python SDK `>=1.27` (`host-mcp/template/requirements.txt:4`)
- Setup: Custom host MCPs write `client-config.json` and `recency.json` into `state/mcp/<name>/`
- Wiring: Only in container mode (agents reach host MCPs over docker bridge via harness-supplied docker args)
- Lifecycle: harness supervises (pidfile, logfile under `state/mcp/<name>/`, started/stopped with stack)

**Lifecycle:**
```
available ──install──► installed-enabled ⇄ disable/enable ⇄ installed-disabled ──uninstall──► available
```
- `harness mcp install <name>` — Copy registry entry → active tree, set enabled: true
- `harness mcp enable|disable <name>` — Flip auto-start flag on installed entry
- `harness mcp uninstall <name> --force` — Remove active entry (data/ preserved)

**Verified at:**
- `harness:600-627` (mcp_compose_files, mcp state enumeration)
- `harness:638-727` (mcp state helpers)
- `mcp-registry/serena/compose.yml` (service definition)

## Code Context & Analysis

**Serena MCP (Semantic Code Analysis):**
- Mounted directory: `HARNESS_PROJECTS_ROOT` (optional, default: host `$HOME`)
- Bind-mounted read-only into Serena container; agents use Serena tools for code analysis
- Tools prefixed `serena_` (e.g., `serena_search`, `serena_analyze`)
- Guidance flows through `HARNESS_MCP_TOOL_RECENCY` and `HARNESS_MCP_STATE_CHECK`

**Verified at:**
- `.env.example:96-106` (HARNESS_PROJECTS_ROOT docs)
- `mcp-registry/serena/compose.yml:45-65` (environment and mounts)

## Web Search

**Exa Integration:**
- **Triggered by:** opencode's `websearch` tool (user-invoked, not automatic)
- **Provider:** Exa web-search API (opencode's own integration; not code-managed by harness)
- **Configuration:**
  - Container mode: `OPENCODE_ENABLE_EXA=1` exported by agent entrypoint (default: on)
  - Host mode: `OPENCODE_ENABLE_EXA=1` explicitly set by harness (`harness:3494`)
- **Network:**
  - Container mode: Exa domain must be in `.harness-allowlist` (user-added)
  - Host mode: No firewall; unrestricted egress (user confirmed via gate)

**Verified at:**
- `harness:3490-3503` (host mode Exa setup)

## Network & Firewall

**Service Network (Container Mode):**
- Bridge: `harness-net` (user-defined)
- Services: `proxy:8000`, `agent` (ephemeral), enabled MCPs (e.g., `serena:9121`)
- Hostname `proxy` is load-bearing: opencode's provider baseURL hardcodes `http://proxy:${PROXY_PORT}/v1` (`harness:2453`, `agents/entrypoint.sh`)

**Egress Firewall (Container Mode):**
- **Mechanism:** iptables + ipset (initialized at container startup by `firewall/init-firewall.sh`)
- **Allowlist:** `/.harness-allowlist` (gitignored, seeded from `.harness-allowlist.example`)
- **Firewall validation:** Startup checks `PROXY_API_URL` hostname is allowlisted; aborts if not (`firewall/init-firewall.sh:11-14`)
- **IPv6:** Disabled via sysctl (IPv4-only firewall; closes unfiltered v6 path)
- **Git push:** Only allowed to hosts annotated `# git-push` in allowlist
- **Bypass:** `harness --net` (per-invocation) or `harness net open <service>` (persistent) sets `HARNESS_FIREWALL_DISABLED=1` on service

**Host Mode (No Firewall):**
- Runs as full host user with unrestricted egress
- Requires explicit per-launch confirmation (`HARNESS_HOST_CONFIRM=1` to auto-approve in automation)

**Verified at:**
- `docker-compose.yml:76-78, 124` (allowlist mounts)
- `agents/Dockerfile:127-133, 135-140` (firewall and git credential blocking)
- `harness:3600` (host mode is Linux/macOS only)

## Authentication & Identity

**Upstream API Authentication:**
- **Storage:** `PROXY_API_KEY` in `.env` (gitignored)
- **Transmission:** `Authorization: Bearer {key}` header on every upstream request
- **Probe:** Harness CLI probes upstream auth at startup via `_gate_on_upstream_auth` (minimal request to check key validity). Locked keys surface unlock URL before launch.

**Agent → Proxy Authentication:**
- **Mechanism:** No real auth; proxy is only reachable on `harness-net` (container) or `127.0.0.1` (host)
- **Dummy key:** Host mode passes `"apiKey": "harness-dummy"` to opencode config (never sent; opencode only talks to loopback proxy) (`harness:3448-3451`)

**Git Credentials (Agents):**
- **Default:** Blocked. `configure-git-credentials.sh` sets `credential.helper=/bin/false`
- **Interactive prompts:** Disabled via `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/false`
- **Push enablement:** Only allowed to hosts annotated `# git-push` in `.harness-allowlist`

**Verified at:**
- `.env.example:29-30` (PROXY_API_KEY docs)
- `harness:5089-5141` (_gate_on_upstream_auth implementation)
- `agents/Dockerfile:135-140` (git credential setup)

## Data Storage

**Persistent Agent Home:**
- **Location:** `state/agent/home/` (bind-mounted, gitignored)
- **Initialization:** `agents/Dockerfile:142-149` snapshots build-time `/home/harness/` into `/etc/skel/harness/`; entrypoint copies skeleton into empty bind mount on first run (marked with `~/.harness-home-initialized`)
- **Persistence:** User installs (`pipx install X`, custom dotfiles) survive container rebuilds

**Debug Dumps (Optional):**
- **Trigger:** `OUTPUT_DIR` env var (off by default)
- **Location:** `state/output/` (bound to `/output` in proxy container)
- **Content:** Per-request JSON files: `<req_id>_01_Request`, `_02_Request`, `_03_Response`, etc.
- **Writing:** `proxy/proxy.py:2231, 2268, 1868, 1908` (save_debug_file)

**MCP Persistent State:**
- **Location:** `state/mcp/<name>/data/` (per-MCP, gitignored)
- **Preservation:** `harness mcp uninstall` keeps data; reinstall doesn't wipe

**No Database:**
- No SQL/NoSQL databases in the stack
- No Redis/Memcached
- Proxy is stateless; opencode session state stored in agent home

**Verified at:**
- `agents/Dockerfile:142-149` (skel-seed)
- `docker-compose.yml:75` (output mount)
- `.env.example:75-90` (OUTPUT_DIR docs)

## Monitoring & Observability

**Logging:**
- **Proxy:** Prints `[req_id] ...` lines to stdout (captured by docker logs or `state/host/proxy.log` in host mode)
- **Debug dumps:** Per-request JSON when `OUTPUT_DIR` set
- **Firewall:** Logs `[harness-firewall]` lines to kernel log

**Health:**
- `GET /health` → `{"status":"ok"}` (Docker healthcheck polls this)

**Error Tracking:**
- None (no Sentry, Datadog, etc.)
- Errors logged to stdout/docker logs

**Verified at:**
- `proxy/proxy.py:2128-2131` (health endpoint)
- `docker-compose.yml:92-97` (healthcheck)
- `proxy/proxy.py:2247, 2257` (error logging)

## Webhook & Callbacks

**Incoming:** None

**Outgoing:**
- Proxy → upstream chat/models calls (request-driven, no async webhooks)
- Agent → allowlisted git hosts (push/fetch)
- Agent → allowlisted package registries (npm, pip, etc.)
- No pub/sub, message queues, or event streams

---

*Integration audit: 2026-06-10*
