#!/usr/bin/env bash
#
# proxy entrypoint — sets up the egress firewall, then execs the Python proxy.
#
# The firewall is run as root (the only user in this image). NET_ADMIN/NET_RAW
# capabilities are granted by docker-compose. PROXY_API_URL is read by
# init-firewall.sh to validate that the upstream LLM hostname is on the
# allowlist before any rule lands — if it isn't, the script aborts with a
# clear error and the container exits.

set -euo pipefail

if [[ -x /usr/local/bin/init-firewall.sh ]]; then
    /usr/local/bin/init-firewall.sh
else
    echo "[proxy-entrypoint] WARN: init-firewall.sh missing; running without firewall" >&2
fi

# Honor an override command (e.g. `docker compose run --rm proxy python -m
# unittest ...` from scripts/proxy_test.sh). When no args are supplied, fall
# through to the default CMD baked into the image.
if (( $# > 0 )); then
    exec "$@"
fi
exec python /app/proxy.py
