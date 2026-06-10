# harness-agent — image for the opencode CLI.
#
# The entrypoint dispatches on a mode argument (opencode / shell) so we don't
# fork the runtime infrastructure (firewall init, UID remap, skel seeding,
# gosu drop). The mode is appended by the host harness script.
#
# Build context is the repo root (see docker-compose.yml). All COPYs below
# use repo-root-relative paths.

FROM node:20-bookworm-slim

# Shared runtime deps:
#   git            — agents shell out to git for some operations
#   curl           — handy for in-container debugging
#   ca-certificates— TLS for any outbound HTTPS
#   procps         — `ps`, used by agent diagnostics
#   gosu           — used by the entrypoint to drop from root to the harness
#                    user after remapping uid/gid to match the host caller
#   jq             — used by the entrypoint to merge config files and by
#                    /usr/local/bin/init-firewall.sh
#   python3 + pipx — let users `pipx install <thing>` inside the agent and
#                    have it land under ~/.local/ (which is bind-mounted out
#                    via the persistent home). pipx solves competing dep
#                    sets for skill-style installers (graphify, etc.)
#   build-essential + python3-dev
#                  — C toolchain + Python headers so pipx/pip can build sdists
#                    of native Python extensions. Required because skill
#                    installers in the wild pull tree-sitter language
#                    grammars (e.g. graphifyy >=0.8.22 depends on
#                    `tree-sitter-dm`, which publishes only an sdist with no
#                    Linux wheels). Without these, `pipx install graphifyy`
#                    aborts on the gcc invocation. README explicitly directs
#                    users to that command, so the agent image must be able
#                    to compile.
#   iptables, ipset, dnsutils, iproute2 — required by init-firewall.sh
#   locales        — without it the container LANG defaults to POSIX, which
#                    makes opencode fall back to ASCII underscores instead of
#                    Unicode box-drawing/indicator glyphs in the TUI.
#                    Generated to C.UTF-8 below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        procps \
        gosu \
        jq \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        pipx \
        build-essential \
        iptables \
        ipset \
        dnsutils \
        iproute2 \
        locales \
    && rm -rf /var/lib/apt/lists/*

# Generate C.UTF-8 and export LANG/LC_ALL so every process inside the
# container (entrypoint, gosu-launched harness user, agent CLIs) sees a
# UTF-8 locale. Use C.UTF-8 (not en_US.UTF-8) for portability: no
# English-specific assumptions, just ASCII-compatible bytes plus UTF-8.
RUN sed -i 's/^# *\(C.UTF-8\)/\1/' /etc/locale.gen \
    && locale-gen C.UTF-8
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Agent CLI at a pinned version. Bumping requires editing this file.
#
# Headless-stdout caveat (handled in entrypoint.sh, not by pinning): since
# 1.14.42, `opencode run`'s renderer only writes the assistant body to a
# non-TTY stdout if the text part's `message.part.updated` event (carrying
# `time.end`) is processed before the session goes idle. For fast/short
# replies — the mock upstream, cached models — the idle event wins that race
# and the body is dropped (`opencode run "say hello"` exits 0 with no
# assistant content). This previously broke `harness -p` / `harness opencode
# -p` (full_pipeline_test.sh T9/T10, integration_test.sh smoke) and forced a
# rollback at 1.15.7 (issue #86). `--format json` does NOT dodge the race
# (verified on 1.15.7: on a race-loss only `step_start` is emitted, and ~10% of
# fast runs emit an entirely empty stream — zero bytes, no id). The headless
# `-p` path in agents/entrypoint.sh therefore recovers the final assistant text
# from the persisted session via `opencode export <session>`, which is
# independent of the render race; when the stream carries no session id it finds
# the newest session via `opencode session list --format json` — see
# architecture/containers.md.
#
# When bumping, refresh the version below and re-verify two things against the
# new binary:
#   1. headless `-p` still round-trips — full_pipeline_test.sh T9/T10 are the
#      gate (they assert the mock reply reaches stdout). If opencode renames
#      `opencode export` or changes its `{messages:[{info.role,parts:[{type,
#      text}]}]}` shape, OR changes `opencode session list --format json`'s
#      `[{id,directory,created}]` shape, update the jq in entrypoint.sh's
#      print-mode branch.
#   2. the proxy still pares the `task` tool description by anchoring on
#      opencode's "Available agent types and the tools they have access to:"
#      header (proxy/proxy.py `_OPENCODE_TASK_AGENTS_HEADER`).
#      proxy/test_proxy.py TestTaskDescriptionParing is the canary — refresh
#      its fixture from the bumped binary and fix the header constant if it
#      drifts.
ARG OPENCODE_VERSION=1.15.7
ARG OPENAI_COMPAT_VERSION=2.0.47

RUN npm install -g \
        "opencode-ai@${OPENCODE_VERSION}" \
        "@ai-sdk/openai-compatible@${OPENAI_COMPAT_VERSION}" \
    && (opencode --version > /etc/opencode-version 2>/dev/null || true)

# Non-root harness user pre-created at uid/gid 1000.
#
# Phase 4's `harness` script overrides USER with `docker run --user 0:0` so
# the container starts as root, the entrypoint remaps harness's uid/gid to
# match the host caller, and re-execs as harness via gosu. When the
# container is started without --user (e.g. test-mode invocations), the
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

# Surface user-installed binaries (pipx, pip --user) on PATH. /home/harness
# is shadowed by the bind mount at runtime; this PATH entry resolves once
# the skel-seeded ~/.local/bin appears at runtime.
ENV PATH="/home/harness/.local/bin:${PATH}"

COPY agents/entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

# Clipboard bridge (container side). opencode's TUI copies by shelling out to
# xclip/xsel/wl-copy, none of which work in this image (no display). This shim
# stands in for them and forwards the copied text to the host-shared bridge
# file, which the harness host process pushes onto the real host clipboard. The
# bridge is on by default: harness sets HARNESS_CLIPBOARD_FILE whenever the host
# has a clipboard tool. With no such file set the shim is an inert no-op. See
# architecture/containers.md.
COPY agents/clipboard-bridge.sh /usr/local/bin/harness-clipboard-bridge
RUN chmod +x /usr/local/bin/harness-clipboard-bridge \
    && for _n in xclip xsel wl-copy; do \
         ln -sf /usr/local/bin/harness-clipboard-bridge "/usr/local/bin/$_n"; \
       done

# No WORKDIR. The harness CLI passes -w pointing at the bind-mount target
# (the host CWD path) on every launch, so the agent lands where the user
# launched from. There is no /workspace anymore — the prior indirection
# was replaced with same-path host-to-container mounts (see issue #15).

# NO `USER harness` directive. The harness script invokes with --user 0:0
# so the entrypoint can run firewall init (which needs NET_ADMIN/NET_RAW)
# before gosu-dropping. Test-mode invocations may run without --user; the
# entrypoint's remap block is then skipped.

ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
