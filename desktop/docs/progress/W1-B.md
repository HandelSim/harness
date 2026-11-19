# W1-B progress (review pass 1: groups B, F, G)

## Contract for other legs
- Immediate poll: `backend.poller.pollNow()` (src/main/backend/poller.ts). W1-A: call it on powerMonitor `resume` / `unlock-screen`.
- Poller has a new optional dep `error?(topic, error)`. manager.ts (not mine) does not pass it, so topic errors go through
  `update(topic, '', { error })`, which is the shape `renderer/core/app.ts` already turns into `store.fail`. Side effect for
  the manager owner: while the `queue` topic errors, `manager.lastQueue` holds `{ error }` (no runner) until the topic recovers
  (the poller then re-sends the cached data). Wiring `error: (t, e) => d.live(t, '', { error: e })` in manager.ts avoids that.
- dock.tsx: `PanelCtx.onDispose(fn)` (additive), plus exported `paramClaimed(key, value)` and `paramValues(key)`.
  QueueView's EmbeddedTicket spreads `ctx`, so it inherits the parent's onDispose (harmless; TicketPanel does not use it).
- No edits to shared/ipc.ts, preload, api.ts.

## Done
1. M1 emit/apply half (poller.ts, store.ts)
   - `Poller.set()` re-sends cached data only for topics new to that owner (was: every cached topic on every set).
   - `TopicStore.apply()` returns early when `sig` equals the stored sig (clears error/stale/unavailable only, keeps data identity);
     field writes are batched so a real change is one render, not one per signal. `fail()` ignores an identical error.
   - Tests: `src/main/backend/poller.test.ts` "Poller.set (M1)" (3 selections emit exactly the 3 ticket topics; the old code
     re-emitted queue+status each time), `src/renderer/core/store.test.ts` (effect render counts: 10 same-sig re-sends = 0 renders,
     unrelated topic 0 renders).
2. H3 renderer half (dock.tsx, TerminalPanel.tsx, terminal/model.ts)
   - `Content.dispose()` runs `ctx.onDispose` hooks deferred (setTimeout 0, after fromJSON/clear finish); moves do not dispose
     (dockview uses skipDispose for moves, checked in dockview-core 8.9.1). Skipped when the dock element left the document
     (App unmount), so a page reload keeps ptys for re-attach.
   - TerminalPanel: `onDispose` kills its pty when alive and no live panel has `params.ptyId === id` (undo that recreates the
     panel re-attaches instead). beforeClose path sets alive=false, so no double kill.
   - `mountDock` reaps: `pty:list` minus every `params.ptyId` in the restored layout, only ptys started before
     `performance.timeOrigin`, and only when appState was loaded (startApp can time out at 1.5 s; then nothing is reaped).
   - Test: `unclaimedPtys` in terminal/model.test.ts. The dispose path itself is DOM/dockview-bound: covered by e2e only.
3. M1 list half + M7 (queue/virtual.tsx new, QueueView.tsx, transcript/model.ts, TranscriptPanel.tsx)
   - `VirtualList` (fixed 32 px rows, windowed above 200 rows, overscan 8, spacers keep the scroll height, `focusIndex`
     scrolls the keyboard selection into view). Rows are a memoised class component (selected / ticket JSON / runner JSON);
     handlers come through a ref, so rows get no new closures. Rows now carry `id="qrow-<key>"` (aria-activedescendant
     pointed at nothing before). data-testids unchanged; click / dblclick / drag reorder unchanged.
   - Transcript: `mergePage` keeps the old object of an unchanged re-read message and returns the same array when nothing
     changed; `load()` skips setMsgs then; messages are memoised on identity; only the newest 200 render, "Show earlier
     (N)" (`data-testid="transcript-earlier"`) adds 200.
   - Tests: queue/virtual.test.ts renders 2,000 rows through preact into a minimal fake DOM and asserts < 60 rows in the DOM,
     spacer + rows = 2000 * 32 px, focusIndex 1500 scrolls and renders that row; small lists render all rows.
     transcript/model.test.ts: 5,000-message merge identity, tail window.
4. M6, L13, pollNow (poller.ts)
   - Topic errors are forwarded once per distinct error; on recovery with unchanged data the cached data is re-sent (same sig,
     so the store clears the stale mark without re-rendering).
   - The 24-topic cap no longer drops topics: the poll is split into requests of 24.
   - Blurred + busy (running queue or working session) polls every 2 s (was 5 s). backend.test.ts cadence assertion updated.
   - `pollNow()`.

## Skipped / not done
- M6 "unsubscribe while ctx.visible is false": not done; splitting requests removes the silent drop, which was the bug.
  Background panels still poll their topics.
- Transcript windowing is tail-based ("Show earlier"), not scroll-windowed; messages have variable height.

## Test results (08:10 UTC)
- Unit: `run-tests.mjs main/backend/poller main/backend/backend renderer/core renderer/views renderer/shell`: 74/74 pass
  (new: poller.test.ts 6, store.test.ts 2, virtual.test.ts 3, terminal/model + transcript/model additions).
- The whole unit suite does not finish within 300 s on this host (slow background-engine tests); no failure seen before the
  timeout. `main/backend/manager` (W1-A's test): all 5 tests pass, but the process never exits (killed by the 280 s timeout).
  A probe run as a last test found no pending timers (so not the poller) and 2 PipeWrap + 2 TCPSocketWrap handles still open:
  child stdio / sockets left by those tests. For W1-A to fix; no leftover processes found afterwards.
- tsc: clean for every file of this leg and the new e2e spec. Remaining errors are only in W1-A files in progress
  (`src/main/pty/client.ts`, `client.test.ts`, `src/main/updater.ts`).
- e2e run 1 (`e2e/views/*.spec.ts`): shell.spec.ts 6/6 pass (queue row click/dblclick, transcript row opens the panel).
- e2e run 2: shell.spec.ts 6/6 pass. New `e2e/views/terminal-life.spec.ts` (H3) verified up to the reload step: the panel's pty
  survives a reload and re-attaches, an unclaimed pty from before the reload is reaped, the claimed one stays alive. It then
  failed on opening the palette with Ctrl+K: focus was in the xterm, where Ctrl+K belongs to the shell (W18 rule, correct app
  behaviour). The spec now clicks the palette button. That last step (Reset layout kills the pty) is NOT yet verified by
  e2e: two-run cap reached. Next e2e run should include this spec.
- No electron / Xvfb processes left after either run.

## Notes for the parent
- Reset layout now kills running terminals without the close confirm (the H3 fix; before, they were orphaned). If a confirm
  is wanted, it belongs in `resetLayout()` (dock.tsx), listing open terminals the way the Updates view does.
