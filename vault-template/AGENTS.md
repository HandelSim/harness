# Knowledge vault

<!-- Your instructions to the agent for this vault. harness hands this file to the
     agent at every launch; edit it to change how the agent works here. -->

The folder holding this file is a knowledge vault about this project. Paths
below are relative to it. Read only the files your task needs:

- **A ticket:** harness already gave you `.raw/tickets/KEY.md` (the ticket),
  `wiki/context/KEY-context.md` (the domain terms and rules it uses, when it uses any)
  and `wiki/notes/tickets/KEY-state.md` (where the work stands). Start there.
- **A word of the project's field you do not know:**
  `grep -i "<word>" wiki/domain/glossary.md`. Each line links to the domain
  document's page in `wiki/domain/`, which quotes the source.
- **Past work, a decision, how some code works:** `grep -i "<word>" wiki/index.md`
  (one line per page), then open the best two or three hits. A code directory's
  page is `wiki/code/<repo>/<repo>--<dir with / as -->.md`.
- **Nothing found:** `grep -ril "<word>" wiki/` and read the `summary:` lines.
- Do not read `wiki/candidates.md` (unchecked lines for people to review) or
  whole folders of `wiki/sources/`. A page can be out of date: check the code
  before you rely on it.

## What you may write

- `wiki/notes/`: your own pages, for what is worth finding next time (a
  decision and why, a fix and its cause). Start each with front matter
  `type: note` and a one-line `summary:`, and link related pages with `[[name]]`.
- The ticket's state note: when you finish a step, record what is done, what is
  next, decisions and open questions, so the next session starts where you ended.
- Never edit a page whose front matter says `generated: harness`, or anything
  in `.raw/` (the sources as they arrived). harness rewrites those.

harness copies each session into `.raw/sessions/`, builds the pages
(`harness ingest`) and commits and pushes; do not run git here unless asked.
