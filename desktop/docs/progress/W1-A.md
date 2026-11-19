# W1-A: main-process fixes from review pass 1 (groups A, C, D)

Status: IN PROGRESS (started 07:52 UTC, stop 08:42 UTC).

## Contract changes (additive, src/shared/ipc.ts + src/preload/index.ts)
- `PathInfo` gains `real?: string` (realpath of the folder) and `repo?: GitProbe`:
  `GitProbe = { isRepo, root (--show-toplevel), branch (symbolic-ref -q --short HEAD, '' when detached), detached,
  hasCommits, inSubfolder, error? }`. Legacy `git: {branch} | null` kept (null when not a repo).
  `shell:path-info` is async (fs.promises + async git, each raced at ~1.5 s).
- `bridge.app.busy(): Promise<AppBusy>` (`app:busy`): `{ terminals (live ptys), queueRunning, ownedServe, onQuit, jobs (running job kinds) }`
  for the W15 confirm before Restart-to-update / Upgrade / quit (renderer dialog is W1-C / group H).
- `bridge.harness.jobs(): Promise<JobInfo[]>` (`harness:jobs`): running + last finished jobs with an output tail.
  `harness.job(kind)` returns the RUNNING job's id when one of that kind (or any upgrade/serve-stop) is running.

## Done
(filled in as items land)

## Skipped / remaining
