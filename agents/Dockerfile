# harness-agent — unified image for both claude-code and opencode CLIs.
#
# Replaces the prior agents/claude/Dockerfile and agents/opencode/Dockerfile.
# Both CLIs are installed in a single image; the entrypoint dispatches on a
# mode argument (claude / opencode / shell) so we don't fork the runtime
# infrastructure (firewall init, UID remap, skel seeding, gosu drop) across
# two Dockerfiles. The mode is appended by the host harness script.
#
# Build context is the repo root (see docker-compose.yml). All COPYs below
# use repo-root-relative paths.

FROM node:20-bookworm-slim

# Shared runtime deps:
#   tmux           — entrypoint wraps the agent in a detached tmux session
#                    so the user can reattach from the host
#   git            — agents shell out to git for some operations
#   curl           — handy for in-container debugging
#   ca-certificates— TLS for any outbound HTTPS
#   procps         — `ps`, used by tmux/agent diagnostics
#   gosu           — used by the entrypoint to drop from root to the harness
#                    user after remapping uid/gid to match the host caller
#   jq             — used by the entrypoint to merge config files and by
#                    /usr/local/bin/init-firewall.sh
#   python3 + pipx — let users `pipx install <thing>` inside the agent and
#                    have it land under ~/.local/ (which is bind-mounted out
#                    via the persistent home). pipx solves competing dep
#                    sets for skill-style installers (graphify, etc.)
#   iptables, ipset, dnsutils, iproute2 — required by init-firewall.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux \
        git \
        curl \
        ca-certificates \
        procps \
        gosu \
        jq \
        python3 \
        python3-pip \
        python3-venv \
        pipx \
        iptables \
        ipset \
        dnsutils \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Both agent CLIs at pinned versions. Bumping requires editing this file.
ARG CLAUDE_CODE_VERSION=2.1.120
ARG OPENCODE_VERSION=1.14.24
ARG OPENAI_COMPAT_VERSION=2.0.41
ARG CCSTATUSLINE_VERSION=2.2.8

RUN npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
        "@ai-sdk/openai-compatible@${OPENAI_COMPAT_VERSION}" \
        "ccstatusline@${CCSTATUSLINE_VERSION}" \
    && (claude --version > /etc/claude-version 2>/dev/null || true) \
    && (opencode --version > /etc/opencode-version 2>/dev/null || true)

# Non-root harness user pre-created at uid/gid 1000.
#
# Phase 4's `harness` script overrides USER with `docker run --user 0:0` so
# the container starts as root, the entrypoint remaps harness's uid/gid to
# match the host caller, and re-execs as harness via gosu. When the
# container is started without --user (e.g. agent_test.sh in test mode), the
# remap block is a no-op.
#
# node:20-bookworm-slim ships with a `node` user pre-created at uid/gid 1000.
# Drop it first so the harness user can claim that slot.
RUN userdel --remove node 2>/dev/null || true \
    && groupdel node 2>/dev/null || true \
    && groupadd --gid 1000 harness \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash harness \
    && chown -R harness:harness /home/harness

# Universal egress firewall + git push blocker. init-firewall.sh runs as
# root (before the gosu drop in the entrypoint), configure-git-credentials.sh
# runs as harness (after the drop) so global git config lands in the user's
# home rather than /root.
COPY firewall/init-firewall.sh /usr/local/bin/init-firewall.sh
COPY firewall/configure-git-credentials.sh /usr/local/bin/configure-git-credentials.sh
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/configure-git-credentials.sh

# Block any interactive credential prompts from git or git-imported helpers.
# Combined with the credential.helper=/bin/false default that
# configure-git-credentials.sh applies, this means an accidental `git push`
# fails fast rather than blocking on a TTY prompt.
ENV GIT_TERMINAL_PROMPT=0
ENV GIT_ASKPASS=/bin/false

# Persistent home: snapshot the build-time /home/harness/ contents into
# /etc/skel/harness/ so the entrypoint can seed an empty bind mount on first
# run. This MUST be the last step that touches /home/harness — anything we
# add to the home dir after this point won't make it into the skeleton and
# therefore won't be visible on a freshly-mounted persistent home.
RUN mkdir -p /etc/skel/harness \
    && cp -a /home/harness/. /etc/skel/harness/ 2>/dev/null || true \
    && chown -R harness:harness /etc/skel/harness

# Default ccstatusline config (used by the claude mode; harmless for opencode
# and shell modes). Lands in the skel so first-run users see a useful status
# line; preserved on subsequent runs because the entrypoint's seed pass uses
# cp -an. Users override with `harness claude-statusline-config`.
COPY agents/defaults/ccstatusline-settings.json \
     /etc/skel/harness/.config/ccstatusline/settings.json
RUN chown -R harness:harness /etc/skel/harness/.config

# Surface user-installed binaries (pipx, pip --user) on PATH. /home/harness
# is shadowed by the bind mount at runtime; this PATH entry resolves once
# the skel-seeded ~/.local/bin appears at runtime.
ENV PATH="/home/harness/.local/bin:${PATH}"

COPY agents/entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

WORKDIR /workspace

# NO `USER harness` directive. The harness script invokes with --user 0:0
# so the entrypoint can run firewall init (which needs NET_ADMIN/NET_RAW)
# before gosu-dropping. agent_test.sh's test mode runs without --user; the
# entrypoint's remap block is then skipped.

ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
