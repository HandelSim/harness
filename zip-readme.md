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
├── install.sh             this installer
├── .env                   your config (never commit this)
├── README.md              this file
├── harness/               the cloned repo (managed by 'harness update')
├── output/                proxy debug dumps (only used if OUTPUT_DIR is set)
├── agent/claude/          full /home/harness for the claude agent container
├── agent/opencode/        full /home/harness for the opencode agent container
├── mcp/<name>/            active MCP services (one dir per enabled service)
└── ollama-data/           persists ollama model blobs
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
harness mcp enable serena
harness start
```

Agents launched after that automatically see the MCP in their config.
First `start` after enabling builds the upstream image, which can take
several minutes; subsequent starts are fast. Disable with
`harness mcp disable serena` (the data dir at `mcp/serena/data/` is
preserved across enable/disable cycles).

See `harness mcp` for all subcommands and `harness/mcp-registry/<name>/README.md`
for the security tradeoffs of each registry entry.

## Common commands

| command            | what it does                                          |
| ------------------ | ----------------------------------------------------- |
| `harness start`    | build and bring up the proxy + ollama services        |
| `harness down`     | stop services (does NOT touch agent containers)       |
| `harness claude`   | launch a claude-code agent in the current directory   |
| `harness opencode` | launch an opencode agent in the current directory     |
| `harness list`     | list running agent containers                         |
| `harness attach`   | re-attach to a running agent (picker if ambiguous)    |
| `harness stop`     | stop a running agent (picker if ambiguous)            |
| `harness logs`     | follow service logs                                   |
| `harness mcp list` | list available + enabled MCP services                 |

Pass `--yolo` to `harness claude` or `harness opencode` for skip-permissions
mode.

## Uninstall

```
rm -rf <this dir>
rm ~/.local/bin/harness
```

The PATH line in your `~/.bashrc` / `~/.zshrc` / `~/.profile` is harmless
if `~/.local/bin` is empty; remove it manually if you want.
