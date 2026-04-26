ARG OLLAMA_VERSION=0.21.2
FROM ollama/ollama:${OLLAMA_VERSION}

# Build context is the repo root (see docker-compose.yml). All COPYs below
# use repo-root-relative paths.

# curl is needed by the entrypoint (health-wait + /api/create POST) and by
# the compose healthcheck. ca-certificates is needed for any TLS that the
# upstream proxy might do later. The remaining packages
# (iptables/ipset/dnsutils/iproute2/jq) are required by
# /usr/local/bin/init-firewall.sh which the entrypoint invokes at startup.
# The base image is debian-slim, so apt works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        iptables \
        ipset \
        dnsutils \
        iproute2 \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Universal egress firewall.
COPY firewall/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

COPY ollama/entrypoint.sh /usr/local/bin/harness-entrypoint.sh
RUN chmod +x /usr/local/bin/harness-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/harness-entrypoint.sh"]
