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
2. **Intent prompts.** Asks where to clone (default `./harness/`), and
   confirms before any writes.
3. **Clone.** `git clone` into the install dir. **The clone IS the
   install root** — there's no separate config dir.
4. **Source full `platform.sh`** now that it's local. Subsequent helpers
   come from the library.
5. **dos2unix on Windows.** Defense-in-depth: ensures bash scripts have
   LF line endings even if Git's autocrlf put CRLFs in the working tree.
6. **Runtime state dirs.** Creates `state/output/`, `state/agent/home/`,
   `state/ollama-data/`, `state/mcp/`.
7. **`.env` and `.harness-allowlist`.** Seeds from `.env.example` and
   `.harness-allowlist.example` (no overwrite if the user's targets
   already exist).
8. **PATH wrapper.** Writes a `harness` script wrapper to
   `~/.local/bin/harness` that `exec`s into `<install-root>/harness`.
   Prints a one-line "add to PATH" reminder if `~/.local/bin` isn't
   already there.
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
2. Apply upgrade actions from `scripts/upgrade-manifest.json` to the
   install root.
3. `harness down --remove-orphans` and `harness start` (skippable via
   `--no-restart`).

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
