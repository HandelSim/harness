#!/usr/bin/env bash
#
# scripts/firewall_test.sh — focused checks that the per-container egress
# firewall (firewall/init-firewall.sh) is actually applied at runtime.
#
# Two phases:
#
#   Phase 1 — positive. Brings up proxy + ollama + mockupstream with a
#   normal allowlist, waits for healthy, then exec's into each container
#   to confirm:
#     * the allowlist is bind-mounted at /etc/harness/allowlist,
#     * iptables default policy on OUTPUT is DROP,
#     * the allowed-domains ipset exists and is populated,
#     * an out-of-allowlist host (example.com) is unreachable.
#
#   Phase 2 — negative. Brings up proxy with PROXY_API_URL pointing at a
#   host that is NOT in the allowlist; asserts the proxy refuses to start
#   and the FATAL guardrail line appears in its logs.
#
# Both phases use `docker compose down --remove-orphans` on cleanup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${REPO_ROOT}/scripts/lib/test_helpers.sh"

require_docker

# ============================================================================
# Phase B2 sanity: api.anthropic.com must NOT be a baked-in default in the
# example allowlist or the test_helpers fixture. The B1 workaround was
# explicitly reversed in B2 — the cosmetic "Unable to connect to Anthropic
# services" warning is preferred over routing any traffic to Anthropic.
# ============================================================================

test_section "B2 sanity: no api.anthropic.com in defaults"
example_allowlist="${REPO_ROOT}/.harness-allowlist.example"
if [[ -f "$example_allowlist" ]]; then
    # Strip comment lines, then look for api.anthropic.com as an active entry.
    if awk '/^[[:space:]]*#/ {next} /^[[:space:]]*$/ {next} {print}' "$example_allowlist" \
            | grep -qE '^[[:space:]]*api\.anthropic\.com([[:space:]#]|$)'; then
        echo "[fw] FAIL: $example_allowlist still ships api.anthropic.com as a default entry" >&2
        echo "[fw]       (the B1 workaround was reversed in B2)" >&2
        exit 1
    fi
    echo "[fw] OK: $example_allowlist does not include api.anthropic.com by default"
else
    echo "[fw] FAIL: $example_allowlist missing" >&2
    exit 1
fi
# test_helpers' generated allowlist must also not include the host.
test_allow_check=$(mktemp -t harness-fw-anthropic-check.XXXXXX.allow)
test_generate_allowlist "$test_allow_check"
if grep -qE '^[[:space:]]*api\.anthropic\.com([[:space:]#]|$)' "$test_allow_check"; then
    echo "[fw] FAIL: test_generate_allowlist still emits api.anthropic.com" >&2
    rm -f "$test_allow_check"
    exit 1
fi
rm -f "$test_allow_check"
echo "[fw] OK: test_generate_allowlist does not emit api.anthropic.com"

# ============================================================================
# Phase 1 — positive firewall posture inside running containers.
# ============================================================================

PROJECT_POS="harness-firewall-pos"
ENV_POS="$(mktemp -t harness-fw-pos.XXXXXX.env)"
OVERRIDE_POS="$(mktemp -t harness-fw-pos.XXXXXX.yml)"
ALLOW_POS="$(mktemp -t harness-fw-pos.XXXXXX.allow)"

test_generate_env "$ENV_POS"
test_generate_mockupstream_override "$OVERRIDE_POS"
test_generate_allowlist "$ALLOW_POS"

export HARNESS_ALLOWLIST_PATH="$ALLOW_POS"

cleanup_pos() {
    test_cleanup "$PROJECT_POS" "$ENV_POS" "$OVERRIDE_POS" "$ALLOW_POS"
}
trap cleanup_pos EXIT INT TERM

# Defensive: clear stragglers.
docker compose --project-name "$PROJECT_POS" \
    -f docker-compose.yml -f "$OVERRIDE_POS" \
    down -v --remove-orphans >/dev/null 2>&1 || true

COMPOSE_POS=(docker compose --project-name "$PROJECT_POS" --env-file "$ENV_POS" \
    -f docker-compose.yml -f "$OVERRIDE_POS")

test_section "Phase 1: positive firewall posture"

echo "[fw] bringing up proxy + ollama + mockupstream"
"${COMPOSE_POS[@]}" up -d --build

echo "[fw] waiting for proxy + ollama + mockupstream to become healthy (up to 180s)"
if ! test_wait_for_healthy "$PROJECT_POS" mockupstream proxy ollama 180; then
    echo "[fw] services failed to become healthy" >&2
    "${COMPOSE_POS[@]}" logs proxy        >&2 || true
    "${COMPOSE_POS[@]}" logs ollama       >&2 || true
    "${COMPOSE_POS[@]}" logs mockupstream >&2 || true
    exit 1
fi

# Per-service assertions. The agent containers (claude-agent / opencode-agent)
# in compose.yml are stub services — they exit immediately because they have
# no `command` and no agent invocation — so we only assert against the long-
# running services here.
assert_firewall_in() {
    local svc="$1"
    local cid
    cid=$("${COMPOSE_POS[@]}" ps -q "$svc")
    if [[ -z "$cid" ]]; then
        echo "[fw] FAIL: no container id for $svc" >&2
        exit 1
    fi
    echo "[fw] checking $svc ($cid)"

    # Allowlist must be bind-mounted.
    if ! docker exec "$cid" test -f /etc/harness/allowlist; then
        echo "[fw] FAIL: $svc has no /etc/harness/allowlist" >&2
        exit 1
    fi

    # iptables default OUTPUT policy must be DROP.
    local pol
    pol=$(docker exec "$cid" iptables -S OUTPUT 2>/dev/null | head -n 1 || true)
    if ! grep -q '^-P OUTPUT DROP' <<<"$pol"; then
        echo "[fw] FAIL: $svc OUTPUT policy is not DROP (got: $pol)" >&2
        docker exec "$cid" iptables -S OUTPUT >&2 || true
        exit 1
    fi

    # ipset must exist and contain at least one entry (allowlist hosts resolved).
    local ipset_count
    ipset_count=$(docker exec "$cid" sh -c 'ipset list allowed-domains 2>/dev/null | awk "/^Number of entries:/ {print \$4}"' || true)
    if [[ -z "$ipset_count" ]]; then
        echo "[fw] FAIL: $svc has no 'allowed-domains' ipset" >&2
        docker exec "$cid" ipset list >&2 || true
        exit 1
    fi
    if (( ipset_count < 1 )); then
        echo "[fw] FAIL: $svc 'allowed-domains' ipset is empty" >&2
        exit 1
    fi
    echo "[fw]   $svc: ipset 'allowed-domains' has $ipset_count entries"

    # Negative: example.com must NOT be reachable from inside the container.
    # Use a 5s connect timeout so the failure is fast. We expect rc != 0.
    set +e
    docker exec "$cid" curl --connect-timeout 5 -s -o /dev/null https://example.com
    local curl_rc=$?
    set -e
    if (( curl_rc == 0 )); then
        echo "[fw] FAIL: $svc can reach example.com (firewall not applied?)" >&2
        exit 1
    fi
    echo "[fw]   $svc: example.com unreachable (rc=$curl_rc) — expected"
}

assert_firewall_in proxy
assert_firewall_in ollama

echo "[fw] Phase 1 OK"

# Tear down phase 1 explicitly so phase 2 starts from a clean slate. Trap
# remains armed in case Phase 2 setup fails.
cleanup_pos
trap - EXIT INT TERM

# ============================================================================
# Phase 2 — negative: PROXY_API_URL guardrail rejects out-of-allowlist host.
# ============================================================================

PROJECT_NEG="harness-firewall-neg"
ENV_NEG="$(mktemp -t harness-fw-neg.XXXXXX.env)"
OVERRIDE_NEG="$(mktemp -t harness-fw-neg.XXXXXX.yml)"
ALLOW_NEG="$(mktemp -t harness-fw-neg.XXXXXX.allow)"

# .env points PROXY_API_URL at a host whose label has a dot (so the guardrail
# enforces it) and is deliberately absent from the allowlist below.
test_generate_env "$ENV_NEG" \
    "PROXY_API_URL=https://blocked.example.com/v1/chat/completions"

# Reuse the same mockupstream override only because compose insists on
# resolvability of `harness-net`; the proxy will fail before we use it.
test_generate_mockupstream_override "$OVERRIDE_NEG"

# Allowlist deliberately omits blocked.example.com.
test_generate_allowlist "$ALLOW_NEG"

export HARNESS_ALLOWLIST_PATH="$ALLOW_NEG"

cleanup_neg() {
    test_cleanup "$PROJECT_NEG" "$ENV_NEG" "$OVERRIDE_NEG" "$ALLOW_NEG"
}
trap cleanup_neg EXIT INT TERM

docker compose --project-name "$PROJECT_NEG" \
    -f docker-compose.yml -f "$OVERRIDE_NEG" \
    down -v --remove-orphans >/dev/null 2>&1 || true

COMPOSE_NEG=(docker compose --project-name "$PROJECT_NEG" --env-file "$ENV_NEG" \
    -f docker-compose.yml -f "$OVERRIDE_NEG")

test_section "Phase 2: PROXY_API_URL guardrail (negative)"

echo "[fw] starting proxy alone with out-of-allowlist PROXY_API_URL"
# `up -d` returns success for the create step even if the container then
# exits — the assertion is on the proxy container's logs (looking for the
# FATAL guardrail line), not on `up`'s exit code. We don't poll for
# State.Status because the proxy service has restart: unless-stopped, so
# the container flaps between "exited" and "restarting" and may briefly
# look "running" again. Logs are persistent and definitive.
"${COMPOSE_NEG[@]}" up -d --build proxy >/dev/null 2>&1 || true

# Give the entrypoint enough time to run init-firewall.sh and emit its
# FATAL line at least once. ~15s covers cold start on most hosts.
deadline=$(( $(date +%s) + 30 ))
matched=0
while true; do
    logs=$("${COMPOSE_NEG[@]}" logs proxy 2>&1 || true)
    if grep -q 'PROXY_API_URL hostname.*not in' <<<"$logs" \
       && grep -q 'blocked\.example\.com' <<<"$logs"; then
        matched=1
        break
    fi
    if (( $(date +%s) >= deadline )); then
        break
    fi
    sleep 2
done

if (( ! matched )); then
    echo "[fw] FAIL: proxy logs did not show PROXY_API_URL guardrail FATAL line" >&2
    "${COMPOSE_NEG[@]}" logs proxy >&2 || true
    exit 1
fi

# Sanity: proxy must NOT be healthy. (It might be transiently "running"
# during a restart cycle, but its healthcheck cannot succeed because the
# python server never starts.)
proxy_cid=$("${COMPOSE_NEG[@]}" ps -q proxy 2>/dev/null || true)
if [[ -n "$proxy_cid" ]]; then
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$proxy_cid" 2>/dev/null || echo "none")
    if [[ "$health" == "healthy" ]]; then
        echo "[fw] FAIL: proxy reports healthy despite guardrail FATAL" >&2
        exit 1
    fi
fi

echo "[fw] Phase 2 OK (proxy guardrail FATAL observed in logs)"

# Tear down phase 2 explicitly so phase 3 starts from a clean slate.
cleanup_neg
trap - EXIT INT TERM

# ============================================================================
# Phase 3 — HARNESS_FIREWALL_DISABLED=1 bypass test. We bring up proxy
# alone with the env var set; the firewall script must short-circuit before
# applying any rule, leaving OUTPUT policy at the kernel default ACCEPT.
# example.com must be reachable from inside the container (the universal
# negative case is reachable iff the firewall is fully disabled).
# ============================================================================

PROJECT_BYP="harness-firewall-bypass"
ENV_BYP="$(mktemp -t harness-fw-byp.XXXXXX.env)"
OVERRIDE_BYP="$(mktemp -t harness-fw-byp.XXXXXX.yml)"
ALLOW_BYP="$(mktemp -t harness-fw-byp.XXXXXX.allow)"

# Use mockupstream so PROXY_API_URL is on the test allowlist.
test_generate_env "$ENV_BYP"
# Layer two override files: the mockupstream snippet AND a snippet that adds
# HARNESS_FIREWALL_DISABLED=1 to the proxy service. Compose merges -f files
# left-to-right; later wins on key collision.
test_generate_mockupstream_override "$OVERRIDE_BYP"
BYPASS_ENV_FILE="$(mktemp -t harness-fw-byp-env.XXXXXX.yml)"
cat >"$BYPASS_ENV_FILE" <<'EOF'
services:
  proxy:
    environment:
      HARNESS_FIREWALL_DISABLED: "1"
EOF
test_generate_allowlist "$ALLOW_BYP"
export HARNESS_ALLOWLIST_PATH="$ALLOW_BYP"

cleanup_byp() {
    test_cleanup "$PROJECT_BYP" "$ENV_BYP" "$OVERRIDE_BYP" "$ALLOW_BYP" "$BYPASS_ENV_FILE"
}
trap cleanup_byp EXIT INT TERM

# Defensive cleanup of any straggler from previous runs.
docker compose --project-name "$PROJECT_BYP" \
    -f docker-compose.yml -f "$OVERRIDE_BYP" -f "$BYPASS_ENV_FILE" \
    down -v --remove-orphans >/dev/null 2>&1 || true

COMPOSE_BYP=(docker compose --project-name "$PROJECT_BYP" --env-file "$ENV_BYP" \
    -f docker-compose.yml -f "$OVERRIDE_BYP" -f "$BYPASS_ENV_FILE")

test_section "Phase 3: HARNESS_FIREWALL_DISABLED=1 bypass"

echo "[fw] bringing up proxy + ollama + mockupstream with bypass on proxy"
"${COMPOSE_BYP[@]}" up -d --build

echo "[fw] waiting for mockupstream + proxy + ollama to become healthy (up to 180s)"
if ! test_wait_for_healthy "$PROJECT_BYP" mockupstream proxy ollama 180; then
    echo "[fw] services failed to become healthy" >&2
    "${COMPOSE_BYP[@]}" logs proxy        >&2 || true
    "${COMPOSE_BYP[@]}" logs mockupstream >&2 || true
    exit 1
fi

# proxy must show the loud "DISABLED via HARNESS_FIREWALL_DISABLED=1" line.
proxy_logs=$("${COMPOSE_BYP[@]}" logs proxy 2>&1 || true)
if ! grep -q 'DISABLED via HARNESS_FIREWALL_DISABLED=1' <<<"$proxy_logs"; then
    echo "[fw] FAIL: proxy did not log DISABLED line" >&2
    echo "${proxy_logs}" | tail -40 >&2
    exit 1
fi

# Inside proxy, OUTPUT policy must NOT be DROP (we never applied rules).
proxy_cid=$("${COMPOSE_BYP[@]}" ps -q proxy)
pol=$(docker exec "$proxy_cid" iptables -S OUTPUT 2>/dev/null | head -n 1 || true)
if grep -q '^-P OUTPUT DROP' <<<"$pol"; then
    echo "[fw] FAIL: proxy OUTPUT policy is DROP despite bypass (got: $pol)" >&2
    exit 1
fi
echo "[fw]   proxy OUTPUT policy: $pol (expected ACCEPT)"

# example.com must be reachable from inside proxy (firewall is off).
set +e
docker exec "$proxy_cid" curl --connect-timeout 5 -s -o /dev/null https://example.com
curl_rc=$?
set -e
# The host network may or may not be allowed to dial out — accept any non-
# firewall failure (timeout 28, connection refused 7, etc.) but FAIL if we
# get the "permission denied / icmp-admin-prohibited" signature (ECONNREFUSED
# manifests as rc=7; iptables REJECT manifests as a refused connection too,
# distinguished only by ICMP). Practical check: if we got a *successful*
# response, that's definitive proof the bypass is working. If we got rc=28
# (timeout) or rc=6/7 (resolve/connect failed), we treat as inconclusive
# rather than a failure — the iptables policy check above is the authoritative
# proof.
if (( curl_rc == 0 )); then
    echo "[fw]   proxy: example.com reachable — bypass confirmed"
else
    echo "[fw]   proxy: example.com curl rc=$curl_rc (network may not allow outbound, but iptables policy != DROP confirms bypass)"
fi

# Other services (ollama) must STILL have the firewall applied — bypass is
# per-service via the env var, not project-wide.
ollama_cid=$("${COMPOSE_BYP[@]}" ps -q ollama)
opol=$(docker exec "$ollama_cid" iptables -S OUTPUT 2>/dev/null | head -n 1 || true)
if ! grep -q '^-P OUTPUT DROP' <<<"$opol"; then
    echo "[fw] FAIL: ollama OUTPUT policy is NOT DROP (bypass leaked across services?)" >&2
    docker exec "$ollama_cid" iptables -S OUTPUT >&2 || true
    exit 1
fi
echo "[fw]   ollama OUTPUT policy: $opol (firewall still applied — good)"

echo "[fw] Phase 3 OK (HARNESS_FIREWALL_DISABLED bypass works per-service)"

echo "============================================================"
echo " FIREWALL TEST PASSED"
echo "============================================================"
exit 0
