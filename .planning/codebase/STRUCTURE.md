# Codebase Structure

**Analysis Date:** 2026-06-02

## Directory Layout

```text
harness/                         the git clone — IS the install root
├── harness                      management CLI (one ~5481-line bash script)
├── harness-install.sh           bootstrap installer (~898 lines)
├── docker-compose.yml           proxy + agent service definitions (113 lines)
├── .env.example                 documented user config template
├── .harness-allowlist.example   egress allowlist template
├── README.md                    user-facing docs
├── MANUAL_TEST_PROMPT.md        manual QA script
├── CLAUDE.md                    agent operating instructions
├── proxy/                       translating proxy (Flask)
├── agents/                      unified agent image (opencode/shell)
├── firewall/                    universal egress firewall + git creds
├── mcp-registry/<name>/         vetted MCP definitions
├── scripts/                     shared bash libraries + upgrade manifest
├── tests/                       test suite (see tests/INVENTORY.md)
├── architecture/                per-module architecture docs
├── docs/                        runtime-specific operator notes
├── .planning/                   GSD planning + this codebase map
└── state/                       gitignored — runtime state (created at install)
    ├── output/                  proxy per-request debug dumps
    ├── agent/home/              shared /home/harness bind-mounted into agents
    └── mcp/<name>/              active MCP services (compose + data)
```

## Directory Purposes

**`proxy/`:**
- Purpose: The translating proxy that fronts the upstream API.
- Contains: `proxy.py` (the Flask app), `Dockerfile`, `entrypoint.sh`
  (runs `init-firewall.sh` then `python3 proxy.py`), `requirements.txt`,
  `test_proxy.py` (unit tests run inside the proxy container).
- Key files: `proxy/proxy.py`, `proxy/test_proxy.py`.

**`agents/`:**
- Purpose: The single image backing both agent modes (opencode, shell).
- Contains: `Dockerfile` (pins `OPENCODE_VERSION`), `entrypoint.sh` (firewall,
  UID remap, gosu drop, git creds, skel seed, mode dispatch, opencode config).
- Key files: `agents/Dockerfile`, `agents/entrypoint.sh`.

**`firewall/`:**
- Purpose: Universal egress allowlist gate run at the top of every container.
- Contains: `init-firewall.sh` (iptables/ipset rules from the allowlist),
  `configure-git-credentials.sh`, `README.md` (operator notes).
- Key files: `firewall/init-firewall.sh`.

**`mcp-registry/<name>/`:**
- Purpose: Vetted, repo-tracked MCP service definitions.
- Contains: `compose.yml`, `client-config.json`, `harness-meta.json.template`,
  `README.md` per entry. `mcp-registry/serena/` is the reference example.

**`scripts/`:**
- Purpose: Shared bash libraries and the upgrade manifest.
- Contains: `check_runtime_calls.sh` (lint), `upgrade-manifest.json`, and
  `lib/` with `platform.sh` (OS/runtime detection, portable realpath, jq
  helpers), `net_helpers.sh` (allowlist/overrides JSON), `upgrade_actions.sh`
  (envfile_merge / linefile_merge / directory_overwrite).

**`tests/`:**
- Purpose: The full test suite, mostly bash, plus the proxy unit tests.
- Contains: per-area `*_test.sh`, `mock_upstream.py`, `fixtures/`, `lib/`,
  `benchmarks/`, and the `INVENTORY.md` / `COVERAGE.md` inventories.

**`architecture/`:**
- Purpose: Short, structural per-module docs kept honest with the code.
- Key files: `README.md` (index), `proxy.md`, `containers.md`,
  `harness-cli.md`, `mcp.md`, `upstream-api.md`, `install-and-upgrade.md`,
  `tests.md`.

**`docs/`:**
- Purpose: Runtime-specific operator notes (`PODMAN.md`, `WINDOWS.md`).

**`.planning/`:**
- Purpose: GSD planning artifacts and this codebase map (`codebase/*.md`).

**`state/` (gitignored, created at install):**
- Purpose: All runtime state. `output/` (proxy debug dumps),
  `agent/home/` (persistent agent home bind mount), `mcp/<name>/` (active MCP
  trees), plus generated files `.harness-runtime.yml`,
  `.harness-net-overrides.json`, `.harness-update-check`.

## Key File Locations

**Entry Points:**
- `harness`: the management CLI; `main()` at `harness:5412`. Bare invocation
  or a leading agent flag launches an opencode agent.
- `proxy/proxy.py`: the Flask app; `main()` at `proxy/proxy.py:1832`,
  request handler `catch_all` at `proxy/proxy.py:1643`.
- `agents/entrypoint.sh`: agent container entrypoint, dispatches opencode/shell.
- `harness-install.sh`: bootstrap installer.

**Configuration:**
- `docker-compose.yml`: proxy + agent services, harness-net, cap_add, sysctls.
- `.env` (gitignored; `.env.example` is the template): user config
  (`PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`, mounts, etc.).
- `.harness-allowlist` (gitignored; `.harness-allowlist.example`): egress hosts.
- `scripts/upgrade-manifest.json`: how `harness upgrade` brings forward state.

**Core Logic:**
- `proxy/proxy.py`: translation, cooperative-prompt injection, tool-call
  extraction, SSE/JSON emission.
- `harness`: orchestration, compose wrapper (`compose()`, `harness:758`),
  runtime override (`write_runtime_override()`, `harness:672`), agent launch
  (`run_agent`, `harness:2270`; `cmd_shell`, `harness:2696`).
- `scripts/lib/*.sh`: cross-cutting bash helpers.

**Testing:**
- `tests/*_test.sh`: per-area suites (proxy, harness, firewall, mcp,
  persistence, scheme_contract, integration, full_pipeline, upgrade, podman).
- `proxy/test_proxy.py`: proxy unit tests.
- `tests/mock_upstream.py`, `tests/fixtures/`, `tests/lib/`.

## Naming Conventions

**Files:**
- CLI subcommand handlers: `cmd_<name>()` inside `harness`.
- Proxy private helpers: leading underscore (`_normalize_api_base`,
  `_scan_balanced_json`, `_setup_prompt_mode`).
- Test suites: `<area>_test.sh`; unit suites `unit_<area>_test.sh`.
- Architecture docs: lowercase-hyphen `.md` matching the module.

**Directories:**
- Per-module top-level dirs named for their artifact (`proxy/`, `agents/`,
  `firewall/`).
- Per-entry MCP dirs keyed by service name (`mcp-registry/serena/`,
  `state/mcp/<name>/`).

## Where to Add New Code

**New proxy behavior (translation, prompt mode, tool handling):**
- Primary code: `proxy/proxy.py` (single file by design).
- Tests: `proxy/test_proxy.py` (unit) and/or `tests/proxy_test.sh`
  (integration via the proxy container).
- Update `architecture/proxy.md` in the same commit if behavior changes.

**New CLI subcommand or flag:**
- Implementation: a `cmd_<name>()` in `harness`; register it in `cmd_help`
  (the source of truth for the subcommand list, `harness:1196`) and `main()`.
- Tests: `tests/harness_test.sh` (or a new `tests/harness_*_test.sh`).
- Update `architecture/harness-cli.md`.

**New shared bash helper:**
- Add to the matching `scripts/lib/*.sh` (`platform.sh`, `net_helpers.sh`,
  `upgrade_actions.sh`), keeping `net_helpers.sh` lazily sourced.

**New MCP service:**
- Vetted/shared: `mcp-registry/<name>/` with the four-file contract
  (`compose.yml`, `client-config.json`, `harness-meta.json.template`,
  `README.md`); add a `registry_actions` entry to
  `scripts/upgrade-manifest.json`.
- One-off/private: `harness mcp register <name> --from <dir|git-url>` (no repo
  commit). See `architecture/mcp.md`.

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

**`mcp-registry/`:**
- Purpose: Repo-tracked MCP definitions (source for `harness mcp install`).
- Generated: No.
- Committed: Yes.

## Monolith Note

Two files carry most of the system and are intentionally single-file:

- `harness` — ~5481 lines (`wc -l`). One bash script: self-locate, env load,
  compose wrapper, runtime override, agent launch, net, MCP, doctor/preflight,
  update/upgrade. `cmd_help` is the canonical subcommand list.
- `proxy/proxy.py` — ~1870 lines (`wc -l`). One Flask app: translation,
  cooperative-prompt modes, tool-call extraction, SSE/JSON emission, config
  validation, debug dumps.

When adding to either, follow the existing in-file section ordering rather
than splitting the file.

---

*Structure analysis: 2026-06-02*
