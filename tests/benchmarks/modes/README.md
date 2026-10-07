# `harness benchmark --test-modes`: hybrid vs single-message A/B

A docker-free A/B benchmark of the proxy's two prompt modes:

- **hybrid**: the default, the normal `harness host` behavior.
- **single**: the opt-in `--single-message` mode (`PROXY_PROMPT_MODE=single`).

Every trial is one real `harness host --yolo [--single-message] -p "<prompt>"`
run (real proxy, real opencode, real upstream). Each trial runs in a fresh
scratch copy of a task's seed files, and the task's own `check.py` scores it.
No docker, no harbor.

## Run it

```bash
# 1) Dry run, no key and no network: a scripted mock upstream on loopback.
#    Proves the wiring end to end in both modes (about 3 min).
harness benchmark --test-modes --mock

# 2) The real benchmark: 6 tasks x 2 modes x 3 repeats = 36 trials.
harness benchmark --test-modes --repeats 3

# Interrupted, or the key locked? Continue where it stopped:
harness benchmark --test-modes --resume tests/benchmarks/runs/modes-<stamp>

# Rebuild the report from results.jsonl only:
harness benchmark --test-modes --report tests/benchmarks/runs/modes-<stamp>
```

Options: `--repeats N` (default 3), `--modes hybrid,single` (the first mode is
the baseline), `--task-ids a,b`, `--timeout SEC` per trial (default 900),
`--list` (print the plan and run nothing), `--keep-workdirs`.

Expect 2 to 6 minutes per real trial, so 36 trials take roughly 1h15m to 3h30m.
The runner prints an estimate and one line per trial.

## Tasks (`tasks/<id>/`)

Each task has `task.json` (`prompt`, `mock_solution`), `files/` (the seed files)
and `check.py` (exit code 0 means pass).

```
fix-bug            fix a wrong divisor in stats.py so test_stats.py passes
rename-symbol      rename get_usr -> get_user across files, no alias left
implement-slugify  implement a function from its docstring + doctests
csv-summary        aggregate a CSV into summary.json (exact numbers)
recall-config      multi-step: read a config early, many reads later, then
                   use the early value (long-context recall)
early-constraint   a rule stated in turn 1 must hold for files made later
                   (instruction persistence)
```

The last two target what the modes change: where earlier turns sit in the
prompt.

## What is measured, and the report

`report.txt` in the run dir contains:

- **Per mode**: pass rate with a Wilson 95% CI, mean seconds and agent requests
  per trial, upstream errors, timeouts, and "mode ok". Mode ok means the proxy
  log proves the mode took effect: every upstream request had that mode's
  shape, and single sent exactly one message.
- **Paired**: each (task, repeat) runs in both modes, so pass/fail is compared
  per pair with an exact McNemar test. Time and request differences come with a
  95% CI.
- **Per task**: passes and mean time for each mode.
- **Verdict**: one or two lines. "no significant difference" with 36 trials
  means any real gap is small, not that there is none.

The plan is block-interleaved: within each repeat, all of one mode's trials run,
then all of the other's. The mode order alternates per repeat (AB, BA, AB), and
the task order is shuffled with a fixed seed. This spreads upstream drift over
the day across both modes and keeps proxy restarts to two per repeat.

Infra failures are listed and excluded from the stats:

- `auth`: a 401/403, or a locked or rejected key.
- `launch`: no request reached the proxy, even if the trial then timed out.
- `upstream`: a failed trial sunk by upstream 5xx/429/network errors (it
  timed out with one, or every request errored). Other 4xx errors, such as
  the upstream refusing an oversized single message, count against the mode.

These stop the run with exit code 2:

- an auth failure;
- a launch failure in the first two trials;
- 3 upstream failures in a row;
- Ctrl-C, which also kills the running trial.

On a stop, the runner prints the full `--resume` command with the same
options. `--resume` builds its plan from the options you pass, so keep them
the same, or raise `--repeats` to extend a finished run. It skips finished
trials and reruns the infra-failed ones.

## Output (`tests/benchmarks/runs/modes[-mock]-<stamp>/`, gitignored)

```
results.jsonl   one JSON row per trial
meta.json       modes, repeats, timeout, tasks, model, upstream kind, host os
report.txt      the report above
trials/<id>.log per-trial check result, proxy-log counters, agent stdout/stderr tail (redacted)
mock.log        (--mock only) the mock's request log
```

The per-trial logs are redacted by `scripts/probe_upstream.py`'s redactor
(API key, upstream URL and host, IPs, emails). Your home dir, user name and
hostname are redacted too.

## Isolation and egress

- **Private install root**: trials use `state/bench-modes/root/`
  (`mock-root/` for `--mock`) via `HARNESS_INSTALL_ROOT`. Its `.env` is a
  symlink to yours, and it gets a free port (`HARNESS_HOST_PORT`). The bench
  proxy has its own pid, log and port, and never touches a `harness host`
  proxy you have running. It shares your install's host toolchain and proxy
  venv (a symlink, or a directory junction on Windows), so nothing is
  downloaded twice.
- **Private opencode dirs**: opencode gets private XDG config, data, cache and
  state dirs there. Your global opencode config, plugins, MCP servers and
  sessions are not loaded.
- **`HARNESS_HOST_NO_WEB=1`**: denies webfetch and websearch, drops Exa, and
  sets `share: disabled`. The runner also sets `OPENCODE_DISABLE_MODELS_FETCH`,
  `_AUTOUPDATE`, `_SHARE`, `_LSP_DOWNLOAD`, `_CLAUDE_CODE` and
  `_DEFAULT_PLUGINS`, and clears `OTEL_*`.
- **Task traffic**: the only place task traffic goes is the upstream API
  (`PROXY_API_URL`), through the local proxy.
- **One-time downloads**: if the toolchain is missing, the warm-up fetches
  public packages: Node, opencode, jq, the proxy's pip deps, and ripgrep if it
  is absent. These carry no task or user data.
- **`--mock`**: the upstream env (`PROXY_*`) is removed from the child
  environment. The mock never sees your key, and nothing leaves loopback.

## Caveats

- **Setup costs**: before trial 1, an untimed warm-up provisions the host
  toolchain and proxy venv (downloads happen only if they are missing; no
  model call). If the warm-up fails, the run stops before trial 1 and prints
  the redacted error and the resume command. A trial's time is the whole `harness host` run, so the first
  trial of a run also includes the auth probe. The first trial of each block also includes a
  proxy restart, a few seconds. Restarts hit both modes equally. The one-off
  probe falls on the baseline's first trial, which is negligible over 36 trials.
- **Statistical power**: 3 repeats is low power for pass-rate differences.
  Use the time and request differences as the main signal, and add repeats if
  the pass rates are close.
- **Key locks**: the key locks every 8 h, which stops a run partway. Resume it
  after the unlock.
- **No upstream cache sharing**: in hybrid mode, message 0 contains the trial's
  unique workdir, so no upstream session or cache is shared across trials.
- **Mock results**: a `--mock` run proves the plumbing, not model quality.
  Both modes pass it in about the same time.

Tests: `tests/unit_bench_modes_test.sh` (docker-free, part of `harness test
unit`) runs `test_modes.py` (stats, plan, log counters, every checker against
its seed and mock solution, mock replies, env scrubbing, redaction, report),
the `cmd_benchmark` routing, `--list`, `HARNESS_HOST_PORT` and
`HARNESS_HOST_NO_WEB`.
