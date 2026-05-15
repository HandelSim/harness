#!/usr/bin/env bash
#
# Dockerized Harbor wrapper. Same CLI surface as `harbor` on PATH, but
# runs out of a pinned container so the host doesn't need uv/pipx/Python.
#
# Usage (identical to bare harbor):
#   harbor.sh run --jobs-dir ./runs --agent-import-path foo:Bar ...
#
# Resolution order in tests/benchmarks/runners/_lib.sh:
#   1. HARNESS_BENCH_HARBOR override (explicit path or 'docker')
#   2. `harbor` on PATH
#   3. this wrapper (built on demand)
#
# Mounts:
#   /var/run/docker.sock         (so Harbor can spawn per-task siblings)
#   <repo root>:/work            (the runner cwd, where Harbor reads/writes)
#   $PWD:$PWD                    (so any absolute paths the runner passes
#                                 resolve identically inside the container)
#
# The image is built lazily on first invocation. Rebuild manually with:
#   docker build -t harness-harbor:<version> tests/benchmarks/harbor

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

IMAGE_TAG="${HARNESS_BENCH_HARBOR_IMAGE:-harness-harbor:0.6.6}"

# Build once and cache. `docker image inspect` exits 0 only if present.
if ! docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
    echo "[harbor.sh] building ${IMAGE_TAG} (one-time)..." >&2
    docker build -t "${IMAGE_TAG}" "${SCRIPT_DIR}" >&2
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

exec docker run --rm "${tty_args[@]}" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "${REPO_ROOT}":"${REPO_ROOT}" \
    -v "${PWD}":"${PWD}" \
    -w "${PWD}" \
    "${env_args[@]}" \
    "${IMAGE_TAG}" "$@"
