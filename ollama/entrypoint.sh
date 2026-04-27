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

# Register one stub model with the proxy's RemoteHost. Used both for the
# canonical OLLAMA_AGENT_MODEL and for the alias names below.
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

# Canonical model: register and abort the entrypoint on failure.
register_stub_model "${MODEL_NAME}" || exit 1

# Sanity: confirm the canonical stub is visible via /api/tags. The alias
# registrations below are best-effort; only the canonical name is critical
# for ollama startup.
TAGS_RESPONSE=$(curl -fsS "${OLLAMA_API}/api/tags")
if ! echo "${TAGS_RESPONSE}" | grep -q "\"${MODEL_NAME}"; then
    echo "[entrypoint] ERROR: stub model '${MODEL_NAME}' not found in /api/tags" >&2
    echo "[entrypoint] /api/tags response: ${TAGS_RESPONSE}" >&2
    exit 1
fi

# Register additional stub aliases for the names claude-code uses internally
# in sub-agent invocations (Task tool, Explore agent, etc.). All point at the
# same RemoteHost as the canonical model — they're aliases that satisfy
# claude-code's model lookups. The proxy ignores the model name in the
# request and uses PROXY_API_MODEL from .env to decide what to send
# upstream, so all aliases functionally route to the same upstream.
#
# Best-effort: a registration failure on one alias logs and continues; we
# don't fail ollama startup over partial coverage.
STUB_ALIASES=(
    sonnet
    opus
    haiku
    claude-sonnet-4-5
    claude-opus-4-5
    claude-haiku-4-5
    claude-3-5-sonnet-20241022
    claude-3-5-haiku-20241022
    claude-3-opus-20240229
)

for alias_name in "${STUB_ALIASES[@]}"; do
    # Skip the canonical name to avoid a duplicate-registration round-trip.
    if [[ "${alias_name}" == "${MODEL_NAME}" ]]; then
        continue
    fi
    if ! register_stub_model "${alias_name}"; then
        echo "[entrypoint] WARN: failed to register stub alias '${alias_name}'; continuing" >&2
    fi
done

echo "[entrypoint] harness ollama ready; stub models -> ${REMOTE_URL}"

# Block on ollama. The trap above tears it down on signals.
wait "${OLLAMA_PID}"
