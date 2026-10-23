# Knowledge vault

<!-- Your instructions to the agent for this vault. harness hands this file to the
     agent at every launch; edit it to change how the agent works here. -->

The folder holding this file is a knowledge vault: Markdown pages about past
work, decisions and the codebases it covers. Paths below are relative to that
folder. `wiki/overview.md` says what the vault is about and what it tracks.

## Look things up first

Before you answer a question about this project, its code, past work or a
decision, or before you start a task in an area the vault covers, check it:

1. Grep `wiki/index.md` for the key words: it lists every page once with a
   one-line summary. A codebase is one line there; its directory pages are
   listed in the module tree at the end of `wiki/code/<repo>/<repo>.md`.
2. Grep the pages themselves: `grep -ril "<word>" wiki/`, then read the
   `summary:` lines of the hits and open the best two or three.
3. A code directory's page has a predictable name:
   `wiki/code/<repo>/<repo>--<dir with / as -->.md`, so `src/api/auth` of `shop`
   is `wiki/code/shop/shop--src--api--auth.md`.
4. Follow the `[[links]]` on a page; `[[name]]` is the file `name.md` anywhere
   under `wiki/`. `wiki/topics/` and `wiki/tickets/` list every page about a
   topic or a ticket.
5. A page can be out of date. Before you rely on it for code, check the code.

## What you may write

- `wiki/notes/`: your own pages, for anything worth finding next time (a
  decision and why, how something works, a fix and its cause). Start each with:

  ```
  ---
  type: note
  summary: one line saying what the page is about
  ---
  ```

  Link the pages it relates to with `[[name]]`.
- Never edit a page whose front matter says `generated: harness` (index, code,
  sources, topics, tickets): harness rewrites them and your edit is lost. Write
  a note that links to it instead.
- Never edit `.raw/`: those are the sources as they arrived. `.raw/sessions/` is
  harness's copy of each past session.
- `pub/` holds finished HTML pages for people to read; `harness serve` lists them.

harness copies each session into `.raw/sessions/`, turns sources into pages
(`harness ingest`), rebuilds the index and commits and pushes; do not run git
in this folder unless asked.
