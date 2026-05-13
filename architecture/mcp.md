# MCP registry and lifecycle

Long-running MCP services live under `mcp-registry/<name>/` (in-repo,
vetted) and `state/mcp/<name>/` (per-install, active). The harness CLI
manages copying between them and merging their compose snippets and
client configs into the runtime.

## Registry layout

Every registry entry under `mcp-registry/<name>/` has:

- **`compose.yml`** — a partial compose snippet defining the service.
  References the `harness_harness-net` network as `external` so it merges
  cleanly with the main `docker-compose.yml`. Lives behind the `mcp`
  profile so `docker compose up` without the profile leaves it alone.
- **`client-config.json`** — the entry merged into the agent's MCP
  config. Uses claude's `{"mcpServers": {"<name>": {...}}}` shape.
  Translated for opencode by the agent entrypoint (see
  [`containers.md`](containers.md)).
- **`harness-meta.json.template`** — metadata; materialized as
  `harness-meta.json` in the active tree on install. Optional
  `repo_clone_url` (and `repo_clone_ref`, default `main`) make
  `harness mcp install` git-clone the upstream repo into
  `state/mcp/<name>/repo/`; the compose snippet then uses that as the
  local build context. Avoids docker's git-URL build-context handling,
  which fails on Windows. `repo_clone_ref` accepts a branch, a tag, or
  a full 40-character SHA; short SHAs are not supported (git's smart-http
  fetch protocol rejects them). Branch/tag refs use a depth-1 clone; full
  SHAs use `git init` + `git fetch --depth=1 <sha>` + checkout `FETCH_HEAD`.
- **`README.md`** — what the service does, what it mounts, security
  notes. Operator-facing.

`mcp-registry/serena/` is the reference example.

## State machine

```
available ──install──▶ installed-enabled ⇄ disable/enable ⇄ installed-disabled ──uninstall──▶ available
```

The four lifecycle verbs (`install` / `uninstall` / `enable` / `disable`)
are distinct and idempotent. `enable`/`disable` only flip the auto-start
flag on an already-installed entry; they do not install or uninstall.

| Verb                                  | Effect |
|---------------------------------------|--------|
| `harness mcp install <name>`          | Copy registry entry → active tree, set `enabled: true`. Re-install needs `--force`. |
| `harness mcp uninstall <name> --force`| Remove the active entry. `data/` is preserved. |
| `harness mcp enable <name>`           | `enabled: true`. `harness start` will include it. |
| `harness mcp disable <name>`          | `enabled: false`. Files stay; `harness start` skips it. |
| `harness mcp up <name>`               | Start container immediately (works even if disabled). |
| `harness mcp down <name>`             | Stop without flipping `enabled`. |
| `harness mcp logs <name>`             | `compose logs -f` for the MCP's services. |
| `harness mcp status <name>`           | Print state, enabled flag, runtime status, paths, services. |
| `harness mcp list [--available]`      | Installed (and registry-not-yet-installed with `--available`). |

## State storage: `harness-meta.json`

`<install-root>/state/mcp/<name>/harness-meta.json`:

```json
{"enabled": true | false, "repo_clone_url": "...", "repo_clone_ref": "..."}
```

`mcp_is_installed(name)` is "compose.yml exists in the active tree" —
not "the directory exists". `data/` survives uninstall, so a directory-
existence check would refuse a clean re-install after an uninstall.

`mcp_is_enabled(name)` returns true when the meta file is missing so
legacy installs from before this metadata existed continue to behave as
enabled. The jq read uses an explicit null check (`if .enabled == null
then "true" else ...`) because jq's `//` treats `false` as missing.

Writes are atomic (temp file + `mv`) so a crashed write can't leave
half-parsed JSON.

## How installed MCPs reach the runtime

### Compose merge

`mcp_compose_files` enumerates every enabled entry under
`state/mcp/<name>/` and emits `-f <path>/compose.yml` flags. The `compose`
wrapper splices these into every `docker compose` invocation, alongside
the main `docker-compose.yml` and the runtime override. Each snippet
references `harness_harness-net` as external, so the network must be
already up — that is, `harness start` must have brought the main compose
up first. `any_mcp_active` decides whether `harness start` adds
`--profile mcp` to its `up` command.

### Client config merge

On each agent launch, the harness CLI writes
`state/agent/home/.harness-mcp-servers.json` by merging every
enabled MCP's `client-config.json`. The agent entrypoint then:

- Folds it into `~/.claude.json` (`merge_claude_mcp_servers`), or
- Translates and folds into `~/.config/opencode/opencode.json`
  (`merge_opencode_mcp_servers`) — claude's
  `{"mcpServers": {<name>: {"url" | "command"}}}` becomes opencode's
  `{"mcp": {<name>: {"type": "remote"|"local", ...}}}`.

Re-merging on every container start means disable propagates without
needing to clean up old entries.

## Upgrade behavior (`directory_overwrite`)

`scripts/upgrade-manifest.json` declares one `directory_overwrite` action
per registry entry that this repo intends to manage upstream. When
`harness upgrade` runs, that action refreshes
`state/mcp/<name>/compose.yml`, `client-config.json`, `README.md`, etc.
from the freshly-pulled `mcp-registry/<name>/`, while explicitly
preserving:

- `harness-meta.json` — your enable/disable state
- `data/` — indexed/persistent state

Files in target that don't exist in source are left in place. Action
only fires when `condition: installed` matches (`mcp_is_installed`
returns true). User-added MCPs dropped manually under `state/mcp/<name>/`
without a matching `mcp-registry/<name>/` source are left alone —
`directory_overwrite` only fires for registry-sourced entries. Discovery
is directory-driven, though, so a custom MCP still shows up in
`harness mcp list` and gets its compose snippet merged.

## `allowed_domains` recommendation

If a registry MCP's `harness-meta.json.template` declares
`allowed_domains: ["api.example.com", ...]`, `harness mcp install` prints
a recommendation block with the matching `harness net allow` commands.
**The allowlist is never modified automatically** — the user copy-pastes
what they actually want. This is deliberate: MCPs are third-party code,
and the security posture is "the operator explicitly opens the egress
they need."

## Adding a new MCP

Currently supported: HTTP/SSE MCPs in Docker containers (Pattern A).
Fork the repo and add `mcp-registry/<name>/` containing the four files
listed above. See `mcp-registry/serena/` as the reference. To make the
new entry follow `harness upgrade`, add a matching `registry_actions`
entry to `scripts/upgrade-manifest.json`. Submit a PR to the official
registry; for private MCPs, maintain your own fork.
