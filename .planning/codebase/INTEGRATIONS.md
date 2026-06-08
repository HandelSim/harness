# External Integrations

**Analysis Date:** 2026-06-08

> Terminology note: two unrelated "host" concepts exist.
> - **`harness host`** = the containerless full-stack runtime (proxy + opencode
>   as host processes). Covered under "Runtime paths".
> - **host MCP** = an MCP server run as a supervised process on the host machine
>   and wired into *container-mode* agents over the docker bridge. Covered under
>   "MCP integration".

## APIs & External Services

**Upstream LLM API (the core integration):**
- A third-party, **non-Anthropic, OpenAI-compatible chat-completions API**.
  The proxy is the only component that talks to it.
  - Base URL: `PROXY_API_URL` (REQUIRED). The proxy normalizes it to
    `scheme://host[/prefix]` and derives endpoints itself:
    - chat: `POST {base}/v1/chat/completions` (`proxy/proxy.py:95`)
    - models: `GET {base}/v1/models` (`proxy/proxy.py:96`, `proxy.py:2130`)
    - Normalization strips a trailing `/v1/chat/completions`,
      `/chat/completions`, or `/v1` so a base OR a full chat URL both work
      (`_normalize_api_base`, `proxy/proxy.py:71-91`). The harness bash CLI
      mirrors this in `_api_base`.
  - Auth: `PROXY_API_KEY` (REQUIRED), sent as `Authorization: Bearer <key>`
    (`proxy/proxy.py:2145`).
  - Model: forwarded passthrough — whatever model the request asks for is sent
    upstream; `DEFAULT_MODEL_NAME` is only the fallback when a request omits a
    model (`.env.example:33-39`). opencode's model dropdown is built from the
    upstream `/v1/models` catalog (`proxy/proxy.py:2132-2134`).

**Upstream contract quirks (verified in `proxy/proxy.py`):**
- **Self-signed TLS.** All upstream calls use `verify=False`; the
  `InsecureRequestWarning` is suppressed at import (`proxy.py:46-50`).
- **Streaming.** Responses are re-emitted as OpenAI-style SSE
  (`text/event-stream`, `data: {chunk}\n\n` … `data: [DONE]`) when
  `stream:true`, else a single `chat.completion` JSON (`proxy.py:1-9`).
- **Cooperative tool use.** Upstream models may not natively support tool
  calls, so the proxy injects tool-use prompting (mode `hybrid` default /
  `user_front` / `passthrough`, `proxy.py:344-359`), then parses ```json tool
  blocks out of the response and re-emits native `tool_calls`. It also rewrites
  the `system` role into a `user` message (upstream drops `system`).
- **Key lifecycle / locked-key unlock.** A locked key returns upstream `401`
  carrying an `unlock_url`; the proxy passes the upstream status+body through
  verbatim (both on chat and `/v1/models`) so the unlock flow reaches opencode
  unchanged (`proxy.py:2138-2142`).
- **Timeout.** `PROXY_TIMEOUT` seconds (default 180).
- See `architecture/upstream-api.md` for the authoritative contract.

**Proxy's own surface (what opencode calls):**
- `GET /health` → `{"status":"ok"}` (`proxy.py:2125`); the docker healthcheck
  curls it (`docker-compose.yml:92-97`).
- `GET /v1/models` → upstream catalog passthrough (`proxy.py:2130`).
- `POST /v1/chat/completions` (via `catch_all`, `proxy.py:2175-2177`) — the
  translated chat endpoint.
- Errors use the `{"error":{"message":...}}` envelope the AI SDK parses
  (`proxy.py:2168-2172`).

**Agent → proxy wiring:**
- opencode uses the `@ai-sdk/openai-compatible` provider with
  `baseURL = http://proxy:${PROXY_PORT:-8000}/v1` and a dummy apiKey
  (`agents/entrypoint.sh:135,189-191`). The compose service name `proxy` is
  load-bearing — renaming it breaks this URL (`docker-compose.yml:3-7`).
- In **host mode** the proxy binds `127.0.0.1:${PROXY_PORT}` instead, and the
  host opencode config points there (`harness:2794-2799`).

## Runtime Paths (how components are composed)

**Container mode (default) — docker / podman + compose:**
- Orchestrated by `docker compose` over `docker-compose.yml`. Runtime is docker
  or podman, auto-detected unless `HARNESS_CONTAINER_RUNTIME` pins it
  (`.env.example:189-197`, `harness:529-530`).
- Services on a single user-defined bridge network `harness-net`
  (`docker-compose.yml:13-16`); services reach each other by service name.
- `proxy` service: built from `proxy/Dockerfile`, healthchecked, `restart:
  unless-stopped`, `NET_ADMIN`+`NET_RAW` for the firewall, IPv6 disabled via
  sysctl (`docker-compose.yml:24-97`).
- `agent` service: built from `agents/Dockerfile`, in the `agent` compose
  profile so `compose up` skips it; launched ephemerally per-invocation via
  `docker run` by the CLI (`docker-compose.yml:105-131`). Entrypoint dispatches
  on a mode arg (`opencode` / `shell`); does uid/gid remap + `gosu` drop and
  same-path CWD bind-mounts.

**Host mode (`harness host`, containerless):**
- No docker, no compose, no firewall. Proxy runs from a lazy venv at
  `state/host/venv` (built once from `proxy/requirements.txt`,
  `harness:2748-2778`); started with `nohup`, bound to `127.0.0.1`, PID tracked
  at `state/host/proxy.pid`, logs at `state/host/proxy.log`
  (`harness:2780-2801`). opencode runs off the host PATH.
- Mandatory per-launch confirmation gate (`host_confirm_gate`, `harness:2710`)
  — runs as the full host user with unrestricted egress; `HARNESS_HOST_CONFIRM=1`
  bypasses for automation. `harness host down` stops the proxy
  (`harness:3025`). No host-MCP wiring in host mode (v1, `harness:2615`).

## Data Storage

**Databases:** None. No DB/ORM in the stack.

**File storage / runtime state:**
- All runtime state under `<install-root>/state/` (gitignored):
  - `state/output/` — proxy debug dumps when `OUTPUT_DIR` is set
    (`docker-compose.yml:75`, `.env.example:71-90`).
  - `state/agent/home/` — shared persistent agent `/home/harness`
    (`harness:11-19`).
  - `state/mcp/<name>/` — active MCP services + clones/data.
  - `state/host/` — host-mode venv, proxy pidfile/logfile, opencode config.
- Agent file access: same-path host↔container bind mounts of the CWD, plus
  optional `HARNESS_EXTRA_MOUNTS` / `--mount` (`.env.example:108-131`).

**Caching:** None (no Redis/Memcached). opencode session state persists in the
agent home; proxy is stateless.

## Authentication & Identity

- **Upstream auth:** bearer token `PROXY_API_KEY` (proxy → upstream only).
- **Agent → proxy:** no real auth; a dummy apiKey (`agents/entrypoint.sh:191`).
  The proxy is only reachable on `harness-net` (container) or loopback (host).
- **Git credentials (agents):** `configure-git-credentials.sh` sets
  `credential.helper=/bin/false` by default; `git push` is blocked unless a
  host is annotated `# git-push` in the allowlist; interactive prompts disabled
  via `GIT_TERMINAL_PROMPT=0` / `GIT_ASKPASS=/bin/false`
  (`agents/Dockerfile:135-140`, `firewall/init-firewall.sh` step 5).

## Monitoring & Observability

- **Error tracking:** None (no Sentry/etc.). Proxy prints `[req_id] ...` lines
  to stdout (`proxy.py:2160`); per-request debug dumps when `OUTPUT_DIR` set.
- **Logs:** container logs via `docker logs`; host-mode proxy log at
  `state/host/proxy.log`. Firewall logs `[harness-firewall]` lines
  (`firewall/init-firewall.sh:28-40`).
- **Health:** `GET /health` (compose healthcheck).

## Network / Egress Firewall

- **Universal egress firewall** (`firewall/init-firewall.sh`) runs at container
  startup as root (needs `NET_ADMIN`/`NET_RAW`), laying a default-deny iptables
  policy with an allowed-host ipset.
- Allowlist read from `/etc/harness/allowlist`, mounted from
  `<install-root>/.harness-allowlist` (`docker-compose.yml:77`,
  `.harness-allowlist.example`). One host per line; `# git-push` annotation
  enables push to that host.
- **Proxy-specific check:** the firewall validates `PROXY_API_URL`'s hostname
  is on the allowlist and aborts otherwise — the upstream LLM host MUST be added
  to the allowlist (`firewall/init-firewall.sh:11-14`,
  `.harness-allowlist.example:29-35`).
- IPv4-only (iptables + `dig +short A` + inet ipset); IPv6 disabled in-container
  via sysctl to close the unfiltered v6 path (`docker-compose.yml:82-88`).
- Bypass: `harness --net` (per-invocation) or `harness net open` (per-service)
  set `HARNESS_FIREWALL_DISABLED=1`, logged loudly (`init-firewall.sh:42-50`).
- **Host mode has NO firewall** — egress is unrestricted (`harness:2610-2613`).

## MCP Integration

**Registry:** `mcp-registry/` holds vetted, container-based MCP definitions.
- Current entry: **Serena** (semantic code analysis, SSE transport).
  - `mcp-registry/serena/compose.yml` — merged into the harness compose graph
    via extra `-f` args; image `harness-serena:v0.1`, built host-side from a
    local clone at `state/mcp/serena/repo` (build context, not a git-URL, to
    dodge Windows path bugs).
  - Agents reach it at `http://serena:9121/sse` on `harness-net`
    (`mcp-registry/serena/client-config.json`).
  - Mounts the user's project root read-only at `/workspaces/projects`
    (`HARNESS_PROJECTS_ROOT`, default host `$HOME`); optional dashboard publish
    via `SERENA_DASHBOARD_PORT` (`.env.example:96-138`).
- Per-MCP metadata: `client-config.json` (opencode MCP wiring),
  `harness-meta.json.template` (clone URL etc.), `recency.json` (per-tool hints
  + `state_check` flags surfaced to the proxy via `HARNESS_MCP_TOOL_RECENCY` /
  `HARNESS_MCP_STATE_CHECK`, `docker-compose.yml:54-64`).

**Host MCP (`host-mcp/`):** MCP servers run as supervised host *processes*
(no container) but wired into *container-mode* agents.
- harness supervises them: pidfile/logfile under `state/mcp/<name>/`,
  started/stopped with the stack (`host_mcp_start_enabled`/`host_mcp_stop_all`,
  `harness:926,1560,1588-1592,2472-2500`).
- Template at `host-mcp/template/` uses the **MCP Python SDK** (`mcp>=1.27`,
  `FastMCP` + streamable-http transport, `host-mcp/template/server.py`,
  `requirements.txt`); `run.sh` builds a `.venv` and launches the server,
  portable across Linux/macOS/Windows Git Bash. Use case: a host-side build MCP
  that shells out to `cmake`/`ctest` (`host-mcp/template/project.json`,
  `AGENTS.md`).
- Container agents reach a host MCP over the docker bridge; harness injects the
  required `docker run` args (`emit_host_mcp_docker_args`, `harness:2461`).
- Recency metadata sourced from `host-mcp/<name>/recency.json`
  (`harness:693-694,735-736`).

## CI/CD & Deployment

**Hosting:** Self-hosted only; the install root is a git clone. No PaaS.

**Install / upgrade:**
- `harness-install.sh` (38KB) clones the repo and symlinks the CLI into
  `~/.local/bin/harness`. Upgrades via `harness update`/`upgrade` (git pull +
  `scripts/upgrade-manifest.json` + `scripts/lib/upgrade_actions.sh`).
- Host-side corporate proxy support: `HTTP_PROXY`/`HTTPS_PROXY` apply to the
  install clone, `harness update`, `mcp install`, and image builds (BuildKit) —
  but are NEVER injected into running containers (`.env.example:150-183`).

**CI Pipeline:** GitHub Actions (`.github/`). CI runs the full test matrix
(unit + docker-based: proxy, harness, persistence, mcp, firewall,
scheme_contract) on every push/PR to `dev`/`main` (per CLAUDE.md). Agents
verify docker-free locally; CI runs the heavy suites.

## Environment Configuration

**Required env vars (both runtimes):**
- `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`
  (`.env.example:27,30,39`; host-mode validates the same set, `harness:2695`).

**Secrets location:**
- `.env` at install root (gitignored). Contains `PROXY_API_KEY` and possibly a
  credentialed `HTTP_PROXY` URL. Never committed; never echoed by agents.

## Webhooks & Callbacks

**Incoming:** None.

**Outgoing:** None beyond the proxy→upstream chat/models calls and agents'
allowlisted git/package-registry traffic.

---

*Integration audit: 2026-06-08*
