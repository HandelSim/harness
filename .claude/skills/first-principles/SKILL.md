---
name: first-principles
description: Decompose a fundamental design or product question to its atomic constraints and rebuild a recommendation from those constraints, instead of pattern-matching on familiar solutions. Use when the question is "should we even be doing this?", "are we solving the right problem?", "is the framing correct?", or when a proposed approach feels off but the user has not given you a concrete alternative. Use when the user explicitly asks for first-principles analysis, when stakes are high (architecture, data model, build/release approach, security boundary), or when standard design-discussion analysis has produced multiple plausible answers without a clear winner. Skip for clearly-scoped implementation tasks where the design is already settled.
---

# First-principles design

This skill is the escalation path beyond the standard "Design discussions"
flow in `CLAUDE.md`. Use it when the question is foundational enough that
listing options and trade-offs is not enough — when you suspect the
question itself is mis-framed, when the user's first idea may be solving
a problem that does not actually exist, or when the stakes justify a
slower, more disciplined pass.

The goal is not to be clever. The goal is to **strip the question down
to things you can verify or measure** and rebuild from there, so the
recommendation rests on constraints rather than on convention or
analogy.

## When to use

- The user is asking *whether* to do something, not *how*.
- A standard design discussion has surfaced two or more options with no
  clear winner and the trade-offs feel like preference rather than fact.
- The proposed solution looks like a copy of how someone else solved a
  superficially similar problem, but the underlying constraints differ.
- A subtle assumption is doing load-bearing work in the framing and you
  cannot tell yet whether it holds.
- The blast radius is large: data loss risk, security boundary,
  irreversible migration, public API shape, contract with another team.

## When NOT to use

- Implementation is already approved and the work is mechanical.
- The question is genuinely a matter of taste and the cost of the wrong
  call is small.
- Time pressure is real and a "good enough" answer beats a "best" answer.
- You are tempted to use this skill to delay a decision the user has
  already made clearly. Don't.

## The seven steps

Run these in order. Do not skip ahead. Write the output of each step
down (in a comment, in scratch, in the proposal) — first principles
work fails silently when the agent thinks each step but skips
articulating it.

### 1. Restate the problem with no solution words in it

Write the problem in one sentence. The sentence must not contain the
name of any candidate solution, technology, file, or framework. If you
cannot, you are not yet describing the problem — you are describing a
solution and looking for justification.

> Bad: "We need to add a Redis cache for the session lookup."
> Good: "Session lookups on the auth path are slow enough that login
> p95 misses our 200ms target."

If the problem statement still contains a solution word, rewrite it.
Repeat until clean.

### 2. List the assumptions and label each one

Every problem statement carries assumptions. List them. For each, mark:

- **Verified** — you read the code, ran the query, checked the docs,
  or have a recent measurement. Cite where.
- **Inferred** — likely true based on related verified facts, but not
  directly checked.
- **Untested** — taken on faith from the user, the ticket, or
  convention. **These are the load-bearing risks.**

If an untested assumption would change the recommendation if it were
false, you must verify it before finishing. If verification is not
possible from inside this conversation, name it explicitly as an open
question for the user — do not paper over it.

### 3. Decompose to atomic constraints

What are the smallest, indivisible facts the solution must respect?
These are usually some mix of:

- Hard physical or protocol limits (disk I/O, network round trips,
  payload size caps, rate limits, timeout budgets).
- Correctness requirements (idempotency, ordering, consistency,
  durability, auth boundary).
- Operational constraints (deploy frequency, rollback behavior,
  observability, on-call burden).
- Human constraints (who must understand this in 6 months, who reviews,
  who owns it).
- Explicit user/product requirements that are not negotiable.

A constraint is atomic when removing or changing it materially changes
the answer. "Should be fast" is not atomic. "p95 < 200ms on the auth
path" is.

### 4. Enumerate solution shapes from the constraints, not from memory

Generate at least three candidate solutions by working *forward* from
the constraints. Specifically: for each constraint or pair of
constraints, ask "what is the simplest mechanism that satisfies this?"
Then combine.

Resist the urge to start from "what would I normally reach for." The
purpose of this step is to surface options the convention-driven
approach would skip. If your three options are "the obvious one,"
"the obvious one with a cache," and "the obvious one with a queue,"
you have not decomposed enough — go back to step 3.

For each candidate, write one sentence on what it optimizes for and
one sentence on what it pays for that with.

### 5. Recommend one, tied to the constraints

Pick one. The justification must reference the atomic constraints from
step 3, not which option is "cleaner" or "more idiomatic." If the
recommendation cannot be defended in terms of the constraints, the
constraints are wrong, the recommendation is wrong, or both.

If the user's original idea is the right answer, say so explicitly
here — do not bury the agreement under hedges. First principles work
that always disagrees with the user is just contrarianism.

### 6. Stress-test the recommendation

List the strongest 1–3 reasons the recommendation could be wrong. Be
concrete. "It might not scale" is not a stress test. "If write volume
exceeds 10× current, the chosen mechanism collapses because of X" is.

For each, decide:

- **Acceptable** — the failure mode is unlikely or recoverable; note
  the trip-wire that would tell you it's happening.
- **Mitigatable** — name the specific mitigation and add it to the
  plan.
- **Disqualifying** — the failure mode is likely or unrecoverable.
  Go back to step 4 and pick again.

If you cannot think of any reasons it might fail, you have not thought
hard enough. Sit with the recommendation for one more pass.

### 7. State what would change your mind

Write one to three sentences describing the new information that
would invalidate the recommendation. This is for the user — and for
future-you reading the decision later. Examples:

- "If verified write volume turns out to be ≥ 5× the current
  estimate, the chosen approach won't hold and we should switch to
  option B."
- "If the legal review concludes the data must stay in-region, the
  managed-service option is off the table."

If you cannot articulate what would change your mind, your
recommendation is not actually grounded in the constraints — it is a
preference dressed up as analysis.

## Output format

When using this skill in a comment or a proposal, structure the output
as the seven numbered sections above. Yes, even when it feels heavy
for the question. The structure is most of the value: it forces every
hidden step to become visible. If a section is genuinely empty (no
relevant assumptions, no plausible failure modes), say so briefly and
move on — do not pad.

Keep prose tight. The reader is the user, not a graduate committee.
A good first-principles writeup is shorter than a bad design doc, not
longer.

## Anti-patterns to avoid

- **First-principles cosplay.** Stating the problem solution-flavored,
  listing the user's idea as the only "constraint-derived" option, and
  declaring it the answer. This is rubber-stamping with extra steps.
- **Constraint inflation.** Treating preferences ("we'd like it to be
  consistent with the rest of the codebase") as atomic constraints to
  rule out unfamiliar options. Style consistency is real but it is
  not atomic — name it as a soft factor, do not let it disqualify.
- **Decomposition theater.** Producing a long list of "constraints"
  that are actually paraphrases of each other. If two constraints
  collapse to the same thing, merge them.
- **Always-disagree.** Reflexively rejecting the user's first idea to
  appear rigorous. If it survives the analysis, say so plainly in
  step 5.
- **Avoiding the recommendation.** Producing a beautiful step 1–4 and
  then refusing to commit at step 5. The user asked for help making
  a decision; help them.
- **Verification-by-assertion.** Marking assumptions as "Verified" in
  step 2 without actually citing how. If you did not check it this
  conversation or earlier, it is not verified.

## Connection to the rest of CLAUDE.md

`CLAUDE.md` already contains a "Design discussions" section that
covers the standard six-step trade-off analysis. That section is
the right tool for most design questions. This skill is heavier
and slower; it is the right tool for the small subset of questions
where the framing itself might be wrong. If you are not sure which
applies, start with the lighter "Design discussions" flow — if it
produces a clear winner, you did not need this skill.
