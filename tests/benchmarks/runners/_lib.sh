#!/usr/bin/env bash
#
# Shared helper library for tests/benchmarks/runners/*.sh.
#
# All runners must source this and call:
#   bench_guard_ci         FIRST. Refuses to run under CI=*.
#   bench_check_arch       Warns on aarch64 + sets BENCH_DEFAULT_CONCURRENT=1.
#   bench_check_binfmt     Verifies binfmt_misc has qemu-x86_64 registered.
#   bench_require_harbor   Errors out if `harbor` is not on PATH.
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
# Two modes:
#   1. Host harbor — `harbor` on PATH (uv/pipx/pip install).
#   2. Dockerized harbor — tests/benchmarks/harbor/harbor.sh, which runs
#      Harbor inside a container with the host docker socket and the repo
#      bind-mounted. Image is built lazily on first call.
#
# Override:
#   HARNESS_BENCH_HARBOR=docker      force the dockerized wrapper
#   HARNESS_BENCH_HARBOR=/path/bin   use a specific binary
#   HARNESS_BENCH_HARBOR=host        force the PATH harbor (fail if absent)
#
# Default: prefer PATH; fall back to dockerized.
#
# The runners use `${HARBOR_BIN}` (not bare `harbor`) for the actual
# invocation. bench_require_harbor sets HARBOR_BIN and exits non-zero
# if neither mode is available.
bench_require_harbor() {
    local mode="${HARNESS_BENCH_HARBOR:-auto}"
    local docker_wrapper="${BENCH_ROOT}/harbor/harbor.sh"
    case "$mode" in
        host)
            command -v harbor >/dev/null 2>&1 || {
                echo "[bench] FATAL: HARNESS_BENCH_HARBOR=host but 'harbor'" \
                     "not on PATH." >&2
                exit 1
            }
            HARBOR_BIN="$(command -v harbor)"
            ;;
        docker)
            [[ -x "$docker_wrapper" ]] || {
                echo "[bench] FATAL: HARNESS_BENCH_HARBOR=docker but" \
                     "${docker_wrapper} missing or not executable." >&2
                exit 1
            }
            HARBOR_BIN="$docker_wrapper"
            ;;
        auto)
            if command -v harbor >/dev/null 2>&1; then
                HARBOR_BIN="$(command -v harbor)"
            elif [[ -x "$docker_wrapper" ]]; then
                echo "[bench] harbor not on PATH; using dockerized wrapper" \
                     "(${docker_wrapper})." >&2
                HARBOR_BIN="$docker_wrapper"
            else
                echo "[bench] FATAL: no harbor available. Install one of:" >&2
                echo "  uv tool install harbor   (host install)" >&2
                echo "  or ensure tests/benchmarks/harbor/harbor.sh is" \
                     "executable (dockerized)" >&2
                exit 1
            fi
            ;;
        *)
            # Treat as an explicit path.
            [[ -x "$mode" ]] || {
                echo "[bench] FATAL: HARNESS_BENCH_HARBOR=$mode is not an" \
                     "executable path." >&2
                exit 1
            }
            HARBOR_BIN="$mode"
            ;;
    esac
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

# --- Docker socket sanity ----------------------------------------------------
#
# Harbor's per-task container needs host docker access to spawn the
# harness compose stack. This is a HARD requirement; print a clear
# diagnostic if the socket is missing or unreadable.
bench_check_docker_socket() {
    if [[ ! -S /var/run/docker.sock ]]; then
        echo "[bench] WARNING: /var/run/docker.sock not present on host." >&2
        echo "[bench] Harbor task containers cannot spawn harness compose." >&2
        return 1
    fi
    if [[ ! -r /var/run/docker.sock ]]; then
        echo "[bench] WARNING: /var/run/docker.sock not readable by" \
             "$(id -un). Add user to the 'docker' group." >&2
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

# --- Scheme env injection ----------------------------------------------------
#
# Read a JSON scheme file from tests/benchmarks/schemes/<name>.json and
# export each key in its "env" object into the current environment.
# Requires python3 (always present on Oracle Linux).
bench_apply_scheme() {
    local scheme="${1:?scheme name required}"
    local scheme_file="${BENCH_ROOT}/schemes/${scheme}.json"
    if [[ ! -f "$scheme_file" ]]; then
        echo "[bench] FATAL: scheme file not found: $scheme_file" >&2
        exit 1
    fi
    # Emit KEY=VALUE lines and source them. python3 handles JSON for us.
    local kv
    kv="$(python3 -c "
import json, shlex, sys
data = json.load(open('${scheme_file}'))
for k, v in (data.get('env') or {}).items():
    print(f'export {k}={shlex.quote(str(v))}')
")"
    eval "$kv"
    echo "[bench] Applied scheme: ${scheme}" >&2
}

# Provide BENCH_ROOT and REPO_ROOT to callers without exporting.
export BENCH_ROOT REPO_ROOT
