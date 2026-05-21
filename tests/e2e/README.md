# tests/e2e

End-to-end TUI tests for the harness. Drives a real tmux session,
captures pane output, asserts against it. Built for the TUI surface that
script-based tests (`harness -p ...`) cannot reach: agent boot, banner
rendering, and clean exit.

This directory ships the **scaffolding** only — driver functions, the
runner, and the schema docs. Scenario files live under `scenarios/` and
are authored separately (Track F2).

## Contents

```
tests/e2e/
├── lib/
│   ├── tmux_driver.sh     # sourceable bash driver (functions below)
│   ├── wait.sh            # re-export of the wait helpers from tmux_driver.sh
│   └── run_scenario.py    # python helper that parses + drives one YAML
├── scenarios/             # YAML scenario files (Track F2 owns)
│   └── .gitkeep
├── run.sh                 # orchestrator: discover + run + aggregate
└── README.md
```

## How to run

```bash
# All scenarios under tests/e2e/scenarios/
bash tests/e2e/run.sh

# Only the boot scenarios
HARNESS_E2E_PATTERN='0[12]-*-boot.yaml' bash tests/e2e/run.sh

# Pin the log directory (useful in CI to upload as an artifact)
HARNESS_E2E_LOG_DIR=/tmp/e2e-run bash tests/e2e/run.sh

# Run against a sandbox scenarios directory (used by the F1 verification flow)
HARNESS_E2E_SCENARIOS_DIR=tests/e2e/scenarios.tmp bash tests/e2e/run.sh
```

Exit code is 0 iff every scenario passes. The summary block at the end
prints totals and lists each failure.

Per-scenario artifacts written under `$HARNESS_E2E_LOG_DIR`:
- `<session>.pipe.log` — every byte tmux wrote to the pane (full
  forensic log; survives scrollback rotation)
- `<session>.transcript.txt` — final pane capture (visible + scrollback)

## Scenario format

Scenarios are YAML. Top-level keys: `name`, `inventory_refs`,
`description`, `setup`, `steps`, `cleanup`. Only `name` and `steps` are
required.

```yaml
name: 01-opencode-boot
inventory_refs: [F001, F003]
description: |
  Launch `harness`, wait for the banner, then exit cleanly.

setup:
  cwd: /path/to/run/in              # optional; defaults to runner cwd
  env:                              # exported into the runner process
    TERM: xterm-256color
    HARNESS_PROXY_SCHEME: current
  tmux:
    width: 200                      # pane columns;   default 200
    height: 50                      # pane rows;      default 50
    history: 50000                  # scrollback;     default 50000
  default_timeout_s: 30             # per-step default; default 30

steps:
  # Type a command into the session shell and press Enter, then wait
  # until `marker` appears in the pane.
  - launch: "harness"
    wait_for_marker: "harness-agent (opencode)"
    timeout_s: 90

  # Bracketed-paste multi-line text, send Enter, wait for output to
  # settle for 500ms.
  - paste: |
      what is 2 + 2?
    send_key: Enter
    wait_stable_ms: 500
    timeout_s: 60

  # Send a named key alone (Enter, Escape, C-c, Tab, ...).
  - send_key: C-c
    wait_stable_ms: 300

  # Send literal text without bracketed-paste (no key-name interpretation
  # of the string itself). Optional `send_key` follows the literal text.
  - send_literal: "exit"
    send_key: Enter

  # Sleep without polling. Use sparingly; prefer wait_for_marker /
  # wait_stable_ms.
  - sleep_s: 0.5

  # Assert against the current pane capture. Each entry is a single-key
  # mapping. `regex_match` runs with re.MULTILINE.
  - capture_assertions:
      - contains: "4"
      - not_contains: "Error"
      - regex_match: "harness-agent \\(opencode\\)"

cleanup:
  kill_session: true                # default true; kills the tmux session
  archive_transcript: true          # default true; writes transcript.txt
```

### Step types

| Top-level key       | Effect                                                                 |
|---------------------|------------------------------------------------------------------------|
| `launch`            | Types the string + Enter (literal — no key-name interpretation).       |
| `paste`             | Bracketed-paste via `tmux load-buffer` + `paste-buffer`.               |
| `send_literal`      | `tmux send-keys -l --` (no key-name interpretation).                   |
| `send_key`          | Bare named key (`Enter`, `Escape`, `C-c`, `Tab`, ...). Also legal as a follow-up on `paste` and `send_literal`. |
| `sleep_s`           | Unconditional sleep in seconds (float).                                |
| `capture_assertions`| List of single-key mappings: `contains`, `not_contains`, `regex_match`.|

Each non-assertion step accepts optional `wait_for_marker`,
`wait_stable_ms`, and `timeout_s` to delay before the next step runs.

### Why two paste APIs?

`send_literal` types the text as if the user keyed it — the TUI sees a
stream of keystrokes. `paste` triggers bracketed paste mode, which TUIs
that distinguish typing from pasting (e.g. some editor / REPL modes)
handle differently. If your scenario is testing paste handling
specifically, use `paste`. Otherwise either is fine for short content;
prefer `paste` for multi-line.

## Adding a scenario

1. Create `tests/e2e/scenarios/NN-short-name.yaml`. The `NN-` prefix
   determines run order (alphabetical).
2. Reference at least one feature inventory ID via `inventory_refs`.
3. Use the mock upstream (`tests/mock_upstream.py`) for anything that
   would otherwise hit a real LLM. e2e tests must be deterministic and
   offline-capable.
4. Run it locally:

   ```bash
   HARNESS_E2E_PATTERN='NN-*' bash tests/e2e/run.sh
   ```

5. Inspect the transcript at `$HARNESS_E2E_LOG_DIR/<session>.transcript.txt`
   to make sure assertions match what's actually on screen.

## Driver functions reference

The bash driver at `tests/e2e/lib/tmux_driver.sh` is sourceable from any
runner. The Python helper at `tests/e2e/lib/run_scenario.py` mirrors the
same primitives.

Why both? `run_scenario.py` does the YAML parsing and assertion logic
(cleaner than pure-bash YAML). The bash driver remains the canonical
reference for the tmux gotchas and is available to ad-hoc shell scripts
or interactive debugging sessions that don't want to go through Python.

### Bash API (source `tests/e2e/lib/tmux_driver.sh`)

| Function                                          | Purpose                                              |
|---------------------------------------------------|------------------------------------------------------|
| `e2e_session_start <name> [w] [h]`                | Fresh detached session of given size; clears screen. |
| `e2e_pipe_pane <name> <log-path>`                 | Stream every byte to a log file.                     |
| `e2e_send_keys <name> <args...>`                  | Pass-through to `tmux send-keys -t`.                 |
| `e2e_send_literal <name> <text> [follow_key]`     | `send-keys -l --` then optional named key.           |
| `e2e_paste <name> <content>`                      | Bracketed paste via `load-buffer` + `paste-buffer`.  |
| `e2e_capture <name>`                              | Print pane (visible + scrollback) to stdout.         |
| `e2e_wait_for_marker <name> <marker> [timeout_s]` | Block until marker appears; 0 on hit, 1 on timeout.  |
| `e2e_wait_stable <name> [stable_ms] [timeout_s]`  | Block until screen stops changing for `stable_ms`.   |
| `e2e_session_kill <name>`                         | Idempotent kill.                                     |

### tmux gotchas the driver handles

These are why the API looks the way it does:

1. **`send-keys` escaping**. tmux interprets bareword args as named keys.
   Literal text must use `-l`. Mixing literal and named in one call is a
   footgun, so `e2e_send_literal` always splits them into two calls.
2. **Multi-line content**. `send-keys -l` does not trigger bracketed
   paste. Use `e2e_paste` for content TUIs should see as a paste.
3. **Pane geometry**. TUIs reflow based on terminal size. Fix it at
   session start via `-x -y`. Defaults: 200x50.
4. **Scrollback**. tmux default `history-limit` is 2000; we set 50000.
5. **`capture-pane` vs `pipe-pane`**. `capture-pane -S -` grabs visible
   + scrollback. `pipe-pane` streams every byte to a file, surviving
   scrollback rotation. Use both: capture for assertions, pipe for
   forensic logs.
6. **`TERM`**. Default tmux TERM is `screen`. The driver exports
   `TERM=xterm-256color` in the first command sent into the session so
   TUIs get full color and unicode.
7. **Streaming output**. After sending input, output streams in.
   Asserting immediately is racy. Use `wait_for_marker` or
   `wait_stable`.
8. **`remain-on-exit`**. The driver does not set this. tmux issue #1663
   documents `capture-pane` truncation on never-attached sessions that
   had `remain-on-exit` on.

## Environment variables

| Variable                       | Purpose                                    | Default                                |
|--------------------------------|--------------------------------------------|----------------------------------------|
| `HARNESS_E2E_SCENARIOS_DIR`    | Override scenarios directory.              | `tests/e2e/scenarios`                  |
| `HARNESS_E2E_PATTERN`          | Bash glob filter on scenario basenames.    | (unset; matches everything)            |
| `HARNESS_E2E_LOG_DIR`          | Directory for pipe-pane logs + transcripts.| `/tmp/harness-e2e-<epoch>`             |
| `HARNESS_E2E_MOCK_UPSTREAM`    | `=1` makes `run.sh` start a `mockupstream` sidecar before the scenarios and remove it after (see "Upstream for scenarios"). | (unset; no sidecar) |
| `HARNESS_PROJECT_NAME`         | Compose project whose `harness-net` the mock sidecar joins. | `harness` |

## Upstream for scenarios

Every scenario drives a full agent → ollama → proxy → upstream round trip,
so the `proxy` service needs a real upstream to forward `/api/chat` to. The
scenarios must stay deterministic and offline-capable, so that upstream is
always `tests/mock_upstream.py`, never a real LLM.

In CI the e2e job runs `./harness start` (which brings up `proxy` + `ollama`
with `PROXY_API_URL=http://mockupstream:9000/...`) and then runs `run.sh`
with `HARNESS_E2E_MOCK_UPSTREAM=1`. That flag makes `run.sh` start the
shared `tests/mock_upstream.py` as a `mockupstream` sidecar on the harness
network (via `test_start_mockupstream` from `tests/lib/test_helpers.sh`),
wait for it healthy, run the scenarios, then remove it. `mockupstream` is a
dotless intra-cluster name, so the proxy's firewall guardrail lets it
through without an allowlist entry.

Run it the same way locally:

```bash
HARNESS_E2E_MOCK_UPSTREAM=1 bash tests/e2e/run.sh
```
