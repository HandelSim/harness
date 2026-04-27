# scripts/lib/test_helpers.sh — sourceable bash toolkit for harness test
# scripts. Extracted from setup boilerplate that used to be duplicated
# across derisk_test.sh, proxy_test.sh, agent_test.sh, full_pipeline_test.sh.
#
# Source from a test:
#
#   source "$REPO_ROOT/scripts/lib/test_helpers.sh"
#   require_docker
#   ENV_FILE=$(mktemp -t harness-foo.XXXXXX.env)
#   test_generate_env "$ENV_FILE"
#   OVERRIDE=$(mktemp -t harness-foo.XXXXXX.yml)
#   test_generate_mockupstream_override "$OVERRIDE"
#   COMPOSE=(docker compose --project-name foo --env-file "$ENV_FILE" \
#       -f docker-compose.yml -f "$OVERRIDE")
#   "${COMPOSE[@]}" up -d --build
#   test_wait_for_healthy foo mockupstream 90
#
# All log output goes to stderr so callers can capture function stdout.

# REPO_ROOT must be set by the caller (every test script already computes it).

# Pull in cross-platform helpers (harness_docker, harness_docker_path,
# harness_detect_os) so individual test scripts don't have to source it
# themselves. test_helpers.sh is the universal entry-point for tests.
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# `harness <agent>` will prompt the user before launching when the firewall
# is open. Tests bypass that prompt unconditionally — they don't have a TTY
# and they're not running real agents anyway.
export HARNESS_NET_CONFIRM=1

# --- preflight --------------------------------------------------------------

# Verify docker daemon is reachable. Exits 1 with a clear message if not.
require_docker() {
    if ! docker info >/dev/null 2>&1; then
        echo "[test-helpers] ERROR: docker daemon is not reachable" >&2
        exit 1
    fi
}

# --- output helpers ---------------------------------------------------------

# Print a colored section header. Useful for visually separating tests.
# Args: <test_name>
test_section() {
    local name="$1"
    printf '\n\033[1;36m============================================================\033[0m\n' >&2
    printf '\033[1;36m %s\033[0m\n' "$name" >&2
    printf '\033[1;36m============================================================\033[0m\n' >&2
}

# --- env / override generation ---------------------------------------------

# Write a baseline .env file suitable for the proxy/ollama/mockupstream
# integration stack. Keys after the second positional are written verbatim
# (KEY=VAL form) and override the defaults for collisions.
#
# Args: <output_path> [extra_kv ...]
# Example:
#   test_generate_env /tmp/my.env "MOCK_SCENARIO=tool" "PUBLISH_OLLAMA_PORT=11434"
test_generate_env() {
    local out="$1"; shift
    cat >"$out" <<'EOF'
PROXY_API_URL=http://mockupstream:9000/v1/chat/completions
PROXY_API_KEY=test-key-1234
PROXY_API_MODEL=test-model
PROXY_HOST=0.0.0.0
PROXY_PORT=8000
OUTPUT_DIR=
PROXY_TIMEOUT=30
OLLAMA_VERSION=0.21.2
OLLAMA_AGENT_MODEL=harness
OLLAMA_CONTEXT_LENGTH=200000
MOCK_SCENARIO=text
PUBLISH_OLLAMA_PORT=
EOF
    # Append/override caller-supplied keys. We don't dedupe — later wins
    # because docker compose / set -a sourcing both honor the last assignment.
    local kv
    for kv in "$@"; do
        printf '%s\n' "$kv" >>"$out"
    done
}

# Write a docker-compose override file that adds a `mockupstream` service
# fronting scripts/mock_upstream.py. Mounts the python file AND the fixture
# directory so the fixture-dispatch path (Phase 7a) sees the same fixtures
# from any test. MOCK_FIXTURES_DIR is set so mock_upstream.py finds them.
#
# Args: <output_path>
# Requires: REPO_ROOT in scope (so we can refer to the host paths). The
# generated yml uses ./scripts/... relative paths because docker compose
# resolves -f-included files relative to the project working directory,
# which every harness test sets to REPO_ROOT before invoking compose.
test_generate_mockupstream_override() {
    local out="$1"
    cat >"$out" <<'EOF'
services:
  mockupstream:
    image: python:3.12-slim
    working_dir: /app
    environment:
      MOCK_SCENARIO: ${MOCK_SCENARIO:-text}
      MOCK_FIXTURES_DIR: /fixtures
    volumes:
      - ./scripts/mock_upstream.py:/app/mock_upstream.py:ro
      - ./scripts/fixtures/responses:/fixtures:ro
    networks:
      - harness-net
    expose:
      - "9000"
    command: >
      sh -c "pip install --quiet --no-cache-dir flask==3.0.3 &&
             python /app/mock_upstream.py"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys;\nu=urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=2);\nsys.exit(0 if u.status==200 else 1)"]
      interval: 5s
      timeout: 3s
      retries: 12
      start_period: 20s
EOF
}

# --- firewall allowlist for tests -------------------------------------------

# Write a permissive-but-not-open .harness-allowlist for test usage. Includes
# the canonical positive-case probes used by init-firewall.sh's verify step
# (api.github.com et al.) plus any extra hosts the caller passes. The intra-
# cluster service names (proxy, ollama, mockupstream) don't need to be on
# the allowlist — they're reached via the host-network bypass rule.
#
# Args: <output_path> [extra_host ...]
test_generate_allowlist() {
    local out="$1"; shift
    cat >"$out" <<'EOF'
# auto-generated by test_generate_allowlist
github.com
api.github.com
codeload.github.com
raw.githubusercontent.com
objects.githubusercontent.com
pypi.org
files.pythonhosted.org
registry.npmjs.org
EOF
    local h
    for h in "$@"; do
        printf '%s\n' "$h" >>"$out"
    done
}

# --- standalone mockupstream sidecar (integration_test.sh) ------------------

# Start a mockupstream sidecar attached to an existing harness compose
# network. Used by scripts/integration_test.sh, which brings the harness
# stack up via the real `harness start` (so docker-compose owns the network)
# and then needs to splice an in-network mock for the proxy to forward to.
#
# This mirrors test_generate_mockupstream_override but as a one-shot
# `docker run` because no compose project owns the mock here. The container
# joins the network with the alias `mockupstream` so PROXY_API_URL=
# http://mockupstream:9000/... resolves the same way it would in
# full_pipeline_test.sh.
#
# Args: <project_name>
# Requires: REPO_ROOT in scope, plus the network
# `<project_name>_harness-net` already created by `harness start`.
test_start_mockupstream() {
    local project="$1"
    local network="${project}_harness-net"
    local cname="${project}-mockupstream-1"
    local fixtures_dir="$REPO_ROOT/scripts/fixtures/responses"

    # Defensive: drop a stale container with the same name so the run -d
    # below doesn't fail with "container name already in use".
    docker rm -f "$cname" >/dev/null 2>&1 || true

    local mock_py_host fixtures_host
    mock_py_host=$(harness_docker_path "$REPO_ROOT/scripts/mock_upstream.py")
    fixtures_host=$(harness_docker_path "$fixtures_dir")

    harness_docker run -d \
        --name "$cname" \
        --network "$network" \
        --network-alias mockupstream \
        -e "MOCK_FIXTURES_DIR=/fixtures" \
        -v "$mock_py_host:/app/mock_upstream.py:ro" \
        -v "$fixtures_host:/fixtures:ro" \
        --health-cmd "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=2).status==200 else 1)\"" \
        --health-interval 5s \
        --health-timeout 3s \
        --health-retries 12 \
        --health-start-period 20s \
        python:3.12-slim \
        sh -c "pip install --quiet --no-cache-dir flask==3.0.3 && python /app/mock_upstream.py" \
        >/dev/null
}

# Wait for a standalone container (non-compose) to report Health.Status ==
# healthy. Used together with test_start_mockupstream where compose ps
# wouldn't see the container.
#
# Args: <container_name> [<timeout=60>]
test_wait_for_container_healthy() {
    local cname="$1"
    local timeout_s="${2:-60}"
    local deadline=$(( $(date +%s) + timeout_s ))
    while (( $(date +%s) < deadline )); do
        local status
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cname" 2>/dev/null || echo "none")
        if [[ "$status" == "healthy" ]]; then
            return 0
        fi
        sleep 2
    done
    echo "[test-helpers] timeout waiting for container $cname to become healthy" >&2
    docker logs "$cname" 2>&1 | tail -30 >&2 || true
    return 1
}

# --- waiters ----------------------------------------------------------------

# Return 0 iff the named compose service has Health.Status == healthy.
# Args: <project> <service>
_test_is_healthy() {
    local project="$1" svc="$2"
    local cid
    cid=$(docker compose --project-name "$project" ps -q "$svc" 2>/dev/null || true)
    if [[ -z "$cid" ]]; then
        return 1
    fi
    local status
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo "none")
    [[ "$status" == "healthy" ]]
}

# Wait for one or more compose services to become healthy. Returns 0 on
# success, 1 on timeout. On timeout dumps `compose ps` to stderr.
#
# Args: <project> <service> [<service> ...] [<timeout=90>]
# The timeout is the LAST positional arg if it's purely digits, otherwise
# defaults to 90.
test_wait_for_healthy() {
    local project="$1"; shift
    local timeout_s=90
    local services=()
    local arg
    for arg in "$@"; do
        services+=("$arg")
    done
    # If the last token is an integer, treat it as the timeout.
    if (( ${#services[@]} > 1 )) && [[ "${services[-1]}" =~ ^[0-9]+$ ]]; then
        timeout_s="${services[-1]}"
        unset 'services[-1]'
    fi
    if (( ${#services[@]} == 0 )); then
        echo "[test-helpers] test_wait_for_healthy: no services given" >&2
        return 1
    fi

    local deadline=$(( $(date +%s) + timeout_s ))
    while true; do
        local all_ok=1 svc
        for svc in "${services[@]}"; do
            if ! _test_is_healthy "$project" "$svc"; then
                all_ok=0
                break
            fi
        done
        if (( all_ok )); then
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            echo "[test-helpers] timeout waiting for healthy: ${services[*]}" >&2
            docker compose --project-name "$project" ps >&2 || true
            return 1
        fi
        sleep 2
    done
}

# --- cleanup ----------------------------------------------------------------

# Tear down a test stack: docker compose down -v --remove-orphans plus rm
# of any temp files supplied. Idempotent and never aborts the caller.
#
# Args: <project> <env_file> <override_file> [<more_files> ...]
test_cleanup() {
    local project="$1" env_file="${2:-}" override="${3:-}"
    shift 3 || true
    if [[ -n "$env_file" && -f "$env_file" && -n "$override" && -f "$override" ]]; then
        docker compose --project-name "$project" \
            --env-file "$env_file" \
            -f docker-compose.yml -f "$override" \
            down -v --remove-orphans >/dev/null 2>&1 || true
    else
        docker compose --project-name "$project" \
            -f docker-compose.yml \
            down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    [[ -n "$env_file"  ]] && rm -f "$env_file"  2>/dev/null || true
    [[ -n "$override"  ]] && rm -f "$override"  2>/dev/null || true
    local f
    for f in "$@"; do
        [[ -n "$f" ]] && rm -f "$f" 2>/dev/null || true
    done
}
