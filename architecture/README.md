# Architecture

Structural overview of the harness system, plus an index to the per-module
deep-dives that live alongside this file.

## What harness is

A container-runtime-based system that lets a coding agent (opencode) talk
to a third-party API endpoint transparently. The agent runs in a container,
calls a local ollama instance, and ollama forwards chat requests to a
translating proxy that calls the upstream API.

```
┌────────────────┐   /api/chat   ┌────────┐   chat-completions   ┌──────────┐
│ agent          │ ────────────▶ │ ollama │ ───────────────────▶ │ proxy    │ ──▶ upstream API
│ (opencode)     │               │  stub  │  (RemoteHost=proxy)  │ flask    │
│                │ ◀──ndjson──── │ model  │ ◀────────────────────│ app      │
└────────────────┘               └────────┘                      └──────────┘
```

Three long-running services share the `harness-net` bridge network and a
universal egress firewall: `ollama`, `proxy`, and any enabled MCP
service. Agents are short-lived containers spawned by `harness` /
`harness opencode` / `harness shell`; they join the same network for the
duration of an invocation.

## Repo IS the install root

The clone of this repo IS the install root. Code, user config (`.env`,
`.harness-allowlist`), and runtime state (`state/`) all live inside the
clone. User config and `state/` are gitignored. `harness update` and
`harness upgrade` operate on this same clone via `git pull --ff-only`.

```
<install-root>/                 the git clone
├── .git/                       managed by `harness update` / `harness upgrade`
├── harness                     management CLI (one big bash script)
├── harness-install.sh          bootstrap installer
├── docker-compose.yml          service definitions
├── proxy/                      translating proxy (Flask)
├── ollama/                     custom ollama image + stub-model entrypoint
├── agents/                     agent image (opencode/shell)
├── firewall/                   universal egress firewall + git-creds
├── mcp-registry/<name>/        vetted MCP definitions
├── scripts/lib/                shared bash libraries (platform, net, upgrade)
├── scripts/upgrade-manifest.json   how `harness upgrade` brings forward state
├── tests/                      see architecture/tests.md
├── .env                        gitignored — user config
├── .harness-allowlist          gitignored — egress allowlist
└── state/                      gitignored — runtime state
    ├── output/                 proxy debug dumps
    ├── agent/home/             shared /home/harness bind-mounted into every agent
    ├── ollama-data/            ollama model blobs
    └── mcp/<name>/             active MCP services (compose + data)
```

## Per-module architecture docs

Read the relevant one(s) before changing code in that area:

- [`harness-cli.md`](harness-cli.md) — the `harness` bash CLI: self-locate,
  env loading, subcommand layout, runtime-override generation, doctor /
  preflight, agent launch path (`docker run` from run_agent / cmd_shell).
- [`proxy.md`](proxy.md) — `proxy/proxy.py`: ollama ↔ upstream translation,
  cooperative tool-use prompt variants, tool-call extraction, NDJSON
  streaming, env-driven config and validation.
- [`upstream-api.md`](upstream-api.md) — the third-party chat API the proxy
  calls: observed contract and quirks (no tools, hidden system prompt, no
  network), API-key lock/expiry lifecycle, request/response schema, HTTP
  status codes.
- [`containers.md`](containers.md) — service composition: `docker-compose.yml`,
  ollama stub-model registration, agent entrypoint (UID remap, firewall,
  gosu drop, skel-seed, mode dispatch), `harness-net` + firewall
  integration.
- [`mcp.md`](mcp.md) — MCP registry layout, install/enable/disable/uninstall
  state machine, `harness-meta.json` schema, how registry MCPs get merged
  into the runtime compose graph and into the agent's client config.
- [`install-and-upgrade.md`](install-and-upgrade.md) — `harness-install.sh`
  (clone → seed → wrapper), the upgrade manifest at
  `scripts/upgrade-manifest.json`, and `scripts/lib/upgrade_actions.sh`
  (envfile_merge / linefile_merge / directory_overwrite). Where the
  B3-MANAGED comment markers anchor.
- [`tests.md`](tests.md) — test layout and conventions. The detailed
  inventory and coverage tables live alongside the tests at
  `tests/INVENTORY.md` and `tests/COVERAGE.md`.

## Other in-tree READMEs (still load-bearing)

These are scoped to their directory and not duplicated in the architecture
docs:

- `firewall/README.md` — operator-facing notes on allowlist format and
  bypass; cross-referenced from `architecture/containers.md`.
- `mcp-registry/serena/README.md` — the reference MCP registry entry;
  cross-referenced from `architecture/mcp.md`.
- `tests/benchmarks/README.md`,
  `tests/fixtures/responses/README.md` — area-specific test guides;
  cross-referenced from `architecture/tests.md`.
- `docs/PODMAN.md`, `docs/WINDOWS.md` — runtime-specific operator notes.

## Keeping these docs honest

These docs are short and structural on purpose — they describe shape,
contracts, and load-bearing invariants, not every function. After a
change that alters behavior covered by one of these docs, update the
doc in the same commit. The `implementing.md` workflow file enforces
this for the agent.
