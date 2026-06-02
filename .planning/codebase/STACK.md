# Technology Stack

**Analysis Date:** 2026-06-02

## Languages

**Primary:**
- Bash — `harness` CLI script, `harness-install.sh`, all container entrypoints (`agents/entrypoint.sh`, `ollama/entrypoint.sh`, `proxy/entrypoint.sh`), firewall scripts (`firewall/init-firewall.sh`, `firewall/configure-git-credentials.sh`), all test scripts under `tests/`
- Python 3.12 — translating proxy (`proxy/proxy.py`), test suite (`proxy/test_proxy.py`), mock API (`tests/benchmarks/mock-api/app.py`), benchmark adapters (`tests/benchmarks/adapters/`)

**Secondary:**
- TOML — benchmark task definitions (`tests/benchmarks/tasks/hello-harness/task.toml`), benchmark adapter build (`tests/benchmarks/adapters/harness_opencode/pyproject.toml`)
- JSON — MCP client config (`mcp-registry/serena/client-config.json`), harness-meta templates (`mcp-registry/serena/harness-meta.json.template`), upgrade manifest (`scripts/upgrade-manifest.json`), benchmark scheme definitions (`tests/benchmarks/schemes/`)
- YAML — service composition (`docker-compose.yml`, `mcp-registry/serena/compose.yml`, `tests/benchmarks/mock-api/docker-compose.mock.yml`)

## Runtime

**Environment:**
- Docker (primary) or Podman 4.0+ rootless (Linux only) — container runtime required for all services. Auto-detected by harness CLI; overridable via `HARNESS_CONTAINER_RUNTIME` in `.env`. See `scripts/lib/platform.sh`.
- Node.js 20 — base image for the agent container (`agents/Dockerfile`: `FROM node:20-bookworm-slim`)
- Python 3.12 — base image for the proxy container (`proxy/Dockerfile`: `FROM python:3.12-slim`)
- ollama base image tag pinned to `0.21.2` (`docker-compose.yml` `OLLAMA_VERSION` arg, default confirmed in `.env.example`)

**Package Manager:**
- npm — installs `opencode-ai` and `@ai-sdk/openai-compatible` globally in the agent image (`agents/Dockerfile`)
- pip — installs `flask==3.0.3` and `requests==2.32.3` in the proxy image (`proxy/requirements.txt`)
- pipx + pip + python3-venv — available inside the agent container for user skill installers (`agents/Dockerfile`)
- Lockfile: none committed (pip uses pinned `==` versions in `proxy/requirements.txt`; npm uses pinned versions in the Dockerfile ARG)

## Frameworks

**Core:**
- Flask 3.0.3 — HTTP server for the translating proxy (`proxy/requirements.txt`, `proxy/proxy.py`: `app = Flask(__name__)`)
- requests 2.32.3 — outbound HTTP to the upstream API from the proxy (`proxy/requirements.txt`)

**Agent CLI:**
- opencode-ai 1.15.7 — the coding agent CLI installed in the agent image (`agents/Dockerfile`: `ARG OPENCODE_VERSION=1.15.7`)
- @ai-sdk/openai-compatible 2.0.47 — opencode provider adapter that points opencode at ollama (`agents/Dockerfile`: `ARG OPENAI_COMPAT_VERSION=2.0.47`)

**Build/Dev:**
- Docker Compose (v2, plugin form `docker compose`) — orchestrates all services; invoked via the `compose()` wrapper in `harness` that injects env-file, project name, and runtime overrides
- Docker Buildx — used in CI for layer caching (`.github/workflows/ci.yml`)

## Key Dependencies

**Critical (proxy image):**
- `flask==3.0.3` — serves the proxy HTTP endpoints (`proxy/requirements.txt`)
- `requests==2.32.3` — POSTs to the upstream API (`proxy/requirements.txt`)
- `iptables`, `ipset`, `dnsutils`, `iproute2`, `jq`, `curl` — required by `firewall/init-firewall.sh` inside the proxy container (`proxy/Dockerfile`)

**Critical (agent image):**
- `opencode-ai@1.15.7` — coding agent CLI (`agents/Dockerfile`)
- `@ai-sdk/openai-compatible@2.0.47` — opencode provider adapter (`agents/Dockerfile`)
- `gosu` — privilege drop from root to `harness` user after UID remap (`agents/Dockerfile`)
- `git`, `python3`, `pipx`, `build-essential`, `python3-dev` — runtime tooling available inside agent containers (`agents/Dockerfile`)

**Critical (ollama image):**
- `ollama/ollama:0.21.2` — base image (`ollama/Dockerfile`); RemoteHost stub-model support requires >= v0.12.0

**Infrastructure:**
- `harness` bash script — single-file management CLI, no external dependencies beyond bash 4+, docker/podman, and optionally `jq` (falls back to proxy container's jq if missing on host)

## Configuration

**Environment:**
- Single `.env` file at the install root (the git clone). Loaded by `docker compose --env-file ./.env` and sourced directly by the `harness` script.
- Required: `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`
- Optional with defaults: `PROXY_HOST` (0.0.0.0), `PROXY_PORT` (8000), `PROXY_TIMEOUT` (180), `OLLAMA_VERSION` (0.21.2), `OLLAMA_CONTEXT_LENGTH` (200000)
- See `.env.example` for the full annotated list of all variables.

**Build:**
- `docker-compose.yml` — service definitions; all image names (`harness-ollama`, `harness-proxy`, `harness-agent`) built locally from repo-root context
- `state/.harness-runtime.yml` — ephemeral compose override generated at each `harness start/restart`; never committed; holds PUBLISH_OLLAMA_PORT, PROXY_PROMPT_MODE, and firewall opt-out env vars
- `mcp-registry/serena/compose.yml` — merged at compose time when Serena MCP is enabled

## Platform Requirements

**Development:**
- Linux, macOS, or Windows (Git Bash) host
- Docker Desktop or Docker Engine with Compose plugin; or rootless Podman 4.0+ on Linux
- Bash 4+ (macOS ships Bash 3.2; users must upgrade via Homebrew — see `docs/PODMAN.md`)
- No host-side Python, Node.js, or npm required — all agent/proxy deps are containerized
- Optional: `jq` on host (harness falls back to the proxy container's jq if missing)

**Production:**
- Same container runtime requirements as development
- All services run containerized; no host-side daemons beyond Docker/Podman
- Firewall requires `NET_ADMIN` and `NET_RAW` capabilities on each container (set in `docker-compose.yml`)

---

*Stack analysis: 2026-06-02*
