# External Integrations

**Analysis Date:** 2026-06-02

## APIs & External Services

**Upstream LLM API (the only mandatory external dependency):**
- A Gemini Enterprise chat product exposed behind a chat-completions-shaped HTTP API (`architecture/upstream-api.md:9-14`). It is a general chat assistant, not a coding-agent API.
- What harness talks to it with: the proxy (`proxy/proxy.py`) POSTs to a derived `{base}/v1/chat/completions` endpoint and GETs `{base}/v1/models`. `PROXY_API_URL` is treated as a BASE, normalized by `_normalize_api_base` (strips a trailing `/v1/chat/completions`, `/chat/completions`, or `/v1`); `CHAT_URL` and `MODELS_URL` are derived once at import (`architecture/proxy.md:53-73`).
- Auth: `Authorization: Bearer ${PROXY_API_KEY}` header on every upstream request (`architecture/proxy.md:38-39`). The same bearer is used for `/v1/models`.
- TLS: the upstream uses a self-signed cert, so the proxy calls with `verify=False` (`architecture/proxy.md:38-39`).
- Multi-model passthrough: the upstream honors the request's `model` field and advertises an OpenAI-style `/v1/models` catalog. The proxy forwards the model opencode selected verbatim, falling back to `DEFAULT_MODEL_NAME` only when a request omits a model (`architecture/proxy.md:61-66`, `architecture/upstream-api.md:16-22`).
- Config knobs: `PROXY_API_URL` (REQUIRED base), `PROXY_API_KEY` (REQUIRED bearer), `DEFAULT_MODEL_NAME` (REQUIRED fallback id), `PROXY_TIMEOUT` (request timeout, default 180s) (`.env.example:19-42`).
- Known upstream quirks the proxy is built around (`architecture/upstream-api.md:30-54`): no native tool support (the proxy injects cooperative-prompt tool-use), a hidden/uncontrollable system prompt (so the proxy always converts the `system` role to `user`), no model-side web access, consecutive `user` messages collapse, and unreliable `usage` (so the proxy estimates tokens locally).
- Failure modes: HTTP `400/401/403/404/429/500/502` (`architecture/upstream-api.md:146-156`). A `401` with an `unlock_url` body means the key is locked (see Authentication below); an empty/unknown-type `401`/`403` is treated as a rejected key. Empty-response short-circuits (well-formed `stop` with zero completion tokens) trigger the proxy's two-part rescue (`Understood.` text plus an optional `bash pwd` rescue tool call) (`architecture/proxy.md:391-449`).

**opencode coding agent:**
- The agent that drives the work, running in the agent container (`harness-agent:latest`). It speaks to the proxy directly over the proxy's OpenAI-compatible interface; there is no ollama hop (`architecture/containers.md:74-90`).
- How harness talks to it: the agent entrypoint writes `~/.config/opencode/opencode.json` with a harness provider block (display name `GenAI Harness`) whose `@ai-sdk/openai-compatible` `baseURL` is `http://proxy:${PROXY_PORT}/v1` (`architecture/containers.md:74-90`, `architecture/proxy.md:9-12`). The model dropdown is built from the proxy's `GET /v1/models` pass-through; `DEFAULT_MODEL_NAME` is always included and is the default selection.
- Config knobs: `PROXY_PORT` (passed into the agent container as `-e` so a custom port resolves), `DEFAULT_MODEL_NAME`, `--yolo` (grants `edit`/`bash`/`webfetch`/`websearch` permissions), `OPENCODE_ENABLE_EXA` (conditionally exported to surface opencode's `websearch` tool).
- Failure modes: if the proxy is unreachable or its key is locked, the model list falls back to `DEFAULT_MODEL_NAME` alone (`architecture/containers.md:84-87`). The headless `-p` path recovers the final assistant text from the persisted session because opencode's non-TTY renderer can race and drop stdout (`architecture/containers.md:123-152`).

**Exa hosted MCP (`mcp.exa.ai`) — opencode `websearch`:**
- opencode's built-in `websearch` tool hits Exa's hosted MCP. The egress firewall blocks it by design with no allowlist entry — websearch belongs to the firewall-down / `--net` use case (`architecture/containers.md:97-103`).
- Reachability gate: opencode's MCP startup is synchronous, so with the firewall up the TCP REJECT is instant (harmless); with the firewall down the entrypoint probes Exa with a 1s connect / 2s overall `curl` before exporting `OPENCODE_ENABLE_EXA=1`, skipping registration if Exa is slow so the TUI still starts (`architecture/containers.md:105-120`).

## Data Storage

**Databases:**
- None. harness runs no database.

**File Storage:**
- Local filesystem only. The host CWD is bind-mounted into the agent at the SAME absolute path (no `/workspace` indirection), so `pwd` round-trips host↔container (`architecture/containers.md:161-188`). Extra folders via `--mount` / `HARNESS_EXTRA_MOUNTS`.
- Persistent agent home: `<install-root>/state/agent/home/` backs every agent invocation; anything a user installs inside an agent survives rebuilds (`architecture/containers.md:190-198`).
- Proxy debug dumps: when `OUTPUT_DIR` is set, per-request JSON dumps land in `<install-root>/state/output/` (`architecture/proxy.md:567-579`).

**Caching:**
- None.

## Authentication & Identity

**Upstream API key lifecycle:**
- The key (`PROXY_API_KEY`) is a long-lived bearer with a lock/expiry lifecycle (`architecture/upstream-api.md:57-84`): keys lock roughly every 8 hours (measure point unverified), expire after ~1 month, and usage is effectively unlimited (though `429` is still possible).
- Lock flow: a locked key returns `401` with `{"error":{"type":"unauthorized","message":"API key locked...","unlock_url":"https://.../unlock/<id>"}}`. Visiting `unlock_url` in a signed-in browser re-enables it. Unlocking CANNOT be automated by harness because it needs a signed-in browser session (`architecture/upstream-api.md:65-84`).
- Launch gating: `harness` runs `_probe_upstream_auth` (POST to `{base}/v1/chat/completions`) before starting the stack and aborts on a locked or rejected key, printing the clickable unlock URL. An `invalid_request`-family error (key fine, request bad) warns and continues. Honors `HARNESS_SKIP_AUTH_PROBE=1` (`architecture/harness-cli.md:43-59`). `harness unlock` is a dedicated subcommand (`architecture/harness-cli.md:81`).
- Storage: `PROXY_API_KEY` lives in the gitignored `.env`. The installer can capture it at install time with a masked prompt and write only that line (`architecture/install-and-upgrade.md:18-23`). Logs redact it via `_redact_key` (`architecture/proxy.md:550-553`).

**Git push credentials (agent containers):**
- `configure-git-credentials.sh` sets `credential.helper=/bin/false` globally, then enables the `store` helper only for allowlist hosts annotated `# git-push` (`architecture/containers.md:57-61`, `.harness-allowlist.example` GitHub section). Without the annotation, pull works but push fails.

## Monitoring & Observability

**Error Tracking:**
- None (no Sentry/Datadog/etc.).

**Logs:**
- Plain stdout/stderr from each container, surfaced via `harness logs proxy` / `harness logs` and `harness mcp logs <name>`. The proxy `print()`s startup config (key redacted) and per-event diagnostics like the empty-response rescue mode (`architecture/proxy.md:429-432`, `:550-553`).
- Optional per-request debug dumps under `state/output/` when `OUTPUT_DIR` is set (`architecture/proxy.md:567-579`).

## CI/CD & Deployment

**Hosting:**
- None. harness is a locally-installed tool on a laptop/host; the git clone IS the install root (`architecture/README.md:27-53`). There is no remote deploy target.

**CI Pipeline:**
- GitHub Actions runs the full test matrix on every push/PR to `dev`/`main` (project `CLAUDE.md`). The docker-based suites (`proxy`, `harness`, `persistence`, `mcp`, `firewall`, `scheme_contract`), `--slow`, integration, full-pipeline, and benchmarks run in CI, not from issue agents.

## Environment Configuration

**Required env vars:**
- `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME` (`.env.example:19-39`). The stack refuses to start without them (`require_runtime_config` in the CLI; `_validate_config` in the proxy).

**Secrets location:**
- `.env` at the install root, gitignored. `PROXY_API_KEY` and any credentialed `HTTP_PROXY`/`HTTPS_PROXY` URL are the sensitive entries (`.env.example:29`, `:180-181`). Never committed; redacted in proxy logs.

## Webhooks & Callbacks

**Incoming:**
- None. The proxy's only inbound surface is the OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`, `/health`) reachable only on the internal `harness-net` bridge — the host does not publish the port (`.env.example:58-60`).

**Outgoing:**
- The proxy's upstream chat/models calls (above). No other outgoing webhooks.

## Networking & Egress Firewall

**`harness-net` bridge:**
- All long-running services (proxy, any enabled MCP) join `harness_harness-net` and reach each other by service name (`http://proxy:8000`, `http://harness-serena:9121/sse`). Agent `docker run` invocations attach to the same network (`architecture/containers.md:247-254`).

**Universal egress firewall:**
- `firewall/init-firewall.sh` runs as root at the top of every container entrypoint (proxy and agent). It reads `/etc/harness/allowlist` (bind-mounted from `<install-root>/.harness-allowlist`) and drops all egress except DNS, the `PROXY_API_URL` host, and allowlisted hosts (`architecture/containers.md:200-225`).
- The proxy entrypoint additionally validates that `PROXY_API_URL`'s host is on the allowlist before applying rules; if not, the proxy aborts (`proxy/entrypoint.sh`, `architecture/containers.md:213-215`). The allowlist example seeds package registries and GitHub but leaves the upstream LLM host for the user to add (`.harness-allowlist.example`).
- IPv4-only: rules use `iptables` + `dig +short A` + an `inet` ipset, so containers disable IPv6 via the `net.ipv6.conf.all.disable_ipv6=1` sysctl to close the v6 hole (`architecture/containers.md:216-224`).
- Knobs / failure modes: `HARNESS_FIREWALL_DISABLED=1` short-circuits the firewall with a loud bypass message (set by `harness net open <service>` and `--net`). `harness net` subcommands (`list`/`allow`/`deny`/`edit`/`status`/`open`/`close`) manage the allowlist; `net open` requires typing `I understand the risks` on a TTY (`architecture/containers.md:210-230`).

**Host corporate proxy (`HTTP_PROXY`/`HTTPS_PROXY`):**
- Optional. Exported into the harness process so host-side git (clone, `update`/`upgrade` pull, `mcp install`) and `docker compose build` route through it (BuildKit reads them). They are NOT injected into running containers — runtime egress stays on the firewall path (`architecture/harness-cli.md:61-72`, `architecture/containers.md:233-245`). A credentialed proxy URL ends up stored in the gitignored `.env` (`.env.example:180-181`).

## MCP Servers

- Long-running MCP services live under `mcp-registry/<name>/` (vetted, in-repo) and `state/mcp/<name>/` (per-install, active). `mcp-registry/serena/` is the reference entry, pinned to upstream tag `v1.3.0` (`architecture/mcp.md:32-44`, `mcp-registry/serena/harness-meta.json.template`).
- How harness wires them in: each registry entry ships `compose.yml` (merged into the runtime graph via extra `-f` flags), `client-config.json` (the canonical `{"mcpServers": {...}}` shape, translated to opencode's `{"mcp": {...}}` on each agent launch), an optional `harness-meta.json[.template]`, and a `README.md` (`architecture/mcp.md:10-44`, `:157-181`).
- Lifecycle: `harness mcp install|register|uninstall|enable|disable|up|down|logs|status|list`. `install` copies a repo-tracked entry; `register --from <dir|git-url>` lands an external source behind a compose-merge + service/container-name-collision validation gate (`architecture/mcp.md:47-155`). Enabled entries with a `compose.yml` auto-start with `harness start`.
- Config knobs: per-MCP env vars in `.env` (e.g. `HARNESS_PROJECTS_ROOT`, `SERENA_DASHBOARD_PORT`); `repo_clone_url`/`repo_clone_ref` in `harness-meta.json` drive a host-side clone into `state/mcp/<name>/repo/` as the build context (`.env.example:96-138`, `architecture/mcp.md:22-30`).
- Egress: an MCP's `allowed_domains` is surfaced as recommended `harness net allow` commands; the allowlist is never modified automatically (`architecture/mcp.md:204-212`).
- Failure mode: a snippet that fails to merge would take the whole `docker compose up` (proxy + agent) down — which is exactly why `register` validates the merge before arming (`architecture/mcp.md:130-147`).

---

*Integration audit: 2026-06-02*
