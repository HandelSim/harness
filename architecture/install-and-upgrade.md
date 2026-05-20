# Install and upgrade

How a host gets from "fresh shell" to "running harness", and how an
existing install moves forward as the repo evolves.

## `harness-install.sh`

A standalone bash script users download from the repo's `main` branch and
run from an empty directory. Stages:

1. **Preflight.** Inline-defined `_inline_*` helpers (so the script
   works pre-clone, when `scripts/lib/platform.sh` isn't local yet)
   check: container runtime present (docker or podman), runtime reachable
   (`docker info`), required commands available. Failures abort with a
   clear message.
2. **Intent prompts.** Confirms before any writes, asks whether to add a
   `harness` wrapper to PATH, and offers to capture an upstream API key now
   (written into `PROXY_API_KEY` in `.env` after seeding; declining leaves
   it for the user to edit manually). No key validation — whatever is
   pasted is accepted verbatim.
3. **Clone.** `git clone` into the install dir. **The clone IS the
   install root** — there's no separate config dir.
4. **Source full `platform.sh`** now that it's local. Subsequent helpers
   come from the library.
5. **dos2unix on Windows.** Defense-in-depth: ensures bash scripts have
   LF line endings even if Git's autocrlf put CRLFs in the working tree.
6. **Runtime state dirs.** Creates `state/output/`, `state/agent/home/`,
   `state/ollama-data/`, `state/mcp/`.
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
   without re-exporting. The initial `git clone` itself just inherits the
   shell's proxy (git's libcurl honors these env vars). These are host-only;
   `harness` strips them from containers (see [`containers.md`](containers.md)).
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
   `PROXY_API_KEY` etc.) and `cd` into a project to run `harness claude`.

Uninstall is `rm -rf <install-root> && rm ~/.local/bin/harness`.

## `harness update` — code-only

`harness update` is a fast-forward `git pull` in the clone. Use this
when all you want is the latest harness code with no side effects on
config or state. No image rebuilds, no compose restart.

## `harness upgrade` — full flow

`harness upgrade` runs the full migration flow:

1. `git pull --ff-only` in the clone.
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

## Agent-launch config merge

`run_agent` (claude/opencode) and `cmd_shell` call
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
