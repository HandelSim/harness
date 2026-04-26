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

MODEL_NAME="${OLLAMA_AGENT_MODEL:-harness}"
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

# Build the JSON body for /api/create. Use printf %s with explicit quoting on
# string fields and explicit numeric formatting on numbers, so we never need
# to depend on jq being present.
CREATE_BODY=$(printf '{"model":"%s","from":"%s","remote_host":"%s","info":{"context_length":%d},"parameters":{"num_ctx":%d}}' \
    "${MODEL_NAME}" \
    "${MODEL_NAME}" \
    "${REMOTE_URL}" \
    "${CONTEXT_LENGTH}" \
    "${CONTEXT_LENGTH}")

echo "[entrypoint] registering stub model"
echo "[entrypoint] POST ${OLLAMA_API}/api/create"
echo "[entrypoint] body: ${CREATE_BODY}"

# /api/create streams NDJSON progress events. Capture the full body so we can
# inspect the final line for success or surface error detail on failure.
CREATE_RESPONSE=$(curl -fsS -X POST \
    -H "Content-Type: application/json" \
    --data "${CREATE_BODY}" \
    "${OLLAMA_API}/api/create" || true)

if [[ -z "${CREATE_RESPONSE}" ]]; then
    echo "[entrypoint] ERROR: empty response from /api/create" >&2
    exit 1
fi

echo "[entrypoint] /api/create response:"
echo "${CREATE_RESPONSE}"

# Final NDJSON line should contain "status":"success".
FINAL_LINE=$(echo "${CREATE_RESPONSE}" | tail -n 1)
if ! echo "${FINAL_LINE}" | grep -q '"status":"success"'; then
    echo "[entrypoint] ERROR: /api/create did not end with status:success" >&2
    echo "[entrypoint] final line was: ${FINAL_LINE}" >&2
    exit 1
fi

# Sanity: confirm the stub is visible via /api/tags.
TAGS_RESPONSE=$(curl -fsS "${OLLAMA_API}/api/tags")
if ! echo "${TAGS_RESPONSE}" | grep -q "\"${MODEL_NAME}"; then
    echo "[entrypoint] ERROR: stub model '${MODEL_NAME}' not found in /api/tags" >&2
    echo "[entrypoint] /api/tags response: ${TAGS_RESPONSE}" >&2
    exit 1
fi

echo "[entrypoint] harness ollama ready, stub model registered: ${MODEL_NAME} -> ${REMOTE_URL}"

# Block on ollama. The trap above tears it down on signals.
wait "${OLLAMA_PID}"
