# harness

Run a coding agent (opencode) against any third-party LLM API, sandboxed.
The agent runs in a container and speaks an OpenAI-compatible wire format to
a local translating proxy; the proxy calls your upstream API and adapts the
request/response (including injecting tool-use instructions and parsing tool
calls back out, since most upstreams don't support tools natively).

```
agent container ──► proxy ──► upstream API
                    (translates wire format + tool calls)
```

Everything lives in one self-contained folder: the installer clones this repo
into `./harness/`, and that clone is the install root. Code, your config, and
runtime state all live inside it.

Runtimes: **Docker** (default) or **Podman** (Linux, rootless). Auto-detected
(docker first), overridable with `HARNESS_CONTAINER_RUNTIME`. See
[docs/PODMAN.md](docs/PODMAN.md). No container runtime at all? See
[host mode](#host-mode-no-container-runtime).

## Install

Run the installer from the directory where you want `./harness/` created:

```bash
curl -fsSL -o harness-install.sh https://raw.githubusercontent.com/HandelSim/harness/main/harness-install.sh
bash harness-install.sh
```

It runs preflight checks, clones the repo into `./harness/`, seeds `.env` and
`.harness-allowlist` from their `.example` templates, and installs a `harness`
wrapper into `~/.local/bin`. It offers to capture your upstream API key (or set
`PROXY_API_KEY` in `.env` yourself afterward).

On Windows, run it from Git Bash ([docs/WINDOWS.md](docs/WINDOWS.md)).

**Then:**

1. Set the three REQUIRED values in `./harness/.env` if they aren't already:
   `PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`.
2. `cd` into any project and run `harness`. The first run builds the container
   images (a few minutes); later runs start in seconds.

Uninstall: `rm -rf ./harness && rm ~/.local/bin/harness`.

### Shipping a pre-configured bundle

To redistribute harness with a pre-edited `.env` + `.harness-allowlist`, bundle
**`harness-bootstrap.sh`** instead of a pinned `harness-install.sh`. The
bootstrap is a thin, version-stable entrypoint: it reads any
`HTTP_PROXY`/`HTTPS_PROXY` from your bundled `.env`, fetches the *current*
`harness-install.sh` from the repo, and hands off to it. Your bundle never goes
stale because the install logic always comes from upstream.

```bash
# in the folder holding harness-bootstrap.sh + .env + .harness-allowlist
source ./harness-bootstrap.sh                            # or: bash ./harness-bootstrap.sh
HARNESS_INSTALL_REF=v1.0 source ./harness-bootstrap.sh   # pin a release
HARNESS_REPO_URL=https://github.com/you/harness source ./harness-bootstrap.sh   # fork/mirror
```

If the fetch fails (offline, bad ref), it falls back to a bundled
`harness-install.sh` if you ship one, else aborts cleanly. New upstream `.env`
variables do not need to enter your bundle: `harness upgrade` merges them in
from `.env.example` without touching your values, so you only edit the bundle to
change your *own* values. Details:
[architecture/install-and-upgrade.md](architecture/install-and-upgrade.md).

## Running

Bare `harness` (or `harness opencode`) launches an opencode agent in the current
directory. The directory you launch from is bind-mounted into the container at
the **same absolute path**, so `pwd` inside the agent matches your host shell and
absolute paths in code and logs round-trip cleanly.

Mount extra folders (repeatable, mounted at their same host path):

```bash
harness --mount /home/me/refs --mount /home/me/data
```

Paths must exist and be directories; the CWD is always mounted and is always the
start dir; container infrastructure paths (`/etc`, `/usr`, ...) are refused. For
a sticky list, set `HARNESS_EXTRA_MOUNTS=/path/a:/path/b` (colon-separated) in
`.env`. More: [architecture/containers.md](architecture/containers.md).

### Common commands

```
harness [flags]        launch opencode in the CWD (default command)
harness shell          bash shell inside the agent container (same home as opencode)
harness host           containerless mode (see below)
harness chatgpt        launch against the ChatGPT backend-api instead of the
                       default upstream ('harness chatgpt host' for the
                       containerless form). Needs CHATGPT_BASE_URL,
                       CHATGPT_MODEL_NAME and CHATGPT_COOKIE_STRING in .env.
harness --net          this launch only: full outbound network, firewall off
harness --yolo         pass-through: auto-approve / skip permissions

harness start|down|restart    manage the shared stack (proxy + enabled MCPs)
harness update                fast-forward git pull, code only
harness upgrade               git pull + migrate config + restart
harness mcp ...               manage MCP servers (see MCPs)
harness net ...               manage the egress allowlist (see Firewall)
harness doctor                environment + network diagnostics
harness preflight             validate .env and allowlist
harness test [section]        run the test suite
```

Any flag harness doesn't recognize is passed straight through to opencode.

### Host mode (no container runtime)

`harness host` runs the proxy + opencode as plain host processes, no docker. The
first run auto-fetches its deps (jq, Node >= 20, opencode) into
`state/host/toolchain/`; you only need Python 3. Host mode has **no egress
firewall** and runs as your full host user, so it prompts to confirm on every
launch (`HARNESS_HOST_CONFIRM=1` to skip in automation). Prefer the sandboxed
container mode; host mode is the fallback.

## Egress firewall

Every container on `harness-net` boots with iptables/ipset rules that drop all
egress except DNS, the `PROXY_API_URL` host, and the hosts in
`<install-root>/.harness-allowlist` (one per line; a trailing `# git-push` also
opens that host's git ports).

```
harness net list                         show every allowed host
harness net allow github.com --git-push  add a host
harness net deny example.com             remove a host
harness net status                       allowlist size + open services
harness net open <service>               disable the firewall for one service
harness net close <service>              restore it
```

`net open` requires typing `I understand the risks` on a TTY. `--net` disables
the firewall for a single agent launch only and prints a loud warning. Run
`harness restart` after editing the allowlist.

## MCPs

Long-running MCP servers (Serena, etc.) are defined under `mcp-registry/<name>/`
and managed with:

```
harness mcp list [--available]   installed entries (and registry entries not yet installed)
harness mcp install <name>       copy registry entry into the active tree, enabled
harness mcp uninstall <name> --force   remove it (data/ preserved)
harness mcp enable|disable <name>      flip auto-start without (un)installing
harness mcp up|down <name>             start/stop manually
harness mcp status|logs <name>         inspect
```

`enable`/`disable` only toggle whether an installed MCP auto-starts with the
stack. Authoring new registry entries and the lifecycle state machine:
[architecture/mcp.md](architecture/mcp.md).

**Installing skills** (graphify, etc.): ask the agent to run the install inside
`harness`, or `harness shell` and run it yourself. Either way it lands in the
shared agent home (`state/agent/home/`) and persists across rebuilds and
upgrades.

## Updating

- `harness update` — fast-forward `git pull` only. Latest code, no side effects.
- `harness upgrade` — `git pull --ff-only`, then migrate your config from the
  upgrade manifest (adds new `.env` vars, allowlist hosts, and MCP definition
  updates **without** overwriting your values), then restart the stack.

```
harness upgrade --check       preview, no writes
harness upgrade --no-prompt    apply without the [y/n] prompt
harness upgrade --no-restart   apply without down/start (e.g. CI)
```

How the manifest and merge actions work:
[architecture/install-and-upgrade.md](architecture/install-and-upgrade.md).

## Layout

The clone IS the install root; your config and `state/` are gitignored:

```
<install-root>/                 the git clone (e.g. ~/harness/)
├── harness                     management CLI
├── harness-install.sh          installer (clone + seed config + PATH)
├── harness-bootstrap.sh        version-stable entrypoint: fetches + runs the current installer
├── docker-compose.yml          services: proxy, agent
├── proxy/                      the translating proxy
├── agents/                     agent image (Dockerfile + entrypoint)
├── mcp-registry/               vetted MCP service definitions
├── firewall/                   egress-firewall init container
├── scripts/lib/                sourceable shell libraries (platform.sh, upgrade_actions.sh)
├── tests/                      test suite (see tests/README.md)
├── architecture/               per-module design docs
├── .env                        your config (gitignored)
├── .harness-allowlist          egress allowlist (gitignored)
└── state/                      runtime state (gitignored): output/, agent/home/, mcp/, host/
```

## Development and tests

Run the stack from a checkout without installing:

```bash
cp .env.example .env && $EDITOR .env
docker compose --env-file .env up --build
```

Tests live in `tests/` ([tests/README.md](tests/README.md)):

```
harness test                  all CI-runnable tests
harness test unit             fast, no-docker
harness test proxy            proxy translation tests
harness test integration --slow   full slow integration test
```

Benchmarks (`harness benchmark smoketest|terminal-bench`) need an upstream API
key and disk, and never run in CI. CI runs lint, unit, docker-tests, pipeline,
integration, and scheme_contract on every push to `dev`/`main`.

Per-module design docs are in [architecture/](architecture/) (CLI, proxy,
containers, MCP, install/upgrade, upstream API, tests). Live test coverage is
tracked in [tests/COVERAGE.md](tests/COVERAGE.md).

Found a bug or have an improvement, however small?
<https://github.com/HandelSim/harness/issues>
