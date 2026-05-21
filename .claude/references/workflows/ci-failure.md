# Workflow: CI-failure auto-fix (label `ci-failure`)

Applies when the trigger is the `ci-failure` label on an issue titled
`CI failure on dev: <sha-7>`. A CI test failed on `dev`; the auto-fix
workflow opened or appended to this issue and triggered you directly.

Default to fix-and-push without asking — most CI failures are within scope
for direct fix.

## Investigate fully before deciding what to do

Apply the same research discipline as the new-issue workflow: read the
relevant code in this repo, use web search for anything externally
answerable (library/API behavior, error messages, version-specific bugs,
syntax, tool flags, third-party docs), and run targeted tests locally to
disambiguate. Do NOT fall back to research-and-propose just because the
cause isn't obvious from the logs alone — investigation is your job.

## Review recent commits — do not undo intentional work

Before changing anything, run `git log --oneline -20 origin/dev` and read
the diffs of commits near the failing SHA to understand what changed
recently and why. A failing test does not always mean the recent change was
wrong — sometimes a change was made intentionally by HandelSim and the
*test* is now what is stale.

- **Never "fix" CI by reverting an intentional change.** If a recent commit
  deliberately changed behavior and an older test now fails, the correct fix
  is to update the test to the new intended behavior (or fix the real bug the
  test exposed), not to roll the change back.
- If you cannot tell whether a recent change was intentional, do NOT guess —
  fall back to research-and-propose and ask HandelSim.

## Eligible for direct fix-and-push (no proposal, no approval)

- Test flake stabilization (retry, sleep, race fix).
- Bug fix where investigation produces a clear root cause.
- Dependency / version bump matching a clear CI error.
- Test-only changes (assertion update when production behavior is
  correct).
- Anything else where, after investigation, you can articulate why the
  fix is correct and what behavior change it produces.

## Must fall back to research-and-propose (HandelSim approval required)

- Security-relevant code: auth, network rules, secrets, firewall scripts,
  or anything under `scripts/` that runs as root inside containers.
- Public API or contract changes.
- Fix would itself require new tests beyond a simple assertion update.
- After full investigation, multiple plausible root causes remain and
  picking one would require a guess.
- You can articulate a plausible second-order failure mode of your own
  fix that you can't rule out.

## Auto-fix flow (when eligible)

1. Read the failing logs from the issue body and comments.
2. Investigate per the research discipline above, and **review recent
   commits** (see "Review recent commits" above) so you do not revert
   intentional work. Reproduce locally if useful. Identify root cause.
3. Implement the fix on the agent branch, committing and pushing at
   checkpoints as you go (see [`implementing.md`](implementing.md) →
   "Checkpoint commits — push as you go").
4. Run the specific failing test locally; it must pass before the final
   commit.
5. Update any architecture doc whose subject was affected, in the same
   commit (see the architecture router in `CLAUDE.md`).
6. Commit with `Fix #<N>: <summary>` + the `Co-authored-by` trailer.
7. Rebase onto `origin/dev`, fast-forward merge `dev`, push — follow the
   standard implementation flow in [`implementing.md`](implementing.md).
8. Close the issue with a comment: fix summary + commit SHA + link to the
   originally-failing CI run.

## Self-cap

Count prior `agent/issue-*` commits referenced from this CI failure
issue. If ≥5 attempts have already been made for this `dev` SHA, do NOT
attempt a 6th. Post a comment listing what was tried, leave the issue
open, and ping HandelSim for review. (The CI watcher enforces the same
cap upstream; this is a redundant safeguard.)
