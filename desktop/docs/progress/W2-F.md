# W2-F: native Vault, vault file, Jira, Debug and the ticket Changes tab

Status: built; unit tests (28 new, 50 with the ticket and shell suites) and typecheck pass. e2e: 6/7 pass; the 7th was
a spec miscount, fixed but not rerun (see "Test results").

## Done
- **Vault** (`views/vault/VaultView.tsx`, `model.ts`; `view.vault`). The header shows the vault name and a switcher
  (`/vaults`, `POST /vault/use`). With no active vault (503 → `unavailable`) the view shows an empty state that
  points to the switcher. There are four tabs:
  - **Pages:** a filter and a windowed list (VirtualList above 200 rows). ↑/↓/Enter work, and Enter opens the file.
  - **Files:** `/vault/tree`, loaded lazily per folder and flattened into one windowed list. ↑/↓ move, →/← open and
    close a folder, and Enter opens a file.
  - **Ingest:**
    - status (running, stale with no heartbeat, failed units, ingested, not yet ingested);
    - Stop (`POST /vault/ingest/stop`), Start and Retry failed (`POST /vault/ingest/start`, form, `what=retry`);
    - a per-source table, the failures with their problems, and the log tail.
  - **History:** the vault's commit log.
- **Vault file** (`VaultFilePanel.tsx`; panel kind `vault-file`, params `{path}`), from `/vault/file`:
  - **Markdown** renders through `ui/Markdown`. Relative links, `![img](…)` and `[[wiki|alias]]` are rewritten to
    `#vault:<rel>`, and a click on one opens another vault-file tab in the app. Links in code are left alone, and
    links that would leave the vault stay as they are.
  - **Images** come from `api.bytes('/vault/file/<rel>')` and are shown through a blob URL, which is revoked on
    unmount. The CSP blocks the server's own URL.
  - **Text** shows up to the server's cap, with a "long file" notice when it is cut.
- **Jira** (`views/jira/JiraView.tsx`, `model.ts`; `view.jira`), from `GET /jira?list=sprint|backlog[&mine=1]`:
  - Controls: Sprint/Backlog, "Only mine", and a filter.
  - Rows show type, assignee, status tone and a `queued: <status>` pill.
  - The list is windowed. ↑/↓ move, Space picks a row, and Enter opens the ticket panel if the row is queued,
    otherwise it picks the row.
  - "Copy N" copies the picked keys. "Add tickets…" copies them and opens the Add tickets dialog, because the queue
    API needs a project.
  - Shows serve's `error` and `note`, and a "showing the first N" line when `more` is set.
  - List and mine persist in the panel params.
- **Debug** (`views/debug/DebugView.tsx`, `model.ts`; `view.debug`). The windowed `/debug` list (runs and reports)
  sits beside the selected `/debug/<kind>/<name>`, rendered as Markdown.
  - Filters: All/Runs/Reports, plus a text filter. ↑/↓ change the selection, which persists in the panel params.
  - The report toolbar has the ticket, Transcript (`transcript` panel), reload and web buttons.
  - A missing report reads "Still looking" when its verdict is running.
- **Ticket Changes tab** (`views/ticket/DiffView.tsx`, `diff-model.ts`; tab `ticket-tab-changes`, plus the panel kind
  `diff` for "open in its own tab"). It uses `GET /tickets/<K>/diff?repo=&view=` and parses with diff2html's parser
  only. Rendering is plain Preact text nodes, never diff2html's HTML.
  - Toolbar: a repo picker when there are several repos, view buttons from serve's `views`, totals, and a commits list.
    The file nav appears when there is more than one file.
  - **Big diffs:** the text is cut at 6 MiB on a file boundary, with a notice. Files after the 8th start collapsed,
    and each file renders 400 lines, then "Show all N lines". 60 files × 1000 lines renders 3,200 rows.
  - **Context:** "N more lines" above a hunk fetches `/tickets/<K>/lines`, up to 500 per click, and splices the
    lines in.
  - **Comments:** click a line, or focus a file body and press ↑/↓/j/k then Enter. Ctrl+Enter saves and Esc cancels.
    The review tray holds line comments plus a general note, capped at the ticket model's `FIX_MAX_COMMENTS`. Click an inline comment to edit it. The draft persists.
  - **Send:** posts `POST /tickets/<K>/fix {comments:[{path,line,side,code,text}]}`. It is gated on `actions.fix`, so
    the button explains when fixes are not offered.
- **Mock serve** (`e2e/mock-serve/server.mjs`, `data/w2f-views.mjs`):
  - Canned GET routes, keyed by path plus sorted query, with the path alone as fallback.
  - `setRoute(key, data, status)` to override a route, or null to remove one. It resets with `setScenario`.
  - Generated `/tickets/<K>/lines`, and the raw `GET /vault/file/<rel>` PNG.
  - **`GET /sessions/<sid>/messages?from=`**: the recorded `data/messages.json` page for any sid in the `sessions`
    topic, filtered by `from`. The renderer calls this path in `TranscriptPanel`.
  - The data file is a `.mjs` module because Playwright loads `server.mjs` as CommonJS, where `import.meta` (needed
    to read a JSON file next to it) throws.

## Tests
- Unit tests:
  - `views/ticket/diff-model.test.ts` (11): parse, the cut, totals, anchors, gaps, insertLines, fix items, and
    review errors.
  - `views/vault/model.test.ts` (9).
  - `views/jira/model.test.ts` (4).
  - `views/debug/model.test.ts` (4).
  - The existing `views/ticket/model.test.ts` and `shell/shell.test.ts` still pass. The existing TicketPanel testids
    are unchanged.
- e2e (`e2e/views/`):
  - `vault.spec.ts`: 402 pages windowed, filter + Enter, in-app link, image blob, tree keyboard, ingest status, tail
    and Stop POST.
  - `jira.spec.ts`: rows, mine, keyboard pick, filter, backlog note and empty state, 500 → error.
  - `debug.spec.ts`: list, report, keyboard, kind filter, Transcript tab, and the empty state.
  - `diff.spec.ts`: "19 more lines", a line comment, the exact fix POST body, an empty view; plus the 60 × 1000 diff
    cap with a keyboard comment.

## Test results
- `node scripts/run-tests.mjs` over the ticket, vault, jira, debug and shell tests: 50/50 pass.
- `tsc --noEmit`: clean.
- e2e, run 2 (`--no-build`, flock): **6 of 7 pass**. The tests that passed:
  - vault ×2;
  - jira;
  - debug ×2;
  - diff, the line comment and fix POST.
- The failure was in the big-diff test and was in the spec, not the app: it expected `diff-line` = 8 × 400. The
  400-row cap counts each file's hunk header, which is not a `diff-line`, so the real count is 8 × 399 = 3,192.
- The fix is in `diff.spec.ts`, but it **has not been rerun** because this leg used up its two e2e runs. The rest of
  that test (keyboard comment → `big/f0.txt:1`) has not run yet either. Please run
  `node scripts/e2e.mjs --no-build e2e/views/diff.spec.ts`.
- Run 1 never got to the tests: Playwright loads `server.mjs` as CJS, and the data file read through `import.meta`
  threw. Fixed by moving the data into `data/w2f-views.mjs`.

## Parity with the web pages still missing
- **Diff:**
  - no side-by-side layout;
  - "N more lines" sits only above hunks, not after a file's last one (the web has both);
  - no word-level highlighting inside changed lines (the web uses diff2html's `matching: 'lines'`).
- **Vault:**
  - Markdown images show as "image: …" links that open the image in-app, not inline, because `ui/Markdown` drops
    `<img>`;
  - no search and no commit/diff view (serve answers 501 for those, so "web" links cover them);
  - Ingest Start has no unit, dir, max or model fields, and there is no "forget codebase";
  - no repo facts (branch, ahead/behind, dirty).
- **Jira:** no one-click "add to queue" from a row, because `POST /queue/add` needs a project. It goes through the Add
  tickets dialog with the keys on the clipboard.
- **Debug:** no live tail of a running debug run. Use reload, or Transcript.
- **Tests:** no e2e covers the vault "no active vault" (503) state.

## Missing endpoints and contract notes (serve not edited)
- `shared/api.ts` `DebugReport.change` is typed as a string, but serve sends a bool plus a `report` bool. The view uses
  `DebugRow = Omit<DebugReport,'change'> & {change?: unknown; report?: boolean}`. Fix the shared type when api.ts is
  next touched.
- `Vaults` rows have no `dir`, and the `VaultOverview` pages have no `mtime`, so neither can be shown.
- There is no JSON endpoint to add a Jira key to the queue without a project.

## Requests for the parent
- The W2-F CSS is appended to `views/ticket/ticket.css` in a marked section, because `main.tsx` imports a fixed CSS
  list that I do not own. Please move the `vt-*`/`vfile-*`, `jr-*` and `dbg-*` rules into
  `views/{vault,jira,debug}/*.css` and import those in `main.tsx`.
- Registry: `registerPanel` lines were added for `view.vault`, `vault-file`, `view.jira`, `view.debug` and `diff`.
  `commands.ts` is unchanged, because the view commands already exist.
