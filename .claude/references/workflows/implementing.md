# Workflow: implementing (after approval) — also covers PR feedback

Applies after HandelSim has approved your proposal in the issue thread, OR
when you are responding to review comments on a PR you previously opened.

The action has already created your working branch from `dev`. You are on
it. **Do NOT create a new branch.**

## Steps

1. Verify with `git branch --show-current`. The branch is
   `agent/issue-<N>-<timestamp>`.
2. Implement the proposed changes.
3. Add or update tests. Run them. They must pass before you commit.
4. Run any linters/formatters the project uses.
5. **Update any architecture doc whose subject was affected by your change**,
   in the same commit as the code change. See the architecture router in
   `CLAUDE.md` for which doc maps to which subject. Architecture docs are
   short and structural — keep them that way. Do not let them drift into
   line-by-line API references.
6. Commit with: `Fix #<N>: <summary>` and include the trailer
   `Co-authored-by: HandelSim <HandelSim@users.noreply.github.com>`.
7. Rebase the agent branch onto the latest `dev` BEFORE the first push:
   - `git fetch origin dev`
   - `git rebase origin/dev`
   This guarantees a fast-forward is possible and means you never need to
   force-push.
8. Push the agent branch with the action-provided git-push helper.

## After the push: auto-merge into `dev` (default path)

If implementation matched the approved plan, fast-forward `dev` to your
branch:

9. `git checkout dev && git pull --ff-only origin dev`
10. `git merge --ff-only agent/issue-<N>-<timestamp>` — this must succeed
    because step 7 rebased onto `origin/dev`.
11. `git push origin dev` — a regular push, never a force-push.
12. Post an end-of-work status block in the issue comment containing:
    - Branch name (e.g. `agent/issue-23-20260507-0251`)
    - Whether the branch was pushed to the remote (yes/no)
    - Whether the branch was fast-forward merged into `dev` (yes/no — if
      no, why, and a PR link)
    - Commit SHA(s)

## When to fall back to a PR instead of auto-merging

If during implementation the scope changed because of unforeseen issues
(approach had to change, unexpected conflicts, tests forced a different
design, additional files were needed, etc.), do NOT auto-merge. Instead:

- Push the branch.
- Open a PR targeting `dev`.
- In the issue comment, explain what changed vs. the approved plan and
  link the PR.

Also fall back to a PR (not a sign of plan drift, just a legitimate failure
mode) if:

- `git merge --ff-only` fails because someone else's commit landed on `dev`
  between your rebase and your push (concurrent dev pushes). Re-rebase and
  retry once; if it still fails, open a PR and note it in the comment.
- Branch protection on `dev` rejects the push from this action. Open a PR
  and note it in the comment.

## Responding to review comments on a PR you opened

- Treat review comments as feedback. Make changes on the same branch and
  push.
- Do not open new PRs.
- Reply to each review comment indicating what you did or why you
  disagreed.
- The architecture-doc update rule (step 5) still applies for any
  behavior change made during the review loop.

## Hard rules — never violate

- **Never force-push.** Anywhere. Ever. Not to feature branches, not to
  `dev`, not to `main`. If you think you need to force-push, you don't —
  rebase first, or fall back to a PR.
- **Never use a non-fast-forward merge.** No `--no-ff`, no merge commits.
  Only `--ff-only`. If `--ff-only` fails, fall back to a PR — do NOT
  escalate to a regular merge.
- **Never push directly to `main`.** `main` is updated only via PR merge
  by a human.
- **`dev` may be pushed to ONLY via the rebase + `--ff-only` flow above.**
  Any other push to `dev` is forbidden.
