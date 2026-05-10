#!/usr/bin/env bash
# tests/e2e/lib/wait.sh — re-export wait functions.
#
# The wait helpers (`e2e_wait_for_marker`, `e2e_wait_stable`) live in
# tmux_driver.sh alongside the rest of the driver. This file exists as a
# stable import point for callers that only want the wait API and don't
# want to know about the driver internals. Sourcing it pulls in the full
# driver — the functions are cheap and there's no other code path that
# can satisfy the wait helpers' dependencies without the driver.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/e2e/lib/tmux_driver.sh
. "$SCRIPT_DIR/tmux_driver.sh"
