# Desktop app (`desktop/`)

An Electron app that covers what the `harness serve` web UI does, plus native
terminals, a key vault, and self-update. The full design is
`desktop/docs/PLAN.md`; this file is the structural summary.

## Processes

```
main (Node 24, out/main/index.cjs)
 ├─ BrowserWindow: renderer at app://harness/index.html (sandboxed, contextIsolation, no node)
 │    └─ preload (out/preload/index.cjs): the one `window.harness` Bridge (src/shared/ipc.ts)
 ├─ utilityProcess: PTY host (out/main/pty-host.cjs, node-pty)
 ├─ child: `bash <root>/harness serve --port N` (only when the app owns the backend)
 └─ child: `bash <root>/harness <cmd>` (CLI runner: config, unlock, check-updates, upgrade, doctor)
```

- Windows: `bash` is Git Bash's `Git\bin\bash.exe`, resolved once by
  `src/main/bash.ts`. It checks `HARNESS_DESKTOP_BASH`, then Program Files,
  then per-user and scoop installs, then a `git.exe` on PATH. It never uses
  WSL's `System32\bash.exe`, which is what `bash` on a shortcut's PATH
  usually is.
  - Children start with `windowsHide` and are stopped with `taskkill /T`,
    since Windows has no process groups.
  - The plain terminal is `bash --login -i`.
  - `stateId` applies `ntpath.normcase` (lowercase, backslashes) so that it
    matches `pub.py`.
  - `app.setAppUserModelId('dev.harness.desktop')` matches the NSIS
    shortcut, so a pinned taskbar icon groups the running window.
- The renderer does no network I/O. Main talks to serve over `node:http` on
  loopback, so requests carry no Origin/Referer/Sec-Fetch headers. Serve's
  session cookie is kept in main's memory only.
- The web panels (`<webview>`, partition `persist:harness-web`) show serve or
  opencode pages. Main strips any preload, forces the secure prefs, refuses
  sources outside the serve/opencode origins, and supplies opencode's Basic
  auth through `app.on('login')`.

## Security model

- `app://harness/` is a privileged standard scheme served from `out/renderer`
  with a strict CSP (`script-src 'self'`, `connect-src 'self'`). Paths are
  confined to that folder.
- Every IPC handler checks that the sender is `app://harness/` and validates
  the payload. API paths must start with `/api/v1/` and contain no traversal.
- Top-level navigation, redirects and `window.open` are refused. External
  links open in the OS browser, and only for http, https and mailto.
- Permission requests are denied on both sessions except
  `clipboard-sanitized-write`; both handlers are installed at ready.
- A second instance exits at once (`app.exit`) before loading any store.
- A crashed renderer is reloaded (not on `clean-exit`), at most 3 times a
  minute; then main shows a static error page. PTYs survive a reload.
- `consent-host` asks a native confirm in main before it persists.
- The packaged app turns off the Electron fuses for runAsNode, NODE_OPTIONS and
  inspect, and only loads the app from the asar (`electron-builder.yml`).

## Backend manager (`src/main/backend/`)

Phases: `locating -> no-install | needs-config | starting -> up`, plus
`reconnecting`, `down`, `needs-login` and `incompatible`.

- Locate: `HARNESS_DESKTOP_ROOT`, then settings, then the repo around the app,
  then `harness` on PATH. A root needs an executable `harness` and
  `serve/pub.py`.
- Before starting serve, main checks `.env` and runs `config list --tsv`. If
  required keys are empty the phase is needs-config, which the app fixes
  without serve.
- Attach first: `/healthz` must answer `X-Harness-State` equal to
  `sha256(realpath(root/state))[:16]` (pub.py `state_id`), then
  `/api/v1/status` with `api == 1`. A 404 there means a serve older than the
  app API.
- Otherwise main spawns serve in its own process group. Failures are
  classified from the output (needs-config, key-locked, key-rejected, port in
  use, python missing). A locked key restarts serve with
  `HARNESS_SKIP_AUTH_PROBE=1`; a restart, set-root or create-env probes the
  key again. Respawns back off 1, 5 and 15 s, at most 3 in 5 minutes.
- `start()` is single-flight: a retry, restart, set-root or create-env during
  a start joins it or adopts the serve already spawned. Every spawned serve
  is tracked until it exits, so quit can stop it.
- Serve writes straight to `<root>/logs/serve.log` (a file fd, not a pipe;
  rotated to `serve.log.1` above 5 MB), so a busy serve never blocks on main.
  Main tails the file while starting to classify failures.
- When the configured port is held by another install, main first probes the
  next 10 ports for a serve of this install and attaches to it, then picks a
  free port.
- `HARNESS_DESKTOP_SERVE_URL` sets attach-only mode, which never spawns serve.
- Access codes: on a 401, polls and GETs share one re-login (single-flight).
  A code serve refuses is never posted again until the saved code changes,
  and the phase becomes `needs-login` (polling stops), so the app cannot run
  serve's sign-in limiter (10 failures in 10 min, then 429) into a block. A
  429 to a typed code says sign-in is blocked, not that the code is wrong.
- Every request to serve has a wall-clock deadline (connect included) and a
  64 MiB response cap (64 KiB for `/healthz`). Ownership is kept by port, so
  a serve this app started stays owned after a respawn attaches to it.
- Live data comes from one multiplexed conditional poll
  (`/api/v1/poll?t=topic~sig`) of the topics visible panels subscribe to.
  Cadence is 1 s when busy (2 s when busy and blurred), 3 s when focused, 5 s
  when blurred, and 15 s when hidden (status and queue only). After a failure
  it backs off 1, 2, 5 and 15 s; `poller.pollNow()` skips the wait. More than
  24 topics are split over several requests. Main sends a topic to the
  renderer only when it changes (a topic-set change re-sends only topics new
  to that window), and forwards a topic error once; the renderer store keeps
  data identity for a same-sig re-send, so it does not re-render.
- The queue list renders only the rows in view above 200 tickets; the
  transcript renders the newest 200 messages, with "Show earlier".
- Quit policy: keep-if-busy (default), stop, or keep. An attached serve that
  the app did not start is never stopped. Stop sends SIGTERM to each owned
  serve's process group, then runs `harness serve stop` (5 s cap). Restart
  and set-root then wait (up to 12 s) until the old port stops answering, so
  the new start does not attach to the old serve as not-owned.

## Terminals (`src/main/pty/`)

- Spec kinds:
  - `plain`: `$SHELL`
  - `agent`: `harness` with flags
  - `host`: `harness host`, with no `--net`
  - `shell`: `harness shell`
- Resume uses `--continue` or `--session <id>`, which both CLI paths pass
  through to opencode.
- The host coalesces output to one message per 8 ms. It pauses a PTY above
  1 MiB of output the renderer has not acknowledged and resumes below
  256 KiB.
- Main keeps a 256 KiB replay ring per terminal for re-attach (a chunk list,
  flattened on attach). If the host dies, every terminal is marked exited.
- `pty:kill` drops the terminal's record at once; the host sends SIGHUP, then
  SIGKILL after 3 s. An exited terminal stays in `pty:list` and attachable for
  60 s, then is dropped.
- A terminal panel removed without its close confirm (layout undo or reset)
  kills its PTY unless the new layout re-attaches to it. At renderer start,
  PTYs from an earlier page that no restored panel claims are killed.

## Keys (`src/main/keys/`)

- Metadata and secrets live in `userData/keys.json` (mode 600). Secrets are
  encrypted with `safeStorage` when the OS keyring is real.
- Without a keyring (`basic_text`) the app keeps no inactive secrets
  (single-key mode). `.env` then holds the only copy of the key. The owner can
  opt in to keeping them anyway (`storeInactive`, `keys:set-store-inactive`);
  turning it off drops the stored inactive secrets from disk (they stay in
  memory for the run).
- Making a key active:
  1. probe the candidate directly (`POST <base>/v1/chat/completions` with
     `max_tokens:1`)
  2. write it with `harness config set KIND --stdin` (the secret goes on
     stdin, never on argv where `ps` shows it)
  3. check it with `harness unlock`
  4. if upstream rejects it, go back to the previous key automatically
- Vault operations run one at a time in main (IPC handlers share one queue);
  the Keys view disables its row buttons while one runs. The direct probe
  always settles (deadline, and a cut-short answer is `unknown`). A harness
  whose `config set` has no `--stdin` is refused before `.env` is touched.
- Secrets never reach the renderer or the logs. The renderer gets `chars` (the
  length) and `last4` only for secrets of 16+ characters (a short PIN or
  access code would be mostly given away). A key locks 14 days after the
  later of its creation and its last unlock.

## Updates

- The app updates through electron-updater with the generic provider, from
  the rolling `desktop-latest` release. `HARNESS_DESKTOP_UPDATE_URL`
  overrides the feed.
- The phase is `unsupported`, with the reason, when:
  - updates are off
  - the app is a dev build
  - on Linux, the app is not an AppImage
- `settings.updates.auto` turns both the background checks (30 s after start,
  then every 6 h) and auto-download on or off; a manual check always works,
  except that a check while an update is downloading or ready does nothing
  (the ready update stays, and "Update ready" is not notified again).
- Restart to update first runs the quit shutdown (state and settings flushed,
  serve per the quit policy, PTYs killed, 8 s cap) and releases the
  single-instance lock, then calls `quitAndInstall`. The AppImage starts the
  new version at once, so the lock must already be free. If the install is
  refused after that, the app relaunches the current version.
- Harness itself updates through the CLI runner: `harness check-updates`, then
  `harness upgrade --no-prompt`. One job of each kind runs at a time (a second
  request gets the running job's id); the last 8 jobs keep a 64 KB output tail
  (`harness:jobs`). CLI children run in their own process group, and a
  timeout kills the whole group. Jobs time out too (check-updates 2 min,
  doctor 5 min, upgrade 15 min, serve-stop 1 min), are stopped on quit, and a
  check-updates during an upgrade is refused.
- The Updates view confirms Restart to update and Upgrade first, listing what
  they interrupt (`app:busy`: open terminals, a running queue run, running
  jobs). Opening the view re-attaches to a running upgrade / check job
  (`harness:jobs`) instead of starting a second one.

## Notifications (`src/renderer/core/notify.ts`)

- The renderer diffs the `queue` topic and the app update phase and shows a
  native notification (`notify:show`) only while the window is unfocused or
  hidden: a ticket needs the owner (questions, plan review, blocked), a
  ticket is done, the queue run ended, an update is ready.
- Per-kind toggles in `settings.notifications`. At most 4 per minute; several
  events of one kind in one poll collapse into one. Events over the limit are
  held and shown as one summary when the window frees (dropped if the window
  gets focus first). A click routes to the
  ticket panel or the view (`notify:click`).

## Background and glass (`src/background/`, `src/renderer/core/background.ts`)

- The background engine draws into `#bg-root` (`z-index:-1`) behind the
  whole UI. Only `<html>` paints `--bg-window`; `body` must stay transparent
  or it covers the canvas.
- Chrome (title bar, rail, sidebar, status bar) and each dock group paint ONE
  translucent layer of `--surface-0`. Tab strips and `.view` are transparent;
  cards, inputs, menus and terminals stay opaque.
- The alphas are computed (`views/background/surface.ts`): the lowest alpha
  at which every text colour used on glass (text-1/2/3, status `--*-text`,
  `--accent-11`, `--icon`) keeps 4.5:1 over every palette ramp stop and the
  ramp's per-channel max, scaled by `appearance.seeThrough` (0 = opaque).
  Effects must stay inside that bound (aurora soft-knees to it in the dark
  theme; no additive compositing). Written to `<html>` as
  `--panel-alpha-auto` / `--chrome-alpha-auto` whenever `bindAppearance`
  re-applies (`appearanceTick`): settings writes and OS theme /
  reduced-motion changes alike; the engine theme follows the same tick.
  Glass off, `prefers-contrast: more` and `prefers-reduced-transparency`
  force alpha 1 (the last two in `glass.css`, after `base.css`).
- `appearance.accent` is a preset name (`ACCENT_PRESETS` in
  `shared/ipc.ts`; `[data-accent]` in `tokens.css`). Any other stored value,
  e.g. an old hex, loads as `iris`.
- `background.onBattery: static` pauses the engine on battery power
  (`power:state`); the last frame stays.
- Low power (`src/background/gpu.ts`): `auto` turns on when the WebGL
  renderer is software (SwiftShader, llvmpipe, ...). Animated backgrounds run
  at 20 fps and half scale; tracers and grayscott draw their CSS variant. The
  per-device override (`auto` / `on` / `off`) is in localStorage
  `harness.bg.lowPower` and in the background picker.
- The governor steps down one rung (never up) on sustained worker frame cost
  or on main-thread jank, reported by the renderer about once a second over a
  2 s window: rAF-interval p95 above `max(34, 2 x display frame + 4)` ms
  (display frame = lowest 10th-percentile interval seen, so 30 Hz displays
  are not jank), or long tasks > 25 % of the window. The worker rung ignores
  long tasks and the frames that overlap them (app work, not the scene). No
  report counts during the first 5 s after engine start; a window resize
  restarts the window.
- A WebGL context loss that is not restored within 4 s (no new frame while
  visible) marks GL broken and falls to the next rung; so does a second loss
  within 60 s.

## Where state lives

- `userData/settings.json` and `userData/state.json`: written atomically,
  mode 600. A corrupt file is moved aside and defaults are loaded. Settings
  apply at once; the file write is debounced (400 ms) and flushed on quit.
- `userData/keys.json` and `userData/logs/app.log` (rotated at 5 MB).
- `<root>/logs/serve.log` (mode 600): output of an owned serve.
- `shell:path-info` is async and realpath'd. It returns `real` and `repo`
  (`isRepo`, `root`, `branch` (empty when detached), `detached`,
  `hasCommits`, `inSubfolder`) from one git probe; each fs or git step gives
  up after 1.5 s.
- Environment overrides are all `HARNESS_DESKTOP_*`:
  - `ROOT`, `SERVE_URL`, `SERVE_PORT`
  - `USER_DATA`
  - `UPDATE_URL`, `NO_UPDATE`
  - `DISABLE_GPU`, `LOG_LEVEL`
  - `E2E`

## Build and test

- Build and unit tests:
  - `node scripts/build.mjs`: esbuild targets main, pty-host, preload,
    renderer, bg-worker and bg-demo. It builds only when run directly, detected by comparing
    `pathToFileURL(realpath(argv[1]))` with `import.meta.url` (a hand-built `file://` + argv[1]
    never matches on Windows); `e2e.mjs` imports `build()` instead.
  - `npm test`: node:test over `src/**/*.test.ts`.
- `npm run e2e`: Playwright `_electron` under `xvfb-run`.
  - Flags: `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --no-sandbox`.
  - Runs against `e2e/mock-serve` and `e2e/fake-harness`. It never runs a
    real serve and never runs docker.
  - The mock serves topics per scenario plus canned GET routes
    (`data/w2f-views.mjs`, keyed by path and sorted query); a spec overrides
    one with `setRoute(key, data, status)`.
- `--smoke-test` is the packaged smoke. It loads the window, checks the
  bridge, echoes through a PTY and exits 0 or 1, and never starts a backend.
- Installing on this machine: `harness desktop install` builds this OS's
  target and installs it (see `harness-cli.md`):
  - Windows: per-user NSIS, with Start-menu and desktop shortcuts named Harness.
  - macOS: `~/Applications`, ad-hoc signed.
  - Linux: `~/.local/share` plus a `.desktop` entry.
  - `harness desktop` launches the app detached, and reinstalls first when
    `desktop/` changed.
- Icon: `build/icon.png` (1024px, a Lorenz attractor) comes from
  `scripts/gen-icon.py` via `rsvg-convert`. electron-builder derives the
  `.ico` and `.icns` from it.
- CI workflows ship as `desktop/ci/*.yml`, for the owner to move into
  `.github/workflows/`.
