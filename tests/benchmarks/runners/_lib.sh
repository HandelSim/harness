#!/usr/bin/env bash
#
# Shared helper library for tests/benchmarks/runners/*.sh.
#
# All runners must source this and call:
#   bench_guard_ci         FIRST. Refuses to run under CI=*.
#   bench_check_arch       Warns on aarch64 + sets BENCH_DEFAULT_CONCURRENT=1.
#   bench_check_binfmt     Verifies binfmt_misc has qemu-x86_64 registered.
#   bench_require_harbor   Sets HARBOR_BIN to the dockerized wrapper.
#   bench_concurrency      Echoes the resolved --n-concurrent value.
#
# These functions are intentionally simple and dependency-free (bash + GNU
# coreutils only). They print human-readable diagnostics to stderr.

set -euo pipefail

# Resolve repo root from this file's location.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(cd "${_LIB_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${BENCH_ROOT}/../.." && pwd)"

# Default concurrency. May be overridden by bench_check_arch.
: "${BENCH_DEFAULT_CONCURRENT:=2}"

# --- CI guard ----------------------------------------------------------------
#
# Hard rule per PLAN.md: benchmarks never run in CI. The check is at the
# top of every runner.
bench_guard_ci() {
    if [[ -n "${CI:-}" ]]; then
        echo "Refusing to run benchmarks in CI environment (CI=${CI})." >&2
        echo "Benchmarks consume LLM API budget and take minutes to hours." >&2
        echo "Run them manually on a developer or dedicated host." >&2
        exit 1
    fi
}

# --- Architecture detection --------------------------------------------------
#
# Terminal-Bench task images are predominantly x86_64. On aarch64 they run
# under QEMU user-mode emulation (5-10x slowdown, some tasks fail). Default
# --n-concurrent=1 to keep wall-clock predictable and reduce contention on
# the (already-emulated) CPU.
bench_check_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)
            # Native: use the user-supplied default.
            ;;
        aarch64|arm64)
            echo "[bench] Host architecture: ${arch}" >&2
            echo "[bench] Terminal-Bench task images are mostly x86_64;" >&2
            echo "[bench] they will run under QEMU user-mode emulation." >&2
            echo "[bench] Expect 5-10x slowdown. Defaulting to" \
                 "--n-concurrent=1." >&2
            BENCH_DEFAULT_CONCURRENT=1
            ;;
        *)
            echo "[bench] Unknown architecture: ${arch}. Proceeding with" \
                 "--n-concurrent=${BENCH_DEFAULT_CONCURRENT}." >&2
            ;;
    esac
}

# --- binfmt_misc check -------------------------------------------------------
#
# On aarch64 we need qemu-x86_64 registered with binfmt_misc so the kernel
# transparently invokes qemu when an x86_64 ELF runs. The standard
# one-liner to register it cross-distro is:
#
#   docker run --privileged --rm tonistiigi/binfmt --install all
#
# (Docs: tests/benchmarks/README.md.)
bench_check_binfmt() {
    local arch
    arch="$(uname -m)"
    [[ "$arch" == "aarch64" || "$arch" == "arm64" ]] || return 0

    if [[ ! -d /proc/sys/fs/binfmt_misc ]]; then
        echo "[bench] WARNING: /proc/sys/fs/binfmt_misc not present;" \
             "x86_64 tasks will fail." >&2
        echo "[bench] On most distros, mount with:" >&2
        echo "  sudo mount binfmt_misc -t binfmt_misc /proc/sys/fs/binfmt_misc" >&2
        return 1
    fi

    if ! grep -qE 'enabled' /proc/sys/fs/binfmt_misc/qemu-x86_64 \
            2>/dev/null; then
        echo "[bench] WARNING: qemu-x86_64 binfmt not registered." >&2
        echo "[bench] Register with:" >&2
        echo "  docker run --privileged --rm tonistiigi/binfmt" \
             "--install all" >&2
        echo "[bench] Continuing — Harbor task containers will fail" \
             "exec until this is fixed." >&2
        return 1
    fi
}

# --- Harbor resolution -------------------------------------------------------
#
# Harbor always runs from the dockerized wrapper at
# tests/benchmarks/harbor/harbor.sh — no host install path is supported.
# The wrapper builds the pinned image on first call and bind-mounts the
# host docker socket so Harbor's per-task containers are siblings on the
# host daemon. Keeping Harbor out of the host environment means a laptop
# with .env credentials only needs docker + this repo.
bench_require_harbor() {
    local docker_wrapper="${BENCH_ROOT}/harbor/harbor.sh"
    if [[ ! -x "$docker_wrapper" ]]; then
        echo "[bench] FATAL: ${docker_wrapper} missing or not executable." >&2
        exit 1
    fi
    HARBOR_BIN="$docker_wrapper"
    export HARBOR_BIN
}

# --- Concurrency resolution --------------------------------------------------
#
# Order of precedence:
#   1. HARNESS_BENCH_N_CONCURRENT env var (explicit override)
#   2. --n-concurrent CLI flag (parsed by caller, passed in $1)
#   3. BENCH_DEFAULT_CONCURRENT (set by bench_check_arch)
bench_concurrency() {
    local cli_value="${1:-}"
    if [[ -n "${HARNESS_BENCH_N_CONCURRENT:-}" ]]; then
        echo "${HARNESS_BENCH_N_CONCURRENT}"
    elif [[ -n "$cli_value" ]]; then
        echo "$cli_value"
    else
        echo "${BENCH_DEFAULT_CONCURRENT}"
    fi
}

# --- Docker daemon sanity ----------------------------------------------------
#
# Harbor's per-task container needs host docker access to spawn the
# harness compose stack. This is a HARD requirement; print a clear
# diagnostic if the daemon is unreachable.
#
# We probe with `docker info` rather than `[[ -S /var/run/docker.sock ]]`
# because the latter is Linux-only — Docker Desktop on Windows/macOS
# reaches the daemon over a named pipe / vsock and has no host-side UNIX
# socket file, even though the `-v /var/run/docker.sock:...` mount used
# by harbor.sh is translated transparently by Docker Desktop's backend.
# `docker info` tests what we actually care about (daemon reachable) and
# works uniformly across Linux, macOS, and Windows.
bench_check_docker_socket() {
    if ! docker info >/dev/null 2>&1; then
        echo "[bench] WARNING: docker daemon not reachable" \
             "(\`docker info\` failed)." >&2
        echo "[bench] Harbor task containers cannot spawn harness compose." >&2
        echo "[bench] On Linux: start dockerd / check the 'docker' group." >&2
        echo "[bench] On Docker Desktop: start the app." >&2
        return 1
    fi
}

# --- Disk space sanity -------------------------------------------------------
#
# Each Terminal-Bench task image plus harness's compose stack consumes
# significant disk. Refuse to start if free space on the runs directory is
# under 20GB.
bench_check_disk() {
    local target="${1:-${BENCH_ROOT}/runs}"
    local free_kb
    free_kb="$(df -Pk "$target" | awk 'NR==2 {print $4}')"
    local free_gb=$((free_kb / 1024 / 1024))
    if (( free_gb < 20 )); then
        echo "[bench] WARNING: only ${free_gb}GB free on $(realpath "$target");" \
             "harbor recommends >=20GB." >&2
    fi
}

# --- jq wrapper (host or container fallback) --------------------------------
#
# bench_jq — run jq with host args/stdin/stdout/exit semantics. Prefers
# host jq; falls back to the proxy image's jq via a one-shot container
# when the host has no jq installed.
#
# Why: scheme files are JSON, so reading them needs a JSON parser. The
# bench runners' contract (README.md) is "no host Python, uv, pipx, or
# harbor binary needed — just docker + this repo" — which implies no
# host jq either. Most dev hosts do have jq, so the host path is fast;
# Windows / minimal Docker Desktop installs get the container path.
#
# Mirrors the `harness_jq` pattern in the top-level `harness` script
# (without the sidecar — bench runners read ≤2 schemes per invocation,
# so a one-shot container per call is fine).
#
# Fallback image: harness-proxy:latest. The proxy ships jq for its own
# firewall scripts (see proxy/Dockerfile), so reusing it avoids pulling
# a new image. If the proxy image isn't built yet, we build it once —
# same UX as harness_jq.
bench_jq() {
    if command -v jq >/dev/null 2>&1; then
        jq "$@"
        return $?
    fi

    if ! docker info >/dev/null 2>&1; then
        echo "[bench] FATAL: jq not installed on host and docker daemon" \
             "not reachable for the fallback." >&2
        echo "[bench] Install jq (apt/dnf/brew/pacman/choco) or start docker." >&2
        return 1
    fi

    if ! docker image inspect harness-proxy:latest >/dev/null 2>&1; then
        echo "[bench] host jq missing; building harness-proxy:latest for the" \
             "jq fallback (one-time, ~2-5 minutes)..." >&2
        if ! (cd "${REPO_ROOT}" && docker compose build proxy >&2); then
            echo "[bench] FATAL: harness-proxy build failed." >&2
            echo "[bench] Install jq on the host instead." >&2
            return 1
        fi
    fi

    # The container can't see host filesystem paths. If the last argument
    # is a host file, redirect it as stdin instead of passing it as a
    # positional arg — matches the `harness_jq` heuristic.
    local args=("$@")
    local n=${#args[@]}
    if (( n > 0 )) && [[ -f "${args[n-1]}" ]]; then
        local file="${args[n-1]}"
        unset 'args[n-1]'
        docker run --rm -i --entrypoint jq \
            harness-proxy:latest "${args[@]}" <"$file"
    else
        docker run --rm -i --entrypoint jq harness-proxy:latest "$@"
    fi
}

# --- Scheme env injection ----------------------------------------------------
#
# Read a JSON scheme file from tests/benchmarks/schemes/<name>.json and
# export each key in its "env" object into the current environment.
# Uses bench_jq, so works on any host with docker (jq optional).
bench_apply_scheme() {
    local scheme="${1:?scheme name required}"
    local scheme_file="${BENCH_ROOT}/schemes/${scheme}.json"
    if [[ ! -f "$scheme_file" ]]; then
        echo "[bench] FATAL: scheme file not found: $scheme_file" >&2
        exit 1
    fi
    # Emit `export KEY=value` lines from the scheme's .env block.
    # @sh formats values with shell-safe single-quote quoting.
    local kv
    kv=$(bench_jq -r '
        (.env // {}) | to_entries[] | "export \(.key)=\(.value | tostring | @sh)"
    ' "$scheme_file") || {
        echo "[bench] FATAL: failed to parse $scheme_file" >&2
        exit 1
    }
    eval "$kv"
    echo "[bench] Applied scheme: ${scheme}" >&2
}

# Provide BENCH_ROOT and REPO_ROOT to callers without exporting.
export BENCH_ROOT REPO_ROOT
