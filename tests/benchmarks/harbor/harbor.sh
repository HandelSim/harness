#!/usr/bin/env bash
#
# Dockerized Harbor wrapper. Runs Harbor out of a pinned container so the
# host never needs uv/pipx/Python/harbor. The benchmark runners under
# tests/benchmarks/runners/ always invoke harbor via this wrapper.
#
# Usage (same CLI surface as bare harbor):
#   harbor.sh run --jobs-dir ./runs --agent-import-path foo:Bar ...
#
# Mounts:
#   /var/run/docker.sock         (so Harbor can spawn per-task siblings)
#   <repo root>:<repo root>      (the runner cwd, where Harbor reads/writes)
#   $PWD:$PWD                    (so any absolute paths the runner passes
#                                 resolve identically inside the container)
#   .harness-allowlist:/etc/harness/allowlist:ro
#                                (consumed by init-firewall.sh in the
#                                 container's entrypoint — restricts harbor's
#                                 outbound to allowlisted hosts only)
#   <cache dir>:/harbor-cache    (persistent harbor HOME/cache — survives the
#                                 --rm container so a dataset downloaded by
#                                 prefetch.sh stays on disk for the sealed run.
#                                 HOME + XDG_CACHE_HOME point here. Override the
#                                 host path with HARNESS_BENCH_CACHE_DIR.)
#
# Capabilities:
#   NET_ADMIN, NET_RAW           (required by init-firewall.sh to manage
#                                 iptables/ipset)
#
# The image is built lazily on first invocation. Rebuild manually with:
#   docker build -t harness-harbor:<version> -f tests/benchmarks/harbor/Dockerfile .
# (build context must be repo root so the Dockerfile can COPY firewall/).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# shellcheck source=../../../scripts/lib/platform.sh
source "${REPO_ROOT}/scripts/lib/platform.sh"

IMAGE_TAG="${HARNESS_BENCH_HARBOR_IMAGE:-harness-harbor:0.6.6}"

# Persistent cache dir. The harbor container runs with `--rm`, so anything
# harbor writes under its own HOME ($HOME/.cache, $HOME/.harbor, ...) is
# destroyed on exit unless we bind-mount it. We point HOME (and
# XDG_CACHE_HOME) at this dir so a dataset that prefetch.sh downloads with
# harbor's backend temporarily allowlisted persists to disk and is reused by
# the later *sealed* run (backend removed from the allowlist). Without this,
# every run re-queries harbor's registry and the backend could never be
# removed — i.e. harbor could never be sealed for real datasets.
#
# Default lives under runs/ (gitignored). Files are created by the container's
# root user, so they may be root-owned on the host. Override with
# HARNESS_BENCH_CACHE_DIR.
CACHE_DIR="${HARNESS_BENCH_CACHE_DIR:-${REPO_ROOT}/tests/benchmarks/runs/.harbor-cache}"
mkdir -p "${CACHE_DIR}"

# Allowlist path: same default as docker-compose.yml so harbor uses the same
# file as the rest of the stack. Override with HARNESS_ALLOWLIST_PATH.
ALLOWLIST_PATH="${HARNESS_ALLOWLIST_PATH:-${REPO_ROOT}/.harness-allowlist}"
if [[ ! -f "${ALLOWLIST_PATH}" ]]; then
    cat >&2 <<EOF
[harbor.sh] FATAL: allowlist file not found at ${ALLOWLIST_PATH}.
[harbor.sh] Harbor runs behind the same egress firewall as the rest of
[harbor.sh] the harness stack and refuses to start without one. Copy
[harbor.sh] .harness-allowlist.example to .harness-allowlist and edit, or
[harbor.sh] set HARNESS_ALLOWLIST_PATH=<path> to point at your existing file.
EOF
    exit 1
fi

# Build once and cache. `docker image inspect` exits 0 only if present.
# Build context is repo root (not SCRIPT_DIR) so the Dockerfile can COPY
# firewall/init-firewall.sh into the image.
if ! harness_docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
    echo "[harbor.sh] building ${IMAGE_TAG} (one-time)..." >&2
    harness_docker build -t "${IMAGE_TAG}" \
        -f "${SCRIPT_DIR}/Dockerfile" "${REPO_ROOT}" >&2
fi

# Forward PROXY_*/HARNESS_* env vars so the runner's exported scheme reaches
# Harbor (which then forwards selected ones into the per-task container via
# --ae / --ee). We avoid `--env-file` because the runner already exported
# what it needs into this process's env.
env_args=()
while IFS='=' read -r k _; do
    case "$k" in
        PROXY_*|HARNESS_*|PYTHONPATH|HARBOR_*) env_args+=("--env" "$k") ;;
    esac
done < <(env)

# TTY only when stdout is a terminal (avoid breaking captured runs).
tty_args=()
[[ -t 0 && -t 1 ]] && tty_args=(-it)

harness_docker_exec run --rm "${tty_args[@]}" \
    --cap-add NET_ADMIN --cap-add NET_RAW \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$(harness_docker_path "${REPO_ROOT}")":"${REPO_ROOT}" \
    -v "$(harness_docker_path "${PWD}")":"${PWD}" \
    -v "$(harness_docker_path "${ALLOWLIST_PATH}")":/etc/harness/allowlist:ro \
    -v "$(harness_docker_path "${CACHE_DIR}")":/harbor-cache \
    --env HOME=/harbor-cache \
    --env XDG_CACHE_HOME=/harbor-cache/.cache \
    -w "${PWD}" \
    "${env_args[@]}" \
    "${IMAGE_TAG}" "$@"
