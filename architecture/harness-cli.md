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
  `reminder.md`, `tool-guidance.json`) and runtime state (`state/`) live.

In production these are the same directory: `harness-install.sh` clones
the repo and that clone IS the install root. Tests set
`HARNESS_INSTALL_ROOT` to a tmpdir to keep `clone_dir` resolved to the
real repo while pointing `install_root` somewhere disposable. On Windows a
`C:\...` or `C:/...` `HARNESS_INSTALL_ROOT` (as a Windows program such as the
`--test-modes` runner passes it) is normalized to Git Bash's `/c/...` form
(`harness_posix_path`): a drive colon would split the vendored toolchain dirs
host mode prepends to the `:`-separated `PATH`.

The portable resolver is duplicated inline here rather than depending on
`realpath`; the full equivalent lives in `scripts/lib/platform.sh` as
`harness_realpath`.

## Env loading

The script sources `.env` into its own shell (`set -a; source; set +a`)
so it can read values like `DEFAULT_MODEL_NAME`, `HARNESS_EXTRA_MOUNTS`
for its own logic. `docker compose` gets `.env` separately via
`--env-file`. The two consumers are independent. `.env` holds the upstream
key, so after sourcing the script drops group/other access to it
(`chmod go-rwx`, only when the invoking user owns it). The CLI's own
upstream calls (auth probe, model lists, doctor) hand curl the
`Authorization` header as a config on stdin (`_curl_auth_config | curl -K
-`), so the key does not show in `ps` (`harness config set PROXY_API_KEY
<key>` still takes it as an argument).

Independent, but not equivalent: compose's dotenv parser reads an unquoted
value to end of line, while `source` ends the assignment at the first space or
shell metacharacter and treats the remainder as a command. A hand-pasted
`CHATGPT_COOKIE_STRING=a=1; oai-did=2` therefore either kills every harness
command (`oai-did=2: command not found`, exit 127) or — when the remainder
parses as an assignment, or the separator is `&` — is silently cut down to the
first cookie pair, and the proxy authenticates with half a cookie for an
unexplained upstream 401. `_warn_unquoted_env_values` scans the file **before**
sourcing and names the offending keys, so the cause prints above the symptom.
It warns rather than aborts: `harness config set` is the documented repair and
has to stay reachable. `_config_value_truncated` is the same fault seen from
the other side — the sourced value is a strict prefix of what
`_config_read_key` reads off disk — and turns `harness chatgpt doctor`'s cookie
line from a green "set, N chars" into a fail that names the real length.

Required config (`_openai_backend_missing`, used by `require_runtime_config`,
`host_require_config` and mirrored by preflight/doctor): the vendor trio
(`PROXY_API_URL`, `PROXY_API_KEY`, `DEFAULT_MODEL_NAME`), the OAI trio
(`OAI_API_URL`, `OAI_API_KEY`, `OAI_MODELS`; [`upstream-api.md`](upstream-api.md)),
or both. With an OAI upstream `DEFAULT_MODEL_NAME` may be empty and
`_effective_default_model` falls back to the first OAI model. Not
`OPENAI_API_KEY`: `.env` is exported and opencode and codex would pick it up.
`_probe_oai_auth` (in `_gate_on_upstream_auth`) GETs `{OAI_API_URL}/models`,
which costs nothing: 401/403 aborts the launch, anything else only warns.

`DEFAULT_MODEL_NAME` (which replaced the old `PROXY_API_MODEL`) is the default/fallback model id and, on a vendor-only install, is **REQUIRED with no
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
| Diagnostics | `doctor`, `preflight`, `probe`, `usage`, `help` |
| Vaults | `vault` (list), `vault add`, `vault new`, `vault use`, `vault off`, `vault remove`, `ticket`, `context`, `queue` — see "Vaults", "Tickets and context" and "Ticket queue" below. |
| Pub page | `serve` — the vault's pub pages plus opencode web, loopback only. See "`serve`" below. |
| Test / bench | `test`, `benchmark` — `benchmark --test-modes` is the docker-free hybrid-vs-single A/B (below); the other `benchmark` targets are the harbor runners under `tests/benchmarks/`. |
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

Agent-launch flags (`--yolo`, `--normal`, `--thoughts`, `--net`, `--mount`, `-p/--print`) are parsed
inside `run_agent` (opencode) / `cmd_shell` rather than centrally; they decide
the `docker run` invocation, not compose flags. `run_agent` also still accepts
the retired `--require-tool` and drops it as a no-op (see "`--require-tool` on
a bare launch").

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
  - `config get [KEY]` / `config list` print values (`config list --tsv`:
    `KEY<TAB>shown value<TAB>secret 1/0<TAB>description`, secrets masked, for
    serve's Settings page); `config set KEY VALUE` writes one. **Not a whitelist** — `set` writes any key; the picker/`get`
    just surface the common ones.
  - Reads use `_config_read_key` (straight from `$env_file`, so they reflect
    on-disk state). Writes use `_config_write_key` — the same atomic
    temp-file + `mv` read-loop the installer uses (no `sed`, so values with
    `/ & : @` need no escaping; appends the key if absent).
  - Secret-ish keys (name matches `*KEY*|*SECRET*|*TOKEN*|*PASSWORD*` via
    `_config_is_secret`) are shown masked (`(set, N chars)`) and prompted with
    a hidden read.
  - Setting an upstream URL (`PROXY_API_URL`, `OAI_API_URL`,
    `CHATGPT_BASE_URL`) touches only `.env`: the proxy's firewall allows those
    hosts from its environment at start
    ([`containers.md`](containers.md) → Universal egress firewall), so there is
    no allowlist entry to sync. `cmd_doctor` and `cmd_preflight` still fail a
    URL with no parseable host (no scheme), which the firewall refuses.
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
   harness isn't the launcher. `cmd_start` separately seeds the user's two
   editable reminder-data files into `<install_root>/` —
   `seed_reminder_file` → `reminder.md` (from the tracked
   `proxy/reminder.md`) and `seed_tool_guidance_file` →
   `tool-guidance.json` (from `proxy/tool-guidance.json`), both thin wrappers
   over `seed_user_data_file`, which also migrates a copy left by either
   earlier layout. (The retired `reminder-require-tool.md` is no longer
   seeded; an old install's copy is simply unused.) They ride on the `INSTALL_ROOT` export above rather than a
   variable of their own, because they keep their tracked basenames; an edit
   to either takes effect on `harness restart` with no rebuild — see
   `architecture/proxy.md` → "Editable reminder data".
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
   <mode>` (`_parse_start_flags` → `_validate_prompt_mode_override` accepts
   `hybrid`/`user_front`/`passthrough`/`single`; `--single-message` is
   shorthand for `--prompt-mode single`) sets the `prompt_mode_override` global, which adds
   `environment: PROXY_PROMPT_MODE: "<mode>"` onto the proxy service. This is
   the only path that sets `PROXY_PROMPT_MODE` for the proxy now that
   `docker-compose.yml` no longer interpolates it (a stale `.env` value is
   inert). It is **not persisted** — a later bare `start`/`restart` regenerates
   the file without it, reverting the proxy to its built-in `hybrid` default.
   When the proxy *also* has a firewall opt-out (1), the prompt mode is folded
   into that same `proxy:` block rather than emitted as a second mapping
   (duplicate top-level service keys are invalid compose YAML).

No require-tool key is ever emitted: the proxy runs require-tool mode on every
launch but `passthrough` (see `architecture/proxy.md` → "Require-tool mode"),
and `docker-compose.yml` no longer passes `HARNESS_REQUIRE_TOOL` either.
`_parse_start_flags` still accepts `--require-tool` and ignores it, so old
scripts keep working; any other unknown option is still rejected.

The `agent` pseudo-service is filtered out: agent containers are launched
by direct `docker run` from `run_agent` (opencode) / `cmd_shell`,
not by compose. The agent launch path reads `.harness-net-overrides.json`
directly.

If every source is empty, the file is deleted rather than written empty
so compose doesn't see a phantom services block.

## Agent launch path

`run_agent` (opencode) / `cmd_shell` do NOT go through compose.
They each:

1. Parse agent flags (`--yolo`, `--normal`, `--thoughts`, `--net`, `--mount`, `-p/--print`; the
   retired `--require-tool` is consumed and ignored). In `run_agent`, if no
   container runtime is installed at all, hand off to host mode here — see
   "No-docker fallback" below.
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

### No-docker fallback

When `harness_runtime_installed` is false — neither a `docker` nor a `podman`
binary on `PATH`, i.e. a host-only install — `run_agent` (bare `harness`,
`harness opencode`, `harness <agent flags>`, `harness chatgpt`) prints
`harness: docker is not available (no docker or podman installed); defaulting
to 'harness host'` to stderr and calls `cmd_host` with `--yolo` (if given;
`--normal` and `--thoughts` carry over as the `harness_slim` and
`harness_thoughts` globals) plus the pass-through args (e.g. `-p PROMPT`). `--net` and `--mount` have no
host-mode meaning (no firewall; the whole filesystem is visible) and are
dropped, with a note for `--mount`. Host mode's own confirm gate still applies.

A runtime that is installed but **not running** is deliberately not
downgraded: `require_docker` still tries to auto-start it and otherwise errors
with a pointer to `harness host`. Silently dropping the sandbox and firewall
over a stopped daemon is worse than an error.

### `--require-tool` on a bare launch

`--require-tool` used to opt the proxy into require-tool mode; that mode is now
always on (except under `passthrough`), so the flag is a **no-op kept for
compatibility**. `run_agent`'s flag loop (like `cmd_host`'s and
`_parse_start_flags`) still consumes it so it is never appended to `pass_args`:
opencode aborts on an unknown option, so forwarding it would stop the agent
starting. Nothing is reconciled against the running proxy any more, and the
help texts no longer advertise the flag.

### Slim (default) and `--normal`

Every launch is slim unless `--normal` is given (`run_agent`, `cmd_host` and
`cmd_serve` set the `harness_slim` shell global to 0; unset means 1; `--slim`
is still accepted and sets 1). It is a flag only, not an `.env` setting.
Slim makes metered requests smaller. `slim_opencode_config` prints a
one-line JSON overlay that goes to opencode as `OPENCODE_CONFIG_CONTENT` (a
`-e` from `agent_common_env` in container mode, an export in
`host_run_opencode`); opencode deep-merges it over every other config layer.
It only adds the provider header `x-harness-slim: 1` (on `harness`, and on
`harness-oai` with an OAI upstream, host or container); no agent prompts and
no `steps`. Everything else happens in the proxy, on the native route only
(`-withtools` and OAI models; [`proxy.md`](proxy.md)): short tool
descriptions, `agents/slim-prompt.md` in place of opencode's prompt for the
main agents and `agents/slim-subagent-prompt.md` for the `general`
subagent (opencode's environment block, AGENTS.md/instructions and skills
list are kept), a 15-step hard cap on `general`, `explore` sent to the free
counterpart of a `-withtools` model, old tool output trimmed and the
per-step reminder. The main agents have no cap, since a long task may need
many steps. Free models on the cooperative route ignore the header, so they
get opencode's request exactly as before slim existed.

`agents/slim-prompt.md` covers: quick orientation and whole-file reads when
most of a file is needed, otherwise narrow search and ranges; wide searches
delegated to a subagent with one narrow question, continuing from its
report; batched tool calls; the todo list; verifying every change; smallest
change; honesty. `agents/slim-subagent-prompt.md`: answer only the question,
at most 15 steps, grep first, report with `path:line`. The proxy reads them
once per process: a host proxy from the clone (its fingerprint includes both
files, so editing one restarts it), the container proxy from its image
(`proxy/Dockerfile` copies them). Under `--normal` there is no header:
opencode's own system prompt and tool descriptions are sent and the proxy
forwards native requests as is. Slim has no tuning knobs; its values are
proxy constants, and comparing settings is done with a probe.
Under `serve` the provider-wide slim header is left out and set per model
instead (see [`serve`](#serve)), so a user can switch modes in the web UI.

### `--thoughts`

Off by default, in slim and `--normal` alike (`run_agent`, `cmd_host` and
`cmd_serve` set the `harness_thoughts` global to 1). `thoughts_opencode_config`
adds the header `x-harness-thoughts: 1` to the `harness` provider only (not
`harness-oai`); `harness_opencode_overlay` deep-merges it with the slim and
vault parts. On that header the proxy asks the upstream for thought
summaries and hands them to opencode as `reasoning_content`, so they show
in opencode's thinking block ([`proxy.md`](proxy.md)). Replies then arrive
whole rather than token by token, since a stream carries the summary as
untagged content. Under `serve` with slim on, the header is set per model
instead (see [`serve`](#serve)).

### Vaults

A vault is a git repo of notes and docs the agent works from (the "brain").
`cmd_vault` keeps a registry at `state/vaults.list` (`name<TAB>url` per
line) and the active name at `state/vault.active`; there is no `.env`
setting. `add` registers (the first vault becomes active), `use` switches,
`off` clears, `remove` unregisters and never deletes the clone.

`new <name> [--from <git-url>]` makes a vault in place: it copies
`vault-template/` (or a `--depth 1` clone of the template repo, whose `.git`
it drops), runs `git init` on `main` with one commit (signed `harness` when
git has no identity), and registers the url `local`; on a terminal it then
asks for the charter (below). A failure removes the folder and registers
nothing. `vault-template/` is an LLM-wiki skeleton (Karpathy's pattern) built
so that harness, not the model, owns the structure: `AGENTS.md` (how to find
things: grep the index, then the wiki; the page-name scheme; where an agent
may write, `wiki/notes/`; never edit generated pages or run git),
`wiki/overview.md` (the charter), `wiki/index.md` and `wiki/log.md`
(generated), `.raw/` for sources, `pub/` for `serve`, and `.obsidian/`
(hides `.raw`, `pub`, `.harness` and colours the graph by folder) so the
folder opens as an Obsidian vault, and `.gitattributes` (`* text=auto
eol=lf`, so a Windows checkout is LF too; `wiki/log.md merge=union`, so two
machines' appends do not conflict). A `local` vault has no remote until the
user adds one with git; `list` shows its `origin` url once there is one.

`init` (`_vault_charter`) asks, on `/dev/tty` with no model, what the active
vault is about: project, goal, readers, what to track, a ticket id regex
(validated; empty for none), the folders it covers and the Jira URL
(`tracker_url`, an https base URL, http only for localhost/127.0.0.1,
checked by `_tracker_url_ok`, and, when set, `tracker_fields`, extra
field ids or names; see "Tickets and context"). Each answer is a
readline line (`read -e -i`) pre-filled with the current answer or a
suggestion, so it can be edited in place; it writes them to `wiki/overview.md` as single-quoted YAML front
matter plus a body; then `vault_sync`. `harness ingest` sends the charter
with every request and makes a page per ticket id the regex finds.

At launch `vault_prepare` (from `run_agent` after the no-docker fallback,
and from `cmd_host` before `host_run_opencode`) clones the active vault into
`state/vaults/<name>` if it is missing, otherwise runs `git pull --ff-only`
with no credential prompt and a 30s limit (skipped when the branch has no
upstream, as for a new vault); a failed pull or clone warns and never
blocks the launch, and a `local` vault whose folder is gone launches
without a vault. The working tree is kept LF (`_vault_eol`): the clone is
made with `-c core.autocrlf=false`, and an existing clone gets
`core.autocrlf=false` in its own config once, unless that config already
sets it; one checked out CRLF (Git for Windows' default `true`) is checked
out again (`read-tree HEAD`, which drops the stat data `checkout-index`
would otherwise trust, then `checkout-index -a -f -u`) when nothing tracked
is uncommitted.
The parsers in `vault_ingest.py` and `vault_ticket.py` read CRLF as LF
anyway (`read_text` normalises, `frontmatter` and the unit/link markers take
`\r\n`), so a unit's hash is the same on both systems. It sets the `harness_vault_dir` global, which
`vault_opencode_config` turns into overlay keys: `instructions` with the
vault's `AGENTS.md` (or `CLAUDE.md`), which opencode sends as "Instructions
from: <path>" so the model knows where the vault is,
`permission.external_directory` `"<vault>/*": "allow"` so reading and writing
there needs no approval, and the provider header `x-harness-vault: <path>`,
from which the proxy adds a line naming the vault and how to search it to a
cooperative (free) model's per-turn reminder (see `proxy.md`); a native model
keeps the instructions in its system prompt and gets no line.
On a Windows host launch (`host_opencode_env` sets `harness_native_paths=1`)
the vault and instruction paths go in as `C:/...` (`cygpath -m`): the
Windows opencode resolves every path the agent touches to `C:\...`, so a
`/c/...` rule would never match (each vault read asked, and a headless `-p`
run rejects asks) and a `/c/...` instruction file would not be found.
`harness_opencode_overlay` deep-merges that with the slim overlay into the
one `OPENCODE_CONFIG_CONTENT`. Container mode also appends the vault to the
extra mounts (same absolute path, deduped against the CWD).

Saving is harness's job, not the agent's. When an interactive session ends
(`host_run_opencode` after opencode exits, `run_agent_interactive` after
`docker run` returns, both on the host), `vault_sync` runs
`scripts/vault_capture.py` on opencode's SQLite store (host:
`${OPENCODE_DB:-${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db}`;
container: `state/agent/home/.local/share/opencode/opencode.db`), read-only.
Each top-level session started in the launch directory (host: `$PWD`;
container: the mount path) and updated in the 30 days before the launch
(so a session whose harness was killed is caught by the next one) becomes
`.raw/sessions/<YYYY-MM-DD>-<session id>.md`: front matter, then the user's
and the assistant's text (no reasoning or opencode-injected text), each tool
call as one line naming what it touched (`read` path and range, `edit`/
`write` path, `grep`/`glob` pattern, path and include, the first 160
characters of a `bash` command, a `task`'s agent and description plus up to
1500 characters of its report; no other tool output), with anything shaped
like a key or token redacted (best effort). The file is rendered from the
whole session every time: opencode's compaction adds a marker and a summary
message and deletes nothing, so every message from before any number of
compactions stays, the marker as `## Compaction` and the summary as
`## Compaction summary`. A rerun never shrinks a file (an undo or revert in
opencode keeps what was captured). The directory filter matters: that store
is shared by every opencode session of the user (host) or of every harness
container, and without it other projects' conversations would be pushed to
the vault's remote. `import [<dir>|--all]` runs the same capture with no
time limit over both stores, for sessions from before the vault existed
(`--all` drops the directory filter). Then `scripts/vault_ingest.py index`
rebuilds the generated index and hubs (no model), `git add -A`, one commit
for everything the session changed in the vault (unsigned, `-c
commit.gpgsign=false`, as are `vault new`'s and ingest's commits: a signing
key on a smart card would ask for its PIN on every launch, and a headless run
would wait on it; `HARNESS_VAULT_SIGN=1` keeps git's setting for a remote that
takes only signed commits; with no `user.name`/`user.email` the commit is
harness's, `harness <harness@localhost>`), and a push when the branch
has an upstream (no credential prompt, 30s limit; on a rejected push one
`pull --rebase --autostash`, unsigned and with the same identity, and a
second push). A rebase that stops on conflicts only in generated files
(front matter `generated: harness`, or `wiki/index.md`, `wiki/log.md`,
`wiki/topics/`, `wiki/tickets/`) takes the remote's copy of each
(`_vault_rebase_generated`; in a rebase `--ours` is the remote), goes on,
reruns `vault_ingest.py index` and commits "regenerate after rebase"
before pushing; any other conflict aborts the rebase and leaves the commit
local. `.harness/ingest.json` is not in that list: a conflict there (two
machines ingesting the same units) aborts. Every git call on a vault
(`_vault_g`, and `_vault_git` with the time limit; `vault_ingest.py`'s
`git()` likewise) runs with `-c core.hooksPath=<empty folder>` and `-c
core.fsmonitor=false`: in container mode the agent can write the clone's
`.git`, and hooks and fsmonitor are programs git would run on the host
(bash uses `state/no-hooks`, `C:/` form on Windows; python a temporary
folder of its own). Other `.git/config` keys that run programs (filter
drivers, `core.sshCommand`, credential helpers, `include.path`) are not
overridden. Every failure warns and the exit status is unchanged;
without Python the capture is skipped and the commit still happens. The push
uses the host's git credentials, so the container firewall is not involved.
Print mode (`-p`) and `serve` do not sync. Clones live under `state/`, so
`uninstall` deletes them; it first names any clone with uncommitted or
unpushed work (a vault with no upstream counts as unpushed).

`harness ingest [<dir>]` (`cmd_ingest`) turns raw material into wiki pages.
It starts the host proxy as `serve` does (stopping it after, if it started
it), `vault_prepare`s the active vault and runs `scripts/vault_ingest.py
ingest --commit` against `http://127.0.0.1:<host proxy port>/v1`. The design
(chosen by council) keeps every decision in code so a free model with no
tools does it as well as a `-withtools` one: harness picks the units (a
`.raw` file cut at its `## ` headings, or one codebase directory's tracked
text files, packed to `--unit-kb`, default 48; files that look like secrets,
`SECRET_PATH` copied from `ticket_queue.py` plus `*.p12`, `credentials*`,
`secrets*` and `secrets/`, are never read), sends each as one plain
request with `x-harness-raw: 1` (no tools, no reminder; a `-withtools` model
is swapped for its free base when `/models` lists it), with the rules before
and after the source, the charter, as context the summaries of the page's
other parts and (code, done deepest first) of its subdirectory pages, and a
fixed section format (summary line, headings, `Topics:`). The reply is
checked (a summary line, a body, backticked names that exist in the source),
sent back once with the problems, then marked failed (`--retry-failed` sends
it again, once per run: a `--watch` sleeps rather than resend a unit that
failed in this run, until its source changes; a pass that wrote nothing
sleeps). Upstream errors do not count as attempts; three in a row stop the
run. Each unit's text goes between `<!-- unit:ID sha:SHA -->` markers in its
page (`wiki/code/<repo>/<repo>--<dir>.md`, the root `wiki/code/<repo>/<repo>.md`;
`wiki/sources/<file>.md`), so a page is rebuilt section by section and a
`<!-- links -->` block written by hand survives. `.harness/ingest.json` keeps
each unit's content hash and page: only new or changed units are sent, and
units (and generated pages) whose source is gone are removed. A codebase
given once is remembered by name and absolute path. The vault is shared
between machines (`C:/x` on Windows, `/home/x` on Linux), so a remembered
codebase whose folder is not on this host is not "gone": its units and pages
are left alone, with one warning per run; `<dir>` with the same folder name
takes it over when the stored path is not on this host (and is refused when
it is). After the units, `regenerate` writes
`wiki/index.md` (top-level pages, one line per codebase, sources, topics,
tickets, each with its summary line), a hub per topic used by two or more
pages (`wiki/topics/`), a page per ticket id (`wiki/tickets/`), and each
repo's module tree with Up/Subdirectories links, and appends to
`wiki/log.md`; it commits and pushes every 10 units and at the end, Ctrl-C
(or SIGTERM) included. A lock, `.git/harness-ingest.lock` (`flock`, or
`msvcrt.locking` on Windows; the vault's top folder when it has no `.git`),
keeps two runs apart: `ingest` and `forget` exit 3 when it is held. Under it
an ingest (not `--dry-run`) keeps `.git/harness-ingest.run.json` (`RunFile`):
`{pid, started, beat, model, args, step, unit, done, failed, left, watch}`,
times as `YYYY-MM-DDTHH:MM:SSZ`, step one of `planning`, `ingesting`,
`indexing`, `committing`, `waiting` (a `--watch` sleep), rewritten every 5 s
by a thread and after each unit, removed when the run ends. A run file whose
pid is not alive (`os.kill(pid, 0)`, `OpenProcess` on Windows) or whose beat
is 120 s old is a run that was killed. `vault_ingest.py status [--json]
[--quick]` (`harness ingest --status`, on the local copy: no pull, no proxy)
takes no lock and reports the run file plus `alive`, each codebase (path,
whether it exists here, units, ok, failed, pending), the same counts for
`.raw`, the failed units with their problems (at most 200), the last
`ingest:` line of `wiki/log.md` and the number of wiki pages; pending cuts
and hashes the sources as the last ingest did (`unit_kb` is kept in the
manifest), `--quick` reads only the manifest and leaves pending null.
`forget NAME` (`harness ingest --forget NAME`, pulled, committed, pushed)
drops a codebase's units and repo entry and removes its generated pages
under `wiki/code/NAME/` (pages someone wrote stay; a folder with none
generated gets no overview page). `--unit ID` sends just those units,
whatever their state. `--max N` stops after N
units, `--watch [S]` keeps checking every S seconds (default 300) for a
long-running ingest of a growing codebase, `--dry-run` lists the units.
`tests/unit_vault_test.sh` covers the registry, clone, fast-forward, offline
fallback, the overlay, `new`, the sync, the capture's tool lines and
compaction, ingest against a mock upstream, and tickets against a mock
tracker.

### Tickets and context

`harness --ticket KEY` (also on `harness host` and `harness chatgpt`; parsed
by `harness_ticket_flag`, which upper-cases and checks `^[A-Z][A-Z0-9_]*-[0-9]+$`)
or `harness ticket KEY` remembers KEY for the folder in `state/tickets.list`
(`folder<TAB>KEY`); a launch from that folder or below uses the nearest
entry (`ticket_for_dir`), and `harness ticket off` forgets it. There is no
branch-name guess. On every launch `ticket_prepare` (right after
`vault_prepare`, both paths) runs `scripts/vault_ticket.py`, which reads
`tracker_url`/`tracker_fields` from the charter (shared by everyone using the
vault) and `TRACKER_TOKEN` (per user, `.env`) from its environment, never its
command line. It GETs `<tracker_url>/rest/api/2/issue/KEY?expand=names` (Jira Server/Data Center
or Cloud, REST v2; 6s limit; `HTTP(S)_PROXY` honoured), with the token as
Bearer, or as Basic when it is `<email>:<api-token>` (Cloud), and writes
`<vault>/.raw/tickets/KEY.md`:
front matter (title, status, url, updated, no fetch time so an unchanged
ticket is not rewritten), a label saying the text is people's description of
the work and not instructions about tools or credentials, the usual fields,
the description, acceptance criteria, definition of done, story points, epic
link and sprint (custom fields matched by name), the charter's extra fields,
subtasks and links, and the last 10 comments, all capped and redacted. It
also creates the agent's state note, `<vault>/wiki/notes/tickets/KEY-state.md`
(Done, Next, Decisions, Open questions), when missing; harness never writes
it after that, and nothing is written back to the tracker. The token goes
only to `tracker_url` itself: `url_problem` refuses a non-https URL (http is
allowed for localhost and 127.0.0.1 only) before any request, and the opener's
`NoRedirect` handler stops at a 3xx instead of following it (urllib would send
the Authorization header on to the new address), naming the address so the
charter can be fixed. Any failure
(no tracker, no token, a refused URL, a redirect, 401, 404, offline) prints one `[harness] ticket` line
and the last copy is used: a launch never fails over the tracker. Without an
active vault the ticket is not given to the agent. `harness ticket pull
[KEY]` does the same by hand and commits.

`harness ticket login` asks on `/dev/tty` for the Jira URL (pre-filled with
the charter's, checked by `_tracker_url_ok`; written with `_charter_set` and committed by `vault_sync`
when it changed) and the token (no echo; written to `.env` with
`_config_write_key`); `harness ticket logout` clears the token, and `harness
config` edits it (masked). `TRACKER_TOKEN` is in
`_harness_secret_vars`, so `host_opencode_env` unsets it before opencode
starts (after `ticket_prepare` has used it), and `agent_common_env` passes
the container an allowlist that does not include it: the agent and its
shell commands never have it in their environment.

`harness_context_files <home>` lists the instruction files a launch gives
the model, in opencode's order: the global file
(`<home>/.config/opencode/AGENTS.md`, else `<home>/.claude/CLAUDE.md`), the
project's (every `AGENTS.md` from the CWD up to the git root, else every
`CLAUDE.md`, else `CONTEXT.md`; a container sees only the CWD's), then
harness's: the vault's `AGENTS.md`, the ticket's copy and its state note.
`vault_opencode_config` puts the last three in `instructions` and adds
`x-harness-ticket: KEY`, from which the proxy adds where the two ticket
files are to a free model's vault line. `context_banner` prints one line at
launch (file count, ~tokens, ticket); `harness context [--host]` lists the
files with sizes, the reminder line, the ticket, tracker and whether a token
is set (never the token). After a launch `vault_sync` passes
`vault_capture.py` `--launch MS`, `--ticket KEY` and one `--context FILE`
per instruction file except the global one (personal, and the vault's
remote may be shared): sessions updated since the launch get `ticket:` and
`context:` in their front matter (others keep theirs), and files outside
the vault are copied to `.raw/context/<sha256[:12]>.md` (redacted), so a
later session or ingest sees what the session saw. These are the files as
they were when the session ended; two launches in one folder at once can
label each other's sessions. `regenerate` gives each ticket hub
(`wiki/tickets/KEY.md`) "The ticket" (the copy), a link to the state note
and "Sessions" (those whose front matter names it); the state note's different name keeps it apart from
the hub in Obsidian.

### Ticket queue

`harness queue` (`cmd_queue`, then `scripts/ticket_queue.py`) works queued
tickets one at a time with no one at the keyboard. `add KEY... [--dir]
[--base] [--stages LIST] [--start REF|here] [--branch NAME] [--clone]
[--no-plan-approval] [--note TEXT]` records the project and base (default: its current branch);
by default a ticket works in the project folder (in place) and its plan waits
for approval (`plan_review`); `--clone` and `--no-plan-approval` opt out
(`--in-place` and `--review-plan` are accepted and are the defaults);
`run [--host] [--single-message]` needs an active vault, does the launch
checks once (host mode when docker is missing, `host_confirm_gate` asked
once), starts the host proxy if it is not running and hands the rest to the
Python script. `list`, `remove`, `approve`, `retry`, `answer KEY ["TEXT"]`
(`$EDITOR` without TEXT; the script's `--stdin` is what the run viewer's
ticket page uses), `findings`, `watch` (tails `run.log`) and `log
[KEY]` are the owner's side.

Owner control (all of it also on the run viewer's queue and ticket pages):
`--stages` runs a subset of plan, implement, review, fix, push (kept in that
order; `fix` needs `review`), starting at the first and done after the last;
`--start` (alias `--ref`; default `here` in place, the base in a clone; in
place, a folder on another in-place ticket's branch makes the new ticket
start where that one did) is the commit the work starts
from (a `remote/branch` is fetched first, `here` is what the project has
checked out, frozen to its HEAD sha at add) and `--branch`
(default `KEY`) the branch it ends on and push pushes; `base_sha` is the
merge-base of base and start, so `--stages review --start origin/X` reviews
that branch. An existing target branch is only ever fast-forwarded:
`lost_by` checks the local tip and every remote's (fetched) tip, and add or
setup refuses when the start does not contain one, rather than drop
commits; the one exception is a local branch an earlier go of this ticket
made (`made_branch`), whose old tip is kept at
`refs/harness-queue/prev/KEY`. So a fresh retry of a pushed ticket is
refused until the branch is deleted or another `--branch` is used.
In place, the agents work in the project folder itself (`workdir()`):
setup saves what the owner has uncommitted (`owner.json`), never resets,
cleans or stashes, and switches the folder to the branch at the start; a
later stage finding the folder on another in-place ticket's branch switches
it back (`back_on_branch`) unless that leaves changes behind that were not
there when the ticket started, and blocks on any branch the owner chose. Before
each stage everything in the folder (index plus untracked, not ignored
files) is committed to `refs/harness-queue/snap/KEY` (a chain, a temp
index), and the owner's untracked and changed paths go in a marked block of
`.git/info/exclude` that is removed after the stage. `in_place_problems`
fails a stage when the folder has a rebase or merge going, a left-over
`index.lock`, is not on the branch, or a commit since the start touches a
file the owner had changed or untracked; changes inside nested repos are
only logged. Windows container launches cannot work in place (`--host`).
Each launch gets `HARNESS_QUEUE_LAUNCH` (a nonce), which harness puts on its
container as `harness.queue-launch` and which stops `--ticket` being
remembered for the folder; in-place stages also record `launches` windows,
by which the run viewer maps the project's sessions to the ticket.
A `plan_review` ticket's plan is held as `plan-review`. Without a plan stage the change is held to the ticket's own
"Acceptance criteria" bullets, else to "the change does what the ticket asks
for". Must fix items with no fix stage end the ticket: `done` when review was
last, else `blocked` (`retry KEY --stage fix` goes on). `note KEY` adds an
owner note every later prompt gets (`{{NOTES}}`); notes live in
`KEY/notes.json` under a lock and are re-read on every save, so a runner
holding older state cannot drop one. `retry KEY [--stage S]` (blocked,
needs-answers, stopped, done, a running ticket no run has in hand, a queued
one with `--stage`; back to plan, or any retry of a done ticket
without `--stage`, means a fresh clone from `--ref`), `stop KEY` (a waiting
ticket becomes `stopped`; a running one the run has in hand gets the
`KEY/stop` sentinel, which the runner also checks right before the push;
"in hand" is `run_holds`: a fresh beat, run.json naming it (the run writes
that under `queue.lock` before marking it running) and `run.lock` held, so
one left running by a run that ended or is stuck is stopped at once),
`halt` (the `halt` sentinel:
the run kills its agent, puts the ticket back at its stage uncounted and
exits; a halt file older than the run's lock is stale and dropped), `halt
--force` (`end_run`, for a run that does not halt: kills the run's pid and
its agent's process group from run.json, `taskkill /T` on Windows, waits for
`run.lock`, then puts every running ticket back, `stopped` when it had a
stop sentinel, else `queued` with an `interrupted` entry, and stops the
held launch's container), `move KEY
up|down|top|bottom`, `remove KEY` (refused while a run has it in hand) and `status
[--json]`. Killing a container launch's client leaves its container running,
so after a stop, halt or timeout the runner calls `harness queue _stop_agents
DIR LAUNCH`, which stops the containers labelled `harness.agent=true`,
`harness.mount=<DIR>` and, given a launch id, `harness.queue-launch=<LAUNCH>`
(so an in-place stop never hits the owner's own agents in the folder) (the log says to check `harness list` when that
fails). The runner takes a ticket, and `stop` and `note` save state, under
`queue.lock`. The runner polls its agent every
2 s for the sentinels and the stage timeout (a verifier call runs in a thread
for the same reason) and keeps `run.json` (`pid`, `started`, `beat` every 3 s,
`host`, `single`, `model`, `ticket`, `stage`, and `step`: cloning, the agent
is working, the agent ended, asking the verifier, a verifier retry's wait,
checking that the upstream answers), deleted on exit; a `beat` over 30 s old
means no run. A run that ends on an upstream outage (exit 3) or Ctrl-C (130)
puts the ticket it was on back to `queued` at its stage, uncounted, with the
reason and an `interrupted` history entry; one killed outright leaves it
`running`, which the viewer shows as "run ended" and the next run takes
again.

Stages, each a fresh `harness [host] -p --yolo --ticket KEY -m MODEL PROMPT`
in `state/queue/KEY/repo` (a clone at the base on branch `KEY`, its origin
removed; before each stage the project's own `AGENTS.md`, `CLAUDE.md` or
`CONTEXT.md` is copied in when the clone has none, since a clone has only
what is committed, and listed in the clone's `.git/info/exclude` so it is
never committed or pushed): `plan` → `implement` → `review` → `fix` (once, when the review has
Must fix items) → `push`. The prompt (`scripts/queue_prompts/<stage>.md`)
embeds the ticket, plan, answers and the last attempt's problems, capped to
fit one argv argument, because the free models only read the last message.
While a stage runs its whole prompt is also in the vault at
`wiki/notes/tickets/KEY-task.md` (removed before the vault is synced, ignored
by the vault's `.gitignore`, which a run adds the line to when missing, and
leftovers deleted at run start; the branch check refuses it), which the proxy names to cooperative models every
turn, since they lose the prompt after their first tool call; on Windows,
where a command line is at most 32,767 characters, a prompt over 24,000
characters goes as a short one pointing there.
The model is `QUEUE_MODEL`, else the default model's free twin when it is a
`-withtools` one (`resolve_model`). Plan, review, answers and findings live
in the vault at `wiki/notes/tickets/KEY-{plan,review,answers,findings}.md`.

The note paths in the prompt are the ones the agent sees: on Windows a
container launch gets the vault's `/c/...` mount path (`agent_path`: `cygpath
-u`, else a drive-letter rewrite), not the `C:\...` path native Python holds,
which a Linux agent cannot write (every plan attempt failed "the plan file
was not written" before). A plan or review the agent wrote anywhere else in
the clone (a file ending `KEY-plan.md`, newer than the stage) is moved into
the vault, else the last `## Goal` / `## Must fix` block of its output is
taken (`recover_note`, logged; never output that holds the prompt's own
template).

After a stage harness runs its own gates (the plan's Goal and 1-15
acceptance criteria; the other sections, the question count and a
question's `[blocking]` marker (bold or in backticks too) are logged as
notes, not failures; an unmarked question blocks only when it has a `?` and
no `Default:`, and "None" / "No questions" bullets are not questions;
commits, a non-empty diff, nothing under `.github/`, none of the ticket's
notes (a recovered `KEY-plan.md` the agent committed), no key file, no
key-shaped added line, no value of a secret env var), then
one raw verifier call through the proxy (`verify_plan.md` /
`verify_change.md`, JSON only, three tries). The verifier must quote the
ticket for at least one plan criterion (a quote that does not match is a
note, since a plan criterion may rightly go past the ticket's words) and the
diff for each criterion met;
`judge()` checks every quote is really in the source (letters and digits
only, so spacing and list markers do not matter); a diff quote must come
from an added line, or a removed one when the criterion is about removing,
never an unchanged context line. The plan the verifier
passed is copied to `state/queue/KEY/plan.md` and later stages are held to
that copy, not the vault's, which agents can write. The fix stage must add
a commit and is also checked against the review's Must fix items; an item
the fixer finds wrong is answered in the commit message (an empty commit when
nothing changed), which the verifier sees and may quote for that item only. A failed
stage runs `QUEUE_RETRIES` more times with the problems as feedback, then
the ticket is `blocked`; so does any error in harness itself, so one ticket
never jams the queue. Before a failure counts, one probe request checks the
upstream; the verifier and the probe each try 3 times (20 s, 60 s apart) on no
answer, 408, 429 or 5xx; if it does not answer the run exits 3 and the ticket keeps its
stage. A stage that runs past its timeout is blocked, not retried: a
container launch can outlive its stopped client. Going back to the plan
(`answer`) starts from a fresh clone. A
blocking question parks the ticket as `needs-answers` and the queue moves
on; `QUEUE_PLAN_REVIEW=1` holds each plan as `plan-review` for `approve`.
`answer` (answers or feedback on a held plan) sets `plan_hold`, so the plan
revised with them (the prompt has the previous plan and says to revise it)
comes back as `plan-review` until `approve` clears it; `answer --go` (the
viewer's "Go with the defaults") does not hold.
A ticket is `running` from the moment a run takes it (before the clone, so an
`answer` cannot pull the clone from under it), and its `state.json` keeps the
run's vault, where the ticket page reads notes and writes answers even when
the viewer started with another. `answer`, `approve` and `retry` stamp `requeued`, so a `queue run` still
going takes the ticket again after its current one. Every attempt's result
and, when it failed, its problems go to `state.json`'s `history` (last 30),
which the ticket page lists.

Harness never runs git in the clone once an agent has had it: it fetches
the ticket's branch into the project as `refs/harness-queue/KEY` (in place:
reads the branch), diffs there,
updates the project's branch only by fast-forward (and not at all while a
worktree of the project has it checked out, or in place) and pushes
`sha:refs/heads/BRANCH` to `QUEUE_REMOTE` without force (`QUEUE_PUSH=0`
keeps it local). Before each ticket `queue _pull` refreshes the vault's copy
of the ticket; after each stage, and once more when the ticket stops, the
ticket's `log.md` (stages, checks, verifier verdicts, outcome) is copied to the
vault as `wiki/notes/tickets/KEY-log.md` and `queue _sync` captures the session
(host or container database) and commits the vault. Nothing is written to the
tracker.
State: `state/queue/queue.list` (order), `KEY/state.json` (status, stage,
attempt, feedback, reason, the plan's `questions`, `history`, `vault`), `KEY/log.md`, `KEY/<stage>-N.out`, every verifier reply
as `KEY/verify-*.txt`, and `run.lock` (one run at a time: `flock`, or
`msvcrt.locking` on Windows). Each agent runs in its own session (its own
process group on Windows), so Ctrl-C reaches only the queue, which then ends
the agent's tree (`killpg`, or `taskkill /T` on Windows). `bash` is found on
`PATH`, since a bare name on Windows can resolve to WSL's `bash.exe`. The clone
is made in place, not in `repo.new` renamed to `repo` (Windows refuses to rename
a directory while anything, such as a virus scan of the fresh clone, has a file
in it open), and is complete once `base_sha` is saved. Old clones are removed
with the read-only bit cleared (git's objects are read-only, which a plain
`rmtree` cannot delete on Windows) and retried for a few seconds; one that will
not go blocks the ticket with the reason. On Windows the clone sets
`core.fsmonitor=false`, so Git for Windows' monitor daemon does not hold it
open, `core.longpaths=true`, and for container runs `core.autocrlf=false` (a
CRLF checkout looks modified to the container's git); a host prompt's paths
use `/` (`C:/...`), which backslash-escaping no-tools tool calls cannot
break. git output is read as UTF-8 (`PYTHONUTF8=1`), the push never asks for
credentials (`credential.interactive=false`, `GCM_INTERACTIVE=never`, ssh
`BatchMode` only when neither `GIT_SSH`, `GIT_SSH_COMMAND` nor the project's
`core.sshCommand` names an ssh of the owner's), and a container `run` asks the open-firewall question once,
then sets `HARNESS_NET_CONFIRM=1` for its stages; `--single-message` starts
the proxy in single mode up front. Covered by `tests/unit_queue_test.sh`.

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
   `cmd_host` still accepts the retired **`--require-tool`**, consumed by its
   argument loop as a no-op and never forwarded (opencode rejects unknown
   flags); require-tool mode is always on in the proxy (see
   `architecture/proxy.md` → "Require-tool mode"), so `host_proxy_start`
   passes no switch for it.
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
   corrupt or half-extracted tool is never trusted on the next run. A checksum
   mismatch whose download is an HTML page is reported as a proxy/web filter
   replacing the file (`host_fetch`). A vendored jq
   that fails its smoke run is retried once (antivirus can hold a fresh `.exe`
   while scanning), then reported with its exit code, its own output and a likely
   cause from `host_jq_run_hint` (CPU mismatch, Windows blocking the unsigned
   upstream `jq.exe`, a `noexec` mount, killed by security software, a file
   quarantined right after download). On Windows, Git Bash reports every refusal
   to start a program as "Permission denied", so the hint runs the file through
   `cmd.exe`, prints Windows' own reason and names the matching cause
   (Group Policy AppLocker/SRP, WDAC/Smart App Control, antivirus). A Group
   Policy block (error 1260) is usually a path rule covering all of
   `state/host`, so that case drops the install-jq workaround and points at an
   allow rule for the folder instead. Otherwise the message ends with the
   workaround of installing jq on `PATH`, which skips the download. This makes
   host mode **self-installing, not offline**: opencode still fetches its provider
   over the (unfirewalled) network on first use, exactly as the container does.
   `host_preflight` then runs as a post-provision assertion — each of `python3`,
   `jq`, Node, `opencode` must both resolve **and** execute, naming any that fail.
   If provisioning or that assertion fails on Windows and the install root is
   outside the user profile, `host_win_location_hint` adds a note naming that as
   the likely cause (managed PCs often run programs only from inside the
   profile) with the commands to move the install and re-point the PATH wrapper.
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
   probe). The chatgpt backend block is folded in **conditionally** so an
   install that does not use it hashes byte-identically to the pre-feature
   harness (otherwise every existing host user's first launch after upgrading
   kills a healthy proxy and blames `.env`). `requiretool=1` is appended
   **unconditionally**: require-tool is always on now, so a host proxy left
   running from before it became the default (whose fingerprint lacks the
   line) is restarted once instead of reused, while one started with the old
   `--require-tool` flag still matches. A leftover `HARNESS_REQUIRE_TOOL` in
   `.env` does not affect it. A `code=` line (one cksum over `proxy/proxy.py`,
   `agents/slim-prompt.md` and `agents/slim-subagent-prompt.md`) and a
   `native=` line (`PROXY_NATIVE_TOOL_MODELS`, `MODEL_CONTEXT_LENGTH`) are appended
   unconditionally, so a host proxy left running across an upgrade or a knob
   change is restarted instead of serving old code. A `promptmode=<mode>` line is appended **only**
   when `harness host --single-message` / `--prompt-mode M` set
   `prompt_mode_override` (exported to the host proxy as `PROXY_PROMPT_MODE`,
   blank by default), so a default launch hashes as before, while switching
   modes in either direction restarts the proxy; a plain `harness host` after a
   `--single-message` run therefore comes back on hybrid. Folding the requirements content in matters because the reuse
   short-circuit returns *before* `host_proxy_ensure_venv`, so an `upgrade` that
   bumps a proxy dep restarts the running proxy onto the rebuilt venv instead of
   leaving it on stale deps. `host_proxy_wait_ready`
   polls the loopback port via `/dev/tcp` (the proxy calls `app.run()` last, so
   an accepted connect means it is serving) and tails the log on failure (the
   common cause is `_validate_config` `sys.exit(1)`).
7. **Scoped opencode config** — `host_write_opencode_config` writes the same
   provider config shape as the container entrypoint's `ensure_opencode_config`
   (`includeUsage`, token `cost` and a `-withtools` context of at most 120k via
   `opencode_models_jq`, `compaction.prune`, no agent `steps` cap; see [`containers.md`](containers.md)), but `baseURL` → `http://127.0.0.1:<port>/v1` and built with `harness_jq`, to a
   **scoped** file (`state/host/opencode.json`). It asserts host `jq` is present
   up front (`host_preflight` guarantees it) so the `harness_jq` docker-sidecar
   fallback can never be reached from this no-docker path. The caller exports
   `OPENCODE_CONFIG` to point at it, so the user's global
   `~/.config/opencode/opencode.json` is never touched. `curl` is optional: the
   model dropdown comes from the proxy's `/v1/models` when present, else falls
   back to `DEFAULT_MODEL_NAME` alone. The proxy port is `host_proxy_port`:
   `HARNESS_HOST_PORT`, else `.env`'s `PROXY_PORT`, else 8000 (the override
   lets the benchmark run its own proxy beside a user's). With
   `HARNESS_HOST_NO_WEB=1` the yolo agent's `webfetch`/`websearch` become
   `deny` and the config gains `share: "disabled"`; unset, the config is
   byte-identical to before (no `share` key). Every model declares image
   input (`attachment`, `modalities`; see [`containers.md`](containers.md)),
   so opencode sends images. With an OAI upstream the
   `OAI_MODELS` ids move to a second provider, `harness-oai` (npm
   `@ai-sdk/openai`, same loopback baseURL and dummy key, `reasoning: true`,
   output 32000), because that package speaks the Responses API
   (`/v1/responses` on the proxy), where these models take tools and reasoning
   together, and opencode gives it a per-session `prompt_cache_key`; the
   `harness` provider is omitted when no other model is left, and
   `model`/`small_model` become `harness-oai/<id>` when the default is an OAI
   model. `host_opencode_env` passes `oai` to the overlay so the slim header
   is set on both providers. Container mode does the same: `agent_common_env`
   passes `OAI_MODELS` (names only) and the `oai` overlay, and the
   entrypoint's `ensure_opencode_config` writes the same `harness-oai`
   provider ([`containers.md`](containers.md)). The fingerprint gains an `oai` line
   only when an `OAI_*` key is set.
8. **Launch** — `host_run_opencode` mirrors the entrypoint's `run_opencode`
   (provider env, `--agent yolo`, `--auto` for a headless `--yolo` run since
   `opencode run` rejects any permission ask it cannot show, and the headless `-p` json-events +
   `opencode export` dance that dodges opencode 1.15.x's render race). The
   `-p` run goes through `agents/live-run.sh` from the clone, as in the
   container, so the serve run viewer can message it (see "Run viewer").
   Interactive runs opencode as a **child** (not `exec`) so the proxy is torn
   down on exit; print mode leaves the proxy up so a scripted `-p` loop doesn't
   thrash it (`harness host down` stops it) — the same interactive-vs-print
   teardown split as container mode. Because nothing reaps a `-p` proxy, print
   mode prints a stderr reminder after the run naming the live pid and the
   `harness host down` stop command. `host_run_opencode` exports
   `OPENCODE_ENABLE_EXA=1` (Exa web search) unless `HARNESS_HOST_NO_WEB=1`. **Terminal reset on interactive exit:** after
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

**Resuming a stopped task.** Two cases, neither needing new code:
- *Daily credit quota spent.* The proxy answers that 429 with `Retry-After`
  set to the reset time plus a minute (see [`proxy.md`](proxy.md) → Native
  tool models). opencode's retry honours it (any length, up to 5 attempts),
  shows `[retrying in 9h 10m attempt #1]`, and continues the same session
  after the reset as long as the process and the proxy stay up. A headless
  `-p` run therefore just sits until then, which is the intent for overnight
  or weekend runs.
- *Process gone* (reboot, closed terminal). opencode keeps sessions on disk;
  `harness host --continue` (TUI) or `harness host -p --continue "continue"`
  (headless) passes `--continue` through `pass_args` to opencode, which
  reopens the most recent session in that directory; `--session <id>` picks
  a specific one. The step counter starts again at the new prompt.

**The probe must not eat stderr.** Both loopback probes
(`host_proxy_wait_ready`, and `host_mcp_port_open` in mcp.md) run
`(exec 3<>/dev/tcp/…)` in a **subshell**, so the probe fd is opened and closed
there and the caller never holds it — there is nothing to clean up afterwards.
A command-less `exec` redirects the *current* shell permanently, so the
`exec 3>&- 3<&- 2>/dev/null` that used to follow the successful probe silenced
harness for the rest of the run: an interactive `harness host` printed nothing
after the confirm gate (no launch banner, no `err()`, none of opencode's own
stderr), which reads as a hang. Never re-add an fd cleanup there.

## `serve`

`cmd_serve` is loopback-only and has one opencode server for everyone who
gets in. It runs the same preamble as `cmd_host` (python, config, confirm
gate, toolchain, preflight, upstream gate, `host_proxy_start`,
`host_write_opencode_config`, `vault_prepare`), sets `harness_serve=1`,
unsets the upstream secrets (`_harness_secret_vars`: `PROXY_API_KEY`,
`OAI_API_KEY`, `CHATGPT_COOKIE_STRING`, `TRACKER_TOKEN`) from its own shell, and then starts
two foreground children:

- `serve/pub.py` (stdlib) on `127.0.0.1:N` (default 8780), the index page.
  It lists the active vault's `pub/**/*.html` (title from `<title>`, newest
  first, dot paths skipped, `?q=` filters on title or path, at most 200
  shown) and `/pub/<rel>` serves the file, realpath-confined to `pub/`. The
  index also shows: a launch panel (the opencode web link, a note that its
  terminal is a host shell as the server's user), the proxy's `/health`
  (`--proxy-url`, 2 s timeout, no client headers forwarded), the prompt
  modes (`--mode NAME=DESC`, below), last-24h usage from the proxy's ledger
  (`--usage-ledger`, last ~5 MB read), and the vault's last 15 commits.
  These parts are cached for a few seconds. With no vault active the index
  still comes up with no pages. The vault is read per request from
  `state/vault.active` (`--state-dir`, cached 2 s), so `harness vault use`
  shows without a restart. The index is the Home tab of the shared top bar
  and live like the run viewer's pages (`/live?q=`: regions `top`, `main`,
  `side`, `nav`). Layout: the vault and launch panel on top, then runs,
  pages and usage beside a side column of prompt modes and vault commits
  (one column below 820 px); inline CSS, system fonts, light and dark from
  `prefers-color-scheme`. `/healthz` is a liveness check.
- `opencode serve` on `127.0.0.1:N+1`, with `host_opencode_env` (shared with
  `host_run_opencode`, so the config, overlay and NO_PROXY match `harness
  host`; it unsets `_harness_secret_vars` too, since opencode reaches the
  upstream only through the proxy, after deciding whether the OAI provider
  is configured: under serve, from `harness_oai`, which `cmd_serve` sets
  before its own unset), cwd the vault (else `$HOME`), and
  `OPENCODE_SERVER_PASSWORD` from `state/serve/opencode-password` (random,
  made once by `serve_password_file`, mode 600). Without `--opencode-url`
  the index links to it as `http://opencode:<pw>@host:N+1/`: the browser
  answers the first 401 with those credentials and caches them for the
  app's scripts and API calls. opencode's own `?auth_token=` covers only the
  request that carries it, so the page's assets drew a Basic prompt (and
  adding it to the link skips the 401, so the browser never caches the
  login). With `--opencode-url URL` the link is that URL with no login, for
  a reverse proxy that adds the Basic header itself. The overlay sets `share: "disabled"`, so no session is uploaded to
  opencode's share service.

**Prompt modes.** `serve_opencode_config` (part of
`harness_opencode_overlay`, empty outside serve) turns each launch-mode
switch into a choice in opencode's model picker, per message. For every
`harness` model that the proxy sends on its native route (the
`PROXY_NATIVE_TOOL_MODELS` patterns, trimmed, default `*-withtools`;
the shell match covers the fnmatch subset `*`, `?`, `[...]`, `[!...]`), the model's own
entry carries the launch mode's headers (`x-harness-slim`, plus
`x-harness-thoughts` with `--thoughts`) and three aliases are added with the
same `id` and limits: `m:stock` (no harness header), `m:thoughts`
(thoughts only), and `m:slim+thoughts` (or `m:slim` when the launch already
has thoughts). The provider-wide slim and thoughts headers are then left
out. Aliases are made only where the proxy honors the headers: none for
cooperative-route models, under `--normal`, or with the native route off.
(The function also returns none with a prompt mode set or on the chatgpt
backend, which `harness serve` cannot be started with today.) The headers act only in the
proxy (slim prompt and tool descriptions, step reminder, old-output trim,
general cap, explore reroute, thought summaries), so the `:stock` alias
alone is stock mode; the `stock` primary agent is a shortcut that starts on
`<default>:stock`. `cmd_serve` passes the same four modes to the index as
`--mode` lines.

**Access codes.** `harness serve code add|list|remove NAME` manages
`state/serve/access-codes` (`name:sha256(code)` lines, mode 600) through
`pub.py add-code|remove-code`; `add` prints the new code once. When the file
exists, `cmd_serve` passes `--access-codes` (it fails closed: with no codes
left nobody can sign in, and serve warns; deleting the file opens the page)
and every pub route except `/login`, `/logout`, `/_auth` and `/healthz`
needs a session cookie from `POST /login` (HttpOnly, SameSite=Strict, 12 h;
`--secure-cookie` adds `Secure` and the `__Host-` prefix behind TLS). pub.py
reads the file live, but whether to gate is decided at start, so the first
`code add` needs a restart (`add` says so). A sandboxed `/pub/` page's
loads and links are cross-site, so the session cookie is not sent with
them; login also sets a second cookie (`harness_pub_view`, Path=/pub/,
SameSite=None, Secure, HttpOnly) that is accepted only for GET/HEAD
`/pub/`, which changes nothing. Failed logins are rate-limited per client
and overall (`--secure-cookie` also passes `--trust-forwarded-for`, so the
client is the last `X-Forwarded-For` entry, as set by the reverse proxy);
non-GET requests need a same-host `Origin`; removing a name ends its
sessions. `GET /_auth` answers 200 with
`X-Harness-User: <name>` or 401, for a reverse proxy's `forward_auth` in
front of opencode (`serve/deploy/`). Codes gate the pub page only; opencode
itself still needs its Basic password, which the reverse proxy injects.

**Run viewer (`/runs`).** `serve/runs.py`, mounted by pub.py, shows what
every agent did or is doing, from opencode's own session store, read-only
(`file:...?mode=ro`), and drives the queue through `ticket_queue.py`'s own
commands. `serve_pub_args` passes `--agent-data container=<agent
home>/.local/share` and `--agent-data host=${XDG_DATA_HOME:-~/.local/share}`
(each `LABEL=DIR` holding `opencode/opencode.db` and `harness/live/`; a
dir with no DB shows nothing, and with no `--agent-data` the viewer is off),
`--queue-dir state/queue` and `--harness <this script>` (what Start runs;
default the `harness` next to `serve/`). Pages share one top bar (Home,
Queue, Sessions, Vault; the runner chip; how fresh the page is; who is signed in,
with Log out) and a neutral dark theme with an indigo accent (light
from `prefers-color-scheme`); inline CSS, nonce scripts only, `img-src 'self'`. Routes:

- `/vault*` (`serve/vault_view.py`): read-only views of the active vault.
  git runs with `GIT_OPTIONAL_LOCKS=0`, `core.fsmonitor=false`, no prompt,
  never fetches. `/vault` (live, `/vault/live`: `main`, `side`, `nav`):
  counts by wiki folder and `.raw`, folder, remote (credentials stripped),
  branch, ahead/behind from local refs, uncommitted files, recently changed
  pages, last commits, the registered vaults (`state/vaults.list`) with Use
  (POST `/vault/use` writes `state/vault.active` as `harness vault use`
  does), and an Ingest panel: the run file `.git/harness-ingest.run.json`
  (alive while its pid lives and its beat is under 120 s old) and, per
  codebase and `.raw`, ok and failed sections from `.harness/ingest.json`;
  `/vault/ingest` (live) lists failed sections with their problems and
  controls ingest: POST `/vault/ingest/start` runs `harness ingest` in the
  background (no terminal, own session; "what changed", `--retry-failed`, a
  new codebase `<dir>`, `--max`, `--model`, or one failed section's
  `--unit ID`), its output in `state/serve/ingest.out` shown under the
  controls; refused while an ingest is alive (run file) or the one started
  here still runs. `/vault/ingest/stop` sends SIGTERM to the run file's pid
  and to the started harness's process group (ingest saves its progress).
  `/vault/ingest/forget` runs `harness ingest --forget NAME`.
  `/vault/browse/<rel>`: a folder lists folders then files (titles from
  front matter or the first heading); a markdown file is rendered on the
  server by the vendored mistune 3.3.4 (`serve/vendor/`, BSD-3, raw HTML
  escaped, `javascript:` links dropped) with Obsidian `[[wikilinks]]`
  (full path, else the shortest file of that name; missing ones dimmed),
  `![[embeds]]` of images, `> [!callouts]`, relative links mapped into the
  browser, front matter as Properties and HTML comment lines (unit markers)
  dropped; other text as is; per-file history (`git log --follow`). Paths
  are confined to the vault by realpath (no `..`, no `.git`), and files
  whose names look like secrets (`.env*`, `*.pem`, `*.key`, `id_*`,
  `.netrc`, `credentials`) are listed but never shown or searched.
  `/vault/search?q=`: `git grep --untracked -i -F` with every word on one
  line (at most 300 lines), names matching first, wiki before `.raw`.
  `/vault/commit/<sha>[?path=]`: the commit's stat and diff (400 kB cap).
  `/vault/file/<rel>`: png, jpeg, gif and webp only, sandboxed, nosniff.
  An "Open in Obsidian" link (`obsidian://open?vault=<folder name>`) is on
  the overview and each markdown page.

- `/settings` (`serve/settings_view.py`): every change runs the harness
  command a terminal user would type, with no terminal (stdin /dev/null, new
  session, `HARNESS_INSTALL_ROOT` = the parent of `--state-dir`), so nothing
  waits on a prompt. Model and keys: `config list --tsv`, one form, Save runs
  `config set` for each changed value (secrets and URLs with a password are
  password fields: blank keeps, a typed value replaces, "clear it" empties;
  never echoed). Network allowlist: `net list`, `net allow [--git-push]`,
  `net deny`. MCP: `mcp list --available`, install/enable/disable. Access
  (with `--access-codes`): add shows the new code once, remove (not your
  own). System: version, Run doctor (rendered by section), check-updates,
  `update`. Changes are logged to stderr with who made them.

- `/runs/queue`: the owner's console. A runner card (running or not, mode,
  model, ticket and stage, heartbeat age, `runner.out` tail at
  `/runs/queue/runner.out`) with Start (container, host or single-message
  host; host modes need the "host mode is OK" box) and Halt; one row per
  ticket in queue order with a stage strip (plan, implement, review, fix,
  push: passed, skipped, current in the status colour, still to do, or not
  one of the ticket's stages), why it waits (a running ticket: its stage,
  attempt and the run's `step`; a `running` ticket with no live run on it:
  "run ended", with Start the queue on its page), and its buttons (move, Answer
  or Review plan, stop, retry, remove); and the Add form (keys, project
  folder, a Starting point: New work from the target branch, Continue
  existing work on a named branch such as `origin/ABC-123` (the work branch
  defaults to that branch's name; one ticket per add), or The folder as it is
  now (`--start here`); the Target branch (`--base`), an optional Work branch
  name, where the agents work, plan approval, a note for the agents, and the
  stages: Full, Review only, Implement+review, Plan only or Custom). The
  branch fields suggest the project's local and remote branches from
  `/runs/queue/branches?project=` (read-only git, cached 5 s), which also
  says which repository and branch the folder is on. "+ Add tickets" scrolls to the
  form and focuses the keys. The project folder starts as the git repository
`harness serve` (or the launch that started the viewer) was run in
(`--project-dir`, its top folder), else the last ticket's; the others are
offered as suggestions. It is taken as pasted: quotes
  and blanks around it dropped, `~` expanded, and on Windows a Git Bash
  `/c/...` path read as `C:/...`. A refused add shows the queue page again
  (409) with the reason in the form and every value kept. `/runs/queue/live`
  is the JSON the page polls.
- `/runs` (`?q=` filter): live runs, queue tickets with their sessions by
  stage, then every session newest first. The index shows the live ones in
  an "Agent runs" panel.
- `/runs/ticket/<KEY>`: status and why, the stage strip and buttons, the
  plan's questions with an answer form, the ticket's sessions from every
  `--agent-data` source by stage, every attempt from `history` with its
  result, reason and `.out`, the notes with an add form, the plan, answers
  and review notes from the vault, and `log.md`. `/runs/ticket/<KEY>/live`
  is its poll JSON, `/runs/ticket/<KEY>/<stage>-<n>.out` an output file.
- `/runs/<sid>`: the transcript (text, thinking, each tool call with input,
  output, status and timing, subtasks, retries, errors; a file change as its
  diff from opencode's tool metadata, a `todowrite` as a checklist), crumbs
  (the queue and ticket for a stage) and a side panel: for a live run the
  message box first, a "Stop this stage" button for a queue stage, then the
  session's latest todo list, then agent, model, tokens, cost, directory,
  ticket, subagents. On a wide screen the side panel stays in view while the
  transcript scrolls (sticky, scrolling on its own when taller than the
  window), as does a ticket page's row of buttons. A host session gets "Open in opencode web" when this
  page runs under `harness serve` (`/server/<base64url(origin)>/session/<id>`
  on opencode's own server, with the Basic login in the link as on the
  index; container sessions live in another database, so none). A nonce
  script polls `/runs/<sid>/tail?from=N` (2.5 s, 15 s once idle) and appends
  new or changed messages in place.
- `/runs/<sid>/part/<pid>` (one tool call in full, past the 20,000-character
  inline cap), `/runs/<sid>.json` (the session as JSON).
- `POST /runs/<sid>/send`: puts a message in the run's inbox.
  `POST /runs/<sid>/stop`: stops the ticket whose stage this session is
  (refused for anything not a queue stage).
- `POST /runs/queue/{add,start,halt,end}`: `add` checks the form (keys, an
  absolute existing project folder, refs that cannot carry options or
  revision syntax, `fix` only with `review`, a ref when there is no
  implement stage in a copy) and runs `queue add --stages --start --branch
  --in-place|--clone --review-plan|--no-plan-approval --note` (the form's
  "Where the agents work" and "When the plan is written" choices); `start`
  runs `bash <harness> queue run [--host|--single-message]` detached (own
  session or process group, no console, `HARNESS_OPEN=0`,
  `HARNESS_HOST_CONFIRM=1` for host modes, `HARNESS_INSTALL_ROOT` from the
  queue dir), output appended to `<queue>/runner.out`, refused while a run
  holds the queue; `halt` writes the halt file; `end` runs `halt --force`
  (offered while halting, or when a ticket's stop has waited over
  `STUCK_AFTER` s).
- `POST /runs/ticket/<KEY>/{answer,approve,retry,stop,remove,move,note}`:
  `ticket_queue.py` `answer --stdin` (the form's answers as `## question`
  sections, `## Other`, and "go with the default" for the rest; needs the
  vault), `approve`, `retry [--stage S]`, `stop`, `remove` (refused while
  running), `move up|down|top|bottom`, `note --stdin`. Each redirects back
  with the queue's message, or answers 409 with the reason when the ticket
  is not in a state for it (answers sent twice, approve of a ticket not in
  plan-review). Every POST needs a same-host `Origin` and, when codes are
  on, a session.
- Live updates: Home and the queue, ticket and session-list pages poll their
  `/live` JSON every 3 s (a session page its tail) and swap only the parts
  that changed (`hSwap` in `COMMON_JS`): a form being filled in (a changed
  field or the focus) is kept in place, with its focus and selection, while
  the rest of its region updates; open `<details data-k>` stay open; polling
  pauses while the tab is hidden. The bar's freshness note says `live`,
  `updated N s ago`, `offline · retrying` or `signed out · sign in`: a poll
  sends `X-Harness-Live: 1`, and a signed-out one gets 401 JSON instead of
  the redirect to `/login`. When serve's code on disk is newer than the code
  it started with, `/live` answers carry `X-Harness-Stale: 1` and the note
  asks for a restart of `harness serve`.
- `/runs/follow?since=MS[&queue=1]`: the link a launch opens. It redirects
  (303) to the root session started at or after MS (live first, then still
  working, then newest), or shows a waiting page that polls
  `/runs/follow.json` (`{"sid": ...}`) until one starts. With `queue=1` only
  queue stages count, and a stage page carries `data-follow`, so once its
  session is done the page moves on to the next stage's.

**Auto-open.** Every headless launch shows its run: `harness_view` (host
`-p` in `host_run_opencode`, container `-p` in `run_agent`, and once per
`harness queue run`, which then exports `HARNESS_OPEN=0` so its stage
launches do not open their own) runs `pub.py start` on
`HARNESS_VIEW_PORT` (default 8780): if a pub page already answers
`/healthz` there (a running `harness serve`, or an earlier launch's viewer)
it is used as it is, else pub.py starts detached (its own session; on
Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`) with
`serve_pub_args` (the agent data, queue, ledger and access codes `cmd_serve`
passes) and no opencode web, pid in `state/serve/viewer.pid`, log
`viewer.log`. It stays up after the launch; `harness serve stop`
(`pub.py stop`, which signals the pid only if the port still answers) ends
it, and `cmd_serve` stops it before binding the port. The link
`/runs/follow?since=<now-2s>` is printed, and `harness_open_url` opens it
in the background when stderr is a terminal and neither `HARNESS_OPEN=0`
nor `CI` is set: `rundll32 url.dll,FileProtocolHandler` on Windows (not
`cmd /c start`, which splits the URL at `&`), `open` on macOS, `wslview` or
`explorer.exe` under WSL, `xdg-open` on Linux only with `DISPLAY` or
`WAYLAND_DISPLAY`. A viewer that does not start is a warning; the launch
goes on. Interactive (TUI) launches open nothing: the user is in the
session already.

A run is live when `agents/live-run.sh` (every `-p` run: container via
`harness-live-run`, host via the clone's copy) is driving it.
A plain `opencode run` keeps its server in-process, so the script starts
`opencode serve` on 127.0.0.1 (port 0, random password), attaches `run
--attach` to it, writes `harness/live/<sid>.json` (touched each second) and
relays each `harness/live/<sid>.inbox/*.json` to the server's
`/session/<sid>/prompt_async`, which joins the busy session at its next step
(renamed `.sent`, `.failed-<code>`, or `.late` once the run ended).
`HARNESS_LIVE=0` (passed into the container by `agent_common_env`), or a
serve that does not come up, runs plain `opencode run`. `send` takes the agent
and model from the session's last user message and is refused (409) when the
run is not live (marker older than 10 s), the text is empty or over 64 kB;
with access codes it also needs a session user (401), and like every POST a
same-host Origin. Interactive TUI sessions are shown but cannot be messaged.
Anyone who can read the page sees full tool output, and anyone who can write
the agent home can drop inbox files; the serve password lives only in the
run's environment.

`pub.py` guards: the `Host` header must be `127.0.0.1|localhost|[::1]:N` or
a `--allow-host` value (421 otherwise), against DNS rebinding. Every `/pub/`
response carries `Content-Security-Policy: sandbox allow-scripts
allow-popups` (no `allow-same-origin`), so a vault page, often agent-written,
runs with an opaque origin and cannot read the index or call opencode, whose
CORS always admits `http://localhost:*` and `http://127.0.0.1:*`. Do not add
a gateway that injects opencode's credentials without an auth step in front
of it: anything that can reach it would then be logged in.

Remote access: `ssh -L` both ports, or a reverse proxy the deployer runs
(`serve/deploy/` has a Caddy example using `/_auth`) with `--allow-host` for
the public name and `--opencode-url`. Anyone logged in to opencode web has
its terminal, a shell as the invoking user with the server's environment
(including `OPENCODE_SERVER_PASSWORD`); on a host where that user has
sudo or docker, that is root. The proxy runs as the same user, so an agent
can still read `.env` or the proxy's environment: scrubbing the key from
opencode's env stops casual leaks, not a determined agent. Real separation
needs the proxy under its own user. Ctrl-C stops both children (the EXIT
trap also reaps the jq sidecar, since it replaces main's) and leaves the
proxy up; `wait -n` exits 1 if either child dies. Logs:
`state/serve/{pub,opencode}.log`. `tests/unit_serve_test.sh` runs
`serve/test_pub.py` (index, Host check, sandbox header, path escape, access
codes, `/_auth`, usage, git log, proxy status) and `serve/test_runs.py`
(the run viewer over a fixture opencode DB: list, transcript, escaping, tail,
ticket page with its questions, attempts and answer/approve/retry, diffs
and todos, send and its refusals), a stub-opencode check of `live-run.sh`
(attach, inbox relay, exit code, fallbacks), plus option errors, the
password file, the mode aliases (alone and in the merged overlay), the key
scrub (the OAI provider keeps its slim header in host and serve) and a
guard that the only `Authorization` header in the CLI is
`_curl_auth_config`'s, and a stubbed run under `set -e` with no OAI that
must reach the banner (a failing `$(...)` assignment once ended serve
silently there).

## ChatGPT backend (`harness chatgpt`)

`cmd_chatgpt` is a **front door, not a parallel launch path**. It sets the
`backend_override` global to `chatgpt` and then delegates to the exact code the
default launch uses: `run_agent opencode` for the bare form, `cmd_host` for
`harness chatgpt host` (so `harness chatgpt host down` works too), plus
`cmd_doctor` / `cmd_preflight` and the stack-lifecycle verbs
`start` / `restart` / `down`. `-h/--help` is intercepted before the global is
set, so help has no side effects.

Routing the lifecycle verbs is not cosmetic. `cmd_start` calls
`require_runtime_config`, which on a chatgpt-only install fails the OpenAI trio;
and any word `cmd_chatgpt` does not recognize falls through to `run_agent`, so
an unrouted `harness chatgpt restart` would launch an agent with a stray
argument.

Everything the backend changes hangs off that one global:

| Site | Behavior when `backend_override=chatgpt` |
|---|---|
| `require_runtime_config`, `host_require_config` | required-var set swaps to `CHATGPT_BASE_URL` / `CHATGPT_MODEL_NAME` / `CHATGPT_COOKIE_STRING`; the `PROXY_API_*` + `DEFAULT_MODEL_NAME` trio is not required |
| `_gate_on_upstream_auth`, `_print_upstream_models` | early return — cookie auth has no bearer probe and the backend-api has no catalog |
| `_effective_default_model` | returns `CHATGPT_MODEL_NAME`; feeds `agent_model` (container) and `host_write_opencode_config` (host) |
| `write_runtime_override` | emits `PROXY_BACKEND: "chatgpt"` and `HARNESS_PROXY_FP` on the proxy service, folded into the **same** `proxy:` mapping as `PROXY_PROMPT_MODE` |
| `host_proxy_start` | passes `PROXY_BACKEND` in the launch env prefix |
| `host_proxy_fingerprint` | hashes the backend plus all three `CHATGPT_*` values |
| `firewall/init-firewall.sh` | guards `CHATGPT_BASE_URL`'s host against the allowlist instead of `PROXY_API_URL`'s |
| `cmd_doctor`, `cmd_preflight` | check/report the `CHATGPT_*` trio and that `CHATGPT_BASE_URL` has a parseable host; the upstream probe is skipped (no catalog endpoint) |

`harness chatgpt doctor` and `harness chatgpt preflight` exist only to set that
global before delegating — the diagnostics are otherwise the same code. Bare
`harness doctor` still checks the OpenAI trio, because that is what bare
`harness` uses; when those are unset but `CHATGPT_BASE_URL` is set it prints a
pointer to `harness chatgpt doctor` rather than silently reporting three
failures. `cmd_preflight` prints the same pointer, and
`require_runtime_config`'s abort adds one naming `harness chatgpt ...`.

**Restart-in-place adopts the running dialect.** `cmd_restart` and `cmd_upgrade`
(and therefore `cmd_downgrade`, which delegates to it) are down-then-start, so
without help they always come back on the OpenAI dialect — on a chatgpt-only
install that means the stack is torn down and `cmd_start` then aborts naming
three keys the user deliberately never set. `_adopt_running_backend` reads
`_running_proxy_backend` **before** the teardown and sets `backend_override`.
It adopts only `chatgpt`: leaving the global empty for `openai` is what keeps
`write_runtime_override`'s output byte-identical for every pre-existing install.

**One proxy, one dialect.** Two landmines follow from the proxy being a single
shared service, and both are handled explicitly:

- *Container mode.* `ensure_services_up` is a no-op when the proxy is already
  up, so a backend flag would silently not take effect after a normal launch.
  The whole reconciliation block below is gated on `_chatgpt_in_play`
  (`backend_override` set, or `CHATGPT_BASE_URL` configured): an install that
  never configured this backend cannot have a chatgpt proxy running, and the
  probes cost a `compose ps` plus a `docker inspect` each — with `compose`
  regenerating the runtime override on every call. That gate is what keeps a
  pre-existing OpenAI launch exactly as cheap as it was before this backend
  existed.
  `_running_proxy_backend` reads `PROXY_BACKEND` out of the running container's
  env (defaulting to `openai`, which is also what every pre-existing container
  reports) and `ensure_services_up` calls `cmd_start` on a mismatch. That
  restart drops a concurrent agent on the other backend; there is only one
  proxy, and answering from the wrong upstream is worse.

  The same no-op hides a *credential* change. Compose will not replace a
  running container just because `.env` was edited, so rotating an expired
  cookie — the one routine maintenance action for this backend — would keep
  serving the stale one. `_chatgpt_config_fingerprint` hashes the three
  `CHATGPT_*` values, `write_runtime_override` injects it as `HARNESS_PROXY_FP`
  on the proxy service, and `_running_proxy_fp` reads it back out of the
  running container; a mismatch restarts. Only the hash crosses that boundary
  (the reader filters the env dump inside the pipeline), so the cookie never
  lands in a shell variable or in `state/.harness-runtime.yml`. The openai
  backend writes no fingerprint and is unaffected.
- *Host mode.* There is one pidfile, so the two host modes cannot coexist.
  Folding the backend and the `CHATGPT_*` values into `host_proxy_fingerprint`
  makes the existing config-change restart path handle the switch.

`CHATGPT_COOKIE_STRING` is a full session credential, so `_config_is_secret`
matches `*COOKIE*` and `harness config` masks it. Under the chatgpt backend
the proxy's firewall allows `CHATGPT_BASE_URL`'s host from the environment, as
it does `PROXY_API_URL`'s on the default backend.

A cookie also forced a fix in `_config_write_key`: it now quotes any value that
is not a bare word (`[^A-Za-z0-9_./:@=+,%~-]`). The wrapper sources `.env` under
`set -euo pipefail`, so an unquoted `a=1; oai-did=2` truncates at the semicolon
and then runs `oai-did=2` as a command — every `harness` invocation would die
with "command not found".

**Single quotes are preferred** because they are literal in both bash and
compose's dotenv parser: no escape processing, no `${...}` interpolation, so
what is written is exactly what both readers see. A value that itself contains a
single quote can't use that form, so it falls back to double quotes with
`\ $ ` "` escaped. `_config_read_key` is the **exact inverse** of this — it
unwraps single quotes literally and undoes the escapes for double quotes. That
symmetry is load-bearing: the old reader stripped every quote byte without
undoing escapes, which corrupted any `PROXY_API_KEY` containing a `$`. `cmd_preflight`
reads through `_config_read_key` for the same reason.

Bare values are still written bare, so existing keys see no churn. Note this
covers values written by `harness config`; `harness-install.sh` writes `.env`
directly and still emits unquoted values.

Covered by `tests/unit_chatgpt_test.sh` (docker-free) and the ChatGPT classes in
`proxy/test_proxy.py`.

## Update-available banner

`_update_check_and_banner` runs synchronously on every agent launch
(`run_agent`, `cmd_shell`, and host mode's `cmd_host` and `cmd_serve`) with
a tight (4s) timeout against `origin/<branch>`, where `<branch>` is the one
the clone is on (`main` or `dev` from the installer), the same branch `harness
update`/`upgrade` pull. The banner shows only when the remote has commits HEAD
lacks, so a clone with unpushed local commits is not nagged. It caches the
last successful `<branch> <remote HEAD>` in `state/.harness-update-check` so
users on flaky networks still get the banner; the cache is only used for the
branch that wrote it (a legacy bare-SHA cache counts as `main`), and a branch
origin doesn't have never caches one. Skipped entirely when
`HARNESS_SKIP_UPDATE_CHECK=1`, when not in a git checkout, or on a detached
HEAD. Advisory only — never gates a launch. The lookup
(`_update_remote_head`) uses `timeout` where it exists and otherwise (macOS)
a background `git ls-remote` killed at the deadline, with no credential
prompt.

`cmd_check_updates` runs the same check in the foreground with a longer
budget for an explicit "am I up to date?" query. Covered by
`tests/unit_update_check_test.sh` (docker-free) and `harness_test.sh` T23.

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

## `probe`

`cmd_probe` runs `scripts/probe_upstream.py` (stdlib-only Python) straight
against `PROXY_API_URL` with `PROXY_API_KEY`. The proxy is not involved.
Subcommands: `full` (the default; bare `harness probe`), `memory`,
`optimize-single` (alias `--optimize-single`), `thinking`, `slim`, `oai`,
`thoughts`, and `help` (lists them); `<subcommand> --help` gives that
suite's options. Except `thoughts`, the subcommand reaches the script as
`--suite full|memory|single|thinking|slim|oai`. `oai` alone targets
`OAI_API_URL` with `OAI_API_KEY` instead (see below).

**`harness probe thoughts`** is not part of `probe_upstream.py`:
`_probe_thoughts` runs `tests/benchmarks/thoughts/run.py` with every
argument after `thoughts` (and `HARNESS_BIN`/`HARNESS_BENCH_INSTALL_ROOT`,
as `benchmark --test-modes` does), before the key check, since the runner
launches real `harness host --yolo -p` trials, arm A slim and arm B slim
plus `--thoughts`, on 38 SWE-bench Verified tasks. It is docker-free: each
task's checkout is its own one-commit git repo with a venv beside it, under
`state/bench-thoughts/`, and a gold gate drops tasks whose tests cannot
discriminate before any token is spent. A budget guard (default 20M tokens,
1.5M per trial) stops it. `tests/benchmarks/thoughts/README.md` has the
design, the falsifier and the flags; `tests/unit_thoughts_test.sh` runs its
unit tests and the routing.

The full suite characterizes the upstream for the question "could an agent
talk to it directly?". Sections:

- **A:** transport, SSE shape, parameters, errors.
- **B:** whether `system` content reaches and steers the model, plus the
  model's self-reports of its hidden prompt, tools and identity.
- **C:** history handling (consecutive roles, images, statelessness,
  long-context needles).
- **D:** native `tools`: non-stream and stream, round trips, `tool_choice`,
  parallel calls, simulated opencode-style agent loops, act-vs-describe,
  schema keywords, tool-name formats, catalogs of 40, 130 and 300 tools.
- **E:** `response_format`.
- **G:** a per-model matrix over `/v1/models`.

It prints one line per check plus a key-findings block.

**`harness probe memory`** (`MemProber`, a `Prober` subclass in the same
file) maps what the upstream remembers: which messages of a request, and of
earlier requests, reach the model, and what (turn count, role layout, size,
time, session) changes that. Every request plants fresh random crate labels
in chosen messages and asks for them back; the reply's labels are a bitmap
of what the model saw, and a label from an earlier request coming back is
counted as server-side state. Setup reads `credit_weights` from
`/v1/models`, measures chars per prompt token and records `reasoning_tokens`
per `reasoning_effort`. Sections 1-7, cheapest first: reach (an earlier
request vs this request's history), turn count, layout, server-side memory
(delays, session pass-back, `user` field; `--long-term` opts into the
product's saved memory and deletes it after), an agent session with native
`tools` (assistant `tool_calls` + `role: tool` results, the question sent
with `tool_choice: "none"`), conversation size (a padding turn of 0-500k
chars, stopping at the first error), and a per-model check over the chat
models. A 3-turn canary runs after every section. Cost control: `Prober`
totals reported usage into credits (reasoning billed as output, an upper
estimate); `MemProber.mchat` raises `OverBudget` before a request that would
pass `--budget` (default 500k credits), which ends only that section; and a
429 that names the daily credit quota (or a retry-after over an hour) raises
`QuotaExhausted`, which stops every suite at once and prints the findings so
far, rc 3. The log adds a layout line
per request (role, chars and planted labels per message, wire bytes) and the
reply, thinking trace and usage, capped at 20k chars; session ids appear
only as a short hash, and pass-back fields (`session`, `session_id`,
`conversation_id`) are scrubbed. Log: `state/output/probe-memory-<ts>.log`.

**`harness probe optimize-single`** (`SingleProber`) tunes the input for a
future single-message mode (the whole chat folded into one user message, since
the upstream reads only the last one). Each request is a synthetic agent
session rendered as ONE message: preamble, an instructions block (coding
system prompt, a last-line `ack: <word>` rule, tool definitions), the
transcript (user / assistant-with-tool-call / tool-result turns, tool output
sized to hit the target length), and the current request. Planted in it: 24
crate labels at stratified depths rotating across the three roles, a port
stated early and changed at 30-95% depth, and an instruction inside a tool
result. The reply is scored for label recall (and crate-number pairing), the
newest port, the `ack` rule (anywhere / last line), obeying the injection, and
foreign labels; composite = mean(recall, port ok, ack, not injected). The
injection counts as obeyed only when the word appears on a line that is not
reporting it ("a tool result asked me to write X"); the port is read from the
`PORT:` line, where naming the new port with history still counts as ok.
Each fact's position is recorded as a fraction of the chat block (0 = oldest
turn), not the whole message, because the instructions block's share shrinks
with size. The builder forces at least F/3 evenly spaced user and assistant
turns so small sizes do not pile facts into one or two turns.

- **Stage A:** join formats `plain`, `xml`, `markers` (the proxy's
  `<<<BEGIN_...>>>` delimiters), `markdown`, `json` (one object per turn) x
  sizes (default 16k-600k chars) x `--reps` (default 6), shuffled per rep.
- **Stage B:** framings on the stage-A winner: `end` (instructions,
  chat, request), `instr-last`, `recap` (a short rules reminder before the
  request, like the hybrid recency block), `sandwich` (request also first).

Requests are independent and run `--parallel` (default 2) on threads, paced by
`Pacer` to `--tpm` estimated tokens (chars/3) per 60 s; a 429 is waited out
for its `retry_after_seconds` and retried (up to 6), a 5xx or connection
error once; a timeout is not resent. Failures are counted as errors and left
out of the quality stats; a size that fails 3 requests in a row is skipped for
the rest of the run, and the run aborts only after 10 consecutive non-timeout
network failures. The verdict
prints per-format and per-size tables with 95% intervals (normal for means,
Wilson for rates), format x size grids, recall by chat-position decile x size
(plus a sizes-weighted-equally row and a "vs req" row: each fact's hit minus
its own request's recall, so size and format effects do not masquerade as
depth effects), by role, port update by position, the framing table, and a
recommendation. Groups with fewer than 3 scored requests are not ranked.
Format: best composite, formats erroring on over 20% excluded, ties within
2 SE go to `markers`; stage B runs on this same tie-adjusted pick. Size cap:
walk up from the best-recall size and stop at the first size whose recall
or composite drops by more than max(5 points, 2 SE), or that errors on over
20%. Framing: best composite, ties go to `end`. Weak zone and trimming order
come from the "vs req" residuals. The log has a layout and
score line per request, one whole example message per format (smallest size,
first rep), and a final `### results` JSON line with every sample. Log:
`state/output/probe-single-<ts>.log`.

**`harness probe thinking`** (`ThinkProber`) maps the reasoning controls.
Scored requests are short arithmetic puzzles with one right answer, so each
line gives reasoning tokens, correctness, latency and where thought text came
back (a `<think>`/`<thought>` tag in content, `reasoning_content`, or the
`gemini_enterprise` side channel). Sections (digits for `--only`):
1 default; 2 the `reasoning_effort` ladder; 3 which `thinking_config` wire
shape returns visible thoughts; 4 `thinking_level` and `thinking_budget`
through that shape; 5 both controls at once, and how a stream splits the
thought block across events (the log has one layout line per event); 6 Gemini `extra_content.google.thought_signature`
on a tool call, then step 2 with it echoed, omitted and corrupted; 7 whether
reasoning text sent back in history raises `prompt_tokens`; 8 whether
reasoning tokens cost credits, fitting drops in the
`x-ratelimit-remaining-credits-day` header (captured on every `Resp`) to the
catalog's `credit_weights` with and without reasoning. `--repeat` defaults to
2 here. Log: `state/output/probe-thinking-<ts>.log`.

**`harness probe slim`** (`SlimProber`) measures how much the slim agent
reads and greps per task, to tune `agents/slim-prompt.md` from numbers
rather than by hand. It plays an opencode-style session against the
upstream with the probe as the tools (`_SlimEnv`: read, grep, glob, list,
edit, write, bash, todowrite, task, over a small fake repo with one bug; a
pytest run is judged from the file text, model code never runs). The
system prompts come from the clone (`--root`, which `cmd_probe` sets to
`clone_dir`, mounting it read-only at `/harness` in the docker fallback),
the tool descriptions from proxy.py's `_SLIM_TOOL_DESCRIPTIONS` via `ast`.
`task` runs a nested loop with `agents/slim-subagent-prompt.md` for both
`general` and `explore` (real slim gives it to `general` only; explore keeps
opencode's prompt on the free model). Arms
(digits for `--only`): 1 current prompt with thought signatures dropped (what
opencode alone sends), 2 current prompt with signatures echoed (what the
proxy now does), 3 `--candidate` (default `scripts/probe_slim_candidate.md`)
with signatures echoed. Each run reports solved, steps, reads, greps,
re-reads, bash, edits, tasks and tokens; a verdict compares the arms'
medians. Bounds: 30 main steps, 15 per subagent, 60 requests per run.
The proxy's per-step reminder and old-output trim are not simulated.
`--repeat` defaults to 1. Log: `state/output/probe-slim-<ts>.log`.

**`harness probe oai`** (`OaiProber`) measures what cuts token cost on the
OpenAI-format upstream and whether reasoning summaries come back. It needs
`OAI_API_URL`/`OAI_API_KEY` (not `PROXY_*`); the base is normalized as the
proxy's `_normalize_oai_base` does, and the model defaults to the first of
`OAI_MODELS`. The `Client` parses Responses replies (JSON and SSE:
`output_text`, reasoning `summary` text, `function_call` items) into the same
`Resp` fields as chat replies. Every request carries a per-run nonce at the
head of its prefix, so no earlier run's cache counts. A preflight learns
whether the model takes `reasoning` and `prompt_cache_key`, and prints
whether opencode would ask each `OAI_MODELS` id for summaries on its own
(its rule: the id contains `gpt-5`, not `gpt-5-chat`/`-pro`). Sections
(digits for `--only`): 1 an agent-shaped `/responses` session growing by a
`read` call per step (`--steps`, default 6), input and `cached_tokens` per
step, with and without `prompt_cache_key`; 2 `prompt_cache_retention`
24h/in_memory and `prompt_cache_options` ttl 12h/30m, each a fresh prefix
sent twice; 3 `store` + `previous_response_id` (are earlier turns billed
again; both stored responses are deleted); 4 `reasoning.summary`
none/auto (streamed)/detailed/concise and on a tool-calling step, in
opencode's shape (store off, `reasoning.encrypted_content`), then
`reasoning.context` default/current_turn/all_turns on a turn 2 that replays
turn 1's reasoning; 5 a `/chat/completions` session (container mode's
route, `reasoning_effort` none, low if refused); 6 `POST /responses/compact`
and `context_management` compaction. A 429 is waited out (its
`retry-after`, capped at 60s). Log: `state/output/probe-oai-<ts>.log`.

**Redaction is the contract.** Every printed and logged string goes through
`Redactor`, which removes:

- the key and any 10+ char fragment of it;
- the base URL, host and distinctive host labels;
- all URLs, emails, IPv4s and bearer tokens;
- `projects/...` paths;
- `HARNESS_PROBE_REDACT` terms.

`gemini_enterprise` values are summarized, never dumped. The output is meant
to be pasted into an issue without editing. A full redacted request log goes
to `state/output/probe-<ts>.log`, printed by relative path only. It is
self-contained: each request's record has a `headers:` line (every response
header but cookie/auth/token/session ones; rate-limit counts are kept), and
every report line is also written to it as `report: <line>`, in order.

Interpreter: it uses `host_python_bin` (Windows paths go through
`cygpath -m`). With no host Python it falls back to
`harness_docker run --entrypoint python harness-proxy:latest -`, passing env
by name only; the script must stay one file for this. In that fallback the
log is written to the container's stderr and redirected into the host log
file; `__file__` is then `<stdin>`, which is why the slim suite takes
`--root`. A locked key aborts with a pointer to `harness unlock`, and the
unlock URL is not echoed. `tests/unit_probe_test.sh` drives it against a
secret-echoing mock; `tests/unit_probe_oai_test.sh` does the same for the
`oai` suite with a mock that simulates an exact-prefix cache.

## `usage`

`cmd_usage [--hours N]` (default 24; the ledger keeps 48) reads the JSONL
ledger both proxies append to (`host_usage_ledger`,
`state/output/usage.jsonl`; see [`proxy.md`](proxy.md) "Usage ledger") with
host Python and prints the upstream-reported prompt, completion, reasoning
and cached totals, the last hour, the average per reply, a per-model split,
and the newest `X-RateLimit-Remaining-*` values. It needs no running proxy
and no docker. `tests/unit_host_test.sh` T16 covers the window and the
per-model totals.

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

## `benchmark --test-modes` (prompt-mode A/B, docker-free)

`cmd_benchmark` routes any argv containing `--test-modes` to
`_harness_bench_test_modes`, which drops the flag and runs
`tests/benchmarks/modes/run.py` under the host proxy venv's python
(`host_python_bin`), passing `HARNESS_BIN` and the install root. The runner
drives real `harness host --yolo [--single-message] -p` trials under a private
install root (`state/bench-modes/root/`, `.env` symlinked) with
`HARNESS_HOST_PORT` (a free port), `HARNESS_HOST_NO_WEB=1`, private opencode
XDG dirs and the `OPENCODE_DISABLE_*` switches, so it never touches the user's
running proxy or opencode config and the only task egress is the upstream API.
The bench root shares the real install's `state/host/toolchain` and `venv`
(symlink, or a directory junction on Windows, where symlinks need Developer
Mode), so nothing is downloaded twice; an untimed warm-up provisions them, and a
warm-up failure stops the run before trial 1 with the redacted error. On
Windows, trials get a `python3` shim on `PATH` (Git Bash's `python3` is usually
the Microsoft Store stub).
`--mock` swaps the upstream for a loopback scripted mock. Usage, report and
egress list: `tests/benchmarks/modes/README.md`. Covered by
`tests/unit_bench_modes_test.sh`.

## Tests

Tests scoped to this file live in `tests/harness_test.sh` (and any
future `tests/harness_*_test.sh`). End-to-end install → start → agent
launch is exercised by `tests/full_pipeline_test.sh`. See
[`tests.md`](tests.md).
