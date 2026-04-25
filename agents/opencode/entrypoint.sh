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

MODEL_NAME="${OLLAMA_AGENT_MODEL:-harness}"
OLLAMA_URL="http://ollama:11434/v1"

# --- banner -----------------------------------------------------------------

echo "============================================================"
echo " harness-opencode-agent"
echo "   model:   harness/${MODEL_NAME}"
echo "   ollama:  ${OLLAMA_URL}"
echo "   yolo:    ${HARNESS_YOLO:-0}"
echo "   test:    ${HARNESS_TEST_MODE:-0}"
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

# Best-effort: silence opencode's update check if it honors this var.
export OPENCODE_DISABLE_AUTOUPDATE=1

# --- argv assembly ----------------------------------------------------------

opencode_args=()
if [[ "${HARNESS_YOLO:-0}" == "1" ]]; then
    opencode_args+=(--agent yolo)
fi

# --- test mode: non-interactive `opencode run` ------------------------------

if [[ "${HARNESS_TEST_MODE:-0}" == "1" ]]; then
    exec opencode run "${opencode_args[@]}" "$@"
fi

# --- normal mode: wrap interactive opencode in a detached tmux session ------

inner=$(printf 'opencode'; for a in "${opencode_args[@]}" "$@"; do printf ' %q' "$a"; done)

tmux new-session -d -s harness-agent \
    "${inner}; ec=\$?; echo; echo '[harness] agent exited (code '\$ec')'; sleep 30; exit \$ec"

while tmux has-session -t harness-agent 2>/dev/null; do
    sleep 5
done
