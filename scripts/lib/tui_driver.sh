# scripts/lib/tui_driver.sh — sourceable bash toolkit for driving AI-coding
# TUIs (claude-code, opencode) inside harness-agent containers via tmux.
#
# Source from a test:
#
#   source "$REPO_ROOT/scripts/lib/tui_driver.sh"
#   tui_wait_for_text "$cname" harness-agent 'Welcome' 30
#   tui_send_line "$cname" harness-agent 'say hello'
#   tui_wait_agent_done "$cname" harness-agent 60
#   tui_assert_response_contains "$cname" harness-agent 'Hello from mock'
#
# Three constraints baked in (each from a real Phase 5/6 bug):
#   1. Enter MUST be sent as hex 0d via `tmux send-keys -H 0d`. The keyword
#      form (`send-keys ... Enter`) silently fails for claude-code and other
#      Ink/React TUIs.
#   2. All `docker exec` calls use `--user harness`. tmux runs under harness
#      after the entrypoint's gosu drop; root's tmux looks at /tmp/tmux-0/
#      and won't find the session.
#   3. ANSI escape sequences vary between TUIs. tui_strip_ansi handles CSI,
#      OSC, DCS, charset designations, and SO/SI shifts before regex match.
#
# All log lines from the toolkit go to stderr so callers can capture
# function output cleanly.

# Cross-platform helpers (harness_docker). Sourced defensively so this file
# can be loaded standalone — most callers also load it via test_helpers.sh
# or directly, but neither path guarantees platform.sh is already in scope.
# shellcheck disable=SC1091
if ! declare -F harness_docker >/dev/null 2>&1; then
    _td_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    source "${_td_dir}/platform.sh"
    unset _td_dir
fi

# --- low-level ----------------------------------------------------------------

# Strip ANSI escape sequences from text on stdin.
# Regex order:
#   - CSI:        \x1b[...<final>     (most common; cursor moves, colors)
#   - OSC:        \x1b]...(BEL|ST)    (window titles, hyperlinks)
#   - DCS:        \x1bP...ST          (device control strings)
#   - charset:    \x1b(<set>          (G0/G1 designations)
#   - SO/SI:      \x0e / \x0f         (alt charset shifts)
tui_strip_ansi() {
    # Final substitution normalizes NBSP (U+00A0, bytes 0xC2 0xA0) to a
    # regular ASCII space. Claude Code >= 2.1 pads its empty input prompt
    # row with NBSPs, and POSIX [[:space:]] does not match NBSP, so
    # downstream regex matchers (notably tui_wait_agent_done's ready_re)
    # silently fail unless we normalize at the source.
    sed -E \
        -e 's/\x1b\[[0-9;:?<=>]*[a-zA-Z]//g' \
        -e 's/\x1b\][^\x07\x1b]*(\x07|\x1b\\)//g' \
        -e 's/\x1bP[^\x1b]*(\x1b\\|$)//g' \
        -e 's/\x1b[()][0-9A-Za-z]//g' \
        -e 's/[\x0e\x0f]//g' \
        -e 's/\xc2\xa0/ /g'
}

# Capture the current pane content, ANSI-stripped.
# Args: <container> <session>
# -J joins wrapped lines so regex matchers don't have to deal with mid-word
# linebreaks introduced by terminal width.
tui_capture_clean() {
    local container="$1" session="$2"
    harness_docker exec --user harness "$container" \
        tmux capture-pane -t "$session" -p -J 2>/dev/null \
        | tui_strip_ansi
}

# Send a single key event. Enter takes the hex path; everything else uses
# tmux's keyword names (Up, Down, Tab, BSpace, etc.).
# Args: <container> <session> <key>
tui_send_key() {
    local container="$1" session="$2" key="$3"
    case "$key" in
        Enter|enter|RET|return)
            # Hex 0d (CR). The keyword form is the known footgun.
            harness_docker exec --user harness "$container" \
                tmux send-keys -t "$session" -H 0d
            ;;
        *)
            harness_docker exec --user harness "$container" \
                tmux send-keys -t "$session" "$key"
            ;;
    esac
}

# Send literal text — no key interpretation. Safe for content that contains
# tmux keyword tokens (Enter, BSpace, etc.) that would otherwise be
# interpreted.
# Args: <container> <session> <text>
tui_send_text() {
    local container="$1" session="$2" text="$3"
    harness_docker exec --user harness "$container" \
        tmux send-keys -t "$session" -l "$text"
}

# Common case: type text + press Enter. Inserts a small delay between the
# two so the TUI sees the text settle before the submit. Without the gap
# some TUIs treat the whole stream as one paste and lose the Enter.
# Args: <container> <session> <text>
tui_send_line() {
    local container="$1" session="$2" text="$3"
    tui_send_text "$container" "$session" "$text"
    sleep 0.1
    tui_send_key "$container" "$session" Enter
}

# Paste multi-line text reliably. send-keys -l with embedded newlines is
# unreliable across TUIs; the buffer dance always works.
# Args: <container> <session> <multiline-text>
tui_paste() {
    local container="$1" session="$2" text="$3"
    local tmp="/tmp/tui_paste_$$_$RANDOM"
    # Stream the text into a file inside the container.
    printf '%s' "$text" \
        | harness_docker exec -i --user harness "$container" \
            bash -c "cat > '$tmp'"
    harness_docker exec --user harness "$container" \
        tmux load-buffer "$tmp" 2>/dev/null || true
    harness_docker exec --user harness "$container" \
        tmux paste-buffer -t "$session" 2>/dev/null || true
    harness_docker exec --user harness "$container" \
        rm -f "$tmp" 2>/dev/null || true
}

# Kill a tmux session inside a container. Idempotent.
# Args: <container> <session>
tui_kill_session() {
    local container="$1" session="$2"
    harness_docker exec --user harness "$container" \
        tmux kill-session -t "$session" 2>/dev/null || true
}

# --- waiters ------------------------------------------------------------------

# Wait for the screen to be "idle" — capture hash unchanged for
# <stable_seconds> consecutive 0.5s polls. Returns 0 if stable, 1 if timeout.
# Args: <container> <session> [stable=2] [timeout=60]
tui_wait_idle() {
    local container="$1" session="$2"
    local stable="${3:-2}" timeout_s="${4:-60}"
    local needed=$(( stable * 2 ))   # 0.5s polls
    local stable_count=0
    local last="" cur=""
    local deadline=$(( $(date +%s) + timeout_s ))
    while (( $(date +%s) < deadline )); do
        cur=$(tui_capture_clean "$container" "$session" | md5sum | awk '{print $1}')
        if [[ "$cur" == "$last" && -n "$cur" ]]; then
            stable_count=$(( stable_count + 1 ))
            if (( stable_count >= needed )); then
                return 0
            fi
        else
            stable_count=0
            last="$cur"
        fi
        sleep 0.5
    done
    return 1
}

# Wait for a regex pattern to appear in the (ANSI-stripped) capture.
# Args: <container> <session> <pattern> [timeout=60]
tui_wait_for_text() {
    local container="$1" session="$2" pattern="$3"
    local timeout_s="${4:-60}"
    local deadline=$(( $(date +%s) + timeout_s ))
    while (( $(date +%s) < deadline )); do
        if tui_capture_clean "$container" "$session" \
            | grep -Eq "$pattern"; then
            return 0
        fi
        sleep 0.5
    done
    echo "[tui_driver] timeout waiting for: $pattern" >&2
    return 1
}

# Wait for the agent to finish processing. Two-phase:
#
#   Phase 1: BUSY appears (or short-circuit if already showing prompt-ready).
#   Phase 2: BUSY disappears AND prompt-ready marker visible on last line.
#
# Spinner words are matched case-insensitively in the last 12 lines so a
# single "Running" earlier in the scrollback doesn't pin us forever.
# Prompt-ready markers: claude shows ❯, opencode shows > or │ >.
#
# Args: <container> <session> [timeout=120]
tui_wait_agent_done() {
    local container="$1" session="$2"
    local timeout_s="${3:-120}"
    # Spinner-word list — case-insensitive. Extend if a TUI uses a word not
    # listed here. claude-code (Ink) covers most of these; opencode (Bubble
    # Tea) tends to use Loading/Generating/Running.
    local spin_re='Running|Thinking|Searching|Reading|Writing|Editing|Pondering|Considering|Generating|Loading|Analyzing|Compiling|Computing|Processing|Working'
    # Prompt-ready markers — claude shows ❯ on its own line; opencode shows
    # `│ > `. We grep across the last ~12 lines (not just the absolute last
    # non-empty line) because newer Claude Code (>= 2.1.119) renders a
    # "? for shortcuts" help row *below* the prompt, so the last non-empty
    # line is help text, not the ❯ marker.
    local ready_re='(^|[[:space:]])❯[[:space:]]*$|│[[:space:]]*>[[:space:]]'
    local deadline=$(( $(date +%s) + timeout_s ))
    local saw_busy=0
    local busy_deadline=$(( $(date +%s) + 5 ))   # grace before deciding agent already idle

    while (( $(date +%s) < deadline )); do
        local cap tail_text
        cap=$(tui_capture_clean "$container" "$session")
        tail_text=$(tail -12 <<<"$cap")

        if (( ! saw_busy )); then
            if grep -Eqi "$spin_re" <<<"$tail_text"; then
                saw_busy=1
            elif (( $(date +%s) >= busy_deadline )); then
                # Never saw busy — agent may have already finished. Check for
                # ready marker; if present, treat as done.
                if grep -Eq "$ready_re" <<<"$tail_text"; then
                    return 0
                fi
            fi
        else
            # Busy was seen; wait for its disappearance + ready marker.
            if ! grep -Eqi "$spin_re" <<<"$tail_text" \
                && grep -Eq "$ready_re" <<<"$tail_text"; then
                return 0
            fi
        fi
        sleep 0.5
    done
    echo "[tui_driver] timeout waiting for agent to finish" >&2
    return 1
}

# Compose helper: send a prompt, wait for the agent to finish, return 0/1.
# Args: <container> <session> <prompt> [timeout=120]
tui_prompt_and_wait() {
    local container="$1" session="$2" prompt="$3"
    local timeout_s="${4:-120}"
    tui_send_line "$container" "$session" "$prompt"
    tui_wait_agent_done "$container" "$session" "$timeout_s"
}

# --- assertions ---------------------------------------------------------------

# Verify the current pane (ANSI-stripped) contains a regex pattern. Returns
# 0/1 with no output on success; on failure dumps the cleaned pane to stderr
# so the caller can investigate without re-capturing.
# Args: <container> <session> <pattern>
tui_assert_response_contains() {
    local container="$1" session="$2" pattern="$3"
    local cap
    cap=$(tui_capture_clean "$container" "$session")
    if grep -Eq "$pattern" <<<"$cap"; then
        return 0
    fi
    {
        echo "[tui_driver] assertion failed: '$pattern' not found"
        echo "--- pane (ANSI stripped) ---"
        echo "$cap"
        echo "--- end pane ---"
    } >&2
    return 1
}
