#!/usr/bin/env bash
#
# live-run.sh EVENTS_FILE [opencode run args...]
#
# Headless `opencode run --format json`, writing its event stream to
# EVENTS_FILE and exiting with run's status, but with the session open to
# messages from outside while it works (the pub page's run viewer). A plain
# `opencode run` keeps its server in-process with no port, so nothing can
# reach a running session; here the run attaches to its own
# `opencode serve` on 127.0.0.1 (random port, random password), and a relay
# hands each message dropped in the inbox to that server's prompt_async,
# which joins the busy session's loop at the next step.
#
#   LIVE=${XDG_DATA_HOME:-$HOME/.local/share}/harness/live
#   LIVE/<session>.json        the run is live; the relay touches it each second
#   LIVE/<session>.inbox/*.json a prompt_async body each; renamed .sent, or
#                               .failed-<http code>, or .late if the run ended
#
# HARNESS_LIVE=0, or a serve that does not come up, runs plain `opencode run`.
# Used by agents/entrypoint.sh (container) and the harness host launch.
#
# Prints "[harness] session: <id>" to stderr as soon as the session is known
# (after the run on the plain path): `harness queue continue` resumes a stage
# that was cut off in that session, and finds it in the stage's output.

set -u
events="$1"; shift

session_of() { grep -o '"sessionID": *"ses_[A-Za-z0-9]*"' "$events" 2>/dev/null | head -1 | cut -d'"' -f4; }
plain() {
    local rc=0 s
    opencode run --format json "$@" >"$events" || rc=$?
    s=$(session_of)
    [[ -n "$s" ]] && echo "[harness] session: $s" >&2
    return "$rc"
}

live="${XDG_DATA_HOME:-$HOME/.local/share}/harness/live"
if [[ "${HARNESS_LIVE:-1}" == "0" ]] || ! mkdir -p "$live" 2>/dev/null \
    || ! command -v curl >/dev/null 2>&1; then
    plain "$@"; exit $?
fi

pw=$(od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n')
[[ -n "$pw" ]] || pw="$RANDOM$RANDOM$RANDOM$$"
serve_log=$(mktemp)
OPENCODE_SERVER_PASSWORD="$pw" opencode serve --hostname 127.0.0.1 --port 0 >"$serve_log" 2>&1 &
serve_pid=$!
run_pid="" sid="" marker="" inbox=""
# Killed (the queue's stage timeout, Ctrl-C, docker stop): take serve and run
# down too, and drop the marker so the run stops showing as live at once.
stop() {
    kill ${run_pid:+"$run_pid"} "$serve_pid" 2>/dev/null
    [[ -n "$marker" ]] && rm -f "$marker"
    rm -f "$serve_log"
    exit "$1"
}
trap 'stop 143' TERM; trap 'stop 130' INT; trap 'stop 129' HUP
url=""
for _ in $(seq 1 100); do
    url=$(grep -o 'http://127\.0\.0\.1:[0-9]*' "$serve_log" 2>/dev/null | head -1)
    [[ -n "$url" ]] && break
    kill -0 "$serve_pid" 2>/dev/null || break
    sleep 0.2
done
if [[ -z "$url" ]]; then
    echo "[harness] live: opencode serve did not start; running without message injection" >&2
    kill "$serve_pid" 2>/dev/null; wait "$serve_pid" 2>/dev/null
    rm -f "$serve_log"
    plain "$@"; exit $?
fi

OPENCODE_SERVER_PASSWORD="$pw" opencode run --attach "$url" --format json "$@" >"$events" &
run_pid=$!

deliver() {
    local f code
    for f in "$inbox"/*.json; do
        [[ -f "$f" ]] || continue
        code=$(curl -s --noproxy '*' -m 30 -o /dev/null -w '%{http_code}' -u "opencode:$pw" \
            -H 'Content-Type: application/json' --data-binary "@$f" \
            "$url/session/$sid/prompt_async" 2>/dev/null) || true
        if [[ "$code" == 2* ]]; then
            mv -f "$f" "${f%.json}.sent"
        else
            mv -f "$f" "${f%.json}.failed-${code:-000}"
        fi
    done
}
while kill -0 "$run_pid" 2>/dev/null; do
    if [[ -z "$sid" ]]; then
        sid=$(session_of)
        if [[ -n "$sid" ]]; then
            echo "[harness] session: $sid" >&2
            marker="$live/$sid.json" inbox="$live/$sid.inbox"
            mkdir -p "$inbox"
            printf '{"session":"%s","pid":%s,"started":%s}\n' "$sid" "$$" "$(date +%s)" >"$marker"
        fi
    fi
    if [[ -n "$sid" ]]; then
        touch "$marker"
        deliver
    fi
    sleep 1
done
wait "$run_pid"; rc=$?

if [[ -n "$sid" ]]; then
    rm -f "$marker"
    for f in "$inbox"/*.json; do
        [[ -f "$f" ]] && mv -f "$f" "${f%.json}.late"
    done
fi
kill "$serve_pid" 2>/dev/null; wait "$serve_pid" 2>/dev/null
rm -f "$serve_log"
exit "$rc"
