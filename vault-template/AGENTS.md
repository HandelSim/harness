# Vault rules

<!-- Your instructions to the agent for this vault. harness hands this file to the
     agent at every launch; edit it to change how the agent works here. -->

This folder is a knowledge vault: a git repo of Markdown that opens as an Obsidian
vault. You maintain it. People drop sources in; you turn them into a linked wiki
that gets more useful with every session, so that a question asked next month is
answered from what was already read and worked out, not from scratch.

## Layout

- `.raw/` holds sources as they arrived: articles, transcripts, PDFs, exports,
  notes. Read them; never edit, rename or delete them. `.raw/sessions/` holds
  harness's copy of each past conversation; ingest those only when asked.
- `wiki/` holds the pages you write and keep current, one topic per page, in
  `wiki/sources/`, `wiki/concepts/`, `wiki/entities/` (people, organizations,
  projects, tools) and `wiki/questions/`.
- `wiki/index.md` lists every wiki page once with a one-line summary.
- `wiki/log.md` records what each session changed.
- `pub/` holds finished HTML pages for people to read in a browser.
  `harness serve` lists them.

## Pages

- File names are lowercase-with-dashes.md. The first `# ` heading is the title.
- Every page starts with front matter:

  ```yaml
  ---
  type: source | concept | entity | question
  sources: [".raw/2026-10-22-some-article.md"]
  updated: 2026-10-22
  ---
  ```

- Link related pages with `[[wikilinks]]` (the file name without `.md`). Link
  generously; a page nothing links to gets lost.
- Say where each claim comes from: a source page or a `.raw/` file. Mark your
  own inferences as such. When sources disagree, record both and say which is
  newer; do not silently overwrite.

## Ingest

When a new file lands in `.raw/`, or you are handed a source:

1. Read all of it.
2. Write a `type: source` page in `wiki/sources/`: what it is, its key points,
   and what it changes in the wiki.
3. Update the concept and entity pages it touches, or create them.
4. Add new pages to `wiki/index.md` and append an entry to `wiki/log.md`.

## Query

When asked a question, start from `wiki/index.md`, read the relevant pages and
follow their links, then answer with links to the pages you used. If the answer
took real work, file it as a `type: question` page so it is not redone.

## Lint

When asked, or when you notice rot: find pages missing from the index, broken
links, orphan pages, stale claims and contradictions. Fix what you can and list
the rest in the log entry.

## Git

harness commits and pushes this vault when the session ends; do not run git
yourself unless asked. Never rewrite history.
