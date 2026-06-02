# Dockerized Harbor for the harness benchmark runners.
#
# Purpose: remove the host requirement for Python + uv/pipx + harbor. The
# runners invoke this image via tests/benchmarks/harbor/harbor.sh, which
# bind-mounts the repo into /work and the host docker socket so Harbor can
# spawn per-task containers as siblings on the host daemon.
#
# Pinned to a Harbor release rather than `latest` so a Harbor breaking
# change doesn't silently invalidate cached runs. Bump HARBOR_VERSION when
# upgrading.
#
# Build context is the repo root (see harbor.sh) so this Dockerfile can
# COPY firewall/init-firewall.sh into the image.
ARG PYTHON_VERSION=3.13
ARG HARBOR_VERSION=0.6.6

FROM python:${PYTHON_VERSION}-slim

ARG HARBOR_VERSION

# --- docker CLI + firewall tooling -----------------------------------------
# - docker-ce-cli / docker-compose-plugin: harbor shells out to `docker` on
#   $PATH to launch per-task containers. We only need the CLI; the daemon
#   lives on the host and we reach it via the bind-mounted socket.
# - iptables / ipset / dnsutils / iproute2 / jq: required by
#   /usr/local/bin/init-firewall.sh which the entrypoint invokes at startup
#   so harbor's outbound traffic is restricted to .harness-allowlist hosts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
        iptables ipset dnsutils iproute2 jq \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && . /etc/os-release \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

# --- harbor -----------------------------------------------------------------
RUN pip install --no-cache-dir "harbor==${HARBOR_VERSION}"

# --- universal egress firewall ---------------------------------------------
# Same init script used by proxy/agents — harbor uses the shared
# .harness-allowlist (mounted at /etc/harness/allowlist by harbor.sh) so
# harbor's outbound is restricted the same way as the rest of the stack.
COPY firewall/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

# Entrypoint shim: runs init-firewall.sh, then execs harbor.
COPY tests/benchmarks/harbor/entrypoint.sh /usr/local/bin/harbor-entrypoint.sh
RUN chmod +x /usr/local/bin/harbor-entrypoint.sh

# Runners mount the repo at /work. PYTHONPATH for adapter loading is set by
# the wrapper, not baked in here.
WORKDIR /work

ENTRYPOINT ["/usr/local/bin/harbor-entrypoint.sh"]
