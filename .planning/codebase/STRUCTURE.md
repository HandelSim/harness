# Codebase Structure

**Analysis Date:** 2026-06-08

## Directory Layout

```text
harness/                         the git clone — IS the install root
├── harness                      management CLI (one ~6589-line bash script)
├── harness-install.sh           bootstrap installer
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
        ├── proxy.pid            host proxy pidfile
        ├── proxy.log            host proxy logfile
        └── opencode.json        scoped opencode config (OPENCODE_CONFIG target)
```

## Directory Purposes

**`proxy/`:**
- Purpose: The translating proxy that fronts the upstream API.
- Contains: `proxy.py` (the Flask app, ~2516 lines), `Dockerfile`,
  `entrypoint.sh` (runs `init-firewall.sh` then `python3 proxy.py`),
  `requirements.txt` (two pure-python wheels — also installed into the host-mode
  venv), `test_proxy.py` (unit tests run inside the proxy container).
- Key files: `proxy/proxy.py`, `proxy/test_proxy.py`.

**`agents/`:**
- Purpose: The single image backing both agent modes (opencode, shell).
- Contains: `Dockerfile` (pins `OPENCODE_VERSION`), `entrypoint.sh` (firewall,
  UID remap, gosu drop, git creds, skel seed, mode dispatch, opencode config).
- Key files: `agents/Dockerfile`, `agents/entrypoint.sh`.

**`firewall/`:**
- Purpose: Universal egress allowlist gate run at the top of every container
  (container mode only — absent in host mode).
- Contains: `init-firewall.sh` (iptables/ipset rules from the allowlist),
  `configure-git-credentials.sh`, `README.md`.
- Key files: `firewall/init-firewall.sh`.

**`mcp-registry/<name>/`:**
- Purpose: Vetted, repo-tracked container-MCP definitions.
- Contains per entry: `compose.yml`, `client-config.json`,
  `harness-meta.json.template`, `recency.json` (per-tool recency + state-check
  data), `README.md`. `mcp-registry/serena/` is the reference example.

**`host-mcp/`:**
- Purpose: Host (non-container) build MCPs — a process on the host (e.g.
  MSVC/CMake) the agent can drive. New top-level concern.
- Contains: `template/` (`project.json`, `server.py`, `run.sh`,
  `requirements.txt`, `AGENTS.md`, `README.md`) copied by `harness mcp
  host-init`; per-name instance dirs created by `host-setup` (gitignored).

**`scripts/`:**
- Purpose: Shared bash libraries and the upgrade manifest.
- Contains: `check_runtime_calls.sh` (lint), `upgrade-manifest.json`, and `lib/`
  with `platform.sh` (OS/runtime detection, portable realpath, jq helpers, TTY
  resolution), `net_helpers.sh` (allowlist/overrides JSON, lazily sourced),
  `upgrade_actions.sh` (envfile_merge / linefile_merge / directory_overwrite).

**`tests/`:**
- Purpose: The full test suite, mostly bash, plus the proxy unit tests.
- Contains: per-area `*_test.sh`, `mock_upstream.py`, `fixtures/`, `lib/`,
  `benchmarks/`, and the `INVENTORY.md` / `COVERAGE.md` inventories.

**`architecture/`:**
- Purpose: Short, structural per-module docs kept honest with the code.
- Key files: `README.md` (index), `proxy.md`, `containers.md`, `harness-cli.md`
  (includes the "Host mode" section), `mcp.md`, `upstream-api.md`,
  `install-and-upgrade.md`, `tests.md`.

**`docs/`:**
- Purpose: Runtime-specific operator notes (`PODMAN.md`, `WINDOWS.md`).

**`.planning/`:**
- Purpose: GSD planning artifacts and this codebase map (`codebase/*.md`).

**`state/` (gitignored, created at install):**
- Purpose: All runtime state. `output/` (proxy debug dumps), `agent/home/`
  (persistent agent home bind mount), `mcp/<name>/` (active container-MCP
  trees), `host/` (host-mode venv + proxy pidfile/logfile + scoped opencode
  config), plus generated files `.harness-runtime.yml`,
  `.harness-net-overrides.json`, `.harness-update-check`.

## Key File Locations

**Entry Points:**
- `harness`: the management CLI; `main()` at `harness:6519`. Bare invocation or
  a leading agent flag launches an opencode agent (container mode).
- `proxy/proxy.py`: the Flask app; `main()` at `proxy/proxy.py:2472`, request
  handler `catch_all` at `proxy/proxy.py:2177`, models passthrough `list_models`
  at `proxy/proxy.py:2131`.
- `agents/entrypoint.sh`: agent container entrypoint, dispatches opencode/shell.
- `harness-install.sh`: bootstrap installer.

**Configuration:**
- `docker-compose.yml`: proxy + agent services, harness-net, cap_add, sysctls.
- `.env` (gitignored; `.env.example` is the template): user config
  (`PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`, mounts, etc.).
- `.harness-allowlist` (gitignored; `.harness-allowlist.example`): egress hosts.
- `scripts/upgrade-manifest.json`: how `harness upgrade` brings forward state.
- `state/host/opencode.json`: scoped opencode config for host mode
  (`OPENCODE_CONFIG` target; the user's global config is never touched).

**Core Logic:**
- `proxy/proxy.py`: translation, cooperative-prompt injection, tool-call
  extraction, meta-tool serving, malformed/empty-response recovery, SSE/JSON
  emission.
- `harness`: orchestration — compose wrapper (`compose()`, `harness:865`),
  runtime override (`write_runtime_override()`, `harness:779`), container agent
  launch (`run_agent`, `harness:3047`; `cmd_shell`, `harness:3508`), host mode
  (`cmd_host`, `harness:2990`; `host_*` helpers `harness:2618`–`2988`).
- `scripts/lib/*.sh`: cross-cutting bash helpers.

**Testing:**
- `tests/*_test.sh`: per-area suites (proxy, harness, firewall, mcp,
  persistence, scheme_contract, integration, full_pipeline, upgrade, podman).
- `proxy/test_proxy.py`: proxy unit tests.
- `tests/mock_upstream.py`, `tests/fixtures/`, `tests/lib/`.

## Naming Conventions

**Files:**
- CLI subcommand handlers: `cmd_<name>()` inside `harness`.
- Host-mode helpers: `host_<verb>()` (`host_preflight`, `host_proxy_start`,
  `host_write_opencode_config`); host path accessors return their path
  (`host_proxy_pidfile`, `host_opencode_config`, `host_venv_dir`).
- Proxy private helpers: leading underscore (`_normalize_api_base`,
  `_scan_balanced_json`, `_setup_prompt_mode`, `_serve_meta_tools`).
- Test suites: `<area>_test.sh`; unit suites `unit_<area>_test.sh`.
- Architecture docs: lowercase-hyphen `.md` matching the module.

**Directories:**
- Per-module top-level dirs named for their artifact (`proxy/`, `agents/`,
  `firewall/`, `host-mcp/`).
- Per-entry MCP dirs keyed by service name (`mcp-registry/serena/`,
  `state/mcp/<name>/`, `host-mcp/<name>/`).

## Where to Add New Code

**New proxy behavior (translation, prompt mode, tool handling):**
- Primary code: `proxy/proxy.py` (single file by design).
- Tests: `proxy/test_proxy.py` (unit) and/or `tests/proxy_test.sh` (integration).
- Update `architecture/proxy.md` in the same commit if behavior changes.

**New CLI subcommand or flag:**
- Implementation: a `cmd_<name>()` in `harness`; register it in `cmd_help`
  (`harness:1326`, the canonical subcommand list) and the dispatch in `main()`
  (`harness:6519`).
- Tests: `tests/harness_test.sh` (or a new `tests/harness_*_test.sh`).
- Update `architecture/harness-cli.md`.

**New host-mode behavior:**
- Add a `host_*` helper alongside the existing block (`harness:2608`+); reuse
  the pidfile/logfile/venv path accessors. Host mode is deliberately minimal
  (single CWD, no host-MCP wiring beyond setup, Linux/macOS only) — keep new
  surface gated by `host_confirm_gate`. Update `architecture/harness-cli.md`
  ("Host mode") and `architecture/containers.md` ("Host mode has no firewall").

**New shared bash helper:**
- Add to the matching `scripts/lib/*.sh` (`platform.sh`, `net_helpers.sh`,
  `upgrade_actions.sh`), keeping `net_helpers.sh` lazily sourced.

**New container MCP service:**
- Vetted/shared: `mcp-registry/<name>/` with the file contract (`compose.yml`,
  `client-config.json`, `harness-meta.json.template`, `recency.json`,
  `README.md`); add a `registry_actions` entry to
  `scripts/upgrade-manifest.json`.
- One-off/private: `harness mcp register <name> --from <dir|git-url>`. See
  `architecture/mcp.md`.

**New host MCP:**
- `harness mcp host-init <name>` scaffolds from `host-mcp/template/`;
  `harness mcp host-setup <name>` launches the agent that tailors it. See
  `architecture/mcp.md` "Host MCPs".

**New container service:**
- Add to `docker-compose.yml`; attach to `harness-net`; apply the firewall
  contract (`cap_add: NET_ADMIN, NET_RAW`, IPv6-disable sysctl, allowlist
  mount). Update `architecture/containers.md`.

## Special Directories

**`state/`:**
- Purpose: All runtime state and generated config.
- Generated: Yes (created at install; many files regenerated per invocation).
- Committed: No (gitignored).

**`state/agent/home/`:**
- Purpose: The single persistent agent home bind-mounted into every agent.
- Generated: Yes (seeded from `/etc/skel/harness/` on first run).
- Committed: No.

**`state/host/`:**
- Purpose: Host-mode runtime — lazy proxy venv, proxy pidfile/logfile, scoped
  `opencode.json`.
- Generated: Yes (created on first `harness host`; venv re-pipped on
  requirements hash change).
- Committed: No.

**`mcp-registry/`:**
- Purpose: Repo-tracked container-MCP definitions (source for `mcp install`).
- Generated: No. Committed: Yes.

**`host-mcp/template/`:**
- Purpose: Repo-tracked scaffold for host build MCPs.
- Generated: No. Committed: Yes (per-name instance dirs under `host-mcp/` are
  gitignored).

## Monolith Note

Two files carry most of the system and are intentionally single-file:

- `harness` — ~6589 lines. One bash script: self-locate, env load, compose
  wrapper, runtime override, container agent launch, host mode, net, MCP,
  doctor/preflight, update/upgrade. `cmd_help` is the canonical subcommand list.
- `proxy/proxy.py` — ~2516 lines. One Flask app: translation, cooperative-prompt
  modes, tool-call extraction, meta-tool serving, malformed/empty-response
  recovery, SSE/JSON emission, config validation, debug dumps.

When adding to either, follow the existing in-file section ordering rather than
splitting the file.

---

*Structure analysis: 2026-06-08*
