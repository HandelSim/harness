ARG OLLAMA_VERSION=0.21.2
FROM ollama/ollama:${OLLAMA_VERSION}

# curl is needed by the entrypoint (health-wait + /api/create POST) and by
# the compose healthcheck. ca-certificates is needed for any TLS that the
# upstream proxy might do later. The base image is debian-slim, so apt works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/harness-entrypoint.sh
RUN chmod +x /usr/local/bin/harness-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/harness-entrypoint.sh"]
