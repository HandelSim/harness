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
  config. Uses the canonical `{"mcpServers": {"<name>": {...}}}` shape.
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

## Pin policy for registry entries

Registry entries with a `repo_clone_url` SHOULD pin `repo_clone_ref` to
an upstream release tag (e.g. `v1.3.0`) rather than tracking `main`.
Tracking `main` makes the harness integration test a moving target —
upstream churn lands here as flaky CI even when nothing in this repo
changed. Pinning to a tag gives reproducible builds and turns "bump
this MCP" into an explicit, reviewable commit. Bump on a cadence (or
when upstream cuts a tag with a fix we need). The reference example
`mcp-registry/serena/` follows this — pinned to `v1.3.0`.
- **`README.md`** — what the service does, what it mounts, security
  notes. Operator-facing.

`mcp-registry/serena/` is the reference example.

## State machine

```
available ──install/register──▶ installed-enabled ⇄ disable/enable ⇄ installed-disabled ──uninstall──▶ available
```

`install` and `register` both move an entry from `available` to
`installed`; they differ only in source. `install` copies a repo-tracked
`mcp-registry/<name>/`; `register` lands an arbitrary external source (a
registry-shaped dir or a git URL) **behind a compose-merge validation
gate** (see [Dynamic registration](#dynamic-registration-register) below).
`enable`/`disable` only flip the auto-start flag on an already-installed
entry; they do not install or uninstall. `uninstall` is the inverse of
both install and register — there is no separate `unregister`.

| Verb                                  | Effect |
|---------------------------------------|--------|
| `harness mcp install <name>`          | Copy registry entry → active tree, set `enabled: true`. Re-install needs `--force`. |
| `harness mcp register <name> --from <dir\|git-url>` | Materialize an external source → active tree behind a validation gate, set `enabled: true` (unless `--no-enable`). Re-register needs `--force`. |
| `harness mcp uninstall <name> --force`| Remove the active entry. `data/` is preserved. Inverse of install **and** register. |
| `harness mcp enable <name>`           | `enabled: true`. `harness start` will include it. |
| `harness mcp disable <name>`          | `enabled: false`. Files stay; `harness start` skips it. |
| `harness mcp up <name>`               | Start container immediately (works even if disabled). |
| `harness mcp down <name>`             | Stop without flipping `enabled`. |
| `harness mcp logs <name>`             | `compose logs -f` for the MCP's services. |
| `harness mcp status <name>`           | Print state, enabled flag, runtime status, paths, services. |
| `harness mcp list [--available]`      | Installed (and registry-not-yet-installed with `--available`). |
| `harness mcp host-init <name> [--port <port>] [--force]` | Scaffold a **host MCP** from `host-mcp/template/` into `host-mcp/<name>/` and register a client-config-only entry. Default port `9100`. See [Host MCPs](#host-mcps-non-container). |
| `harness mcp host-setup <name> [agent flags...]` | Launch an agent inside `host-mcp/<name>/` so it reads that folder's `AGENTS.md` and tailors the server with the user. |

## State storage: `harness-meta.json`

`<install-root>/state/mcp/<name>/harness-meta.json`:

```json
{"enabled": true | false, "repo_clone_url": "...", "repo_clone_ref": "..."}
```

`mcp_is_installed(name)` is "compose.yml **or** client-config.json exists
in the active tree" — not "the directory exists". The compose.yml arm
covers normal container MCPs; the client-config.json arm covers host MCPs
(see [Host MCPs](#host-mcps-non-container) below), which have no compose
snippet. `data/` survives uninstall, so a directory-existence check would
refuse a clean re-install after an uninstall.

`mcp_is_host(name)` is "client-config.json present **and** compose.yml
absent" — the test that distinguishes a host MCP from a container MCP. It
gates the `up`/`down`/`logs` host-message guards and the `status` transport
block.

`mcp_is_enabled(name)` returns true when the meta file is missing so
legacy installs from before this metadata existed continue to behave as
enabled. The jq read uses an explicit null check (`if .enabled == null
then "true" else ...`) because jq's `//` treats `false` as missing.

All meta reads and writes here (`mcp_is_enabled`, `_mcp_set_enabled_file`,
`mcp_print_firewall_recs`, the register provenance write) and the
`write_agent_mcp_config` client-config merge go through `harness_jq`, not
bare `jq`. That keeps docker the only host dependency: on a host without
host jq they run jq in the proxy container (see harness-cli.md
"`harness_jq` fallback"). `_mcp_set_enabled_file` flips `.enabled` through
`harness_jq` precisely so the meta's other fields survive the toggle; only
a missing meta (or no jq path at all) falls back to writing a minimal
`{"enabled": <value>}`. The merge slurps its entries via
`cat … | harness_jq -s` rather than as positional file args, because the
container fallback maps only a single trailing file arg into the container.

Writes are atomic (temp file + `mv`) so a crashed write can't leave
half-parsed JSON.

## Dynamic registration (`register`)

`harness mcp register <name> --from <dir|git-url> [--ref <ref>] [--no-enable]
[--start] [--force]` brings an MCP into the runtime **without a repo
commit**. Startup is already registry-independent — `mcp_compose_files`
re-reads `state/mcp/*/` on every `harness start` and includes any enabled
entry with a `compose.yml` — so registration reduces to one job: write a
**validated** `state/mcp/<name>/` from a user-supplied source. No startup
code changes; the auto-start guarantee is automatic.

`install` and `register` share `mcp_materialize_src` (copy descriptors →
target, clone any declared upstream build context → `<target>/repo`, set
the enabled flag) and `mcp_print_firewall_recs`, so the two commands
cannot drift.

**Sources (`--from`).** Same four-file contract as a registry entry
(`compose.yml` + `client-config.json` required; `harness-meta.json[.template]`
+ `README.md` optional):
- a **directory** shaped like a registry entry, or
- a **git URL** — cloned once at `--ref` into `<staging>/repo` (which
  doubles as the build context); descriptors are read from the repo root
  or a `harness-mcp/` subdir. The git-URL provenance is recorded into the
  staged `harness-meta.json` (`repo_clone_url`/`repo_clone_ref`).

**Materialize → validate → arm.** The source is materialized into a hidden
**staging dir** `state/mcp/.staging-<name>/` (invisible to every discovery
glob, which all use `*/` and skip dotdirs — so a crashed run never leaks
into the live set). It is only armed if the validation gate passes:

1. **Merge check** — `config -q` over (full current graph + staged snippet)
   under a throwaway project name. Catches bad YAML and graphs that fail to
   merge. This is the load-bearing check: every MCP snippet splices into ONE
   `docker compose up`, so a snippet that fails to merge would take the proxy
   and agent down with it.
2. **Collision check** — every **service name** AND **container_name** the
   staged snippet declares must be NEW. Compose silently *merges* same-named
   services (override semantics) and fails `up` on duplicate
   `container_name`; neither surfaces from `config -q`, so they are diffed
   explicitly against the live graph (both sides enumerated with
   `--profile mcp`, or profiled MCP services would be invisible). An empty
   service list fails closed.
3. **Port warning** — duplicate published host ports are warned (they fail
   only at `up` time), not blocked.

On any failure the staging dir is discarded and nothing in `state/mcp/`
changed. On success the staged non-`data` files are swapped into
`state/mcp/<name>/` while `data/` is left untouched in place (a crash can
never orphan it), and `data/` is (re)created.

**Why validate-then-enable** (not disabled-by-default): the only failure
mode that affects *other* services is a snippet that fails to merge, and
the gate closes that hole at register time. A single MCP's runtime crash is
isolated by compose (`restart: unless-stopped`). So defaulting to enabled is
safe and matches the goal "make sure they come up when harness does".
`--no-enable` stages without arming; `--start` boots it immediately via
`cmd_mcp_up`.

**Shadow warning.** If `<name>` also names a `mcp-registry/` entry, register
warns: a registered entry that later shares a registry name would start
being managed by `harness upgrade` (`directory_overwrite` fires on
`condition: installed`), silently overwriting the registered files. Use a
distinct name.

## How installed MCPs reach the runtime

### Compose merge

`mcp_compose_files` enumerates every enabled entry under
`state/mcp/<name>/` and emits `-f <path>/compose.yml` flags. The `compose`
wrapper splices these into every `docker compose` invocation, alongside
the main `docker-compose.yml` and the runtime override. Each snippet
references `harness_harness-net` as external, so the network must be
already up — that is, `harness start` must have brought the main compose
up first. `any_mcp_active` decides whether `harness start` adds
`--profile mcp` to its `up` command **and whether `harness down` adds it to
the teardown** — a bare `compose down` leaves the profiled MCP container
running (the `-f` snippet is on the argv but the profile gates it out), which
then blocks network removal ("resource is still in use").

### Client config merge

On each agent launch, the harness CLI writes
`state/agent/home/.harness-mcp-servers.json` by merging every
enabled MCP's `client-config.json`. The agent entrypoint then translates
and folds it into `~/.config/opencode/opencode.json`
(`merge_opencode_mcp_servers`) — the canonical
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

If an MCP's `harness-meta.json[.template]` declares
`allowed_domains: ["api.example.com", ...]`, both `harness mcp install` and
`harness mcp register` print a recommendation block with the matching
`harness net allow` commands (shared `mcp_print_firewall_recs`).
**The allowlist is never modified automatically** — the user copy-pastes
what they actually want. This is deliberate: MCPs are third-party code,
and the security posture is "the operator explicitly opens the egress
they need."

## Host MCPs (non-container)

A **host MCP** runs as a plain process on the host machine, not in a
container harness manages. The motivating case: a harness agent runs in a
Linux container but the project needs a host-only toolchain to build (e.g.
Visual Studio / MSVC / CMake on Windows). The agent edits the source (the
project is bind-mounted into the container) and calls build/test/run tools
over HTTP at `http://host.docker.internal:<port>/mcp`; the host process runs
the real compiler and returns the output.

**It is not a docker service.** A host MCP's `state/mcp/<name>/` has a
`client-config.json` and a `harness-meta.json` but **no `compose.yml`**. The
absence of `compose.yml` is the whole mechanism:

- The client-config merge (`write_agent_mcp_config`) enumerates by
  `client-config.json` presence (+ `mcp_is_enabled`), so the host MCP's URL is
  wired into every agent launched after registration — automatically, with no
  new code. The URL form (`{"url": "..."}`, no `command`) makes the agent
  entrypoint translate it to opencode's `{"type": "remote", ...}`.
- `mcp_compose_files` / `any_mcp_active` require `compose.yml`, so a host MCP
  is invisible to `harness start`'s compose merge and profile decision — harness
  never tries to bring up a container for it.
- `mcp_is_installed` was broadened to "compose.yml OR client-config.json" so a
  host MCP counts as installed (shows in `list`/`status`, blocks a name clash).
- `mcp_is_host` ("client-config.json present, compose.yml absent") gates the
  `up`/`down`/`logs` guards (they print "this is a host MCP, run it on the host"
  and exit) and the `status` transport block (which prints the endpoint URL and
  the instance folder instead of services/data).

**Two directories per host MCP:**

- `host-mcp/<name>/` — the server source (copied from `host-mcp/template/`,
  with `__MCP_NAME__`/`__MCP_PORT__` substituted). In production
  `install_root == clone_dir`, so this lives in the repo; it is **gitignored**
  (`/host-mcp/*` with `!/host-mcp/template/`) because each instance is
  user-specific. This is what the user runs on the host and what `host-setup`
  bind-mounts for the tailoring agent.
- `state/mcp/<name>/` — the registration (`client-config.json` +
  `harness-meta.json`, `transport: "host"`, `host_port`,
  `allowed_domains: ["host.docker.internal"]`). No `data/`, no `compose.yml`.

**The template** (`host-mcp/template/`, git-tracked) is a FastMCP
streamable-http server (`mcp.server.fastmcp.FastMCP`, endpoint `/mcp`) with
stub CMake/CTest build tools, a `project.json`, `run.ps1`/`run.sh` launchers,
`requirements.txt`, and an `AGENTS.md` that briefs the `host-setup` agent on
how to interview the user and prune the tools.

**Setup flow** (the goal is one short, guided path):

1. `harness mcp host-init <name> [--port <port>]` — scaffold + register.
2. `harness mcp host-setup <name>` — agent reads `AGENTS.md`, tailors
   `server.py` + `project.json` with the user.
3. `harness net allow host.docker.internal` — open egress so the agent
   container can reach the host. On **Linux** hosts the agent container also
   needs `--add-host=host.docker.internal:host-gateway`; on Docker Desktop /
   Windows that name resolves automatically.
4. Run the server on the host (`run.ps1` / `run.sh`) and keep it up.

**Uninstall** removes `state/mcp/<name>/` (the registration); the container-
teardown step is gated on `compose.yml` so it is skipped. The `host-mcp/<name>/`
server source is **left in place** (it is the user's customized code; remove it
by hand if you want it gone). `harness upgrade`'s `directory_overwrite` only
fires for registry-sourced entries, so it never touches a host MCP.

## Adding a new MCP

Currently supported: HTTP/SSE MCPs in Docker containers (Pattern A), and
**host MCPs** (non-container processes on the host — see
[Host MCPs](#host-mcps-non-container)).

- **Vetted / shared:** fork the repo and add `mcp-registry/<name>/`
  containing the four files listed above (see `mcp-registry/serena/` as the
  reference). To make the new entry follow `harness upgrade`, add a matching
  `registry_actions` entry to `scripts/upgrade-manifest.json`. Submit a PR to
  the official registry; for private MCPs, maintain your own fork.
- **Dynamic / no fork:** `harness mcp register <name> --from <dir|git-url>`
  lands the same four-file contract into the active tree directly, behind the
  validation gate, without a repo commit. This is the right path for one-off,
  private, or experimental MCPs.
