# W1-C: keys, updates, notifications, add-tickets / new-session probe (review pass 1)

Status: DONE except where listed under Remaining (stop 08:42 UTC).

## Done (with test results)
- **M8 `harness config set KEY --stdin`** (harness `_config_set`): reads one line from stdin (hidden prompt on a tty),
  strips a trailing CR, refuses a multi-line value, empty stdin clears. The `KEY value...` form is unchanged.
  `bash -n harness` ok; `tests/unit_config_test.sh` new T18 (stdin set, CRLF, multi-line refusal, empty clears,
  QUEUE_PARALLEL validated via stdin, argv two-word form) -> CONFIG TEST PASSED. `check_runtime_calls.sh` ok.
  architecture/harness-cli.md updated (ps note + config set bullet).
- **M8 vault uses it**: `src/main/keys/vault.ts` `writeEnv` -> `runHarnessStdin(root, ['config','set',KIND,'--stdin'], secret)`
  (local spawn with piped stdin, timeout, output cap). Clearing still sends '' on argv (not secret).
- **M9 storeInactive setter end to end**: `KeyVault.setStoreInactive(on)` + `keys:set-store-inactive` handler;
  off drops stored inactive secrets from disk (kept in memory for the run). KeysView: checkbox
  `keys-store-inactive` when the keyring is not secure (replaces the wrong "store is inactive" notice); AddKey shows
  `add-key-inactive-hint` when an inactive save would be refused.
- **L10**: `last4` only for secrets of 16+ chars (`LAST4_MIN`), `chars` always set; keys.json migration on load
  recomputes last4/chars from the decrypted secret (cleared if unknown). KeysView shows `••••last4` or `N chars`.
  `src/main/keys/vault.test.ts` 5/5 pass (stdin on activate, real-spawn stdin, last4Of, migration, M9 on/off).
- **H2 renderer half + W15**: Updates view confirms Restart to update and Upgrade with the consequences
  (`views/updates/consequences.ts`: terminals closed, queue run stops/keeps running, running jobs, or
  "Could not check"). Uses `bridge.app.busy()`, falling back to `pty.list` + `GET /queue` if the handler is missing.
  `consequences.test.ts` 2/2 pass.
- **M3 renderer half**: Updates view re-attaches on mount to a running upgrade / check-updates job via
  `bridge.harness.jobs()` and follows `job:output`/`job:exit`; `run()` follows the already-running job's id instead
  of starting a second one. Guarded (catch) when `harness:jobs` is absent.
- **M10 UI**: `update-auto` checkbox in the Updates view (`patchSettings({updates:{auto}})`), hidden when unsupported.
- **M4/W22 notifications**: new `src/renderer/core/notify.ts` (queue diff: needs-answers/plan-review/blocked ->
  needsInput, done -> ticketDone, runner running->stopped -> runEnded; update phase -> ready). Only while the window
  is unfocused or hidden; per-kind `settings.notifications` toggles (existing Settings UI); RateLimit 4/min; several
  of one kind collapse. Wired in main.tsx; `notify:click` opens the ticket panel or view. main.tsx keeps the
  `queue` topic subscribed while any queue notification kind is on. `notify.test.ts` 5/5 pass.
  ("failed" in the queue vocabulary is `blocked`, which is reported as needs-you.)
- **L5**: `app:will-quit` saves layout + state then acks with `bridge.window.action('close')`.
- **W5 steps 3-4 / F3 / F1 UI half (AddTicketsDialog)**: debounced project probe via `shell.pathInfo` (W1-A's
  `repo: GitProbe`, legacy `git` fallback): `add-project-info` shows the case (not found / not a folder / not git
  (+error) / empty repo / detached HEAD / subfolder with "use the repo's top folder"); a not-found or relative
  project is refused inline on submit. Loads `GET /queue/branches` + `/queue/repos` (`?project=`) when online:
  an existing branch named after a single ticket key (local first, then remote) is announced (`add-existing-branch`)
  and preselects Continue with that branch until the owner picks a start; branch datalist on Continue from; per-repo
  mode selects (`add-repos`, "On a work branch" / "Left uncommitted", defaults as the web form) when the tree has
  more than one repo, sent as `repo_modes`. NewSessionDialog shows the same probe text (`gitCase`), with
  "use the repo's top folder" for a subfolder. `views/session/model.ts` `gitCase` + `ticketBranch`;
  `views/session/model.test.ts` 7/7 pass.
- architecture/desktop.md: Keys (stdin, storeInactive, L10), Updates (confirms, re-attach), new Notifications section.
- `npx tsc --noEmit -p tsconfig.json`: 0 errors (08:01).
- e2e (views + keys + update specs, once via the lock): see "e2e" below.

## Contract changes (additive)
- `src/shared/ipc.ts`: `KeyMeta.chars?: number`; `keys.setStoreInactive(on: boolean): Promise<KeyOpResult>`.
- `src/preload/index.ts`: `keys.setStoreInactive` -> channel `keys:set-store-inactive` `{ on }`.
- Consumes W1-A's `app.busy`, `harness.jobs`, `PathInfo.repo` (all guarded if the handler is missing).

## Edits outside my file list (needed for M8; please review)
- `desktop/e2e/fake-harness/fake-harness.mjs`: `config set KEY --stdin` reads the value from stdin; the call log
  does not mask the literal `--stdin`.
- `desktop/e2e/platform/keys.spec.ts`: expects `config set PROXY_API_KEY --stdin` and that the call log never holds
  the secret; when not secure, turns storeInactive on and adds an inactive key (last4 '' / chars 10).

## Skipped / remaining
- Key-lock notifications (`settings.notifications.key`): not in my item list; not wired.
- `runHarnessStdin` lives in vault.ts because `cli.ts runHarness` (W1-A) has stdin 'ignore'. Could fold into
  `runHarness` as an `input` option later.
- No e2e for the add-tickets probe / repo modes (mock serve has no `/queue/branches` / `/queue/repos` fixtures);
  covered by the pure-helper unit tests only.
