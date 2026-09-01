# Codebase Structure

**Analysis Date:** 2026-06-10

## Directory Layout

```text
harness/                         the git clone — IS the install root
├── harness                      management CLI (one ~7421-line bash script)
├── harness-install.sh           bootstrap installer (~41K, one-shot setup)
├── docker-compose.yml           proxy + agent service definitions (131 lines)
├── .env.example                 documented user config template
├── .harness-allowlist.example   egress allowlist template
├── README.md                    user-facing docs
├── MANUAL_TEST_PROMPT.md        manual QA script
├── CLAUDE.md                    agent operating instructions
├── proxy/                       translating proxy (Flask)
├── agents/                      unified agent image (opencode/shell)
├── firewall/                    universal egress firewall + git creds
├── mcp-registry/<name>/         vetted container-MCP definitions
├── host-mcp/                    host (non-container) MCP scaffold + instances
│   ├── template/                scaffold copied by `mcp host-init`
│   └── <name>/                  a registered host build MCP (gitignored)
├── scripts/                     shared bash libraries + upgrade manifest
├── tests/                       test suite (see tests/INVENTORY.md)
├── architecture/                per-module architecture docs
├── docs/                        runtime-specific operator notes
├── .planning/                   GSD planning + this codebase map
└── state/                       gitignored — runtime state (created at install)
    ├── output/                  proxy per-request debug dumps
    ├── agent/home/              shared /home/harness bind-mounted into agents
    ├── mcp/<name>/              active container-MCP services (compose + data)
    └── host/                    host-mode runtime state
        ├── venv/                lazy proxy venv (`harness host`)
        ├── toolchain/           host-mode deps (jq, node, opencode)
        ├── proxy.pid            host proxy pidfile
        ├── proxy.log            host proxy logfile
        ├── opencode.json        scoped opencode config (OPENCODE_CONFIG target)
        └── proxy.fp             host proxy config fingerprint
```

## Directory Purposes

**`proxy/`:**
- Purpose: The translating proxy that fronts the upstream API.
- Contains: `proxy.py` (the Flask app, ~2558 lines), `Dockerfile`, `entrypoint.sh` (runs `init-firewall.sh` then `python3 proxy.py`), `requirements.txt` (two pure-python wheels — also installed into the host-mode venv), `test_proxy.py` (unit tests run inside the proxy container).
- Key files: `proxy/proxy.py`, `proxy/test_proxy.py`.

**`agents/`:**
- Purpose: The unified agent container image (opencode and/or shell mode).
- Contains: `Dockerfile` (builds the image, pins opencode + Node + jq versions), `entrypoint.sh` (~372 lines, the runtime dispatch point — firewall, UID remap, gosu drop, config, mode dispatch), `clipboard-bridge.sh` (OSC 52 clipboard bridge shim), `requirements.txt` (Python deps for opencode compatibility).
- Key files: `agents/Dockerfile`, `agents/entrypoint.sh`.
- Note: Single image backs both `opencode` and `shell` modes; mode is selected at runtime.

**`firewall/`:**
- Purpose: Universal egress firewall + git credentials integration.
- Contains: `init-firewall.sh` (~329 lines, iptables/ipset rules at container startup), `configure-git-credentials.sh` (git credential helper + git-push-enabled hosts), `clipboard-bridge.sh` (OSC 52 copy bridge shim).
- Key files: `firewall/init-firewall.sh`.
- Note: Container-bound; host mode has no firewall (defense-in-depth is loopback-only binding).

**`mcp-registry/`:**
- Purpose: Versioned, in-repo MCP service definitions.
- Contains: Per-service directories (e.g. `serena/`), each with:
  - `compose.yml` — partial compose snippet (joins `harness_harness-net` as external)
  - `client-config.json` — opencode MCP client config
  - `harness-meta.json.template` — metadata (repo_clone_url, repo_clone_ref, allowed_domains)
  - `recency.json` — per-tool one-line guidance (for the hybrid-mode recency reminder)
  - `README.md` — operator-facing docs
- Key files: `mcp-registry/serena/` (reference implementation).

**`host-mcp/`:**
- Purpose: Host (non-container) MCP scaffold and instances.
- Contains: `template/` (scaffold for new host MCPs — `mcp host-init` copies this), `<name>/` (active host MCP instances, gitignored — user-customized servers).
- Key files: `host-mcp/template/server.py`, `host-mcp/template/AGENTS.md`.

**`scripts/`:**
- Purpose: Shared bash utility libraries and metadata.
- Contains:
  - `lib/platform.sh` — OS detection, container runtime detection, portable realpath, jq helpers, Windows path handling
  - `lib/net_helpers.sh` — allowlist + override JSON manipulation (lazily sourced)
  - `lib/upgrade_actions.sh` — actions called by `harness upgrade` (envfile_merge, linefile_merge, directory_overwrite, userfile_sync)
  - `upgrade-manifest.json` — what gets upgraded and how (registry_actions, etc.)
- Key files: `scripts/lib/platform.sh`, `scripts/upgrade-manifest.json`.

**`tests/`:**
- Purpose: Test suite (docker-free + docker-based paths).
- Contains: `harness_test.sh`, `unit_host_test.sh`, `unit_host_toolchain_test.sh` (bash unit tests), `proxy_test.sh` (integration via proxy container), `proxy/test_proxy.py` (Python unit tests in the proxy container), fixtures, benchmarks.
- Key files: `tests/harness_test.sh`, `tests/unit_host_test.sh`, `tests/proxy_test.sh`.
- Note: See `tests/INVENTORY.md` and `tests/COVERAGE.md` for detailed coverage.

**`architecture/`:**
- Purpose: Per-module detailed architecture docs (referenced from `.CLAUDE.md` workflow router).
- Contains: `README.md`, `harness-cli.md`, `proxy.md`, `containers.md`, `mcp.md`, `upstream-api.md`, `install-and-upgrade.md`, `tests.md`.
- Loaded by the agent during issue work to understand the system being modified.

**`docs/`:**
- Purpose: Runtime-specific operator notes.
- Contains: `PODMAN.md` (podman-specific setup), `WINDOWS.md` (Windows / Git Bash notes), `hybrid-mode-consolidation/` (historical docs on prompt-mode refactor).

**`state/`:**
- Purpose: Runtime state (gitignored).
- Contains:
  - `output/` — proxy per-request debug dumps (when `OUTPUT_DIR` is set)
  - `agent/home/` — shared agent home (bind-mounted into every agent; survives rebuilds)
  - `mcp/<name>/` — active MCP services (compose snippets, client config, enabled flag, data directory)
  - `host/` — host-mode runtime state (proxy venv, toolchain, pids, logs, opencode config)

## Key File Locations

**Entry Points:**
- `harness` — CLI entry point (bash); spawned by user or orchestrator
- `proxy/proxy.py` — Proxy entry point (Python); module load runs config validation, Flask routes setup
- `agents/entrypoint.sh` — Container entry point (bash); firewall + privilege drop + mode dispatch

**Configuration:**
- `.env` — User env config (gitignored; created from `.env.example`)
- `.harness-allowlist` — Egress allowlist (gitignored; created from `.harness-allowlist.example`)
- `docker-compose.yml` — Service definitions (tracked; read at `harness start` time)
- `scripts/upgrade-manifest.json` — Upgrade actions (tracked; read by `harness upgrade`)

**Core Logic:**
- `harness` — CLI dispatch, service lifecycle, agent launch (7421 lines)
- `proxy/proxy.py` — OpenAI-compat endpoint, upstream translation, tool injection (2558 lines)
- `agents/entrypoint.sh` — Container setup, mode dispatch (372 lines)
- `firewall/init-firewall.sh` — Iptables/ipset rules (329 lines)

**Testing:**
- `tests/harness_test.sh` — CLI unit tests (docker-free)
- `tests/unit_host_test.sh` — Host-mode unit tests (docker-free)
- `tests/unit_host_toolchain_test.sh` — Toolchain provisioner tests (docker-free)
- `proxy/test_proxy.py` — Proxy unit tests (run in proxy container)
- `tests/proxy_test.sh` — Proxy integration tests

## Naming Conventions

**Files:**
- `harness` — main CLI (no suffix)
- `<cmd>_test.sh` — test file (bash)
- `test_<module>.py` — test file (Python)
- `init-<service>.sh` — initialization script (entry point)
- `*.template` — template file (substitution happens at install time)
- `compose.yml` — compose snippet (lowercase `.yml`)

**Directories:**
- `mcp-registry/<name>/` — MCP service (kebab-case; matches service name in compose)
- `host-mcp/<name>/` — Host MCP instance (kebab-case; user-defined name)
- `state/mcp/<name>/` — Active (installed) MCP directory (kebab-case; matches `mcp-registry/<name>/`)
- `state/output/` — Debug output (per-request files named `<req_id>_<seqnum>_<purpose>.json`)

**Functions (bash):**
- `cmd_<name>` — Subcommand handler (e.g. `cmd_start`, `cmd_opencode`)
- `run_<thing>` — Runner/launch function (e.g. `run_agent`, `host_run_opencode`)
- `<verb>_<noun>` — Action function (e.g. `ensure_services_up`, `stop_stack_if_last_agent`)
- `_<name>` — Internal helper (leading underscore)

**Functions (Python):**
- `catch_all(path)` — Flask route handler (catch-all, *not* named after HTTP verb)
- `translate_<thing>` — Transformation function
- `extract_<thing>` — Parser/extractor function
- `format_<thing>` — Formatter function
- `_<name>` — Internal helper (leading underscore)

**Environment Variables:**
- `UPPERCASE_SNAKE_CASE` — Config (e.g. `PROXY_API_URL`, `DEFAULT_MODEL_NAME`)
- `HARNESS_*` — Harness-specific (e.g. `HARNESS_FIREWALL_DISABLED`, `HARNESS_HOST_OS`)

## Where to Add New Code

**New Feature:**
- If it's a CLI subcommand: add a `cmd_<name>` function in `harness`
- If it's proxy logic (endpoint, tool handling, etc.): add to `proxy/proxy.py`
- If it's agent behavior: modify `agents/entrypoint.sh` or the agent image's `Dockerfile`

**New MCP Service (vetted):**
- Create `mcp-registry/<name>/` with the four-file contract (`compose.yml`, `client-config.json`, `harness-meta.json.template`, `recency.json`, `README.md`)
- Add entry to `scripts/upgrade-manifest.json` (if it should auto-upgrade)

**New Test:**
- Docker-free (preferred for CI speed): add to `tests/harness_test.sh` or `tests/unit_host_test.sh`
- Docker-based: add to `tests/proxy_test.sh` or `proxy/test_proxy.py`
- See `tests/INVENTORY.md` for test organization

**Utility Library:**
- Bash helpers: add function to `scripts/lib/platform.sh` (platform-specific) or `scripts/lib/net_helpers.sh` (network/firewall)
- Python utilities: keep them in-file in `proxy/proxy.py` (single-file design; no module split)

**Architecture Documentation:**
- Per-module deep-dive: edit the relevant file in `architecture/` (e.g. `architecture/harness-cli.md`)
- System overview: edit `architecture/README.md`
- Update the doc **in the same commit** as the code change (see `CLAUDE.md` → "Doc-update rule")

## Special Directories

**`state/output/`:**
- Purpose: Proxy per-request debug dumps (when `OUTPUT_DIR` is set in `.env`)
- Generated: Yes (by `proxy/proxy.py` on each request)
- Committed: No (gitignored)
- Format: JSON dumps named `<req_id>_<seqnum>_<purpose>.json` (e.g. `20260610_123456_789_01_Inbound_Request.json`)

**`state/agent/home/`:**
- Purpose: Shared agent home directory (persistent across container rebuilds)
- Generated: Yes (on first agent launch; seeded from `/etc/skel/harness/` in the image)
- Committed: No (gitignored)
- Lifecycle: Survives container rebuilds; shared across all agent invocations

**`state/mcp/<name>/`:**
- Purpose: Active (installed/registered) MCP service directory
- Generated: Yes (by `harness mcp install` or `harness mcp register`)
- Committed: No (gitignored)
- Contains: `compose.yml`, `client-config.json`, `harness-meta.json` (metadata), `data/` (per-MCP runtime state), `repo/` (cloned source if from git)

**`state/host/`:**
- Purpose: Host-mode (`harness host`) runtime state
- Generated: Yes (on first host launch or when config changes)
- Committed: No (gitignored)
- Contains: `venv/` (Python venv for proxy), `toolchain/` (vendored jq, node, opencode), `proxy.pid`, `proxy.log`, `proxy.fp` (config fingerprint), `opencode.json` (scoped config)

---

*Structure analysis: 2026-06-10*
