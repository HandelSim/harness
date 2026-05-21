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
so it can read values like `PUBLISH_OLLAMA_PORT`, `OLLAMA_AGENT_MODEL`,
`HARNESS_EXTRA_MOUNTS` for its own logic. `docker compose` gets `.env`
separately via `--env-file`. The two consumers are independent.

`OLLAMA_AGENT_MODEL` has four independent consumers — the CLI
(`agent_model=`), `docker-compose.yml`, `ollama/entrypoint.sh`, and
`agents/entrypoint.sh` — and **all four fall back to the same default,
`GenAI`**, when the var is unset/blank. This invariant matters: the ollama
side registers the stub model under this name and the opencode side points
the agent at it, so a divergent default makes opencode request a model name
ollama never created (silent 404s). Keep the four in sync.

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

1. **`PUBLISH_OLLAMA_PORT`** — when set in `.env`, exposes ollama on the
   host. Default is internal-only.
2. **Per-service firewall opt-out** — for every service with
   `firewall_disabled: true` in `state/.harness-net-overrides.json`, an
   `environment: HARNESS_FIREWALL_DISABLED: "1"` block is emitted. The
   firewall init script short-circuits on that variable. We never replace
   the service's entrypoint or remove its `cap_add`.

The `agent` pseudo-service is filtered out: agent containers are launched
by direct `docker run` from `run_agent` (opencode) / `cmd_shell`,
not by compose. The agent launch path reads `.harness-net-overrides.json`
directly.

If both sources are empty, the file is deleted rather than written empty
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
  `[runtime]`, etc.
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
