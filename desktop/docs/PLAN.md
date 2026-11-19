# Harness desktop app: build plan

Status: plan, 2026-11-19. Inputs: the serve inventory and design research in the session
scratchpad (referred to below as **INV** and **DR**; DR section letters (a)-(h) are cited
instead of repeating tokens), `architecture/README.md`, `architecture/harness-cli.md`
("serve"), `architecture/upstream-api.md` (key lifecycle).

Facts checked while writing this plan (not in the inputs):
- Electron 45.4.1 bundles Node 24.22.3, Chromium 156, module ABI 150 (releases.electronjs.org).
- `node-pty@1.1.0` depends on `node-addon-api` (N-API), installs with `prebuild.js || node-gyp rebuild`.
- `@preact/signals@2.11.4` peer range is `preact >=10.25.0 || >=11.0.0-0`; `preact@11.2.0` is `latest`.
- `harness downgrade` picks its target with `git describe --tags` and **no `--match`**
  (`_downgrade_target_tag`), and the repo's release tags are `v0.1`, `v1.0`, `v1.1`.
  Any tag reachable from the branch therefore counts as a harness release (drives D10).
- `.env` is `source`d by the CLI (harness:194), so a process env var cannot override a key in it.
- `harness serve code add NAME` prints only the new code on stdout (pub.py `codes_main`).
- `harness config set` fails when `.env` is absent (`_config_write_key`), and
  `host_require_config` aborts `harness serve` when `.env` or a required value is missing.
- The build host has Python 3.9.25: `serve/api.py` must run on 3.9 (no `match`, no `X | Y` annotations evaluated at runtime).

Unverified items are marked **[U]** with the fallback already chosen.

---

## 1. Decisions

| # | Decision | Rationale (one line) |
|---|---|---|
| D1 | **Electron 45.4.1**, app in new top-level `desktop/`. | No Rust on the build host (no Tauri); one runtime gives node-pty, `<webview>`, electron-updater. |
| D2 | Processes: **main** (window, IPC, backend manager, HTTP client, poller, keys, updater), **sandboxed renderer**, **`pty-host` utilityProcess**, **background worker**. | A node-pty crash or output storm must not stall the window or main; background must not share the UI thread. |
| D3 | Renderer: **Preact 11.2.0 + @preact/signals 2.11.4**, TSX. | ~14 stateful views with forms that must survive 1 Hz live updates; signals update only what changed. Not React (weight, DR(f) verdict); not vanilla (we would re-create the web UI's `hSwap` form-preservation machinery by hand). dockview/xterm stay vanilla and mount into refs. |
| D4 | Bundler: **esbuild 0.28.2 only**; `tsc --noEmit` for types. **Vite is not used.** | Four targets (main, preload, renderer, worker), one tool, sub-second builds, no dev server so the CSP never needs `ws:`/`unsafe-eval`. |
| D5 | Docking: **dockview-core 8.9.1** (library, not custom). | Drag-to-dock overlays, floating groups and `toJSON/fromJSON` are where the weeks go (DR(f)); MIT, zero deps. Enterprise-only bits (overflow dropdown, keyboard group focus) are built small on `DockviewApi`. **No popout windows in v1** (xterm WebGL context is document-bound: DR(f) risk 1). |
| D6 | **The renderer never does network I/O.** Every serve call goes renderer -> preload bridge -> main -> `node:http` to loopback. | `origin_ok` refuses any Origin that is not serve's own, and an `app://` renderer always sends one; main with `node:http` sends no Origin/Referer/Sec-Fetch-Site, which `origin_ok` accepts. One place for cookies, auth and retries; renderer CSP is `connect-src 'self'`. Do **not** use `fetch`/`net.fetch` in main for serve (they add fetch-metadata headers). |
| D7 | Live updates: **one multiplexed conditional poll** (`GET /api/v1/poll?t=topic~sig...`), driven by main, pushed to the renderer over IPC only on change. **Not SSE.** | The data sources are files and a read-only SQLite DB with no change notification, so the server would poll anyway; stdlib `ThreadingHTTPServer` plus serve's `USR1` re-exec make long-lived streams the fragile part; `Runs.memo` (mtime cache) makes a 1 Hz poll nearly free; it is the same `sig` semantics the web UI already relies on. |
| D8 | Sessions are dock panels of three kinds: **terminal** (node-pty running `harness` / `harness host` / `harness shell` / a shell in a chosen folder, xterm.js 6), **chat** (`<webview>` on opencode web), **transcript** (native read-only view of any recorded session, with a composer when the run is live). | The opencode TUI needs a real TTY (INV 6); opencode web is already the serve chat; headless runs can only be messaged through the inbox, which the transcript composer uses. |
| D9 | Parity safety net: a **Web panel** (`<webview>`, themed with `insertCSS`) can open any serve page; every native view has "Open web version". | Guarantees "at least everything the web page has" even where a native twin is thinner (see parity matrix 3.16). `<webview>` rather than `WebContentsView` because it lives in the DOM: docking, z-order under the palette and drag overlays all just work. |
| D10 | Updates, two streams. **App**: electron-updater 6.8.11 with the **generic provider** at `https://github.com/HandelSim/harness/releases/download/desktop-latest/`, tags created on an **orphan commit**. **Harness install**: `harness check-updates` + `harness upgrade --no-prompt` through main's CLI runner. | Overrides the "GitHub provider" prior: its "latest release" lookup breaks as soon as a non-desktop release exists in the repo, and a desktop tag reachable from `dev`/`main` would become a `harness downgrade` target (verified above). A rolling `desktop-latest` release on an orphan commit avoids both without touching `harness`. |
| D11 | Keys: secrets live in an app **key vault** (Electron `safeStorage`, `userData/keys.json`, mode 600); the **active** key per kind is written to `.env` with `harness config set` from main; multi-key with lock countdown, expiry date, probe and one-click rotate/revert. | The upstream lifecycle (locks 14 days after creation/last unlock; fixed expiry; rotate = add, deploy, verify, revoke) makes a second stored key and a countdown genuinely useful. Writes go through the same CLI path serve's settings use (`save_config` shells out to `harness config set`), but from main, because key entry must work while serve cannot start (missing/locked key). |
| D12 | App state in `userData`: `settings.json`, `state.json` (window, dock layout, panel params, drafts, palette recency), `keys.json`, `logs/`. Atomic writes; a corrupt file is renamed aside and defaults load. | Restart restores everything; a bad file can never crash-loop the app. |
| D13 | Security: `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`; renderer served from a privileged `app://harness/` scheme with a strict CSP; typed preload bridge, every IPC handler checks the sender frame and validates its payload; backend URL must be `http://127.0.0.1:<port>` or `http://localhost:<port>`; the app binds no listening socket; `<webview>` locked down in `will-attach-webview`. Auth to serve reuses serve's own model unchanged (below). | Detailed in 1.1. |
| D14 | Backgrounds: separate engine (`src/background`), CSS layers for static ones, one `<canvas>` transferred to a module Worker (OffscreenCanvas; WebGL2 or 2D) for dynamic ones, fps-capped, self-throttling, paused/dimmed by one `setState`, reduced-motion aware, seeded PRNG. | DR(e), DR(h). Chrome panels get translucency without `backdrop-filter` because the background is rendered soft at low resolution (DR(c)). |
| D15 | Tests: Python `unittest` for `api.py`; `node:test` on esbuild-bundled `*.test.ts` for TS logic (no vitest dependency); Playwright 1.65.1 `_electron` under `xvfb-run` for e2e against a mock serve, plus a real `pub.py` on a temp state dir. | Uses only tools already verified on the host. |
| D16 | Out of v1: popout windows, tray icon, non-loopback backends, a `harness desktop` CLI subcommand, Lenia, key-vault sync. | Each is separable; none blocks parity. |

### 1.1 Security model in detail

- **Window**: `webPreferences { contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, webviewTag: true, navigateOnDragDrop: false, spellcheck: false }`. `will-navigate` and `setWindowOpenHandler` on the main window deny everything (external links go through `shell:open-external`, which accepts only `http:`, `https:`, `mailto:`).
- **Scheme**: `protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }])`; handler serves `out/renderer/**` only, path-normalised, no traversal. If module workers fail to load from `app://` **[U]**, fall back to `file://` with the same CSP via `<meta>`; L2 spikes this first.
- **CSP** (response header on every `app://` document): `default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. (`'unsafe-inline'` styles only: xterm and dockview inject `<style>`.)
- **IPC**: one `handle()` wrapper: rejects unless `event.senderFrame.url` starts with `app://harness/`; runs a hand-written validator per channel (no schema dependency); returns typed results, never throws raw errors across the bridge. `api:*` only forwards paths matching `^/api/v1/` plus the allowlist `^/vault/file/` (image bytes); the renderer cannot make main fetch an arbitrary URL.
- **PTY spawn**: the renderer sends a `PtySpec` (kind + validated flags + cwd), never a command line. Main builds argv from an allowlist (5.2). `cwd` must be an existing directory.
- **Webviews**: `will-attach-webview` deletes `preload`, forces `nodeIntegration=false, contextIsolation=true, sandbox=true`, and cancels unless `src` origin is the serve origin or the opencode origin. Partition `persist:harness-web`. In the guest: navigation outside those two origins is cancelled and offered to the OS browser; `setWindowOpenHandler` denies; permission requests are denied except clipboard write.
- **Auth to serve** (no new auth code in serve):
  - Access codes off (default): nothing to do.
  - Access codes on: `/api/v1/*` answers `401 {"ok":false,"error":{"code":"auth"}}`. The app shows "This backend asks for an access code" with two actions: **Create one for this app** (main runs `harness serve code add desktop-<hostname>`, reads the code from stdout) or **Enter a code**. Main then `POST /login` (form field `code`) over `node:http`, keeps the two cookies in memory, stores the code in the key vault (kind `ACCESS_CODE`), re-logs in on 401, and copies the cookies into the `persist:harness-web` partition so webviews are signed in.
  - opencode web (HTTP Basic `opencode:<password>`): main gets the password from `GET /api/v1/opencode` and answers Electron's `login` event for the opencode origin. The password never reaches the renderer.
- **CSRF on the API**: POST requires `origin_ok` (unchanged) **and** `Content-Type: application/json`; no CORS headers are ever sent.
- **Secrets**: never logged; never sent to the renderer after entry (only `label`, `kind`, `last4`, dates, probe state). `safeStorage` on Linux can fall back to `basic_text` (no keyring; this headless host): then the vault reports `secure:false`, the app stores **no** inactive secrets by default (single-key mode: the only copy is `.env`), and storing more needs an explicit opt-in toggle labelled "obfuscated, not encrypted".
- **Known, accepted**: `harness config set KEY VALUE` puts the secret on a process argv for the call's duration, exactly as serve's settings page does today. Follow-up (not in this build): a `--stdin` form of `config set`.
- **Packaging**: electron-builder `electronFuses` (`runAsNode:false`, `enableNodeCliInspectArguments:false`, `onlyLoadAppFromAsar:true`) **[U: confirm the key exists in 26.18.0; drop if not]**.

### 1.2 Backend manager (how the app gets a serve)

State machine (`BackendState.phase`, 5.2): `locating -> no-install | needs-config | starting -> up`, with `reconnecting`, `down`, `needs-login`, `incompatible`.

1. **Locate the install root** (first hit wins): `HARNESS_DESKTOP_ROOT` env; `settings.installRoot`; when unpackaged, the repo that contains `desktop/` (`../harness` exists); `harness` on `PATH` resolved through its wrapper/symlink; `~/harness`. Valid root = has an executable `harness` script and `serve/pub.py`. None: `no-install`.
2. **Config check**: `harness config list --tsv` (CLI runner). No `.env`: offer "Create .env from .env.example" (main copies the file; nothing else writes `.env` directly). Required values empty (same rule as `_openai_backend_missing`): `needs-config` with the missing keys listed.
3. **Attach or spawn** on `settings.backend.port` (default 8780; env `HARNESS_DESKTOP_SERVE_PORT`; `HARNESS_DESKTOP_SERVE_URL` forces attach-only and is what e2e uses):
   - `GET /healthz`. If it answers and `X-Harness-State` equals `sha256(realpath(<root>/state))[:16]`: `GET /api/v1/status`.
     - 200 and `mode:"serve"`: **attach** (`owned:false`).
     - 200 and `mode:"viewer"` (background viewer: no opencode web, no proxy URL): spawn serve; `harness serve` takes the viewer's port over by itself.
     - 404: a serve from before the API. Offer **Restart backend** (`harness serve stop`, then spawn).
   - Healthz answers with another state id: another install owns the port; spawn on the next free port from `port+10`.
   - No answer: spawn.
4. **Spawn**: `bash <root>/harness serve --port N`, `cwd` = last project or `$HOME`, stdin ignored (non-tty path: `wait -n`), env `HARNESS_HOST_CONFIRM=1` (only after the app's one-time host-mode consent, W1), `HARNESS_OPEN=0`, `NO_COLOR=1`, `TERM=dumb`; own process group; stdout+stderr to a 400-line ring and `userData/logs/serve.log`. Ready = `/healthz` ok and `/api/v1/status` ok, polled every 250 ms for up to 120 s (first run may install the toolchain: the startup screen shows the log tail live).
5. **Failure classification** (`classify.ts`, unit-tested on captured text): missing `.env`/values -> `needs-config`; key locked or rejected (the probe banner, with `unlock_url` if present) -> **start again with `HARNESS_SKIP_AUTH_PROBE=1`** so queue, history and settings stay usable, and raise the key banner; python missing; port in use; anything else -> `down` with the log tail, **Retry** and **Run doctor**.
6. **Crash recovery**: an owned child exiting, or 3 consecutive poll failures, gives `reconnecting`; main re-probes `/healthz` (a `USR1` re-exec after `harness upgrade` shows as a new `X-Harness-Pid`: just resume). An owned serve that is really gone is respawned after 1 s, 5 s, 15 s (max 3 in 5 min), then `down` with a manual **Start**. The renderer keeps the last data, marks it stale, and disables actions with the reason "backend offline". PTY sessions are separate processes and keep running.
7. **Stale code**: any API response with `X-Harness-Stale: 1` sets `stale:true`: a quiet banner "harness changed on disk: restart backend" with **Restart**.
8. **Compatibility**: `status.api` must equal `API_VERSION` (1). Lower: `incompatible` "update harness"; higher: `incompatible` "update the app".
9. **Quit**: `settings.backend.onQuit` = `keep-if-busy` (default: leave an owned serve running when a queue run or the findings agent is active, because auto-resume lives in serve), `stop` (`harness serve stop`), or `keep`. An attached serve is never stopped. The host proxy is left alone, as `harness serve` does.

---

## 2. UX workflows

Notation: **Mod** = Cmd on macOS, Ctrl elsewhere. Every flow lists the failure branch.
All destructive actions use a danger-styled confirm; all others are single-click with undo or are idempotent.

### W1 First run / onboarding
1. Window opens on the default static background (B1+B2) with a centred glass card, 4 steps with a progress rail: **Install, Configuration, Key, Backend**. Each step auto-advances when already satisfied, so a healthy install goes straight to Home in about a second.
2. **Install**: found -> shows the path and `git log -1` line. Not found -> "Harness is not installed here": the documented install command from `docs/INSTALL.md` in a copyable block, **Choose folder...** (native picker, validated as in 1.2 step 1) and **Check again**. The app does not run the installer itself.
3. **Configuration**: no `.env` -> **Create configuration**; then a short form for the missing required values only (`PROXY_API_URL`, `DEFAULT_MODEL_NAME`, or the OAI/ChatGPT set, chosen with a 3-way "Upstream type" segmented control). Descriptions come from `config list --tsv`.
4. **Key**: paste field (masked, reveal-on-hold), optional "created on" date (defaults today) and "expires on" date. **Save and verify** stores it in the vault, writes it to `.env`, runs the probe (W13). Results: ok -> next; locked -> the unlock URL as a button ("Open unlock page"), then **I unlocked it, check again**; rejected -> inline error with the upstream message, field keeps focus; network error -> "Could not reach upstream (proxy?)" with **Continue anyway**.
5. **Backend**: host-mode consent (one time): states plainly that serve and host sessions run without the container firewall and isolation (the CLI's own gate text), checkbox + **Start**. Then the live log tail until `up`. Failure -> the classified message, **Retry**, **Run doctor**, **Open log**.
6. **Done**: three tiles: "Open a project", "Add tickets to the queue", "Pick a background". Dismiss lands on Home. Onboarding can be reopened from the palette ("Setup assistant").

### W2 Open a project and start a session
1. `Mod+T`, rail **+**, or palette "New session" opens the **New session** dialog (480 px).
2. Fields: **Folder** (combobox: recent projects, typed path with completion from `shell:path-info`, **Browse...**; shows git branch when it is a repo); **Mode** (cards): Container agent (`harness`), Host agent (`harness host`), Container shell (`harness shell`), Web chat (opencode web), Plain shell; **Options** (agent modes): yolo, normal prompts, thoughts, network, ticket key, and for host mode resume last session (`--continue`); **Open in**: this group / split right / split down.
3. Enter starts. Host modes ask for the host consent if not yet given. The tab appears immediately with a "starting" spinner dot; first PTY byte swaps it to the running dot.
4. Failure: process exits non-zero in under 5 s -> the terminal keeps the CLI's own message and shows an inline bar "Exited with code N" with **Retry**, **Edit options**, and for container failures **Try host mode**.
5. The folder is added to recents; drag a folder from the OS onto the window to open the dialog pre-filled.

### W3 Several session tabs
- `Mod+T` again (dialog remembers the last choice; `Mod+Shift+T` = "same again, no dialog"; palette "Duplicate session").
- Tab label = folder name + mode glyph; when the session shows up in `/api/v1/sessions` (same `directory`, `source` matching the mode, `created >= launch - 5 s`) the tab links to it: label becomes the session title, the dot follows `working`, and the tab menu gains **Open transcript**.
- Sidebar "Sessions" section lists open tabs (grouped by dock group) then recent sessions; click focuses, middle-click closes.
- More than 8 visible terminals: the oldest hidden ones drop their WebGL addon (DOM renderer) to stay under the context cap (DR(g)); invisible to the user.

### W4 Dock, split, rearrange, close, restore
1. **Drag a tab** onto another group's centre (join), an edge (split), or the window edge (edge group). Drop overlay is dockview's, themed. `Esc` cancels a drag.
2. Keyboard: `Mod+\` split right, `Mod+Shift+\` split down, `Mod+Alt+Arrow` focus neighbour group, `Mod+Alt+Shift+Arrow` move tab to neighbour group, `Ctrl+Tab` / `Ctrl+Shift+Tab` cycle tabs in the group, `Mod+1..9` tab N.
3. Tab context menu: Close, Close others, Close to the right, Split right/down, Float, Pin (pinned tabs sort first and lose the close button), Rename, Copy path, Open transcript.
4. Sash drag resizes; double-click a sash equalises. `Mod+J` toggles the bottom group; `Mod+B` the sidebar.
5. **Close**: `Mod+W`. A terminal with a live process asks "Stop the agent and close?" (setting `terminal.confirmClose`). Closed tabs go on a 20-deep stack: `Mod+Shift+T` (outside a terminal on Linux/Windows: see W18) reopens (terminal = relaunch with the same spec, optionally `--continue`; others reopen as they were).
6. **Layout undo**: `Mod+Alt+Z` steps back through the last 20 `toJSON` snapshots; palette "Reset layout".
7. Overflowing tab strip: chevron dropdown with a filter field.

### W5 Queue, end to end
The Queue view is a singleton panel (rail, `Alt+2`). Left: ticket list; right (or a separate `ticket` panel when double-clicked / `Mod+Enter`): ticket detail.

**Add tickets** (`A`, toolbar **Add tickets**, palette, or **Add** on a Jira row)
1. Dialog (800 px) with the stage flow drawn as the pipeline it runs (plan and review panels, fix rounds), like the web dialog.
2. Source toggle: **Jira keys** (chips input: paste a list; each chip validates) or **Local ticket** (title + description).
3. Project folder (same combobox as W2) -> branches and repos load from `/queue/branches` and `/queue/repos`; **Start from**: new work / continue existing branch / here (no branch). If a branch already named after the ticket exists, the dialog says so and preselects Continue (owner can pick New work), as the web form does.
4. Stages (toggles + presets), **After the plan**: go / take defaults / wait for me; optional note; per-repo branch modes when the project has several repos.
5. Submit -> `POST /queue/add`. Pending state on the button; result toast; a refusal (409) shows the server's message inline at the top of the dialog and keeps every field. Partial success (some keys refused) keeps only the failed chips.

**Start / resume** (`S`)
6. Runner card at the top: state (idle, starting, running: ticket + stage + step + elapsed, halting, exited), model, mode, parallel slots (`on[]`). **Start** split-button: container / host / single-message. Host needs the checkbox the API calls `confirm_host`. Refusal reason shown inline.

**Watch live**
7. Selecting a running ticket shows the **Live** tab: stage timeline (plan -> work -> verify -> review rounds -> push) with the current stage pulsing; under it the stage's **transcript** (the stage's `session.id`, tailing; auto-follows to the next stage's session via `/follow?queue=1`) and a **Raw output** toggle (the `<stage>-<n>.out` file, byte-offset tail). "Pin to bottom" turns off when the user scrolls up; a "N new" pill restores it. **Open in own tab** docks the transcript elsewhere.

**Inject a note / message**
8. Composer under the live transcript: **Send to agent** (`POST /sessions/<sid>/send`: reaches the running stage through the inbox) and, via a dropdown on the button, **Add note** (`POST /tickets/K/note`: recorded on the ticket for the next stage). Enter sends, Shift+Enter newline. Not live -> the send half is disabled with the reason; note still works.

**Stop stage, retry, halt, end**
9. Ticket actions bar (each button from `ticket.actions[name]`, disabled ones show `reason` in a tooltip): **Stop** (asks; shows "stopping..." while `stopping`), **Retry** (menu of `retry_choices`), **Continue** (optional text), **Follow-up**, **Edit**, **Move** (also drag-reorder and `Alt+Up/Down`), **Archive / Unarchive**, **Remove** (danger confirm).
10. Runner: **Halt after this stage** (`H`; card shows "halting") and **End now** (danger confirm naming the ticket and stage that will be killed).

**Needs answers / plan review**
11. Status `needs-answers` or `plan-review` raises a badge on the rail and an OS notification (if unfocused). The Ticket tab shows the plan packet, acceptance criteria and questions; each question has its options/default and a free-text field; **Send answers**, **Take defaults**, **Approve plan**, **Request changes** (feedback text). The form carries the plan `sig`; if the server answers that the plan changed (`CHANGED`), the view reloads the plan, **keeps what was typed**, and marks changed questions.

**Review results, diff, push**
12. Status `done`/`blocked`/`stopped`: **Result** tab: outcome summary, warnings, `diffstat`, commits, push state per repo (`push/pushes`, `left_uncommitted`), `compare_url` as **Open compare** (external).
13. **Changes** tab: repo selector, view selector (work / uncommitted / unpushed), file tree + diff (diff2html, side-by-side or unified), "show more lines" via `/lines`, click a line to add a review comment; the comment tray (max 40 x 2000 chars, enforced client-side too) sends **Fix these** (`POST /tickets/K/fix`).
14. **Push**: pushing is done by the queue's own push stage (`QUEUE_PUSH`, per-ticket stages). The Result tab shows what was pushed; when something is left unpushed it offers **Retry from push** (retry with that stage) and **Open terminal in the clone** (plain shell tab at the ticket's repo dir). There is no separate push endpoint to call, and the app does not run `git push` itself.
15. **History** tab: sessions per stage (open any as a transcript), attempts, rounds, notes, `log.md`.

**Edge cases the view must handle** (each gets a unit or e2e test, section 8): ticket removed while open (`gone` -> panel shows "This ticket was removed" + Close); `interrupted` (running with no live run) -> offers Resume run; `stopping` + `stop_wait`; runner `starting`/`exited`; parallel runs (several `on[]`); double-submit (button disabled until the response; a second click is ignored); a tq.py call that takes up to 120 s (pending state, no timeout toast before 130 s); connection lost mid-action (**no automatic retry of a POST**: "Result unknown, state refreshed" once the poll returns); reorder race (optimistic move, reconciled by the next poll); drafts (answers, notes, comments, add-dialog fields) persisted per ticket/field in `state.json` and restored after restart, cleared on success.

### W6 Sessions (history)
List of all recorded sessions (both sources), filter box (`q`), chips: working, queue, debug, findings, chat. Row: title, folder, model, tokens, cost, updated. Enter opens the **transcript** panel; **Open in web chat** for `host` sessions (opencode web link); **Follow newest** toggle opens whatever starts next. Transcript: message list (virtualised), tool calls collapsed with status, **Load full output** per part, subagent (children) sidebar, **Export JSON** (`/sessions/<sid>` + all messages, saved through a native save dialog), composer when live, **Stop** when it is a queue stage.

### W7 Findings
Board of findings (cards: title, repo/area, lens, verdict; bug cards expand to steps/expected/actual/trace). Drag to reorder (`move`), multi-select + **Archive**, Archived tab with **Restore**. Agent card: state, heartbeat, counts per lens, last passes; **Start** (folder checklist + extra paths), **Stop**, **Notes for the agent** (4000 chars), **Folders** (save / reset), **Watch it work** (opens the newest findings session as a following transcript: `/follow?findings=1`), **Knowledge notes** list -> note reader.

### W8 Vault
Vault switcher (list from `/vaults`, **Use**), overview (repo facts, pub pages, activity log), **Browse** (tree + markdown reader with internal links; images through `/vault/file/`), **Search**, commit view, **Ingest** (start with unit/dir/max/model, live progress from the ingest run file + output tail, **Stop**, **Retry failed**, **Forget codebase** with danger confirm). Pub pages open in a Web panel (they are sandboxed HTML).

### W9 Jira
Tabs Sprint / Backlog, "Mine" toggle, project filter. Rows: key, summary, status, assignee, type, priority; a row already in the queue shows its queue status instead of **Add**. **Add** opens the Add tickets dialog for that key; multi-select + **Add selected**. No tracker token -> empty state linking to Keys.

### W10 Settings
Sections (left list, right pane; search field filters across all): **Appearance** (theme, accent, density, UI scale, glass on/off, see-through panels, reduce motion), **Background** (W16), **Terminal**, **Sessions**, **Queue** (default start mode + the harness queue keys), **Harness configuration** (every editable `.env` key grouped as `settings_view.GROUPS` does; secrets show "(set, N chars)" and link to Keys), **Network allowlist** (list, allow/deny host, push toggle), **MCP** (install/enable/disable), **Access codes** (add shows the code once in a copy dialog; remove), **System** (doctor output, version, **Check for updates**), **Notifications**, **Keyboard** (list with search; rebind by recording a chord; conflicts flagged), **Advanced** (install root, port, on-quit behaviour, open logs/user data folder, reset layout, reset all).
Changes to app settings apply instantly and persist (no Save button). Harness configuration rows save on blur/Enter with a per-row pending tick and inline error; a note says which changes need a backend restart, with a **Restart backend** button.

### W11-W14 Keys
- **W11 Add**: Keys view -> **Add key** -> kind (Upstream API key, OpenAI-format key, Tracker token, ChatGPT cookie), label, secret, created/expires dates -> **Save** (stored inactive) or **Save and make active**.
- **W12 Rotate**: on a stored key, **Make active**: (1) candidate probe (direct, W13); rejected -> stop, nothing changed; (2) `harness config set`; (3) canonical probe; (4) on rejected: automatic **revert** to the previous key and an explanation; on ok: toast "Active key changed. Revoke the old key in the upstream portal when you are ready", the old key gets a "superseded" tag; (5) if serve is up: "Restart backend to apply" (serve and the host proxy read the key at start) with **Restart now**.
- **W13 Validate**: **Check** on any key. Active key = `harness unlock` through the CLI runner (canonical: honours `.env` proxies), parsed to `ok | locked(unlock_url) | rejected(message) | unknown`. Inactive key = main's own 1-token probe (`POST <_api_base>/v1/chat/completions`, `max_tokens:1`) with the same classification table as `_probe_upstream_auth` (INV 5); a network failure is `unknown`, never "bad". Automatic checks: at start, after any key change, every 6 h, and when a session or queue error mentions the lock. Locked -> banner in the status bar and Keys view with **Open unlock page** and **Check again**; ok after locked sets `lastUnlockAt = now`.
- **Lifecycle display**: each upstream key shows "locks in N days" (14 days after the later of created / last unlock; team keys can be flagged "admin unlock") and "expires in N days" when a date was given. Notifications 48 h before a lock and 7 days / 1 day before expiry.
- **W14 Remove**: inactive -> confirm, delete. Active -> must pick a replacement or "Clear from .env" (danger confirm: sessions will fail).
- **Import**: on first run with an existing `.env`, each set secret appears as "Active (in .env, not stored in the app)"; nothing is copied until the user rotates.

### W15 Updates
- **App**: check on start + every 6 h (`settings.updates.auto`). Available -> status-bar pill "Update ready to download"; Updates view shows version, notes, size, **Download** (progress), then **Restart to update** (asks first if terminals are running or a queue run is active; layout is saved first). Not auto-updatable (unpackaged, Linux non-AppImage, unsigned macOS) -> the view says exactly why and offers **Open release page**. Errors show the updater's message and **Try again**.
- **Harness**: Updates view second card: branch, local sha, remote sha, last check; **Check now** (`harness check-updates`; "update available" is matched in the text because both outcomes exit 0), **Upgrade** -> confirm that lists consequences (a running queue run restarts between tickets; a web chat in flight fails; serve restarts itself) -> `harness upgrade --no-prompt` with its output streamed into a log pane -> on success the backend manager reconnects and re-checks API compatibility. Failure leaves the log open with **Copy** and **Open terminal here**.
- Version skew: `incompatible` state (1.2 step 8) names which side to update and links to the right card.

### W16 Backgrounds
1. Status-bar picker, `Mod+Shift+B`, Settings -> Background, or palette "Background: ...".
2. Picker (popover 560 px): grid of thumbnails grouped Static / Flow / Chaos / Automata / Fields / System; hover or arrow-key **previews live** with a cross-fade; `Esc` reverts; Enter/click applies.
3. Right column for the selected background: its parameters (from the descriptor), **Intensity**, **Speed**, **Seed** with **Shuffle**, fps cap, "Change every day / every launch" (seeded shuffle), **Reset**.
4. Global: on window blur dim or pause; on battery static or run; a "Performance" line shows live fps / frame ms and the current tier, and says when the engine downgraded itself.
5. Failure: WebGL unavailable or context lost -> engine falls back (worker 2D, then CSS static) and the picker marks the affected ones "needs GPU".

### W17 Command palette
`Mod+K` (also `Ctrl+Shift+P`). Groups: Recent, Sessions, Tickets, Actions, Views, Settings, Backgrounds. Prefix `>` commands, `#` tickets, `@` sessions, `/` vault files. `Mod+Enter` opens in a new split. Nested pages (Backspace on empty input pops). Every command in the app is registered once in a command registry that feeds the palette, menus, shortcuts and the Keyboard settings list (DR(d)).

### W18 Keyboard shortcuts (defaults; all rebindable)
| Action | Shortcut |
|---|---|
| Palette | `Mod+K`, `Ctrl+Shift+P` |
| New session / same again / reopen closed | `Mod+T` / `Mod+Shift+D` / `Mod+Shift+T` |
| Close tab | `Mod+W` |
| Next / previous tab; tab N | `Ctrl+Tab` / `Ctrl+Shift+Tab`; `Mod+1..9` |
| Split right / down | `Mod+\` / `Mod+Shift+\` |
| Focus group / move tab to group | `Mod+Alt+Arrow` / `Mod+Alt+Shift+Arrow` |
| Sidebar / bottom group / zen (hide chrome) | `Mod+B` / `Mod+J` / `Mod+.` |
| Views: Home, Queue, Sessions, Findings, Vault, Jira, Keys, Settings, Updates | `Alt+1..9` (macOS `Ctrl+1..9`) |
| Settings | `Mod+,` |
| Find in panel | `Mod+F` |
| Background picker | `Mod+Shift+B` |
| UI zoom | `Mod+=`, `Mod+-`, `Mod+0` |
| Layout undo | `Mod+Alt+Z` |
| Cycle regions (rail, sidebar, dock, status bar) | `F6` / `Shift+F6` |
| Fullscreen | `F11` (macOS `Ctrl+Cmd+F`) |
| Queue view | `A` add, `S` start, `H` halt, `J`/`K` or arrows select, `Enter` open, `N` note, `R` retry, `.` actions, `Alt+Up/Down` reorder |
| Terminal copy / paste | macOS `Cmd+C/V`; others `Ctrl+Shift+C/V` |

**Terminal rule (Linux/Windows)**: while a terminal has focus, plain `Ctrl+<letter>` belongs to the program in the PTY (`Ctrl+K`, `Ctrl+W`, `Ctrl+T`, `Ctrl+B`, `Ctrl+J` are all meaningful there). App shortcuts are then reached with **`Ctrl+Shift+<same key>`**; xterm's `attachCustomKeyEventHandler` lets only those and `Alt`/`F`-key chords through to the app. On macOS `Cmd` never collides, so nothing changes.

### W19 Error, empty, loading, offline states (one pattern everywhere)
- **Loading**: skeleton rows for the first load only (never a spinner over existing data); a view switch renders the cached topic immediately and refreshes behind it.
- **Empty**: icon, one sentence, one primary action (Queue: "No tickets queued" + **Add tickets**; Sessions: **New session**; Findings: **Start the findings agent**; Keys: **Add key**).
- **Error**: inline panel with the server's message, **Retry**, **Copy details**; never a blocking modal for a failed read.
- **Offline / stale**: status-bar chip "backend offline, retrying in N s" (click = Backend popover: state, log tail, Start/Restart); views dim their header timestamp ("as of 12:41"); action buttons disabled with the reason. Returning online clears it silently.
- **Signed out** (codes on): `needs-login` sheet over the dock area; terminals stay usable.
- **Action feedback**: buttons own their pending state; success = toast (5 s) with the server's `msg`; refusal = inline, persistent until edited.

### W20 Restart restores state
Saved (debounced 500 ms, and synchronously on quit): window bounds/display/maximised, sidebar width and section collapse, full dock layout, each panel's params (terminal spec + a scrollback snapshot from the serialize addon capped at 200 KiB, transcript sid + scroll, ticket key + tab, web URL, view filters), active view, drafts, palette recency, background. On start the layout is rebuilt before data arrives. Terminal tabs come back as **ended** with their old scrollback dimmed and a bar: **Resume session** (`--continue`; host sessions, see 5.2), **Start fresh**, **Close**; `settings.sessions.restore = tabs-and-resume` does the resume automatically. A panel whose kind or params no longer validate is dropped, never fatal. A window that would open off-screen is re-centred.

### W21 Serve crash recovery
As 1.2 step 6. User-visible: chip turns amber "reconnecting", then either back to normal with a toast "Backend restarted" or red "Backend stopped" with **Start** and **Show log**. Live transcript panels resume from their last message index; a queue run that died with serve shows `interrupted` and offers **Resume run**. Drafts are untouched.

### W22 Notifications
OS notifications (only while the window is unfocused; each individually switchable): ticket needs answers / plan review, ticket done or blocked, queue run ended, key locked or about to lock/expire, app update downloaded. Click focuses the window and routes to the item. In-app: rail badges (Queue: count needing input; Keys: warning dot; Updates: dot).

---

## 3. Screen-by-screen spec

Tokens, glass recipe, sizes and motion come from DR(b), DR(c), DR(d); this section only adds what is specific. Every interactive element gets a `data-testid` (naming in 7.5).

### 3.1 Shell
Layout exactly as DR(d): title bar 38, rail 44, sidebar 260 (200-420, `Mod+B`), dock area, status bar 24. `#bg-root` sits behind everything (`z-index: var(--z-bg)`).
- **Title bar**: app icon/traffic-light inset, workspace label (install root folder name + backend chip), centre palette pill, right: Backend chip, Keys warning (only when not ok), Settings. Draggable; controls `no-drag`.
- **Rail**: Home, Queue (badge), Sessions, Findings, Vault, Jira, Debug | bottom: Keys (dot), Updates (dot), Settings. Click opens/focuses that singleton panel; `Mod+click` opens it in a split.
- **Sidebar** sections (collapsible, state persisted): **Open** (tabs by group), **Queue** (compact ticket rows: status dot, key, stage; click opens the ticket), **Recent sessions**, **Pinned**.
- **Dock area**: dockview with the "Spaced" theme mapped onto tokens (DR(f)); empty watermark shows `Mod+T`, `Mod+K` hints and the three onboarding tiles.
- **Status bar**: left: backend chip, runner state ("running ABC-12 work 3/8" or "idle"), counts (working sessions, queued, needs input); right: key state, harness/app update state, background picker button, fps (dev only).
- **Overlays**: palette, dialogs, toasts, menus, popovers per DR(d). Only these use real `backdrop-filter`.
- **States**: `data-phase` on `<html>` mirrors `BackendState.phase`; `data-theme`, `data-accent`, `data-density`, `data-glass` drive tokens; `data-ready="1"` once the first layout is mounted (e2e waits on it).

### 3.2 Panel kinds (dockview components)
| Kind | Instance key | Singleton | Body |
|---|---|---|---|
| `view.home` ... `view.updates` (home, queue, sessions, findings, vault, jira, debug, settings, keys, updates) | kind | yes | native view |
| `terminal` | pty id | no | xterm.js |
| `chat` | opencode session id or `new` | no | `<webview>` opencode web |
| `transcript` | session id | per sid | native transcript |
| `ticket` | ticket key | per key | ticket detail (same component as the queue's right pane) |
| `diff` | key + repo + view | per tuple | diff viewer |
| `web` | URL path | no | `<webview>` on a serve page |
| `vault-file` | vault + path | per path | markdown reader |
Each kind implements `{ mount(el, params, ctx), onShow(), onHide(), serialize(), dispose() }`; `onHide` stops its topic subscriptions, `onShow` resumes them and re-`fit()`s terminals.

### 3.3 Home
Regions: greeting row with backend + key health cards; **Working now** (live sessions, ticket in progress); **Usage** (24 h ledger: requests, tokens, cost, sparkline); **Quick actions** (New session, Add tickets, Open web chat); **Vault pages** (pub pages -> Web panel); **Activity** (vault log); **Prompt modes** (from `status.modes`). States: skeleton; proxy down card with **Restart backend**; no vault card.

### 3.4 Queue
Toolbar: Add tickets, Start split-button, Halt, End, filter field, Archived toggle, runner log button (opens the last 400 lines in the bottom group). Runner card. Ticket list: fixed 32 px rows (drag handle, status pill, key, title, stage/step, attempt, updated), virtualised above 300 rows, multi-select, drag reorder, context menu = the actions bar. Right pane = ticket detail with tabs **Live**, **Ticket** (details, plan, questions), **Changes**, **Result**, **History**, **Agents** (all sessions of the ticket incl. debug agents, with panel labels and working dots). States: empty; runner starting/halting/exited; every edge case in W5.

### 3.5 Ticket detail
Header: key, title, status pill, stage timeline, actions bar. Tab bodies as W5 steps 7-15. The **Edit** action opens the Add dialog in edit mode (values from `GET /tickets/K/edit`).

### 3.6 Transcript
Header (title, model, tokens, cost, source badge, working dot, ticket link, Follow toggle, Export, Open in web chat). Body: virtual list of messages; parts by `type`: `text` (markdown through marked + DOMPurify), `reasoning` (collapsed), `tool` (card: name, status, duration, input, output preview, **Load full output**), `file`, step markers; unknown types render as collapsed JSON. Right rail (collapsible): subagents, inbox (pending/sent/failed messages). Footer: composer or the reason it is disabled. Tail: appended tokens are batched per animation frame (DR(h) 8).

### 3.7 Terminal
xterm per DR(g): opaque `--surface-0`, fit on a ResizeObserver, WebGL addon only while visible, search bar (`Mod+F`), links through `shell:open-external`, unicode11. Top-right hover toolbar: restart, clear, split, open transcript. Exit bar and restore bar as W2/W20. Paste of more than 5 lines or containing a newline asks once ("Paste N lines?", remember choice).

### 3.8 Chat and Web panels
Slim toolbar: back, forward, reload, location label (path only), **Open in browser**, and for Web panels **Open native view** when one exists. Body `<webview>`; on `dom-ready` the app injects theme CSS (page background/foreground/accent mapped from tokens; serve's own nav bar hidden because the app has one). Load failure shows the standard error panel with Retry.

### 3.9 Sessions, 3.10 Findings, 3.11 Vault, 3.12 Jira
As W6-W9. Common list chrome: filter field, chips, virtual list, detail on the right or in a new panel.

### 3.13 Debug
List of debug reports (kind, name, ticket, verdict, change, title, time) -> report reader (markdown) with a link to its session.

### 3.14 Settings, Keys, Updates
As W10-W15. Keys rows: kind icon, label, `....last4`, state pill (active / stored / superseded), probe state with time, lock and expiry countdowns, actions (Check, Make active, Edit, Remove). A header strip states the storage backend ("Encrypted with the OS keychain" or the `basic_text` warning from 1.1).

### 3.15 Onboarding and Backend popover
As W1 and W19.

### 3.16 Parity matrix (serve page -> app surface)
| Serve route (INV 2) | Native | Fallback |
|---|---|---|
| `/` home | Home | Web panel |
| `/pub/<rel>` | opens in Web panel | - |
| `/runs`, `/runs/<sid>`, `/runs/<sid>/tail`, `/part` , `/send`, `/stop`, `/follow` | Sessions, Transcript | Web panel |
| `/runs/queue` (+ jira, branches, repos, runner.out, add/start/halt/end) | Queue, Add dialog, Jira | Web panel |
| `/runs/ticket/K` (ticket, history, agent, diff, lines, edit, `.out`, all actions) | Ticket detail tabs, Diff | Web panel |
| `/runs/findings/*` (board, agent, notes) | Findings | Web panel |
| `/runs/debug*` | Debug | Web panel |
| `/vault*` (overview, ingest, search, browse, commit, file, use, ingest actions) | Vault | Web panel |
| `/settings*` (config, net, mcp, access, system, panels) | Settings, Keys, Updates | Web panel |
| `/login`, `/logout` | handled by main (1.1) | - |
| opencode web | Chat panel | OS browser |

---

## 4. JSON API contract (`serve/api.py`, prefix `/api/v1`)

### 4.1 Hook in `serve/pub.py` (the only edits there; target <= 30 changed lines)
- `import api  # noqa: E402` next to the other view imports (pub.py:52-59).
- **`api.py` must not `import pub`**: pub.py runs as `__main__`, importing it again loads a second copy. pub.py builds one module-level `API_HELPERS = types.SimpleNamespace(read_ledger=read_ledger, proxy_status=proxy_status, vault_log=vault_log, state_id=state_id, ...)` after those functions are defined and passes it in. api.py imports `runs`, `settings_view`, `vault_view`, `diff_view`, `findings_view`, `findings_agent_view`, `jira_view` directly, as pub.py does.
- `handle_get`: (a) inside `if cfg.codes and not user:` add first: `/api/` paths answer `api.deny(self, head)` (401 JSON); (b) right after that block, before the `/` and `/live` branch: `if path.startswith("/api/"): return api.handle(self, cfg, API_HELPERS, "GET", path, url.query, user, head)`.
- `do_POST`: add `"/api/"` to the prefix tuple that requires a session (pub.py's `("/runs/", "/vault/", "/settings/")`), answer a missing session on `/api/` with `api.deny`, and dispatch `api.handle(self, cfg, API_HELPERS, "POST", path, "", user, False)`. `host_ok` and `origin_ok` stay in front, unchanged (their plain-text 403 is mapped to `error.code = "origin"` by the client).
- Nothing else in pub.py, and **nothing in runs.py or the `*_view.py` files**, changes. Where a rule exists only inside an HTML renderer, api.py calls the predicates that renderer calls (`continue_refused`, `followup_refused`, `fix_refused`, `retry_choices`, `STOPPABLE`/`RETRYABLE`/`CONTINUABLE`/`FIXABLE`); if a twin cannot be built without editing runs.py, L1 ships that endpoint as 501 and reports it (the app falls back to the Web panel, 4.6).

### 4.2 Conventions
- **Envelope.** Read: `200 {"ok":true,"sig":"<sha1>","data":...}`; with `?sig=<s>` equal to the current sig: `200 {"ok":true,"sig":"<s>","same":true}`. Action: `200 {"ok":true,"msg":"...","data":{...}?}`. Error: `{"ok":false,"error":{"code":"...","message":"..."}}`.
- **sig** = `sha1(json.dumps(data, sort_keys=True, default=str))`, the same construction as the web UI's `_sig`. `data` must not contain wall-clock "now" values or ages, or the sig never settles (ages are computed client-side from timestamps; `now` travels outside `data`).
- **Status codes**: 400 `bad-request` (bad JSON, bad param, unknown action), 401 `auth`, 403 `origin`, 404 `not-found` (unknown path, session, ticket, file), 405 `method`, 409 `refused` (the `(False, msg)` of an action; `message` is that text) or `changed` (answer/approve against an older plan: the `CHANGED` text), 413 `too-large`, 415 `content-type`, 500 `internal` (exception class + text, traceback to stderr only), 501 `unavailable` (endpoint not built on this harness), 503 `unavailable` (`cfg.runs is None` for a runs endpoint, or no vault for a vault endpoint).
- **Headers** on every response: `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`, `X-Harness-Api: 1`, and `X-Harness-Stale: 1` when `cfg.code_changed()`. No CORS headers, ever. HEAD is answered like GET without a body.
- **POST bodies**: one JSON object, `Content-Type: application/json` required, `Content-Length` required. Size limits are the ones pub.py applies to the matching form route (L1 reads them at `runs_send` / `ticket_post` / `settings_post`; do not invent new ones).
- **Form bridge** (`_form(body)`): the action functions take `{name: [value]}` dicts. JSON keys **are the form field names of INV 4**; values convert as: string -> `[s]`; number -> `[str(n)]`; `true` -> `["1"]`; `false`/`null` -> key omitted; list -> one entry per item. Three typed conveniences are also accepted and expanded: `stages: ["plan","work"]` -> `stage_<s>=1`; `repo_modes: {"<rel>": "<value>"}` -> `repo_mode:<rel>`; `comments: [{text,path,line,side,code,at,repo}]` -> the JSON string `fix` expects; and for `answer`: `answers: {"<question id>": "<text>"}`, `feedback`, `defaults`, `go`, `sig`, mapped onto the per-question field names `ticket_action` reads (not enumerated in INV: L1 reads runs.py:4003 ff. and lists them in the architecture doc).
- **Sender**: `Runs.send(sid, text, sender=user or "desktop")`.
- **Time**: epoch **milliseconds** for session times (as stored), whatever `state.json` holds for ticket times (passed through; the client formats defensively).
- **Python 3.9**. No third-party imports. Handlers are plain functions in two registries, `READS = {"queue": queue_data, ...}` and `ACTIONS`, so `/poll` and the GET endpoints share one implementation and cannot drift.
- **Cost**: disk-touching reads go through `Runs.memo` where one exists; `proxy_status` (2 s network timeout) and the git version lookup are cached in api.py (5 s / 60 s TTL) so `/status` is safe at 1 Hz.

### 4.3 Read endpoints
P0 = required for the app to boot and run the queue and sessions; P1 = required for parity natively; P2 = may ship as 501 (Web panel covers it).

| P | GET path | `data` (types in 4.7) | Backed by |
|---|---|---|---|
| 0 | `/status` | `Status` (`mode` is `serve` when `cfg.opencode_port` is non-zero, else `viewer`: the background viewer starts no opencode web) | `cfg`, `state_id`, `proxy_status`, `cfg.code_changed()`, git |
| 0 | `/poll?t=<topic>~<sig>&t=...` | see 4.5 (not enveloped per request) | the `READS` registry |
| 0 | `/opencode` | `{port,url,user,password}`; 404 when `cfg.opencode_port` is 0 | `cfg.opencode_password()` |
| 0 | `/queue` | `Queue` | `Runs.runner`, `ticket_keys`, `archived_keys`, `ticket_state` |
| 0 | `/tickets/<K>` | `Ticket` | `ticket_state`, `*_refused`, `retry_choices`, `ticket_sessions`, `ticket_agents`, `stage_outs` |
| 0 | `/tickets/<K>/out/<name>?from=<byte>` | `OutChunk` (<= 256 KiB a call; decoded `errors="replace"`, cut on a UTF-8 boundary; `reset:true` when the file is now shorter than `from`) | `stage_outs`, `ticket_file` |
| 0 | `/sessions?q=&limit=&all=1` | `SessionRow[]` (default roots only, limit 300) | `Runs.sessions`, `live`, `ticket_of_row`, `agent_sessions` |
| 0 | `/sessions/<sid>` | `SessionHead` | `Runs.find`, `children`, `inbox`, `live` |
| 0 | `/sessions/<sid>/messages?from=<i>&limit=<n>` | `MessagesPage`. Tool-part output longer than 4000 chars is cut and flagged `truncated:true`. | `Runs.messages` |
| 0 | `/sessions/<sid>/parts/<pid>` | `{text}` | `part_text` |
| 0 | `/follow?since=<ms>&queue=1` or `&findings=1` | `{sid: string or null}` | `follow_target` |
| 1 | `/home` | `Home` | `read_ledger`, `vault_log`, pub page list, live sessions (data twin of `live_summary`) |
| 1 | `/sessions/<sid>/export` | the `export()` dict | `export` |
| 1 | `/queue/runner-log` | `{text}` (last 400 lines) | `Runs.runner_out` |
| 1 | `/queue/branches?project=`, `/queue/repos?project=` | pass-through of the existing JSON | `project_branches`, `repo_tree` |
| 1 | `/tickets/<K>/edit` | `{form: {...}}` current values keyed by the add-form field names | the values `render_ticket_edit` fills in |
| 1 | `/tickets/<K>/diff?repo=&view=` | `Diff` | `diff_view.repos`, `diff_view.changes` |
| 1 | `/tickets/<K>/lines?repo&view&rev&path&from&to` | `{lines,total}` | `diff_view.lines` |
| 1 | `/jira?list=sprint` or `backlog`, `&mine=1` | `Jira` | `jira_view.lookup`, `jira_view.row` |
| 1 | `/findings` | `Findings` | `findings_view.collect`, `read_board`, `arrange` |
| 1 | `/findings/agent` | `FindingsAgent` | `findings_agent_view.read_status`, `_is_running`, `passes`, `read_folders`, `last_repos`, `knowledge_index` |
| 1 | `/findings/notes/<slug>/<name>` | `{text}` | `knowledge_text` |
| 1 | `/vaults`, `/vault`, `/vault/ingest` | `Vaults`, `VaultOverview`, `Ingest` | `vault_view.list_vaults`, `repo_facts`, `pages`, `ingest_summary`, `ingest_run`, `vault_log` |
| 1 | `/vault/tree?path=`, `/vault/file?path=` | `{entries:[{name,path,dir,size}]}`, `{path,kind,text}` (text <= 1 MiB; images stay on the existing `/vault/file/<rel>` bytes route) | vault dir, with vault_view's own path-safety helper |
| 1 | `/settings` | `SettingsData` (secrets only as "(set, N chars)") | `settings_view.read_config`, `read_allowlist`, `read_mcp`, `system_info`, access-code names |
| 1 | `/settings/doctor` | `{text}` (slow; never part of `/settings`) | `settings_view.read_doctor` |
| 1 | `/update` | `HarnessUpdate` (no network: reads `state/.harness-update-check` and `git rev-parse`) | new, small |
| 2 | `/debug`, `/debug/<runs or reports>/<name>` | `DebugReport[]`, `{text}` | `debug_reports`, `agent_sessions` |
| 2 | `/vault/search?q=` | `{hits:[{path,line,text}]}` | data twin of `vault_view.search` |
| 2 | `/vault/commit/<sha>?path=` | `{sha,subject,date,diff}` | data twin of `render_commit` |

### 4.4 Action endpoints (POST; answer `{ok,msg}` or 409)
| P | POST path | Body | Backed by |
|---|---|---|---|
| 0 | `/sessions/<sid>/send` | `{text}` | `Runs.send` |
| 0 | `/sessions/<sid>/stop` | `{}` | `session_stop` |
| 0 | `/queue/add` | add-form fields (INV 4): `kind, keys, title, desc, project, start, cont, base, branch, preset, stages[], after_plan, note, repo_modes{}` | `queue_action(runs,"add",form)` -> `add_tickets` |
| 0 | `/queue/start` | `{mode:"container"\|"host"\|"single", confirm_host?:true}` | `queue_action` -> `Runs.start_runner` |
| 0 | `/queue/halt`, `/queue/end` | `{}` | `queue_action` |
| 0 | `/tickets/<K>/<action>` for every name in `TICKET_ACTIONS`: `answer` `{answers,feedback?,defaults?,go?,sig}`; `approve` `{sig}`; `retry` `{stage?}`; `continue` `{text?,confirm_host?}`; `followup` `{text}`; `note` `{text}`; `fix` `{comments[]}`; `move` `{where}` or `{before}`; `edit` add-form fields; `stop`, `remove`, `archive`, `unarchive` `{}` | `ticket_action(runs,key,action,form)` |
| 1 | `/findings/archive` `{id}` or `{sel:[...]}`, `/findings/restore` `{id}`, `/findings/move` `{key,before}` | | `findings_view.action` |
| 1 | `/findings/agent/start` `{pick?,dir:[...],more?}`, `/stop` `{}`, `/notes` `{notes}`, `/folders` `{dir:[...]}` or `{act:"reset"}` | | `findings_agent_view.agent_action` |
| 1 | `/vault/use` `{name}`, `/vault/ingest/start` `{unit?,dir?,what?,max?,model?}`, `/vault/ingest/stop` `{}`, `/vault/ingest/forget` `{name}` | | `vault_view.use_vault`, `start_ingest`, `stop_ingest`, `forget_codebase` |
| 1 | `/settings/config` `{set:{KEY:value}, clear:[KEY]}` (bridged to `v_<KEY>` / `clear_<KEY>`) | | `settings_view.save_config` |
| 1 | `/settings/net` `{act,host,push?}`, `/settings/mcp` `{act,name}`, `/settings/system` `{act:"doctor"\|"check"\|"update"}` (-> `data.output`) | | `net_change`, `mcp_change`, `system_action` |
| 1 | `/settings/access` `{act:"add"\|"remove",name}`; add answers `data.code` once | | the code path `/settings/access` uses today |

**Deliberately not in the API** (main's CLI runner does them, 5.2): the key probe, `harness upgrade`, starting/stopping serve, and `.env` bootstrap. Reason: each must work while serve is down, and an upgrade re-execs serve under the request.

### 4.5 Live updates: `/poll`
- Request: `GET /api/v1/poll?t=queue~ab12...&t=ticket:ABC-12~&t=status~9f...` (up to 24 `t`; sig may be empty). Topic grammar `^[a-z-]+(:[A-Za-z0-9._-]+)?$`.
- Response: `{"ok":true,"now":<epoch s>,"stale":bool,"topics":{"<topic>":{"sig":..,"data":..} | {"sig":..,"same":true} | {"error":{"code":..,"message":..}}}}`. One failing topic never fails the others.
- Topics: `status`, `home`, `queue`, `sessions`, `findings`, `findings-agent`, `vault`, `ingest`, `debug`, `ticket:<KEY>` (data `Ticket`, or `{"gone":true}`), `session:<sid>` (data `SessionHead`: its `count` and `updated` tell the client when to fetch `/messages`).
- Client (main's poller, 5.3): 1 s while the window is focused and something is running (runner running, a subscribed session working), else 3 s; 5 s when blurred; when hidden only `status`+`queue` every 15 s (for notifications); failure backoff 1, 2, 5, 15 s. Transcript and stage-output tails are fetched on demand when the head changes, not on a timer.

### 4.6 Fallback rule
An endpoint answering 404 or 501 marks that native view "not available on this harness version" with **Open web version** (Web panel on the matching serve page). This is what lets L1 land P0 first and makes old/new harness skew survivable.

### 4.7 Types (normative; the parent extracts this block verbatim)

```ts
// FILE: desktop/src/shared/api.ts
// Wire contract of serve/api.py. Fields listed are the ones the UI relies on;
// objects marked "pass-through" carry every key the Python side has and the UI
// must ignore keys it does not know.
export const API_VERSION = 1;
export const API_PREFIX = '/api/v1';

export type ApiErrorCode =
  | 'bad-request' | 'auth' | 'origin' | 'not-found' | 'method' | 'refused' | 'changed'
  | 'too-large' | 'content-type' | 'internal' | 'unavailable'
  | 'offline' | 'timeout' | 'unknown-result';            // last three are made by main, never by serve
export interface ApiError { ok: false; status: number; error: { code: ApiErrorCode; message: string } }
export interface ReadOk<T> { ok: true; sig: string; data?: T; same?: true }
export interface ActionOk { ok: true; msg: string; data?: Record<string, unknown> }
export type ReadResult<T> = ReadOk<T> | ApiError;
export type ActionResult = ActionOk | ApiError;
export interface Gate { ok: boolean; reason: string }    // reason: why a control is disabled ('' when ok)

export interface Status {
  api: number; mode: 'serve' | 'viewer'; pid: number; state_id: string; stale: boolean;
  root: string; state_dir: string; codes: boolean; user: string | null; runs: boolean;
  opencode: { port: number; url: string } | null;
  proxy: { url: string; up: boolean; detail: string } | null;
  vault: { name: string; dir: string } | null;
  modes: { name: string; description: string }[];
  version: { branch: string; sha: string; describe: string };
}

export interface SessionRow {
  id: string; parent: string | null; source: string;     // 'container' | 'host'
  directory: string; title: string; agent: string; model: string;
  created: number; updated: number;                      // epoch ms
  tokens_in: number; tokens_out: number; cost: number; kids: number;
  working: boolean;                                      // updated within 90 s or live
  live: boolean;                                         // relay alive: can be messaged
  ticket: string | null;                                 // queue ticket key
  kind: 'session' | 'queue' | 'debug' | 'findings'; label: string;
  web: string | null;                                    // opencode-web URL for this session, when there is one
}
export interface InboxItem { at: number; sender: string; state: string; text: string }
export interface SessionHead {
  row: SessionRow; count: number;                        // number of messages
  children: { id: string; title: string; agent: string; created: number; updated: number }[];
  inbox: InboxItem[]; send: Gate; stop: Gate;
}
export interface Part { id: string; type: string; truncated?: boolean; [k: string]: unknown }  // opencode part, pass-through
export interface Message { id: string; index: number; created: number; role: string; info: Record<string, unknown>; parts: Part[] }
export interface MessagesPage { from: number; count: number; messages: Message[]; working: boolean; live: boolean }

export type TicketStatus = 'queued' | 'running' | 'interrupted' | 'needs-answers' | 'plan-review' | 'blocked' | 'stopped' | 'done';
export type TicketAction = 'answer' | 'approve' | 'retry' | 'continue' | 'stop' | 'remove' | 'move' | 'note'
  | 'followup' | 'edit' | 'fix' | 'archive' | 'unarchive';
export interface Runner {                                 // Runs.runner(), pass-through
  running: boolean; starting: boolean; halting: boolean; exited: unknown;
  ticket: string; stage: string; step: string; model: string; started: unknown; since: unknown;
  host: boolean; single: boolean; watch: boolean; parallel: number; on: unknown[]; pid: number; beat: unknown;
  [k: string]: unknown;
}
export interface TicketSummary {                          // subset of ticket_state for list rows
  key: string; title: string; status: TicketStatus; stage: string; step: string; reason: string;
  created: unknown; updated: unknown; finished: unknown; attempt: number; project: string; branch: string;
  stopping: boolean; debug: boolean; session: { stage: string; id: string } | null;
  actions: Record<TicketAction, Gate>;
}
export interface Queue {
  runner: Runner; tickets: TicketSummary[]; archived: TicketSummary[];
  start: Gate;                                            // may a run be started now
}
export interface TicketState {                            // Runs.ticket_state(), pass-through (INV 3 lists every field)
  status: TicketStatus; stage: string; title: string; reason: string; plan_sig: string;
  criteria: { id: string; text: string; [k: string]: unknown }[];
  questions: { id: string; text: string; ask: string; blocking: boolean; default: string; options: unknown[] }[];
  notes: { at: unknown; text: string }[];
  history: { at: unknown; stage: string; attempt: number; result: string; details: string }[];
  [k: string]: unknown;
}
export interface Ticket {
  key: string; archived: boolean; state: TicketState;
  actions: Record<TicketAction, Gate>;
  retry_choices: { value: string; label: string }[];
  sessions: { sid: string; stage: string; label: string; kind: string; working: boolean; created: number }[];
  outs: { name: string; mtime: number; size: number }[];
  log: string;                                            // log.md text (capped 256 KiB, newest kept)
}
export interface OutChunk { name: string; from: number; next: number; size: number; text: string; reset: boolean }
export interface Diff {
  repos: { rel: string; label: string }[]; views: string[]; repo: string; view: string;
  diff: string; commits: { sha: string; subject: string }[]; notes: unknown; says: unknown; rev: string; error: string;
}
export interface JiraRow { key: string; summary: string; status: string; assignee: string; type: string; priority: string;
  queued: TicketStatus | null; [k: string]: unknown }
export interface Jira { rows: JiraRow[]; error: string; note: string; more: boolean; projects: string[] }
export interface Finding { id: string; title: string; archived: boolean; [k: string]: unknown }      // pass-through
export interface Findings { items: Finding[]; order: string[] }
export interface FindingsAgent { running: boolean; status: Record<string, unknown>; passes: Record<string, unknown>[];
  folders: unknown; last_repos: unknown; notes: string; knowledge: { slug: string; name: string; title: string }[] }
export interface Vaults { active: string; vaults: { name: string; url: string }[] }
export interface Ingest { run: Record<string, unknown> | null; summary: unknown; out_tail: string }
export interface VaultOverview { name: string; dir: string; facts: unknown; pages: { rel: string; title: string }[];
  ingest: Ingest; log: unknown }
export interface Home { live: SessionRow[]; usage: Record<string, unknown> | null;   // read_ledger(): {day,hour,models,remaining}
  pages: { rel: string; title: string }[]; activity: unknown }
export interface ConfigRow { key: string; value: string; secret: boolean; desc: string; group: string }
export interface SettingsData { config: ConfigRow[]; allowlist: { host: string; push: boolean; note: string }[];
  mcp: { name: string; state: string; status: string }[]; access: { enabled: boolean; names: string[] };
  system: Record<string, unknown> }
export interface HarnessUpdate { branch: string; local: string; remote: string | null; behind: boolean | null; checked_at: number | null }
export interface DebugReport { kind: string; name: string; mtime: number; key: string; verdict: string; change: string;
  title: string; session: string }

export interface TopicMap {
  status: Status; home: Home; queue: Queue; sessions: SessionRow[]; findings: Findings;
  'findings-agent': FindingsAgent; vault: VaultOverview; ingest: Ingest; debug: DebugReport[];
}
export type Topic = keyof TopicMap | `ticket:${string}` | `session:${string}`;
export type TopicData<T extends Topic> =
  T extends keyof TopicMap ? TopicMap[T] :
  T extends `ticket:${string}` ? Ticket | { gone: true } :
  T extends `session:${string}` ? SessionHead | { gone: true } : never;
```

Where a type above says `unknown` or pass-through, L1 documents the actual shape in `architecture/harness-cli.md` and L3 renders it defensively (unknown keys ignored, missing keys tolerated). The file is frozen for the build; a needed change goes to the parent, not into a leg's copy.

---

## 5. IPC contract (main <-> renderer, through the preload bridge)

### 5.1 Rules
- The preload exposes exactly one object, `window.harness: Bridge` (below), with `contextBridge.exposeInMainWorld`. No `ipcRenderer` leaks.
- Request/response = `ipcRenderer.invoke(channel, payload)`; events = `webContents.send(channel, payload)`, surfaced as `bridge.on(channel, cb)` returning an unsubscribe function.
- Every handler: sender check, payload validation, typed result (1.1). Handlers never throw to the renderer: failures are `{ ok:false, error }`.
- **PTY data is relayed through main** (pty-host -> main -> renderer), not over a transferred MessagePort: output is already coalesced to one message per 8 ms per terminal in the pty-host, so the extra hop is a few hundred small messages a second at worst, and it avoids moving ports across `contextBridge`. Flow control: the renderer acks consumed characters (`pty:ack`); the pty-host calls `pty.pause()` above 1 MiB unacked and `resume()` below 256 KiB.

### 5.2 Channels
| Channel (invoke) | Payload -> Result | Notes |
|---|---|---|
| `app:info` | `-> AppInfo` | versions, platform, paths, safeStorage backend, reduced motion, e2e flag |
| `backend:get` | `-> BackendState` | |
| `backend:action` | `BackendAction -> BackendState` | start, restart, stop, retry, set-root, create-env, consent-host, login, create-code |
| `api:get` | `{path, query?, sig?} -> ReadResult<unknown>` | path must match `^/api/v1/`; timeout 10 s |
| `api:post` | `{path, body} -> ActionResult` | timeout 150 s; **never retried**; connection lost after send -> `unknown-result` |
| `api:bytes` | `{path} -> {ok, mime, data: Uint8Array}` | only `^/vault/file/` |
| `live:set` | `{topics: Topic[]} -> void` | the full set this window wants; main polls the union |
| `harness:config-list` | `-> {ok, rows: ConfigRow[]}` | `harness config list --tsv`; works with serve down |
| `harness:config-set` | `{key, value} -> {ok, msg}` | refuses secret keys (those go through `keys:*`) and keys outside the editable set |
| `harness:job` | `{job: 'doctor' \| 'check-updates' \| 'upgrade' \| 'serve-stop'} -> {id}` | output streams on `job:output`, ends with `job:exit` |
| `harness:update-get` | `-> HarnessUpdateState` | parsed `check-updates` result + last check time |
| `keys:list` | `-> KeyVaultState` | metadata only |
| `keys:add` | `{kind, label, secret, createdAt?, expiresAt?, team?, activate?} -> KeyOpResult` | the only channel that carries a secret, renderer -> main, once |
| `keys:update` / `keys:remove` | `{id, ...meta}` / `{id} -> KeyOpResult` | |
| `keys:activate` | `{id} -> KeyOpResult` | W12 steps 1-4, incl. automatic revert |
| `keys:probe` | `{id} or {active: KeyKind} -> KeyProbe` | W13 |
| `keys:clear-active` | `{kind} -> KeyOpResult` | |
| `settings:get` / `settings:set` | `-> Settings` / `{patch} -> Settings` | deep merge, validated, atomic write |
| `state:get` / `state:set` | `-> AppState` / `{patch} -> void` | renderer debounces 500 ms; main flushes on quit |
| `pty:spawn` | `{spec: PtySpec, cols, rows} -> {ok, id} or error` | main builds argv (below) |
| `pty:write` / `pty:resize` / `pty:ack` / `pty:kill` | `{id, data}` / `{id, cols, rows}` / `{id, chars}` / `{id}` | fire-and-forget (`send`, not `invoke`) |
| `pty:list` / `pty:attach` | `-> PtyInfo[]` / `{id} -> {info, replay}` | reattach after a renderer reload; replay = 256 KiB ring |
| `web:url` | `{target: 'serve' \| 'opencode', path} -> {ok, url, partition}` | main sets cookies first; renderer puts `url` in `<webview src>` |
| `web:theme` | `{css} -> void` | CSS main injects into every guest on `dom-ready` |
| `update:get` / `update:check` / `update:download` / `update:install` | `-> AppUpdateState` | |
| `shell:open-external` / `shell:show-item` | `{url}` / `{path}` | scheme allowlist |
| `shell:pick-folder` / `shell:path-info` / `shell:save-file` | `{defaultPath?} -> string or null` / `{path, list?} -> PathInfo` / `{name, text} -> string or null` | |
| `window:action` | `{action: 'minimize' \| 'maximize' \| 'close' \| 'fullscreen' \| 'zoom', value?}` | |
| `notify:show` | `{id, title, body, route}` | click -> `notify:click` |
| `log:write` | `{level, msg}` | renderer errors into `userData/logs/app.log` |

| Event (main -> renderer) | Payload |
|---|---|
| `backend:state` | `BackendState` |
| `live:update` | `{topic, sig, data}` (only when changed) |
| `live:health` | `{ok, failures, nextInMs}` |
| `job:output` / `job:exit` | `{id, chunk}` / `{id, code}` |
| `keys:changed` | `KeyVaultState` |
| `settings:changed` | `Settings` |
| `pty:data` / `pty:exit` | `{id, data}` / `{id, code, signal}` |
| `update:state` | `AppUpdateState` |
| `window:state` | `{maximized, fullscreen, focused, visible}` |
| `power:state` | `{onBattery, idle}` |
| `notify:click` | `{route}` |
| `menu:command` | `{command}` (native menu -> command registry) |
| `app:will-quit` | `{}` -> renderer flushes `state:set`, then calls `window:action close` |

**PTY argv allowlist** (`src/main/pty/spec.ts`; the only place a command line is built):
| `PtySpec.kind` | Command | Flags from the spec |
|---|---|---|
| `agent` | `bash <root>/harness` | `--yolo`, `--normal`, `--thoughts`, `--net` |
| `host` | `bash <root>/harness host` | `--ticket <KEY>` (KEY must match the ticket-key pattern), `--continue`, `--session <id>` (session-id pattern); env `HARNESS_HOST_CONFIRM=1` only when consent is recorded |
| `shell` | `bash <root>/harness shell` | none |
| `plain` | the user's `$SHELL` (fallback `/bin/bash`; Windows: Git Bash when found) | none |
Env: the app's env plus `TERM=xterm-256color`, `COLORTERM=truecolor`, `HARNESS_OPEN=0`. `--continue` / `--session` are documented for `harness host` only (architecture/harness-cli.md); `PtySpec.resume` is therefore honoured for `host` and ignored for `agent` unless L2 confirms the container path passes them to opencode, in which case the dialog and the restore bar offer it there too. L2 confirms every flag against the CLI before wiring it; an unsupported one is dropped from the dialog, not passed through.

### 5.3 Types (normative; extracted verbatim by the parent)

```ts
// FILE: desktop/src/shared/ipc.ts
import type { ActionResult, ConfigRow, ReadResult, Topic } from './api';

export type BackendPhase = 'locating' | 'no-install' | 'needs-config' | 'starting' | 'up'
  | 'reconnecting' | 'down' | 'needs-login' | 'incompatible';
export interface BackendState {
  phase: BackendPhase; root: string | null; url: string | null; port: number | null;
  owned: boolean; pid: number | null; stale: boolean; apiVersion: number | null;
  missing: string[];                      // needs-config: required .env keys that are empty ('.env' when the file is absent)
  hostConsent: boolean;
  problem: { code: string; message: string; unlockUrl?: string } | null;
  logTail: string[];                      // last lines of the owned serve's output
  since: number;
}
export type BackendAction =
  | { action: 'start' | 'restart' | 'stop' | 'retry' | 'create-env' | 'consent-host' | 'create-code' }
  | { action: 'set-root'; root: string }
  | { action: 'login'; code: string };

export interface AppInfo {
  version: string; platform: NodeJS.Platform; arch: string; packaged: boolean; e2e: boolean;
  userData: string; home: string; electron: string; chrome: string;
  keyStorage: { secure: boolean; backend: string };
  reducedMotion: boolean; onBattery: boolean;
}

export type KeyKind = 'PROXY_API_KEY' | 'OAI_API_KEY' | 'TRACKER_TOKEN' | 'CHATGPT_COOKIE_STRING' | 'ACCESS_CODE';
export type ProbeState = 'ok' | 'locked' | 'rejected' | 'unknown';
export interface KeyProbe { state: ProbeState; at: number; message: string; unlockUrl?: string }
export interface KeyMeta {
  id: string; kind: KeyKind; label: string; last4: string; active: boolean; superseded: boolean; team: boolean;
  addedAt: number; createdAt: number | null; expiresAt: number | null; lastUnlockAt: number | null;
  locksAt: number | null;                 // computed: max(createdAt, lastUnlockAt) + 14 days; null when unknown
  probe: KeyProbe | null;
}
export interface KeyVaultState {
  secure: boolean; backend: string; storeInactive: boolean;
  keys: KeyMeta[];
  env: Partial<Record<KeyKind, { set: boolean; chars: number; inVault: boolean }>>;  // what .env holds, from config list
}
export type KeyOpResult = { ok: true; state: KeyVaultState; msg: string; reverted?: boolean }
  | { ok: false; error: { code: string; message: string }; state: KeyVaultState };

export interface PtySpec {
  kind: 'agent' | 'host' | 'shell' | 'plain'; cwd: string;
  yolo?: boolean; normal?: boolean; thoughts?: boolean; net?: boolean;
  ticket?: string; resume?: 'last' | { session: string };
}
export interface PtyInfo { id: string; spec: PtySpec; pid: number; startedAt: number; exited: { code: number; signal: number } | null }

export interface AppUpdateState {
  phase: 'unsupported' | 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error';
  current: string; next: string | null; notes: string; percent: number; reason: string; checkedAt: number | null;
  releasePage: string;
}
export interface HarnessUpdateState { branch: string; local: string; available: boolean | null; text: string; checkedAt: number | null }
export interface PathInfo { exists: boolean; dir: boolean; git: { branch: string } | null; entries?: string[] }

export interface Settings {
  appearance: { theme: 'dark' | 'light' | 'system'; accent: string; density: 'comfortable' | 'compact';
    uiScale: number; glass: boolean; seeThrough: number; reduceMotion: 'system' | 'on' | 'off' };
  background: { config: import('./bg').BgConfig; onBlur: 'run' | 'dim' | 'pause'; onBattery: 'run' | 'static';
    rotate: 'never' | 'daily' | 'launch' };
  terminal: { fontFamily: string; fontSize: number; lineHeight: number; cursorBlink: boolean; scrollback: number;
    confirmClose: boolean; confirmPaste: boolean; webgl: boolean };
  sessions: { restore: 'tabs' | 'tabs-and-resume' | 'none'; defaultKind: PtySpec['kind'] };
  queue: { startMode: 'container' | 'host' | 'single' };
  backend: { installRoot: string | null; port: number; onQuit: 'keep-if-busy' | 'stop' | 'keep'; hostConsent: boolean };
  updates: { auto: boolean; harnessCheck: boolean };
  notifications: { needsInput: boolean; ticketDone: boolean; runEnded: boolean; key: boolean; update: boolean };
  keymap: Record<string, string>;          // command id -> chord; only overrides are stored
}
export interface AppState {
  version: 1;
  window: { x?: number; y?: number; width: number; height: number; maximized: boolean; fullscreen: boolean };
  shell: { sidebar: number; sidebarOpen: boolean; collapsed: Record<string, boolean>; activeView: string };
  dock: unknown;                           // dockview toJSON()
  panels: Record<string, unknown>;         // panel id -> that panel's serialize()
  closed: unknown[];                       // reopen stack (<= 20)
  drafts: Record<string, string>;          // "<scope>:<id>:<field>" -> text
  recents: { projects: string[]; commands: string[] };
}

export interface BridgeEvents {
  'backend:state': BackendState; 'live:update': { topic: Topic; sig: string; data: unknown };
  'live:health': { ok: boolean; failures: number; nextInMs: number };
  'job:output': { id: string; chunk: string }; 'job:exit': { id: string; code: number };
  'keys:changed': KeyVaultState; 'settings:changed': Settings;
  'pty:data': { id: string; data: string }; 'pty:exit': { id: string; code: number; signal: number };
  'update:state': AppUpdateState; 'window:state': { maximized: boolean; fullscreen: boolean; focused: boolean; visible: boolean };
  'power:state': { onBattery: boolean; idle: boolean }; 'notify:click': { route: string };
  'menu:command': { command: string }; 'app:will-quit': Record<string, never>;
}
export interface Bridge {
  on<K extends keyof BridgeEvents>(event: K, cb: (payload: BridgeEvents[K]) => void): () => void;
  app: { info(): Promise<AppInfo> };
  backend: { get(): Promise<BackendState>; action(a: BackendAction): Promise<BackendState> };
  api: { get<T>(path: string, query?: Record<string, string | number | boolean>, sig?: string): Promise<ReadResult<T>>;
    post(path: string, body?: Record<string, unknown>): Promise<ActionResult>;
    bytes(path: string): Promise<{ ok: boolean; mime: string; data: Uint8Array }> };
  live: { set(topics: Topic[]): Promise<void> };
  harness: { configList(): Promise<{ ok: boolean; rows: ConfigRow[]; message: string }>;
    configSet(key: string, value: string): Promise<{ ok: boolean; msg: string }>;
    job(job: 'doctor' | 'check-updates' | 'upgrade' | 'serve-stop'): Promise<{ id: string }>;
    updateGet(): Promise<HarnessUpdateState> };
  keys: { list(): Promise<KeyVaultState>;
    add(k: { kind: KeyKind; label: string; secret: string; createdAt?: number; expiresAt?: number; team?: boolean; activate?: boolean }): Promise<KeyOpResult>;
    update(k: { id: string; label?: string; createdAt?: number | null; expiresAt?: number | null; team?: boolean }): Promise<KeyOpResult>;
    remove(id: string): Promise<KeyOpResult>; activate(id: string): Promise<KeyOpResult>;
    probe(target: { id: string } | { active: KeyKind }): Promise<KeyProbe>; clearActive(kind: KeyKind): Promise<KeyOpResult> };
  settings: { get(): Promise<Settings>; set(patch: DeepPartial<Settings>): Promise<Settings> };
  state: { get(): Promise<AppState>; set(patch: Partial<AppState>): Promise<void> };
  pty: { spawn(spec: PtySpec, cols: number, rows: number): Promise<{ ok: true; id: string } | { ok: false; error: { code: string; message: string } }>;
    write(id: string, data: string): void; resize(id: string, cols: number, rows: number): void;
    ack(id: string, chars: number): void; kill(id: string): void;
    list(): Promise<PtyInfo[]>; attach(id: string): Promise<{ info: PtyInfo; replay: string } | null> };
  web: { url(target: 'serve' | 'opencode', path: string): Promise<{ ok: boolean; url: string; partition: string }>;
    theme(css: string): Promise<void> };
  update: { get(): Promise<AppUpdateState>; check(): Promise<AppUpdateState>; download(): Promise<AppUpdateState>; install(): Promise<void> };
  shell: { openExternal(url: string): Promise<boolean>; showItem(path: string): Promise<void>;
    pickFolder(defaultPath?: string): Promise<string | null>; pathInfo(path: string, list?: boolean): Promise<PathInfo>;
    saveFile(name: string, text: string): Promise<string | null> };
  window: { action(action: 'minimize' | 'maximize' | 'close' | 'fullscreen' | 'zoom', value?: number): Promise<void> };
  notify: { show(n: { id: string; title: string; body: string; route: string }): Promise<void> };
  log(level: 'info' | 'warn' | 'error', msg: string): void;
}
export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };
declare global { interface Window { harness: Bridge } }
```

### 5.4 Background engine contract (L4 implements, L3 consumes)

```ts
// FILE: desktop/src/shared/bg.ts
export type BgGroup = 'static' | 'flow' | 'chaos' | 'automata' | 'fields' | 'system';
export interface BgParamSpec {
  key: string; label: string; type: 'range' | 'select' | 'toggle';
  min?: number; max?: number; step?: number; options?: { value: string; label: string }[];
  default: number | string | boolean;
}
export interface BgDescriptor {
  id: string; name: string; group: BgGroup; blurb: string;
  renderer: 'css' | 'worker2d' | 'webgl2';   // preferred; the engine may fall back
  dynamic: boolean; cost: 1 | 2 | 3;         // DR(e) cost class
  params: BgParamSpec[];
}
export interface BgConfig {
  id: string; seed: number; params: Record<string, number | string | boolean>;
  intensity: number;                          // 0..1 opacity/contrast of the field
  speed: number;                              // 0..2, 1 = designed speed
  fpsCap: 15 | 24 | 30 | 60;
}
export interface BgTheme { mode: 'dark' | 'light'; base: string; fg: string; accent: string; accent2: string }  // resolved CSS colours
export type BgRunState = 'run' | 'dim' | 'pause';
export interface BgStats {
  fps: number; frameMs: number; renderer: 'css' | 'worker2d' | 'webgl2' | 'main2d';
  tier: 0 | 1 | 2 | 3;                        // 0 = full, 3 = static fallback
  downgraded: boolean; reason: string;
}
export interface BgEngine {
  catalog(): BgDescriptor[];
  set(config: BgConfig, opts?: { fadeMs?: number }): Promise<void>;   // commit
  preview(config: BgConfig | null): void;                             // null reverts to the committed one
  setTheme(theme: BgTheme): void;
  setRunState(state: BgRunState): void;
  setReducedMotion(on: boolean): void;        // on: dynamic backgrounds render one settled frame and stop
  thumbnail(id: string, w: number, h: number, theme: BgTheme): Promise<string>;  // blob: URL, cached
  onStats(cb: (s: BgStats) => void): () => void;
  dispose(): void;
}
export interface BgCreateOpts { workerUrl: string }
// Implemented by desktop/src/background/index.ts:
//   export function createBackground(root: HTMLElement, opts: BgCreateOpts): BgEngine
//   export const DEFAULT_BG: BgConfig
```
L3 owns: where `#bg-root` sits, when `setRunState` is called (window `visible`/`focused`, `power:state`, a modal open, a perf-sensitive drag), the picker UI built from `catalog()`, persisting `BgConfig` in `Settings.background`. L4 owns everything inside `root` (it creates and replaces its own canvas and CSS layers) and its own resize observation.

---

## 6. File layout, dependencies, build

### 6.1 Layout and ownership
```
desktop/
  package.json  package-lock.json  tsconfig.json        PARENT (pre-step)
  src/shared/{api.ts,ipc.ts,bg.ts}                      PARENT (extracted from this plan; read-only to legs)
  docs/PLAN.md                                          this file
  electron-builder.yml  playwright.config.ts            L2
  build/                 icons, mac entitlements        L2
  ci/desktop-ci.yml  ci/desktop-release.yml             L2  (for the owner to move into .github/workflows/)
  scripts/{build.mjs,dev.mjs,run-tests.mjs,e2e.mjs}     L2
  src/main/                                             L2
    index.ts window.ts protocol.ts security.ts ipc.ts menu.ts notify.ts power.ts log.ts store.ts
    settings.ts state.ts cli.ts updater.ts harness-update.ts webview.ts
    backend/{manager.ts,locate.ts,classify.ts,http.ts,poller.ts,auth.ts}
    keys/{vault.ts,probe.ts,lifecycle.ts}
    pty/{host.ts,client.ts,spec.ts}                     host.ts = utilityProcess entry
    **/*.test.ts
  src/preload/index.ts                                  L2
  src/renderer/                                         L3
    index.html  main.tsx
    styles/{tokens.css,base.css,glass.css,dockview.css,xterm-theme.ts}
    core/{bridge.ts,store.ts,commands.ts,keymap.ts,drafts.ts,format.ts,toast.ts,notify-rules.ts}
    ui/        Button Input Combobox Chips Tabs Menu Dialog Tooltip Pill Skeleton EmptyState ErrorPanel VirtualList Markdown
    shell/     App TitleBar Rail Sidebar StatusBar Dock panels Palette Onboarding BackendPopover
    views/     home/ queue/ ticket/ sessions/ transcript/ terminal/ web/ findings/ vault/ jira/ debug/
               settings/ keys/ updates/ background/     (each view folder holds its *.tsx and a pure model.ts)
    **/*.test.ts
  src/background/                                       L4
    index.ts engine.ts worker.ts protocol.ts catalog.ts governor.ts prng.ts noise.ts palette.ts
    gl/{program.ts,quad.ts,fbo.ts}
    effects/{static.ts,silk.ts,aurora.ts,flowfield.ts,attractor.ts,tracers.ts,grayscott.ts,life.ts}
    demo.html demo.ts                                   standalone page: visual check + perf probe
    **/*.test.ts
  e2e/                                                  L2, except the two folders below
    fixtures.ts                                         launch helper + mock control (7.5)
    mock-serve/{server.mjs,scenarios.mjs,data/*.json,fake-harness}
    platform/*.spec.ts                                  L2: boot, security, backend, pty, keys, updater, restore, crash
    real-serve.spec.ts  perf.spec.ts                    L2
    views/                                              L3: *.spec.ts for every view + their extra fixtures
    bg/                                                 L4: background specs
architecture/desktop.md                                 L2 (new, short, structural)
architecture/harness-cli.md                             L1 (serve section: JSON API)
serve/api.py  serve/test_api.py  serve/pub.py(hook)     L1
tests/unit_serve_test.sh                                L1 (one line: run test_api)
```
Parent-only edits at integration: `.gitignore` (`desktop/node_modules/`, `desktop/out/`, `desktop/dist/`, `desktop/test-results/`), one link line in `architecture/README.md`, `tests/INVENTORY.md` / `tests/COVERAGE.md` rows (other work is editing those files now; L1 hands the rows over as text).

### 6.2 `package.json` (parent creates this verbatim, then `npm install`)
```json
{
  "name": "harness-desktop",
  "productName": "Harness",
  "version": "0.1.0",
  "private": true,
  "description": "Desktop app for harness",
  "author": "harness",
  "homepage": "https://github.com/HandelSim/harness",
  "main": "out/main/index.cjs",
  "engines": { "node": ">=22" },
  "scripts": {
    "typecheck": "tsc --noEmit -p tsconfig.json",
    "build": "node scripts/build.mjs",
    "dev": "node scripts/dev.mjs",
    "start": "electron .",
    "test": "node scripts/run-tests.mjs",
    "e2e": "node scripts/e2e.mjs",
    "check": "npm run typecheck && npm run test",
    "rebuild:native": "electron-builder install-app-deps",
    "dist": "node scripts/build.mjs --prod && electron-builder --publish never",
    "dist:dir": "node scripts/build.mjs --prod && electron-builder --dir --publish never"
  },
  "dependencies": {
    "electron-updater": "6.8.11",
    "node-pty": "1.1.0"
  },
  "devDependencies": {
    "@fontsource-variable/inter": "5.3.0",
    "@fontsource-variable/jetbrains-mono": "5.3.0",
    "@playwright/test": "1.65.1",
    "@preact/signals": "2.11.4",
    "@types/node": "22.20.5",
    "@xterm/addon-fit": "0.11.0",
    "@xterm/addon-search": "0.16.0",
    "@xterm/addon-serialize": "0.14.0",
    "@xterm/addon-unicode11": "0.9.0",
    "@xterm/addon-web-links": "0.12.0",
    "@xterm/addon-webgl": "0.19.0",
    "@xterm/xterm": "6.0.0",
    "diff2html": "3.4.56",
    "dockview-core": "8.9.1",
    "dompurify": "3.4.18",
    "electron": "45.4.1",
    "electron-builder": "26.18.0",
    "esbuild": "0.28.2",
    "marked": "18.2.1",
    "preact": "11.2.0",
    "simplex-noise": "4.0.3",
    "typescript": "7.0.2"
  }
}
```
- Only the two modules that stay external to the bundles are `dependencies` (so only they are packed into the asar); everything else is bundled by esbuild and is a devDependency.
- Left out on purpose: vite, vitest, @electron/rebuild, React, `@xterm/addon-clipboard`, `@xterm/addon-progress` (not needed for v1).
- No `postinstall` script: `scripts/` does not exist when the parent installs. Parent pre-step, in order: `cd desktop && nice -n 19 npm install` (downloads the Electron binary; node-pty compiles from source with the host gcc/python3) then `nice -n 19 npm run rebuild:native`. node-pty 1.1.0 is N-API, so the rebuild is belt and braces; L2's first task is the smoke test that loads it inside an Electron utilityProcess. `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` (only Electron is driven).
- `tsconfig.json` (parent): `strict`, `noEmit`, `target ES2023`, `module ESNext`, `moduleResolution Bundler`, `jsx react-jsx`, `jsxImportSource preact`, `lib [ES2023, DOM, DOM.Iterable, WebWorker]`, `types [node]`, `skipLibCheck`, `include ["src","e2e","scripts"]`. **Verified**: the three contract blocks of 4.7, 5.3 and 5.4, extracted as below, pass `tsc --noEmit` with exactly these options under typescript 7.0.2 + @types/node 22.20.5 on this host. TypeScript is used only for type checks; esbuild compiles.

### 6.3 Build pipeline (`scripts/build.mjs`, esbuild API, one process, all targets in parallel)
| Bundle | Entry -> Output | Settings |
|---|---|---|
| main | `src/main/index.ts` -> `out/main/index.cjs` | `platform node`, `format cjs`, `target node24`, external `electron`, `node-pty`, `electron-updater` |
| pty-host | `src/main/pty/host.ts` -> `out/main/pty-host.cjs` | same; external `node-pty` |
| preload | `src/preload/index.ts` -> `out/preload/index.cjs` | `format cjs`, single file, external `electron` only (sandboxed preloads cannot `require` anything else) |
| renderer | `src/renderer/main.tsx` -> `out/renderer/assets/` | `platform browser`, `format esm`, `splitting`, `target chrome156`, `jsx automatic` + `jsxImportSource preact`, loaders `.woff2 file`, `.glsl text`; views are `import()`ed so first paint loads the shell only; `index.html` copied to `out/renderer/` |
| bg worker | `src/background/worker.ts` -> `out/renderer/assets/bg-worker.js` | `format esm`, no splitting, **stable name** (L3 passes `workerUrl: 'assets/bg-worker.js'`) |
| bg demo | `src/background/demo.ts` + `demo.html` -> `out/renderer/bg-demo.html` | dev builds only |
- `--prod`: minify, no sourcemaps for preload/main, `define __DEV__=false`. Dev: sourcemaps, `--watch` in `dev.mjs`, which restarts Electron on main/preload changes and reloads the window on renderer changes.
- Tests: `run-tests.mjs` bundles every `src/**/*.test.ts` to `out/test/**/*.test.mjs` (`platform node`, `format esm`) and runs `node --test out/test`. Test files import only pure modules (no `electron`, no DOM); DOM-bound code is covered by e2e.
- `e2e.mjs`: build, then `playwright test`, wrapped in `xvfb-run -a` when on Linux with no `DISPLAY`.
- Packaging (`electron-builder.yml`): `appId dev.harness.desktop`; `files: [out/**, package.json, "!out/test/**"]`; `asar: true`, `asarUnpack: ["**/node_modules/node-pty/**"]` (native module + spawn helper); `npmRebuild: true` (rebuilds node-pty for the target at pack time); Linux `AppImage` + `dir` (arm64, x64), mac `dmg` + `zip`, Windows `nsis`; `publish: { provider: generic, url: <D10 URL> }`. On this host only `dist:dir` is an acceptance target; installers are built by `ci/desktop-release.yml` on native runners.
- Release workflow (`ci/desktop-release.yml`, `workflow_dispatch` with a version input): build matrix -> upload artifacts + `latest*.yml` to the rolling release **`desktop-latest`** and to an immutable `desktop-vX.Y.Z`, both tagged on the orphan branch `desktop-releases` (`gh release create --target <orphan sha>`), never on `dev`/`main`.

### 6.4 Environment variables (every configurable value)
`HARNESS_DESKTOP_ROOT` (install root), `HARNESS_DESKTOP_SERVE_URL` (attach-only backend), `HARNESS_DESKTOP_SERVE_PORT`, `HARNESS_DESKTOP_USER_DATA` (userData dir), `HARNESS_DESKTOP_UPDATE_URL` (update feed; with it set, the updater also runs unpackaged via `forceDevUpdateConfig`), `HARNESS_DESKTOP_NO_UPDATE=1`, `HARNESS_DESKTOP_DISABLE_GPU=1`, `HARNESS_DESKTOP_LOG_LEVEL`, `HARNESS_DESKTOP_E2E=1` (no single-instance lock, native dialogs and OS notifications are stubbed and recorded, no update timers). Nothing else is read from the environment; no path, port, URL or name is hardcoded outside `src/main/settings.ts` defaults.

---

## 7. Implementation: four parallel legs

### 7.0 Parent pre-step, M0, integration
**Pre-step (parent, before any leg starts)**
1. Write `desktop/package.json` (6.2) and `desktop/tsconfig.json`; extract the three `// FILE:` blocks (4.7, 5.3, 5.4) verbatim into `desktop/src/shared/`, from the repo root:
```bash
python3 - <<'PY'
import os, re
t = open('desktop/docs/PLAN.md', encoding='utf-8').read()
for m in re.finditer(r"```ts\n(// FILE: (desktop/src/shared/\w+\.ts)\n.*?)```", t, re.S):
    os.makedirs(os.path.dirname(m.group(2)), exist_ok=True)
    open(m.group(2), 'w', encoding='utf-8').write(m.group(1))
PY
```
2. `cd desktop && nice -n 19 npm install && nice -n 19 npm run rebuild:native`; confirm `npx tsc --noEmit` passes with only `src/shared` present.
3. Add the `.gitignore` lines (6.1).

**M0 (each leg's first ~20 minutes: land a stub so the tree builds end to end and nobody waits)**
- L1: the pub.py hook, `/status`, `/poll` with `status` and `queue`.
- L2: `scripts/build.mjs` (a missing entry of another leg is skipped with a warning in dev, fatal with `--prod`), main + window + `app://` + preload exposing **every** `Bridge` member (unbuilt ones answer `{ok:false,error:{code:'unavailable'}}`), mock serve with `/healthz`, `/status`, `/poll`, `e2e/fixtures.ts` and a boot spec.
- L3: `index.html` + `main.tsx` mounting the shell skeleton and setting `data-ready`.
- L4: `index.ts` exporting `createBackground` and `DEFAULT_BG` with the CSS static background only.

**Integration (parent, after the legs)**: `npm run check`; `python3 serve/test_api.py --dump desktop/e2e/mock-serve/data` (replaces hand-written mock data with real api.py output); `npm run e2e`; `bash tests/unit_serve_test.sh`; `npm run dist:dir` + packaged smoke; `architecture/README.md` link; hand-over rows for `tests/INVENTORY.md` / `COVERAGE.md`; commit.

### 7.1 L1: serve JSON API
- **Owns**: `serve/api.py`, `serve/test_api.py`, the hook lines in `serve/pub.py` (4.1), `architecture/harness-cli.md` (new "JSON API (`/api/v1`)" subsection under `## serve`: hook, envelope, endpoint list, the answer-form field mapping, every `unknown` shape of 4.7 spelled out), `tests/unit_serve_test.sh` (add `test_api` to the unittest line).
- **Read-only**: `serve/runs.py`, `serve/*_view.py`, `serve/test_pub.py`, `scripts/ticket_queue.py` (other work is editing it), `desktop/src/shared/api.ts`.
- **Implements**: all of section 4. **Consumes**: nothing from other legs.
- **Order**: M0, P0 reads, all actions, P1 reads, `--dump`, P2.
- **Acceptance**
  - `cd serve && python3 -m unittest -q test_api test_pub` and `bash tests/unit_serve_test.sh` pass on Python 3.9; `python3 -m py_compile serve/api.py`.
  - pub.py diff <= 30 lines; `test_pub.py` unchanged and green (no behaviour change on existing routes).
  - Every P0/P1 endpoint returns the 4.7 shape on a fixture state dir; P2 is implemented or answers 501.
  - No secret value in any response except `/opencode` (password) and `/settings/access` add (`data.code`), both session-gated.
  - `python3 serve/test_api.py --dump DIR` writes one JSON file per read endpoint/topic from the fixture state dir.
- **Tests** (`serve/test_api.py`, live server on an ephemeral loopback port like `test_pub.py`; state dirs built in `tempfile`)
  - Envelope and sig: stable across two calls; changes when a `state.json` changes; `?sig=` gives `same`; no wall-clock value inside `data`.
  - `/poll`: mixed changed/same topics; unknown topic and a raising topic are isolated as `error`; more than 24 topics is 400; bad topic grammar is 400.
  - Auth: codes on, no cookie -> 401 JSON for GET and POST; after `POST /login` -> 200. Codes off -> 200.
  - CSRF: POST with a foreign `Origin` -> 403; with no Origin -> accepted; non-JSON content type -> 415; body over the limit -> 413; malformed JSON -> 400; array body -> 400.
  - `cfg.runs is None`: runs endpoints 503, `/status` still 200.
  - Queue/ticket reads from written fixture tickets in every status (`queued`, `running`, `interrupted`, `needs-answers`, `plan-review`, `blocked`, `stopped`, `done`), incl. `stopping`, archived, a ticket dir with a missing or corrupt `state.json`, a removed ticket (404, poll `gone`).
  - Gates: for each status, `actions` equals what the constants and `*_refused` say (table-driven).
  - Actions: with `ticket_action` / `queue_action` monkeypatched to capture, every name in `TICKET_ACTIONS` and `QUEUE_ACTIONS` arrives with the right `{name:[value]}` form (incl. `stages`, `repo_modes`, `comments`, `answers` expansions); `(False,msg)` -> 409 `refused`; the `CHANGED` text -> 409 `changed`; unknown action -> 400; bad ticket key -> 404.
  - `/tickets/K/out/<name>`: offset tail, offset past EOF, truncated file (`reset`), a multibyte character split at the chunk edge, invalid UTF-8, traversal names (`..`, `/`, encoded) -> 404.
  - Sessions against a small SQLite fixture built by the test: list, head, messages `from`/`limit`, truncated tool output and `/parts/<pid>`, bad sid -> 404, send to a not-live session -> 409.
  - Settings: secrets rendered only as "(set, N chars)"; `set`/`clear` bridge to `v_<KEY>`/`clear_<KEY>` (captured).
  - Concurrency: 20 parallel `/poll` calls while a `state.json` is rewritten: no 500.
  - Perf: `/poll` of 6 unchanged topics on a 50-ticket fixture, median under 50 ms (assert bound 500 ms).

### 7.2 L2: Electron platform
- **Owns**: `desktop/src/main/**`, `desktop/src/preload/**`, `desktop/scripts/**`, `desktop/electron-builder.yml`, `desktop/playwright.config.ts`, `desktop/build/**`, `desktop/ci/**`, `desktop/e2e/**` except `e2e/views/**` and `e2e/bg/**`, `architecture/desktop.md` (new: process model, security model, backend manager state machine, IPC list, update model, where state lives).
- **Read-only**: `desktop/src/shared/**`, `desktop/src/renderer/**`, `desktop/src/background/**`, `harness` (CLI behaviour), `serve/**`.
- **Implements**: 1.1, 1.2, all of section 5, 6.3, 6.4, the fixture and mock contract of 7.5. **Consumes**: the API (as a client), `out/renderer` (serves it).
- **Order**: M0; HTTP client + poller + backend manager (attach first, then spawn); stores; pty-host; CLI runner + keys; webview hardening + serve/opencode auth; updater + harness update; packaging, CI files, architecture doc. e2e grows with each.
- **Acceptance**
  - `npm run typecheck`, `npm run test`, `npm run e2e -- e2e/platform` pass under xvfb on this host.
  - Boot to `data-ready` against the mock in under 3 s (asserted bound 8 s).
  - Security spec: `window.require` and `process` are undefined in the page; a renderer `fetch` to the mock's URL is blocked by CSP; top-level navigation and `window.open` are denied; a `<webview>` with a foreign `src` never attaches and one with a `preload` attribute loses it; an `api:get` for a path outside `/api/v1/` is refused.
  - No request from main to serve carries `Origin`, `Referer` or `Sec-Fetch-*` (the mock records headers).
  - PTY: plain shell round trip, resize, exit code, kill; a bounded 5 MB flood (`head -c 5000000 /dev/zero | base64`) finishes with the main process responsive (IPC ping under 100 ms throughout) and pty-host memory bounded by flow control; killing the pty-host marks tabs ended and a new spawn works.
  - Backend: every `BackendPhase` reached through mock scenarios and the fake `harness`; mock killed and restarted -> `reconnecting` -> `up` without a renderer reload; respawn limit holds; `X-Harness-Stale` sets `stale`.
  - Keys: add, activate, automatic revert on a rejected canonical probe, remove, clear, all against the fake `harness` and a fake upstream on loopback; a test greps every recorded renderer-bound IPC payload and the log files for the secret: none; `basic_text` backend -> single-key mode.
  - State: two launches on one `userData` restore bounds, layout and panel params; a corrupt `state.json` or `settings.json` is renamed aside and defaults load.
  - Updater: with `HARNESS_DESKTOP_UPDATE_URL` at the mock feed the state reaches `available`; unpackaged without it -> `unsupported` with a reason.
  - `npm run dist:dir` output starts under xvfb, reaches `data-ready`, and spawns a PTY (packaged node-pty smoke).
- **Unit tests** (node:test): `backend/classify` (captured serve failure texts), `backend/locate`, `backend/http` (against an in-process server: headers sent, timeouts, cookie jar, 401 re-login once, lost connection after send -> `unknown-result`, no POST retry), `backend/poller` (fake clock: cadence table of 4.5, backoff, topic union, `same`), `store` (atomic write, corrupt recovery, version migration), `settings` (validation, deep merge, unknown keys dropped), `keys/lifecycle` (`locksAt`, reminder schedule), `keys/probe` (classification table of INV 5, `unlock_url` extraction, base-URL trimming like `_api_base`), `pty/spec` (argv allowlist, injection attempts, bad cwd), `security` (URL and path allowlists), `cli` (`config list --tsv` parsing, `check-updates` text match, job output framing).

### 7.3 L3: renderer
- **Owns**: `desktop/src/renderer/**`, `desktop/e2e/views/**`.
- **Read-only**: `desktop/src/shared/**`, `desktop/src/background/index.ts` (API only), `desktop/e2e/fixtures.ts`, the design research.
- **Implements**: sections 2 and 3 in full. **Consumes**: `window.harness` (5.3), API types (4.7), `createBackground` (5.4), the e2e fixtures (7.5).
- **Order**: tokens, shell, dock + panel registry, topic store, command registry + palette; then Queue, Ticket, Transcript, Terminal, New session; then Onboarding, Backend popover, Keys, Settings, Updates; then Home, Sessions, Web, Chat; then Findings, Vault, Jira, Debug; then background picker, keymap editor, polish.
- **Rules**: every server-derived decision (which action is enabled, why not) comes from `Gate` values, never re-derived in TS. Server strings reach the DOM as text nodes, or through the one `Markdown` component (marked + DOMPurify); diff2html output also goes through DOMPurify. No `backdrop-filter` outside overlays (DR(c)). A live update never steals focus, moves scroll, or clears a draft.
- **Acceptance**
  - Every row of the parity matrix (3.16) is reachable; each workflow W1-W22 has at least one e2e spec.
  - Cached view switch p95 under 100 ms (asserted bound 250 ms on this host); no long task over 50 ms while typing in a composer with 1 Hz updates arriving.
  - Initial renderer chunk <= 350 KiB minified (fonts excluded); every view is a lazy chunk.
  - The page issues no request other than `app://` and `blob:` URLs (e2e listens on `request`).
  - Keyboard-only pass: every control reachable, focus ring visible, `F6` cycles regions, the terminal rule of W18 holds.
  - Endpoint 404/501 -> the view's "Open web version" fallback (4.6) works.
- **Unit tests** (node:test, pure `model.ts`/`core` modules): `core/store` (topic ref-count, `same`, `gone`, stale marking, resubscribe), `core/commands`, `core/keymap` (chord parsing, platform mods, terminal rule, conflicts, overrides), `core/drafts`, `core/format` (times in unknown formats, durations, bytes, cost), `core/notify-rules` (status transitions -> notifications, none on first load or restart), `shell/panels` (serialize/restore validation, singleton rules, reopen stack), `views/queue/model` (**the queue edge matrix**: every status x runner state -> pill, row actions, badges, counts; optimistic reorder reconciliation; parallel `on[]`; `interrupted`; `stopping`), `views/ticket/model` (plan-changed merge keeps typed answers and flags changed questions; comment caps 40 x 2000; retry choices), `views/transcript/model` (page merge by id, the re-request index rule, truncated parts, unknown part types), `views/keys/model` (countdown text, warning thresholds).
- **e2e** (`e2e/views/`): onboarding (all four failure branches), sessions-tabs, docking (mouse drag to centre/edge, keyboard splits, close/reopen, layout undo), queue-flow (add -> start -> live -> note -> send -> stop -> retry -> halt -> end -> result -> diff -> fix), queue-edges (every item of the W5 edge list), plan-review, transcript, findings, vault, jira, settings, keys, updates, palette, shortcuts, states (offline, empty, error, signed out), restore, fallback.

### 7.4 L4: background engine
- **Owns**: `desktop/src/background/**`, `desktop/e2e/bg/**`.
- **Read-only**: `desktop/src/shared/bg.ts`, the design research (e) and (h), `desktop/e2e/fixtures.ts`.
- **Implements**: 5.4. **Consumes**: nothing (L3 hands in `root`, theme and run state).
- **Catalog v1** (DR(e) ids): must ship B0 system/solid, B1 mesh gradient + B2 dot grid (CSS, the default), B3 silk (fBm domain warp, WebGL2), B5 curl-noise flow field, B6 Clifford / de Jong density, B7 Lorenz / Aizawa / Thomas / Halvorsen tracers, B8 Gray-Scott; stretch B4 aurora, B9 Life. B10-B12 and Lenia are out of v1.
- **Design fixed by this plan**
  - Renderer ladder: WebGL2 in the worker -> 2D in the worker -> main-thread 2D at 15 fps (no `transferControlToOffscreen`) -> CSS static. Context lost: rebuild on restore; two losses in 60 s -> next rung.
  - Governor (pure, unit-tested): rolling mean frame time over 60 frames; over 1.5x budget for 2 s -> lower render scale (0.5, 0.35, 0.25), then fps cap (60, 30, 15), then static. It never climbs back by itself (no oscillation); a config change resets it.
  - `pause` stops the frame loop entirely (zero frames, zero timers); `dim` = 15 fps and reduced intensity; the engine also pauses itself on `document.hidden`. Reduced motion: one settled frame, then stop.
  - Fixed-timestep simulation with a clamped `dt`, so speed does not depend on fps; all randomness from a seeded PRNG: same seed + params -> same sequence.
  - Low-resolution render (scale 0.25-0.5) upscaled by the compositor is the "blur" (DR(c)); no CSS `filter` on the canvas.
  - Thumbnails: the worker renders a settled frame into a second small OffscreenCanvas, `convertToBlob`, cached by id + theme.
- **Acceptance**
  - Unit tests pass; `bg-demo.html` cycles the whole catalog without console errors.
  - e2e (`e2e/bg/`): for each id, `set` resolves, stats arrive, no error; `pause` -> zero frames in 1 s; reduced motion -> zero frames after settling; `preview(x)` then `preview(null)` restores the committed config; a forced heavy config makes the governor downgrade within 3 s and report `downgraded`.
  - With any background at defaults the page's own `requestAnimationFrame` interval p95 stays under 25 ms on this host (the work is in the worker).
  - 20 s run: JS heap of page and worker does not grow more than 10 MB.
  - Worker bundle <= 150 KiB minified.
- **Unit tests**: `prng` (determinism, range), `noise` (range, continuity, seed), `governor` (ladder, no oscillation, pause, fake clock), `attractor` (every shipped preset of Clifford, de Jong, Lorenz, Aizawa, Thomas, Halvorsen stays finite and bounded for 1e5 steps; RK4 step against a hand-checked Lorenz step), `grayscott` (shipped presets stay within [0,1], no NaN after 500 steps on a 32x32 grid), `life` (blinker, glider, wrap), `catalog` (unique ids, defaults within range, `DEFAULT_BG` valid), `palette` (CSS colour parsing), `protocol` (worker message validation).

### 7.5 Shared interfaces between legs
- **Types**: `desktop/src/shared/{api,ipc,bg}.ts`, frozen (7.0).
- **Build entries** (L2's `build.mjs` expects exactly): `src/main/index.ts`, `src/main/pty/host.ts`, `src/preload/index.ts`, `src/renderer/main.tsx`, `src/renderer/index.html`, `src/background/worker.ts`, `src/background/demo.ts`, `src/background/demo.html`. Worker URL at runtime: `assets/bg-worker.js`.
- **DOM contract** (L3 provides, L2 and L4 specs rely on): `<html data-ready="1" data-phase data-theme>`; `#bg-root`; `data-testid` values `rail-<view>`, `view-<view>`, `tab-<panel id>`, `panel-<panel id>`, `palette`, `palette-input`, `status-backend`, `status-runner`, `status-key`, `status-bg`, `backend-popover`, `dialog-new-session`, `dialog-add-tickets`, `queue-add`, `queue-start`, `queue-halt`, `queue-end`, `ticket-row-<KEY>`, `ticket-tab-<name>`, `ticket-action-<action>`, `composer-input`, `composer-send`, `terminal-<pty id>`, `transcript-<sid>`, `onboarding-step-<n>`, `key-add`, `key-row-<id>`, `bg-picker`, `bg-item-<id>`, `toast`, `empty-state`, `error-panel`. View ids: `home, queue, sessions, findings, vault, jira, debug, keys, updates, settings`.
- **e2e fixture** (`e2e/fixtures.ts`, L2): `test` extended with `mock`, `app`, `win` and `launch(opts)`:
  ```ts
  interface MockServe {
    url: string;
    scenario(name: 'healthy' | 'empty' | 'codes-on' | 'old-serve' | 'incompatible' | 'viewer' | 'stale'
      | 'queue-running' | 'needs-answers' | 'plan-changed' | 'refuse-all' | 'slow-actions' | 'flaky'): Promise<void>;
    setTopic(topic: string, data: unknown): Promise<void>;      // also what the matching GET endpoint answers
    onPost(path: string, reply: { status?: number; body?: unknown; delayMs?: number; drop?: boolean }): Promise<void>;
    fail(mode: 'none' | 'drop' | '500' | 'hang', count?: number): Promise<void>;
    requests(filter?: string): Promise<{ method: string; path: string; headers: Record<string, string>; body: unknown }[]>;
    stop(): Promise<void>; start(): Promise<void>;              // crash/recovery tests
  }
  interface LaunchOpts { userData?: string; fakeHarness?: { env?: 'ok' | 'missing' | 'empty-key'; unlock?: 'ok' | 'locked' | 'rejected';
    serve?: 'ok' | 'fails' }; attach?: boolean; args?: string[] }
  ```
  `fake-harness` is a bash script that answers `config list --tsv`, `config set`, `unlock`, `check-updates`, `upgrade --no-prompt`, `serve` (execs the mock), `serve stop`, `serve code add`, `doctor`, steered by a JSON file in the temp install root. It lets onboarding, keys and updates run with no real install and no network.

---

## 8. Test plan

| Layer | What | Command | Runs where |
|---|---|---|---|
| Python unit | `serve/test_api.py` (7.1) + existing `test_pub.py` | `bash tests/unit_serve_test.sh` | here; existing CI already runs this script, so no `.github` change is needed |
| TS types | whole tree | `npm run typecheck` | here, CI |
| TS unit | `src/**/*.test.ts` via node:test (7.2-7.4 lists) | `npm run test` | here, CI |
| e2e, mock | Playwright `_electron.launch` under `xvfb-run -a`, mock serve + fake harness; platform, views, bg specs | `npm run e2e` | here, CI |
| e2e, real serve | `e2e/real-serve.spec.ts`: starts `python3 serve/pub.py` on an ephemeral loopback port with a temp `--state-dir`/`--queue-dir`/`--agent-data` and fixture tickets, app in attach mode; reads every P0/P1 endpoint through the UI and runs the docker-free actions (`note`, `move`, `archive`, `unarchive`, `remove`). No docker, no model calls. It is the drift alarm between the mock and the real API. | part of `npm run e2e` (skipped with a message when `python3` is absent) | here, CI |
| Perf | `e2e/perf.spec.ts` (below) | `npm run e2e -- e2e/perf.spec.ts` | here (bounds), CI (report only) |
| Packaged smoke | `dist:dir` binary boots, PTY works | `npm run dist:dir` + spec | here, CI matrix |

**Perf checks** (targets / asserted bounds on this software-GL host; numbers land in `desktop/test-results/perf.json`; bounds overridable by env):
- Startup to `data-ready`: 3 s / 8 s.
- Cached view switch (command dispatch to the frame after the view's `data-view-ready`), p95 of 30: 100 ms / 250 ms.
- Frame time with a background running: page `requestAnimationFrame` interval p95 while a 5,000-row virtual list auto-scrolls and B5 runs: 16.7 ms / 25 ms; worker `frameMs` reported.
- Transcript: 2,000 messages appended at 20 per second: no long task over 100 ms.
- Terminal flood (7.2) and `/poll` cost (7.1).

**Electron under xvfb**: the fixture launches with `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`, and adds `--no-sandbox` only when `HARNESS_DESKTOP_E2E_NO_SANDBOX=1` or the first launch dies on the sandbox check **[U: L2 settles the exact flags in M0]**. WebGL-dependent assertions are skipped with a visible message if no WebGL2 context can be made; the engine's fallback rung is then what gets tested.

**CI** (`desktop/ci/desktop-ci.yml`, for the owner to move into `.github/workflows/`; path filter `desktop/**`, `serve/**`): node 22 -> `npm ci` -> `npm run check` -> `npm run build` -> `xvfb-run -a npm run e2e` (mock + real serve) -> upload Playwright traces and `perf.json`; matrix job (linux x64, linux arm64, macOS, Windows): `npm run dist:dir` + packaged smoke. `desktop/ci/desktop-release.yml` is the release pipeline of 6.3.

**Not testable on this host, stated so nobody assumes otherwise**: real-GPU frame rates and the look of glass; macOS and Windows packaging; the install step of an app auto-update on any platform (the check and download states are tested against the mock feed); container-mode sessions (docker is forbidden here: `agent`/`shell` PTY kinds run only against the fake harness); a real upstream key probe (tested against a loopback fake upstream); opencode web inside the Chat panel (only the attach and Basic-auth plumbing is tested, against a stub page).

---

## 9. Top 5 risks, with the mitigation already in the design

1. **The backend cannot start in exactly the states where the user most needs the app** (no `.env`, empty required values, key locked or expired: `host_require_config` and the upstream probe abort `harness serve`). *Folded in*: onboarding, configuration and all key management run through main's CLI runner and never need serve (D11, 1.2, W1, W11-W14); a locked key restarts serve with `HARNESS_SKIP_AUTH_PROBE=1` so queue, history and settings stay usable; terminals are independent of serve. Consequence accepted: secrets pass on argv to `harness config set`, as the web UI already does.

2. **Contract drift between four legs working at once.** *Folded in*: three frozen, parent-owned contract files extracted from this plan; M0 stubs so the whole tree builds in the first 20 minutes; server-computed `Gate`s so business rules exist once (in Python); mock data regenerated from real `api.py` output at integration (`--dump`); a real-serve e2e spec as the drift alarm; an integer `status.api` checked at attach.

3. **Native parity is larger than one leg (15-25 data twins, rules buried in HTML renderers, runs.py off limits).** *Folded in*: P0/P1/P2 ordering with the queue and sessions first; the 404/501 fallback rule (4.6) and the Web panel (D9) mean a missing twin degrades one view to its themed web page instead of blocking the release; L1 reuses the renderers' predicates rather than editing or copying them. The owner's "at least everything the web page has" holds on day one either way.

4. **A "nice" GUI that is slow: glass, animated backgrounds, many terminals, webviews, 1 Hz updates.** *Folded in*: no `backdrop-filter` on chrome (translucency over a low-resolution field); backgrounds in a worker with a one-way governor, hard pause, and a static default; WebGL terminals capped at 8 with a DOM fallback; lazy view chunks and virtual lists; polling only for topics a visible panel subscribes to, IPC only on change, tails fetched on head change; PTY coalescing and flow control; no popout windows in v1. Limit stated: this host has no GPU, so CI asserts main-thread responsiveness and governor behaviour, and real frame rates need one manual pass on a real machine.

5. **Release and platform realities.** A desktop tag reachable from `dev`/`main` would become a `harness downgrade` target; the GitHub provider's "latest" breaks with mixed releases; unsigned macOS builds cannot self-update; Linux self-updates only as AppImage; node-pty is native on four targets; `.github/` is off limits. *Folded in*: generic provider on a rolling `desktop-latest` release tagged on an orphan branch (D10); workflows delivered under `desktop/ci/` for the owner to move; an explicit `unsupported` update state that says why and links the release page; `dist:dir` + packaged smoke here and installers on native CI runners; N-API node-pty, `asarUnpack`, and electron-builder's rebuild at pack time. Owner input still needed: signing certificates (macOS notarisation, Windows) and moving the two workflow files.
