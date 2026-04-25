#!/usr/bin/env bash
#
# harness-claude-agent entrypoint.
#
# Maps harness env vars onto claude-code's runtime config:
#   ANTHROPIC_BASE_URL          — must be set by the caller (Phase 4's `harness`
#                                 script). Validated here.
#   ANTHROPIC_API_KEY /
#   ANTHROPIC_AUTH_TOKEN        — at least one is required by claude-code, even
#                                 a dummy value. Filled with a placeholder if
#                                 absent.
#   ANTHROPIC_MODEL,
#   ANTHROPIC_SMALL_FAST_MODEL  — set by the caller to the stub model name so
#                                 every claude-code request lands on our proxy.
#
# Harness-level controls:
#   HARNESS_YOLO=1              — adds --dangerously-skip-permissions
#   HARNESS_TEST_MODE=1         — bypass tmux, exec claude directly (for
#                                 scripts/agent_test.sh)
#   HARNESS_AGENT_ARGS          — currently unused; any extras come in via "$@"
#
# Normal mode wraps claude in a detached tmux session named `harness-agent`
# so a user can reattach from the host. After the wrapped command exits we
# print a status line and sleep 30s before the session ends, giving anyone
# attaching just-too-late a chance to see the exit code.

set -euo pipefail

# --- UID remap --------------------------------------------------------------
#
# The image's default USER is harness (uid 1000). Phase 4's `harness` script
# overrides that with `docker run --user 0:0` so the container starts as
# root, this block fires, and we remap harness's uid/gid to match the
# host caller before re-execing as harness via gosu. Files written into
# bind-mounted volumes are then owned by the host user.
#
# When the container is started without --user (e.g. agent_test.sh in test
# mode), it runs as harness directly — id -u is non-zero, this block is
# skipped, and execution continues as before.
if [[ "$(id -u)" == "0" && -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
    current_uid=$(id -u harness 2>/dev/null || echo "")
    current_gid=$(id -g harness 2>/dev/null || echo "")
    if [[ "${current_uid}" != "${HOST_UID}" || "${current_gid}" != "${HOST_GID}" ]]; then
        # -o allows duplicate ids — defends against hosts where uid 0/whatever
        # is already claimed by another account inside the image.
        groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
            || groupadd -g "${HOST_GID}" -o harness
        usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness
        chown -R "${HOST_UID}:${HOST_GID}" /home/harness 2>/dev/null || true
    fi
    exec gosu harness "$0" "$@"
fi

# --- env validation ---------------------------------------------------------

if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
    echo "[harness-claude] ERROR: ANTHROPIC_BASE_URL is not set" >&2
    echo "[harness-claude]   This must point at the in-network ollama (e.g. http://ollama:11434)" >&2
    exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    echo "[harness-claude] WARN: no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set; using dummy"
    export ANTHROPIC_API_KEY="harness-dummy"
fi

# Suppress claude-code's auto-update phone-home. Respected by claude-code.
export DISABLE_AUTOUPDATER=1

# Phase 4 may bind-mount ~/.claude empty (or not at all). Make sure the dir
# exists so claude-code doesn't error trying to read its config dir.
mkdir -p "${HOME}/.claude"

# --- banner -----------------------------------------------------------------

echo "============================================================"
echo " harness-claude-agent"
echo "   model:    ${OLLAMA_AGENT_MODEL:-<unset>}"
echo "   base_url: ${ANTHROPIC_BASE_URL}"
echo "   yolo:     ${HARNESS_YOLO:-0}"
echo "   test:     ${HARNESS_TEST_MODE:-0}"
echo "============================================================"

# --- argv assembly ----------------------------------------------------------

claude_args=()
if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
    claude_args+=(--dangerously-skip-permissions)
fi
# Append everything the caller passed verbatim.
claude_args+=("$@")

# --- test mode: exec claude directly, no tmux -------------------------------

if [[ "${HARNESS_TEST_MODE:-0}" == "1" ]]; then
    exec claude "${claude_args[@]}"
fi

# --- normal mode: wrap claude in a detached tmux session --------------------
#
# We build the inner shell command by quoting each arg with `printf %q` so
# it survives interpolation into the tmux `new-session` command string.
# tmux's command parser splits on shell-style whitespace, so unquoted args
# would be re-parsed.

inner=$(printf 'claude'; for a in "${claude_args[@]}"; do printf ' %q' "$a"; done)

tmux new-session -d -s harness-agent \
    "${inner}; ec=\$?; echo; echo '[harness] agent exited (code '\$ec')'; sleep 30; exit \$ec"

# Park here until the session ends. Phase 4's `harness` script `docker exec`s
# into this container to attach with `tmux attach -t harness-agent`.
while tmux has-session -t harness-agent 2>/dev/null; do
    sleep 5
done
