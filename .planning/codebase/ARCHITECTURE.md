<!-- refreshed: 2026-06-10, last_mapped_commit: 4ebbb2c -->

# Architecture

**Analysis Date:** 2026-06-10

## System Overview

Harness is a translating proxy plus an opinionated `opencode` coding agent, orchestrated by one bash CLI. The agent talks to a long-running Flask proxy over an OpenAI-compatible Chat Completions interface; the proxy translates to/from a third-party upstream chat-completions API (self-signed cert) and injects cooperative tool-use prompts so a non-tool-native upstream can still produce tool calls.

There are **two runtime paths** for the same proxy↔opencode wiring:

1. **Container mode (normal):** `harness` launches ephemeral agent containers via direct `docker run`; the proxy (and any enabled MCP) run as long-lived compose services. A universal egress firewall and container/user isolation apply. Components reach each other by service name on the `harness-net` bridge.
2. **Host mode (`harness host`, containerless):** the proxy and opencode run as plain **host processes** — no docker, no images, no firewall, no isolation. Lighter footprint, strictly bigger blast radius, so **every launch is gated behind a mandatory confirmation**.

```text
┌──────────────────────────────────────────────────────────────────────┐
│  harness CLI (bash, ~7421 lines)  — self-locate, env, docker run /     │
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

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| **harness CLI** | Lifecycle management, agent launch, config loading, docker compose orchestration | `harness` |
| **proxy.py** | OpenAI-compat endpoint, upstream translation, tool-call parsing, recency injection | `proxy/proxy.py` |
| **Agent entrypoint** | Firewall setup, UID remap, gosu drop, opencode config, mode dispatch | `agents/entrypoint.sh` |
| **init-firewall.sh** | Egress firewall rules (iptables/ipset), allowlist enforcement | `firewall/init-firewall.sh` |
| **MCP registry** | Vetted long-running services (containers or host processes) | `mcp-registry/` |
| **docker-compose.yml** | Service definitions, network, mounts, env passthrough | `docker-compose.yml` |

## Pattern Overview

**Overall:** Layered translation + cooperative-prompt injection.

**Key Characteristics:**
- **Translation layer:** Agent speaks OpenAI-compatible format; upstream may use different schema. Proxy normalizes bidirectionally.
- **Cooperative tool injection:** Upstream doesn't natively support tool calls. Proxy injects markdown-like scaffold prompts (three modes: `user_front`, `hybrid`, `passthrough`), then parses ```json blocks from upstream response back into OpenAI `tool_calls`.
- **Single-file services:** Proxy is one Flask app (`proxy/proxy.py`, ~2558 lines). Agent entrypoint is one bash script (`agents/entrypoint.sh`, ~372 lines). CLI is one bash script (`harness`, ~7421 lines). Simplicity over modularity.
- **Container-based isolation:** Agents are ephemeral; proxy + MCPs are long-running on a shared bridge network. Firewall is per-container egress enforcement.
- **Host mode (containerless):** Mirroring container path but as host processes — proxy venv, opencode+deps vendored in `state/host/toolchain/`, no firewall (defense-in-depth via loopback binding).

## Layers

**CLI Layer (`harness`):**
- Purpose: User-facing command dispatcher, config loading, docker/podman orchestration, agent launch wrapper
- Location: `harness` (~7421 lines of bash)
- Contains: Subcommand functions (`cmd_start`, `cmd_opencode`, `cmd_shell`, etc.), lifecycle helpers, compose wrapper, runtime override generation
- Depends on: `scripts/lib/` (platform, net, upgrade helpers), `.env`, `docker-compose.yml`, `scripts/upgrade-manifest.json`
- Used by: End users, CI test harness, orchestrator scripts

**Proxy Layer (`proxy/proxy.py`):**
- Purpose: OpenAI-compatible chat endpoint, upstream API translation, tool-call cooperative injection + parsing, recency/state-check/meta-tool serving
- Location: `proxy/proxy.py` (~2558 lines of Python)
- Contains: Flask app, request/response handlers, prompt builders (modes), tool extraction, SSE/JSON emission, debug logging
- Depends on: `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`, `PROXY_HOST`, `PROXY_PORT`, `HARNESS_HOST_OS`, `HARNESS_MCP_TOOL_RECENCY`, `HARNESS_MCP_STATE_CHECK` (all via env)
- Used by: Agent (opencode) via HTTP, MCP services (as a network dependency), CLI (health checks, model pulls)

**Agent Layer (`agents/`):**
- Purpose: Container image + entrypoint; runs opencode or shell, manages firewall, UID remap, opencode config
- Location: `agents/` directory
- Contains: `Dockerfile` (image build, opencode + Node + jq pinned), `entrypoint.sh` (~372 lines, privilege drop + mode dispatch), helper scripts (git creds, clipboard bridge)
- Depends on: `firewall/init-firewall.sh`, `HARNESS_HOST_CWD`, `DEFAULT_MODEL_NAME`, `HARNESS_FIREWALL_DISABLED`, `HOST_UID`, `HOST_GID`
- Used by: `harness opencode`, `harness shell`, `harness -p` (print mode)

**Firewall Layer (`firewall/`):**
- Purpose: Container egress enforcement via iptables + ipset; allowlist-based filtering
- Location: `firewall/` directory
- Contains: `init-firewall.sh` (~329 lines, iptables/ipset rules), `configure-git-credentials.sh` (git-push credential helper), `clipboard-bridge.sh` (OSC 52 bridge)
- Depends on: `.harness-allowlist` (mount at `/etc/harness/allowlist`), DNS resolution, `dig` binary
- Used by: Agent + proxy entrypoints (called before privilege drop)

**MCP Registry (`mcp-registry/`):**
- Purpose: Vetted long-running service definitions (mostly containers, but also host MCPs)
- Location: `mcp-registry/` directory
- Contains: Per-service subdirs (`serena/` is the reference); each has `compose.yml`, `client-config.json`, `harness-meta.json.template`, `recency.json`, `README.md`
- Depends on: `docker-compose.yml` network merge (`harness_harness-net` as external)
- Used by: `harness mcp install`, `harness mcp register`, agent config merge

**State Directory (`state/`):**
- Purpose: Runtime state (output dumps, persistent agent home, active MCP state, host-mode toolchain/venv)
- Location: `state/` directory (gitignored)
- Contains: `output/` (proxy debug dumps), `agent/home/` (bind-mounted into every agent, persistent), `mcp/<name>/` (active MCP entries, enabled/disabled flags), `host/` (host-mode proxy venv + toolchain + pids)

## Data Flow

### Primary Request Path (Agent → Upstream → Agent)

1. **Client request** (`opencode` → proxy)
   - Agent: POST `http://proxy:8000/v1/chat/completions`
   - Body: `{model, messages, tools, stream: true}`
   - Initiated by opencode's `@ai-sdk/openai-compatible` provider

2. **Proxy ingress** (`catch_all()` in `proxy/proxy.py`)
   - Parse JSON: extract `model`, `messages`, `tools`, `stream`
   - Flatten any multimodal `content` arrays to strings
   - Format tools to text: `_format_tools_to_text(tools)` → markdown list

3. **History translation & prompt injection** (`translate_history_and_apply_prompt()`)
   - Flatten messages to role-alternating array (upstream may not handle role lists)
   - Convert system message to user (upstream drops system role silently)
   - Inject cooperative tool-use scaffold (mode-dependent: `user_front`, `hybrid`, `passthrough`)
   - Hybrid mode: tool defs to system/stable-prefix, recency (live user request + reminder + per-tool entries) to last turn

4. **Upstream POST**
   - Endpoint: `{PROXY_API_BASE}/v1/chat/completions`
   - Auth: `Bearer {PROXY_API_KEY}`
   - Timeout: `PROXY_TIMEOUT` seconds (default 180)
   - Verify SSL: `False` (upstream may use self-signed cert)

5. **Upstream response**
   - Shape: OpenAI-like `{choices: [{message: {content: str, tool_calls: [...]}}], usage: {...}}`
   - May include ```json fenced tool-call blocks in `content` instead of structured `tool_calls`

6. **Tool-call extraction** (`extract_tool_calls_and_text()`)
   - Walk response left-to-right for ```json fences
   - Use balanced-brace scanner (NOT regex) to find matching `}`
   - Parse `{name, arguments}` objects; left-in-text policy for failures
   - Tolerant lift: if `arguments` missing but `name` is known tool, lift top-level keys

7. **Response assembly** (`generate_openai_sse()` or `build_openai_response()`)
   - When `stream: true` (always, for opencode): emit SSE chunks
   - When `stream: false`: single `chat.completion` JSON object

8. **Agent processing** (opencode renderer)
   - Parses SSE chunks into text + tool calls
   - Renders TUI
   - Invokes tool calls, collects results
   - Streams results back to proxy as tool-result turn

### Container Mode: Egress Firewall Path

1. **Container startup** (`docker run` from `harness` CLI)
   - Image: `harness-agent:latest` (built from `agents/Dockerfile`)
   - User: `--user 0:0` (root; dropped in entrypoint)
   - Mounts: CWD + `/home/harness/` + allowlist (read-only) + extras
   - Env: `HARNESS_HOST_CWD`, `HOST_UID`, `HOST_GID`, `HARNESS_FIREWALL_DISABLED`, plus proxy/model config

2. **Privilege drop + firewall** (entrypoint, still root)
   - Call `init-firewall.sh` (unless `HARNESS_FIREWALL_DISABLED=1`)
   - Lays IPv4-only iptables rules: default DROP on output, ALLOW list (DNS, `PROXY_API_URL` host, allowlist entries)
   - Disables IPv6 (kernel-level via sysctl) to close v6 egress hole

3. **UID remap** (entrypoint, still root)
   - If `HOST_UID`/`HOST_GID` set: `usermod -u HOST_UID` + `groupmod -g HOST_GID` + `chown -R`

4. **gosu drop** (entrypoint, still root)
   - `exec gosu harness "$0" "$@"` — privilege drop to unprivileged `harness` user

5. **User-side init** (entrypoint, now harness user)
   - Git credentials: `configure-git-credentials.sh` sets `credential.helper`
   - Skel seed: copy `/etc/skel/harness/.*` to `$HOME` on first run
   - cd to host CWD

6. **Mode dispatch** (entrypoint, harness user)
   - `mode="${1:-opencode}"`; call `run_opencode` or drop to `bash`

7. **Outbound requests** (from agent, opencode, or proxy)
   - All egress constrained by firewall

### Host Mode: No Firewall, Direct Process Path

1. **Host launch** (`harness host`)
   - Skips docker entirely; runs proxy + opencode as host processes

2. **Config gate** (`host_require_config`)
   - Enforces `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`
   - NO firewall allowlist (host mode has no egress confinement)

3. **Confirmation gate** (`host_confirm_gate`)
   - MANDATORY on every launch (unlike `--net` per-invocation flag)
   - Warns: no firewall, full host user, full filesystem access

4. **Toolchain provision** (`host_ensure_toolchain`)
   - Provisions `jq`, Node >= 20, `opencode` into `state/host/toolchain/`

5. **Auth gate + catalog** (`_gate_on_upstream_auth`, `_print_upstream_models`)
   - Same checks as container mode

6. **Proxy venv** (`host_proxy_start`)
   - Lazily builds venv under `state/host/venv`
   - `nohup` proxy/proxy.py with loopback-only binding (`PROXY_HOST=127.0.0.1`)

7. **OpenCode config** (`host_write_opencode_config`)
   - Scoped to `state/host/opencode.json` (not user's global `~/.config/opencode/`)
   - Provider baseURL: `http://127.0.0.1:<port>/v1` (loopback to host proxy)

8. **Launch + proxy management** (`host_run_opencode`)
   - Interactive: run as child (not `exec`) so proxy is torn down on exit
   - Print (`-p`): run as child, proxy stays up

9. **Corp proxy routing** (Verified from recent commits)
   - On a host behind corp proxy:
     - **Proxy's upstream hop** uses corp proxy: `requests` inherits shell env → upstream reachable
     - **opencode's loopback hop** bypasses corp proxy: `NO_PROXY=127.0.0.1,localhost,::1` → loopback call goes direct
   - Why the split: host has no direct internet; upstream requires proxy. But loopback (the local proxy) must bypass or the corp proxy would try to tunnel it, resulting in 504.

## Key Abstractions

**Cooperative Tool Injection:**
- Purpose: Upstream has no native tool-support; proxy injects scaffold to make models emit ```json blocks, then parses them back.
- Pattern: Three modes (user_front, hybrid, passthrough) + tool-call extraction pipeline. Hybrid is default.

**MCP Tool Recency Injection:**
- Purpose: Each turn's recency block includes one-line per-tool guidance (so model doesn't re-reference full 10KB schema on every turn).
- Pattern: Opencode tools → hardcoded map; MCP tools → data from `recency.json` (owned by each MCP). Proxy loads JSON from `HARNESS_MCP_TOOL_RECENCY` env at startup.

**State-Check Marker (Orient-First Rule):**
- Purpose: Flag state-mutating tools; model checks state before calling them.
- Pattern: Tool's `recency.json` value is either string (guidance) or `{line, state_check: true}`. Proxy renders marker + conditional reminder.

**Empty-Response Rescue:**
- Purpose: Upstream silently short-circuits on certain recency-message content. Rescue with no-op tool call to displace trigger content.
- Pattern: Detect `clean_text == ""` AND `tool_call_payloads == []`; inject rescue tool call (or text fallback).

**Malformed Tool-Call Retry:**
- Purpose: Recover from model producing invalid ```json fence or unescaped `\` sequences.
- Pattern: Conservative detector appends corrective `[assistant, user]` pair and re-POSTs once.

**Allowlist + Firewall Enforcement:**
- Purpose: Container-bound egress confinement; agents cannot call arbitrary hosts.
- Pattern: Firewall reads `/etc/harness/allowlist` at startup, builds ipset + iptables rules (default-DROP).

## Entry Points

**CLI Entry** (`harness`):
- Location: `harness` (line 1+)
- Triggers: User runs `harness`, `harness opencode`, `harness shell`, `harness start`, etc.
- Responsibilities: Self-locate install root, load `.env`, dispatch subcommand, manage services, launch agents

**Proxy Entry** (`proxy/proxy.py`):
- Location: `proxy/proxy.py:main()` at module load / startup
- Triggers: Container startup (entrypoint calls `python3 proxy.py`), `docker compose restart proxy`, host mode (`nohup python3 proxy.py`)
- Responsibilities: Flask app startup, config validation, request routing, upstream calls

**Agent Entry** (`agents/entrypoint.sh`):
- Location: `agents/entrypoint.sh` (line 1+)
- Triggers: Container startup (defined in `agents/Dockerfile`)
- Responsibilities: Firewall, UID remap, gosu drop, mode dispatch (opencode or shell)

## Architectural Constraints

- **Single proxy instance per install:** One long-running Flask app binds to one port. All agents on the network route through it.
- **Shared home directory:** Every agent uses the same bind-mounted `state/agent/home/`. Persistent home state survives container rebuilds.
- **IPv4-only firewall:** Kernel IPv6 disabled in every container; firewall rules are `iptables` + `ipset` (IPv4). No IPv6 egress.
- **No streaming from upstream:** Proxy fetches full response before emitting SSE to client. Latency is upstream round-trip, not streaming latency.
- **System-role conversion:** Upstream silently drops system messages, so proxy converts `messages[0]` (system) to user role. Default ON.
- **Tool-call format:** Upstream doesn't natively support OpenAI `tool_calls` field, so proxy injects markdown scaffold + parses ```json blocks. Fallback to left-in-text if parsing fails.
- **No cross-request state in proxy:** Proxy is stateless per-request (except module-level config at startup). MCP schema freshness is per-request.
- **Host mode has no firewall:** Firewall is per-container via iptables + network namespace. Host-mode proxy binds loopback-only as defense-in-depth; host processes run unfiltered (explicit tradeoff, gated behind confirmation).

## Anti-Patterns

### Upstream System-Prompt Expectation

**What happens:** Code assumes upstream honors `role: system` messages. Proxy sends system message in a translated history and upstream silently ignores it.

**Why it's wrong:** Silent failures are hard to debug; the system message (critical context like "you are a code editor agent") disappears from the effective prompt.

**Do this instead:** Always convert system messages to user role **at translation time** (`_CHANGE_SYSTEM_TO_USER` in `proxy/proxy.py:translate_history_and_apply_prompt()`). The default is ON; if an upstream truly honors system messages, the code constant can be disabled, but **never assume** — verify with the upstream first.

**File:** `proxy/proxy.py:_CHANGE_SYSTEM_TO_USER`, `proxy/proxy.py:translate_history_and_apply_prompt()`

### Trusting Upstream Token Counts

**What happens:** Proxy uses `prompt_tokens` from upstream and agent's context bar drifts as conversation grows.

**Why it's wrong:** Observed upstream behavior (count not monotonic, occasional shrinkage) suggests server-side sliding-window truncation. Agent's context bar becomes unreliable.

**Do this instead:** Estimate tokens locally (`_estimate_tokens()`; uses `len(text) // 3` for agent-heavy code + tool calls). Source of truth is the translated history the proxy actually sent, not the upstream's bookkeeping.

**File:** `proxy/proxy.py:_estimate_tokens()`, `proxy/proxy.py:catch_all()`

### Regex-Based Tool-Call Extraction

**What happens:** Use regex with lookahead/lookbehind to find ```json ... ``` blocks. Regex fails when tool-call strings contain embedded backticks or newlines.

**Why it's wrong:** Models emit arguments like `python -c "..."` with real newline bytes (not `\\n` escapes) or backticks in the JSON. Lazy regex terminates on the first inner ``` and truncates the call.

**Do this instead:** Use balanced-brace scanning (`_scan_balanced_json()`) that tracks string boundaries and backslash escapes. Tolerant JSON parsing (`json.loads(..., strict=False)`) accepts unescaped control bytes.

**File:** `proxy/proxy.py:extract_tool_calls_and_text()`, `proxy/proxy.py:_scan_balanced_json()`

---

*Architecture analysis: 2026-06-10*
