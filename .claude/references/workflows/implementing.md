# Workflow: implementing (after approval) — also covers PR feedback

Applies after HandelSim has approved your proposal in the issue thread, OR
when you are responding to review comments on a PR you previously opened.

The action has already created your working branch from `dev`. You are on
it. **Do NOT create a new branch.**

## Steps

1. Verify with `git branch --show-current`. The branch is
   `agent/issue-<N>-<timestamp>`.
2. **Resume a prior WIP branch if one exists** — see "Resume a prior WIP
   branch" below. Otherwise start fresh on the current branch.
3. Implement the proposed changes. **Commit and push at checkpoints as you
   go** — see "Checkpoint commits — push as you go" below. This is what
   makes a stalled or crashed run recoverable by the next agent.
4. Add or update tests **minimally — just enough to exercise the
   functionality you added or changed, not an exhaustive suite.** Run them.
   The *final* state must pass before the `dev` ff-merge (intermediate
   checkpoint commits may be red).
5. Run any linters/formatters the project uses.
6. **Update any architecture doc whose subject was affected by your change**,
   in the same commit as the code change. See the architecture router in
   `CLAUDE.md` for which doc maps to which subject. Architecture docs are
   short and structural — keep them that way. Do not let them drift into
   line-by-line API references.
7. Make the final commit with `Fix #<N>: <summary>` and the trailer
   `Co-authored-by: HandelSim <HandelSim@users.noreply.github.com>`, then
   push the agent branch with the action-provided git-push helper. This is
   the **last** push to the agent branch.
8. Rebase the agent branch onto the latest `dev`:
   - `git fetch origin dev`
   - `git rebase origin/dev`
   This is a **local-only** rebase that enables the `--ff-only` merge into
   `dev`. It rewrites commits you already pushed in steps 3 and 7, so the
   local branch will diverge from the remote agent branch — that is
   expected. **Do NOT re-push or force-push the agent branch after this
   rebase** (see Hard rules). The remote agent branch is a recovery
   artifact; the authoritative result reaches `dev` via the ff-merge below.

## Resume a prior WIP branch

The action creates a fresh `agent/issue-<N>-<timestamp>` branch from `dev`
on every trigger, so a crashed or stalled earlier run leaves its work on a
*different* `agent/issue-<N>-*` branch. Because the runner checks out full
history, you can recover it:

- `git fetch origin`
- List prior unmerged branches for this issue, then pick the newest by the
  timestamp in the name (exclude your current branch):
  `git branch -r --no-merged origin/dev --list 'origin/agent/issue-<N>-*'`
- If one exists, **review its commits** (`git log` and
  `git diff origin/dev...<that-branch>`) to judge whether the approach is
  sound:
  - Sound → adopt it: `git reset --hard <that-branch>`, then continue. Your
    checkpoint pushes go to your *current* branch as normal.
  - Unsound or abandoned → discard it (`git reset --hard origin/dev`) and
    start fresh.
- Do not delete the old branch (no stale-branch cleanup for now).
- Record in your end-of-work status which prior branch, if any, you resumed
  from or discarded.

## Checkpoint commits — push as you go

Keep the remote branch current so a crash never loses work. Use a
**semantic cadence, not a timer**:

- **Before running tests**, and **before any long-running, risky, or
  destructive step.**
- After a coherent sub-change lands (a passing test, a self-contained edit).

At each checkpoint: `git add -A && git commit -m "WIP #<N>: <what>"`, then
push the agent branch with the action-provided git-push helper. Checkpoint
pushes are **plain fast-forward pushes** — you have not rebased yet, so they
always fast-forward. Checkpoint commits may be incomplete or red; only the
*final* state (step 4) must be green before the `dev` ff-merge.

## After the rebase: auto-merge into `dev` (default path)

If implementation matched the approved plan, fast-forward `dev` to your
locally rebased branch:

9. `git checkout dev && git pull --ff-only origin dev`
10. `git merge --ff-only agent/issue-<N>-<timestamp>` — this uses your local,
    rebased branch ref and must succeed because step 8 rebased onto
    `origin/dev`.
11. `git push origin dev` — a regular push, never a force-push.
12. Post an end-of-work status block in the issue comment containing:
    - Branch name (e.g. `agent/issue-23-20260507-0251`)
    - Which prior WIP branch you resumed from or discarded, if any
    - Whether the branch was pushed to the remote (yes/no)
    - Whether the branch was fast-forward merged into `dev` (yes/no — if
      no, why, and a PR link)
    - Commit SHA(s)

## When to fall back to a PR instead of auto-merging

If during implementation the scope changed because of unforeseen issues
(approach had to change, unexpected conflicts, tests forced a different
design, additional files were needed, etc.), do NOT auto-merge. Instead:

- Make sure your final commit is pushed to the agent branch (step 7) and
  **skip the local rebase in step 8** — the PR is opened from the remote
  agent branch, which must not be rewritten.
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
- The architecture-doc update rule (step 6) still applies for any
  behavior change made during the review loop.

## Hard rules — never violate

- **Never force-push.** Anywhere. Ever. Not to feature branches, not to
  `dev`, not to `main`. If you think you need to force-push, you don't —
  rebase first, or fall back to a PR.
- **The agent branch is push-only-forward.** Checkpoint pushes (step 3) and
  the final push (step 7) are plain fast-forwards because you have not
  rebased yet. After the local rebase in step 8 you must NOT push the agent
  branch again — force-pushing it to "keep it tidy" is forbidden. Let the
  remote agent branch lag; the work reaches `dev` via the `--ff-only` merge.
- **Never use a non-fast-forward merge.** No `--no-ff`, no merge commits.
  Only `--ff-only`. If `--ff-only` fails, fall back to a PR — do NOT
  escalate to a regular merge.
- **Never push directly to `main`.** `main` is updated only via PR merge
  by a human.
- **`dev` may be pushed to ONLY via the rebase + `--ff-only` flow above.**
  Any other push to `dev` is forbidden.
