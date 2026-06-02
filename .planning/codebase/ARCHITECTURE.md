<!-- refreshed: 2026-06-02 -->
# Architecture

**Analysis Date:** 2026-06-02

## System Overview

`harness` is a translating proxy plus a containerized `opencode` coding
agent. The agent runs in an ephemeral container and talks to a long-running
Flask proxy over an OpenAI-compatible Chat Completions interface; the proxy
translates to/from a third-party upstream API (a Gemini Enterprise gateway
behind a self-signed cert) and injects cooperative tool-use prompts so a
non-tool-native upstream can still produce tool calls.

```text
┌──────────────────────────────────────────────────────────────────────┐
│  harness CLI (bash)  — orchestration, docker run / docker compose      │
│  `harness`                                                             │
└───────────────┬──────────────────────────────────────────────────────┘
                │ launches
                ▼
┌──────────────────────────┐  POST /v1/chat/completions  ┌──────────────┐
│ agent (opencode)         │ ─────────────────────────▶  │ proxy (Flask)│ ──▶ upstream API
│ `agents/Dockerfile`      │     (OpenAI-compatible)     │ `proxy/      │   (chat-completions,
│ `agents/entrypoint.sh`   │ ◀────── SSE / JSON ──────── │  proxy.py`   │    self-signed cert)
└──────────────────────────┘                             └──────────────┘
        │                                                       │
        │ both join harness-net (bridge) + egress firewall      │
        ▼                                                       ▼
┌──────────────────────────┐                          ┌──────────────────┐
│ MCP services (optional)  │                          │ egress firewall  │
│ `state/mcp/<name>/`      │                          │ `firewall/       │
│ `mcp-registry/<name>/`   │                          │  init-firewall.sh│
└──────────────────────────┘                          └──────────────────┘
```

There is **no** ollama hop. opencode speaks the proxy's OpenAI-compatible
endpoint directly via its `@ai-sdk/openai-compatible` provider
(`baseURL: http://proxy:${PROXY_PORT}/v1`). The old three-hop
opencode -> ollama -> proxy path has been fully removed; the only surviving
ollama token is `OLLAMA_CONTEXT_LENGTH` as a legacy read-alias for
`MODEL_CONTEXT_LENGTH` (`proxy/proxy.py`).

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| harness CLI | Orchestration: self-locate, env load, compose wrapper, agent `docker run`, doctor/preflight, net, MCP, update/upgrade | `harness` |
| Translating proxy | OpenAI ↔ upstream translation, cooperative-prompt injection, tool-call extraction, SSE/JSON emission | `proxy/proxy.py` |
| Agent image | opencode/shell runtime, UID remap, firewall, gosu drop, opencode config + model dropdown, MCP client-config merge | `agents/Dockerfile`, `agents/entrypoint.sh` |
| Egress firewall | iptables/ipset allowlist gate at the top of every container entrypoint | `firewall/init-firewall.sh` |
| Service composition | proxy + agent service definitions, harness-net, cap_add, sysctls | `docker-compose.yml` |
| MCP registry | Vetted MCP definitions; per-install active tree | `mcp-registry/<name>/`, `state/mcp/<name>/` |
| Shared libraries | OS/runtime detection, portable realpath, net helpers, upgrade actions | `scripts/lib/*.sh` |

## Pattern Overview

**Overall:** A single translating proxy in front of an opinionated agent
container, orchestrated by one bash CLI. The repo clone IS the install root.

**Key Characteristics:**
- Two monolithic files carry most logic: `proxy/proxy.py` (~1870 lines) and
  `harness` (~5481 lines). Both are single-file by design.
- The proxy is single-process, single-file Flask. It does NOT stream from
  upstream: it gets the full upstream response, then translates and emits.
- The CLI launches agents by direct `docker run`, not compose; compose runs
  only the long-running services (proxy + any enabled MCP).
- Same-path bind mounts: the host CWD is mounted into the agent at the same
  absolute path, so `pwd` round-trips.

## Layers

**Orchestration (`harness` CLI):**
- Purpose: Resolve install root, load `.env`, build the compose graph, launch
  ephemeral agents, run diagnostics, manage net/MCP/update.
- Location: `harness`, `scripts/lib/*.sh`.
- Depends on: docker/podman runtime, `docker-compose.yml`, `state/`.
- Used by: the human operator (and tests via `HARNESS_INSTALL_ROOT`).

**Translation (`proxy/proxy.py`):**
- Purpose: Expose OpenAI Chat Completions, translate to the upstream wire
  format, inject cooperative tool-use scaffolding, parse tool calls back.
- Location: `proxy/proxy.py`.
- Depends on: the upstream API (`PROXY_API_URL`), the egress firewall.
- Used by: opencode in the agent container.

**Agent runtime (`agents/`):**
- Purpose: Run opencode/shell under the right UID, firewall, and config;
  build opencode's provider block and model dropdown from the proxy's
  `/v1/models`.
- Location: `agents/Dockerfile`, `agents/entrypoint.sh`.
- Depends on: the proxy (`http://proxy:${PROXY_PORT}/v1`), the firewall.
- Used by: the CLI's `run_agent` / `cmd_shell`.

## Data Flow

### Primary Request Path (proxy request lifecycle)

`catch_all(path)` (`proxy/proxy.py:1643`) is the single Flask route handler
that owns every non-health request:

1. Read JSON body — `model`, `messages`, `tools`, OpenAI `stream`
   (`proxy/proxy.py:1648-1657`).
2. Build a tools-as-text string via `format_tools_to_text`
   (`proxy/proxy.py:1661`, def at `proxy/proxy.py:432`).
3. `translate_history_and_apply_prompt(...)` flattens the inbound
   conversation into a role-alternating array and injects the
   cooperative-prompt scaffold per the active prompt mode
   (`proxy/proxy.py:1662`, def at `proxy/proxy.py:1077`).
4. POST `{model: <requested>, messages: translated}` to `CHAT_URL`
   (`{base}/v1/chat/completions`) with `Bearer PROXY_API_KEY`,
   `verify=False` (self-signed cert), `timeout=PROXY_TIMEOUT`
   (`proxy/proxy.py:1690-1697`). The requested model is forwarded verbatim,
   falling back to `DEFAULT_MODEL_NAME` only when absent
   (`proxy/proxy.py:1669`).
5. `extract_assistant_content(target_json)` pulls assistant text
   (`proxy/proxy.py:1722`, def at `proxy/proxy.py:1501`).
6. `extract_tool_calls_and_text(...)` parses ```json tool-call blocks out of
   the text using `_scan_balanced_json` (`proxy/proxy.py:1726`, def at
   `proxy/proxy.py:797`; scanner at `proxy/proxy.py:752`).
7. Empty-response rescue: if both `clean_text` and tool calls are empty,
   substitute `"Understood."` and a no-op `pwd` bash call when a shell tool
   is exposed (`proxy/proxy.py:1750-1764`).
8. Emit OpenAI: `generate_openai_sse` when `stream: true`, else
   `build_openai_response` (`proxy/proxy.py:1790-1798`; defs at
   `proxy/proxy.py:1392` and `proxy/proxy.py:1453`).

Errors return the OpenAI `{"error":{"message":…}}` envelope via
`_client_error` (`proxy/proxy.py:1634`) plus a debug dump under `OUTPUT_DIR`.

### Agent Launch Flow

1. `harness` (bare, or with a leading agent flag) dispatches to `run_agent`
   (`harness:2270`); `harness shell` to `cmd_shell` (`harness:2696`).
2. The CLI computes mounts, cap_add (NET_ADMIN/NET_RAW), allowlist mount, and
   network, then `docker run`s `harness-agent:latest` with a mode arg.
3. `agents/entrypoint.sh` runs the firewall, remaps UID, drops privileges via
   gosu, writes opencode config, merges MCP client-config, and `cd`s to the
   host CWD before dispatching to opencode or bash.

### GET /v1/models Passthrough

`list_models` (`proxy/proxy.py:1597`) is a thin pass-through that forwards
`MODELS_URL` with the bearer key and returns the upstream status + body
verbatim. The agent entrypoint consumes it at startup to build opencode's
model dropdown; a locked-key 401 (with its unlock URL) reaches the caller
unchanged. Declared as an explicit route so it wins over `catch_all`.

**State Management:**
- Per-request only inside the proxy; no cross-request session state. Local
  token estimation (`_estimate_tokens`, `proxy/proxy.py:1357`) gives the
  agent a monotonic context count because upstream `prompt_tokens` is
  unreliable (sliding-window truncation).
- Agent home persists across launches via the `state/agent/home/` bind mount.

## Key Abstractions

**Cooperative-prompt mediation:**
- Purpose: The upstream does not natively support tool calls, so the proxy
  injects a scaffold telling the model to emit ```json blocks of the form
  `{"name": ..., "arguments": {...}}`, then parses them back into native
  `tool_calls`.
- Modes (`PROXY_PROMPT_MODE`, resolved in `_setup_prompt_mode`,
  `proxy/proxy.py:298`):
  - `hybrid` (default) — tool definitions on the system message (folded into
    the user-role index-0 stable prefix), plus a consolidated recency block on
    the last user message with the live request first
    (`<<<BEGIN_USER_REQUEST>>>`), then a reminder with Operating/Honesty/
    Environment bullets and one entry per tool. Hybrid alone emits the
    `<<<BEGIN_X>>>` content-category delimiters.
  - `user_front` — full scaffolding on the last user message, request placed
    before the tool list.
  - `passthrough` — benchmark control; skips all mediation and forwards
    `tools` to upstream verbatim (`proxy/proxy.py:1681-1682`).
- `PROXY_PROMPT_MODE` is NOT a user `.env` knob. The proxy defaults to
  `hybrid`; `docker-compose.yml` no longer interpolates it (a stale `.env`
  cannot override). `harness start/restart --prompt-mode <mode>` injects it
  ephemerally via the runtime override.

**Tool-call extraction:**
- `extract_tool_calls_and_text` walks the response left-to-right for ```json
  fences and uses `_scan_balanced_json` (not regex) to find the matching
  closing brace, so argument strings containing backticks/nested fences do
  not truncate. Parsing uses `json.loads(..., strict=False)` for embedded
  control characters; misshaped calls with a known `name` but no `arguments`
  get a tolerant top-level lift. Blocks that fail to parse are left in text.

## Container Topology

`docker-compose.yml` defines two services on one bridge network
(`harness-net`):

- **proxy** (`docker-compose.yml:24-80`) — builds `proxy/Dockerfile`, image
  `harness-proxy:latest`. Entrypoint runs `init-firewall.sh` then
  `python3 proxy.py`. `cap_add: NET_ADMIN, NET_RAW`, IPv6 disabled via
  `sysctls`, `restart: unless-stopped`, healthcheck on `/health`. The only
  long-running service besides enabled MCPs.
- **agent** (`docker-compose.yml:88-113`) — builds `agents/Dockerfile`, image
  `harness-agent:latest`, behind the `agent` compose profile so
  `docker compose up` skips it. Launched ephemerally by the CLI via
  `docker run`; the compose entry documents the runtime contract and supports
  `docker compose up agent` for debugging.
- **MCP services** — each enabled `state/mcp/<name>/compose.yml` is spliced
  into the compose graph by `mcp_compose_files` (`harness:577`); snippets
  reference `harness_harness-net` as `external` and sit behind the `mcp`
  profile.
- **firewall** — not a service; `firewall/init-firewall.sh` runs as root at
  the top of every container entrypoint, reading the bind-mounted allowlist.

The hostname `proxy` is load-bearing: opencode's `baseURL` points at
`http://proxy:${PROXY_PORT}/v1` (`docker-compose.yml:1-7`).

## CLI Orchestration Role

- **Compose wrapper** — `compose()` (`harness:758`) is the single entry point
  for any `docker compose`/`podman compose` call. It regenerates the runtime
  override, builds the `-f` args (main compose + override + MCP snippets), and
  exports `INSTALL_ROOT`, `HARNESS_ALLOWLIST_PATH`, `HARNESS_PROJECTS_ROOT`,
  and `HARNESS_HOST_OS`.
- **Runtime override** — `write_runtime_override()` (`harness:672`)
  regenerates `state/.harness-runtime.yml` per invocation (never tracked): a
  per-service firewall opt-out and the ephemeral `--prompt-mode` injection.
- **Agent launch** — `run_agent` / `cmd_shell` parse agent flags
  (`--yolo`, `--net`, `--mount`, `-p/--print`), compute mounts, and
  `docker run` the agent image directly (not via compose).
- **Upstream gating** — `_probe_upstream_auth` (`harness:960`) POSTs to the
  upstream before start and aborts on a locked/rejected key with the clickable
  unlock URL.
- **Diagnostics** — `cmd_doctor` (`harness:4564`, read-only) and
  `cmd_preflight` (`harness:4869`, validates `.env`/allowlist/daemon).

## MCP Lifecycle

State machine (`architecture/mcp.md`):

```text
available ──install/register──▶ installed-enabled ⇄ disable/enable ⇄ installed-disabled ──uninstall──▶ available
```

- `install` copies a repo-tracked `mcp-registry/<name>/` into
  `state/mcp/<name>/`; `register` lands an arbitrary external source behind a
  compose-merge validation gate (merge check, service/container_name
  collision check, port-warning).
- `mcp_is_installed(name)` is "compose.yml exists in the active tree" — not
  directory existence — so `data/` survives uninstall.
- Enabled entries reach the runtime two ways: compose merge
  (`mcp_compose_files`, `harness:577`) and client-config merge into
  `state/agent/home/.harness-mcp-servers.json`, folded into opencode config
  by the agent entrypoint (`merge_opencode_mcp_servers`).
- `harness upgrade` refreshes registry-sourced entries via
  `directory_overwrite`, preserving `harness-meta.json` and `data/`.

## Install / Upgrade Flow

- `harness-install.sh` clones the repo (the clone IS the install root), seeds
  user config and `state/`, and symlinks `harness` into `~/.local/bin`.
- `harness update` / `harness upgrade` operate on the same clone via
  `git pull --ff-only`. `harness upgrade` brings forward `state/` per
  `scripts/upgrade-manifest.json`, using action functions in
  `scripts/lib/upgrade_actions.sh` (envfile_merge / linefile_merge /
  directory_overwrite), anchored on B3-MANAGED comment markers.

## Architectural Constraints

- **Threading:** The proxy is single-process Flask and does NOT stream from
  upstream — it receives the full upstream response, then translates and
  emits (`proxy/proxy.py` response-emission section). Chunks are materialized
  to a list before streaming.
- **Self-signed upstream:** Every upstream POST uses `verify=False`
  (`proxy/proxy.py:1695`). The egress firewall is the trust boundary, not
  TLS verification.
- **System role conversion:** `_CHANGE_SYSTEM_TO_USER` is default ON — the
  upstream silently drops the `system` role, so system messages are folded
  into a leading user message (`proxy/proxy.py`, `translate_history_…`).
- **Model passthrough:** `PROXY_API_URL` is a base, not a full endpoint;
  `_normalize_api_base` (`proxy/proxy.py:72`) strips trailing
  `/v1/chat/completions`, `/chat/completions`, or `/v1`. `DEFAULT_MODEL_NAME`
  is REQUIRED with no hardcoded default and is only the fallback.
- **CLI/proxy URL parity:** the CLI's `_api_base` must mirror the proxy's
  `_normalize_api_base` — keep the two in sync.
- **IPv4-only firewall:** containers disable kernel IPv6 at creation via
  sysctls, or IPv6 egress would be an unfiltered hole.

## Anti-Patterns

### Treating `PROXY_PROMPT_MODE` as a `.env` knob

**What happens:** A stale `.env` carries `PROXY_PROMPT_MODE=...` and someone
assumes it controls the proxy.
**Why it's wrong:** `docker-compose.yml` deliberately no longer interpolates
it, so the value is inert; only the runtime override
(`harness start/restart --prompt-mode`) reaches the proxy now.
**Do this instead:** Use `--prompt-mode <hybrid|user_front|passthrough>` on
`start`/`restart`; the validator falls back to `hybrid` for any other value.

### Adding a fixed model id at the proxy

**What happens:** Hardcoding a model id instead of forwarding the requested
one.
**Why it's wrong:** The requested model flows opencode -> proxy -> upstream
unchanged; that passthrough is what lets a user switch models from opencode
(`proxy/proxy.py:1664-1669`).
**Do this instead:** Forward `model_name or DEFAULT_MODEL_NAME`; treat
`DEFAULT_MODEL_NAME` only as the no-model-specified fallback.

### Parsing tool-call JSON with regex

**What happens:** A lazy regex match on ```json fences terminates on the
first inner ``` inside an argument string.
**Why it's wrong:** It truncates JSON whose arguments contain backticks or
nested code fences.
**Do this instead:** Use `_scan_balanced_json` (`proxy/proxy.py:752`), which
tracks string boundaries and escapes.

## Error Handling

**Strategy:** The proxy returns OpenAI-shaped error envelopes so the AI SDK
surfaces them to opencode; every error path writes a debug dump under
`OUTPUT_DIR`.

**Patterns:**
- Upstream request failure -> 502 + `_03_API_Error.json`
  (`proxy/proxy.py:1698-1701`).
- Upstream >= 400 -> 502 with the upstream status (`proxy/proxy.py:1703-1711`).
- Unhandled exception -> 500 + `_99_Fatal_Error.json`
  (`proxy/proxy.py:1800-1804`).
- The CLI gates the launch on a bad/locked upstream key
  (`_probe_upstream_auth`) so a bad key never reaches the proxy.

## Cross-Cutting Concerns

**Logging:** `print(..., flush=True)` per request in `catch_all` (req id,
method, model, message/tool counts, rescue mode), visible via
`harness logs proxy`. Per-request debug dumps under `state/output/`.
**Validation:** `_validate_config` (`proxy/proxy.py:1819`) enforces the three
REQUIRED env values at startup; `cmd_preflight` validates `.env`, allowlist,
and daemon before `harness start`.
**Authentication:** `Bearer PROXY_API_KEY` on every upstream call; the egress
firewall restricts which hosts the proxy/agent can reach.

---

*Architecture analysis: 2026-06-02*
