# Technology Stack

**Analysis Date:** 2026-06-08

> harness is a container-runtime-based system: a coding agent (opencode, a
> Node CLI) talks to a custom Flask proxy that translates OpenAI-compatible
> requests into a non-Anthropic upstream chat-completions API. The whole
> stack normally runs in Docker via `docker-compose.yml`, orchestrated by the
> `harness` bash CLI. A newer **containerless `harness host` mode** runs the
> proxy + opencode as plain host processes (no docker, no firewall).

## Languages

**Primary:**
- **Bash** — the orchestration layer. `harness` CLI is ~6,500 lines
  (`harness`, 274KB), plus `agents/entrypoint.sh`, `proxy/entrypoint.sh`,
  `firewall/init-firewall.sh`, `harness-install.sh`, and `scripts/lib/*.sh`.
  `set -euo pipefail` throughout (`harness:27`).
- **Python 3.12** — the translating proxy. `proxy/proxy.py` (~2,180 lines,
  Flask app). Container base is `python:3.12-slim` (`proxy/Dockerfile:1`).
  Host mode requires only `python3` (any 3.x with `venv`); verified by
  `host_preflight` (`harness:2650`).

**Secondary:**
- **JavaScript / Node.js 20** — the agent runtime is the `opencode` CLI,
  installed via npm in the agent image (`agents/Dockerfile:10` =
  `node:20-bookworm-slim`). Host mode requires Node.js >= 20 on the host PATH
  (`harness:2661-2673`). No first-party JS source — opencode is consumed as a
  pinned npm package.
- **Python 3 (host MCP SDK)** — `host-mcp/template/server.py` uses the MCP
  Python SDK (`host-mcp/template/requirements.txt`: `mcp>=1.27`). Separate
  from the proxy's deps.
- **Dockerfile / Compose YAML / JSON** — image builds, service composition,
  and config (opencode provider config, MCP client-config, recency metadata).

## Runtime

**Two runtime paths exist:**

1. **Container mode (default).** Services run as Docker containers on a
   user-defined bridge network (`harness-net`), orchestrated by
   `docker compose`. Container runtime is **docker or podman** — auto-detected
   (docker first, then podman) unless pinned via `HARNESS_CONTAINER_RUNTIME`
   (`.env.example:189-197`, `harness:529-530`). Podman support targets Linux +
   rootless podman 4.0+ with the built-in `podman compose` (`.env.example:194`).
2. **Host mode (`harness host`, containerless).** No docker, no containers,
   no egress firewall. The proxy runs from a lazily-built Python venv at
   `state/host/venv` (created on first launch from `proxy/requirements.txt`,
   `harness:2748-2778`); opencode runs directly off the host PATH. Gated
   behind a mandatory per-launch confirmation (`host_confirm_gate`,
   `harness:2710`) because it runs as the full host user with unrestricted
   egress. Deliberately minimal: Linux/macOS only, single CWD, no host-MCP
   wiring (`harness:2614-2616`).

**Package managers:**
- **pip** — proxy Python deps (`proxy/Dockerfile:23`; host venv via
  `pip install -r proxy/requirements.txt`, `harness:2772`).
- **npm** — global install of `opencode-ai` + `@ai-sdk/openai-compatible` in
  the agent image (`agents/Dockerfile:106-108`).
- **pipx / apt** — inside the agent image for user-installed tools
  (`agents/Dockerfile:52`).
- **git** — `harness update`/`upgrade`/`mcp install` use git; the install
  root IS the git clone (`harness:11-12`).
- Lockfiles: none. Python deps are exact-pinned in `proxy/requirements.txt`;
  npm packages pinned via Dockerfile ARGs (no `package.json`/lockfile in repo).

## Frameworks

**Core (proxy):**
- **Flask 3.0.3** — HTTP server exposing the OpenAI-compatible endpoint
  (`proxy/requirements.txt:1`; `Flask(__name__)` at `proxy/proxy.py:2122`).
  Runs on Flask's built-in WSGI dev server (`app.run`); no gunicorn/uwsgi.
- **requests 2.32.3** — upstream HTTP client (`proxy/requirements.txt:2`).
  Note `verify=False` — upstream uses a self-signed cert
  (`proxy/proxy.py:46-50`). *Verified.*

**Agent:**
- **opencode** (`opencode-ai`) — the coding agent CLI. Pinned
  `OPENCODE_VERSION=1.15.7` (`agents/Dockerfile:103`).
- **@ai-sdk/openai-compatible** — opencode's provider adapter that speaks to
  the proxy. Pinned `OPENAI_COMPAT_VERSION=2.0.47` (`agents/Dockerfile:104`).

**Host MCP (optional, separate):**
- **mcp** Python SDK `>=1.27` — `FastMCP` + streamable-http transport for the
  host build MCP template (`host-mcp/template/requirements.txt`).

**Testing:**
- **pytest** — proxy unit tests (`proxy/test_proxy.py`; `.pytest_cache/`
  present). Bash test harness under `tests/` driven by `harness test`.
  (See TESTING.md — out of scope for this doc.)

## Key Dependencies

**Critical (proxy, exact-pinned):**
- `flask==3.0.3` — HTTP framework.
- `requests==2.32.3` — upstream client.
- These two are the ONLY Python runtime deps; the host-mode venv installs
  exactly these (`harness:2771`).

**Critical (agent, ARG-pinned in image):**
- `opencode-ai@1.15.7` — agent CLI. Version is load-bearing: opencode's
  headless-stdout render race and tool-description parsing are documented and
  worked around per-version (`agents/Dockerfile:70-102`). Bumping requires
  re-verifying `full_pipeline_test.sh` T9/T10 and the proxy's `task`-tool
  parsing canary.
- `@ai-sdk/openai-compatible@2.0.47` — provider adapter.

**Infrastructure (agent image, apt):**
- `git`, `curl`, `ca-certificates`, `procps`, `gosu` (uid/gid remap + privilege
  drop), `jq` (config merge + firewall), `python3`/`python3-venv`/`pipx`/
  `build-essential`/`python3-dev` (in-container tool installs that compile
  native extensions), `iptables`/`ipset`/`dnsutils`/`iproute2` (firewall),
  `locales` (C.UTF-8 for opencode TUI glyphs). `agents/Dockerfile:40-59`.
- Proxy image apt set is narrower: `curl`, `ca-certificates`,
  `iptables`/`ipset`/`dnsutils`/`iproute2`, `jq` (`proxy/Dockerfile:11-20`).

## Configuration

**Environment:**
- Single `.env` at the install root (= the git clone). Documented in full by
  `.env.example` (`.env.example:1-7`). `docker compose --env-file ./.env`.
- **REQUIRED** vars (both runtimes): `PROXY_API_URL`, `PROXY_API_KEY`,
  `DEFAULT_MODEL_NAME` (`.env.example:27,30,39`; host-mode validates the same
  three in `host_require_config`, `harness:2695`).
- Notable optional vars: `PROXY_PORT` (default 8000), `PROXY_TIMEOUT` (180s),
  `MODEL_CONTEXT_LENGTH` (200000, legacy alias `OLLAMA_CONTEXT_LENGTH`),
  `OUTPUT_DIR` (debug dumps, off by default), `HARNESS_CONTAINER_RUNTIME`,
  `HARNESS_EXTRA_MOUNTS`, `HARNESS_PROJECTS_ROOT`, `HTTP_PROXY`/`HTTPS_PROXY`
  (host-side only — never injected into containers, `.env.example:163-167`).
- Prompt-injection mode (`hybrid` default / `user_front` / `passthrough`) is
  NOT a `.env` knob; it is an ephemeral per-launch flag `--prompt-mode`,
  injected as `PROXY_PROMPT_MODE` (`.env.example:44-52`,
  `proxy/proxy.py:344-359`).

**Build:**
- `docker-compose.yml` (root) — proxy + agent services.
- `proxy/Dockerfile`, `agents/Dockerfile` — both use repo root as build
  context so they can COPY `firewall/` scripts.
- MCP compose snippets merged via extra `-f` args
  (`mcp-registry/serena/compose.yml`).

## Platform Requirements

**Development / runtime (container mode):**
- A container runtime: Docker, or rootless podman 4.0+ on Linux.
- `jq`, `git` on the host (CLI orchestration). Disk for multi-GB images
  (e.g. Serena image ~2GB, `mcp-registry/serena/compose.yml`).
- Cross-platform host: Linux, macOS, Windows Git Bash (the CLI uses a portable
  `realpath` resolver and handles MSYS path translation, `harness:43-51`).

**Runtime (host mode):**
- Linux/macOS only (`harness:2615`).
- `python3` (+ `venv`), `jq`, Node.js >= 20, and `opencode` on PATH
  (`host_preflight`, `harness:2647-2682`). No docker.

**Production:**
- Self-hosted; no managed PaaS. The install root is a git clone updated in
  place via `harness update`/`upgrade`.

---

*Stack analysis: 2026-06-08*
