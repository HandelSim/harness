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

DEFAULT_MODEL_NAME="${DEFAULT_MODEL_NAME:-}"
CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-200000}"
PROXY_PORT="${PROXY_PORT:-8000}"
REMOTE_URL="http://proxy:${PROXY_PORT}"
MODELS_URL="http://proxy:${PROXY_PORT}/v1/models"
OLLAMA_API="http://127.0.0.1:11434"

echo "============================================================"
echo " harness ollama entrypoint"
echo "   default model:  ${DEFAULT_MODEL_NAME:-<unset>}"
echo "   context length: ${CONTEXT_LENGTH}"
echo "   forward target: ${REMOTE_URL}"
echo "   models source:  ${MODELS_URL}"
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

# Discover which models the upstream advertises and register a stub for each,
# so the user can switch between them from opencode. The proxy's /v1/models
# route forwards GET {upstream}/v1/models with the bearer key and surfaces the
# upstream body verbatim — so a locked-key 401 (with its unlock_url) reaches us
# here too, reusing the same unlock flow the chat path uses.
#
# Failure handling:
#   - Locked key (401-ish body carrying an unlock_url): print the URL and abort.
#     The stack genuinely can't work until the user unlocks.
#   - Anything else (proxy/upstream unreachable, 5xx, empty list): warn loudly
#     and fall back to registering just DEFAULT_MODEL_NAME, so a transient
#     upstream hiccup doesn't brick the whole stack in a restart loop.
discovered_models=()
echo "[entrypoint] discovering upstream models via ${MODELS_URL}"
models_resp=$(curl -sS -w $'\n__HTTP_STATUS__%{http_code}' --max-time 30 "${MODELS_URL}" 2>&1) || true
models_status=$(printf '%s' "${models_resp}" | grep -o '__HTTP_STATUS__[0-9]*$' | sed 's/__HTTP_STATUS__//')
models_body=$(printf '%s' "${models_resp}" | sed 's/__HTTP_STATUS__[0-9]*$//')

if [[ -n "${models_status}" && "${models_status:0:1}" == "2" ]]; then
    mapfile -t discovered_models < <(printf '%s' "${models_body}" | jq -r '.data[].id // empty' 2>/dev/null || true)
    if (( ${#discovered_models[@]} > 0 )); then
        echo "[entrypoint] upstream advertises ${#discovered_models[@]} model(s): ${discovered_models[*]}"
    else
        echo "[entrypoint] WARN: /v1/models returned no ids; falling back to default model" >&2
    fi
else
    unlock_url=$(printf '%s' "${models_body}" | jq -r '.error.unlock_url // .error.metadata.unlock_url // .unlock_url // empty' 2>/dev/null || true)
    if [[ -n "${unlock_url}" ]]; then
        echo "[entrypoint] ERROR: upstream API key is locked (status ${models_status:-none})." >&2
        echo "[entrypoint]   Visit this URL in a signed-in browser to unlock it, then 'harness restart':" >&2
        echo "[entrypoint]   ${unlock_url}" >&2
        exit 1
    fi
    echo "[entrypoint] WARN: could not fetch models from proxy (status ${models_status:-none}); falling back to default model" >&2
    printf '%s\n' "${models_body}" | sed 's/^/[entrypoint]   /' >&2
fi

# Always ensure the default model is registered so opencode's default
# selection (harness/${DEFAULT_MODEL_NAME}) always resolves, even if the
# upstream didn't list it. Build a deduped registration set.
register_models=()
add_model() {
    local m="$1" existing
    [[ -z "$m" ]] && return 0
    for existing in "${register_models[@]}"; do
        [[ "$existing" == "$m" ]] && return 0
    done
    register_models+=("$m")
}
for m in "${discovered_models[@]}"; do add_model "$m"; done
add_model "${DEFAULT_MODEL_NAME}"

if (( ${#register_models[@]} == 0 )); then
    echo "[entrypoint] ERROR: no models to register (DEFAULT_MODEL_NAME unset and discovery empty)" >&2
    exit 1
fi

echo "[entrypoint] registering ${#register_models[@]} stub model(s): ${register_models[*]}"
for m in "${register_models[@]}"; do
    register_stub_model "${m}" || exit 1
done

# Sanity: confirm the registered stubs are visible via /api/tags.
TAGS_RESPONSE=$(curl -fsS "${OLLAMA_API}/api/tags")
for m in "${register_models[@]}"; do
    if ! echo "${TAGS_RESPONSE}" | grep -q "\"${m}"; then
        echo "[entrypoint] ERROR: stub model '${m}' not found in /api/tags" >&2
        echo "[entrypoint] /api/tags response: ${TAGS_RESPONSE}" >&2
        exit 1
    fi
done

echo "[entrypoint] harness ollama ready; ${#register_models[@]} stub model(s) -> ${REMOTE_URL}"

# Block on ollama. The trap above tears it down on signals.
wait "${OLLAMA_PID}"
