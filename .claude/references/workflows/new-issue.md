# Workflow: new issue (or first @claude comment on one)

Applies when the trigger is an issue body or an issue comment and you have
not yet posted a proposal on this issue.

**Default to research-and-propose. Do NOT write code on the first response
unless the fix is trivial** (see Trivial-fix exception below).

## 1. Research the problem before writing the proposal

- Read the relevant code in this repo.
- Use web search for anything externally answerable: library/API behavior,
  error messages, version-specific bugs, standard practices, syntax, tool
  flags, third-party service docs, etc.
- The "Risks / open questions" section is only for things that genuinely
  require *the user's* input — their preferences, ambiguous scope, or
  project-specific context the agent can't know. It is NOT a place to
  park questions the agent could have answered with a search or by reading
  code.
- If a question can be answered by reading code or searching the web, the
  agent must answer it before posting the proposal.

If the work touches a module covered by an architecture doc (see the
architecture router in `CLAUDE.md`), read that doc before drafting the
proposal.

## 2. If the request is genuinely unclear after research

Ask for clarification in a comment and stop. Do not guess.

## 3. Post a comment with

- Your understanding of the problem (1–3 sentences)
- Proposed approach (bulleted list of changes)
- Files you plan to touch
- Tests you plan to add or modify
- Risks or open questions — items that require user input only (see step 1)

End the comment with: "Let me know if this looks right or if you'd like
changes."

Then **stop. Do not write code.**

## Proposal-phase comment must NOT

- Reference a branch by name (no commits exist yet on the working branch).
- Claim that anything has been pushed to the remote.
- Include a PR-creation link.
- Imply work is in progress beyond the research itself.

## Design discussions

If the work is a design decision (architecture, API shape, data model,
build approach, tooling choice — anything with more than one reasonable
answer), apply the design-discussion checklist BEFORE drafting the
proposal. See [`.claude/references/design-discussions.md`](../design-discussions.md).

## Trivial-fix exception

Skip research-and-propose only if ALL of these hold:

- Fix is one of: typo, comment-only change, dependency version bump,
  obvious one-liner.
- Risk is near-zero.
- The change is fully covered by existing tests, OR the file has no logic
  (docs, config).

If unsure whether something qualifies, treat it as non-trivial and propose.
