# Technology Stack

**Analysis Date:** 2026-06-02

## Languages

**Primary:**
- Python 3.12 - the translating proxy. Image base is `python:3.12-slim` (`proxy/Dockerfile:1`). The single source file is `proxy/proxy.py`.
- Bash - the `harness` management CLI (one ~4200-line script, `harness`), the agent and proxy container entrypoints (`agents/entrypoint.sh`, `proxy/entrypoint.sh`), the egress firewall (`firewall/init-firewall.sh`), the installer (`harness-install.sh`), and the shared libraries under `scripts/lib/` (`platform.sh`, `net_helpers.sh`, `upgrade_actions.sh`).

**Secondary:**
- TypeScript/JavaScript - not authored here, but pulled in at runtime as the opencode coding agent and its provider package (npm-installed into the agent image, `agents/Dockerfile:106-109`). Node 20 is the agent image base (`agents/Dockerfile:10`).

There is no Go, Rust, or compiled component in this repo. Ollama has been fully removed on this branch: there is no `ollama/` directory and no ollama service. The only surviving ollama token is the env var `OLLAMA_CONTEXT_LENGTH`, kept deliberately as a legacy read-alias for `MODEL_CONTEXT_LENGTH` (`proxy/proxy.py:63-67`, `.env.example:67-69`).

## Runtime

**Environment:**
- Container runtime: Docker or Podman. Auto-detected (docker first, then podman) unless pinned via `HARNESS_CONTAINER_RUNTIME` (`.env.example:189-197`, `scripts/lib/platform.sh:83-129`). Podman support targets Linux + rootless podman 4.0+ with the built-in `podman compose` subcommand (`.env.example:192-194`).
- Proxy process: Python 3.12 running Flask's built-in development server. `proxy.py`'s `main()` ends in `app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)` (`proxy/proxy.py:1866`) — no gunicorn/uvicorn/waitress WSGI server is used. Single-process, single-threaded. The proxy never streams from upstream: it completes the upstream call fully, then translates and emits (`architecture/proxy.md:479-485`).
- Agent process: the opencode binary (TypeScript CLI) running inside the Node 20 agent container, launched per-invocation by `harness` via `docker run` (not compose).

**Package Manager:**
- Python deps: `pip` installing a pinned `proxy/requirements.txt` (`proxy/Dockerfile:22-23`).
- Agent deps: `npm install -g` with exact-version pins (`agents/Dockerfile:106-109`).
- No host-level package manifest (no `package.json`, `pyproject.toml`, or root `requirements.txt`) — the repo IS the install root and ships shell + Python source directly.

## Frameworks

**Core:**
- Flask 3.0.3 (`proxy/requirements.txt:1`) - the proxy's HTTP framework. Exposes the OpenAI-compatible surface: `POST /v1/chat/completions` (catch-all route handler `catch_all`), `GET /v1/models` (explicit pass-through route), and `GET /health`. The Flask app object is created at `proxy/proxy.py:1588`.
- requests 2.32.3 (`proxy/requirements.txt:2`) - the proxy's HTTP client for calling the upstream Gemini Enterprise gateway. Calls use `verify=False` because the upstream presents a self-signed cert (`architecture/proxy.md:38-39`).

**Agent runtime (npm, pinned in `agents/Dockerfile`):**
- opencode (`opencode-ai`) 1.15.7 - the coding agent CLI (`agents/Dockerfile:103`, `:106-109`). Self-update is disabled at launch (`OPENCODE_DISABLE_AUTOUPDATE=1`); the version is managed solely by this pin (`architecture/containers.md:95-97`).
- `@ai-sdk/openai-compatible` 2.0.47 - the AI SDK provider opencode uses to speak the proxy's OpenAI-compatible interface, pointed at `http://proxy:${PROXY_PORT}/v1` (`agents/Dockerfile:104`, `:106-109`; `architecture/proxy.md:9-12`).

**Testing:**
- Python unit tests: `unittest` (stdlib), in `proxy/test_proxy.py`, run inside the proxy container (`architecture/proxy.md:581-587`).
- Shell/integration tests: bash test scripts under `tests/` (e.g. `tests/proxy_test.sh`, `tests/harness_test.sh`, `tests/full_pipeline_test.sh`, `tests/integration_test.sh`), discovered and run via `harness test [section]` (`harness:1251`). No bats framework — tests are plain bash with `shellcheck` directives.
- Static lint guard: `scripts/check_runtime_calls.sh` (guards against raw `docker` call sites bypassing the runtime wrapper) plus advisory `shellcheck` (project `CLAUDE.md` local-testing section).

**Build/Dev:**
- Docker Compose - service composition (`docker-compose.yml`); the proxy and any enabled MCP run as compose services, agents launch via direct `docker run`.
- `jq` - config/JSON manipulation in the CLI, entrypoints, and firewall scripts. Shipped in both images; the CLI has a container-sidecar fallback (`harness_jq`) when host `jq` is absent (`architecture/harness-cli.md:260-293`).

## Key Dependencies

**Critical:**
- Flask 3.0.3 (`proxy/requirements.txt:1`) - if this changes, the route surface and SSE emission (`generate_openai_sse`) must be re-verified.
- requests 2.32.3 (`proxy/requirements.txt:2`) - the only upstream HTTP path.
- opencode-ai 1.15.7 (`agents/Dockerfile:103`) - version-coupled to harness in two load-bearing ways documented in the Dockerfile: (1) the headless `-p` stdout-recovery path depends on `opencode export` / `opencode session list --format json` shapes, and (2) the proxy's `task`-description paring anchors on opencode's `Available agent types and the tools they have access to:` header. Bumping requires re-verifying both against the new binary (`agents/Dockerfile:70-102`).
- `@ai-sdk/openai-compatible` 2.0.47 (`agents/Dockerfile:104`) - the provider that makes opencode talk to the proxy; its streaming-accumulation contract drives how the proxy frames tool-call deltas.

**Infrastructure (apt, installed in the images):**
- Agent image (`agents/Dockerfile:40-59`): `git`, `curl`, `ca-certificates`, `procps`, `gosu` (privilege drop), `jq`, `python3` + `python3-pip` + `python3-venv` + `python3-dev` + `pipx` (lets users `pipx install` tools into the bind-mounted home), `build-essential` (C toolchain for native Python extension builds), `iptables`, `ipset`, `dnsutils`, `iproute2` (firewall), `locales` (C.UTF-8 for TUI glyphs).
- Proxy image (`proxy/Dockerfile:11-20`): `curl` (healthcheck), `ca-certificates`, `iptables`, `ipset`, `dnsutils`, `iproute2`, `jq` (all for the firewall the proxy entrypoint runs).

## Configuration

**Environment:**
- Single `.env` at the install root (the install root IS the git clone). Documented exhaustively in `.env.example`. `docker compose` receives it via `--env-file`; the `harness` CLI sources it separately for its own logic (`architecture/harness-cli.md:27-32`).
- Required vars: `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME` (`.env.example:19-39`; `proxy/proxy.py:544` documents the REQUIRED set; `_validate_config` exits if any is empty — `architecture/proxy.md:539-553`).
- Notable optional vars: `PROXY_PORT` (default 8000), `PROXY_TIMEOUT` (default 180), `MODEL_CONTEXT_LENGTH` (default 200000, legacy alias `OLLAMA_CONTEXT_LENGTH`), `OUTPUT_DIR` (debug dumps, off by default), `HARNESS_HOST_OS` (set by the CLI), `HARNESS_EXTRA_MOUNTS`, `HARNESS_CONTAINER_RUNTIME`, `HTTP_PROXY`/`HTTPS_PROXY` (host-side only, never injected into running containers — `.env.example:150-183`).
- `PROXY_PROMPT_MODE` is deliberately NOT a `.env` knob: the proxy defaults to `hybrid` and `docker-compose.yml` no longer interpolates it (`docker-compose.yml:43-47`, `architecture/proxy.md:82-90`). It is only set ephemerally via `harness start/restart --prompt-mode <mode>`.

**Build:**
- `docker-compose.yml` - two services (`proxy`, `agent`); the `agent` service sits behind the `agent` compose profile so `docker compose up` leaves it alone (`docker-compose.yml:88-111`).
- `proxy/Dockerfile`, `agents/Dockerfile` - both use the repo root as build context so they can `COPY firewall/` scripts.
- `state/.harness-runtime.yml` - a runtime compose override regenerated on every compose call by `write_runtime_override`; never tracked (`architecture/harness-cli.md:128-156`).

## Platform Requirements

**Development:**
- A container runtime (Docker or Podman) reachable via `docker info` (`architecture/install-and-upgrade.md:11-16`).
- Bash. On Windows, Git Bash (MSYS) is a first-class target: the CLI handles MSYS path rewriting, Windows TTY/ConPTY/winpty resolution, and `//`-prefixed working-dir escapes (`architecture/harness-cli.md:179-229`).
- OS-detection layer: `harness_detect_os` in `scripts/lib/platform.sh:24-31` resolves `linux` / `macos` / `windows` / `unknown` off `uname -s`. The CLI exports `HARNESS_HOST_OS` into the proxy's compose env so the proxy can give host-appropriate setup advice (`architecture/harness-cli.md:113-119`).

**Production:**
- Same as development. There is no separate deploy target: harness runs locally on a laptop/host. The clone IS the install root; `harness update` / `harness upgrade` move it forward via `git pull --ff-only` (`architecture/README.md:27-32`, `architecture/install-and-upgrade.md`).
- IPv4-only egress: every container is created with the kernel IPv6 stack disabled via the `net.ipv6.conf.all.disable_ipv6=1` sysctl (`docker-compose.yml:70-71`), because the firewall is IPv4-only.

---

*Stack analysis: 2026-06-02*
