# Design discussions

When the work is a design decision (architecture, API shape, data model,
build approach, tooling choice — anything where there's more than one
reasonable answer), do this BEFORE proposing changes:

1. **State the problem in one sentence**, stripped of any solution words.
   ("The agent needs to ship code without a manual PR step" — not "we
   need to add an auto-merge feature".)
2. **List the assumptions you're carrying.** What is the request taking
   as given? Mark each as verified, inferred, or untested.
3. **Enumerate at least two viable approaches.** One option is not a
   decision. If only one is viable, explain explicitly why the others
   fail.
4. **For each approach, name the trade-off.** What does it optimize for?
   What does it pay for that with? (e.g. "auto-ff-merge optimizes for
   round-trip speed; pays for it with no human gate before changes hit
   `dev`".)
5. **Recommend one, and say why.** Tie the recommendation to the
   constraints in steps 1–2, not to which option is "cleaner".
6. **Self-critique the recommendation.** Strongest 1–3 reasons it could
   be wrong. If those reasons are likely, redesign.

Treat the user's first idea as one option among several, not the default.
If their idea is the right one, say so explicitly after step 5 — don't
just rubber-stamp it.

For genuinely fundamental questions ("should we even be doing this?",
"are we solving the right problem?"), escalate to the more rigorous
first-principles skill at `.claude/skills/first-principles/SKILL.md`.
