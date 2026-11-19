# W1-A: main-process fixes from review pass 1 (groups A, C, D)

Status: DONE. Started 07:52 UTC, stop 08:42 UTC.

## Contract changes (additive, src/shared/ipc.ts + src/preload/index.ts)
- `PathInfo` gains `real?: string` (realpath of the folder) and `repo?: GitProbe`:
  `GitProbe = { isRepo, root (--show-toplevel, realpath'd), branch (symbolic-ref -q --short HEAD, '' when detached), detached,
  hasCommits, inSubfolder, error? }`. Legacy `git: {branch} | null` kept: null when not a repo, and now non-null for a repo with
  no commits (branch = the unborn branch); `branch` is '' when detached, so `info.git.branch || 'detached'` still reads right.
  `shell:path-info` is async (fs.promises + async git, each raced at 1.5 s). Implementation: `src/main/pathinfo.ts`.
- `bridge.app.busy(): Promise<AppBusy>` (`app:busy`): `{ terminals (live ptys), queueRunning, ownedServe, onQuit, jobs (running
  job kinds) }` for the W15 confirm before Restart-to-update / Upgrade / quit (the dialog is group H).
- `bridge.harness.jobs(): Promise<JobInfo[]>` (`harness:jobs`): running + the last 8 jobs with a 64 KB output tail.
  `harness.job(kind)` returns the RUNNING job's id when one of the same kind is running (one per kind; an upgrade and a
  serve-stop can still overlap, the renderer confirm should check `app:busy().jobs`).
- `update:install` now resolves after the shutdown (it used to return at once); the app quits right after.

## Done (unit tests: 21/21 pass in my runner, `node --test` on the esbuild bundles of the 6 files below)
- H1 (`backend/manager.ts`): `start()` single-flight; restart / set-root / create-env go through `fresh()` (gen++, await the
  in-flight start, start again); `spawnServe` adopts an already-spawned child; every spawned child tracked in `children` until
  it exits, `onQuit`/`stopServe` stop all of them. Tests: retry-during-start = one serve, owned, killed on quit;
  create-env mid-start adopts the serve (manager.test.ts, fake harness from e2e/fake-harness).
- L1/L2: serve stdout/stderr go to `<root>/logs/serve.log` via a file fd (no pipe to a dead app), log rotated at 5 MB, mode 600;
  stop = SIGTERM each owned process group first, then `harness serve stop` capped at 5 s (was 20 s). keep / keep-if-busy also
  stop respawn and reprobe timers and unref the children.
- L3: port held by another install -> probe port+1..+10 for a serve of this state id and attach (no second spawn). Test.
- L15: child 'error' handled (spawn ENOENT -> phase down "could not start harness serve: ..."), retry works. Test.
- L11: create-env chmods `.env` 0600. Test.
- L8 (main half): `consent-host` awaits a native confirm (`confirmHost` dep, dialog in index.ts; e2e auto-yes, recorded in
  `__harnessE2E.record.dialogs`). Not unit-tested (the dialog); the manager path is a plain await.
- H3 main half + L4 (`pty/client.ts`, `pty/host.ts`): `pty:kill` deletes the record; exited / host-crashed terminals reaped after
  60 s; host kill = SIGHUP then SIGKILL after 3 s; replay ring is a chunk list flattened on attach. `liveCount()`. Tests:
  client.test.ts (kill + reap, host crash reap, 256 KiB ring).
- H2 main half (`updater.ts`, `index.ts`): `install()` is async and once-only: `beforeInstall` = shared `shutdown()` (quitting,
  flush state + settings, `backend.onQuit()` + `pty.shutdown()` raced at 8 s) then `app.releaseSingleInstanceLock()`, then
  `quitAndInstall(false, true)`; before-quit sees `quitting` and lets the quit through. A refused install (electron-updater emits
  'error' synchronously) relaunches the current version. Tests: updater.test.ts (order, single call, refusal path) with a fake
  autoUpdater injected via the new `au` dep.
- M10: `updates.auto` false = no background checks and `autoDownload=false`; re-applied on every settings change (`setAuto`).
  Test.
- M3 (`cli.ts` `Jobs`, wired in index.ts): one running job per kind, ring of 8, 64 KB tail, `harness:jobs`; check-updates parse
  moved to `done`. Test (jobs.test.ts).
- L9 (`cli.ts`): `runHarness` spawns detached, a timeout SIGTERMs the group, SIGKILLs after 2 s, and resolves 200 ms later even
  if 'close' never fires. Test with a grandchild that ignores SIGTERM and holds the pipe: resolves timedOut, grandchild gone.
- M2 / F1 / F2: async `shell:path-info` (`pathinfo.ts`). Test (pathinfo.test.ts): plain folder, no-commit repo, detached,
  subfolder, symlink -> realpath, file, missing path.
- L6: second instance `app.exit(0)` before loading settings/state; `whenReady` body returns early. Not unit-testable (top level
  of index.ts); typechecked only.
- L7: default session denies every permission except `clipboard-sanitized-write` (used by the terminal / copy buttons); the
  web partition is set up at ready instead of on the first `web:url`. Typechecked only.
- M5: `render-process-gone` reloads `app://harness/index.html` unless `clean-exit`, at most 3 per minute, then a static
  error page (`window.ts` `crashReload` / `CRASH_PAGE`). Typechecked only (window.ts imports electron, so no unit test).
- L12: `powerMonitor` 'resume' and 'unlock-screen' call `backend.poller.kick(0)`. For W1-B: this is the only hook; if
  `poller.kick` is renamed, update index.ts.
- L16: `settings:set` applies + broadcasts at once; the file write is debounced 400 ms and flushed in `shutdown()`.
- architecture/desktop.md updated (security, backend manager, terminals, updates, where state lives).
- tsc: `npx tsc --noEmit -p tsconfig.json` clean (0 errors, whole project) at 08:13 UTC.

## Not verified
- I did not run the new tests against the old code to see them fail first.
- L6, L7, M5, L12, L16 and the index.ts wiring of H2 have no unit test; e2e result below is the only runtime check.
- electron-updater: `quitAndInstall` sets `quitAndInstallCalled`, so its own 'quit' handler does not install twice (read,
  not run). Separately: index.ts used to `app.exit(0)` from before-quit, which skips the 'quit' event, so
  `autoInstallOnAppQuit` likely never worked; still true for a plain quit (unchanged, out of scope).
- `logTail` is now refreshed only while starting and on exit (from serve.log), not continuously while up.

## Skipped / remaining
- L5 main half: nothing to change. `window:action 'close'` already sets `closeAllowed` and closes, and renderer/main.tsx:37 already
  acks after the flush; the 1.5 s timer stays as the fallback for a hung renderer.
- `startJob` (the streaming runner used by Jobs) is not spawned detached; only `runHarness` got the group kill (L9 scope).
- M3 "no queue guard" for upgrade / serve-stop: main exposes `app:busy().queueRunning`; the refusal/confirm is the renderer's.

## e2e
- `flock e2e.lock nice -n 19 timeout 900 node scripts/e2e.mjs e2e/platform/*.spec.ts` (one run, 08:15-08:17 UTC): 17 passed (1.9 m),
  rc 0. Log: scratchpad/w1a-e2e.log. After it, the only electron/Xvfb processes on the host belonged to another leg's
  `e2e/bg/engine.spec.ts` run, which took the lock after mine.
