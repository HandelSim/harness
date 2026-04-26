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

# Build context is the repo root (see docker-compose.yml). All COPYs below
# use repo-root-relative paths.

# Runtime deps:
#   tmux           — the entrypoint wraps claude in a tmux session for reattach
#   git            — claude-code shells out to git for some operations
#   curl           — handy for in-container debugging; no required path uses it
#   ca-certificates— TLS for any outbound HTTPS (none expected, but harmless)
#   procps         — `ps`, used by tmux/claude diagnostics
#   gosu           — used by the entrypoint to drop from root to the harness
#                    user after remapping uid/gid to match the host caller
#   jq             — used by the harness script to merge MCP client configs;
#                    the harness script itself runs on the host but the
#                    in-container `claude mcp` workflow uses jq too. Also
#                    required by /usr/local/bin/init-firewall.sh.
#   python3 + pipx — let users `pipx install <thing>` inside the agent and
#                    have it land under ~/.local/ (which is bind-mounted out
#                    via the persistent home). pip alone wouldn't isolate
#                    competing dep sets; pipx solves that for skill-style
#                    installers (graphify, etc.). venv backs pipx.
#   iptables, ipset, dnsutils, iproute2 — required by init-firewall.sh.
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

# Pin claude-code to the version verified against this harness. Bump
# deliberately when upstream releases a new version that we re-test against.
ARG CLAUDE_CODE_VERSION=2.1.120
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version > /etc/claude-version 2>/dev/null || true

# Pre-install ccstatusline globally so the default settings.json wired up by
# the entrypoint can run without npx hitting the network. Pinned for
# reproducibility — bump deliberately when re-tested.
ARG CCSTATUSLINE_VERSION=2.2.8
RUN npm install -g "ccstatusline@${CCSTATUSLINE_VERSION}"

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

# Universal egress firewall + git push blocker. init-firewall.sh runs as
# root (before the gosu drop in the entrypoint), configure-git-credentials.sh
# runs as harness (after the drop) so global git config lands in the
# user's home rather than /root.
COPY firewall/init-firewall.sh /usr/local/bin/init-firewall.sh
COPY firewall/configure-git-credentials.sh /usr/local/bin/configure-git-credentials.sh
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/configure-git-credentials.sh

COPY agents/claude/entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

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

# B3-MANAGED: ccstatusline-default — curated default ccstatusline config
# (model + cwd + git branch + context bar). Lands in the skel so first-run
# users see a useful status line; preserved on subsequent runs because the
# entrypoint's seed pass uses cp -an. Users override with
# `harness claude-statusline-config` (which writes to the bind-mounted home).
COPY agents/claude/defaults/ccstatusline-settings.json \
     /etc/skel/harness/.config/ccstatusline/settings.json
RUN chown -R harness:harness /etc/skel/harness/.config

# Surface user-installed binaries (pipx, pip --user) on PATH. /home/harness
# is shadowed by the bind mount at runtime; this PATH entry resolves once
# the skel-seeded ~/.local/bin appears at runtime.
ENV PATH="/home/harness/.local/bin:${PATH}"

WORKDIR /workspace
USER harness

ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
