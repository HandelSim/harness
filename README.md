# harness

A docker-based runtime that lets you launch a coding agent (claude-code,
opencode) against a third-party API endpoint, transparently. The agent runs
in a container, talks to a local ollama instance, and ollama forwards chat
requests to a translating proxy that calls the upstream API.

```
agent container ──► ollama ──► proxy ──► upstream API

  • ollama: registers a stub model that forwards via RemoteHost to the proxy
  • proxy:  translates between ollama's wire format and the upstream's, AND
            injects tool-use instructions / parses tool calls (since the
            upstream doesn't natively support tool calls)
```

This repo is the source for the harness runtime AND the installer. The
installer clones the repo into `./harness/` (the install root); code,
user config, and runtime state all live inside it.

## Installation

Download `harness-install.sh` and run it from an empty directory:

```bash
mkdir -p ~/harness-install && cd ~/harness-install
curl -fsSL -o harness-install.sh https://raw.githubusercontent.com/HandelSim/harness/main/harness-install.sh
bash harness-install.sh
```

The installer clones the repo into `./harness/` (the install root), seeds
`.env` and `.harness-allowlist` from their `.example` templates, and writes
a `harness` wrapper to `~/.local/bin/harness`.

(On Windows, use Git Bash. See [docs/WINDOWS.md](docs/WINDOWS.md) for
Windows-specific setup.)

After install:
1. Edit `~/harness-install/harness/.env` and set `PROXY_API_KEY` (and any
   other required values for your upstream).
2. cd into a project directory and run `harness claude` or `harness opencode`.

To uninstall:
```bash
rm -rf ~/harness-install/harness
rm ~/.local/bin/harness
```

## Repo structure

```
harness/
├── harness                  management CLI
├── harness-install.sh       bootstrap installer (run once after cloning)
├── docker-compose.yml       services: ollama, proxy, agents
├── .env.example             documented env variables (copy to ./.env at the install root)
├── ollama/                  custom ollama image + entrypoint that registers
│                            the stub model with RemoteHost set to the proxy
├── proxy/                   the translating proxy
├── agents/                  unified agent image (Dockerfile + entrypoint
│                            with mode dispatch: claude, opencode, shell)
├── mcp-registry/            vetted MCP service definitions
└── scripts/
    ├── proxy_test.sh        proxy translation tests (incl. ollama RemoteHost forwarding smoke)
    ├── harness_test.sh      management script tests
    ├── persistence_test.sh  persistent home + skel-seed test
    ├── mcp_test.sh          MCP install/enable/disable/uninstall lifecycle test
    ├── firewall_test.sh     firewall guardrail (negative) + bypass
    ├── upgrade_test.sh      upgrade actions library + synthetic version transition
    ├── full_pipeline_test.sh end-to-end install → run → print-mode round-trip
    ├── integration_test.sh  end-to-end Serena MCP + Graphify skill (HARNESS_RUN_SLOW=1)
    ├── lib/                 sourceable test toolkits (test_helpers, net_helpers)
    └── fixtures/
        ├── responses/       mock_upstream fixture dispatch table
        └── test-project/    small Python calculator package used by integration_test.sh
```

## Persistent agent home

A single bind-mounted home — `<install-root>/state/agent/home/` — backs
every agent invocation (claude, opencode, shell). Anything a user installs
inside an agent (`pipx install graphifyy`, `pip install --user requests`,
custom dotfiles) is visible across all modes and survives container
rebuilds. The image's build-time home contents are snapshotted into
`/etc/skel/harness/`, and the entrypoint copies them into an empty bind
mount on first run, marking with `~/.harness-home-initialized` so
subsequent runs skip the seed.

## MCP registry

Long-running MCP servers — Serena, etc. — are described by a small set of
files under `mcp-registry/<name>/`:

- `compose.yml` — partial compose snippet defining the service. References
  the `harness_harness-net` network as external so it merges cleanly with
  the main `docker-compose.yml`. Lives behind the `mcp` profile so
  `docker compose up` without the profile leaves it alone.
- `client-config.json` — the entry that gets merged into the agent's MCP
  config. Uses the `{"mcpServers": {"<name>": {...}}}` shape.
- `README.md` — what the service does, what it mounts, security notes.

### Lifecycle

The MCP lifecycle has four state-changing verbs and a few inspection verbs.
The state diagram is:

```
available ──install──► installed-enabled ⇄ disable / enable ⇄ installed-disabled ──uninstall──► available
```

Per-install state lives in `<install-root>/state/mcp/<name>/harness-meta.json`
(`{"enabled": true|false}`).

| Verb                                  | What it does                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------------- |
| `harness mcp install <name>`          | Copy registry entry → active tree, set `enabled: true`. Re-install needs `--force`. |
| `harness mcp uninstall <name> --force` | Remove the active entry. `data/` is preserved.                              |
| `harness mcp enable <name>`           | State toggle: set `enabled: true`. `harness start` will include it.          |
| `harness mcp disable <name>`          | State toggle: set `enabled: false`. Files stay; `harness start` skips it.    |
| `harness mcp up <name>`               | Manually start the container (works even if disabled).                       |
| `harness mcp down <name>`             | Manually stop the container without flipping `enabled`.                      |
| `harness mcp logs <name>`             | `docker compose logs -f` for the MCP's services.                             |
| `harness mcp status <name>`           | Print state, enabled flag, runtime status, paths, services.                  |
| `harness mcp list`                    | Installed entries with `STATE` column.                                       |
| `harness mcp list --available`        | Installed entries plus registry entries not yet installed.                   |

The four lifecycle verbs (`install` / `uninstall` / `enable` / `disable`)
are distinct and idempotent. `enable`/`disable` only flip the auto-start
flag on an already-installed entry; they do not install or uninstall.

### Adding new MCPs

Currently supported: HTTP/SSE MCPs in Docker containers (Pattern A). To
add a new MCP, fork the repo and add a directory under `mcp-registry/<name>/`
containing:

- `compose.yml` — partial compose snippet defining the service. References
  the `harness_harness-net` network as external so it merges cleanly with
  the main `docker-compose.yml`. Lives behind the `mcp` profile so
  `docker compose up` without the profile leaves it alone.
- `client-config.json` — the entry that gets merged into the agent's MCP
  config. Uses the `{"mcpServers": {"<name>": {...}}}` shape.
- `harness-meta.json.template` — metadata. Materialized into the active
  tree as `harness-meta.json` on install.
- `README.md` — what the MCP does and any required env vars.

See `mcp-registry/serena/` as the reference example. Submit a PR to add
to the official registry. For private/internal MCPs, fork the repo and
maintain your own registry entry.

### Installing skills (graphify, etc.)

Skills are CLI tools the agent invokes. To install one:

**Easy way (recommended):** ask the agent. From inside `harness claude`:

> Please install graphify by running `pipx install graphifyy` and then
> `graphify install`.

The agent runs the commands inside the container, registers the skill,
and confirms.

**Direct way:** run `harness shell` to drop into a bash shell inside the
agent container. Run the install commands yourself, exit. The shell
shares the same persistent home as `harness claude` and `harness
opencode`, so the install is available everywhere.

Either way, the install persists across container restarts and
`harness upgrade`.

## Universal egress firewall

Every container on `harness-net` boots with iptables/ipset rules that drop
egress except to:

- DNS (UDP/53)
- the configured `PROXY_API_URL` host (resolved at boot)
- entries in `<install-root>/.harness-allowlist` (one host per line; lines
  ending `# git-push` are also allowed to reach the SSH/HTTPS git ports of
  that host)

The image built from `firewall/init-firewall.sh` runs as a privileged
init-container per service (`NET_ADMIN`, `NET_RAW`). The seed allowlist
ships as `.harness-allowlist.example`; `harness-install.sh` copies it to
`<install-root>/.harness-allowlist` on first run.

### `harness net` — managing the allowlist + bypass overrides

```
harness net list                        # show every host (pull/push)
harness net allow github.com --git-push # add a host
harness net deny example.com            # remove a host
harness net edit                        # open in $EDITOR
harness net status                      # allowlist size + open services
harness net open <service>              # disable firewall for one service
harness net close <service>             # restore the firewall
```

`net open` requires you to type the literal phrase `I understand the risks`
on a TTY prompt — scripts cannot bypass this. `<service>` is one of
`proxy`, `ollama`, `agent`, or any installed MCP service. State lives in
`<install-root>/.harness-net-overrides.json` (managed by the script;
override the path via `HARNESS_NET_OVERRIDES_PATH` for tests). Run
`harness restart` after any mutation to apply it to live containers.

### `--net` per-launch bypass

```
harness claude --net      # this launch only — full outbound network
```

Sets `HARNESS_FIREWALL_DISABLED=1` for the agent container only; the next
launch (without `--net`) goes back to the universal firewall. A loud
warning prints to stderr when the flag is in effect.

### `harness doctor [network]`

`harness doctor` reports a `[network]` section listing the allowlist path,
host count, whether `PROXY_API_URL`'s host is on the list, and any
services with active overrides.

### MCP `allowed_domains`

If a registry MCP's `harness-meta.json.template` declares
`allowed_domains: ["api.example.com", ...]`, `harness mcp install` prints a
recommendation block with the matching `harness net allow` commands. The
allowlist is **never** modified automatically — the user copy-pastes what
they actually want.

## Layout

The clone IS the install root. Code, user config, and runtime state all
live inside it; user config and `state/` are gitignored:

```
<install-root>/                 the git clone (e.g. ~/harness/)
├── .git/                       managed by `harness update` / `harness upgrade`
├── harness-install.sh, harness, docker-compose.yml, ...   tracked code
├── .env                        your config (gitignored)
├── .harness-allowlist          egress allowlist (gitignored)
├── .harness-net-overrides.json firewall overrides (gitignored)
└── state/                      runtime state (gitignored)
    ├── output/                 proxy debug dumps
    ├── agent/{claude,opencode}/ persistent /home/harness for each agent
    ├── ollama-data/            ollama model blobs
    └── mcp/<name>/             active MCP services (compose.yml + data)
```

To uninstall: `rm -rf <install-root> && rm ~/.local/bin/harness`.

## Local development

The `.env` file lives inside the clone:

```
$ cp .env.example .env
$ $EDITOR .env          # fill in PROXY_API_URL / PROXY_API_KEY / PROXY_API_MODEL
$ docker compose --env-file .env up --build
```

To expose ollama on the host (useful for poking at it from outside the docker
network), set `PUBLISH_OLLAMA_PORT=11434` in `.env`.

### Iterating on the proxy

If you're modifying `proxy/proxy.py` to debug or refine its behavior, you
can rebuild and restart just the proxy service without touching anything
else:

```bash
docker compose --project-name harness restart proxy
```

This picks up your edits in ~10-15 seconds without affecting ollama, agents,
or MCP services. Faster than `harness restart` for the proxy-iteration loop.

## Updating

`harness update` runs a fast-forward `git pull` in the clone. Use this when
all you want is the latest harness code with no side effects.

`harness upgrade` runs the full upgrade flow:

1. `git pull --ff-only` in the clone
2. Apply upgrade actions from `scripts/upgrade-manifest.json` to your install
   root
3. `harness down --remove-orphans` and `harness start`

Upgrade actions are conservative: they add new env variables, new allowlist
hostnames, and new config keys WITHOUT overwriting your customizations.
Each newly-introduced item is annotated with a marker comment
(`# Added by harness upgrade on YYYY-MM-DD`) so you can spot what changed.

```
$ harness upgrade --check         # preview only (no git pull, no writes)
$ harness upgrade --no-prompt     # apply without the [Y/n] prompt
$ harness upgrade --no-restart    # apply without down/start (e.g. CI)
```

### How the manifest works

The manifest at `scripts/upgrade-manifest.json` is the contract between the
upstream repo and your local install root. Since B4 the install root IS the
clone, so "managed files" means files harness writes inside the clone that
aren't tracked git content (`.env`, `.harness-allowlist`, `state/mcp/<name>/`,
the ccstatusline config under `state/agent/home/`). Every `B3-MANAGED:`
comment in the codebase has a matching manifest entry (see audit step in
Phase B3 docs).

Action types:

- **envfile_merge** — appends new `KEY=VALUE` entries from the source to the
  target, preserving existing values and surfacing the source's preceding
  comments for context. Used for `.env`.
- **linefile_merge** — appends new entries (one per line, `#` comments
  ignored). If an entry exists in both files but with different inline
  annotations (e.g. `# git-push`), the user's line is preserved and a
  warning is emitted. Used for `.harness-allowlist`.
- **json_merge** — recursively adds keys present in source but missing in
  target. User values win at every depth; arrays are treated as scalars
  (user-wins, no element merging). Used for ccstatusline config.
- **directory_overwrite** — refreshes a managed directory tree, with an
  explicit `preserve` list for paths inside the directory that are user or
  system state (typically `harness-meta.json`, `data/`). Files in target
  that don't exist in source are left in place. Used for installed MCP
  registry definitions.

Files harness manages (covered by the manifest):

- `.env` — env vars merged in (preserves your values)
- `.harness-allowlist` — new hosts appended (preserves your entries and
  any `# git-push` annotations)
- `state/agent/home/.config/ccstatusline/settings.json` — new widgets/keys
  added (preserves layout and user widget customizations)
- `state/mcp/<name>/` — definition files (`compose.yml`, `client-config.json`,
  `README.md`) updated (preserves `harness-meta.json` enable state and
  `data/` indexed state)

Files purely user-managed (not in the manifest):

- `.harness-net-overrides.json` — controlled by `harness net open/close`
- `state/output/` — proxy debug dumps
- `state/ollama-data/` — model blobs
- **User-installed skills and `pipx` packages** under `state/agent/<tool>/`
  (e.g., `state/agent/home/.local/bin/graphify`, `state/agent/home/.claude/skills/graphify/`).
  These live entirely inside the bind-mounted agent home and are never
  touched by upgrade actions. The skel-seed step in the agent entrypoint
  only runs once per home (gated by `~/.harness-home-initialized`), so
  reinstalling the image during `harness upgrade` does not re-seed over
  user files.
- **User-added MCPs** dropped manually under `state/mcp/<name>/` (i.e. an
  MCP that did not come from the registry). The `directory_overwrite`
  action only fires for entries that have a corresponding source under
  `mcp-registry/<name>/`; with no source present the user dir is left
  alone. Discovery is directory-driven — `harness mcp list` and the
  compose merge scan `state/mcp/*/` regardless of registry origin, so a
  custom MCP shows up alongside the registry-installed ones.

To force a full reset of a harness-managed file (and lose customizations):
delete the file in your install root, then run `harness upgrade`. The
target-missing branch of each action will recreate it from source.

## Customizing the claude status line

The claude image pre-installs `ccstatusline` and seeds a curated default
config (model + cwd + git branch + context bar) into
`/etc/skel/harness/.config/ccstatusline/settings.json`. The entrypoint
also merges a `statusLine` block into `~/.claude/settings.json` so claude
calls `ccstatusline` on each render.

To change the status line, run:

```
harness claude-statusline-config
```

This launches an ephemeral claude container (no ollama/proxy required)
attached to the persistent agent home; the ccstatusline TUI writes its
edits straight to `<install-root>/state/agent/home/.config/ccstatusline/settings.json`.

## Tests

```
$ bash scripts/proxy_test.sh         # proxy translation, RemoteHost forwarding, stub model context length
$ bash scripts/harness_test.sh       # management script subcommands
$ bash scripts/persistence_test.sh   # persistent home + skel seed
$ bash scripts/mcp_test.sh           # MCP install/enable/disable/uninstall lifecycle
$ bash scripts/firewall_test.sh      # firewall guardrail (negative) + per-service bypass
$ bash scripts/upgrade_test.sh       # upgrade actions library + synthetic version transition
$ bash scripts/full_pipeline_test.sh # full install + run pipeline (covers both agents via print-mode round-trip)
$ HARNESS_RUN_SLOW=1 bash scripts/integration_test.sh  # end-to-end Serena + Graphify (slow, ~10-15 min)
```

### Integration test (Serena + Graphify)

`scripts/integration_test.sh` is the canonical regression test for the two
flagship integrations: Serena (Pattern A — HTTP MCP server) and Graphify
(Pattern B — pipx-installed skill CLI). It is gated behind
`HARNESS_RUN_SLOW=1` because the first run pulls/builds the ~2 GB Serena
image, so the default test suite stays fast.

The test exercises four phases against a clean install in a temp directory:

1. **Stack setup** — `harness start`, build agent images, attach a
   mockupstream sidecar to `harness-net`, run `harness claude -p "say hello"`
   end-to-end through the proxy.
2. **Serena (HTTP MCP)** — install → restart → reach on tcp://serena:9121
   from the proxy → trigger an agent launch and verify
   `state/agent/home/.harness-mcp-servers.json` has the merged entry →
   confirm `/workspaces/projects/test-project/` is visible inside the serena container
   → drive a TUI claude that asks serena to find the `Calculator` symbol →
   `mcp down/up`, `mcp disable/enable`, `mcp uninstall --force`.
3. **Graphify (skill)** — launch a long-lived agent container with the same
   firewall + UID-remap entrypoint as the real harness → `pipx install
   graphifyy` → confirm the binary is visible from the host bind mount →
   `graphify install` registers `~/.claude/skills/graphify/SKILL.md` →
   `graphify update .` on the test-project fixture writes `graphify-out/graph.json`
   containing `Calculator` and `ScientificCalculator` → assert the output
   files are owned by the host UID (not container uid 1000) → tear the
   container down and confirm a fresh container can still call graphify
   from the persistent home.
4. **Cross-test invariants** — `harness doctor` returns 0 and the state
   directory layout is intact.

The fixture project at `scripts/fixtures/test-project/` is a small Python
calculator package (Calculator, ScientificCalculator, ExpressionParser, an
exception hierarchy, and pytest tests) that gives both Serena and Graphify
a non-trivial multi-module symbol graph to chew on.

### Test toolkits

`scripts/lib/` ships sourceable bash libraries shared across tests:

- `test_helpers.sh` — common setup: `require_docker`, `test_section`,
  `test_generate_env`, `test_generate_mockupstream_override` (mounts the
  fixture-dispatch directory), `test_wait_for_healthy`, `test_cleanup`.
  `integration_test.sh` adds two helpers used when the harness compose
  owns the network: `test_start_mockupstream` (one-shot `docker run` of
  the mock joined to `<project>_harness-net` with the alias `mockupstream`)
  and `test_wait_for_container_healthy` for non-compose containers.

### Mock-upstream fixture dispatch

`scripts/mock_upstream.py` supports two modes:

- **Legacy** — `MOCK_SCENARIO=text|tool` env var picks one of two canned
  responses. Used by `proxy_test.sh` and `firewall_test.sh`.
- **Fixture dispatch** — set `MOCK_FIXTURES_DIR=/fixtures` and mount
  `scripts/fixtures/responses/` there. The mock loads every `*.json`
  lexicographically and matches the most recent user message against each
  fixture's `match` regex; first match wins. `99_default.json` is the
  catch-all. See `scripts/fixtures/responses/README.md` for the file shape
  and naming convention (`NN_short_slug.json`, with reserved priority
  ranges per scenario family).
