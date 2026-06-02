# Codebase Structure

**Analysis Date:** 2026-06-02

## Directory Layout

```
<install-root>/                     # git clone AND install root (same directory)
├── harness                         # Management CLI — bash, ~5111 lines
├── harness-install.sh              # Bootstrap installer (~36KB)
├── docker-compose.yml              # Service definitions (ollama, proxy, agent)
├── CLAUDE.md                       # Agent operating instructions (auto-loaded)
├── README.md                       # User-facing overview
├── MANUAL_TEST_PROMPT.md           # Manual smoke-test prompts
│
├── proxy/                          # Translating proxy (Flask)
│   ├── proxy.py                    # Main application, ~1802 lines
│   ├── test_proxy.py               # Unit tests (run in proxy container)
│   ├── Dockerfile                  # Builds harness-proxy:latest
│   └── requirements.txt            # Python deps (flask, requests)
│
├── ollama/                         # Custom ollama image + stub-model entrypoint
│   ├── entrypoint.sh               # Model discovery + stub registration
│   └── Dockerfile                  # Builds harness-ollama:<version>
│
├── agents/                         # Agent image (opencode + shell)
│   ├── entrypoint.sh               # Firewall/UID/gosu/skel/mode dispatch
│   ├── configure-git-credentials.sh # Git push credential setup
│   └── Dockerfile                  # Builds harness-agent:latest
│
├── firewall/                       # Universal egress firewall
│   ├── init-firewall.sh            # iptables/ipset rule installer (all containers)
│   └── README.md                   # Operator debugging notes
│
├── mcp-registry/                   # Vetted MCP service definitions
│   └── serena/                     # Reference example
│       ├── compose.yml             # Partial compose snippet (external harness-net)
│       ├── client-config.json      # Agent MCP config (mcpServers shape)
│       ├── harness-meta.json.template  # Metadata + repo_clone_url/ref
│       └── README.md               # Operator-facing description
│
├── architecture/                   # Authoritative architecture docs
│   ├── README.md                   # System overview + doc index
│   ├── harness-cli.md              # harness bash CLI deep-dive
│   ├── proxy.md                    # proxy.py deep-dive
│   ├── containers.md               # Service composition + agent launch path
│   ├── upstream-api.md             # Upstream API contract + quirks
│   ├── mcp.md                      # MCP lifecycle and registry layout
│   ├── install-and-upgrade.md      # Install + upgrade machinery
│   └── tests.md                    # Test layout and conventions
│
├── scripts/                        # Shared bash libraries + manifests
│   ├── check_runtime_calls.sh      # Linter: detects disallowed runtime patterns
│   ├── upgrade-manifest.json       # Upgrade actions for harness upgrade
│   └── lib/
│       ├── platform.sh             # OS/runtime detection, mount validation, jq fallback
│       ├── net_helpers.sh          # Allowlist + net-overrides JSON manipulation
│       └── upgrade_actions.sh      # envfile_merge / linefile_merge / directory_overwrite
│
├── tests/                          # All test suites
│   ├── INVENTORY.md                # Test inventory
│   ├── COVERAGE.md                 # Coverage tables
│   ├── README.md                   # Test guide
│   ├── harness_test.sh             # harness CLI tests (docker-based)
│   ├── proxy_test.sh               # Proxy integration tests (docker-based)
│   ├── integration_test.sh         # Integration tests (docker-based)
│   ├── full_pipeline_test.sh       # End-to-end pipeline (docker-based)
│   ├── persistence_test.sh         # Persistence tests (docker-based)
│   ├── firewall_test.sh            # Firewall tests (docker-based)
│   ├── mcp_test.sh                 # MCP tests (docker-based)
│   ├── scheme_contract_test.sh     # Scheme contract tests (docker-based)
│   ├── upgrade_test.sh             # Upgrade tests (docker-based)
│   ├── podman_smoke_test.sh        # Podman smoke tests
│   ├── unit_platform_timer_test.sh # Unit test (docker-free)
│   ├── unit_workdir_test.sh        # Unit test (docker-free)
│   ├── mock_upstream.py            # Mock upstream API for tests
│   ├── lib/
│   │   └── test_helpers.sh         # Shared test utilities
│   ├── fixtures/
│   │   ├── responses/              # Fixture API response files
│   │   │   └── README.md
│   │   └── test-project/           # Minimal project used in test scenarios
│   └── benchmarks/                 # Benchmark suite
│       ├── README.md
│       ├── adapters/               # Benchmark adapters
│       ├── cache/                  # Response cache
│       ├── harbor/                 # Harbor tooling
│       ├── mock-api/               # Mock API for benchmarks
│       ├── runners/                # Benchmark runner scripts
│       ├── runs/                   # Benchmark run results
│       ├── schemes/                # Benchmark scoring schemes
│       └── tasks/                  # Benchmark task definitions
│
├── docs/                           # Supplementary operator docs
│   ├── PODMAN.md                   # Podman-specific notes
│   ├── WINDOWS.md                  # Windows-specific notes
│   └── hybrid-mode-consolidation/  # Design notes for hybrid prompt mode
│
├── .claude/                        # Agent workflow instructions (not user-facing)
│   ├── references/workflows/       # new-issue.md, implementing.md, etc.
│   └── skills/first-principles/    # First-principles reasoning skill
│
└── .planning/                      # Planning artifacts (gitignored or tracked)
    └── codebase/                   # Codebase analysis documents (this file)

# Gitignored at install root (not shown above):
#   .env                            # User config (PROXY_API_URL, PROXY_API_KEY, etc.)
#   .harness-allowlist              # Egress allowlist (one host per line)
#   state/                          # Runtime state
#     output/                       # Proxy debug dumps (bind-mounted to proxy /output)
#     agent/home/                   # Shared agent /home/harness across all launches
#     ollama-data/                  # Ollama model blobs
#     mcp/<name>/                   # Active MCP service state
#       compose.yml                 # Copied from mcp-registry on install
#       client-config.json          # Agent MCP config entry
#       harness-meta.json           # enabled flag, repo_clone_url/ref
#       data/                       # MCP persistent data (survives uninstall)
#       repo/                       # Cloned upstream repo (when repo_clone_url set)
#     .harness-runtime.yml          # Ephemeral compose override (regenerated per compose())
#     .harness-net-overrides.json   # Persisted per-service firewall overrides
#     .harness-update-check         # Cached remote HEAD for update banner
```

## Directory Purposes

**`proxy/`:**
- Purpose: The format-translating Flask proxy that sits between ollama and the upstream API
- Key files: `proxy/proxy.py` (all logic), `proxy/test_proxy.py` (unit tests)
- Build: `proxy/Dockerfile` builds `harness-proxy:latest`

**`ollama/`:**
- Purpose: Custom ollama image that registers per-model stub entries pointing at the proxy
- Key files: `ollama/entrypoint.sh` (model discovery + registration)
- Build: `ollama/Dockerfile` builds `harness-ollama:<OLLAMA_VERSION>`

**`agents/`:**
- Purpose: Single unified agent image serving both `opencode` and `shell` modes
- Key files: `agents/entrypoint.sh` (all init + mode dispatch), `agents/configure-git-credentials.sh`
- Build: `agents/Dockerfile` builds `harness-agent:latest`

**`firewall/`:**
- Purpose: egress firewall script that runs in every container entrypoint
- Key files: `firewall/init-firewall.sh` (iptables/ipset rules), `firewall/README.md` (operator debugging)

**`mcp-registry/`:**
- Purpose: In-repo vetted MCP service definitions
- Pattern: one subdirectory per MCP, each containing `compose.yml`, `client-config.json`, `harness-meta.json.template`, `README.md`
- Reference example: `mcp-registry/serena/`

**`scripts/lib/`:**
- Purpose: Shared bash libraries sourced by `harness` and test scripts
- Key files: `platform.sh` (always sourced), `net_helpers.sh` (lazy-loaded), `upgrade_actions.sh` (upgrade only)

**`state/`:**
- Purpose: Gitignored runtime state — volumes, generated configs, debug output
- Generated: Yes (at runtime)
- Committed: No

**`architecture/`:**
- Purpose: Short structural docs (shape, contracts, invariants) — kept honest per-change
- These are the authoritative reference before modifying any component

## Key File Locations

**Entry Points:**
- `harness`: CLI management entrypoint; `main()` at end of file
- `proxy/proxy.py`: Flask app; `main()` at line 1764, `catch_all` route at line 1561
- `ollama/entrypoint.sh`: Container entrypoint; model registration loop at line 156
- `agents/entrypoint.sh`: Container entrypoint; mode dispatch at line 123

**Configuration:**
- `.env`: User config (`PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`, etc.) — gitignored
- `docker-compose.yml`: Service definitions, environment passthrough, volume mounts
- `state/.harness-runtime.yml`: Ephemeral compose override (runtime-generated, never committed)
- `state/.harness-net-overrides.json`: Persisted per-service firewall state
- `scripts/upgrade-manifest.json`: Upgrade action definitions for `harness upgrade`

**Core Logic:**
- `proxy/proxy.py:catch_all` (line 1561): Single Flask route handler; owns the entire request lifecycle
- `proxy/proxy.py:translate_history_and_apply_prompt` (line 1075): ollama→upstream message translation + prompt injection
- `proxy/proxy.py:extract_tool_calls_and_text` (line 795): Balanced-brace JSON parser for tool calls
- `harness:run_agent` (line 2292): Agent container launch logic
- `harness:compose` (line ~370): Single entry point for all `docker compose` invocations
- `harness:write_runtime_override` (~line 330): Generates `state/.harness-runtime.yml`
- `scripts/lib/platform.sh:harness_resolve_interactive_tty` (line 284): TTY strategy for interactive launches

**Testing:**
- `tests/unit_platform_timer_test.sh`: Docker-free unit tests (platform helpers, timers)
- `tests/unit_workdir_test.sh`: Docker-free unit tests (working directory)
- `proxy/test_proxy.py`: Unit tests for proxy logic (run in proxy container)
- `tests/harness_test.sh`: Docker-based CLI tests
- `tests/proxy_test.sh`: Docker-based proxy integration tests

## Naming Conventions

**Files:**
- Shell scripts: `snake_case.sh` (e.g. `init-firewall.sh`, `upgrade_actions.sh`)
- Python: `snake_case.py` (e.g. `proxy.py`, `test_proxy.py`, `mock_upstream.py`)
- Test files: `<subject>_test.sh` or `unit_<subject>_test.sh` for docker-free unit tests
- Architecture docs: `<component>.md` (lowercase kebab) in `architecture/`
- Docker images: `harness-<service>:<tag>` (e.g. `harness-proxy:latest`, `harness-agent:latest`)

**Directories:**
- MCP registry entries: `mcp-registry/<name>/` (lowercase)
- Active MCP state: `state/mcp/<name>/` (matches registry name)

**Bash functions:**
- CLI subcommands: `cmd_<name>` (e.g. `cmd_start`, `cmd_mcp_install`)
- Internal helpers: descriptive snake_case (e.g. `write_runtime_override`, `_gate_on_upstream_auth`)
- Library exports: `harness_<name>` prefix in `scripts/lib/platform.sh` (e.g. `harness_detect_os`, `harness_container_workdir`)

**Python:**
- Private module-level globals: `_UPPER_SNAKE_CASE` (e.g. `_PROMPT_MODE`, `_HOST_OS`, `_OUTPUT_DIR`)
- Public module-level constants: `UPPER_SNAKE_CASE` (e.g. `CHAT_URL`, `PROXY_PORT`)
- Private functions: `_snake_case` prefix (e.g. `_normalize_api_base`, `_strip_model_tag`)
- Public functions: `snake_case` (e.g. `catch_all`, `translate_history_and_apply_prompt`)

## Where to Add New Code

**New proxy behavior (prompt injection, tool-call parsing, upstream handling):**
- Implementation: `proxy/proxy.py`
- Tests: `proxy/test_proxy.py` (unit), `tests/proxy_test.sh` (integration)
- Update doc: `architecture/proxy.md`

**New `harness` subcommand:**
- Add `cmd_<name>()` function in `harness`
- Add to dispatch table in `main()` (end of `harness` file)
- Add to help text in `cmd_help()`
- Update doc: `architecture/harness-cli.md`

**New MCP registry entry:**
- Create `mcp-registry/<name>/` with `compose.yml`, `client-config.json`, `harness-meta.json.template`, `README.md`
- Add matching `registry_actions` entry to `scripts/upgrade-manifest.json`
- Reference: `mcp-registry/serena/` as the template
- Update doc: `architecture/mcp.md`

**New allowlist/network logic:**
- Add to `scripts/lib/net_helpers.sh` (lazy-loaded) or `harness` directly for simple cases
- Tests: `tests/firewall_test.sh`

**New platform/runtime helper:**
- Add to `scripts/lib/platform.sh` with `harness_` prefix
- Tests: `tests/unit_platform_timer_test.sh` or a new `tests/unit_<subject>_test.sh`

**New agent entrypoint behavior:**
- Add to `agents/entrypoint.sh`
- Update doc: `architecture/containers.md`

**New test:**
- Docker-free unit test: `tests/unit_<subject>_test.sh`
- Docker-based: `tests/<subject>_test.sh`
- See `tests/INVENTORY.md` for naming/registration conventions

## Special Directories

**`state/`:**
- Purpose: All gitignored runtime state (volumes, generated configs, debug output)
- Generated: Yes (at runtime by `harness start` and container entrypoints)
- Committed: No

**`state/output/`:**
- Purpose: Per-request proxy debug dumps (`<req_id>_01_Ollama_Request.json` through `_04_NDJSON_Response.json`)
- Generated: Yes (when `OUTPUT_DIR` env var is set in proxy container)
- Committed: No

**`state/agent/home/`:**
- Purpose: Bind-mounted as `/home/harness` in every agent container — shared across all agent launches and container rebuilds
- Generated: Yes (seeded from `/etc/skel/harness/` on first run)
- Committed: No

**`.planning/codebase/`:**
- Purpose: Codebase analysis documents consumed by planning and execution workflows
- Generated: Yes (by `gsd:map-codebase`)
- Committed: Yes (tracked)

---

*Structure analysis: 2026-06-02*
