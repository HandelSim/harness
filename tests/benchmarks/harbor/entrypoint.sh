#!/usr/bin/env bash
#
# harbor entrypoint — sets up the egress firewall, then execs harbor.
#
# Why: harbor (the harborframework.com CLI) phones home to a hosted
# package registry (Supabase + CDN) to resolve --dataset arguments like
# `terminal-bench/canary` -> content-hash + tarball URL. While benchmarks
# are running we want zero outbound from harbor that isn't explicitly on
# the user's .harness-allowlist, so upstream LLM responses can't leak to
# harbor's backend or anywhere else harbor might decide to call.
#
# This entrypoint enforces that by running the universal harness firewall
# (same init-firewall.sh used by proxy/agents) before harbor ever
# starts. The firewall:
#   - Reads /etc/harness/allowlist (bind-mounted from .harness-allowlist
#     by harbor.sh).
#   - Sets default DROP on OUTPUT, allows only the allowlisted host IPs
#     plus loopback / DNS / docker DNS / intra-host network.
#   - Verifies the policy is live by probing example.com (must be blocked)
#     and one canonical allowlist host (must be reachable). The script
#     aborts non-zero if either probe disagrees with the policy.
#
# So by the time `exec harbor "$@"` runs, the firewall is provably up.
# If harbor tries to call its registry / Supabase / CDN and those hosts
# aren't in .harness-allowlist, the connection gets icmp-admin-prohibited
# immediately — no traffic leaves the container.
#
# Runs as root (the only user in this image). NET_ADMIN/NET_RAW capabilities
# are granted by harbor.sh's `docker run`. If the firewall fails to come up,
# we abort rather than fall through to harbor — running benchmarks without
# the firewall would defeat the whole point.

set -euo pipefail

if [[ ! -x /usr/local/bin/init-firewall.sh ]]; then
    echo "[harbor-entrypoint] FATAL: /usr/local/bin/init-firewall.sh missing." >&2
    echo "[harbor-entrypoint] Rebuild the harness-harbor image." >&2
    exit 1
fi

echo "[harbor-entrypoint] applying egress firewall (allowlist: /etc/harness/allowlist)" >&2
/usr/local/bin/init-firewall.sh
echo "[harbor-entrypoint] firewall up — harbor outbound restricted to allowlisted hosts" >&2

exec harbor "$@"
