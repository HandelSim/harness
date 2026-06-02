# Proposal: `harness mcp register` — dynamic MCP registration

Status: proposal (not built). Lives under `.planning/` because it describes
planned work, not current architecture. Authoritative current behavior is
`architecture/mcp.md` + the `harness` script.

## Problem

Today an MCP can only be brought into the runtime via `harness mcp install
<name>`, which reads exclusively from the repo-tracked `mcp-registry/<name>/`
(`harness:3218` errors `unknown MCP` otherwise). Registering a new MCP means
forking the repo, adding a `mcp-registry/<name>/` dir, and committing. The owner
wants to register MCPs dynamically (without a repo commit) while keeping the
guarantee that registered MCPs come up when `harness` starts.

## Key realization: startup is already dynamic and registry-independent

`harness start` carries no baked-in MCP list. `mcp_compose_files` (`harness:579`)
walks `state/mcp/*/` on every run and includes any entry that has a `compose.yml`
and `enabled: true` (`mcp_is_enabled`, `harness:628`), splicing `-f <snippet>`
into the single merged `docker compose up` via the `compose` wrapper
(`harness:778`). `any_mcp_active` decides the `--profile mcp` flag. The enabled
set is re-read from disk every start. A hand-dropped `state/mcp/<name>/` with no
registry source is already discovered, listed (as `local`), and merged.

So the auto-start guarantee already holds for anything in `state/mcp/<name>/`.
Dynamic registration reduces to one job: **write a validated
`state/mcp/<name>/` from a user-supplied source.** No startup code changes.

## Command surface

```
harness mcp register <name> --from <dir|git-url> [--ref <ref>]
                            [--no-enable] [--start] [--force]
```

- `<name>`: active-tree name. Defaults to basename of `--from` if omitted.
- `--from <dir>`: a registry-entry-shaped directory (same four-file contract as
  `mcp-registry/<name>/`: `compose.yml` required, `client-config.json` required,
  `harness-meta.json[.template]` optional, `README.md` optional). This is the
  primary path.
- `--from <git-url>`: convenience. Clone (reusing install's existing
  `repo_clone_url` machinery, `harness:3265-3326`), then look for the descriptor
  files at repo root or `harness-mcp/`. If absent, error and tell the user to
  point `--from` at a dir containing `compose.yml` + `client-config.json`.
- `--ref <ref>`: clone ref (branch / tag / full 40-char SHA), same rules as
  `repo_clone_ref`. Source's `harness-meta` `repo_clone_url`/`_ref` are honored
  when present.
- `--no-enable`: stage as `enabled: false` (don't arm for auto-start).
- `--start`: after a successful register, boot it now (`cmd_mcp_up`) and report
  health, instead of waiting for the next `harness start`.
- `--force`: re-register over an existing installed entry (mirrors install's
  re-install guard; `data/` is preserved).

## Behavior (materialize → validate → arm)

1. Resolve `<name>`. Refuse if already installed (`mcp_is_installed` =
   `compose.yml` exists in active tree) unless `--force`.
2. **Warn on shadow:** if `<name>` matches an existing `mcp-registry/` entry,
   warn. A registered entry that later shares a registry name would start being
   managed by `harness upgrade` (`directory_overwrite` fires on
   `condition: installed`). Recommend a distinct name.
3. Materialize into a **staging dir** (`state/mcp/.staging-<name>/`), not the
   live path. Clone the upstream repo here if a `repo_clone_url` is declared.
   Synthesize `harness-meta.json` (`enabled` per `--no-enable`) if the source
   only ships a `.template` or none.
4. **VALIDATE (the load-bearing step).** Two checks, both before anything is
   armed:
   - **Merge check:** run the harness `compose` machinery in `config -q` mode
     with the staged snippet appended to the full current `-f` set (main +
     runtime override + all currently-enabled MCP snippets). This catches bad
     YAML and graphs that fail to merge.
   - **Service-name collision check:** diff `compose config --services` with and
     without the staged snippet; every service the snippet defines must be NEW.
     Compose silently merges same-named services (override semantics) rather than
     erroring, so two MCPs both defining a service named `mcp` would clobber each
     other invisibly. `config -q` will not catch this; an explicit service-name
     diff will. (Nice-to-have: warn on duplicate published host ports, which
     fail only at `up` time.)
   On any failure: print the error, discard staging, exit non-zero. Nothing in
   `state/mcp/` changed.
5. On success: atomically move staging → `state/mcp/<name>/`, `mkdir -p data/`.
6. **Firewall:** print the `harness net allow <host>` recommendation block from
   the source's `allowed_domains` (reuse install's block, `harness:3342-3361`).
   Never modify the allowlist automatically. This preserves the existing posture:
   MCPs are third-party code; the operator opens egress explicitly.
7. **Arm:** nothing extra needed. If enabled, the next agent launch's
   `write_agent_mcp_config` (`harness:2243`) folds its `client-config.json` into
   `.harness-mcp-servers.json`, and the next `harness start` includes its
   compose snippet. If `--start`, call `cmd_mcp_up` now.
8. Print: landed path, enabled state, services, and next-step hint
   (`harness start` / `harness mcp up <name>`).

## Why validate-then-enable (not disabled-by-default)

The only failure mode that affects OTHER services is a snippet that fails to
merge: because all MCP snippets splice into ONE `docker compose up`, a malformed
snippet fails the whole `up` and takes the proxy and agent down with it. The
mandatory `config -q` + service-name checks in step 4 close that hole at register
time. A single MCP's *runtime* crash is isolated: compose starts it and moves on,
and `restart: unless-stopped` retries it without affecting siblings. So once the
merge is validated, defaulting to enabled is safe and matches the owner's stated
goal ("make sure they come up when harness does"). `--no-enable` is there for
users who want to stage without arming.

## No inverse command needed

`harness mcp uninstall <name> --force` already operates on the active tree
(`$mcp_active_dir/$name`), removes config, and preserves `data/`
(`harness:3368+`). It works on a registered (non-registry) entry unchanged. Do
not add a separate `unregister`.

## Recommended implementation shape

`install` and `register` converge: both "materialize a source dir into
`state/mcp/<name>/` (+ optional clone) + recommend firewall opens." Factor that
shared body out of `cmd_mcp_install` into a helper; `install` calls it with
`src=$registry_dir/<name>`, `register` with `src=<--from dir>`. This keeps
behavior identical and avoids drift.

Opportunity: the step-4 validation gate benefits `install` too — a bad
registry-committed snippet can equally brick startup, and install does not
validate-merge today. Backfilling the gate into the shared helper hardens both
paths. (Optional; scope to taste.)

## Non-goals (do not build)

- A second registry root, a registration daemon, or a filesystem watcher. The
  fresh-evaluation-on-start already gives dynamic pickup.
- Hot-reload of a running stack. Registration arms the NEXT `harness start`
  unless `--start` is passed.
- New MCP transport types. Same Pattern A (HTTP/SSE containers in Docker) as
  today.
- Auto-opening the firewall allowlist.

## Test plan (`tests/mcp_test.sh` additions, T-style)

- TR1: `register <name> --from <valid-dir>` materializes `state/mcp/<name>/`
  with `compose.yml` + `client-config.json` + `harness-meta.json` (`enabled:
  true`) and creates `data/`.
- TR2: `register --from <dir-with-malformed-compose>` is rejected; nothing left
  in `state/mcp/` (staging discarded); non-zero exit.
- TR3: service-name collision — register a snippet whose service name already
  exists in the merged graph is rejected.
- TR4: `--no-enable` lands `enabled: false`; `harness start` does NOT bring it
  up; `harness mcp enable` + start does.
- TR5: enabled registered MCP appears in `mcp_compose_files` output and in
  `.harness-mcp-servers.json` (mirrors T7).
- TR6: register refuses an already-installed name without `--force`; `--force`
  re-materializes and preserves `data/`.
- TR7: register prints `allowed_domains` → `harness net allow` recommendations
  and does NOT modify the allowlist (mirrors T18/T19).
- TR8: `harness mcp uninstall <registered-name> --force` removes config,
  preserves `data/` (confirms the inverse; no new command).
- TR9: register a name that exists in `mcp-registry/` prints a shadow warning.
- TR10 (docker tier): `register --from <git-url-source>` clones into
  `state/mcp/<name>/repo/` at the pinned ref; `--start` boots and reports health.

## Interaction with the ollama-removal milestone

This feature shares surfaces with the active ollama-removal milestone (compose
merge, the `harness` CLI, firewall wiring, `tests/mcp_test.sh`). Sequence it as
its OWN milestone AFTER the ollama removal, not folded in:

- MCP startup is ollama-independent (`mcp_compose_files`/`any_mcp_active` do not
  reference ollama), so the removal does not threaten MCP auto-start.
- But the ollama removal must update the MCP-adjacent ollama references it will
  break: `tests/mcp_test.sh` T6 (`tests/mcp_test.sh:321-326`) asserts the
  **ollama** container is up as the "core service" next to MCP services — Phase 3
  / 4 must swap that assertion to the **proxy**. The `harness mcp help` text that
  lists services "proxy, ollama, agent, plus any installed MCP"
  (`harness:3807`) also needs the ollama mention removed.
- Designing `register`'s merge-validation against the post-ollama compose graph
  (one fewer service, proxy-direct) avoids rework. Land ollama removal first,
  then build `register` on the settled topology.

These two T6 / help-text items are existing ollama↔MCP couplings; they belong in
the ollama milestone's Phase 3-4 test/doc updates (REQ-009/REQ-010), already in
scope.
