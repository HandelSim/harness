# `harness` CLI

The `harness` bash script is the management entrypoint. ~4200 lines, one
file. Symlinked into `~/.local/bin/harness` by `harness-install.sh`; users
invoke it from anywhere.

## Self-locate and install-root resolution

The script resolves `$0` through any wrapper or symlink using a portable
realpath (so it works on Windows Git Bash too) to find:

- **`clone_dir`** — where the code lives (this script + `docker-compose.yml`
  + `scripts/`).
- **`install_root`** — where user config (`.env`, `.harness-allowlist`,
  `.harness-reminder.md`) and runtime state (`state/`) live.

In production these are the same directory: `harness-install.sh` clones
the repo and that clone IS the install root. Tests set
`HARNESS_INSTALL_ROOT` to a tmpdir to keep `clone_dir` resolved to the
real repo while pointing `install_root` somewhere disposable.

The portable resolver is duplicated inline here rather than depending on
`realpath`; the full equivalent lives in `scripts/lib/platform.sh` as
`harness_realpath`.

## Env loading

The script sources `.env` into its own shell (`set -a; source; set +a`)
so it can read values like `DEFAULT_MODEL_NAME`, `HARNESS_EXTRA_MOUNTS`
for its own logic. `docker compose` gets `.env` separately via
`--env-file`. The two consumers are independent.

`DEFAULT_MODEL_NAME` (which replaced the old `PROXY_API_MODEL` +
`OLLAMA_AGENT_MODEL`) is the default/fallback model id and is **REQUIRED with no
hardcoded default** — once the selected model passes through to the upstream, a
cosmetic default would be a real upstream id we can't know. `agent_model` reads
it (empty when unset) and `require_runtime_config` enforces it's set before any
launch. The agent entrypoint discovers the upstream's full model list from the
proxy's `/v1/models` catalog (always including this one); opencode selects
`harness/${DEFAULT_MODEL_NAME}` by default.

### Upstream URL base + auth/model probes

`PROXY_API_URL` is a **base**; `_api_base` normalizes it (stripping a trailing
`/v1/chat/completions`, `/chat/completions`, or `/v1`), mirroring proxy.py's
`_normalize_api_base` — keep the two in sync. `_probe_upstream_auth` POSTs to
`{base}/v1/chat/completions` before the stack starts and **gates the launch**:
a locked key (`401`/`403` with an unlock URL in the body) aborts and prints the
clickable unlock URL; a `401`/`403` with **no** unlock URL also aborts as a
rejected key (#108) **unless** the upstream's `error.type` is in the
`invalid_request` family — the one "key is fine, the probe request was bad"
case (#43, `_auth_probe_type_is_request_error`) that warns-and-continues. An
empty/unknown type on a `401`/`403` aborts. Aborting here is what stops a bad
key from reaching the proxy, where every request would fail. `_print_upstream_models` then GETs
`{base}/v1/models` (best-effort): on success it prints the catalog, on a locked
key it shows the unlock URL and aborts, and on an unreachable upstream (e.g.
tests pointing at an in-network mock the host can't reach) it stays quiet and
proceeds — the agent entrypoint discovers the model list from the proxy's
`/v1/models` route at config-build time. Both honor `HARNESS_SKIP_AUTH_PROBE=1`.

### Host proxy (`HTTP_PROXY` / `HTTPS_PROXY`)

Optional. Exported into the process env so the host-side work this script
runs picks them up: the git calls (`update`/`upgrade` pull, `mcp install`
clone — git's libcurl reads them straight from the env) and `docker compose
build`, where BuildKit routes base-image pulls and the image `RUN` steps
through the proxy. They are **not** forwarded into running containers (see
[`containers.md`](containers.md)). Resolution around the `.env` source: a
non-empty value in `.env` wins; a blank value (the optional default) is
prevented from clobbering a proxy the invoking shell already exported by
capturing the shell values first and restoring any `.env` left empty.
`harness_normalize_proxy_env` then mirrors the upper/lower-case spellings.

**Host-mode (`harness host`) toolchain pulls honor the proxy too.** The
containerless launch fetches its deps on the host: jq + the Node archive +
checksum manifests via `host_fetch` (curl, which reads `*_PROXY` from the
env directly), `opencode` via `npm`, and the proxy venv via `pip`. npm and
pip honor those env vars only as config *defaults*, so an ambient
`~/.npmrc` / `pip.conf` proxy could override them; `host_proxy_flags`
(`npm`/`pip` dialects) passes the `.env` proxy as a CLI flag — top of both
tools' config precedence — so `.env` wins regardless, keyed off
`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`. The one curl host mode aims at the
**local** proxy (the `/v1/models` dropdown pull in step 7) passes
`--noproxy 127.0.0.1,localhost` so a corporate `HTTP_PROXY` never hijacks
the loopback call and silently collapses the dropdown to
`DEFAULT_MODEL_NAME`; the upstream pulls (auth gate, `_print_upstream_models`)
keep using the proxy.

**On a corp-proxy host the two request hops route oppositely: the proxy's
upstream hop USES the corp proxy, opencode's loopback hop does NOT.** A host
behind a corporate proxy reaches the internet only through it, so:

- `proxy.py`'s UPSTREAM hop (the chat completions it forwards to
  `PROXY_API_URL`) goes THROUGH the corp proxy. It uses python `requests`
  (`trust_env` defaults `True`), and `host_proxy_start` lets it inherit this
  shell's proxy env on purpose — the host has no other egress. The container
  differs by necessity: its network reaches the upstream directly, so
  `docker-compose.yml`'s proxy service carries no proxy vars. (An earlier host
  version `env -u`'d the vars to mirror the container and 504'd — the host
  *can't* reach the upstream directly.)
- opencode's LOOPBACK hop to the host proxy (its provider baseURL is
  `http://127.0.0.1:PORT/v1`) must BYPASS the corp proxy. opencode runs on Bun,
  whose native fetch honors `HTTP_PROXY`/`HTTPS_PROXY`, so a corp proxy would
  otherwise tunnel the loopback call (which can't reach this box) and every chat
  would 504. `host_run_opencode` sets `NO_PROXY=127.0.0.1,localhost,::1` (merged
  with any `.env` value) — opencode's own documented loopback bypass
  (opencode.ai/docs/network) — so the provider call goes direct while other
  opencode egress (e.g. Exa web search) still uses the proxy. This mirrors the
  `--noproxy 127.0.0.1,localhost` curl guard for the `/v1/models` dropdown pull.

Regression-guarded by `unit_host_test.sh` T11 (no scrub on `proxy.py`) and T12
(opencode loopback `NO_PROXY`).

## Subcommand surface

`cmd_help` is the source of truth for the user-facing subcommand list.
The implementation has one `cmd_<name>` function per subcommand:

| Group | Subcommands |
|---|---|
| Lifecycle | `start`, `down`, `restart`, `logs`, `unlock` |
| Update / upgrade | `update`, `upgrade`, `downgrade`, `check-updates` |
| Agent launch | `opencode`, `shell`, `list`, `stop` |
| Config / setup | `config` (get/set/list + interactive picker), `model` (pick `DEFAULT_MODEL_NAME` from the live catalog), `uninstall` — see "Config and setup commands" below. |
| Containerless | `host`, `host down` — run the proxy + opencode as plain host processes, no docker. See "Host mode" below. |
| Alternate upstream | `chatgpt`, `chatgpt host` — the same two launch paths run against the ChatGPT backend-api instead of the OpenAI-compatible upstream. See "ChatGPT backend" below. |
| Diagnostics | `doctor`, `preflight`, `help` |
| Test / bench | `test`, `benchmark` |
| Net (allowlist + per-service firewall) | `net list`, `net allow`, `net deny`, `net edit`, `net status`, `net open`, `net close` |
| MCP (long-running services) | `mcp list`, `mcp install`, `mcp uninstall`, `mcp enable`, `mcp disable`, `mcp up`, `mcp down`, `mcp logs`, `mcp status` |
| MCP (host, non-container) | `mcp host-init`, `mcp host-setup` — scaffold + register a host build MCP (a process on the host, e.g. MSVC/CMake) and launch the agent that tailors it. See [`mcp.md`](mcp.md) "Host MCPs". `host-setup` `cd`s into `host-mcp/<name>/` and calls the normal `run_agent` so opencode auto-loads that folder's `AGENTS.md`. |

### Bare `harness` launches the agent

`main()` dispatches as follows: zero args, or a leading agent flag
(anything starting with `-` other than `-h`/`--help`), launches an opencode
agent in the CWD and forwards everything (`harness`, `harness --yolo`,
`harness -p "x"`). `-h`/`--help` show help. A leading bare word is treated
as a subcommand: known ones dispatch; an unknown word errors (so a typo
like `harness statt` is caught instead of silently launching an agent).
`harness opencode` remains as an explicit alias for the bare form.

Agent-launch flags (`--yolo`, `--net`, `--mount`, `-p/--print`) are parsed
inside `run_agent` (opencode) / `cmd_shell` rather than centrally; they
decide the `docker run` invocation, not compose flags.

### Config and setup commands

`harness-install.sh` is fetched once and **frozen** at install time, so
anything a user re-runs after install lives in this (upgradeable) CLI, not the
installer. Three commands cover post-install setup:

- **`cmd_config`** — view and change `.env` values.
  - Bare `harness config` first prints the subcommand usage (`_config_help`, so
    you learn `get`/`set`/`list` without `--help`), then — on a tty — opens an
    interactive **picker** of the common settings (so you don't have to memorize
    `.env` keys); with no tty it prints the current values instead of erroring.
    This bare path runs through `_config_overview`. The curated set comes from
    `_config_editable_keys` (`KEY|description` pairs). Picking
    `DEFAULT_MODEL_NAME` delegates to `cmd_model`.
  - `config get [KEY]` / `config list` print values; `config set KEY VALUE`
    writes one. **Not a whitelist** — `set` writes any key; the picker/`get`
    just surface the common ones.
  - Reads use `_config_read_key` (straight from `$env_file`, so they reflect
    on-disk state). Writes use `_config_write_key` — the same atomic
    temp-file + `mv` read-loop the installer uses (no `sed`, so values with
    `/ & : @` need no escaping; appends the key if absent).
  - Secret-ish keys (name matches `*KEY*|*SECRET*|*TOKEN*|*PASSWORD*` via
    `_config_is_secret`) are shown masked (`(set, N chars)`) and prompted with
    a hidden read.
  - Setting `PROXY_API_URL` also adds its host to the egress allowlist
    (`_config_sync_allowlist_from_url` → `load_net_helpers` +
    `netlib_add_host`), since an upstream missing from the allowlist is the
    top "AI silently unreachable on a fresh URL" cause. Degrades to a printed
    hint when `net_helpers.sh` isn't reachable.
- **`cmd_model`** — pick `DEFAULT_MODEL_NAME` from the upstream's live
  `/v1/models` catalog (same logic the installer runs at "configuring default
  model", now re-runnable). Curls the catalog via `_api_base`, shows a numbered
  menu (jq parse, grep/sed fallback), and falls back to free-text entry when
  the catalog can't be fetched. Writes via `_config_write_key`.
- **`cmd_uninstall`** — tear down this install completely. Best-effort, in
  order: **(1)** remove the containers *first* (so an in-use image can't block
  image removal) — agent containers launched via `docker run`
  (`label=harness.project=$project_name` + `label=harness.agent=true`) and the
  compose services such as the proxy (`label=com.docker.compose.project=$project_name`),
  each swept by label with `rm -f -v`. Both sweeps are needed because
  `compose down` alone skips containers whose compose working-dir labels don't
  match this invocation (verified: a relabelled container survives a bare
  `compose down`). **(2)** `<runtime> compose … down --rmi all --remove-orphans
  --volumes` to delete the built images (`harness-proxy`, `harness-agent`),
  volumes, and network. **(3)** an explicit `rmi -f harness-proxy:latest
  harness-agent:latest` fallback for any image compose left behind. Finally
  removes `state/`, the install root, and the `$HOME/.local/bin/harness`
  wrapper. **Prompts for a typed `yes` first** (via `/dev/tty`, or
  `HARNESS_CONFIRM_FROM_STDIN=1` in tests); `--yes`/`-y` skips the prompt for
  automation. Refuses to run when `install_root` is empty or `/`. This is the
  everyday replacement for the installer's `-u` recovery path, which performs
  the same container+image teardown.

## Compose wrapper (`compose()`)

`compose()` is the single entry point for any `docker compose` / `podman
compose` invocation. It:

1. Calls `write_runtime_override` (next section) to (re)generate
   `state/.harness-runtime.yml`.
2. Builds an args list: `--project-name`, the main `docker-compose.yml`,
   the runtime override (if non-empty), and any active MCP compose
   snippets discovered via `mcp_compose_files`.
3. Exports `INSTALL_ROOT`, `HARNESS_ALLOWLIST_PATH`, `HARNESS_PROJECTS_ROOT`
   so MCP compose snippets can reference them with plain `${VAR}` (no
   defaults), plus `HARNESS_HOST_OS="$(harness_detect_os)"` so the proxy
   service learns the host platform. The proxy injects it into the hybrid
   recency reminder's Environment line (see `architecture/proxy.md` →
   Host-OS injection); `docker-compose.yml` defaults it to `unknown` when
   harness isn't the launcher. `cmd_start` separately seeds the user's
   editable recency-reminder file (`seed_reminder_file` →
   `.harness-reminder.md`, from the tracked `proxy/reminder.md`) and exports
   `HARNESS_REMINDER_PATH` at it, so a reminder edit takes effect on
   `harness restart` with no rebuild — see `architecture/proxy.md` →
   "Editable reminder prose".
4. Invokes the detected container runtime (`docker` or `podman`, per
   `scripts/lib/platform.sh:harness_container_runtime`).

Project name is fixed (`harness`) but overridable for tests via
`HARNESS_PROJECT_NAME` so `harness_test.sh` doesn't collide with a real
instance on the same daemon.

## Runtime override (`write_runtime_override`)

`state/.harness-runtime.yml` is regenerated on every compose invocation
and never tracked. It carries two things:

1. **Per-service firewall opt-out** — for every service with
   `firewall_disabled: true` in `state/.harness-net-overrides.json`, an
   `environment: HARNESS_FIREWALL_DISABLED: "1"` block is emitted. The
   firewall init script short-circuits on that variable. We never replace
   the service's entrypoint or remove its `cap_add`.
2. **Ephemeral `--prompt-mode`** — `harness start/restart --prompt-mode
   <mode>` (`_parse_prompt_mode_flag` validates `hybrid`/`user_front`/
   `passthrough`) sets the `prompt_mode_override` global, which adds
   `environment: PROXY_PROMPT_MODE: "<mode>"` onto the proxy service. This is
   the only path that sets `PROXY_PROMPT_MODE` for the proxy now that
   `docker-compose.yml` no longer interpolates it (a stale `.env` value is
   inert). It is **not persisted** — a later bare `start`/`restart` regenerates
   the file without it, reverting the proxy to its built-in `hybrid` default.
   When the proxy *also* has a firewall opt-out (1), the prompt mode is folded
   into that same `proxy:` block rather than emitted as a second mapping
   (duplicate top-level service keys are invalid compose YAML).

The `agent` pseudo-service is filtered out: agent containers are launched
by direct `docker run` from `run_agent` (opencode) / `cmd_shell`,
not by compose. The agent launch path reads `.harness-net-overrides.json`
directly.

If all three sources are empty, the file is deleted rather than written empty
so compose doesn't see a phantom services block.

## Agent launch path

`run_agent` (opencode) / `cmd_shell` do NOT go through compose.
They each:

1. Parse agent flags (`--yolo`, `--net`, `--mount`, `-p/--print`).
2. Compute mounts: CWD at the same absolute path, plus extras from
   `--mount` and `HARNESS_EXTRA_MOUNTS` (deduped, validated, refused if
   under container infra paths like `/etc`, `/usr`, `/home/harness`).
3. Compose `--cap-add NET_ADMIN`, `--cap-add NET_RAW`, the allowlist
   read-only mount, network `--network harness_harness-net`, and a
   per-launch unique container name.
4. `docker run` the unified agent image (`harness-agent:latest`) with the
   mode arg (`opencode` or `shell`) and any forwarded flags.

The container name carries a per-launch random suffix, so multiple agents
can run from the same directory at once. `list`/`stop`/the picker discover
agents by label (`harness.agent`/`tool`/`mount`), never by name, so the
name needs no determinism — only uniqueness, to avoid Docker's unique-name
collision. The `--print` path sets no container name but now carries the
same `harness.agent`/`project`/`tool`/`mount` labels (so a running `-p`
agent counts toward the project total and protects the shared stack — see
"Last-agent stack teardown"); it was already concurrent.

### Interactive TTY resolution (Windows) — issue #82

The two interactive launches (`run_agent_interactive`, `cmd_shell` — **not**
the `-p/--print` path) decide the `docker run` TTY flags via
`harness_resolve_interactive_tty` (`scripts/lib/platform.sh`) instead of a bare
`[[ -t 0 ]]`. The split exists because on Windows Git Bash, bash's `[[ -t 0 ]]`
(an MSYS pty) and the native `docker.exe`'s `isTerminal()` (a real Windows
console) disagree: under MinTTY without ConPTY, bash says "tty" so harness adds
`-t`, then `docker.exe` rejects it with *"cannot attach stdin to a TTY-enabled
container because stdin is not a terminal"*.

The resolver returns one of three strategies:

- `it` — stdin is a real tty to the runtime (Linux/macOS always; Windows when
  ConPTY/Windows Terminal makes `docker.exe` accept `-t`). Launch as before.
- `it-winpty` — Windows, `docker.exe` rejects `-t`, but `winpty` is on PATH.
  The launch is wrapped via `harness_docker_winpty` so winpty bridges a real
  console (keeps `-it`, TUI works). Set `HARNESS_NO_WINPTY=1` to opt out.
- `i` — no usable tty (genuinely piped stdin, or Windows with no winpty). Falls
  back to `-i`; the Windows-from-a-tty case prints a one-line degraded note.

The actual `docker.exe` capability is probed once per launch by
`harness_runtime_tty_ok` (a throwaway `--rm --entrypoint true` run against the
already-built agent image — no pull, instant). On Linux/macOS the whole thing
collapses to the historical "`-it` if stdin is a tty, else `-i`" with no probe.
`harness_tty_strategy` is the pure (I/O-free) decision function the resolver
wraps, kept separate so it is unit-testable without a real tty or daemon.

### Windows container working directory — issue #112

Bash on MSYS rewrites Unix-form path arguments to Windows form when
invoking native `.exe` binaries: `/c/Users/foo` in `argv` becomes
`C:/Users/foo`, and the `-v C:/foo:/c/foo` composite is additionally
treated as a path *list* (because of the embedded `:` plus forward
slashes) and joined with `;`, producing `C:\foo;C:\foo`. Two failure
modes follow:

- `-w C:/Users/foo` — Linux docker daemon rejects with *"the working
  directory is invalid, it needs to be an absolute path"*.
- `-v C:\foo;C:\foo` — docker's volume parser splits on `:`, gets three
  parts, treats the third as the mode → *"invalid mode: \\foo"*.

Both bash inline-prefix (`VAR=val cmd`) and `env VAR=val cmd` are
unreliable here: in the affected MSYS vintages the variable lands in the
child's environment AFTER argv conversion has already run in the parent
shell. The fix is to put both toggles in bash's own environment for the
duration of the call:

`harness_docker`, `harness_docker_winpty`, `harness_docker_exec`, and
`harness_runtime_tty_ok` (all in `scripts/lib/platform.sh`) use
`local -x MSYS_NO_PATHCONV=1` and `local -x MSYS2_ARG_CONV_EXCL='*'` on
Windows (plain `export` for the `exec` helper, which never returns).
`MSYS_NO_PATHCONV` covers single-path conversion in older Git for
Windows; `MSYS2_ARG_CONV_EXCL='*'` covers path-list conversion in modern
MSYS2 — both are set because field reports have hit each layer.

`harness_container_workdir` remains as defense-in-depth for the `-w`
arg: on Windows it prefixes the path with the MSYS UNC escape `//`,
which MSYS unconditionally leaves alone (independent of the env-var
toggle). The Linux kernel collapses `//foo` → `/foo` on `chdir`, so the
container working directory still matches the bind-mount target. The
three docker launch sites (`run_agent_interactive`, `run_agent_print`,
`cmd_shell`) pass `-w "$(harness_container_workdir "$mount_path")"`.

**Bind mounts — the `winpty` path needs more than the env toggles.** The
env-var toggles fix the `harness_docker` path, but the interactive launch
routes through `harness_docker_winpty` (`winpty docker …`), and `winpty`
does its *own* argv path-list conversion that the bash-level toggles do
not reach. So `-v C:/foo:/c/foo` still gets rewritten to `C:\foo;C:\foo`,
and docker reports `invalid mode: \foo`. The escape that survives both
layers is to drop the `-v src:tgt` composite entirely in favour of the
comma-delimited `--mount type=bind,source=<src>,target=<tgt>[,readonly]`
form — a single token with no bare `:` for either converter to latch
onto. `harness_add_bind_mount <array> <src> <tgt> [ro]`
(`scripts/lib/platform.sh`) appends `--mount=…` on Windows and the
classic `-v` on Linux/macOS; all bind mounts at the three launch sites
(CWD, `/home/harness`, the read-only allowlist, and every `--mount` /
`HARNESS_EXTRA_MOUNTS` extra) go through it. The mount **source** stays in
Windows mixed form (`C:/…` via `harness_docker_path`) because Docker
Desktop's WSL2 backend mounts that reliably where a raw `/c/…` source can
come up empty.

`harness doctor`'s Windows `[runtime]` block surfaces the resolved
`mount_path`, the escaped `-w` form, **and** the CWD `--mount` bind-mount
arg so all three failure modes are one-line diagnosable.

### Post-run issue footer (interactive only)

The interactive launch paths (`run_agent_interactive`, `cmd_shell`) run
docker as a child rather than `exec`-ing it, so once the session exits
they print `_print_agent_issue_footer` — the same "report a bug" line +
`/issues` URL that `harness-install.sh` prints at the end of install — to
**stderr**, then `exit` with the container's exit code. Reasons it lives
*after* the run and on stderr: an interactive TUI takes the alt-screen, so
anything printed before launch is wiped; stderr keeps it out of captured
stdout. The `--print` (`-p`) path is left untouched — no footer — because
headless single-shot is used in scripts and pipes where a per-call footer
is noise. Because these paths now `exit` instead of `exec`, the top-level
`EXIT` trap fires and reaps the jq sidecar; the explicit pre-run
`_reap_jq_sidecar` call is therefore a harmless idempotent no-op the second
time.

### Last-agent stack teardown

The shared stack's lifecycle is bound to the agents': `ensure_services_up`
brings it up on launch, and the last agent to exit brings it down, so the
control plane never lingers with no agent using it. After an **interactive**
agent exits, `run_agent_interactive` calls `stop_stack_if_last_agent`, which
counts the remaining agent containers for the project
(`label=harness.project=<p> label=harness.agent=true`); the just-exited
container is already gone (`--rm`), so a zero count means it was the last
one, and the proxy + enabled MCPs (container **and** host) are torn down via
`cmd_down`. This is unconditional — there is no opt-in env var. The
`cmd_down` call is best-effort and isolated in a subshell
(`( cmd_down ) >/dev/null 2>&1 || true`): `cmd_down` → `require_docker` can
`exit 1` if the runtime probe fails, and a bare `|| true` catches only a
non-zero *return*, not an `exit`. Without the subshell that `exit` would
terminate `run_agent_interactive` before its final `exit "$rc"`, swallowing
the container's exit code (#81).

Scope notes: **print (`-p`) agents carry the same `harness.agent` labels**,
so they count toward the project total. A concurrent `-p` run therefore keeps
the count non-zero and holds the stack up — an interactive exit won't tear
the stack out from under it. Print agents do **not** trigger teardown
themselves (only `run_agent_interactive` calls `stop_stack_if_last_agent`),
so a scripted `-p` loop doesn't churn the stack up and down. The trade-off:
a `-p` agent now appears in `harness list` for the duration of its run. The
count is project-scoped, so concurrent interactive agents likewise keep the
stack up until the last one exits. This is the inverse of `cmd_down`'s own
agent-stop sweep, which tears agents down when the *stack* goes down.

## Host mode (containerless)

`harness host` runs the whole stack as plain host processes — no docker, no
containers — for a lighter footprint (no daemon, no multi-GB images). `cmd_host`
orchestrates it; `harness host down` (`cmd_host_down`) stops the proxy. All
host-mode helpers live in the "host mode (containerless)" block alongside the
host-MCP supervision they reuse the shape of.

What it does, in order:

1. **`host_require_python3`** — Python 3 is the one host-mode dependency harness
   does **not** auto-provision: it bootstraps the proxy venv, so it must already
   exist before harness can fetch anything else. Checked **before** the confirm
   gate so a Python-less host fails fast without prompting or downloading. The
   interpreter is resolved by `host_python_bin` (used everywhere harness needs to
   call Python): `python3`, then `python`, then `py -3` — because Windows Git
   Bash often has no `python3` (python.org ships `python` / the `py` launcher;
   only the MS Store build provides `python3`). Each candidate is **verified by
   running `--version`** and confirming it reports `Python 3`, not trusted on
   `command -v` alone: Windows puts an App-execution-alias stub named
   `python3.exe` on `PATH` by default that prints "Python was not found…" instead
   of running, so a bare `command -v python3` would pick the stub and the later
   `python3 -m venv` would fail as if Python were absent even with a working
   `python` present. Probing `--version` rejects the stub and falls through.
2. **`host_require_config`** — a lighter `require_runtime_config`: the same three
   REQUIRED proxy vars (`PROXY_API_URL`/`PROXY_API_KEY`/`DEFAULT_MODEL_NAME`,
   read from the already-sourced `.env`), but **no allowlist check** — the
   firewall allowlist only governs container mode.
3. **`host_confirm_gate`** — mandatory on **every** launch (unlike `--net`'s
   per-invocation flag), worded harder: host mode has no egress firewall and
   runs opencode as the full host user (full filesystem incl. `~/.ssh`/`~/.aws`,
   unfiltered network), a strictly bigger blast radius than `--net`.
   `HARNESS_HOST_CONFIRM=1` bypasses it for automation; refuses non-interactive
   without `/dev/tty`. This is why host mode exists as a separate, gated command
   rather than a flag on the normal launch.
4. **`host_ensure_toolchain`** (after the gate, so no download happens before
   consent) — provisions the other three deps (`jq`, Node >= 20, `opencode`) into
   `state/host/toolchain/` and prepends the vendored dirs to `PATH` for the rest
   of the launch. Each provisioner prefers a satisfactory **host** binary (jq any
   version; Node major >= 20; opencode only when its version equals the pin,
   since `host_run_opencode`'s `opencode export` shapes are version-fragile);
   otherwise it downloads: jq's static release binary and Node's archive are
   sha256-verified against the upstream `sha256sum.txt` / `SHASUMS256.txt`, and
   `opencode-ai` + `@ai-sdk/openai-compatible` are `npm install`ed into a scoped
   prefix (using the vendored Node). **Per-OS layout** is routed through
   `harness_detect_os` so one code path serves all three: the exe suffix
   (`host_exe_suffix` → `.exe` on Windows), the Node archive kind
   (`host_extract_archive` unpacks Windows' `.zip` via bsdtar/`unzip`, else
   `.tar.gz`), and where the binaries land after extraction — Node's `node.exe`
   and npm's `opencode` shim sit at the **root** of their dirs on Windows
   (`host_node_exe` / `host_opencode_exe`), under `bin/` on Linux/macOS. jq has
   no Windows arm64 build upstream, so `host_jq_platform` fails closed there;
   Windows host support targets x64. Pins (`HARNESS_HOST_{JQ,NODE,OPENCODE,OPENAI_COMPAT}_VERSION`,
   all env-overridable) mirror `agents/Dockerfile` so host runs the same opencode
   + provider the container suite validates; a unit test guards that the opencode
   / provider pins stay in sync with the Dockerfile ARGs. Stamps (`.stamp-jq`
   etc.) are written **only after** a `--version` smoke run succeeds, so a
   corrupt or half-extracted tool is never trusted on the next run. This makes
   host mode **self-installing, not offline**: opencode still fetches its provider
   over the (unfirewalled) network on first use, exactly as the container does.
   `host_preflight` then runs as a post-provision assertion — each of `python3`,
   `jq`, Node, `opencode` must both resolve **and** execute, naming any that fail.
5. **Upstream auth gate + model catalog** — the same two checks container mode
   runs in `cmd_start`, which host mode previously skipped: `_gate_on_upstream_auth`
   aborts the launch on a **locked or rejected** key (printing the unlock URL),
   and `_print_upstream_models` pulls and prints the upstream `/v1/models` catalog
   (best-effort; also aborts on a locked key). Both run **after** `host_preflight`
   so the vendored host `jq` is on `PATH` for unlock-URL / catalog parsing
   (`_probe_upstream_auth` keeps a `grep` fallback regardless) and **before**
   `host_proxy_start` so a dead key never spins up the proxy — closing the gap
   where host mode launched opencode against a locked key and every request then
   failed deep in the proxy with no unlock URL surfaced. Both honor
   `HARNESS_SKIP_AUTH_PROBE=1` (CI / offline), and both no-op cleanly when `curl`
   is absent. The catalog print is the upstream-direct pull; the opencode model
   dropdown is built separately from the *proxy's* `/v1/models` in step 7.
6. **Proxy supervision** — `host_proxy_start` lazily builds a venv under
   `state/host/venv` (`host_proxy_ensure_venv`: `$(host_python_bin) -m venv` +
   `pip install` of `proxy/requirements.txt`'s two pure-python wheels, re-pip only
   when the requirements hash changes), then `nohup`s `proxy/proxy.py` with the
   venv interpreter (`host_venv_python` → `Scripts/python.exe` on Windows,
   `bin/python` elsewhere) and a pidfile +
   logfile under `state/host/`, mirroring the host-MCP pidfile model. **It binds
   `127.0.0.1` only** (`PROXY_HOST=127.0.0.1`) so the host proxy is never
   reachable off-box; it also exports `HARNESS_FORCE_LOOPBACK=1`, which makes
   `proxy.py` refuse to bind any non-loopback `PROXY_HOST` — a second layer so a
   regression in this launch line can't expose the keyed proxy on the LAN in the
   firewall-less mode. The proxy reads its REQUIRED env straight from this shell.
   **Debug dumps are opt-in, same as container mode:** the proxy inherits
   `OUTPUT_DIR` from `.env` (default empty, no dumps), so the higher-blast-radius
   host mode never silently writes every prompt to disk. The documented value is
   the container target `/output`, which container mode bind-mounts to
   `state/output/`. Host mode has no bind mount, so `host_proxy_start` remaps
   `/output` (and the Windows `//output`) to `state/output/` and passes the
   result explicitly on the launch. Without the remap a literal `/output` made
   the proxy try to create it at the host filesystem root, fail its writability
   probe, and silently disable dumps (a reported bug: `harness host` wrote
   nothing even with `OUTPUT_DIR` set). A deliberate absolute host path passes
   through unchanged. **Config-change detection:** the reuse short-circuit
   (`host_proxy_running`) is gated on a fingerprint (`host_proxy_fingerprint`:
   port + the required upstream vars + `OUTPUT_DIR` + `proxy/requirements.txt`
   content, stored at `state/host/proxy.fp`); if any of those changed since the
   proxy started, the stale proxy is stopped and restarted rather than reused
   with old config (which on a port change would otherwise strand the readiness
   probe). Folding the requirements content in matters because the reuse
   short-circuit returns *before* `host_proxy_ensure_venv`, so an `upgrade` that
   bumps a proxy dep restarts the running proxy onto the rebuilt venv instead of
   leaving it on stale deps. `host_proxy_wait_ready`
   polls the loopback port via `/dev/tcp` (the proxy calls `app.run()` last, so
   an accepted connect means it is serving) and tails the log on failure (the
   common cause is `_validate_config` `sys.exit(1)`).
7. **Scoped opencode config** — `host_write_opencode_config` writes the same
   provider config shape as the container entrypoint's `ensure_opencode_config`,
   but `baseURL` → `http://127.0.0.1:<port>/v1` and built with `harness_jq`, to a
   **scoped** file (`state/host/opencode.json`). It asserts host `jq` is present
   up front (`host_preflight` guarantees it) so the `harness_jq` docker-sidecar
   fallback can never be reached from this no-docker path. The caller exports
   `OPENCODE_CONFIG` to point at it, so the user's global
   `~/.config/opencode/opencode.json` is never touched. `curl` is optional: the
   model dropdown comes from the proxy's `/v1/models` when present, else falls
   back to `DEFAULT_MODEL_NAME` alone.
8. **Launch** — `host_run_opencode` mirrors the entrypoint's `run_opencode`
   (provider env, `--agent yolo`, and the headless `-p` json-events +
   `opencode export` dance that dodges opencode 1.15.x's render race).
   Interactive runs opencode as a **child** (not `exec`) so the proxy is torn
   down on exit; print mode leaves the proxy up so a scripted `-p` loop doesn't
   thrash it (`harness host down` stops it) — the same interactive-vs-print
   teardown split as container mode. Because nothing reaps a `-p` proxy, print
   mode prints a stderr reminder after the run naming the live pid and the
   `harness host down` stop command. **Terminal reset on interactive exit:** after
   the interactive child returns (before `host_proxy_stop`), `host_reset_terminal`
   re-sends the xterm DECRST disables for mouse-tracking (`?1000`/`?1002`/`?1003`
   + the `?1005`/`?1006`/`?1015` report encodings), focus-reporting (`?1004`) and
   bracketed-paste (`?2004`), plus a cursor-show (`?25h`). opencode's TUI enables
   mouse-tracking but a crash or abrupt exit can return without disabling it,
   leaving the host terminal reporting — every later mouse move then dumps raw
   bytes (e.g. `35;77;12M`) into the shell. opencode owns the bug; harness owns
   the launch, so it cleans up on the way out. Container mode never hits this (the
   shell dies with the container), so this is host-interactive only. It is guarded
   to a real TTY on stdout (`[[ -t 1 ]]`) so a piped run never receives escape
   bytes, and print mode (`-p`) skips it (its stdout is data); DECRST is
   idempotent, so a clean opencode exit is a no-op. Alt-screen (`?1049`) is left
   untouched because opencode restores it on normal exit.

The host helpers have docker-free unit coverage in `tests/unit_host_test.sh`
(sourced via `HARNESS_SOURCE_ONLY=1`): `host_require_config` rejection,
`host_confirm_gate` auto-confirm, `host_preflight` missing-dep reporting,
`host_write_opencode_config` JSON shape + jq guard, `host_proxy_fingerprint`
stability, and (T9) that `cmd_host` runs the upstream auth gate like container
mode — aborting before `host_proxy_start` on a locked key, advancing through the
model pull into the proxy on a valid key, and bypassing the gate under
`HARNESS_SKIP_AUTH_PROBE=1`. The toolchain provisioner has its own docker-free coverage in
`tests/unit_host_toolchain_test.sh` (download-free): arch/platform mapping,
`host_sha_from_manifest` parsing, `host_sha256_check`, stamp-gated idempotency
and PATH assembly with stubbed binaries, the pins-match-`agents/Dockerfile`
drift guard, the Windows (Git Bash) layout branch (`.exe` tokens, root-level
node/opencode, `Scripts` venv, win-arm64 jq guarded — exercised on Linux by
stubbing `harness_detect_os`/`uname`), and `host_extract_archive` kind dispatch.

v1 is deliberately minimal: single CWD, no host-MCP wiring. It runs on Linux,
macOS, and Windows under Git Bash (MSYS); see [`WINDOWS.md`](../docs/WINDOWS.md)
→ "Host mode on Windows" for the opencode-under-Git-Bash caveats.
The egress firewall is genuinely container-bound (it lays a host-global iptables
default-deny that cannot be scoped to one process), so it is simply absent here —
see [`containers.md`](containers.md) → "Host mode has no firewall".

Container subcommands (`start`/`opencode`/`shell`) go through `require_docker`,
which now appends a "for a containerless run, use `harness host`" hint to its
unreachable-runtime failure so a host-only install points the user the right way.

`harness host -h`/`--help` prints host-mode usage and returns **before** any
preflight, confirm, or proxy-start side effect, so the help text is safe to
read on a box that can't actually launch.

**Host-only install detection.** `harness_runtime_installed` (`command -v
docker || command -v podman` — a *binary* presence check, distinct from
`harness_docker_running`'s daemon-reachability probe) is what lets the
maintenance subcommands behave on a box that never installed a runtime:
`cmd_upgrade` runs its docker-free path (see
[`install-and-upgrade.md`](install-and-upgrade.md) → "Host-only upgrade"),
`cmd_down` stops the host proxy + host MCPs and returns without
`require_docker`, and `cmd_restart` points the user at `harness host down &&
harness host` instead of erroring. A container install with the daemon merely
stopped still has the binary, so it does **not** take these paths — it hits
`require_docker` and is told to start the daemon.

**Debug breadcrumbs.** Host launches print the proxy log path
(`host mode: proxy log -> …`) on start, and `host_proxy_wait_ready` prints the
full log path on **both** failure branches (proxy exited during startup, or
never began listening) so a failed launch always points at the log that
explains why.

## ChatGPT backend (`harness chatgpt`)

`cmd_chatgpt` is a **front door, not a parallel launch path**. It sets the
`backend_override` global to `chatgpt` and then delegates to the exact code the
default launch uses: `run_agent opencode` for the bare form, `cmd_host` for
`harness chatgpt host` (so `harness chatgpt host down` works too). `-h/--help`
is intercepted before the global is set, so help has no side effects.

Everything the backend changes hangs off that one global:

| Site | Behavior when `backend_override=chatgpt` |
|---|---|
| `require_runtime_config`, `host_require_config` | required-var set swaps to `CHATGPT_BASE_URL` / `CHATGPT_MODEL_NAME` / `CHATGPT_COOKIE_STRING`; the `PROXY_API_*` + `DEFAULT_MODEL_NAME` trio is not required |
| `_gate_on_upstream_auth`, `_print_upstream_models` | early return — cookie auth has no bearer probe and the backend-api has no catalog |
| `_effective_default_model` | returns `CHATGPT_MODEL_NAME`; feeds `agent_model` (container) and `host_write_opencode_config` (host) |
| `write_runtime_override` | emits `PROXY_BACKEND: "chatgpt"` on the proxy service, folded into the **same** `proxy:` mapping as `PROXY_PROMPT_MODE` |
| `host_proxy_start` | passes `PROXY_BACKEND` in the launch env prefix |
| `host_proxy_fingerprint` | hashes the backend plus all three `CHATGPT_*` values |
| `firewall/init-firewall.sh` | guards `CHATGPT_BASE_URL`'s host against the allowlist instead of `PROXY_API_URL`'s |

**One proxy, one dialect.** Two landmines follow from the proxy being a single
shared service, and both are handled explicitly:

- *Container mode.* `ensure_services_up` is a no-op when the proxy is already
  up, so a backend flag would silently not take effect after a normal launch.
  `_running_proxy_backend` reads `PROXY_BACKEND` out of the running container's
  env (defaulting to `openai`, which is also what every pre-existing container
  reports) and `ensure_services_up` calls `cmd_start` on a mismatch. That
  restart drops a concurrent agent on the other backend; there is only one
  proxy, and answering from the wrong upstream is worse.
- *Host mode.* There is one pidfile, so the two host modes cannot coexist.
  Folding the backend and the `CHATGPT_*` values into `host_proxy_fingerprint`
  makes the existing config-change restart path handle the switch.

`CHATGPT_COOKIE_STRING` is a full session credential, so `_config_is_secret`
matches `*COOKIE*` and `harness config` masks it. `harness config set
CHATGPT_BASE_URL <url>` syncs the egress allowlist, exactly like
`PROXY_API_URL`.

A cookie also forced a fix in `_config_write_key`: it now double-quotes any
value that is not a bare word (`[^A-Za-z0-9_./:@=+,%~-]`), escaping `\ $ ` "`.
The wrapper sources `.env` under `set -euo pipefail`, so an unquoted
`a=1; oai-did=2` truncates at the semicolon and then runs `oai-did=2` as a
command — every `harness` invocation would die with "command not found".
`_config_read_key` already stripped quotes, so the round-trip is unchanged, and
bare values are still written bare (no churn for existing keys). This also
fixes the pre-existing case of a `PROXY_API_URL` containing `&`.

Covered by `tests/unit_chatgpt_test.sh` (docker-free) and the ChatGPT classes in
`proxy/test_proxy.py`.

## Update-available banner

`_update_check_and_banner` runs synchronously on every agent launch with
a tight (4s) timeout against `origin/main`. It caches the last successful
remote HEAD in `state/.harness-update-check` so users on flaky networks
still get the banner. Skipped entirely when `HARNESS_SKIP_UPDATE_CHECK=1`,
when not in a git checkout, or when not on `main`. Advisory only — never
gates a launch.

`cmd_check_updates` runs the same check in the foreground with a longer
budget for an explicit "am I up to date?" query.

## `harness_jq` fallback

The script uses `jq` to parse `scripts/upgrade-manifest.json` and the
net-overrides file. `jq` is a host dependency users often lack; when host
`jq` is missing the script runs jq inside the proxy image (which already
ships jq for its firewall scripts) so CLI jq calls work transparently.

The fallback does **not** spawn a container per call. `_ensure_jq_sidecar`
lazily starts one long-lived `sleep infinity` sidecar per `harness`
invocation, named `harness-jq-$$` — keyed on the main PID so every
subshell (`$(…)`, `< <(…)`) converges on the same name without shared
in-process state. Each `harness_jq` call is then a `docker exec` into
that sidecar, paying the ~container-creation cost once per invocation
instead of once per call (`harness upgrade` makes ~3 jq calls per merged
file — manifest fields are read as one `@tsv` row, the per-action result
is validated and accumulated in one `--argjson` call, and each merge
helper builds its summary object in a single `jq -n`).

Sidecar lifecycle:
- **Reap** — `_reap_jq_sidecar` removes it. Runs from an `EXIT` trap for
  normal commands; the agent-launch / `mcp logs` paths `exec` the runtime
  (bypassing the trap) so they call `_reap_jq_sidecar` explicitly just
  before `harness_docker_exec`. A per-PID marker file under `state/`
  makes the reap a cheap no-op when no sidecar was created.
- **Sweep** — `_sweep_stale_jq_sidecars` removes `harness-jq-<pid>`
  containers whose owning process is no longer alive (crash, SIGKILL).
  Called once per jq-less invocation from `_ensure_jq_sidecar` and again
  from `cmd_down`.

On a fresh install with no proxy image yet, the fallback inline-builds
the proxy image (one-time, ~2–5 min) with a loud warning so the user
doesn't think the script froze. That build re-enters `compose()` →
`write_runtime_override`; the `_harness_wro_in_progress` guard there
breaks the recursion.

## `doctor` and `preflight`

- `cmd_doctor` — diagnostic-only. Reads runtime state, never modifies.
  Reports `[network]` (allowlist path, host count, whether `PROXY_API_URL`'s
  host is on the allowlist, any services with active overrides), `[mcp]`,
  `[runtime]`, etc. On Windows, `[runtime]` adds an `interactive tty` line
  that probes whether `docker.exe` accepts `-t` in the current terminal and
  whether winpty is needed/present (see "Interactive TTY resolution" below) —
  the probe is a throwaway `--rm` container, so doctor stays read-only.
  **Host-only awareness:** when `harness_runtime_installed` is false, the
  runtime/compose checks downgrade `fail` → `warn` (and the allowlist-missing
  check reads `n/a (host-only)`) so doctor exits 0 on a valid containerless
  box, and a dedicated `[host]` section reports the host-mode prerequisites
  (`python3`/`jq`/`node`/`opencode`, Node >= 20) plus whether the host venv
  exists and whether the host proxy is currently running (pid/port/log).
  The final `[upstream]` section is doctor's one **live network probe**: it
  curls the upstream `/v1/models` from the host (bounded timeouts) so a user
  diagnosing "the AI never responds" gets the real reachability verdict.
  `2xx` → ok; `401`/`403` (key rejected) and `404` (wrong endpoint) → `fail`;
  no response retries with `-k` to tell a dead network (warn) from an untrusted
  TLS cert (warn, with a `PROXY_API_CACERT` hint) apart. **Network-down is a
  warn, not a fail**, so `harness doctor` still exits 0 when merely offline.
  Skipped when `PROXY_API_URL`/`PROXY_API_KEY` is unset, `curl` is missing, or
  `HARNESS_SKIP_AUTH_PROBE=1`.
- `cmd_preflight` — validates `.env`, allowlist, and docker daemon
  reachability BEFORE `harness start`. Fails loudly on config errors so
  the user doesn't get an opaque compose error. On a host-only box it
  prints an `ⓘ … validating host mode` note, checks the host-mode prereqs
  (`python3`/`jq`/`node`/`opencode`, Node >= 20) instead of the daemon, and
  skips the allowlist file-exists and `PROXY_API_URL`-in-allowlist checks
  (the firewall allowlist only governs container mode).

## Shared libraries under `scripts/lib/`

- `platform.sh` — OS detection, container runtime detection, portable
  realpath, jq helpers. Sourced by the harness script after the inline
  self-locate runs.
- `net_helpers.sh` — lazily sourced (`load_net_helpers`) so the
  no-op startup cost on commands that don't touch the network stays
  near zero. Owns the allowlist + overrides JSON manipulation used by
  `cmd_net_*`.
- `upgrade_actions.sh` — the action functions called by `cmd_upgrade`.
  See [`install-and-upgrade.md`](install-and-upgrade.md).

## Tests

Tests scoped to this file live in `tests/harness_test.sh` (and any
future `tests/harness_*_test.sh`). End-to-end install → start → agent
launch is exercised by `tests/full_pipeline_test.sh`. See
[`tests.md`](tests.md).
