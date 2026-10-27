# `--thoughts` probe (SWE-bench Verified, paired, token-capped)

A/B of `harness host --yolo -p` (arm A, default slim mode) against the same
command plus `--thoughts` (arm B), on 38 SWE-bench Verified tasks
(`tasks.json`: id, repo, difficulty, solve rate across 12 public
mini-SWE-agent runs; selection = not "<15 min fix", solve rate 0.30-0.70,
django/sympy/sphinx/pytest/pylint). Stock opencode in both arms.

Docker-free. `swe_env.py` builds each task once under
`<install root>/state/bench-thoughts/envs/<id>/`, following SWE-bench v4.1.0's
specs: a checkout of `base_commit` that is its own git repo (one local
`pristine` commit, no history, no remote), plus a venv next to it. Because the
checkout has its own `.git`, opencode treats it as the project root and never
loads the harness repo's AGENTS.md or CLAUDE.md. The agent also gets a working
`git diff`. The gold patch and the test patch exist only in the dataset cache
(`data/rows.json`), and the gate's results and logs go to `data/gates/<id>/`.
They are applied to the checkout only inside the grader, after the agent has
finished, and the grader resets the checkout to `pristine` afterwards. It
then runs the **gold gate**. The gate requires that at least one FAIL_TO_PASS
test fails with only the test patch applied, and that the gold patch resolves
the task. A task that fails the gate is excluded before any token is spent. The
grade is the same SWE-bench eval, run on the agent's `git diff`.
Only verdicts are cached in `gate.json`. A task whose env failed to build
(an exception, such as a failed clone) is retried on the next run, and
`--rebuild-envs` rebuilds everything.

## Quick suite: `--suite local` (one day's allowance)

The SWE-bench suite needs far more than one day's tokens: a trial there can
cost up to 1.5M. `--suite local` runs the same A/B, through the same runner,
with the same guard, ledger check and report, on the six small scored tasks of
`tests/benchmarks/modes/tasks/` (fix-bug, rename-symbol, implement-slugify,
csv-summary, recall-config, early-constraint). Each trial gets a fresh scratch
copy of the task's seed files, and the task's `check.py` grades it. There are
no envs to build and no gate. Each task runs once per repeat in each arm. A
task's arm order flips every repeat, and half of each repeat starts with each
arm. The defaults are a 3,500,000-token budget, a 250,000 cap per trial, a
900 s timeout and 4 repeats (24 pairs). The budget guard ends the run early
when the observed cost says the next pair will not fit.

```
harness probe thoughts --suite local --dry-run    # plan and budget, offline
harness probe thoughts --suite local              # the run
harness probe thoughts --suite local --resume tests/benchmarks/runs/thoughts-local-<stamp>
```

What it can show with about 20 pairs:

- **Cost**: tokens per trial, prompt, completion and reasoning tokens per
  step, step-1 prompt tokens (the same input in both arms, so the same count),
  and prompt tokens per 1,000 chars sent. If the thoughts that come back in
  the history were billed, B would pay more per char.
- **Breakage**: upstream errors, empty replies, tool errors and arm
  verification. Every request in an arm must have that arm's shape, and the run
  stops after a trial whose requests never had it, for example when
  `DEFAULT_MODEL_NAME` is not a native tool model.
- **It cannot show** a pass-rate difference. These tasks are easy, so both
  arms will mostly pass. The model also sees the same context in both arms
  (the upstream ignores `reasoning_content`), so a large quality effect is not
  expected.

## Run it (owner)

```
python3 tests/benchmarks/thoughts/run.py --dry-run          # plan + budget math, offline
python3 tests/benchmarks/thoughts/run.py --gate-only        # build envs + gold gate, no model calls
python3 tests/benchmarks/thoughts/run.py                    # the probe: budget 20M, cap 1.5M/trial
python3 tests/benchmarks/thoughts/run.py --resume tests/benchmarks/runs/thoughts-<stamp>
python3 tests/benchmarks/thoughts/run.py --report tests/benchmarks/runs/thoughts-<stamp>
```

Flags: `--budget N` (default 20,000,000), `--trial-token-cap N`
(1,500,000), `--timeout S` (2400), `--pairs N`, `--task-ids a,b`,
`--dataset PATH` (local datasets-server rows instead of HF), `--cache-dir`,
`--harness-bin`, `--install-root`. No new environment variables.

## Token guard (in code: `guard()`, `LogMeter`, `ledger_check()`)

- Tokens are counted as prompt + completion + reasoning, taken from the proxy
  log's `usage:` lines. This is conservative if the upstream already includes
  reasoning in completion.
- A request with no usage line (in flight when the trial is killed, or "none
  reported") is counted at chars/3 of its prompt, and the trial is flagged.
- Before each pair, the probe continues only if `spent + 2 * p75(trial tokens)
  <= budget`. Until 2 trials have finished, the cap stands in for p75.
- Each trial is killed once its live count reaches
  `min(cap, (budget - spent) / 2)`. A killed trial is scored as fail and
  marked `capped`.
- Overshoot is at most the request(s) in flight at the kill.
- Infra failures (auth, launch, upstream outage) count against the budget but
  are left out of the statistics.
- A runner killed mid-trial leaves `inflight.json`. `--resume` books those
  tokens as a `lost` row.
- The proxy's usage ledger is checked after every trial. The run stops if the
  ledger sum and the log count differ by more than 10%, or if the upstream's
  remaining tokens-day figure is below the next pair's projection. Credits are
  weighted, so they are reported but never used to stop.

## Pre-registered falsifier

Claim under test: *`--thoughts` is functionally safe on real agentic coding
work, i.e. it is no worse than slim, and the thinking really happens.* The
thresholds are fixed now, before any run. Any one of these falsifies the
claim:

1. **The arm did not run.** More than 1 B trial fails arm verification (not
   every request is `mode=native/slim/thoughts`), or B's mean reasoning
   tokens/step is 0. In either case the probe is void and must be fixed, not
   read as evidence.
2. **Functional breakage.** The B-minus-A difference, summed over complete
   pairs, is at least 3 for any one of these counters:
   - upstream 4xx/5xx errors
   - signatures switched off
   - FATAL lines
   - empty replies
   - message errors
   - tool-error rate (at least 5 percentage points instead of 3)
3. **Worse outcomes.** thoughts-only failures exceed slim-only failures by 3
   or more (the discordant pairs, b - c >= 3), or the exact McNemar test gives
   p < 0.05 against B.
4. **The cost does not pay.** B's mean tokens/trial is at least 1.5 times A's,
   while B's pass count is not higher than A's.

Treat the pass-rate statistics as a guardrail: they are underpowered. With 38
pairs at most, only a large effect shows. A null result on criterion 3 is not
evidence that the two arms are equal.

## Known gaps

- Django 3.0/3.1 specify Python 3.6, which is not installable here (uv's
  floor is 3.8). The nearest installed newer Python is used instead, and this
  is recorded in the gate notes.
- apt and locale-gen pre-install steps are skipped.
- Windows (Git Bash) is supported:
  - checkouts are extracted with Python's `tarfile`, and are LF whatever
    `core.autocrlf` is set to;
  - venvs use `Scripts\python.exe`;
  - interpreters are found with `py -3.X`;
  - shell steps (sed pre-steps, test commands) run under Git Bash's `bash`.

  Tests that only fail on Windows fail the gate, so the gate excludes those
  tasks; it does not grade them as wrong.
- Builds use PyPI.
- There is no git history in the checkouts, so `SETUPTOOLS_SCM_PRETEND_VERSION`
  pins the version.
- `--yolo` agents can read outside their checkout, including the dataset cache
  under `state/bench-thoughts/data/` (which holds the gold patches). The
  `contaminated` counter flags any tool input that mentions any of these, once
  the agent's own checkout path is removed from the input:
  - the cache directory's basename, or `bench-thoughts`
  - `rows.json`, the `--dataset` file names, or `SWE-bench_Verified`
  - the gate files
  - the run directory, or `benchmarks/runs` (the other arm's patch and eval
    log)

  A read the agent makes without naming any of these is not caught.
- `mock_upstream.py` speaks the cooperative route, so the native/thoughts path
  has no offline end-to-end test. A fake `harness` that writes proxy-log lines
  tests the token-cap kill instead.

Tests: `python3 -m unittest tests/benchmarks/thoughts/test_thoughts.py`.
