# harness-claude-agent
#
# Runs Anthropic's `claude` CLI (claude-code) configured to talk to the local
# ollama instance via env vars. The entrypoint (assembled at runtime, not
# bake-time) maps harness-level env vars onto the per-tool flags claude-code
# expects.
#
# The image is a runtime shell only: nothing about the upstream API, the
# stub model name, or the user's auth lives in the image. All of that comes
# from `docker run -e ...` (Phase 4's `harness` script).

FROM node:20-bookworm-slim

# Runtime deps:
#   tmux           — the entrypoint wraps claude in a tmux session for reattach
#   git            — claude-code shells out to git for some operations
#   curl           — handy for in-container debugging; no required path uses it
#   ca-certificates— TLS for any outbound HTTPS (none expected, but harmless)
#   procps         — `ps`, used by tmux/claude diagnostics
#   gosu           — used by the entrypoint to drop from root to the harness
#                    user after remapping uid/gid to match the host caller
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux \
        git \
        curl \
        ca-certificates \
        procps \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Pin claude-code to the version verified against this harness. Bump
# deliberately when upstream releases a new version that we re-test against.
ARG CLAUDE_CODE_VERSION=2.1.119
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version > /etc/claude-version 2>/dev/null || true

# Non-root harness user pre-created at uid/gid 1000.
#
# The default container user is harness (USER directive below). Phase 4's
# `harness` script overrides this with `docker run --user 0:0` so the
# container starts as root, the entrypoint remaps harness's uid/gid to
# match the host caller, and re-execs as harness via gosu. When the
# container is started without --user (e.g. agent_test.sh in test mode),
# it runs directly as harness and the entrypoint's remap block is a no-op.
#
# node:20-bookworm-slim ships with a `node` user pre-created at uid/gid 1000.
# Drop it first so the harness user can claim that slot.
RUN userdel --remove node 2>/dev/null || true \
    && groupdel node 2>/dev/null || true \
    && groupadd --gid 1000 harness \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash harness \
    && chown -R harness:harness /home/harness

COPY entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

WORKDIR /workspace
USER harness

ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
