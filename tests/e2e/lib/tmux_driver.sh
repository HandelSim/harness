#!/usr/bin/env bash
# tests/e2e/lib/tmux_driver.sh
#
# tmux driver for e2e TUI tests. Sourceable bash library.
#
# Why these functions exist (the tmux gotchas they paper over):
#
#   1. send-keys escaping. `tmux send-keys` interprets bareword arguments as
#      named keys (Enter, Escape, C-c, Tab). Literal text must use `-l`.
#      Mixing literal and named in one call is a footgun, so
#      `e2e_send_literal` always sends literal text first and the optional
#      named key in a separate call.
#
#   2. Multi-line content. send-keys -l does not trigger bracketed paste,
#      which some TUIs require to treat input as a paste rather than a
#      sequence of keystrokes. `e2e_paste` uses load-buffer + paste-buffer
#      to get the bracketed-paste behavior.
#
#   3. Pane geometry. TUIs reflow based on terminal size. Reproducibility
#      requires fixing the geometry up-front via `new-session -x -y`.
#      Defaults: 200x50.
#
#   4. Scrollback. tmux default history-limit is 2000 lines; long captures
#      get truncated. We set 50000 per session.
#
#   5. capture-pane vs pipe-pane. `capture-pane -S -` grabs the current
#      visible buffer plus full scrollback. `pipe-pane` streams every byte
#      written to the pane to a file, including bytes that scrolled out of
#      the scrollback buffer. Use both: capture-pane for assertions,
#      pipe-pane for forensic logs.
#
#   6. TERM. Default tmux TERM is `screen` which strips colors and some
#      unicode. e2e_session_start exports TERM=xterm-256color in the first
#      command sent to the session.
#
#   7. Streaming output. After sending input, output streams in. Asserting
#      against capture-pane immediately is racy. Two waits are provided:
#      `e2e_wait_for_marker` (poll until a string appears) and
#      `e2e_wait_stable` (poll until the screen stops changing for N ms).
#
#   8. remain-on-exit. We avoid it. tmux #1663 documents a case where
#      capture-pane truncates output on a session that was never attached
#      and had remain-on-exit set.
#
# Source from a runner:
#
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$SCRIPT_DIR/lib/tmux_driver.sh"
#     e2e_session_start mysession 200 50
#     e2e_pipe_pane mysession /tmp/mysession.log
#     e2e_send_keys mysession "echo hello" Enter
#     e2e_wait_for_marker mysession "hello" 5
#     e2e_capture mysession | grep -q hello
#     e2e_session_kill mysession

# Note: deliberately NOT set -e here. Callers vary in how they want to
# handle the return codes of individual driver functions, and `set -e`
# inside a sourced library breaks pipeline-style checks like
# `if e2e_wait_for_marker ...; then`.
set -uo pipefail

# Start a fresh tmux session of the given size. Kills any existing session
# of the same name first so callers don't have to do that themselves.
# Args:
#   $1  session name
#   $2  width  (default 200)
#   $3  height (default 50)
e2e_session_start() {
    local name="$1"
    local width="${2:-200}"
    local height="${3:-50}"
    tmux kill-session -t "$name" 2>/dev/null || true
    tmux new-session -d -s "$name" -x "$width" -y "$height"
    tmux set-option -t "$name" history-limit 50000
    # Set TERM and clear screen so subsequent captures start from a known
    # baseline (just a shell prompt at top of pane).
    tmux send-keys -t "$name" 'export TERM=xterm-256color; clear' Enter
    sleep 0.3
}

# Stream every byte written to the session's pane into a file. Useful when
# capture-pane only shows what's currently visible plus scrollback, but
# scrollback isn't enough.
# Args:
#   $1  session name
#   $2  log file path (will be appended to)
e2e_pipe_pane() {
    local name="$1"
    local log="$2"
    tmux pipe-pane -t "$name" "cat >> $log"
}

# Pass-through to `tmux send-keys -t <session>`. Use for named keys
# (Enter, Escape, C-c, Tab) or short literal strings with no `-l`. For
# arbitrary text use e2e_send_literal or e2e_paste.
# Args:
#   $1   session name
#   $2…  arguments forwarded to send-keys
e2e_send_keys() {
    local name="$1"; shift
    tmux send-keys -t "$name" "$@"
}

# Send literal text (no key-name interpretation) followed by an optional
# named key. This is the safe way to type a string that might contain
# tokens tmux would otherwise treat as named keys (e.g. "Tab", "Enter").
# Args:
#   $1  session name
#   $2  literal text
#   $3  named key to send afterwards (optional; e.g. Enter)
e2e_send_literal() {
    local name="$1"
    local text="$2"
    local key="${3:-}"
    tmux send-keys -t "$name" -l -- "$text"
    if [[ -n "$key" ]]; then
        tmux send-keys -t "$name" "$key"
    fi
}

# Paste multi-line content using a tmux buffer. This triggers bracketed
# paste mode, which TUIs that distinguish typing from pasting (e.g. some
# editor/REPL modes) handle correctly. Does NOT append a newline; pair
# with a follow-up `e2e_send_keys <session> Enter` if needed.
# Args:
#   $1  session name
#   $2  content (may contain newlines)
e2e_paste() {
    local name="$1"
    local content="$2"
    printf '%s' "$content" | tmux load-buffer -
    tmux paste-buffer -t "$name"
}

# Print the current pane contents (visible + scrollback) to stdout.
# `-S -` says "start from the beginning of scrollback".
# Args:
#   $1  session name
e2e_capture() {
    local name="$1"
    tmux capture-pane -t "$name" -p -S -
}

# Block until `marker` appears in the pane capture or `timeout` seconds
# have elapsed. Polls every 0.5s. Returns 0 on hit, 1 on timeout.
# Args:
#   $1  session name
#   $2  marker substring (literal, fixed-string match)
#   $3  timeout in seconds (default 30)
e2e_wait_for_marker() {
    local name="$1"
    local marker="$2"
    local timeout="${3:-30}"
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        if e2e_capture "$name" | grep -qF "$marker"; then
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    echo "TIMEOUT waiting for marker '$marker' in session $name" >&2
    return 1
}

# Block until the pane capture stops changing for `stable_ms` milliseconds,
# or until `timeout` seconds have elapsed. Useful when there's no single
# marker to wait for but the screen still settles after streaming. Polls
# every 200ms. Returns 0 on stability, 1 on timeout.
# Args:
#   $1  session name
#   $2  required stability window in ms (default 1000)
#   $3  overall timeout in seconds (default 60)
e2e_wait_stable() {
    local name="$1"
    local stable_ms="${2:-1000}"
    local timeout="${3:-60}"
    local elapsed=0
    local stable_seen_ms=0
    local prev_hash=""
    local interval_ms=200
    while [[ $elapsed -lt $((timeout * 1000)) ]]; do
        local cur_hash
        cur_hash=$(e2e_capture "$name" | sha256sum | cut -d' ' -f1)
        if [[ "$cur_hash" == "$prev_hash" ]]; then
            stable_seen_ms=$((stable_seen_ms + interval_ms))
            if [[ $stable_seen_ms -ge $stable_ms ]]; then
                return 0
            fi
        else
            stable_seen_ms=0
            prev_hash="$cur_hash"
        fi
        sleep 0.2
        elapsed=$((elapsed + interval_ms))
    done
    echo "TIMEOUT waiting for stable output in session $name" >&2
    return 1
}

# Kill a tmux session if it exists. Safe to call multiple times.
# Args:
#   $1  session name
e2e_session_kill() {
    local name="$1"
    tmux kill-session -t "$name" 2>/dev/null || true
}
