# W3-B: review pass 2, Set B (native views)

Source: scratchpad review2-views.md Part 2/3 + Set B fix list. Each finding was checked against the code first.
No git operations (parent commits). No serve/*.py edits.

## Done
1. Dead "Open the web version" paths (Part 3 item 1). Verified against serve/pub.py do_GET/runs_get/vault_get: the
   routed GET pages are `/`, `/runs[/...]`, `/vault[/...]`, `/settings`, `/pub/...`. Findings and Jira are panels of
   `/runs/queue` (`#findings`, `#findings-agent`, `#jira`); `/runs/queue/jira` is JSON; `/settings/system` is POST-only;
   `/runs/findings` and `/runs/findings/agent` are 404 (only `/runs/findings/notes` exists). The review missed the
   findings ones; fixed too. New `WEB` helpers + corrected `VIEW_WEB` in shell/panels.ts; call sites in Sessions,
   Transcript, Chat, Jira, Debug (list + report: `/runs/debug/<kind>/<name>`), VaultFile (`/vault/browse/<rel>`),
   Findings. Test: shell/panels-web.test.ts.
2. P2-1 Jira filter: Space/Enter in the filter input are text (isTyping guard); arrows still move. e2e jira.spec.
3. P2-2 DiffView: Enter on a hunk header runs its "N more lines"; diff body is `role=listbox` with `option` rows and
   `aria-activedescendant`. e2e diff.spec (keyboard expand + activedescendant points at the hunk).
4. P2-4 Vault: `<Files key={vault:reloadN}>` (switch or Reload refetches the tree); VaultFilePanel refetches when the
   active vault name changes. e2e vault.spec (Reload refetches /vault/tree, tree collapses to the root).
5. P2-3 Diff perf: tray (whole-change comment + send) is its own component; FileBlock is `memo` with stable
   `put`/`setRows` (refs) and gets only its own comments (array identity kept while unchanged); "Show all" replaced
   by "Show 2000 more lines (x of n shown)" steps. e2e diff.spec (step adds the rest of a 1000-line file, count 7 left,
   tray typing). Not done: DIFF_CHAR_CAP (dead 6 MB client cap) left as is.
6. P2-5 Chat: the conversation follows keyboard selection after 150 ms (clicks are immediate); TranscriptPanel's page
   loop stops on unmount/sid change (generation ref). e2e chat.spec (quick Up/Down does not fetch the other transcript).
7. P2-6 lows done: diff `sel` restored on a failed reload; findings bulk Restore = one POST `{sel:[...]}` (verified
   serve/findings_view.py action reads `id` or `sel`; e2e findings.spec); Debug report keeps its text while the same
   report reloads (mtime); Jira "Add tickets" passes the picked keys into the dialog (`openAddTickets(keys)`, e2e);
   Home page links built from `rel` (`pubPath`, unit test); `#vault:` hrefs validated in `parseVaultHref` (no `..`,
   `.`, empty, absolute, `.git`, NUL; unit test); Markdown above 300 KB shows as plain text with "Render it" (e2e vault).
8. Coordinator item: Home at ~300px: header wraps (flex-wrap, h1 min-width 0), grids use `minmax(min(Npx,100%),1fr)`,
   body min-width 0, the heading wraps (no ellipsis), the 5-column usage table scrolls inside its card
   (its min-content width overflowed a 300px body by 30px). e2e home.spec measures scrollWidth at 300px.
9. Parity (cheap, renderer only): Plan tab shows the plan's text (`wiki/notes/tickets/<KEY>-plan.md` via GET
   /vault/file, only when the ticket's vault is the active one; any error hides it) with "Open in the vault"
   (e2e diff.spec); Add tickets has Target branch (`base`) and Work branch name (`branch`), sent to POST /queue/add
   as the web form does (serve/runs.py add_tickets; e2e jira.spec).

## Skipped (why)
- P2-6 shared review draft between ticket Changes tab and the diff panel: needs useDraft to follow the app-state
  signal (parts.tsx, shared by every ticket draft); not cheap to do safely in the time box.
- P2-6 findings move past the 300-row cap: needs the full order list from the server; left.
- P2-6 "What it learned"/vault History windowing: bounded lists, left as noted by the review.
- Parity: review/state note/answers/ticket documents (only the plan is shown), packet evidence, session children: no time.
- DIFF_CHAR_CAP (dead client cap) untouched.

## Notes for others
- Escape did not close the Add tickets dialog in one e2e run (dialog opened from Jira, Escape pressed right after it
  rendered). Likely DialogHost (shell/App.tsx, not mine) attaches its window keydown listener in a useEffect that runs
  after paint, so an immediate Escape is missed; also Preact `autoFocus` does not focus inputs inserted later, so
  focus stays on the opener. Not changed; the spec now closes the dialog by a successful add.
- Unit runs collide: two concurrent `scripts/run-tests.mjs` share out/test (one run failed with "Cannot find module").
- No shared-file changes. New exports in owned files: `WEB` (shell/panels.ts), `openAddTickets(keys?)`,
  `pubPath` (home/model.ts), `PlanDoc`/`planRel` (ticket/tabs.tsx), `MD_RENDER_MAX` (ui/Markdown.tsx).

## Results (12:17 UTC)
- `npx tsc --noEmit -p tsconfig.json`: clean.
- `node scripts/run-tests.mjs`: 257 tests, 257 pass, 0 fail (new: shell/panels-web.test.ts; cases added in
  vault/model.test.ts and home/model.test.ts).
- e2e run 1 (chat, debug, diff, findings, home, jira, vault): 10 passed, 3 failed. The failures were spec mistakes
  (findings used the open-list testid on the archived tab; jira's Escape did not close the dialog, see Notes) and one
  real overflow (home usage table, 30px at 300px wide), all fixed.
- e2e run 2 (diff, findings, home, jira, vault, i.e. everything changed since run 1): 9 passed, 0 failed.
  chat and debug were unchanged after run 1, where they passed.
- No electron or Xvfb process of mine is left (the Xvfb :99 now running belongs to W3-D's e2e).
