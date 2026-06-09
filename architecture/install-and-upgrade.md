# Install and upgrade

How a host gets from "fresh shell" to "running harness", and how an
existing install moves forward as the repo evolves.

## `harness-install.sh`

A standalone bash script users download from the repo's `main` branch and
run from an empty directory. Stages:

1. **Preflight.** Inline-defined `_inline_*` helpers (so the script
   works pre-clone, when `scripts/lib/platform.sh` isn't local yet)
   check `git`, disk space, write access, and a free clone dir; failures
   abort with a clear message. The **container runtime is optional**: harness
   has a containerless `host` mode (`harness host` — see
   [`harness-cli.md`](harness-cli.md) → "Host mode"), so a missing/unreachable
   docker or podman is a warning, not an abort. When neither runtime is found,
   preflight sets a `HOST_ONLY` flag and the closing message tells the user to
   use `harness host` and that container subcommands will need docker installed
   later. (When a runtime *is* present, compose-availability and reachability
   are still reported, but as warnings — container mode needs them, `harness
   host` does not.) On the `HOST_ONLY` path the installer also **probes the
   host-mode prerequisites**, but only `python3` is a real requirement (it
   bootstraps the proxy venv, so harness can't fetch anything without it) — a
   missing `python3` warns. `jq`, `node` >= 20, and `opencode` are **not**
   warned about: `harness host` auto-provisions them into `state/host/toolchain`
   on first run (`host_ensure_toolchain` — see [`harness-cli.md`](harness-cli.md)
   → "Host mode"), so the probe reports a present host binary as "reused" and an
   absent one as "harness fetches it automatically", informational either way.
   The node-major check parses `node --version` and notes a major < 20 will be
   superseded by the vendored Node, not an error.
2. **Intent prompts.** Prints what the install will do, then asks whether to
   add a `harness` wrapper to PATH and offers to capture an upstream API key
   now (written into `PROXY_API_KEY` in `.env` after seeding; declining leaves
   it for the user to edit manually). No key validation — whatever is
   pasted is accepted verbatim. The key prompt (`_read_secret_masked`) echoes
   one `*` per character instead of hiding input entirely, so a wrong/blank
   paste is visible by length without revealing the value (backspace works; it
   falls back to a fully hidden read off a tty). There is no separate "continue?" gate: the
   PATH prompt is the first interactive stop (so the intent text still gets a
   beat of consideration) and Ctrl-C aborts at any prompt. The old confirm
   prompt was also broken when the script is sourced — its abort called
   `exit_or_return 0`, whose `return` only leaves that helper, not the sourced
   script, so "n" continued the install anyway. **All fatal sites** (preflight
   failure, pre-existing install root, failed clone, missing `platform.sh`)
   abort correctly in both modes by running the terminating `return`/`exit` at
   the script's **top level**; a `return` buried in a helper (`fail`,
   `exit_or_return`) can't end a sourced script, which was the root cause of
   #105/#106.
3. **Clone.** `git clone` into the install dir. **The clone IS the
   install root** — there's no separate config dir. A failed clone (no
   network, unreachable git host, corp proxy not exported) is caught by
   checking git's exit code and aborts with an actionable message — the
   installer never proceeds past a broken clone. The clone's proxy is
   resolved first: `HTTP_PROXY`/`HTTPS_PROXY` set in a `.env` dropped beside
   the installer are exported for it (both upper- and lower-case, since git's
   libcurl gives the lower-case name precedence); a blank/absent value there
   leaves the host's exported proxy untouched.
4. **Source full `platform.sh`** now that it's local. Subsequent helpers
   come from the library.
5. **dos2unix on Windows.** Defense-in-depth: ensures bash scripts have
   LF line endings even if Git's autocrlf put CRLFs in the working tree.
6. **Runtime state dirs.** Creates `state/output/`, `state/agent/home/`,
   `state/mcp/`.
7. **`.env` and `.harness-allowlist`.** For each, in priority order: leave
   a target already in the install root untouched → else copy one dropped
   beside the installer (the directory containing `harness-install.sh`,
   `$script_dir` — **copy, not move**, so the shipped folder stays intact)
   → else seed from the `.example`. This lets a distributor ship
   `harness-install.sh` + a pre-edited `.env` + `.harness-allowlist` as one
   folder and have the installer place them automatically. If an API key
   was captured at the prompt, its value is then written into the `.env`'s
   `PROXY_API_KEY=` line (prompt wins over any pre-placed value; the
   rewrite touches only that line via a bash read-loop, no `sed` escaping).
   If `HTTP_PROXY`/`HTTPS_PROXY` are exported in the installing
   shell, their values are persisted into the `.env` (filling only blank
   lines, so a pre-placed value wins) so later `harness` runs reuse the proxy
   without re-exporting. The initial `git clone` (stage 3) takes its proxy
   from, in priority: the beside-installer `.env`'s `HTTP_PROXY`/`HTTPS_PROXY`
   if set, else whatever the host shell exports (git's libcurl honors these
   env vars). `harness` later exports them so `docker compose build` runs
   through the proxy too; they are not forwarded into running containers (see
   [`containers.md`](containers.md)).
8. **PATH wrapper.** Writes a `harness` script wrapper to
   `~/.local/bin/harness` that `exec`s into `<install-root>/harness`.
   Prints a one-line "add to PATH" reminder if `~/.local/bin` isn't
   already there. The PATH export goes to the shell rcfile (`.bashrc` for
   bash). On **Windows Git Bash** the installer also bridges
   `~/.bash_profile` → `~/.bashrc`: Git Bash starts login shells, which read
   `~/.bash_profile` and skip `~/.bashrc`, so without the bridge the PATH
   line never runs in fresh sessions. The bridge preserves an existing
   `~/.profile` when it has to create `~/.bash_profile`.
9. **Final message.** Tells the user to edit `.env` (set
   `PROXY_API_KEY` etc.) and `cd` into a project to run the agent. The
   printed run command is host-aware: on a `HOST_ONLY` install (no runtime
   detected) the "Next" steps say `harness host`, otherwise `harness`.

Uninstall is `rm -rf <install-root> && rm ~/.local/bin/harness`.

## `harness update` — code-only

`harness update` is a fast-forward `git pull` in the clone. Use this
when all you want is the latest harness code with no side effects on
config or state. No image rebuilds, no compose restart.

If the clone's branch has **diverged** from its upstream (the remote was
rebased/force-pushed, so `--ff-only` can't proceed), `update` re-fetches
and — only on a true divergence, only with a terminal — offers a
`git reset --hard @{u}` that discards the local commits to recover. The
prompt defaults to **N**; declining (or any non-divergence pull failure,
or no terminal) aborts as before. Shares the `_upgrade_pull_or_reset`
helper with `harness upgrade`.

## `harness upgrade` — full flow

`harness upgrade` runs the full migration flow:

1. `git pull --ff-only` in the clone. If the branch has **diverged** from
   its upstream (so a fast-forward is impossible), re-fetch and — only on
   a true divergence — offer a `git reset --hard @{u}` recovery
   (defaults to N; `--no-prompt`/CI never auto-resets; non-divergence
   failures abort unchanged). Accepting the reset advances `HEAD`, so the
   re-exec in 1a fires and the rest of the upgrade runs on the reset code.
1a. **If the pull advanced `HEAD`**, re-exec into the freshly-pulled
   `harness` with `--resume-after-pull` so `cmd_upgrade`'s own
   orchestration (flag parser, manifest runner, rebuild/restart) runs
   from new bytes for the rest of this upgrade. Gated on `bash -n` of
   the new file: if the syntax check fails, we warn and continue with
   the in-memory pre-pull orchestrator instead of halting mid-flight.
   `scripts/lib/upgrade_actions.sh` and the manifest are re-sourced /
   re-read post-pull regardless and so don't need this — re-exec
   exclusively covers changes to the `harness` script itself.
2. **Precheck** which managed files actually need a merge — pure bash +
   awk, no jq and no container (see `upgrade_envfile_needs_merge` /
   `upgrade_linefile_needs_merge`). If nothing needs merging, print
   "Configuration files are up to date — no merges needed." and skip BOTH
   the prompt and the merges. Otherwise list only the files that need
   updating and prompt once (all-or-nothing). This is why a no-op upgrade
   no longer prompts or spins jq for every config file.
3. Apply the flagged upgrade actions from `scripts/upgrade-manifest.json`
   to the install root (`apply_upgrade_actions` takes an optional ID
   filter so only the prechecked actions run).
4. `harness down --remove-orphans` and `harness start` (skippable via
   `--no-restart`).

`--check` is the exception: it lists every action and dry-runs them all
(full preview), bypassing the precheck.

**Host-only upgrade.** When no container runtime is installed
(`harness_runtime_installed` is false — `command -v docker || command -v
podman`, a binary check, not a daemon-reachability check), `cmd_upgrade`
takes a docker-free path instead of aborting at `require_docker`. It still
pulls code (step 1, the only part `harness host` actually needs — `proxy.py`
runs from the updated clone) and still merges `.env`/allowlist (steps 2–3),
then prints `[harness] host-only upgrade complete.` and `return`s **before**
the rebuild/restart block (step 4 is a no-op with no images to rebuild or
containers to restart). The host venv is not touched here: it refreshes
lazily on the next `harness host` (it is sha-stamped against
`proxy/requirements.txt`). The host toolchain (`state/host/toolchain`) is
likewise untouched at upgrade time and re-provisions lazily on the next
`harness host` only if the pulled code bumped a `HARNESS_HOST_*_VERSION` pin
(each tool is stamped against its pinned version). If a proxy is already
running, the completion message tells the user to bounce it with `harness host
down && harness host`.
The config merge needs jq; on a host-only box without jq the merge step is
skipped with a warning (the pull still applies) rather than failing. A
container install with the daemon merely stopped is NOT treated as host-only
— it still hits `require_docker`, which tells the user to start the daemon,
so a transient docker outage never silently downgrades the upgrade. The same
`harness_runtime_installed` gate makes `harness down` a host-proxy/MCP stop
(no `require_docker`) and makes `harness restart` point host users at
`harness host down && harness host` instead of erroring on a missing runtime.

Declining the `[y/n]` confirmation skips step 3 (the file merges) but
still runs step 4 — the git pull has already happened, so the rebuild +
restart are needed to avoid running stale images on new code. To abort
the whole upgrade, use Ctrl-C.

**Cross-version contract on `--resume-after-pull`.** Once a version
that introduces the re-exec is in the field, every future version must
keep `cmd_upgrade --resume-after-pull` as a valid (no-op skip-pull)
flag forever — old code in the field re-execs into new code by passing
it. Dropping the flag breaks any upgrade FROM that older version.

Upgrade actions are conservative: they add new env variables, new
allowlist hostnames, and new config keys WITHOUT overwriting existing
customizations. Each newly-introduced item is annotated with a marker
comment (`# Added by harness upgrade on YYYY-MM-DD`) so users can spot
what changed.

Flags:

- `--check` — preview only (no git pull, no writes).
- `--no-prompt` — apply without the [y/n] confirmation.
- `--no-restart` — apply without down/start (e.g. for CI).
- `--rebuild` — `compose build --no-cache`; slower.

## `harness downgrade` — move back to the previous release tag

`harness downgrade` rolls the clone back to the previous **release tag** and
then re-runs the upgrade machinery so the running images match the older code.
Release tags are deliberate releases on `main` (two-number scheme, e.g.
`v0.1` / `v1.0`); `update`/`upgrade` still track the branch tip, not tags.

Flow:

1. `git fetch --tags`, then `_downgrade_target_tag` resolves the target
   **topologically** via `git describe` (no version parsing, so two-number
   tags work): HEAD exactly on a tag → the tag before it; HEAD past the latest
   tag → the latest tag. No earlier tag → hard error.
2. A loud confirm (defaults to **N**) states how many commits the
   `git reset --hard` will discard. `.env`, `.harness-allowlist`, and `state/`
   are gitignored, so only tracked code is rolled back — same guarantee as the
   `upgrade` divergence reset.
3. `git reset --hard <target>` on the **current branch** (not a detached tag
   checkout). Staying on the branch is the whole point: the branch is now
   strictly behind upstream, so a later `update`/`upgrade` fast-forwards back
   to the tip with no detached-HEAD special-casing.
4. **Re-exec the downgraded `harness upgrade --resume-after-pull`** so the
   config merge + image rebuild + restart run from the *older* code (and any
   config var removed since the target release is merged back in).
   `--resume-after-pull` skips the git pull (downgrade already moved HEAD).
   This reuses the same re-exec hand-off and cross-version `--resume-after-pull`
   contract as `harness upgrade` (above); gated on `bash -n` of the downgraded
   script, falling back to an in-process orchestration if it fails the check.

Flags mirror `upgrade`: `--no-prompt` (skip the confirm — required for a
non-interactive run), `--no-restart` (pure git reset, no rebuild; the only
path that doesn't require docker), `--rebuild` (`--no-cache`). Because the
branch is intentionally left behind the tip, the `main` update banner will
report "update available" after a downgrade — expected, not a bug.

## Agent-launch config merge

`run_agent` (opencode) and `cmd_shell` call
`_check_and_offer_config_merge` at startup, right after the update banner.
It reuses the same precheck + merge functions as `harness upgrade`, but only
for the two config files (`.env`, `.harness-allowlist`) — not the rebuild,
restart, or MCP registry actions. Purpose: a plain `harness update` pulls new
code that may introduce env vars / allowlist hosts without merging them; this
surfaces the merge at the next agent launch instead of silently running on a
stale config.

It is deliberately cheap and non-blocking:

- The precheck is the pure-bash `upgrade_*_needs_merge` against the two
  fixed config paths — no jq, no manifest read, no container. When nothing
  changed it's a silent sub-10ms no-op on every launch.
- One prompt, all-or-nothing (matches `harness upgrade`).
- It NEVER gates the launch: headless `-p`/`--print` mode is skipped
  entirely, a missing terminal falls through with a "run `harness upgrade`"
  hint, and declining continues to the agent. `HARNESS_SKIP_CONFIG_MERGE=1`
  disables it.

Because the precheck is hard-coded to the two known config files (rather than
manifest-driven, to avoid a per-launch jq/container cost), a future *new*
config-file action added to the manifest is picked up by `harness upgrade`
but not by this launch path until the helper is extended.

## The manifest (`scripts/upgrade-manifest.json`)

The contract between this repo and a user's install root. Since the
install root IS the clone, "managed files" means files harness writes
inside the clone that aren't tracked git content: `.env`,
`.harness-allowlist`, and per-MCP state under `state/mcp/<name>/`.

Every `B3-MANAGED:` comment in the codebase has a matching manifest
entry. The comments anchor in:

- `agents/Dockerfile` (image-build-time managed files)
- `agents/entrypoint.sh` (runtime-managed in the bind-mounted home)
- `harness-install.sh` (install-time seeded files)
- `harness`'s `cmd_mcp_install` (MCP active-tree state)

Files explicitly NOT in the manifest are user-managed or regenerated
each launch — see the manifest's `description` field for the canonical
list.

## Action types (`scripts/lib/upgrade_actions.sh`)

Three action types, each implemented as a bash function. The harness
script dispatches per manifest entry, each emits one JSON-line summary,
and `cmd_upgrade` aggregates with jq.

### `envfile_merge`

Used for `.env`. Appends new `KEY=VALUE` entries from the source to the
target, preserving existing values and surfacing the source's preceding
comments for context. The user's values for keys already present in
their `.env` always win.

### `linefile_merge`

Used for `.harness-allowlist`. Appends new entries (one per line, `#`
comments ignored). If an entry exists in both files but with different
inline annotations (e.g. `# git-push`), the user's line is preserved
and a warning is emitted. Idempotent.

### `directory_overwrite`

Used for installed MCP registry definitions
(`state/mcp/<name>/`). Refreshes the managed directory tree from
`mcp-registry/<name>/`, with an explicit `preserve` list for paths
inside the directory that are user or system state (typically
`harness-meta.json` and `data/`). Files in target that don't exist in
source are left in place. The `condition: installed` field gates this
on `mcp_is_installed(name)` so registry entries the user never opted
into don't get auto-installed.

## Atomic writes

All three action functions write via `.tmp + rename` so an interrupted
upgrade can never leave a half-written config behind. The harness
script's per-call jq round-trips also use the same pattern.

## Standalone use of the library

`tests/upgrade_test.sh` sources `scripts/lib/upgrade_actions.sh`
directly. The library guards against double-source via
`HARNESS_UPGRADE_ACTIONS_LOADED` and defines a fallback `harness_jq` (no
container fallback — straight host jq, or hard error) when sourced
outside the full harness script.

## Force-reset escape hatch

To force a full reset of a harness-managed file (and lose
customizations): delete the file in the install root, then run
`harness upgrade`. The "target missing" branch of each action recreates
it from source.
