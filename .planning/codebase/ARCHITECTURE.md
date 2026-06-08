<!-- refreshed: 2026-06-08 -->
# Architecture

**Analysis Date:** 2026-06-08

## System Overview

`harness` is a translating proxy plus an opinionated `opencode` coding agent,
orchestrated by one bash CLI. The agent talks to a long-running Flask proxy
over an OpenAI-compatible Chat Completions interface; the proxy translates
to/from a third-party upstream chat-completions API (self-signed cert) and
injects cooperative tool-use prompts so a non-tool-native upstream can still
produce tool calls.

There are **two runtime paths** for the same proxy↔opencode wiring:

1. **Container mode (normal):** `harness` launches ephemeral agent containers
   via direct `docker run`; the proxy (and any enabled MCP) run as long-lived
   compose services. A universal egress firewall and container/user isolation
   apply. Components reach each other by service name on the `harness-net`
   bridge.
2. **Host mode (`harness host`, containerless):** the proxy and opencode run as
   plain **host processes** — no docker, no images, no firewall, no isolation.
   Lighter footprint, strictly bigger blast radius, so **every launch is gated
   behind a mandatory confirmation**.

```text
┌──────────────────────────────────────────────────────────────────────┐
│  harness CLI (bash, ~6589 lines)  — self-locate, env, docker run /     │
│  docker compose, host-mode supervision, net, MCP, update/upgrade       │
│  `harness`                                                             │
└──────────┬───────────────────────────────────┬───────────────────────┘
           │ container mode                     │ host mode (`harness host`)
           │ (docker run / compose)             │ (host processes, gated)
           ▼                                    ▼
┌──────────────────────────┐  POST /v1/chat/completions  ┌──────────────┐
│ agent (opencode)         │ ─────────────────────────▶  │ proxy (Flask)│ ──▶ upstream API
│ `agents/Dockerfile`      │     (OpenAI-compatible)     │ `proxy/      │   (chat-completions,
│ `agents/entrypoint.sh`   │ ◀────── SSE / JSON ──────── │  proxy.py`   │    self-signed cert)
│  — OR host opencode      │                             │  — OR host   │
│    (state/host/...)      │                             │    proxy     │
└──────────────────────────┘                             └──────────────┘
        │ container mode only: both join harness-net + egress firewall │
        ▼                                                              ▼
┌──────────────────────────┐                          ┌──────────────────┐
│ MCP services (optional)  │                          │ egress firewall  │
│ `state/mcp/<name>/`      │                          │ `firewall/       │
│ `mcp-registry/<name>/`   │                          │  init-firewall.sh│
│ + host MCPs `host-mcp/`  │                          │ (container-bound)│
└──────────────────────────┘                          └──────────────────┘
```

There is **no** ollama hop. opencode speaks the proxy's OpenAI-compatible
endpoint directly via its `@ai-sdk/openai-compatible` provider
(`baseURL: http://proxy:${PROXY_PORT}/v1` in container mode,
`http://127.0.0.1:<port>/v1` in host mode). The only surviving ollama token is
`OLLAMA_CONTEXT_LENGTH`, a legacy read-alias for `MODEL_CONTEXT_LENGTH`.

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| harness CLI | Orchestration: self-locate, env load, compose wrapper, agent `docker run`, host-mode supervision, doctor/preflight, net, MCP, update/upgrade | `harness` |
| Translating proxy | OpenAI ↔ upstream translation, cooperative-prompt injection, tool-call extraction, meta-tool serving, SSE/JSON emission | `proxy/proxy.py` |
| Agent image | opencode/shell runtime, UID remap, firewall, gosu drop, opencode config + model dropdown, MCP client-config merge | `agents/Dockerfile`, `agents/entrypoint.sh` |
| Egress firewall | iptables/ipset allowlist gate at the top of every container entrypoint (container mode only) | `firewall/init-firewall.sh` |
| Service composition | proxy + agent service definitions, harness-net, cap_add, sysctls | `docker-compose.yml` |
| Container MCP registry | Vetted MCP definitions; per-install active tree | `mcp-registry/<name>/`, `state/mcp/<name>/` |
| Host MCP scaffold | Non-container build MCPs run as host processes (e.g. MSVC/CMake) | `host-mcp/template/`, `host-mcp/<name>/` |
| Shared libraries | OS/runtime detection, portable realpath, net helpers, upgrade actions | `scripts/lib/*.sh` |

## Pattern Overview

**Overall:** A single translating proxy in front of an opinionated agent,
orchestrated by one bash CLI, runnable either containerized (default) or
containerless. The repo clone IS the install root.

**Key Characteristics:**
- Two monolithic files carry most logic: `proxy/proxy.py` (~2516 lines) and
  `harness` (~6589 lines). Both are single-file by design.
- The proxy is single-process, single-file Flask. It does NOT stream from
  upstream: it gets the full upstream response, then translates and emits.
- In container mode the CLI launches agents by direct `docker run`, not
  compose; compose runs only the long-running services (proxy + enabled MCPs).
- In host mode there is no compose, no docker; the proxy is `nohup`'d with a
  pidfile/logfile under `state/host/`, and opencode runs as a host child.
- Same-path bind mounts (container mode): the host CWD is mounted into the
  agent at the same absolute path, so `pwd` round-trips.

## Layers

**Orchestration (`harness` CLI):**
- Purpose: Resolve install root, load `.env`, build the compose graph or
  supervise host processes, launch agents, run diagnostics, manage net/MCP/update.
- Location: `harness`, `scripts/lib/*.sh`.
- Depends on: docker/podman runtime (container mode) or host python3/node/jq
  (host mode); `docker-compose.yml`; `state/`.
- Used by: the human operator (and tests via `HARNESS_INSTALL_ROOT`).

**Translation (`proxy/proxy.py`):**
- Purpose: Expose OpenAI Chat Completions, translate to the upstream wire
  format, inject cooperative tool-use scaffolding, parse tool calls back,
  serve synthetic meta-tools (tool-search).
- Location: `proxy/proxy.py`.
- Depends on: the upstream API (`PROXY_API_URL`); the egress firewall (container mode).
- Used by: opencode (container or host).

**Agent runtime (`agents/` container, or host opencode):**
- Purpose: Run opencode/shell under the right UID, firewall, and config; build
  opencode's provider block and model dropdown from the proxy's `/v1/models`.
- Location: `agents/Dockerfile`, `agents/entrypoint.sh` (container);
  `host_*` functions in `harness` writing `state/host/opencode.json` (host).
- Depends on: the proxy; the firewall (container mode only).
- Used by: the CLI's `run_agent`/`cmd_shell` (container) or `cmd_host` (host).

## Data Flow

### Primary Request Path (proxy request lifecycle)

`catch_all(path)` (`proxy/proxy.py:2177`) is the single Flask route handler
that owns every non-health request:

1. Read JSON body — `model`, `messages`, `tools`, OpenAI `stream`.
2. Build a tools-as-text string via `format_tools_to_text`.
3. `translate_history_and_apply_prompt(...)` (`proxy/proxy.py:1296`) flattens
   the inbound conversation into a role-alternating array and injects the
   cooperative-prompt scaffold per the active prompt mode.
4. POST `{model: <requested>, messages: translated}` to `CHAT_URL`
   (`{base}/v1/chat/completions`) with `Bearer PROXY_API_KEY`, `verify=False`
   (self-signed cert), `timeout=PROXY_TIMEOUT`. Requested model forwarded
   verbatim, falling back to `DEFAULT_MODEL_NAME` only when absent.
5. `extract_assistant_content(...)` pulls assistant text.
6. `extract_tool_calls_and_text(...)` (`proxy/proxy.py:1016`) parses ```json
   tool-call blocks out of the text using `_scan_balanced_json`
   (`proxy/proxy.py:902`), not regex.
7. In-proxy recovery loops (all invisible to opencode): malformed tool-call
   retry (budget 1), meta-tool serving via `_serve_meta_tools`
   (`proxy/proxy.py:1997`, budget 3) when `HARNESS_TOOL_SEARCH=1`, and
   empty-response rescue (substitute a no-op `bash pwd` call when a shell tool
   is exposed, else `"Understood."`).
8. Emit OpenAI: `generate_openai_sse` (`proxy/proxy.py:1616`) when
   `stream: true`, else `build_openai_response` (`proxy/proxy.py:1677`).

Errors return the OpenAI `{"error":{"message":…}}` envelope via `_client_error`
plus a debug dump under `OUTPUT_DIR`.

### Container Agent Launch Flow

1. `harness` (bare, or with a leading agent flag) dispatches to `run_agent`
   (`harness:3047`); `harness shell` to `cmd_shell` (`harness:3508`).
2. `ensure_services_up` brings the stack up; the CLI computes mounts, cap_add
   (NET_ADMIN/NET_RAW), allowlist mount, IPv6-disable sysctl, and network, then
   `docker run`s `harness-agent:latest` with a mode arg and a per-launch random
   container name.
3. `agents/entrypoint.sh` runs the firewall, remaps UID, drops privileges via
   gosu, writes opencode config, merges MCP client-config, and `cd`s to the
   host CWD before dispatching to opencode or bash.
4. On interactive exit, `stop_stack_if_last_agent` counts remaining
   project-labelled agents and tears the stack down when none remain.

### Host Agent Launch Flow (`harness host`)

`cmd_host` (`harness:2990`) orchestrates, in order:

1. `host_preflight` (`harness:2647`) — requires host `jq`, `python3`, Node ≥ 20
   + `opencode` (no docker fallback exists). Missing deps abort with hints.
2. `host_require_config` (`harness:2687`) — same three REQUIRED proxy vars, but
   **no allowlist check** (the firewall is container-only).
3. `host_confirm_gate` (`harness:2710`) — **mandatory on every launch**;
   `HARNESS_HOST_CONFIRM=1` bypasses for automation, refuses non-interactive
   without `/dev/tty`.
4. `host_proxy_start` (`harness:2785`) — lazily builds a venv at
   `state/host/venv` (`host_proxy_ensure_venv`, re-pip only on requirements
   hash change), then `nohup`s `proxy/proxy.py` bound to `127.0.0.1` with a
   pidfile (`state/host/proxy.pid`) and logfile (`state/host/proxy.log`).
   `host_proxy_wait_ready` (`harness:2808`) polls the loopback port via
   `/dev/tcp`.
5. `host_write_opencode_config` (`harness:2854`) — writes a **scoped**
   `state/host/opencode.json` (baseURL → `http://127.0.0.1:<port>/v1`); the
   caller exports `OPENCODE_CONFIG` so the user's global config is untouched.
6. `host_run_opencode` (`harness:2920`) — mirrors the entrypoint's
   `run_opencode` (provider env, `--agent yolo`, headless `-p` json-events +
   `opencode export` dance). Interactive runs opencode as a **child** so the
   proxy is torn down on exit; print mode leaves it up. `harness host down`
   (`cmd_host_down`, `harness:3025`) stops the proxy.

### GET /v1/models Passthrough

`list_models` (`proxy/proxy.py:2131`) is a thin pass-through forwarding
`MODELS_URL` with the bearer key, returning the upstream status + body
verbatim. Both the container entrypoint's `ensure_opencode_config` and host
mode's `host_write_opencode_config` consume it at startup to build opencode's
model dropdown; a locked-key 401 (with its unlock URL) reaches the caller
unchanged. Declared as an explicit route so it wins over `catch_all`.

**State Management:**
- Per-request only inside the proxy; no cross-request session state. The
  tool-search registry is the inbound `tools` array, rebuilt every request —
  no cross-request cache to go stale.
- Local token estimation (`_estimate_tokens`, `proxy/proxy.py:1581`) gives the
  agent a monotonic context count because upstream `prompt_tokens` is
  unreliable (sliding-window truncation).
- Agent home persists across launches via the `state/agent/home/` bind mount
  (container mode). Host mode keeps proxy state under `state/host/`.

## Key Abstractions

**Cooperative-prompt mediation:**
- The upstream does not natively support tool calls, so the proxy injects a
  scaffold telling the model to emit ```json blocks of the form
  `{"name": ..., "arguments": {...}}`, then parses them back into native
  `tool_calls`.
- Modes (`PROXY_PROMPT_MODE`, resolved in `_setup_prompt_mode`):
  - `hybrid` (default) — tool definitions on the system message (folded into
    the user-role index-0 stable prefix), plus a consolidated recency block on
    the last user message with the live request first (`<<<BEGIN_USER_REQUEST>>>`),
    then an Operating/Honesty/Environment reminder and one entry per tool.
    Hybrid alone emits the `<<<BEGIN_X>>>` content-category delimiters.
    Per-tool guidance comes from the code map `_HYBRID_TOOL_GUIDANCE` (opencode
    tools) or the data map `_MCP_TOOL_RECENCY` (enabled MCPs). The Environment
    line echoes the live host CWD and host OS family.
  - `user_front` — full scaffolding on the last user message, request before
    the tool list.
  - `passthrough` — benchmark control; skips all mediation, forwards `tools`
    to upstream verbatim.
- `PROXY_PROMPT_MODE` is NOT a user `.env` knob. The proxy defaults to
  `hybrid`; `docker-compose.yml` no longer interpolates it. `harness
  start/restart --prompt-mode <mode>` injects it ephemerally via the runtime
  override.

**Tool-call extraction:**
- `extract_tool_calls_and_text` walks the response left-to-right for ```json
  fences and uses `_scan_balanced_json` (not regex) to find the matching brace,
  so argument strings containing backticks/nested fences do not truncate.
  Parsing uses `json.loads(..., strict=False)` for embedded control characters;
  misshaped calls with a known `name` but no `arguments` get a tolerant
  top-level lift. Blocks that fail to parse are left in text.

## Container Topology

`docker-compose.yml` defines two services on one bridge network (`harness-net`):

- **proxy** (`docker-compose.yml:24-97`) — builds `proxy/Dockerfile`, image
  `harness-proxy:latest`. Entrypoint runs `init-firewall.sh` then
  `python3 proxy.py`. `cap_add: NET_ADMIN, NET_RAW`, IPv6 disabled via
  `sysctls`, `restart: unless-stopped`, healthcheck on `/health`. The only
  long-running service besides enabled MCPs.
- **agent** (`docker-compose.yml:105-131`) — builds `agents/Dockerfile`, image
  `harness-agent:latest`, behind the `agent` compose profile so
  `docker compose up` skips it. Launched ephemerally by the CLI via
  `docker run`; the compose entry documents the runtime contract.
- **MCP services** — each enabled `state/mcp/<name>/compose.yml` is spliced in
  by `mcp_compose_files` (`harness:588`); snippets reference `harness_harness-net`
  as `external`.
- **firewall** — not a service; `firewall/init-firewall.sh` runs as root at the
  top of every container entrypoint, reading the bind-mounted allowlist.

The hostname `proxy` is load-bearing: opencode's container `baseURL` points at
`http://proxy:${PROXY_PORT}/v1` (`docker-compose.yml:1-7`).

## CLI Orchestration Role

- **Compose wrapper** — `compose()` (`harness:865`) is the single entry point
  for any `docker compose`/`podman compose` call. It regenerates the runtime
  override, builds the `-f` args (main compose + override + MCP snippets), and
  exports `INSTALL_ROOT`, `HARNESS_ALLOWLIST_PATH`, `HARNESS_PROJECTS_ROOT`,
  `HARNESS_HOST_OS`, `HARNESS_MCP_TOOL_RECENCY`, `HARNESS_MCP_STATE_CHECK`.
- **Runtime override** — `write_runtime_override()` (`harness:779`) regenerates
  `state/.harness-runtime.yml` per invocation (never tracked): per-service
  firewall opt-out and the ephemeral `--prompt-mode` injection.
- **Agent launch** — `run_agent`/`cmd_shell` parse agent flags
  (`--yolo`, `--net`, `--mount`, `-p/--print`) and `docker run` the agent image
  directly (not via compose).
- **Host-mode supervision** — `cmd_host`/`cmd_host_down` and the `host_*`
  helpers run the proxy + opencode as host processes (no docker).
- **Upstream gating** — `_probe_upstream_auth` (`harness:1090`) POSTs to the
  upstream before start and aborts on a locked/rejected key with the unlock URL.
- **Diagnostics** — `cmd_doctor` (`harness:5671`, read-only) and `cmd_preflight`
  (`harness:5976`, validates `.env`/allowlist/daemon).
- **Docker gate** — `require_docker` (`harness:514`) appends a "for a
  containerless run, use `harness host`" hint on an unreachable runtime.

## MCP Lifecycle

State machine (`architecture/mcp.md`):

```text
available ──install/register──▶ installed-enabled ⇄ disable/enable ⇄ installed-disabled ──uninstall──▶ available
```

- `install` copies a repo-tracked `mcp-registry/<name>/` into `state/mcp/<name>/`;
  `register` lands an arbitrary external source behind a compose-merge gate.
- `mcp_is_installed(name)` is "compose.yml exists in the active tree" — not
  directory existence — so `data/` survives uninstall.
- Enabled entries reach the runtime two ways: compose merge (`mcp_compose_files`)
  and client-config merge into `state/agent/home/.harness-mcp-servers.json`,
  folded into opencode config by the entrypoint (`merge_opencode_mcp_servers`).
- Per-MCP recency/state-check data (`recency.json`) is collected into
  `HARNESS_MCP_TOOL_RECENCY`/`HARNESS_MCP_STATE_CHECK` env vars and loaded by
  the proxy at startup.
- **Host MCPs** (`harness mcp host-init`/`host-setup`) scaffold a non-container
  build MCP from `host-mcp/template/` into `host-mcp/<name>/`; supervised as
  host processes with their own pidfile/logfile, bound to the stack lifecycle.
- `harness upgrade` refreshes registry-sourced entries via `directory_overwrite`,
  preserving `harness-meta.json` and `data/`.

## Install / Upgrade Flow

- `harness-install.sh` clones the repo (the clone IS the install root), seeds
  user config and `state/`, and symlinks `harness` into `~/.local/bin`.
- `harness update` = `git pull --ff-only` only (code refresh).
- `harness upgrade` = pull + apply `scripts/upgrade-manifest.json` (bring
  forward `state/` via action functions in `scripts/lib/upgrade_actions.sh`:
  envfile_merge / linefile_merge / directory_overwrite, anchored on B3-MANAGED
  markers) + rebuild images + restart the stack.

## Architectural Constraints

- **Threading:** The proxy is single-process Flask and does NOT stream from
  upstream — it receives the full response, then translates and emits. Chunks
  are materialized to a list before streaming.
- **Self-signed upstream:** Every upstream POST uses `verify=False`. The egress
  firewall is the trust boundary, not TLS verification.
- **System role conversion:** `_CHANGE_SYSTEM_TO_USER` default ON — the upstream
  silently drops the `system` role, so system messages fold into a leading user
  message.
- **Model passthrough:** `PROXY_API_URL` is a base, not a full endpoint;
  `_normalize_api_base` (`proxy/proxy.py:72`) strips trailing
  `/v1/chat/completions`, `/chat/completions`, or `/v1`. `DEFAULT_MODEL_NAME`
  is REQUIRED with no hardcoded default and is only the fallback.
- **CLI/proxy URL parity:** the CLI's `_api_base` must mirror the proxy's
  `_normalize_api_base` — keep the two in sync.
- **IPv4-only firewall:** containers disable kernel IPv6 at creation via
  sysctls, or IPv6 egress would be an unfiltered hole.
- **Host mode has no isolation:** no egress firewall (it lays a host-global
  iptables default-deny that cannot be scoped to one process), no
  container/user boundary; opencode runs as the full host user. Host proxy
  binds `127.0.0.1` only. This is why every host launch is confirmation-gated.

## Anti-Patterns

### Treating `PROXY_PROMPT_MODE` as a `.env` knob

**What happens:** A stale `.env` carries `PROXY_PROMPT_MODE=...` and someone
assumes it controls the proxy.
**Why it's wrong:** `docker-compose.yml` deliberately no longer interpolates it,
so the value is inert; only the runtime override
(`harness start/restart --prompt-mode`) reaches the proxy now.
**Do this instead:** Use `--prompt-mode <hybrid|user_front|passthrough>`; the
validator falls back to `hybrid` for any other value.

### Adding a fixed model id at the proxy

**What happens:** Hardcoding a model id instead of forwarding the requested one.
**Why it's wrong:** The requested model flows opencode → proxy → upstream
unchanged; that passthrough is what lets a user switch models from opencode.
**Do this instead:** Forward `model_name or DEFAULT_MODEL_NAME`; treat
`DEFAULT_MODEL_NAME` only as the no-model-specified fallback.

### Parsing tool-call JSON with regex

**What happens:** A lazy regex match on ```json fences terminates on the first
inner ``` inside an argument string.
**Why it's wrong:** It truncates JSON whose arguments contain backticks or
nested code fences.
**Do this instead:** Use `_scan_balanced_json` (`proxy/proxy.py:902`), which
tracks string boundaries and escapes.

### Expecting the firewall to confine host mode

**What happens:** Assuming `harness host` is sandboxed like a container run.
**Why it's wrong:** The egress firewall is container-bound; host mode runs with
unfiltered network as the full host user (full filesystem incl. `~/.ssh`).
**Do this instead:** Treat host mode as trusted-machine only; the mandatory
`host_confirm_gate` is the deliberate friction.

## Error Handling

**Strategy:** The proxy returns OpenAI-shaped error envelopes so the AI SDK
surfaces them to opencode; every error path writes a debug dump under
`OUTPUT_DIR`.

**Patterns:**
- Upstream request failure → 502 + `_03_API_Error.json`.
- Upstream ≥ 400 → 502 with the upstream status.
- Unhandled exception → 500 + `_99_Fatal_Error.json`.
- The CLI gates the launch on a bad/locked upstream key (`_probe_upstream_auth`)
  so a bad key never reaches the proxy.
- Host mode surfaces the proxy log tail on a failed start (the common cause is
  `_validate_config` `sys.exit(1)`).

## Cross-Cutting Concerns

**Logging:** `print(..., flush=True)` per request in `catch_all` (req id, model,
counts, catalog size, retry/rescue mode), visible via `harness logs proxy`
(container) or `state/host/proxy.log` (host). Per-request debug dumps under
`state/output/`.
**Validation:** `_validate_config` (`proxy/proxy.py:2459`) enforces the three
REQUIRED env values at startup; `cmd_preflight`/`host_require_config` validate
config before launch.
**Authentication:** `Bearer PROXY_API_KEY` on every upstream call; the egress
firewall restricts which hosts the proxy/agent can reach (container mode).

---

*Architecture analysis: 2026-06-08*
