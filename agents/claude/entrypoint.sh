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
#                                 absent. AUTH_TOKEN is preferred over API_KEY
#                                 for the dummy: setting AUTH_TOKEN causes
#                                 claude-code's startup connectivity probe to
#                                 api.anthropic.com to be skipped, which
#                                 matters because our default firewall does
#                                 not allowlist api.anthropic.com.
#   ANTHROPIC_MODEL,
#   ANTHROPIC_SMALL_FAST_MODEL  — set by the caller to the stub model name so
#                                 every claude-code request lands on our proxy.
#
# Harness-level controls:
#   HARNESS_YOLO=1              — adds --dangerously-skip-permissions
#   HARNESS_TEST_MODE=1         — bypass tmux, exec claude directly (for
#                                 scripts/agent_test.sh)
#   HARNESS_PRINT_MODE=1        — same effect as HARNESS_TEST_MODE: bypass
#                                 tmux and exec claude directly. Used by
#                                 `harness claude -p ...` for headless
#                                 single-shot invocations from the host.
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
    # --- firewall (root-side) -------------------------------------------------
    #
    # Lay down the egress firewall before dropping privileges. iptables/ipset
    # require NET_ADMIN/NET_RAW which gosu does NOT preserve when stepping
    # down to a non-zero uid. Done unconditionally before the uid remap so a
    # missing HARNESS_TEST_MODE / HARNESS_PRINT_MODE container still gets
    # locked down.
    if [[ -x /usr/local/bin/init-firewall.sh ]]; then
        /usr/local/bin/init-firewall.sh
    else
        echo "[agent-entrypoint] WARN: init-firewall.sh missing; running without firewall" >&2
    fi

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

# --- firewall (no-remap path) ----------------------------------------------
#
# When the container is started without --user 0:0 (e.g. agent_test.sh in
# test mode), we land here directly as harness. The firewall would still be
# desirable, but iptables needs NET_ADMIN — which the harness user does not
# have. So we only run it on the root branch above; in test mode the test
# scripts opt out of the firewall via their own startup paths.

# --- git credentials (user-side) --------------------------------------------
#
# Runs after the gosu drop so `git config --global` writes to
# /home/harness/.gitconfig (not /root/.gitconfig). Best-effort: a missing
# allowlist is unusual — the firewall would have already failed — but we
# don't want to make the agent itself unstartable on credential setup
# hiccups.
if [[ -x /usr/local/bin/configure-git-credentials.sh ]]; then
    /usr/local/bin/configure-git-credentials.sh /etc/harness/allowlist \
        || echo "[agent-entrypoint] WARN: configure-git-credentials.sh failed; git push protection may be incomplete" >&2
fi

# --- skel seed --------------------------------------------------------------
#
# The harness script bind-mounts <install-root>/agent/claude over
# /home/harness, so on first run the home dir is empty (or only contains the
# user's bring-along files). Restore the build-time skeleton (~/.bashrc,
# pipx's data dir layout, etc.) once, marked by ~/.harness-home-initialized.
# `cp -an` is "archive + no-clobber" so any file the user already placed in
# the bind mount wins. Failures on individual files (perms quirks) shouldn't
# abort the agent — hence the `|| true`.
if [[ ! -f "${HOME}/.harness-home-initialized" ]]; then
    if [[ -d /etc/skel/harness ]]; then
        cp -an /etc/skel/harness/. "${HOME}/" 2>/dev/null || true
    fi
    touch "${HOME}/.harness-home-initialized" 2>/dev/null || true
fi

# --- env validation ---------------------------------------------------------

if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
    echo "[harness-claude] ERROR: ANTHROPIC_BASE_URL is not set" >&2
    echo "[harness-claude]   This must point at the in-network ollama (e.g. http://ollama:11434)" >&2
    exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    echo "[harness-claude] WARN: no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set; using dummy"
    # ANTHROPIC_AUTH_TOKEN (not API_KEY) — claude-code's startup preflight only
    # skips the api.anthropic.com /api/hello + /v1/oauth/hello probe when one
    # of: AUTH_TOKEN, apiKeyHelper, CLAUDE_CODE_USE_BEDROCK/VERTEX/etc, or
    # API_KEY routed through `en8()` is set. Plain ANTHROPIC_API_KEY does NOT
    # take that path here, so the probe runs and hard-exits behind our
    # default firewall (api.anthropic.com is not allowlisted by design).
    export ANTHROPIC_AUTH_TOKEN="harness-dummy"
fi

# Suppress claude-code's auto-update phone-home. Respected by claude-code.
export DISABLE_AUTOUPDATER=1

# Phase 4 may bind-mount ~/.claude empty (or not at all). Make sure the dir
# exists so claude-code doesn't error trying to read its config dir.
mkdir -p "${HOME}/.claude"

# Suppress the "Co-Authored-By: Claude" trailer that claude-code adds to
# git commits by default. The harness threat model includes "no accidental
# attribution leakage to commits the user pushes from a host shell"; the
# block on `git push` from inside the agent (configure-git-credentials.sh)
# makes that less load-bearing, but the trailer would still show up if the
# user later pushed a branch the agent committed onto. Left enabled, the
# trailer is a quiet ergonomic surprise; turning it off is the conservative
# default. Users can re-enable it by editing ~/.claude/settings.json.
#
# Only seed the file if it doesn't exist — preserve any user customizations
# from previous runs. jq merge would be safer but we want zero deps on the
# claude settings format, which has changed shape across versions.
if [[ ! -f "${HOME}/.claude/settings.json" ]]; then
    cat > "${HOME}/.claude/settings.json" <<'EOF'
{
  "includeCoAuthoredBy": false
}
EOF
fi

# B3-MANAGED: claude-statusline — wire up ccstatusline as the claude
# statusLine command. The image pre-installs ccstatusline globally; we just
# need claude to invoke it. Idempotent: if the user has already configured a
# statusLine block (e.g. via /statusline), preserve it. We only stamp our
# default into the file when no statusLine key is present at all.
if command -v jq >/dev/null 2>&1 && [[ -f "${HOME}/.claude/settings.json" ]]; then
    has_status=$(jq 'has("statusLine")' "${HOME}/.claude/settings.json" 2>/dev/null || echo "false")
    if [[ "$has_status" != "true" ]]; then
        tmp_settings="${HOME}/.claude/settings.json.tmp.$$"
        if jq '. + {
            "statusLine": {
                "type": "command",
                "command": "ccstatusline",
                "padding": 0
            }
        }' "${HOME}/.claude/settings.json" >"$tmp_settings" 2>/dev/null; then
            mv "$tmp_settings" "${HOME}/.claude/settings.json"
        else
            rm -f "$tmp_settings" 2>/dev/null || true
        fi
    fi
fi

# --- MCP server config merge ------------------------------------------------
#
# The harness script writes the merged set of registry MCP entries to
# ~/.harness-mcp-servers.json (in claude's `{"mcpServers": {...}}` shape) so
# we can fold them into ~/.claude.json without duplicating per-MCP knowledge
# in the host script. We re-merge on every container start so disabling an
# MCP via `harness mcp disable` propagates immediately on the next agent
# launch. User-added entries in ~/.claude.json's mcpServers block are
# preserved (only keys that the harness owns are overwritten).
if [[ -f "${HOME}/.harness-mcp-servers.json" ]] && command -v jq >/dev/null 2>&1; then
    claude_config="${HOME}/.claude.json"
    if [[ ! -f "${claude_config}" ]]; then
        echo "{}" > "${claude_config}"
    fi
    merged=$(jq -s '
        .[0] as $existing
        | .[1] as $harness
        | $existing
        | .mcpServers = ((.mcpServers // {}) + ($harness.mcpServers // {}))
    ' "${claude_config}" "${HOME}/.harness-mcp-servers.json" 2>/dev/null || true)
    if [[ -n "${merged}" ]]; then
        printf '%s\n' "${merged}" > "${claude_config}"
    fi
fi

# --- banner -----------------------------------------------------------------

echo "============================================================"
echo " harness-claude-agent"
echo "   model:    ${OLLAMA_AGENT_MODEL:-<unset>}"
echo "   base_url: ${ANTHROPIC_BASE_URL}"
echo "   yolo:     ${HARNESS_YOLO:-0}"
echo "   test:     ${HARNESS_TEST_MODE:-0}"
echo "   print:    ${HARNESS_PRINT_MODE:-0}"
echo "============================================================"

# --- argv assembly ----------------------------------------------------------

claude_args=()
if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
    claude_args+=(--dangerously-skip-permissions)
fi
# Append everything the caller passed verbatim.
claude_args+=("$@")

# --- test/print mode: exec claude directly, no tmux -------------------------
#
# Both HARNESS_TEST_MODE and HARNESS_PRINT_MODE bypass tmux. Test mode is set
# by scripts/agent_test.sh; print mode is set by `harness claude -p ...` so
# stdout from claude propagates straight back to the invoking shell.
# claude's own `-p` flag is part of "$@" already (the harness script forwards
# it verbatim) so we don't add it here.

if [[ "${HARNESS_TEST_MODE:-0}" == "1" || "${HARNESS_PRINT_MODE:-0}" == "1" ]]; then
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
