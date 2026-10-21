#!/usr/bin/env bash
#
# firewall/init-firewall.sh — universal egress firewall for harness containers.
#
# Notable behaviors:
#   1. Allowlist of hosts is read from /etc/harness/allowlist (mounted from
#      <install-root>/.harness-allowlist), not hardcoded.
#   2. VS Code marketplace and statsig hosts are dropped (not used here).
#   3. GitHub IP-range fetch is best-effort: a transient failure logs a WARN
#      rather than aborting. The per-host `dig` resolution still works for
#      github.com hostnames in the allowlist.
#   4. Proxy container only: the hosts of the upstream URLs the proxy will
#      call (PROXY_API_URL and OAI_API_URL, or CHATGPT_BASE_URL under
#      PROXY_BACKEND=chatgpt) are allowed automatically from the environment, so
#      whatever .env points at is reachable without an allowlist edit. A set
#      but unparseable URL aborts with a clear message.
#   5. After firewall is up, /usr/local/bin/configure-git-credentials.sh is
#      invoked if present (agent containers ship it; the proxy doesn't).
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
#   * `harness --net` / `harness opencode --net` (per-invocation)
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

# --- 1b. upstream URL hosts from the environment (proxy container only) -----
#
# The proxy's upstreams come from .env, so their hosts are allowed from the
# same place instead of being kept in sync with the allowlist by hand. Only the
# URLs this proxy will actually call: PROXY_API_URL plus OAI_API_URL (when an
# OAI upstream is configured), or only CHATGPT_BASE_URL under
# PROXY_BACKEND=chatgpt (that backend calls neither of the other two). No other
# container sets these; a value with
# an intra-cluster host (http://mockupstream:8080, no dot) needs no ipset entry
# because the host-network rule below already covers it.
upstream_vars=(PROXY_API_URL OAI_API_URL)
if [[ "${PROXY_BACKEND:-}" == "chatgpt" ]]; then
    upstream_vars=(CHATGPT_BASE_URL)
fi
for uvar in "${upstream_vars[@]}"; do
    uurl="${!uvar:-}"
    [[ -z "$uurl" ]] && continue
    uhost=$(echo "$uurl" | awk -F[/:] '{print $4}')
    if [[ -z "$uhost" ]]; then
        # The parse assumes a scheme://host form. A scheme-less value
        # ("api.example.com/v1") yields nothing; the proxy would then fail at
        # request time with an opaque connect error. Say so now instead.
        cat >&2 <<EOF
[harness-firewall] FATAL: could not parse a hostname out of ${uvar}='${uurl}'.
[harness-firewall] It must include a scheme, e.g. https://api.example.com/v1.
[harness-firewall] Fix it with:
[harness-firewall]     harness config set ${uvar} <url>
EOF
        exit 1
    fi
    [[ "$uhost" == *.* ]] || continue
    ALLOW_HOSTS+=("$uhost")
    log "allowed $uhost (from $uvar)"
done

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
# so containers in the same docker-compose project (proxy, mcp, etc.)
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
    if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        ips="$host"   # an IPv4 literal (e.g. an upstream URL by IP); dig has nothing to resolve
    else
        ips=$(dig +short A "$host" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
    fi
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

# --- 9b. allow host MCP hosts (host.docker.internal) -------------------------
#
# A host MCP runs as a process on the user's own machine; the agent reaches it
# at http://host.docker.internal:<port>/mcp. harness sets HARNESS_HOST_MCP_HOSTS
# only when an enabled host MCP is present, so this whole block is inert for the
# proxy, serena, and every other container — zero regression when unset.
#
# host.docker.internal is an /etc/hosts entry (Docker injects it; on Linux it
# comes from harness's --add-host=...:host-gateway), NOT a DNS name. The section-9
# loop above uses `dig`, which queries DNS and ignores /etc/hosts, so it can
# never resolve this name. Resolve it from /etc/hosts instead (getent reads
# /etc/hosts; awk is a fallback if getent is unavailable) and open just that IP.
if [[ -n "${HARNESS_HOST_MCP_HOSTS:-}" ]]; then
    hm_count=0
    for hmhost in ${HARNESS_HOST_MCP_HOSTS//,/ }; do
        [[ -z "$hmhost" ]] && continue
        hm_ips=$(getent ahostsv4 "$hmhost" 2>/dev/null | awk '{print $1}' \
            | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u || true)
        if [[ -z "$hm_ips" ]]; then
            # getent missing or didn't read /etc/hosts; parse the file directly.
            hm_ips=$(awk -v h="$hmhost" \
                '($0 !~ /^[[:space:]]*#/) { for (i=2;i<=NF;i++) if ($i==h) { print $1; break } }' \
                /etc/hosts 2>/dev/null \
                | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u || true)
        fi
        if [[ -z "$hm_ips" ]]; then
            warn "host MCP host '$hmhost' not found in /etc/hosts — agent cannot reach it"
            continue
        fi
        while IFS= read -r ip; do
            [[ -z "$ip" ]] && continue
            ipset add allowed-domains "$ip" -exist 2>/dev/null || true
            hm_count=$((hm_count + 1))
            log "allowed host MCP $hmhost -> $ip"
        done <<< "$hm_ips"
    done
    log "resolved $hm_count host MCP IP(s) from /etc/hosts"
fi

# --- 10. apply firewall ------------------------------------------------------
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

# --- 11. verify --------------------------------------------------------------

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

# --- 12. done ---------------------------------------------------------------
#
# configure-git-credentials.sh is intentionally NOT invoked here. Agent
# entrypoints run it themselves after the gosu drop so `git config --global`
# writes to /home/harness/.gitconfig rather than /root/.gitconfig. The proxy
# doesn't need it (no git inside that image).

log "init complete"
