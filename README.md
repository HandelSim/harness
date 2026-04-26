# harness

A docker-based runtime that lets you launch a coding agent (claude-code,
opencode) against a third-party API endpoint, transparently. The agent runs
in a container, talks to a local ollama instance, and ollama forwards chat
requests to a translating proxy that calls the upstream API.

```
agent container ──► ollama ──(RemoteHost forward)──► proxy ──► upstream API
```

This repo is the source for the harness runtime. End users install via a
zip distribution (built in Phase 4); this repo is for development.

## Repo structure

```
harness/
├── harness                  management CLI (Phase 4)
├── harness-install.sh       bootstrap installer (Phase 4; also bundled in zip)
├── zip-readme.md            README that ships in the distribution zip
├── docker-compose.yml       services: ollama, proxy, agents
├── .env.example             documented env variables (copy to ./.env at the install root)
├── ollama/                  custom ollama image + entrypoint that registers
│                            the stub model with RemoteHost set to the proxy
├── proxy/                   the translating proxy
├── agents/                  agent images (claude, opencode)
├── mcp-registry/            vetted MCP service definitions (Phase 6)
└── scripts/
    ├── derisk_test.sh       Phase 1 end-to-end smoke
    ├── proxy_test.sh        Phase 2 proxy translation tests
    ├── agent_test.sh        Phase 3 end-to-end via both agents
    ├── harness_test.sh      Phase 4 management script tests
    ├── persistence_test.sh  Phase 6 persistent home + skel-seed test
    ├── mcp_test.sh          MCP install/enable/disable/uninstall lifecycle test
    ├── firewall_test.sh     Phase B1/B2 universal egress firewall + bypass
    ├── full_pipeline_test.sh end-to-end install → run → tmux drive
    ├── integration_test.sh  Phase 7b: end-to-end Serena MCP + Graphify skill (HARNESS_RUN_SLOW=1)
    ├── lib/                 sourceable test toolkits (tui_driver, test_helpers, net_helpers)
    ├── fixtures/
    │   ├── responses/       mock_upstream fixture dispatch table (Phase 7a)
    │   └── test-project/    small Python calculator package used by integration_test.sh
    └── build_zip.sh         produces dist/harness-distribution.zip
```

## Persistent agent homes

The agent containers' entire `/home/harness` is bind-mounted from
`<install-root>/state/agent/<tool>/`. Anything a user installs inside an agent
(`pipx install graphifyy`, `pip install --user requests`, custom dotfiles)
survives container rebuilds. The image's build-time home contents are
snapshotted into `/etc/skel/harness/`, and the entrypoint copies them into
an empty bind mount on first run, marking with
`~/.harness-home-initialized` so subsequent runs skip the seed.

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

Phase 7a split the Phase 6 `enable`/`disable` verbs into a finer-grained
lifecycle. The state diagram is:

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
| `harness mcp list`                    | All registry entries with `STATE` column.                                    |
| `harness mcp install-custom <path>`   | Install an MCP from a local directory instead of the registry.               |

The Phase 6 forms — `harness mcp enable <not-yet-installed>` and
`harness mcp disable <name> --force` — still work but emit a `DEPRECATED`
warning to stderr and forward to `install` / `uninstall --force`.

### Contributing a registry entry

1. Create `mcp-registry/<name>/` with the three required files
   (`compose.yml`, `client-config.json`, `README.md`).
2. Make sure the compose snippet uses `profiles: [mcp]` and joins the
   `harness-net` network.
3. Document any optional env vars in `README.md` and add them to
   `.env.example` if they have a default that users should know about.
4. Test locally with `HARNESS_REGISTRY_DIR=$(pwd)/mcp-registry harness mcp install <name>`.

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
`proxy`, `ollama`, `claude-agent`, `opencode-agent`. State lives in
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

## De-risk test

Phase 1 ships an automated end-to-end test that verifies ollama's RemoteHost
forwarding is wired up correctly:

```
$ bash scripts/derisk_test.sh
```

The script builds the images, brings up ollama + a mock proxy, and asserts
that a chat request to ollama is forwarded to the proxy and the response is
returned to the caller. It tears everything down on exit.

## End-user installation

End users do not clone this repo. They install via the distribution zip,
which ships `harness-install.sh`, a pre-filled `.env`, and a quick-start README.
See `zip-readme.md` for the user-facing instructions.

The installer runs a **preflight check** before any prompts (git, docker,
docker compose v2, daemon reachability, disk space, write access). On
Windows and macOS, the preflight will attempt to auto-start Docker Desktop
if it isn't running. After install, `harness preflight` re-runs a similar
set of checks plus configuration validation (.env, allowlist, hostname
alignment) — run it after editing `.env` to catch issues before
`harness start`.

### Windows

harness runs on Windows via Git Bash (Git for Windows + Docker Desktop).
PowerShell and cmd are not supported. See `docs/WINDOWS.md` for setup,
limitations, and troubleshooting.

To build the distribution zip from the current repo state:

```
$ bash scripts/build_zip.sh
# -> dist/harness-distribution.zip
```

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
the ccstatusline config under `state/agent/claude/`). Every `B3-MANAGED:`
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
- `state/agent/claude/.config/ccstatusline/settings.json` — new widgets/keys
  added (preserves layout and user widget customizations)
- `state/mcp/<name>/` — definition files (`compose.yml`, `client-config.json`,
  `README.md`) updated (preserves `harness-meta.json` enable state and
  `data/` indexed state)

Files purely user-managed (not in the manifest):

- `.harness-net-overrides.json` — controlled by `harness net open/close`
- `state/output/` — proxy debug dumps
- `state/ollama-data/` — model blobs
- **User-installed skills and `pipx` packages** under `state/agent/<tool>/`
  (e.g., `state/agent/claude/.local/bin/graphify`, `state/agent/claude/.claude/skills/graphify/`).
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
edits straight to `<install-root>/state/agent/claude/.config/ccstatusline/settings.json`.

## Tests

```
$ bash scripts/derisk_test.sh        # ollama RemoteHost forwarding
$ bash scripts/proxy_test.sh         # proxy translation
$ bash scripts/agent_test.sh         # end-to-end via both agents
$ bash scripts/harness_test.sh       # management script subcommands
$ bash scripts/persistence_test.sh   # persistent home + skel seed
$ bash scripts/mcp_test.sh           # MCP install/enable/disable/uninstall lifecycle
$ bash scripts/firewall_test.sh      # universal egress firewall + bypass overrides
$ bash scripts/upgrade_test.sh       # upgrade actions library + synthetic version transition
$ bash scripts/full_pipeline_test.sh # full install + run pipeline (drives a TUI via lib/tui_driver.sh)
$ HARNESS_RUN_SLOW=1 bash scripts/integration_test.sh  # Phase 7b: end-to-end Serena + Graphify (slow, ~10-15 min)
```

### Phase 7b integration test

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
   `state/agent/claude/.harness-mcp-servers.json` has the merged entry →
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

- `tui_driver.sh` — drives tmux-wrapped agent TUIs. Bakes in three hard-won
  constraints: hex `0d` for Enter (the keyword form silently fails on
  Ink/React TUIs), `--user harness` on every `docker exec`, and ANSI-strip
  before regex match. Pair `tui_send_line` + `tui_wait_agent_done` for the
  prompt-then-wait pattern.
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
  responses. Used by `derisk_test.sh`, `proxy_test.sh`, `agent_test.sh`.
- **Fixture dispatch** — set `MOCK_FIXTURES_DIR=/fixtures` and mount
  `scripts/fixtures/responses/` there. The mock loads every `*.json`
  lexicographically and matches the most recent user message against each
  fixture's `match` regex; first match wins. `99_default.json` is the
  catch-all. See `scripts/fixtures/responses/README.md` for the file shape
  and naming convention (`NN_short_slug.json`, with reserved priority
  ranges per scenario family).

## Project phases

- **Phase 1** — repo skeleton, ollama service, mock proxy, de-risk test
- **Phase 2** — real translating proxy
- **Phase 3** — agent containers (claude-code, opencode)
- **Phase 4** — `harness` management script + `harness-install.sh` + zip
- **Phase 6** — persistent agent homes + MCP server registry
- **Phase 7a** — MCP lifecycle granularity (install/enable/disable/up/down/logs/status), TUI test toolkit, fixture-dispatch mock upstream
- **Phase B1** — universal egress firewall (per-container iptables/ipset, allowlist seeded from `.harness-allowlist`)
- **Phase B2** — user-facing firewall controls (`harness net`, `--net`, service overrides), `harness restart`, `claude-statusline-config`, ccstatusline default, MCP `allowed_domains` print
- **Phase B3** — upgrade machinery (`harness upgrade --check/--no-prompt/--no-restart`), manifest-driven action library at `scripts/lib/upgrade_actions.sh`, `scripts/upgrade-manifest.json`
