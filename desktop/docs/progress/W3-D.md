# W3-D: review 2 fixes (backend client, keys, jobs, notifications, serve/api.py poll cost)

Source: scratchpad review2-desktop.md (+ review2-serve-api.md for S1-S4). Owned: src/main/** except
settings.ts, src/renderer/views/keys/**, src/renderer/core/notify.ts, serve/api.py, serve/test_api.py.

## Done (each verified against the code first)

- A1 (High) `backend/manager.ts`, `backend/http.ts`: re-login is single-flight (`reloginP`, shared by polls and
  `ServeClient.get`); a code serve refused (`failedCode`) is never posted again until the saved code changes;
  a 401 with a failed re-login in `pollFetch` goes to `needs-login` and stops the poller; `attach` no longer
  re-logins a second time after `client.get` already did. New `ServeClient.loginStatus()` tells 'bad' from
  'limited' (HTTP 429); the login / create-code actions say "too many failed sign-ins ... try again later"
  on 429. Tests: `backend/auth.test.ts` (code removed while up: exactly one re-login post, needs-login, no
  more posts from GETs/Retry/1.2 s of polling, typed new code works; 5 parallel 401s = 1 post; 429 message).
- A2 `backend/http.ts`: one wall-clock deadline per request (connect included), settle-once, `res`/`req`
  `close` handling, response cap (`maxBytes`, default 64 MiB; `/healthz` 64 KiB, `/login` 1 MiB). Tests in
  `auth.test.ts`: never-accepted connect bounded at 1 s; cap refused; cut body settles.
- A3 `manager.ts`: `ownedPort` kept from spawn (cleared whenever state sets `owned: false`), `ownsPort` uses
  it. Test `manager.test.ts` "A3": child handle dropped, Retry still attaches `owned: true`.
- K1 `keys/vault.ts`: `probeActive(kind, record?)`; `activate` passes `null` and stores the result on the
  candidate. Test "K1" (fails without the fix: the restored key showed `rejected`).
- K2 `vault.ts`: `serial()` promise chain; every `keys:*` IPC handler goes through it (internal calls such
  as add -> activate stay direct, no deadlock). Keys view: `keyOpsBusy` signal, Test / Make active / Remove
  disabled while an op runs. Test "K2" through the registered handlers (fails without the fix: events
  interleave and vault != .env).
- K3 `vault.ts`: `stdinSupported(root)` (memo by mtime): a script with `_config_set()` and no `--stdin` is
  refused with "too old ... update harness first; .env was not changed" before anything is written;
  injectable `stdinOk` dep. Test "K3".
- K4 `keys/probe.ts`: settle-once, wall-clock deadline, `res` error/aborted/close and `req` close resolve
  `unknown`. Test "K4" (cut answer, never-accepted connect).
- K6 `vault.ts`: `setStoreInactive(false)` nulls `enc` only after the secret was decrypted; after a switch the
  previous key's `enc` is dropped only on a backend known to have no keyring (`basic_text` / `none`), not when
  the keyring is just locked or not started. Test "K6" (fails without the fix).
- J1 `cli.ts`, `index.ts`: per-kind job timeout (`JOB_TIMEOUT_MS`), group kill (`startJob` is detached and
  returns a group-kill handle), forced end after a grace if the process never closes, `Jobs.stopAll()` in
  `shutdown()`, check-updates refused while upgrade runs. Test `jobs.test.ts` "J1".
- N1 `renderer/core/notify.ts`: notes over the limit are held; a timer at `RateLimit.waitMs()` shows them as
  one summary (`summary()`); dropped if the window is focused by then; disposer cancels the timer. Tests in
  new `renderer/core/notify-held.test.ts`.
- A4 `manager.ts`: `stopServe` returns whether it stopped anything; restart and set-root then `waitDown()` (poll
  `/healthz` every 200 ms, at most 12 s) so the next start does not attach to the old serve as not-owned.
  Test `auth.test.ts` "A4" (late exit waited for; never-exiting serve bounded; nothing stopped = no wait).
- A5 `manager.ts`: `fresh()` clears `skipProbe` (restart / set-root / create-env probe the key again). The
  "after keys:activate" half is not done (needs a vault -> backend hook). Test `manager.test.ts` "A5"
  (fails without the fix).
- K5 (half) `vault.ts`: a keys.json entry that is not an object with string `id`/`kind` is dropped on load
  instead of throwing at module load. Test "K5" (fails without). Running the legacy migration after
  `whenReady` is not done.
- K7 (first point) `vault.ts`: `runHarnessStdin` spawns detached and kills the group on timeout (`killGroup`
  now exported from `cli.ts`). Test "K7" (fails without: the forked grandchild holds the pipes 30 s).
- N2 `index.ts`: Notifications are held in a Set (max 20) until click/close; the e2e/unsupported record is
  capped at 200. No unit test (index.ts is the Electron entry; GC behaviour not reproducible in node:test).
- N3 `updater.ts`: `check()` while `ready` or `downloading` returns the state unchanged. Test
  `updater.test.ts` "N3" (fails without: the phase goes to `available`).
- P1 `pty/client.ts`: a `spawned` answer for an id nobody waits on (and no record) is killed in the host.
  Test `client.test.ts` "P1".
- serve/api.py (fork, verified by re-running both suites): S1 in-place ticket index built once per request
  (`Ctx.ticket_of_row`, `_Indexed` for ticket:K); S2 `repo_facts` kept 10 s with refresh in background,
  ingest summary kept by stat stamp; S3 debug reports kept by stat stamp (running log at most every 2 s);
  S4 `ticket_state` kept per ticket by stat stamp (+ runner signature for a running one); S-L2 the API's
  `why` says "still stopping" for a stuck stop. Payload shapes unchanged. Reported timings: sessions
  710 -> 11 ms (3000 sessions / 600 tickets), ticket:K 1100 -> 13 ms, debug 321 -> 4 ms (200 runs), queue
  77 -> 49 ms warm. 6 new tests (`PollCostTest`), all failing against HEAD api.py.
- architecture/desktop.md: access-code/deadline/ownership paragraph, restart waits for the old serve, key
  re-probe on restart, vault queue + probe + `--stdin` check, job timeouts, check-while-ready, held
  notifications. architecture/harness-cli.md (fork): one paragraph on the poll caches under JSON API -> Poll.

## Skipped / remaining

- V1 (queue list scroll on reorder): W3-B's files (`views/queue/virtual.tsx`, `QueueView.tsx`). Remaining.
- jobs:cancel IPC (J1 "cancel"): needs ipc/preload + Updates view (not mine); timeout + quit-stop cover the
  dead-feature case. Remaining.
- A6 (Windows normcase in stateId), K7 (proxy support, `add` with a rejected activate keeping the entry), P2 (exited-terminal
  placeholder): Low, remaining.
- A7: partly wrong. A second "Create code" does not mint another code: pub.py `add_code` refuses an existing
  name ("already has a code (remove-code it first)"), so codes do not pile up; the real effect is that the
  second click fails with that message. Fix (remove then add, keep the old code if add fails) remaining;
  the `mem` cleanup in `saveAccessCode` too. The GET `unknown-result` label nit: not changed.
- serve S4 dropping `actions` from archived tickets, S2 `%ar` dates, S2 dropping `beat` from the ingest run:
  each changes the payload shape (or needs vault_view.py/pub.py); skipped by the brief's "keep payload
  shapes identical". runs.py `fix_refused` still calls `ticket_state` per ticket (W3-C's file).

## Test results

- `npx tsc --noEmit -p tsconfig.json`: clean.
- `node scripts/run-tests.mjs`: 258 tests, 258 pass, 0 fail (251 before the late A4/A5/K5/K6/K7/N3/P1 tests).
- Negative checks (test run against the code without the fix, then restored): K1, K2, A5, K5, K6, K7 fail
  without; serve `PollCostTest` (6) fail against HEAD api.py (fork's check).
- `python3 serve/test_api.py`: 48 tests OK. `python3 serve/test_pub.py` (pub imports api): 53 tests OK.
- e2e run 1 (backend, keys, security, boot specs; before the late fixes): 12 passed.
- e2e run 2 (backend, keys, pty, update, security, boot specs; all fixes in, the build ran after the last
  edit since it waits on the e2e lock): 16 passed (2.3 min), exit 0.

## Contract changes to shared files

None (no edits to src/shared/*, src/preload/*).
