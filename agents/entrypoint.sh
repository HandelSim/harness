#!/usr/bin/env bash
#
# harness-agent — unified entrypoint dispatching on a mode argument:
#
#   claude    — run claude-code
#   opencode  — run opencode
#   shell     — drop into bash inside the container (for installing skills,
#                debugging, etc.)
#
# Replaces the prior per-tool entrypoints. All shared infrastructure (UID
# remap, firewall, gosu drop, skel seed, git config) runs once at the top,
# regardless of mode; mode dispatch happens after privilege drop.
#
# Common harness-level controls:
#   HARNESS_YOLO=1              — claude: --dangerously-skip-permissions
#                                 opencode: --agent yolo
#   HARNESS_PRINT_MODE=1        — `harness <agent> -p ...` headless single-shot
#   HARNESS_HOST_CWD=<path>     — host CWD; entrypoint symlinks it to /workspace
#                                 so PWD inside the container reflects the host
#                                 path (e.g. /c/Users/you/projects/myapp)
#   HARNESS_FIREWALL_DISABLED=1 — skip init-firewall.sh entirely
#                                 (--net flag or `harness net open`)

set -euo pipefail

# --- root-side init: firewall + UID remap + gosu drop -----------------------
#
# When the harness script invokes with `docker run --user 0:0`, we land here
# as root. We always drop to the `harness` user before running any agent
# code; the remap to match the host caller's uid/gid is conditional on
# HOST_UID/HOST_GID being set.
#
# Why drop unconditionally: claude-code refuses to run with
# --dangerously-skip-permissions as root, and we want test invocations
# (which don't pass --user 0:0 — they let docker default and would land
# as root because the image has no `USER` directive) to behave the same
# as production launches w.r.t. user identity.
if [[ "$(id -u)" == "0" ]]; then
    # Lay down the egress firewall before dropping privileges
    # (iptables/ipset need NET_ADMIN/NET_RAW which gosu does NOT preserve
    # when stepping down to a non-zero uid). Skipped if explicitly opted
    # out via HARNESS_FIREWALL_DISABLED=1.
    if [[ "${HARNESS_FIREWALL_DISABLED:-0}" != "1" ]]; then
        if [[ -x /usr/local/bin/init-firewall.sh ]]; then
            /usr/local/bin/init-firewall.sh \
                || echo "[agent-entrypoint] WARN: init-firewall.sh failed; continuing without firewall" >&2
        else
            echo "[agent-entrypoint] WARN: init-firewall.sh missing; running without firewall" >&2
        fi
    fi

    # UID remap is only requested when the harness script set HOST_UID/GID.
    # Test scripts that don't pass --user 0:0 may still land here (because
    # of the no-USER-directive choice); they don't need the remap.
    if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
        current_uid=$(id -u harness 2>/dev/null || echo "")
        current_gid=$(id -g harness 2>/dev/null || echo "")
        if [[ "${current_uid}" != "${HOST_UID}" || "${current_gid}" != "${HOST_GID}" ]]; then
            # -o allows duplicate ids — defends against hosts where uid
            # 0/whatever is already claimed by another account.
            groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
                || groupadd -g "${HOST_GID}" -o harness
            usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness
            chown -R "${HOST_UID}:${HOST_GID}" /home/harness 2>/dev/null || true
        fi
    fi

    # Create the host CWD symlink while still root. The harness user has no
    # write permission at /, so this MUST happen before the gosu drop.
    # (The cd into the symlinked path happens after the drop — see below;
    # gosu re-execs the entrypoint and resets CWD.)
    if [[ -n "${HARNESS_HOST_CWD:-}" && "${HARNESS_HOST_CWD}" != "/workspace" ]]; then
        parent=$(dirname "${HARNESS_HOST_CWD}")
        if [[ "$parent" != "/" && "$parent" != "." ]]; then
            mkdir -p "$parent"
        fi
        ln -snf /workspace "${HARNESS_HOST_CWD}"
        # Make the symlink owned by harness so the user can replace it
        # later if needed (defense in depth — symlink ownership rarely
        # matters for traversal but doesn't hurt). Best-effort.
        if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
            chown -h "${HOST_UID}:${HOST_GID}" "${HARNESS_HOST_CWD}" 2>/dev/null || true
        fi
    fi

    exec gosu harness "$0" "$@"
fi

# --- We're now running as the harness user ----------------------------------

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
# The harness script bind-mounts <install-root>/state/agent/home over
# /home/harness, so on first run the home dir is empty (or only contains
# the user's bring-along files). Restore the build-time skeleton (~/.bashrc,
# pipx's data dir layout, ccstatusline default config, etc.) once, marked
# by ~/.harness-home-initialized. `cp -an` is "archive + no-clobber" so any
# file the user already placed in the bind mount wins. Failures on
# individual files (perms quirks) shouldn't abort the agent — hence
# the `|| true`.
if [[ ! -f "${HOME}/.harness-home-initialized" ]]; then
    if [[ -d /etc/skel/harness ]]; then
        cp -an /etc/skel/harness/. "${HOME}/" 2>/dev/null || true
    fi
    touch "${HOME}/.harness-home-initialized" 2>/dev/null || true
fi

# --- change into host CWD path ----------------------------------------------
#
# The symlink itself was created above (still root, before the gosu drop).
# Here we just cd into it so PWD reflects the host path (e.g.
# /c/Users/you/projects/myapp) — claude-code's statusline picks it up,
# and the user isn't confused when working with multiple projects. Both
# /workspace and the host path resolve to the same files via the
# symlink. cd failure is non-fatal (we fall back to /workspace).
if [[ -n "${HARNESS_HOST_CWD:-}" && "${HARNESS_HOST_CWD}" != "/workspace" ]]; then
    if [[ -L "${HARNESS_HOST_CWD}" ]]; then
        cd "${HARNESS_HOST_CWD}" || cd /workspace
    fi
fi

# --- mode dispatch ----------------------------------------------------------

mode="${1:-claude}"
shift || true

# --- helpers shared across modes --------------------------------------------

ensure_claude_config() {
    # B3-MANAGED: claude-settings — ~/.claude/settings.json. Seed the
    # includeCoAuthoredBy: false key (suppresses the auto Co-Authored-By
    # trailer) and the statusLine block (wires up ccstatusline). Idempotent:
    # existing user customizations win on collision.
    local settings_dir="${HOME}/.claude"
    local settings_file="${settings_dir}/settings.json"
    mkdir -p "$settings_dir"

    if [[ ! -f "$settings_file" ]]; then
        cat > "$settings_file" <<'EOF'
{
  "includeCoAuthoredBy": false,
  "statusLine": {
    "type": "command",
    "command": "ccstatusline",
    "padding": 0
  }
}
EOF
        return 0
    fi

    if ! command -v jq >/dev/null 2>&1; then
        return 0
    fi

    # Merge: ensure includeCoAuthoredBy is present (default to false if
    # absent), and add the default statusLine block only if no statusLine
    # key exists at all.
    local tmp="${settings_file}.tmp.$$"
    if jq '
        (if has("includeCoAuthoredBy") then . else . + {"includeCoAuthoredBy": false} end)
        | (if has("statusLine") then . else . + {"statusLine": {"type": "command", "command": "ccstatusline", "padding": 0}} end)
    ' "$settings_file" > "$tmp" 2>/dev/null; then
        mv "$tmp" "$settings_file"
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
}

merge_claude_mcp_servers() {
    # The harness script writes the merged set of registry MCP entries to
    # ~/.harness-mcp-servers.json (in claude's `{"mcpServers": {...}}` shape).
    # Fold them into ~/.claude.json without duplicating per-MCP knowledge in
    # the host script. Re-merge every container start so disabling propagates.
    if [[ ! -f "${HOME}/.harness-mcp-servers.json" ]] || ! command -v jq >/dev/null 2>&1; then
        return 0
    fi
    local cfg="${HOME}/.claude.json"
    [[ -f "$cfg" ]] || echo "{}" > "$cfg"
    local merged
    merged=$(jq -s '
        .[0] as $existing
        | .[1] as $harness
        | $existing
        | .mcpServers = ((.mcpServers // {}) + ($harness.mcpServers // {}))
    ' "$cfg" "${HOME}/.harness-mcp-servers.json" 2>/dev/null || true)
    if [[ -n "${merged}" ]]; then
        printf '%s\n' "${merged}" > "$cfg"
    fi
}

ensure_opencode_config() {
    local config_dir="${HOME}/.config/opencode"
    local config_file="${config_dir}/opencode.json"
    mkdir -p "$config_dir"

    local model_name="${OLLAMA_AGENT_MODEL:-harness}"
    local ollama_url="http://ollama:11434/v1"

    # Always (re)write the harness-managed provider/model/agent block —
    # OLLAMA_AGENT_MODEL may have changed between launches. We use a
    # heredoc (escaped $ for the schema literal) and overwrite unconditionally.
    cat > "$config_file" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "harness": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Harness",
      "options": {
        "baseURL": "${ollama_url}",
        "apiKey": "harness-dummy"
      },
      "models": {
        "${model_name}": {
          "name": "Harness Proxy"
        }
      }
    }
  },
  "model": "harness/${model_name}",
  "small_model": "harness/${model_name}",
  "agent": {
    "yolo": {
      "description": "Auto-approve all permissions; harness yolo mode",
      "permission": {
        "edit": "allow",
        "bash": {"*": "allow"},
        "webfetch": "allow"
      }
    }
  }
}
EOF
}

merge_opencode_mcp_servers() {
    # The harness script writes ~/.harness-mcp-servers.json in claude's shape.
    # Opencode expects an `mcp` top-level block with a different per-entry
    # shape:
    #   {"mcp": {"<name>": {"type": "remote", "url": "..."}}} for HTTP/SSE
    #   {"mcp": {"<name>": {"type": "local", "command": [...]}}} for stdio
    # Translate inline so the host harness script stays agent-agnostic.
    if [[ ! -f "${HOME}/.harness-mcp-servers.json" ]] || ! command -v jq >/dev/null 2>&1; then
        return 0
    fi
    local config_file="${HOME}/.config/opencode/opencode.json"
    [[ -f "$config_file" ]] || return 0

    local merged
    merged=$(jq -s '
        .[0] as $cfg
        | .[1] as $harness
        | ($harness.mcpServers // {}) as $servers
        | $servers
        | to_entries
        | map(
            .value as $v
            | if ($v.command // null) != null then
                  {key: .key, value: {type: "local", command: ([$v.command] + ($v.args // []))}}
              else
                  {key: .key, value: {type: "remote", url: ($v.url // "")}}
              end
          )
        | from_entries
        | . as $opencode_mcp
        | $cfg | .mcp = ((.mcp // {}) + $opencode_mcp)
    ' "$config_file" "${HOME}/.harness-mcp-servers.json" 2>/dev/null || true)
    if [[ -n "${merged}" ]]; then
        printf '%s\n' "${merged}" > "$config_file"
    fi
}

# --- mode: claude ----------------------------------------------------------

run_claude() {
    if [[ -z "${ANTHROPIC_BASE_URL:-}" ]]; then
        echo "[harness-claude] ERROR: ANTHROPIC_BASE_URL is not set" >&2
        echo "[harness-claude]   This must point at the in-network ollama (e.g. http://ollama:11434)" >&2
        exit 1
    fi

    if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
        echo "[harness-claude] WARN: no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set; using dummy"
        # AUTH_TOKEN (not API_KEY): claude-code's startup connectivity probe
        # to api.anthropic.com is gated to skip when AUTH_TOKEN is set; with
        # only API_KEY it hard-exits behind our default firewall.
        export ANTHROPIC_AUTH_TOKEN="harness-dummy"
    fi

    # Suppress claude-code's auto-update phone-home.
    export DISABLE_AUTOUPDATER=1

    ensure_claude_config
    merge_claude_mcp_servers

    echo "============================================================"
    echo " harness-agent (claude)"
    echo "   model:    ${OLLAMA_AGENT_MODEL:-<unset>}"
    echo "   base_url: ${ANTHROPIC_BASE_URL}"
    echo "   yolo:     ${HARNESS_YOLO:-0}"
    echo "   print:    ${HARNESS_PRINT_MODE:-0}"
    echo "============================================================"

    local args=()
    if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
        args+=(--dangerously-skip-permissions)
    fi
    args+=("$@")

    # Always foreground exec — no tmux. The container's PID 1 becomes claude
    # itself, so the user's terminal connects directly to its PTY and the
    # container exits when claude exits.
    exec claude "${args[@]}"
}

# --- mode: opencode --------------------------------------------------------

run_opencode() {
    ensure_opencode_config
    merge_opencode_mcp_servers

    export OPENCODE_DISABLE_AUTOUPDATE=1

    echo "============================================================"
    echo " harness-agent (opencode)"
    echo "   model:   harness/${OLLAMA_AGENT_MODEL:-harness}"
    echo "   ollama:  http://ollama:11434/v1"
    echo "   yolo:    ${HARNESS_YOLO:-0}"
    echo "   print:   ${HARNESS_PRINT_MODE:-0}"
    echo "============================================================"

    local args=()
    if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
        args+=(--agent yolo)
    fi

    if [[ "${HARNESS_PRINT_MODE:-0}" == "1" ]]; then
        # opencode has no `-p` flag — strip a leading -p / --print if the
        # harness forwarded it, hand the rest to `opencode run`.
        local op_args=()
        local seen_p=0
        local arg
        for arg in "$@"; do
            if [[ "$seen_p" == "0" && ("$arg" == "-p" || "$arg" == "--print") ]]; then
                seen_p=1
                continue
            fi
            op_args+=("$arg")
        done
        exec opencode run "${args[@]}" "${op_args[@]}"
    fi

    # Always foreground exec — no tmux. See run_claude for rationale.
    exec opencode "${args[@]}" "$@"
}

# --- mode: shell -----------------------------------------------------------

run_shell() {
    echo "============================================================"
    echo " harness-agent (shell)"
    echo "   workspace: $(pwd)"
    echo "   home:      ${HOME}"
    echo "============================================================"
    echo
    echo "Drop into an interactive bash inside the agent container. Exit"
    echo "with 'exit' or Ctrl+D. The home directory is shared across all"
    echo "agent modes, so installs (pipx, etc.) persist."
    echo
    exec bash -l
}

# --- dispatch --------------------------------------------------------------

case "$mode" in
    claude)
        run_claude "$@"
        ;;
    opencode)
        run_opencode "$@"
        ;;
    shell)
        run_shell
        ;;
    *)
        echo "[agent-entrypoint] unknown mode: $mode" >&2
        echo "[agent-entrypoint] valid modes: claude, opencode, shell" >&2
        exit 1
        ;;
esac
