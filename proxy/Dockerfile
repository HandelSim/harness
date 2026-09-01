FROM python:3.12-slim

WORKDIR /app

# Build context is the repo root (see docker-compose.yml). All COPYs below
# use repo-root-relative paths.

# curl is needed by the docker healthcheck. The other packages
# (iptables/ipset/dnsutils/iproute2/jq/ca-certificates) are required by
# /usr/local/bin/init-firewall.sh which the entrypoint invokes at startup.
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

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/proxy.py .
COPY proxy/test_proxy.py .

# Shipped default for the hybrid recency reminder. docker-compose bind-mounts
# the user's editable copy (<install root>/.harness-data/reminder.md) over
# this path; the baked copy is what a container launched without that mount
# uses.
COPY proxy/reminder.md .

# Shipped default for the reminder's per-tool entries (legend, per-tool
# guidance, detail-tool list). Same deal: docker-compose bind-mounts the user's
# editable copy (<install root>/.harness-data/tool-guidance.json) over this
# path.
COPY proxy/tool-guidance.json .

# Universal egress firewall.
COPY firewall/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

# Entrypoint shim: runs init-firewall.sh, then execs proxy.py.
COPY proxy/entrypoint.sh /usr/local/bin/proxy-entrypoint.sh
RUN chmod +x /usr/local/bin/proxy-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/proxy-entrypoint.sh"]
