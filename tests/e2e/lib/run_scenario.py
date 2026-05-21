#!/usr/bin/env python3
"""Run a single e2e scenario described by a YAML file.

Usage:
    run_scenario.py <scenario.yaml> [--log-dir <dir>]

The YAML schema is documented in tests/e2e/README.md. Briefly:

    name: <scenario-name>
    inventory_refs: [F001, F003]
    description: |
      ...
    setup:
      cwd: <path>
      env: { TERM: xterm-256color, ... }
      tmux: { width: 200, height: 50, history: 50000 }
    steps:
      - launch: "<command>"
        wait_for_marker: "<string>"
        timeout_s: 10
      - paste: "<multi-line content>"
        send_key: Enter
        wait_for_marker: "<string>"
        wait_stable_ms: 500
        timeout_s: 60
      - capture_assertions:
          - contains: "<string>"
          - not_contains: "<string>"
          - regex_match: "<regex>"
    cleanup:
      kill_session: true
      archive_transcript: true

Exit codes:
  0  every step and assertion passed
  1  scenario failed
  2  invocation / configuration error
  77 scenario is marked `expected_failure: true` and failed as expected (XFAIL)
  78 scenario is marked `expected_failure: true` but unexpectedly passed (XPASS)

A scenario marks itself as expected-failure with a top-level
`expected_failure: true` and a sibling `expected_failure_reason: "<text>"`.
The reason is required so the marker doesn't become a silent skip; if
the reason is missing the runner treats the scenario as unmarked.

Why this is Python rather than bash:
- YAML parsing in pure bash is painful and brittle.
- The orchestrator (tests/e2e/run.sh) still owns scenario discovery,
  environment-variable plumbing, and pass/fail aggregation; this script
  only owns the parse+drive+assert for a single scenario.
- The tmux invocations here mirror what tmux_driver.sh does in bash. The
  driver library remains the canonical reference for the gotchas (see
  the header comment of tmux_driver.sh).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# tmux primitives. These mirror tests/e2e/lib/tmux_driver.sh. Keep them in
# sync if you change one; the bash and python paths are intentionally
# interchangeable.
# ---------------------------------------------------------------------------


def _tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a tmux command. Returns the CompletedProcess so callers can read stdout."""
    return subprocess.run(
        ["tmux", *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def session_start(name: str, width: int = 200, height: int = 50, history: int = 50000) -> None:
    # Kill any existing session of this name; don't fail if it isn't there.
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)
    _tmux("new-session", "-d", "-s", name, "-x", str(width), "-y", str(height))
    _tmux("set-option", "-t", name, "history-limit", str(history))
    # Set TERM and clear the screen so subsequent captures start from a known
    # baseline. Same rationale as the bash driver.
    _tmux("send-keys", "-t", name, "export TERM=xterm-256color; clear", "Enter")
    time.sleep(0.3)


def pipe_pane(name: str, log_path: str) -> None:
    # Quoting: tmux runs the command via /bin/sh, so we shell-escape the path.
    quoted = log_path.replace("'", "'\\''")
    _tmux("pipe-pane", "-t", name, f"cat >> '{quoted}'")


def send_keys(name: str, *keys: str) -> None:
    _tmux("send-keys", "-t", name, *keys)


def send_literal(name: str, text: str, follow_key: str | None = None) -> None:
    # `-l` = literal, `--` = end of options so text starting with '-' is safe.
    _tmux("send-keys", "-t", name, "-l", "--", text)
    if follow_key:
        _tmux("send-keys", "-t", name, follow_key)


def paste(name: str, content: str) -> None:
    # load-buffer reads from stdin when given '-'.
    proc = subprocess.Popen(["tmux", "load-buffer", "-"], stdin=subprocess.PIPE)
    proc.communicate(content.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"tmux load-buffer failed with rc={proc.returncode}")
    _tmux("paste-buffer", "-t", name)


def capture(name: str) -> str:
    proc = _tmux("capture-pane", "-t", name, "-p", "-S", "-", capture=True)
    return proc.stdout


def wait_for_marker(name: str, marker: str, timeout_s: int = 30) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker in capture(name):
            return True
        time.sleep(0.5)
    return False


def wait_stable(name: str, stable_ms: int = 1000, timeout_s: int = 60) -> bool:
    # Track the stability window against the wall clock too — capture()
    # + sha256() can be slow on a busy CI runner, and adding interval_ms
    # per iteration regardless of wall cost makes the function declare
    # "stable" sooner than the requested window actually elapsed.
    deadline = time.monotonic() + timeout_s
    stable_start = time.monotonic()
    prev_hash = ""
    interval_s = 0.2
    while time.monotonic() < deadline:
        cur_hash = hashlib.sha256(capture(name).encode("utf-8")).hexdigest()
        if cur_hash == prev_hash:
            if (time.monotonic() - stable_start) * 1000 >= stable_ms:
                return True
        else:
            prev_hash = cur_hash
            stable_start = time.monotonic()
        time.sleep(interval_s)
    return False


def session_kill(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)


# ---------------------------------------------------------------------------
# Scenario execution.
# ---------------------------------------------------------------------------


class ScenarioError(Exception):
    """Raised when a scenario step or assertion fails."""


def _safe_session_name(scenario_name: str) -> str:
    # tmux session names can't contain '.' or ':'. Replace anything that
    # isn't [A-Za-z0-9_-] with '_' so a scenario name like "01-opencode-boot"
    # becomes "01-opencode-boot" (unchanged) and "foo.bar:baz" becomes
    # "foo_bar_baz".
    return re.sub(r"[^A-Za-z0-9_-]", "_", scenario_name)


def _wait_after_step(session: str, step: dict[str, Any], default_timeout: int) -> None:
    """Apply whichever wait directives the step specifies."""
    timeout_s = int(step.get("timeout_s", default_timeout))
    if "wait_for_marker" in step:
        marker = step["wait_for_marker"]
        if not wait_for_marker(session, marker, timeout_s):
            raise ScenarioError(
                f"timed out after {timeout_s}s waiting for marker {marker!r}"
            )
    if "wait_stable_ms" in step:
        stable_ms = int(step["wait_stable_ms"])
        if not wait_stable(session, stable_ms, timeout_s):
            raise ScenarioError(
                f"timed out after {timeout_s}s waiting for stable output "
                f"({stable_ms}ms window)"
            )


def _run_assertions(session: str, assertions: list[dict[str, Any]]) -> None:
    """Evaluate a capture_assertions block against the current pane capture."""
    pane = capture(session)
    failures: list[str] = []
    for idx, assertion in enumerate(assertions):
        if not isinstance(assertion, dict) or len(assertion) != 1:
            failures.append(
                f"assertion #{idx}: must be a single-key mapping, got {assertion!r}"
            )
            continue
        ((kind, expected),) = assertion.items()
        if kind == "contains":
            if expected not in pane:
                failures.append(f"contains {expected!r}: not found in pane")
        elif kind == "not_contains":
            if expected in pane:
                failures.append(f"not_contains {expected!r}: unexpectedly present")
        elif kind == "regex_match":
            if not re.search(expected, pane, re.MULTILINE):
                failures.append(f"regex_match {expected!r}: no match")
        else:
            failures.append(f"unknown assertion kind: {kind!r}")
    if failures:
        # Include a tail of the pane so logs are useful. Limit to 40 lines so
        # we don't bury the failure in noise.
        tail = "\n".join(pane.splitlines()[-40:])
        joined = "\n  - " + "\n  - ".join(failures)
        raise ScenarioError(
            f"capture_assertions failed:{joined}\n--- last 40 lines of pane ---\n{tail}"
        )


def _run_step(session: str, step: dict[str, Any], default_timeout: int) -> None:
    """Run one step of a scenario. Each step is dispatched by its top-level keys."""
    if "launch" in step:
        # Launch a command. The session shell is already running; we just
        # type the command and hit Enter. send_literal so the command is
        # not interpreted as tmux key names.
        send_literal(session, step["launch"], "Enter")
        _wait_after_step(session, step, default_timeout)
        return

    if "send_key" in step and "paste" not in step and "send_literal" not in step:
        # Bare named key (Enter, Escape, C-c, Tab, ...).
        send_keys(session, step["send_key"])
        _wait_after_step(session, step, default_timeout)
        return

    if "send_literal" in step:
        send_literal(session, step["send_literal"], step.get("send_key"))
        _wait_after_step(session, step, default_timeout)
        return

    if "paste" in step:
        paste(session, step["paste"])
        if "send_key" in step:
            send_keys(session, step["send_key"])
        _wait_after_step(session, step, default_timeout)
        return

    if "capture_assertions" in step:
        _run_assertions(session, step["capture_assertions"])
        return

    if "sleep_s" in step:
        time.sleep(float(step["sleep_s"]))
        return

    raise ScenarioError(f"unknown step shape: {sorted(step.keys())}")


def run_scenario(scenario_path: Path, log_dir: Path) -> int:
    with scenario_path.open("r", encoding="utf-8") as fh:
        scenario = yaml.safe_load(fh)

    if not isinstance(scenario, dict):
        print(f"FAIL {scenario_path}: top-level must be a mapping", file=sys.stderr)
        return 1

    name = scenario.get("name") or scenario_path.stem
    session = _safe_session_name(name)
    setup = scenario.get("setup") or {}
    tmux_cfg = setup.get("tmux") or {}
    width = int(tmux_cfg.get("width", 200))
    height = int(tmux_cfg.get("height", 50))
    history = int(tmux_cfg.get("history", 50000))
    default_timeout = int(setup.get("default_timeout_s", 30))

    # Apply env overrides for this process so they're inherited by any
    # subprocesses we spawn (and so tests that read os.environ see them).
    # tmux's own environment is not affected — for that, callers should
    # bake exports into the scenario's first `launch` step.
    for key, value in (setup.get("env") or {}).items():
        os.environ[str(key)] = str(value)

    cwd_override = setup.get("cwd")
    if cwd_override:
        os.chdir(cwd_override)

    log_dir.mkdir(parents=True, exist_ok=True)
    pane_log = log_dir / f"{session}.pipe.log"
    transcript = log_dir / f"{session}.transcript.txt"

    print(f"[{name}] starting session {session} ({width}x{height})", flush=True)
    session_start(session, width=width, height=height, history=history)
    pipe_pane(session, str(pane_log))

    failed_step: int | None = None
    failure_msg: str | None = None
    try:
        for idx, step in enumerate(scenario.get("steps") or []):
            try:
                _run_step(session, step, default_timeout)
            except ScenarioError as exc:
                failed_step = idx
                failure_msg = str(exc)
                break
    finally:
        cleanup = scenario.get("cleanup") or {}
        if cleanup.get("archive_transcript", True):
            try:
                transcript.write_text(capture(session), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                print(f"[{name}] WARN failed to archive transcript: {exc}", file=sys.stderr)
        if cleanup.get("kill_session", True):
            session_kill(session)

    expected_failure = bool(scenario.get("expected_failure"))
    reason = (scenario.get("expected_failure_reason") or "").strip()
    if expected_failure and not reason:
        print(
            f"[{name}] WARN expected_failure: true present but "
            "expected_failure_reason is empty — treating as unmarked",
            file=sys.stderr,
        )
        expected_failure = False

    if failed_step is not None:
        print(
            f"[{name}] {'XFAIL' if expected_failure else 'FAIL'} "
            f"at step {failed_step}: {failure_msg}",
            file=sys.stderr,
        )
        print(f"[{name}] pipe-pane log: {pane_log}", file=sys.stderr)
        print(f"[{name}] transcript:    {transcript}", file=sys.stderr)
        if expected_failure:
            print(f"[{name}] expected_failure_reason: {reason}", file=sys.stderr)
            return 77
        return 1

    if expected_failure:
        print(
            f"[{name}] XPASS — scenario was marked expected_failure but passed. "
            f"Remove the marker. Original reason: {reason}",
            file=sys.stderr,
        )
        return 78
    print(f"[{name}] PASS", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a single e2e scenario.")
    parser.add_argument("scenario", type=Path, help="path to scenario YAML")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get("HARNESS_E2E_LOG_DIR", "/tmp/harness-e2e")),
        help="directory for pipe-pane logs and transcripts",
    )
    args = parser.parse_args(argv)

    if not args.scenario.exists():
        print(f"scenario not found: {args.scenario}", file=sys.stderr)
        return 2

    try:
        return run_scenario(args.scenario, args.log_dir)
    except Exception as exc:  # noqa: BLE001 — top-level guard so a bug here doesn't crash silently
        print(f"[run_scenario] unhandled error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
