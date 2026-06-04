# `harness` CLI

The `harness` bash script is the management entrypoint. ~4200 lines, one
file. Symlinked into `~/.local/bin/harness` by `harness-install.sh`; users
invoke it from anywhere.

## Self-locate and install-root resolution

The script resolves `$0` through any wrapper or symlink using a portable
realpath (so it works on Windows Git Bash too) to find:

- **`clone_dir`** — where the code lives (this script + `docker-compose.yml`
  + `scripts/`).
- **`install_root`** — where user config (`.env`, `.harness-allowlist`) and
  runtime state (`state/`) live.

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

## Subcommand surface

`cmd_help` is the source of truth for the user-facing subcommand list.
The implementation has one `cmd_<name>` function per subcommand:

| Group | Subcommands |
|---|---|
| Lifecycle | `start`, `down`, `restart`, `logs`, `unlock` |
| Update / upgrade | `update`, `upgrade`, `downgrade`, `check-updates` |
| Agent launch | `opencode`, `shell`, `list`, `stop` |
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
   harness isn't the launcher.
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
collision. The `--print` path sets no name or labels and was already
concurrent.

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

Bash on MSYS rewrites Unix-form path arguments to Windows form before
invoking native `.exe` binaries: `/c/Users/foo` in `argv` becomes
`C:/Users/foo` by the time `docker.exe` sees it. The inline
`MSYS_NO_PATHCONV=1` prefix on `harness_docker` / `harness_docker_winpty`
reaches the child process's environment but the conversion has already
happened in bash. The Linux docker daemon then rejects `-w C:/Users/foo`
with *"the working directory is invalid, it needs to be an absolute
path"*. The bind-mount source is unaffected because `harness_docker_path`
emits Windows-form (`C:/...`), and the `-v src:tgt` composite is not
single-Unix-path-shaped so MSYS leaves it alone.

`harness_container_workdir` (`scripts/lib/platform.sh`) wraps the `-w`
arg in the MSYS UNC-escape: it prefixes the resolved `mount_path` with
`//` on Windows (pass-through elsewhere). MSYS skips conversion for args
starting with `//`, the Linux kernel collapses `//foo` to `/foo` on
`chdir`, and the container working directory ends up matching the
bind-mount target. The three docker launch sites (`run_agent_interactive`,
`run_agent_print`, `cmd_shell`) all pass `-w "$(harness_container_workdir
"$mount_path")"`. `harness doctor`'s Windows `[runtime]` block surfaces
both the resolved `mount_path` and the escaped `-w` form so this failure
mode is one-line diagnosable.

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

### Last-agent stack teardown (opt-in)

`HARNESS_STOP_ON_LAST_AGENT=1` binds the shared stack's lifecycle to the
agents'. After an **interactive** agent exits, `run_agent_interactive`
counts the remaining agent containers for the project
(`label=harness.project=<p> label=harness.agent=true`); the just-exited
container is already gone (`--rm`), so a zero count means it was the last
one, and the proxy + enabled MCPs are torn down via `cmd_down`.

Default is **off** — the established model is "start once, launch many":
`ensure_services_up` brings the stack up on the first launch and it persists
so later launches hit the fast already-running path. With the flag on, each
later launch re-starts the stack (cheap when images are already built;
slower the first time MCP images must build). The flag is read at exit, so
it can be set per-session or in `.env`. Scope notes: only interactive
launches participate (`--print` agents are unlabeled, so they neither count
nor trigger teardown — don't run print-mode agents concurrently with this
flag set, or an interactive exit can stop the stack out from under them);
the count is project-scoped, so concurrent interactive agents keep the stack
up until the last one exits. This is the inverse of `cmd_down`'s own
agent-stop sweep, which tears agents down when the *stack* goes down.

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
- `cmd_preflight` — validates `.env`, allowlist, and docker daemon
  reachability BEFORE `harness start`. Fails loudly on config errors so
  the user doesn't get an opaque compose error.

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
