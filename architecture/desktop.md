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
  `HARNESS_SKIP_AUTH_PROBE=1`. Respawns back off 1, 5 and 15 s, at most 3 in
  5 minutes.
- `HARNESS_DESKTOP_SERVE_URL` sets attach-only mode, which never spawns serve.
- Live data comes from one multiplexed conditional poll
  (`/api/v1/poll?t=topic~sig`) of the topics visible panels subscribe to.
  Cadence is 1 s when busy, 3 s when focused, 5 s when blurred, and 15 s when
  hidden (status and queue only). After a failure it backs off 1, 2, 5 and
  15 s. Main sends a topic to the renderer only when it changes.
- Quit policy: keep-if-busy (default), stop, or keep. An attached serve that
  the app did not start is never stopped.

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
- Main keeps a 256 KiB replay ring per terminal for re-attach. If the host
  dies, every terminal is marked exited.

## Keys (`src/main/keys/`)

- Metadata and secrets live in `userData/keys.json` (mode 600). Secrets are
  encrypted with `safeStorage` when the OS keyring is real.
- Without a keyring (`basic_text`) the app keeps no inactive secrets
  (single-key mode). `.env` then holds the only copy of the key.
- Making a key active:
  1. probe the candidate directly (`POST <base>/v1/chat/completions` with
     `max_tokens:1`)
  2. write it with `harness config set`
  3. check it with `harness unlock`
  4. if upstream rejects it, go back to the previous key automatically
- Secrets never reach the renderer or the logs. A key locks 14 days after the
  later of its creation and its last unlock.

## Updates

- The app updates through electron-updater with the generic provider, from
  the rolling `desktop-latest` release. `HARNESS_DESKTOP_UPDATE_URL`
  overrides the feed.
- The phase is `unsupported`, with the reason, when:
  - updates are off
  - the app is a dev build
  - on Linux, the app is not an AppImage
- Harness itself updates through the CLI runner: `harness check-updates`, then
  `harness upgrade --no-prompt`.

## Where state lives

- `userData/settings.json` and `userData/state.json`: written atomically,
  mode 600. A corrupt file is moved aside and defaults are loaded.
- `userData/keys.json` and `userData/logs/app.log` (rotated at 5 MB).
- `<root>/logs/serve.log`: output of an owned serve.
- Environment overrides are all `HARNESS_DESKTOP_*`:
  - `ROOT`, `SERVE_URL`, `SERVE_PORT`
  - `USER_DATA`
  - `UPDATE_URL`, `NO_UPDATE`
  - `DISABLE_GPU`, `LOG_LEVEL`
  - `E2E`

## Build and test

- Build and unit tests:
  - `node scripts/build.mjs`: esbuild targets main, pty-host, preload,
    renderer, bg-worker and bg-demo.
  - `npm test`: node:test over `src/**/*.test.ts`.
- `npm run e2e`: Playwright `_electron` under `xvfb-run`.
  - Flags: `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader --no-sandbox`.
  - Runs against `e2e/mock-serve` and `e2e/fake-harness`. It never runs a
    real serve and never runs docker.
- `--smoke-test` is the packaged smoke. It loads the window, checks the
  bridge, echoes through a PTY and exits 0 or 1, and never starts a backend.
- CI workflows ship as `desktop/ci/*.yml`, for the owner to move into
  `.github/workflows/`.
