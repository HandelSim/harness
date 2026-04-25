#!/usr/bin/env bash
#
# harness-opencode-agent entrypoint.
#
# Generates ~/.config/opencode/opencode.json at startup so the model name
# (driven by OLLAMA_AGENT_MODEL) doesn't have to be baked into the image.
# Defines a custom openai-compatible provider named `harness` pointed at
# the in-network ollama, and registers OLLAMA_AGENT_MODEL as the only model
# under it. Selects that model for both `model` and `small_model`.
#
# Yolo mode: opencode doesn't expose a global skip-permissions flag the way
# claude-code does. The closest documented mechanism is to define an agent
# with permissive `permission` settings and select it via --agent. We define
# a `yolo` agent here and the entrypoint passes --agent yolo when
# HARNESS_YOLO=1. The permission schema below matches opencode 1.14.x; if
# upstream changes the schema and yolo stops working, the user will see
# permission prompts inside the TUI and can edit opencode.json by hand.

set -euo pipefail

# --- UID remap --------------------------------------------------------------
#
# See claude-agent entrypoint.sh for the full rationale. Summary: when
# Phase 4's `harness` script starts the container with --user 0:0, this
# block remaps harness's uid/gid to match the host caller and re-execs as
# harness via gosu. Without --user (test mode), id -u is non-zero and this
# block is skipped.
if [[ "$(id -u)" == "0" && -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
    current_uid=$(id -u harness 2>/dev/null || echo "")
    current_gid=$(id -g harness 2>/dev/null || echo "")
    if [[ "${current_uid}" != "${HOST_UID}" || "${current_gid}" != "${HOST_GID}" ]]; then
        groupmod -g "${HOST_GID}" -o harness 2>/dev/null \
            || groupadd -g "${HOST_GID}" -o harness
        usermod -u "${HOST_UID}" -g "${HOST_GID}" -o harness
        chown -R "${HOST_UID}:${HOST_GID}" /home/harness 2>/dev/null || true
    fi
    exec gosu harness "$0" "$@"
fi

# --- skel seed --------------------------------------------------------------
#
# See claude entrypoint.sh for the rationale. Summary: with the persistent
# home bind mount, /home/harness/ is shadowed; this restores the build-time
# skeleton once on first run, marked by ~/.harness-home-initialized.
if [[ ! -f "${HOME}/.harness-home-initialized" ]]; then
    if [[ -d /etc/skel/harness ]]; then
        cp -an /etc/skel/harness/. "${HOME}/" 2>/dev/null || true
    fi
    touch "${HOME}/.harness-home-initialized" 2>/dev/null || true
fi

MODEL_NAME="${OLLAMA_AGENT_MODEL:-harness}"
OLLAMA_URL="http://ollama:11434/v1"

# --- banner -----------------------------------------------------------------

echo "============================================================"
echo " harness-opencode-agent"
echo "   model:   harness/${MODEL_NAME}"
echo "   ollama:  ${OLLAMA_URL}"
echo "   yolo:    ${HARNESS_YOLO:-0}"
echo "   test:    ${HARNESS_TEST_MODE:-0}"
echo "   print:   ${HARNESS_PRINT_MODE:-0}"
echo "============================================================"

# --- generate opencode.json -------------------------------------------------

CONFIG_DIR="${HOME}/.config/opencode"
CONFIG_FILE="${CONFIG_DIR}/opencode.json"
mkdir -p "${CONFIG_DIR}"

# Heredoc with single-quoted EOF token would inhibit substitution; we want
# substitution for ${MODEL_NAME} and ${OLLAMA_URL}, so we leave EOF unquoted
# and escape the literal $ and \ characters.
cat > "${CONFIG_FILE}" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "harness": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Harness",
      "options": {
        "baseURL": "${OLLAMA_URL}",
        "apiKey": "harness-dummy"
      },
      "models": {
        "${MODEL_NAME}": {
          "name": "Harness Proxy"
        }
      }
    }
  },
  "model": "harness/${MODEL_NAME}",
  "small_model": "harness/${MODEL_NAME}",
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

# --- MCP server config merge ------------------------------------------------
#
# The harness script writes ~/.harness-mcp-servers.json in claude's shape:
#   {"mcpServers": {"<name>": {"type": "sse", "url": "..."}}}
# Opencode expects an `mcp` top-level block with a different per-entry shape:
#   {"mcp": {"<name>": {"type": "remote", "url": "..."}}} for HTTP/SSE
#   {"mcp": {"<name>": {"type": "local", "command": [...]}}} for stdio
# Translate each registry entry inline so the host harness script can stay
# agent-agnostic. If jq is missing or the file is malformed, we just skip
# (the agent still starts; the user will see an empty MCP list).
if [[ -f "${HOME}/.harness-mcp-servers.json" ]] && command -v jq >/dev/null 2>&1; then
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
    ' "${CONFIG_FILE}" "${HOME}/.harness-mcp-servers.json" 2>/dev/null || true)
    if [[ -n "${merged}" ]]; then
        printf '%s\n' "${merged}" > "${CONFIG_FILE}"
    fi
fi

# Best-effort: silence opencode's update check if it honors this var.
export OPENCODE_DISABLE_AUTOUPDATE=1

# --- argv assembly ----------------------------------------------------------

opencode_args=()
if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
    opencode_args+=(--agent yolo)
fi

# --- test/print mode: non-interactive `opencode run` -----------------------
#
# Both HARNESS_TEST_MODE and HARNESS_PRINT_MODE bypass tmux. Test mode is set
# by scripts/agent_test.sh; print mode is set by `harness claude/opencode -p`.
#
# Opencode does not expose a `-p` flag — the harness script forwards `-p` /
# `--print` verbatim alongside the prompt, and we strip it here before
# handing the rest to `opencode run`.

if [[ "${HARNESS_TEST_MODE:-0}" == "1" || "${HARNESS_PRINT_MODE:-0}" == "1" ]]; then
    forwarded_args=()
    seen_p=0
    for arg in "$@"; do
        if [[ "$seen_p" == "0" && ("$arg" == "-p" || "$arg" == "--print") ]]; then
            seen_p=1
            continue
        fi
        forwarded_args+=("$arg")
    done
    exec opencode run "${opencode_args[@]}" "${forwarded_args[@]}"
fi

# --- normal mode: wrap interactive opencode in a detached tmux session ------

inner=$(printf 'opencode'; for a in "${opencode_args[@]}" "$@"; do printf ' %q' "$a"; done)

tmux new-session -d -s harness-agent \
    "${inner}; ec=\$?; echo; echo '[harness] agent exited (code '\$ec')'; sleep 30; exit \$ec"

while tmux has-session -t harness-agent 2>/dev/null; do
    sleep 5
done
