#!/usr/bin/env bash
#
# harness ollama entrypoint:
#   1. Start `ollama serve` in the background.
#   2. Wait for it to accept HTTP.
#   3. POST /api/create to register a stub model whose RemoteHost points at
#      the proxy service. This makes ollama forward chat requests for that
#      model to http://proxy:${PROXY_PORT}.
#   4. Block on the ollama process so PID 1 stays alive.

set -euo pipefail

# Bring up the egress firewall before doing anything network-touching.
# `ollama serve` and the /api/create + /api/tags probes that follow all
# go out via the rules this lays down. Runs as root (the ollama image's
# default user).
if [[ -x /usr/local/bin/init-firewall.sh ]]; then
    /usr/local/bin/init-firewall.sh
else
    echo "[entrypoint] WARN: init-firewall.sh missing; running without firewall" >&2
fi

MODEL_NAME="${OLLAMA_AGENT_MODEL:-GenAI}"
CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-200000}"
PROXY_PORT="${PROXY_PORT:-8000}"
REMOTE_URL="http://proxy:${PROXY_PORT}"
OLLAMA_API="http://127.0.0.1:11434"

echo "============================================================"
echo " harness ollama entrypoint"
echo "   stub model:     ${MODEL_NAME}"
echo "   context length: ${CONTEXT_LENGTH}"
echo "   forward target: ${REMOTE_URL}"
echo "   OLLAMA_REMOTES: ${OLLAMA_REMOTES:-<unset>}"
echo "============================================================"

# Start ollama serve in the background.
ollama serve &
OLLAMA_PID=$!

# Make sure we don't leave a stranded ollama process if the script exits.
trap 'echo "[entrypoint] shutting down ollama (pid ${OLLAMA_PID})"; kill "${OLLAMA_PID}" 2>/dev/null || true; wait "${OLLAMA_PID}" 2>/dev/null || true' EXIT INT TERM

# Wait for the API to come up. 60 attempts × 1s.
echo "[entrypoint] waiting for ollama API at ${OLLAMA_API}/api/tags"
for attempt in $(seq 1 60); do
    if curl -fsS -o /dev/null "${OLLAMA_API}/api/tags"; then
        echo "[entrypoint] ollama API is up (after ${attempt}s)"
        break
    fi
    if [[ "${attempt}" -eq 60 ]]; then
        echo "[entrypoint] ERROR: ollama API never came up after 60s" >&2
        exit 1
    fi
    sleep 1
done

# Register the stub model with the proxy's RemoteHost.
#
# Args: <model_name>
# Returns: 0 on success, non-zero if /api/create didn't end with
# status:success or the model isn't visible in /api/tags afterwards.
register_stub_model() {
    local name="$1"
    local body
    body=$(printf '{"model":"%s","from":"%s","remote_host":"%s","info":{"context_length":%d},"parameters":{"num_ctx":%d}}' \
        "${name}" \
        "${name}" \
        "${REMOTE_URL}" \
        "${CONTEXT_LENGTH}" \
        "${CONTEXT_LENGTH}")

    echo "[entrypoint] registering stub model '${name}'"

    local response
    response=$(curl -fsS -X POST \
        -H "Content-Type: application/json" \
        --data "${body}" \
        "${OLLAMA_API}/api/create" || true)

    if [[ -z "${response}" ]]; then
        echo "[entrypoint] ERROR: empty response from /api/create for '${name}'" >&2
        return 1
    fi

    local final_line
    final_line=$(echo "${response}" | tail -n 1)
    if ! echo "${final_line}" | grep -q '"status":"success"'; then
        echo "[entrypoint] ERROR: /api/create for '${name}' did not end with status:success" >&2
        echo "[entrypoint] final line was: ${final_line}" >&2
        return 1
    fi
    return 0
}

# Register the canonical model and abort the entrypoint on failure.
register_stub_model "${MODEL_NAME}" || exit 1

# Sanity: confirm the stub model is visible via /api/tags.
TAGS_RESPONSE=$(curl -fsS "${OLLAMA_API}/api/tags")
if ! echo "${TAGS_RESPONSE}" | grep -q "\"${MODEL_NAME}"; then
    echo "[entrypoint] ERROR: stub model '${MODEL_NAME}' not found in /api/tags" >&2
    echo "[entrypoint] /api/tags response: ${TAGS_RESPONSE}" >&2
    exit 1
fi

echo "[entrypoint] harness ollama ready; stub model -> ${REMOTE_URL}"

# Block on ollama. The trap above tears it down on signals.
wait "${OLLAMA_PID}"
