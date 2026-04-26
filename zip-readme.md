# harness

A Docker-based wrapper that runs `claude-code` or `opencode` against a
custom upstream API via a translating proxy. The agent sees a normal
local model endpoint; the proxy forwards real chat requests to your
configured upstream.

## What's in this zip

- `install.sh` — one-shot installer
- `.env` — config file you fill in before running anything
- `README.md` — this file

## Quick start

1. (Optional) Move this folder somewhere permanent. The installer creates
   persistent state alongside it (model blobs, agent config, debug dumps).
2. Edit `.env` and fill in `PROXY_API_KEY` plus any other blank required values.
3. Run: `bash install.sh`
4. Follow the prompts. The installer clones the harness repo into `./harness/`
   and (with your permission) symlinks the `harness` command into
   `~/.local/bin/`.
5. Open a new terminal if the installer modified your shell's PATH.
6. Run: `harness start`
7. `cd` into any project directory and run: `harness claude` (or
   `harness opencode`).

## Requirements

- `git`
- `docker` (engine running)
- `docker compose` v2 (the `docker compose` subcommand, not the legacy
  `docker-compose` binary)

## Updating

- `harness update` — `git pull --ff-only` in the clone (no rebuild)
- `harness upgrade` — pull, rebuild images, restart services

`harness update` refuses to run over local modifications to the clone;
revert or stash them first.

## Where state lives

After install, the layout is:

```
<this dir>/
├── install.sh                       this installer
├── .env                             your config (never commit this)
├── .harness-allowlist               egress allowlist (managed by 'harness net')
├── .harness-net-overrides.json      per-service firewall opt-outs (managed by 'harness net open/close')
├── README.md                        this file
├── harness/                         the cloned repo (managed by 'harness update')
├── output/                          proxy debug dumps (only used if OUTPUT_DIR is set)
├── agent/claude/                    full /home/harness for the claude agent container
├── agent/opencode/                  full /home/harness for the opencode agent container
├── mcp/<name>/                      active MCP services (one dir per installed service; harness-meta.json holds enabled flag)
└── ollama-data/                     persists ollama model blobs
```

The `agent/<tool>/` dirs are the agent containers' entire `/home/harness`,
so anything you `pipx install` or `pip install --user` inside an agent
survives container rebuilds. The first time an agent starts against an
empty home, the build-time skeleton (shells dotfiles, etc.) is restored
from `/etc/skel/harness/` inside the image.

## Adding MCP servers (Serena and friends)

Long-running MCP servers (semantic code analysis, etc.) live in a registry
under `harness/mcp-registry/`. Bring one online with:

```
harness mcp install serena
harness start
```

Agents launched after that automatically see the MCP in their config.
First `start` after install builds the upstream image, which can take
several minutes; subsequent starts are fast.

The lifecycle has separate verbs for "remove this entirely" vs "stop
auto-starting it":

| verb                                   | effect                                                 |
| -------------------------------------- | ------------------------------------------------------ |
| `harness mcp install <name>`           | copy registry entry → active tree, mark enabled        |
| `harness mcp uninstall <name> --force` | remove the active entry; `data/` is preserved          |
| `harness mcp disable <name>`           | flip auto-start off; entry stays installed             |
| `harness mcp enable <name>`            | flip auto-start back on                                |
| `harness mcp up <name>`                | start the container manually (works while disabled)    |
| `harness mcp down <name>`              | stop the container without flipping the flag           |
| `harness mcp logs <name>`              | follow the MCP's logs                                  |
| `harness mcp status <name>`            | print state, enabled flag, runtime status, paths       |
| `harness mcp list`                     | all registry entries with current state                |

The Phase 6 forms — `mcp enable <not-yet-installed>` and
`mcp disable <name> --force` — still work but emit a `DEPRECATED` warning
and forward to `install` / `uninstall --force`.

See `harness mcp` for all subcommands and `harness/mcp-registry/<name>/README.md`
for the security tradeoffs of each registry entry.

## Common commands

| command                            | what it does                                          |
| ---------------------------------- | ----------------------------------------------------- |
| `harness start`                    | build and bring up the proxy + ollama services        |
| `harness down`                     | stop services (does NOT touch agent containers)       |
| `harness restart`                  | down + start (use after editing `.env` / `.harness-allowlist`) |
| `harness claude`                   | launch a claude-code agent in the current directory   |
| `harness opencode`                 | launch an opencode agent in the current directory     |
| `harness claude-statusline-config` | edit the claude status line in an ephemeral container |
| `harness list`                     | list running agent containers                         |
| `harness attach`                   | re-attach to a running agent (picker if ambiguous)    |
| `harness stop`                     | stop a running agent (picker if ambiguous)            |
| `harness logs`                     | follow service logs                                   |
| `harness net <subcmd>`             | manage the egress allowlist + firewall overrides      |
| `harness mcp list`                 | list registry entries with state column               |
| `harness doctor`                   | runtime diagnostics (proxy, ollama, MCPs, network)    |

Agent flags:

- `--yolo` — pass auto-approve / skip-permissions to the agent.
- `--net` — disable the per-container firewall **for this launch only**
  (full outbound network). A loud warning is logged to stderr. Useful when
  you need temporary access to a host you don't want on the persistent
  allowlist.

## Network firewall

Every container runs behind a per-container egress firewall. By default,
only DNS, the upstream API host (from `PROXY_API_URL`), and entries in
`<install-root>/.harness-allowlist` are reachable.

```
harness net list                        # what's currently allowed
harness net allow github.com --git-push # add a host (--git-push permits SSH/HTTPS git push)
harness net deny github.com             # remove a host
harness net edit                        # open the allowlist in $EDITOR
harness net status                      # summary + active overrides
harness net open <service>              # disable firewall for one service (proxy/ollama/claude-agent/opencode-agent)
harness net close <service>             # restore the firewall for that service
```

`harness net open` requires you to type `I understand the risks` on a TTY
prompt. Run `harness restart` after `allow` / `deny` / `open` / `close`
to apply changes to running containers.

`harness doctor` includes a `[network]` section reporting allowlist size,
whether your `PROXY_API_URL` host is on the list, and any active overrides.

When you `harness mcp install` a registry entry that declares
`allowed_domains`, the installer prints a recommendation block listing
the `harness net allow` commands you'd need to run for the MCP to reach
its upstream services. The allowlist is never modified automatically.

## Uninstall

```
rm -rf <this dir>
rm ~/.local/bin/harness
```

The PATH line in your `~/.bashrc` / `~/.zshrc` / `~/.profile` is harmless
if `~/.local/bin` is empty; remove it manually if you want.
