# Tests

All test artifacts for harness live under `tests/`. Production runtime
tooling stays under `scripts/`; anything sourced from `tests/` is
test-only.

This file is the architectural overview. For the per-feature inventory
and coverage map, see `tests/INVENTORY.md` and `tests/COVERAGE.md`
(co-located with the tests, not duplicated here).

## Layout

| Path                       | What it is |
|----------------------------|------------|
| `tests/INVENTORY.md`       | Flat list of every testable feature/behavior in the harness, with stable IDs (`F###`, `P###`, `A###`, ...). The source of truth that coverage maps against. |
| `tests/COVERAGE.md`        | Map of every inventory ID to its test(s), with a status per row and quoted assertion evidence. (See that file for the legend and current stats — not duplicated here.) |
| `tests/lib/`               | Sourceable bash test toolkits. `test_helpers.sh` is the main one (`require_docker`, `test_section`, `test_generate_env`, `test_generate_mockupstream_override`, `test_wait_for_healthy`, `test_cleanup`, plus integration helpers). |
| `tests/fixtures/`          | Test fixtures. `fixtures/responses/` holds mock-upstream response fixtures (see `fixtures/responses/README.md`); `fixtures/test-project/` is the small Python calculator package used by `integration_test.sh`. |
| `tests/mock_upstream.py`   | Mock upstream LLM API used by every docker-based test. Two modes: legacy `MOCK_SCENARIO=text\|tool` and fixture dispatch (`MOCK_FIXTURES_DIR=/fixtures`). |
| `tests/*_test.sh`          | Top-level test scripts (one per area: `proxy`, `harness`, `persistence`, `mcp`, `firewall`, `upgrade`, `full_pipeline`, `integration`, `scheme_contract`, `podman_smoke`). |
| `tests/e2e/`               | tmux-driven TUI scenarios. `e2e/lib/` (driver), `e2e/run.sh` (orchestrator), `e2e/scenarios/*.yaml` (per-scenario specs). See `tests/e2e/README.md`. |
| `tests/benchmarks/`        | Harbor-based agent benchmarks (Terminal-Bench 2.0, SWE-bench Lite). Adapters under `benchmarks/adapters/`, schemes under `benchmarks/schemes/`, runners under `benchmarks/runners/`. Benchmarks NEVER run in CI. See `tests/benchmarks/README.md`. |

## Quick start

```
harness test                       # all CI-runnable tests
harness test proxy                 # only proxy tests (globs tests/proxy*_test.sh)
harness test --pattern 'mcp*'      # explicit glob — runs every match under tests/
harness test integration --slow    # full slow integration test (HARNESS_RUN_SLOW=1)
```

`harness test` discovers test scripts by globbing `tests/*_test.sh`, so a
newly-added `tests/<name>_test.sh` is picked up automatically — no
wrapper edits needed.

## Sections

`harness test [SECTION]` selects a group:

| Section          | What runs |
|------------------|-----------|
| `all` (default)  | Every `tests/*_test.sh` script. |
| `unit`           | Standalone tests with no docker dependency (currently `upgrade_test.sh`). |
| `integration`    | Docker-based tests. Use `--slow` for `integration_test.sh`. |
| `e2e`            | `tests/e2e/run.sh` — every tmux scenario under `tests/e2e/scenarios/`. |
| `<prefix>`       | Matches `tests/<prefix>*_test.sh`. E.g. `harness test proxy` runs `proxy_test.sh` and any future `proxy_*_test.sh`. |

`--pattern '<glob>'` overrides the section and runs whatever the glob
matches under `tests/`. Useful for one-off runs (`--pattern 'scheme_*'`).

## Environment variables

| Variable                    | Effect |
|-----------------------------|--------|
| `HARNESS_RUN_SLOW`          | `=1` opts into the slow integration test (`integration_test.sh`). `harness test --slow` sets this for you. The default suite stays fast without it. |
| `HARNESS_E2E_PATTERN`       | Glob restricting which scenarios `tests/e2e/run.sh` will execute (default: `tests/e2e/scenarios/*.yaml`). |
| `HARNESS_E2E_LOG_DIR`       | Where the tmux driver writes per-scenario `pipe-pane` transcripts. Defaults to a temp dir under the worktree. Keep this to retain transcripts after a failed run. |
| `HARNESS_CONTAINER_RUNTIME` | `=podman` runs the suite under podman instead of docker. All `*_test.sh` honor this. |

`mock_upstream.py` honors `MOCK_SCENARIO` (legacy) or `MOCK_FIXTURES_DIR`
(fixture dispatch). Tests set these directly; you don't normally set
them from your shell.

## Adding a new test

### Top-level script

1. Drop a `tests/<name>_test.sh` file using `tests/lib/test_helpers.sh`
   for common setup. Use existing scripts (`proxy_test.sh`,
   `mcp_test.sh`) as the model — the `test_section` helper structures
   output, and `test_cleanup` runs on EXIT.
2. The first behavior assertion sets the tone — assert on specific
   structured output, not just exit codes or "non-empty". See the
   conventions section below.
3. If the test needs a mock upstream response that doesn't exist yet,
   add a fixture under `tests/fixtures/responses/` (see that directory's
   README) rather than embedding the response in the test.
4. Verify discovery: `harness test --pattern '<name>*'` should find and
   run the new file. No wrapper edits required.
5. If the test is slow (> ~60 s) or needs a heavy image, gate it behind
   `HARNESS_RUN_SLOW=1` so CI's default matrix doesn't pick it up.

### e2e scenario

1. Add `tests/e2e/scenarios/NN-<name>.yaml` per the schema in
   `tests/e2e/README.md`. Number prefix controls ordering.
2. Reference at least one `inventory_refs:` ID so the scenario maps back
   to `INVENTORY.md`.
3. Always use `tests/mock_upstream.py` for the upstream side — never a
   real LLM.
4. Run `harness test e2e` or `bash tests/e2e/run.sh` to exercise it.
5. Use `wait_for_marker` and `wait_stable_ms` rather than fixed sleeps —
   the driver helpers exist for exactly this. See the e2e/README.md
   pitfalls section.

### Benchmark

Benchmarks are NOT regular tests; they're driven by `harness benchmark
<target>` and owned by `tests/benchmarks/`. Targets:

- `harness benchmark smoketest` — 3–5 small tasks; 5–15 min; verifies
  wiring. Required gate before any full-scale target.
- `harness benchmark terminal-bench` — full Terminal-Bench 2.0 run;
  6–12 hrs.
- `harness benchmark swe-bench-lite` — full SWE-bench Lite run; 4–8 hrs.
- `harness benchmark compare-schemes` — run the same task list across
  every scheme; for comparing prompt-injection schemes head-to-head.

See `tests/benchmarks/README.md` for the full reference: installation,
docker socket caveat, smoketest-first guidance, adding a new scheme,
adding a new benchmark target.

## Test file conventions

- **Shebang and flags.** `#!/usr/bin/env bash` with `set -euo pipefail`
  at the top. Tests must fail loudly, not silently.
- **Source the toolkit.**
  `source "$(dirname "$0")/lib/test_helpers.sh"`. Use `require_docker`
  (or the equivalent) at the top so the test errors out cleanly when
  prereqs are missing rather than crashing mid-run.
- **Sectioned output.** `test_section "what this section verifies"`
  before each block of assertions. Makes failures easy to locate in CI
  logs.
- **Assert on structure, not noise.** Real assertions look like
  `[[ "$x" == "$y" ]]`, `assertEquals`, or `grep -q '<specific>' || exit 1`.
  Avoid always-pass patterns like `[[ -n "$x" ]]` against any non-empty
  output, or `grep ... > /dev/null; true` with no exit check.
- **Cleanup with `trap`.** Register `test_cleanup` (or your own) on
  `EXIT`/`ERR` so failed tests don't leak containers, volumes, or temp
  dirs.
- **No real upstream.** Every docker-based test wires the agent to
  `tests/mock_upstream.py` via `test_generate_mockupstream_override` or
  an equivalent compose override. Tests must never hit a real LLM API.
- **CI-safe.** The default suite must run in under ~10 min total and
  use no more than ~7 GB RAM / ~14 GB disk per matrix shard. Heavy
  tests gate behind `HARNESS_RUN_SLOW=1`.

## Mock upstream

`tests/mock_upstream.py` is the LLM-API stand-in every docker-based test
uses. Two modes:

- **Legacy.** `MOCK_SCENARIO=text` or `MOCK_SCENARIO=tool` picks one of
  two canned responses. Used by `proxy_test.sh` and `firewall_test.sh`
  where a single response is enough.
- **Fixture dispatch.** Set `MOCK_FIXTURES_DIR=/fixtures` and mount
  `tests/fixtures/responses/` there. The mock loads every `*.json`
  lexicographically and matches the latest user message against each
  fixture's `match` regex; first match wins. `99_default.json` is the
  catch-all.

See `tests/fixtures/responses/README.md` for the file shape, naming
convention (`NN_short_slug.json` with reserved priority ranges per
scenario family), and how to add a new fixture.

## Continuous integration

CI runs on every push to `dev` and `main` and on every PR targeting
either branch (see `.github/workflows/ci.yml`):

- `lint` — `bash -n` over all shell scripts, `check_runtime_calls.sh`,
  advisory `shellcheck`.
- `unit` — `tests/upgrade_test.sh`.
- `docker-tests` — matrix over the docker-based `*_test.sh` (proxy,
  harness, persistence, mcp, firewall).
- `pipeline` — `tests/full_pipeline_test.sh`.
- `integration` — `HARNESS_RUN_SLOW=1 tests/integration_test.sh`.
- `e2e` — `tests/e2e/run.sh` over every scenario. The job runs
  `./harness start` with `PROXY_API_URL` pointed at `mockupstream`, then
  runs `run.sh` with `HARNESS_E2E_MOCK_UPSTREAM=1` so it provisions a
  `mock_upstream.py` sidecar on the harness network for the scenarios to
  drive (see `tests/e2e/README.md`).
- `scheme_contract` — `tests/scheme_contract_test.sh`.

Benchmarks (`harness benchmark ...`) NEVER run in CI; they need an
upstream API key, hours of wall-clock time, and significant disk. See
`tests/benchmarks/README.md`.

`podman_smoke_test.sh` is not run in CI (no podman on `ubuntu-latest`);
run it manually on Linux when touching the runtime wrapper.
