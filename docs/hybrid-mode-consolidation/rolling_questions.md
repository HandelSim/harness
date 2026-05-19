# Rolling questions / chosen interpretations — hybrid mode consolidation (issue #64)

## Q1: Does the consolidation remove `passthrough` mode?

**Ambiguity.** The issue says "The new valid set is `("hybrid", "user_front")`" but
the explicit Removal list is `user`, `system`, `user_bookend` — and the design
rationale only addresses the five cooperative-prompt modes (`user`, `system`,
`hybrid`, `user_front`, `user_bookend`). `passthrough` is a sixth, separately-
documented bypass mode (benchmark control, described in `architecture/proxy.md`)
that the issue body never mentions.

**Interpretation chosen:** preserve `passthrough` in the `valid` tuple. Rationale:
issue Section 7 ("Things to NOT do") and the "Process — defaults" section both
prioritize "preserve backward compatibility for code paths not explicitly listed
for change." `passthrough` is not listed. The "five prompt-injection modes"
language in the issue refers to the five cooperative-prompt modes, not to
`passthrough`, which is a bypass with its own dedicated test class
(`TestPassthroughMode`) and benchmark scheme file
(`tests/benchmarks/schemes/passthrough.json`).

Resulting valid tuple: `("hybrid", "user_front", "passthrough")`. Fallback target
remains `"user_front"` per the issue.

## Q2: `scripts/proxy_test.sh` doesn't exist — what's the integration test?

**Observation.** Verification step 2 says `bash scripts/proxy_test.sh`. No such
file. The actual proxy integration test is `tests/proxy_test.sh`.

**Interpretation chosen:** treat `tests/proxy_test.sh` as the intended file.

## Q3: `docs/02-decisions.md` and `docs/06-history.md` referenced but absent

**Observation.** The issue mentions updating "the project's decisions doc
(likely `docs/02-decisions.md` if it exists)" and "Do not modify history docs.
If there's a file like `docs/06-history.md` or `CHANGELOG.md` …" The `docs/`
directory only contains `PODMAN.md` and `WINDOWS.md`. Architecture docs live
under `architecture/`. No decisions or history doc exists in either location.

**Interpretation chosen:** there is no decisions doc to rewrite and no history
doc to append to. Update `architecture/proxy.md` (the matching architecture
router target) and skip creating new decisions/history docs.

## Q4: Scope of doc/test changes touching removed modes

**Observation.** The issue lists `README.md`, `.env.example`, `MANUAL_TEST_PROMPT.md`,
`docs/`. But the removed mode names also appear in:
- `docker-compose.yml` (`PROXY_PROMPT_MODE` comment)
- `tests/scheme_contract_test.sh` (per-scheme contract assertions)
- `tests/fixtures/responses/scheme-user/`, `scheme-system/`, `scheme-user_bookend/`
- `tests/INVENTORY.md` (entries P013–P017)
- `tests/COVERAGE.md` (entries P010, P013–P018)
- `tests/benchmarks/mock-smoketest.sh` (scheme list)
- `tests/benchmarks/adapters/harness_claude/harness_claude_agent.py` (docstring)
- `tests/benchmarks/README.md` (mode discussion)
- `tests/mock_upstream.py` (line 210 comment)

**Interpretation chosen:** update all of these. Rationale: the spec's intent is
to consolidate the mode set; leaving dead references and tests that exercise
removed modes would actively break the integration test suite. The issue's
verification step 5 (`grep` check) requires no matches in active code; that
applies to these files. Fixture directories for removed modes will be deleted.

## Q5: Scheme contract test — keep or simplify?

**Observation.** `tests/scheme_contract_test.sh` runs five schemes. After
consolidation only two remain.

**Interpretation chosen:** keep the test file, shrink the SCHEMES list to
`("user_front" "hybrid")`, drop the per-scheme assertion branches for the
removed modes, and update the `hybrid` branch's assertions to match the new
reminder text ("Reminder:" instead of "Tool reminder", presence of the
"do not invent" sentence, presence of tool names list).

## Q6: Benchmark mode list

**Observation.** `tests/benchmarks/mock-smoketest.sh` line 80 iterates
`user_front user_bookend user system hybrid`.

**Interpretation chosen:** reduce to `user_front hybrid`. The smoke-test is a
sanity check that PROXY_PROMPT_MODE actually switches per scheme; the set of
modes it probes is just the cooperative set.
