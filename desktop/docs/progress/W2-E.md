# W2-E: native Home, Chat and Findings

Status: DONE (08:34 UTC). Unit tests 15/15, tsc clean, e2e 6/6 against the mock.

## Done
- **Home** (`views/home/HomeView.tsx`, `model.ts`, `home.css`; registered as `view.home`). Home was already the first
  view (`mountDock` and `resetLayout` open it), so dock.tsx needed no change. Topics: `home`, `status`, `queue`.
  - Health tiles: backend (phase, port, branch and sha), model proxy (up/down, detail, **Restart backend** when it is
    down), key (active key label and probe state; opens Keys), and vault (name, or "no vault selected").
  - Quick actions: New session, Add tickets, Chat, Open opencode web (the OS browser, only when serve has opencode
    web), Findings.
  - Queue summary: what the runner is doing, counts (running, waiting on you, queued, stuck, done), and up to 5
    tickets that need answers or a plan review (each opens its ticket panel).
  - Working now: `home.live` rows; Enter or a click opens the transcript.
  - Usage, last 24 h: the four tiles, last hour, reasoning and cached tokens, a per-model table with share bars
    (top 50), and the upstream remaining line.
  - Vault pages: filter shown when there are more than 8, capped at 50 with an "N more" line; each opens in the Web
    panel at `/pub/<rel>`.
  - Vault activity and Prompt modes.
  - States: skeleton while loading, ErrorPanel with retry, and an empty overview when the endpoint is unavailable.
    Each card has its own empty text. Vault cards are hidden when no vault is selected, as on the web page.
- **Chat** (`views/chat/ChatPanel.tsx`, `model.ts`, `chat.css`; registered as panel kind `chat`, command
  `chat.open` "Open chat"). serve's chat is opencode web, a browser UI with no JSON API. The native equivalent lists
  sessions from `sessions`, live (messageable) runs first, with a filter and an All/Live toggle (windowed with
  VirtualList). It embeds the imported `TranscriptPanel` for the picked session using a sub-ctx that is keyed by sid
  and maps the tab title to "Chat · <title>". The transcript's own composer posts `/sessions/<sid>/send` when the
  session's send gate is open. A session that is not live shows a read-only notice; a host session also gets "Open in
  opencode web" (`row.web`, or the opencode url). Keyboard: ↑/↓/Home/End pick a session and Enter focuses the
  composer. The picked sid and scope persist in the panel params. States: skeleton, ErrorPanel, unavailable → Web
  panel, and empty (with New session).
- **Findings** (`views/findings/FindingsView.tsx`, `model.ts`, `findings.css`; registered as `view.findings`).
  Topics: `findings`, `findings-agent`. Tabs: Open, Archived, Agent, What it learned.
  - Board: master/detail. The windowed list has checkbox, severity dot, title, source and age. The detail shows
    tags, where (path:line), from (a ticket link opens the ticket panel, otherwise "findings agent · project"), noted,
    body (ui/Markdown), evidence, the bug fields (ui, steps, expected, actual, trace, verdict), and rolling items
    with "Not this one" (archive `{id: item}`; 50 shown, then "N more").
  - Row Archive/Restore. Select all, Copy selected (md lines), Archive selected (`{sel:[...]}`), Restore selected.
  - Filter by text, source and category. Caps are 300 open and 200 archived, as on the web.
  - Keyboard: ↑/↓/Home/End select, Space ticks, Alt+↑/↓ moves (`/findings/move {key, before}`, unfiltered board
    only).
  - Agent card: state label and tone match `agent_card` (never started / STATES / stopped / not answering, with the
    stale heartbeat), the same info rows, the per-question counts table, and the last 20 passes (each opens its
    session). Watch it work / Its last pass uses `/follow?findings=1`. Stop asks first, with the web confirm text.
    Start… opens a folder form (saved folders, the last run's repos, "Other folders, one per line") that posts
    `{pick:1, dir, more}`; the same form has "Save for the next start" (`/folders {dir}`) and "Forget the choice"
    (`/folders {act:'reset'}`). Notes for every pass go to `/notes` (max 4000).
  - Head pill shows the agent state, with Start/Stop.
  - What it learned: notes grouped by repo with a filter (↑/↓), and a reader that loads
    `/findings/notes/<slug>/<name>` and renders it with ui/Markdown (skeleton, error with retry, empty).
- **Pure models + unit tests**: `views/{home,chat,findings}/model.test.ts`.
- **Additive shell lines**: `registry.ts` (3 `registerPanel` lines), `commands.ts` (`chat.open`, which uses a
  dynamic `import('./dock')`, so the import line is unchanged).

## Test results (real)
- `node scripts/run-tests.mjs views/chat/model views/findings/model views/home/model`: **15 pass, 0 fail**.
- `npx tsc --noEmit -p tsconfig.json`: **clean** for the whole project.
- Renderer build OK. The view CSS text is in the lazy chunks (`grep home-health out/renderer/assets/*.js`).
- e2e, all under the shared flock:
  - Run 1 (08:26): **0 tests ran**. W2-F's in-progress `server.mjs` read `data/*.json` through `import.meta.url`,
    which Playwright's CJS transform of fixtures.ts cannot load (`SyntaxError: Cannot use 'import.meta' outside a
    module`). W2-F fixed it at 08:29 (`import ... from './data/w2f-views.mjs'`).
  - Run 2 (08:29, a scratch copy of e2e/ with only that load line patched, against the same `out/`): 5 pass, 1 fail.
    `chat: no sessions` hit a palette race: the palette's open effect resets the query after Playwright's `fill`, so
    Enter ran the first unfiltered command ("Add tickets…"). Fixed in the spec, which now waits for the input to be
    focused and for "Open chat" to be the selected option before pressing Enter.
  - Run 3 (08:33, `node scripts/e2e.mjs --no-build e2e/views/{home,chat,findings}.spec.ts` in the repo):
    **6 passed (1.2 m)**.
  - Afterwards no electron, Xvfb or xvfb-run process was left from these runs. The scratch copy is deleted.

## Requests for W2-F
- None open. My specs need no new routes. They use `mock.setTopic('<path>', data)`, which `topicPath()` maps to
  `GET /api/v1/<path>`: `findings/notes/<slug>/<name>` and `sessions/<sid>/messages`. Topics are matched before
  W2-F's canned routes, so the two do not clash.
- FYI for any spec that drives the palette: wait for `palette-input` to be focused before `fill`, then wait for the
  selected option. `terminal-life.spec.ts` fills and presses Enter straight away, so it can hit the same race.

## Skipped / parity still missing vs the web pages
- Home: page `mtime` is not shown. The opencode web launch opens the OS browser (it asks for the opencode password),
  because WebPanel only loads `target: 'serve'`. Main's `web:url` already supports `opencode` with basic auth, so a
  WebPanel `target` param would allow an in-app opencode tab (not my file).
- Chat: no new opencode chat from the app (serve has no create/prompt-session route; New session starts a terminal
  agent instead). Sessions that are not live (host/interactive opencode sessions) cannot be messaged through serve,
  so the panel offers opencode web for them. There is no model or agent picker.
- Findings: "New ticket from selected" (the web board's Add form prefill) is not built; Copy selected covers the text.
  Drag reorder is replaced by Alt+↑/↓. The "Change the model in Settings" link is text, not a deep link. Child
  findings (`children`) are not listed separately.
- Not virtualised: Findings detail (one finding at a time), Home cards (all capped: live 20, pages 50, activity 30,
  models 50).

## Notes for the parent
- View CSS: main.tsx's comment says lazy chunks' CSS is not loaded, and main.tsx is not mine. Each view imports its
  `.css` with `with { type: 'text' }` (esbuild 0.28) and injects one `<style id="css-<view>">` on first import.
  To switch to the main.tsx convention, add `import './views/{home,chat,findings}/<x>.css'` there and drop the
  injection lines.
- architecture/desktop.md does not describe individual views, so no doc change.
- No dock.tsx change needed (Home is already the default first view).
- Palette (not my file): `Palette.tsx` resets `q` to the open prefix in a `useEffect`. Input that arrives before that
  effect runs is lost. A user cannot type that fast, but scripted input can. Setting `q` when the palette opens (in
  the `paletteOpen` setter) instead of in an effect would close the race.

## Files touched
- New: `desktop/src/renderer/views/home/{HomeView.tsx,model.ts,model.test.ts,home.css}`,
  `views/chat/{ChatPanel.tsx,model.ts,model.test.ts,chat.css}`,
  `views/findings/{FindingsView.tsx,model.ts,model.test.ts,findings.css}`,
  `desktop/e2e/views/{home,chat,findings}.spec.ts`, this file.
- Additive: `desktop/src/renderer/shell/registry.ts` (3 lines), `desktop/src/renderer/shell/commands.ts` (1 entry).
- No new files in `e2e/mock-serve/data/`. `server.mjs` was not edited.
