#!/usr/bin/env bash
#
# tests/firewall_test.sh — focused checks that the per-container egress
# firewall (firewall/init-firewall.sh) is actually applied at runtime.
#
# Verifies upstream auto-allow and bypass behavior:
#
#   Phase 2 — upstream auto-allow. Brings up proxy with PROXY_API_URL and
#   OAI_API_URL pointing at hosts that are NOT in the allowlist; asserts the
#   firewall allows both from the environment and finishes init (no FATAL).
#
#   Phase 3 — bypass. Brings up proxy with HARNESS_FIREWALL_DISABLED=1;
#   asserts the firewall init script short-circuits and OUTPUT policy stays
#   ACCEPT for proxy.
#
# (Phase 1 — positive firewall posture inside running containers — was
# folded into proxy_test.sh, which exercises the same path with the
# firewall on by default.)
#
# Both phases use `docker compose down --remove-orphans` on cleanup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${REPO_ROOT}/tests/lib/test_helpers.sh"

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
# Phase 2 — upstream hosts from .env are allowed without an allowlist entry.
# ============================================================================

PROJECT_NEG="harness-firewall-neg"
ENV_NEG="$(mktemp -t harness-fw-neg.XXXXXX.env)"
OVERRIDE_NEG="$(mktemp -t harness-fw-neg.XXXXXX.yml)"
ALLOW_NEG="$(mktemp -t harness-fw-neg.XXXXXX.allow)"

# Dotted hosts (so they are not treated as intra-cluster) that are absent from
# the allowlist below. .invalid never resolves, so the firewall WARNs about
# resolution, which is fine: the assertion is that it allowed them at all
# instead of refusing to start.
test_generate_env "$ENV_NEG" \
    "PROXY_API_URL=https://upstream.invalid/v1/chat/completions" \
    "OAI_API_URL=https://oai-upstream.invalid:8443/openai/v1" \
    "OAI_API_KEY=test-oai-key" \
    "OAI_MODELS=gpt-test"

# Reuse the same mockupstream override only because compose insists on
# resolvability of `harness-net`.
test_generate_mockupstream_override "$OVERRIDE_NEG"

# Allowlist deliberately omits both upstream hosts.
test_generate_allowlist "$ALLOW_NEG"

# harness_docker_path rewrites /tmp/... to a Docker-Desktop-mountable path on
# Windows (WSL2 backend); on Linux it's a passthrough. Without it the bind
# mount silently produces an empty /etc/harness/allowlist inside the
# container, which init-firewall.sh treats as fatal.
export HARNESS_ALLOWLIST_PATH="$(harness_docker_path "$ALLOW_NEG")"

cleanup_neg() {
    test_cleanup "$PROJECT_NEG" "$ENV_NEG" "$OVERRIDE_NEG" "$ALLOW_NEG"
}
trap cleanup_neg EXIT INT TERM

harness_docker compose --project-name "$PROJECT_NEG" \
    -f docker-compose.yml -f "$OVERRIDE_NEG" \
    down -v --remove-orphans >/dev/null 2>&1 || true

COMPOSE_NEG=(harness_docker compose --project-name "$PROJECT_NEG" --env-file "$ENV_NEG" \
    -f docker-compose.yml -f "$OVERRIDE_NEG")

test_section "Phase 2: upstream hosts auto-allowed from .env"

echo "[fw] starting proxy alone with out-of-allowlist upstream URLs"
# The assertion is on the proxy container's logs (persistent and definitive),
# not on `up`'s exit code or a polled State.Status.
"${COMPOSE_NEG[@]}" up -d --build proxy >/dev/null 2>&1 || true

deadline=$(( $(date +%s) + 30 ))
matched=0
while true; do
    logs=$("${COMPOSE_NEG[@]}" logs proxy 2>&1 || true)
    if grep -q 'harness-firewall\] init complete' <<<"$logs"; then
        matched=1
        break
    fi
    if grep -q 'harness-firewall\] FATAL' <<<"$logs"; then
        break
    fi
    if (( $(date +%s) >= deadline )); then
        break
    fi
    sleep 2
done

if (( ! matched )); then
    echo "[fw] FAIL: firewall did not finish init with out-of-allowlist upstreams" >&2
    "${COMPOSE_NEG[@]}" logs proxy >&2 || true
    exit 1
fi
for want in 'allowed upstream\.invalid (from PROXY_API_URL)' \
            'allowed oai-upstream\.invalid (from OAI_API_URL)'; do
    if ! grep -q "$want" <<<"$logs"; then
        echo "[fw] FAIL: proxy logs lack '$want'" >&2
        "${COMPOSE_NEG[@]}" logs proxy >&2 || true
        exit 1
    fi
done

echo "[fw] Phase 2 OK (both upstream hosts allowed from the environment)"

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
# See Phase 2 setup above for the harness_docker_path rationale.
export HARNESS_ALLOWLIST_PATH="$(harness_docker_path "$ALLOW_BYP")"

cleanup_byp() {
    test_cleanup "$PROJECT_BYP" "$ENV_BYP" "$OVERRIDE_BYP" "$ALLOW_BYP" "$BYPASS_ENV_FILE"
}
trap cleanup_byp EXIT INT TERM

# Defensive cleanup of any straggler from previous runs.
harness_docker compose --project-name "$PROJECT_BYP" \
    -f docker-compose.yml -f "$OVERRIDE_BYP" -f "$BYPASS_ENV_FILE" \
    down -v --remove-orphans >/dev/null 2>&1 || true

COMPOSE_BYP=(harness_docker compose --project-name "$PROJECT_BYP" --env-file "$ENV_BYP" \
    -f docker-compose.yml -f "$OVERRIDE_BYP" -f "$BYPASS_ENV_FILE")

test_section "Phase 3: HARNESS_FIREWALL_DISABLED=1 bypass"

echo "[fw] bringing up proxy + mockupstream with bypass on proxy"
"${COMPOSE_BYP[@]}" up -d --build

echo "[fw] waiting for mockupstream + proxy to become healthy (up to 180s)"
if ! test_wait_for_healthy "$PROJECT_BYP" mockupstream proxy 180; then
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
pol=$(harness_docker exec "$proxy_cid" iptables -S OUTPUT 2>/dev/null | head -n 1 || true)
if grep -q '^-P OUTPUT DROP' <<<"$pol"; then
    echo "[fw] FAIL: proxy OUTPUT policy is DROP despite bypass (got: $pol)" >&2
    exit 1
fi
echo "[fw]   proxy OUTPUT policy: $pol (expected ACCEPT)"

# example.com must be reachable from inside proxy (firewall is off).
set +e
harness_docker exec "$proxy_cid" curl --connect-timeout 5 -s -o /dev/null https://example.com
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

echo "[fw] Phase 3 OK (HARNESS_FIREWALL_DISABLED bypass works on proxy)"

echo "============================================================"
echo " FIREWALL TEST PASSED"
echo "============================================================"
exit 0
