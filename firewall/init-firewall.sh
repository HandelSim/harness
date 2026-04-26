#!/usr/bin/env bash
#
# firewall/init-firewall.sh — universal egress firewall for harness containers.
#
# Adapted from the canonical Anthropic devcontainer init script:
#   https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh
#
# Differences from the upstream version:
#   1. Allowlist of hosts is read from /etc/harness/allowlist (mounted from
#      <install-root>/.harness-allowlist), not hardcoded.
#   2. VS Code marketplace and statsig hosts are dropped (not used here).
#   3. GitHub IP-range fetch is best-effort: a transient failure logs a WARN
#      rather than aborting. The per-host `dig` resolution still works for
#      github.com hostnames in the allowlist.
#   4. Proxy container only: PROXY_API_URL hostname is validated against the
#      allowlist before applying rules. If absent, the script aborts with a
#      clear message — the proxy cannot reach upstream otherwise.
#   5. After firewall is up, /usr/local/bin/configure-git-credentials.sh is
#      invoked if present (agent containers ship it; proxy/ollama don't).
#
# Required packages (Debian-family): iptables, ipset, dnsutils, iproute2,
#   curl, jq. `aggregate` is optional — its absence triggers a non-aggregated
#   ipset, which is fine for the volume of IPs involved.
#
# Runs as root (the container entrypoint must invoke before any privilege
# drop, so NET_ADMIN/NET_RAW capabilities are still available).

set -euo pipefail
IFS=$'\n\t'

ALLOWLIST_FILE="${ALLOWLIST_FILE:-/etc/harness/allowlist}"

log() {
    echo "[harness-firewall] $*"
}
warn() {
    echo "[harness-firewall] WARN: $*" >&2
}
fatal() {
    echo "[harness-firewall] FATAL: $*" >&2
    exit 1
}

# --- 0a. opt-out switch -----------------------------------------------------
#
# When HARNESS_FIREWALL_DISABLED=1 we skip every rule and exit 0. Used by:
#   * `harness net open <service>` (service-level: stamped into the runtime
#     compose override as an env var on that service)
#   * `harness claude --net` / `harness opencode --net` (per-invocation)
# The variable is set deliberately by the harness CLI; the user is asked to
# acknowledge with a typed phrase before `net open` is honored. We log loudly
# so the bypass shows up in `docker logs` and there's a paper trail.
if [[ "${HARNESS_FIREWALL_DISABLED:-0}" == "1" ]]; then
    log "DISABLED via HARNESS_FIREWALL_DISABLED=1; skipping all rules"
    log "all egress is unrestricted in this container"
    exit 0
fi

log "starting init at $(date -u +%FT%TZ)"

# --- 0. sanity checks ---------------------------------------------------------

for tool in iptables ipset dig curl jq awk ip; do
    command -v "$tool" >/dev/null 2>&1 || fatal "$tool not found in PATH"
done
HAVE_AGGREGATE=0
command -v aggregate >/dev/null 2>&1 && HAVE_AGGREGATE=1

if [[ ! -f "$ALLOWLIST_FILE" ]]; then
    fatal "allowlist file not found at $ALLOWLIST_FILE; container cannot start. \
Mount it from <install-root>/.harness-allowlist via the docker-compose service definition."
fi

# --- 1. parse allowlist -------------------------------------------------------
#
# Returns hostnames on stdout, one per line. Inline `# git-push` annotations
# are stripped; configure-git-credentials.sh re-parses the same file later.

allowlist_hosts() {
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        {
            line = $0
            sub(/[[:space:]]*#.*$/, "", line)   # strip inline comments
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (length(line) > 0) print line
        }
    ' "$ALLOWLIST_FILE"
}

ALLOW_HOSTS=()
while IFS= read -r h; do
    [[ -n "$h" ]] && ALLOW_HOSTS+=("$h")
done < <(allowlist_hosts)

if (( ${#ALLOW_HOSTS[@]} == 0 )); then
    warn "allowlist contains no resolvable hosts — egress will be effectively closed except for DNS/loopback/host-net"
fi

# --- 2. preserve Docker DNS ---------------------------------------------------
#
# Without this, the container's DNS resolution (which goes through the Docker
# embedded resolver at 127.0.0.11) breaks immediately when we flush nat.

DOCKER_DNS_RULES=$(iptables-save -t nat | grep '127\.0\.0\.11' || true)

# --- 3. flush ----------------------------------------------------------------

iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

# --- 4. restore Docker DNS rules ---------------------------------------------

if [[ -n "$DOCKER_DNS_RULES" ]]; then
    log "restoring Docker DNS rules"
    iptables -t nat -N DOCKER_OUTPUT 2>/dev/null || true
    iptables -t nat -N DOCKER_POSTROUTING 2>/dev/null || true
    echo "$DOCKER_DNS_RULES" | xargs -L 1 iptables -t nat
else
    log "no Docker DNS rules to restore (host network or unusual setup)"
fi

# --- 5. allow basic egress ---------------------------------------------------

iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -i lo -j ACCEPT
# DNS — UDP and TCP (large responses fall back to TCP)
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A INPUT  -p udp --sport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
# SSH (debug-time access into the container)
iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT
iptables -A INPUT  -p tcp --sport 22 -m state --state ESTABLISHED -j ACCEPT

# --- 6. allow host network ---------------------------------------------------
#
# The default route's gateway lives in the host network. Allow that whole /24
# so containers in the same docker-compose project (proxy, ollama, mcp, etc.)
# can talk to each other without each appearing in the allowlist.

DEFAULT_IFACE=$(ip route | awk '$1=="default"{print $5; exit}')
HOST_GW=$(ip route | awk '$1=="default"{print $3; exit}')
if [[ -n "$HOST_GW" ]]; then
    HOST_NETWORK=$(echo "$HOST_GW" | sed 's/\.[0-9]*$/.0\/24/')
    log "host network detected as $HOST_NETWORK (iface $DEFAULT_IFACE)"
    iptables -A INPUT  -s "$HOST_NETWORK" -j ACCEPT
    iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT
else
    warn "could not detect default route gateway — intra-cluster traffic may be blocked"
fi

# --- 7. create ipset ---------------------------------------------------------

ipset create allowed-domains hash:net family inet hashsize 1024 maxelem 65536 -exist
ipset flush allowed-domains

# --- 8. fetch GitHub IP ranges (best-effort) ---------------------------------
#
# Only relevant if the allowlist references any github host. Failure here is
# logged but not fatal — per-host dig resolution below still works.

if grep -qE 'github\.com|githubusercontent\.com|githubapp\.com' "$ALLOWLIST_FILE"; then
    log "fetching GitHub IP ranges"
    gh_meta=$(curl -fsS --max-time 10 https://api.github.com/meta 2>/dev/null || true)
    if [[ -n "$gh_meta" ]] && echo "$gh_meta" | jq -e . >/dev/null 2>&1; then
        gh_cidrs=$(echo "$gh_meta" | jq -r '(.web // [])[], (.api // [])[], (.git // [])[], (.packages // [])[], (.actions // [])[]' 2>/dev/null \
            | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' \
            | sort -u || true)
        if (( HAVE_AGGREGATE )) && [[ -n "$gh_cidrs" ]]; then
            gh_cidrs=$(echo "$gh_cidrs" | aggregate -q 2>/dev/null || echo "$gh_cidrs")
        fi
        gh_count=0
        while IFS= read -r cidr; do
            [[ -z "$cidr" ]] && continue
            if ipset add allowed-domains "$cidr" -exist 2>/dev/null; then
                gh_count=$((gh_count + 1))
            fi
        done <<< "$gh_cidrs"
        log "added $gh_count GitHub IPv4 CIDRs"
    else
        warn "GitHub meta fetch failed or returned non-JSON; per-host dig resolution will still cover github.com"
    fi
fi

# --- 9. resolve allowlist hosts ----------------------------------------------

resolved_count=0
unresolvable=()
for host in "${ALLOW_HOSTS[@]}"; do
    ips=$(dig +short A "$host" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
    if [[ -z "$ips" ]]; then
        unresolvable+=("$host")
        continue
    fi
    while IFS= read -r ip; do
        [[ -z "$ip" ]] && continue
        ipset add allowed-domains "$ip" -exist 2>/dev/null || true
        resolved_count=$((resolved_count + 1))
    done <<< "$ips"
done
log "resolved $resolved_count IPs across ${#ALLOW_HOSTS[@]} hosts"
if (( ${#unresolvable[@]} > 0 )); then
    warn "could not resolve: ${unresolvable[*]} (DNS issue or typo)"
fi

# --- 10. PROXY_API_URL guardrail (proxy container only) ----------------------
#
# The proxy refuses to start if its upstream LLM API hostname is not in the
# allowlist. Catches the most common misconfiguration before the user sees
# opaque connection errors at request time. Other containers either don't set
# PROXY_API_URL or set it to an intra-cluster name (e.g. http://ollama:11434)
# whose hostname is not expected to be in the allowlist — those skip the
# guardrail.

if [[ -n "${PROXY_API_URL:-}" ]]; then
    api_host=$(echo "$PROXY_API_URL" | awk -F[/:] '{print $4}')
    if [[ -n "$api_host" ]]; then
        # Only enforce for non-intra-cluster hosts. A bare hostname with no
        # dots (ollama, mockupstream) is intra-cluster and lives behind the
        # host-network rule above.
        if [[ "$api_host" == *.* ]]; then
            if ! grep -qE "^[[:space:]]*${api_host}([[:space:]]|#|\$)" "$ALLOWLIST_FILE"; then
                cat >&2 <<EOF
[harness-firewall] FATAL: PROXY_API_URL hostname '${api_host}' is not in $ALLOWLIST_FILE.
[harness-firewall] The proxy cannot reach its upstream. Add it with:
[harness-firewall]     harness net allow ${api_host}
[harness-firewall] (or edit <install-root>/.harness-allowlist directly, then 'harness restart').
EOF
                exit 1
            fi
        fi
    fi
fi

# --- 11. apply firewall ------------------------------------------------------
#
# Default deny on OUTPUT/INPUT/FORWARD; allow only what we whitelisted.
# REJECT (rather than DROP) on the catch-all so blocked clients get an
# immediate "Permission denied" instead of waiting for a connect timeout —
# faster failures aid debugging.

iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Established connections — ride on whatever rule already let them out.
iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

# --- 12. verify --------------------------------------------------------------

log "verifying egress posture"

# Negative: example.com must be unreachable (it's never in our allowlists).
if curl --connect-timeout 5 -s -o /dev/null https://example.com 2>/dev/null; then
    fatal "example.com is reachable but should be blocked — firewall did not apply correctly"
fi

# Positive: at least one stable host from the allowlist must be reachable.
verify_host=""
for candidate in api.github.com pypi.org registry.npmjs.org; do
    if grep -qE "^[[:space:]]*${candidate}([[:space:]]|#|\$)" "$ALLOWLIST_FILE"; then
        verify_host="$candidate"
        break
    fi
done
if [[ -n "$verify_host" ]]; then
    if ! curl --connect-timeout 5 -s -o /dev/null "https://${verify_host}" 2>/dev/null; then
        fatal "$verify_host should be reachable but is not — firewall is too aggressive"
    fi
    log "verify ok: example.com blocked, $verify_host reachable"
else
    log "verify ok: example.com blocked (no canonical positive-case host in allowlist; skipping positive check)"
fi

# --- 13. done ---------------------------------------------------------------
#
# configure-git-credentials.sh is intentionally NOT invoked here. Agent
# entrypoints run it themselves after the gosu drop so `git config --global`
# writes to /home/harness/.gitconfig rather than /root/.gitconfig. Proxy and
# ollama don't need it (no git inside those images).

log "init complete"
