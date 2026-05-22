# shellcheck shell=bash
#
# platform.sh — sourceable cross-platform helper library.
#
# Provides:
#   - OS detection (linux/macos/windows/unknown)
#   - Path resolution that works on Linux, macOS, and Git Bash on Windows
#   - Container runtime selection (docker/podman) + daemon checks +
#     Docker Desktop / Podman machine auto-start
#   - Preflight check primitives (used by both install.sh and the harness
#     script's `harness preflight` command)
#
# All functions emit logs to stderr and return 0 (success/found/passing) or
# 1 (failure/not-found/missing). Functions never write to stdout unless
# explicitly stated (path-resolving helpers echo their result to stdout).
#
# Source from a bash script with:
#   source "$install_root/scripts/lib/platform.sh"

# === OS detection ===

# Detect the OS family.
# Returns 0 always; echoes one of: linux, windows, macos, unknown
harness_detect_os() {
    case "$(uname -s)" in
        Linux*) echo "linux";;
        Darwin*) echo "macos";;
        MINGW*|MSYS*|CYGWIN*) echo "windows";;
        *) echo "unknown";;
    esac
}

# Test whether the current environment is Git Bash on Windows.
harness_is_git_bash() {
    [[ "$(harness_detect_os)" == "windows" ]]
}

# === Path resolution ===

# Resolve an absolute, canonical path. Works on Linux, macOS, Windows Git Bash.
# Args: <path>
# Echoes resolved path; returns 1 if path doesn't exist.
harness_realpath() {
    local target="$1"
    if [[ -z "$target" ]]; then
        echo "harness_realpath: missing arg" >&2
        return 1
    fi
    if command -v realpath >/dev/null 2>&1; then
        realpath "$target"
        return $?
    fi
    if command -v readlink >/dev/null 2>&1 && readlink -f "$target" >/dev/null 2>&1; then
        readlink -f "$target"
        return $?
    fi
    # Pure bash fallback
    if [[ -d "$target" ]]; then
        (cd "$target" && pwd -P)
    elif [[ -f "$target" ]]; then
        local dir base
        dir=$(dirname "$target")
        base=$(basename "$target")
        echo "$(cd "$dir" && pwd -P)/$base"
    else
        return 1
    fi
}

# Normalize a path: forward slashes, no duplicate slashes.
# Args: <path>
harness_normalize_path() {
    local p="$1"
    p="${p//\\//}"      # backslash to forward slash
    p="${p//\/\//\/}"   # collapse double slashes
    echo "$p"
}

# === Container runtime selection ===
#
# harness supports two container runtimes: Docker and Podman. Selection is:
#
#   1. If $HARNESS_CONTAINER_RUNTIME is set (to `docker` or `podman`), use it.
#   2. Else if `docker` is on PATH, use docker.
#   3. Else if `podman` is on PATH, use podman.
#   4. Else default to `docker` (downstream calls will fail with an actionable
#      "command not found" error).
#
# The result is cached in _harness_runtime_cache after the first call so the
# auto-detection runs once per shell invocation. Callers that change PATH or
# the env var partway through a run can clear the cache themselves
# (unset _harness_runtime_cache) — the harness CLI never does this.
#
# This function ECHOES the runtime name on stdout (`docker` or `podman`) and
# returns 0. It does NOT verify the daemon/socket is reachable; for that, see
# harness_runtime_running / harness_require_runtime below.
harness_container_runtime() {
    if [[ -n "${_harness_runtime_cache:-}" ]]; then
        printf '%s' "$_harness_runtime_cache"
        return 0
    fi
    local rt=""
    if [[ -n "${HARNESS_CONTAINER_RUNTIME:-}" ]]; then
        rt="$HARNESS_CONTAINER_RUNTIME"
        case "$rt" in
            docker|podman) ;;
            *)
                echo "[harness] WARN: HARNESS_CONTAINER_RUNTIME='$rt' is not 'docker' or 'podman'; falling back to auto-detect" >&2
                rt=""
                ;;
        esac
    fi
    if [[ -z "$rt" ]]; then
        if command -v docker >/dev/null 2>&1; then
            rt=docker
        elif command -v podman >/dev/null 2>&1; then
            rt=podman
        else
            # Neither is installed; default to docker so downstream errors
            # surface a clear "docker: command not found" rather than a
            # cryptic empty-arg failure.
            rt=docker
        fi
    fi
    _harness_runtime_cache="$rt"
    printf '%s' "$rt"
}

# True if the resolved runtime is podman.
harness_runtime_is_podman() {
    [[ "$(harness_container_runtime)" == "podman" ]]
}

# === Container runtime invocation wrappers ===
#
# Git Bash on Windows (MSYS) auto-translates UNIX-style paths in arguments
# when calling native Windows binaries like docker.exe. That is fine for
# host paths (/c/Users/foo becomes C:\Users\foo, which docker can mount)
# but breaks for *container-internal* arguments like `--entrypoint /bin/bash`
# (which gets mangled to C:/Program Files/Git/usr/bin/bash, a path that
# doesn't exist inside the container).
#
# harness_docker wraps the runtime call with MSYS_NO_PATHCONV=1 on Windows
# so that no path translation happens. Use this any time runtime args
# include an in-container UNIX path. On Linux/macOS this is a transparent
# passthrough.
#
# Despite the name, this wrapper routes through whichever container runtime
# harness_container_runtime resolved (docker or podman). The name is kept as
# `harness_docker` for backwards compatibility with the bulk of the codebase
# that was written before podman support; see also the alias
# `harness_runtime` further down.
#
# Host proxy env vars (HTTP_PROXY/HTTPS_PROXY) are NOT stripped here: they
# flow through to the runtime call so `docker compose build` inherits them and
# BuildKit routes base-image pulls and the image `RUN` steps through the corp
# proxy. The build runs on the host's BuildKit *before* any container/firewall
# exists, so it genuinely needs the proxy. Running containers never receive it
# because docker-compose.yml declares no proxy vars in any service
# `environment:` and compose does not copy the host env into started
# containers — see architecture/containers.md.
#
# Usage: harness_docker [runtime-args...]
# Example: harness_docker run --rm --entrypoint /bin/bash my-image -c 'echo hi'
harness_docker() {
    local rt
    rt=$(harness_container_runtime)
    if [[ "$(harness_detect_os)" == "windows" ]]; then
        MSYS_NO_PATHCONV=1 "$rt" "$@"
    else
        "$rt" "$@"
    fi
}

# Same as harness_docker but `exec`s into the runtime process instead of
# returning. Use this in the final exec sites of the harness CLI
# (run_agent_print's foreground launch, attach paths, etc.) so that
# signals and exit codes flow through cleanly. `exec harness_docker ...`
# does not work because `exec` requires an external program, not a shell
# function — calling this helper instead preserves exec semantics by
# wrapping `env MSYS_NO_PATHCONV=1 <runtime>` into the exec'd process on
# Windows.
harness_docker_exec() {
    local rt
    rt=$(harness_container_runtime)
    if [[ "$(harness_detect_os)" == "windows" ]]; then
        exec env MSYS_NO_PATHCONV=1 "$rt" "$@"
    else
        exec "$rt" "$@"
    fi
}

# Friendly aliases. Prefer these in new code; harness_docker is retained for
# the existing call sites that already use it.
harness_runtime() { harness_docker "$@"; }
harness_runtime_exec() { harness_docker_exec "$@"; }

# === Interactive TTY resolution (issue #82) ===
#
# On Windows Git Bash the two layers disagree about what "stdin is a terminal"
# means:
#   - bash's `[[ -t 0 ]]` asks MSYS — for a MinTTY window the answer is *yes*
#     (MinTTY hands bash an MSYS pty).
#   - the native docker.exe's isTerminal() asks Windows — for that same MinTTY
#     pty the answer is *no* (it's a pipe, not a real console handle).
# So harness can hand docker `-t`, and docker then refuses it with
#   "cannot attach stdin to a TTY-enabled container because stdin is not a
#    terminal"  (docker/cli cli/streams/in.go: CheckTty).
# ConPTY (modern Git-for-Windows / Windows Terminal) bridges the pty to a real
# console so `-t` works; plain MinTTY without it doesn't — which is why the
# failure is per-terminal and flaky. winpty allocates a real console for the
# whole process tree, restoring `-t`.
#
# Linux/macOS have no such split: bash and the runtime see the same fd, so the
# helpers below collapse to the historical "`-it` if stdin is a tty, else `-i`".

# Probe whether the native container runtime will accept an interactive TTY
# (`-t -i`) from the current stdin. Asks the runtime directly rather than
# trusting bash's `[[ -t 0 ]]`. The image must already exist locally so the
# throwaway run is instant (no pull). Silent; returns 0 iff `-it` is accepted.
# Args: <image>
harness_runtime_tty_ok() {
    local image="$1" rt
    [[ -n "$image" ]] || return 1
    rt=$(harness_container_runtime)
    MSYS_NO_PATHCONV=1 "$rt" run --rm -it --entrypoint true "$image" >/dev/null 2>&1
}

# True if winpty is on PATH (ships with Git for Windows).
harness_winpty_available() {
    command -v winpty >/dev/null 2>&1
}

# Like harness_docker, but routes the runtime call through winpty so a MinTTY
# pty is bridged to a real Windows console for an interactive `-it` launch.
# Only meaningful on Windows; callers reach it via the 'it-winpty' strategy
# from harness_resolve_interactive_tty.
harness_docker_winpty() {
    local rt
    rt=$(harness_container_runtime)
    MSYS_NO_PATHCONV=1 winpty "$rt" "$@"
}

# Pure decision: pick the interactive TTY strategy from explicit facts. No I/O,
# so it is directly unit-testable; the live wiring is harness_resolve_interactive_tty.
# Args: <os> <stdin_is_tty:0|1> <runtime_tty_ok:0|1> <winpty_present:0|1>
# Echoes one of: it | it-winpty | i
harness_tty_strategy() {
    local os="$1" stdin_tty="$2" rt_ok="$3" winpty="$4"
    # Non-Windows: bash and the runtime share the same fd, so bash's view wins.
    if [[ "$os" != "windows" ]]; then
        [[ "$stdin_tty" == 1 ]] && { echo it; return 0; }
        echo i; return 0
    fi
    # Windows: if bash itself has no tty (genuinely piped/redirected stdin),
    # `-t` is hopeless and winpty can't conjure a terminal — degrade silently.
    if [[ "$stdin_tty" != 1 ]]; then
        echo i; return 0
    fi
    # docker.exe accepts `-t` directly (ConPTY / Windows Terminal): use as-is.
    if [[ "$rt_ok" == 1 ]]; then
        echo it; return 0
    fi
    # docker.exe refuses `-t` (MinTTY pty without ConPTY): winpty bridges it.
    if [[ "$winpty" == 1 ]]; then
        echo it-winpty; return 0
    fi
    # No winpty: degrade to `-i` so the launch still works (TUI may render
    # degraded). The caller surfaces a note.
    echo i; return 0
}

# One-line, actionable note printed to stderr when an interactive Windows
# launch degrades to `-i` because docker rejected `-t` and winpty is absent.
harness_tty_degraded_note() {
    echo "[harness] stdin isn't a Windows console; running the TUI without a TTY (may render degraded). For a full TTY use Windows Terminal, or install winpty and re-run." >&2
}

# Resolve the interactive TTY strategy for a launch, running the live probes.
# Echoes the strategy token (it | it-winpty | i) on stdout; on Windows, prints
# the degraded-mode note to stderr when falling back to `-i` from a real tty.
# Set HARNESS_NO_WINPTY=1 to opt out of winpty wrapping (treats it as absent).
# Args: <image>
harness_resolve_interactive_tty() {
    local image="$1"
    local os stdin_tty=0 rt_ok=0 winpty=0
    os=$(harness_detect_os)
    [[ -t 0 ]] && stdin_tty=1
    if [[ "$os" == "windows" && "$stdin_tty" == 1 ]]; then
        if harness_runtime_tty_ok "$image"; then
            rt_ok=1
        elif [[ -z "${HARNESS_NO_WINPTY:-}" ]] && harness_winpty_available; then
            winpty=1
        fi
    fi
    local strategy
    strategy=$(harness_tty_strategy "$os" "$stdin_tty" "$rt_ok" "$winpty")
    if [[ "$os" == "windows" && "$strategy" == "i" && "$stdin_tty" == 1 ]]; then
        harness_tty_degraded_note
    fi
    printf '%s' "$strategy"
}

# Mirror the upper/lower-case spellings of the proxy env vars so git's libcurl
# picks them up regardless of which it checks (libcurl special-cases lowercase
# `http_proxy`; HTTPS honors either case). Fills a missing side from a present
# one; never overwrites an explicit value. Called by harness after .env load.
harness_normalize_proxy_env() {
    [[ -n "${HTTP_PROXY:-}"  && -z "${http_proxy:-}"  ]] && export http_proxy="$HTTP_PROXY"
    [[ -n "${http_proxy:-}"  && -z "${HTTP_PROXY:-}"  ]] && export HTTP_PROXY="$http_proxy"
    [[ -n "${HTTPS_PROXY:-}" && -z "${https_proxy:-}" ]] && export https_proxy="$HTTPS_PROXY"
    [[ -n "${https_proxy:-}" && -z "${HTTPS_PROXY:-}" ]] && export HTTPS_PROXY="$https_proxy"
    return 0
}

# Convert a host path to a form Docker Desktop accepts reliably for bind
# mounts. On Linux/macOS this is a passthrough.
#
# On Windows: Docker Desktop's WSL2 backend handles `C:/Users/...` and
# `/c/Users/...` for paths under the user profile, but `/tmp/...` (a
# Git Bash MSYS-only mount that points at C:\Users\<u>\AppData\Local\Temp)
# is not visible from outside Git Bash and bind mounts of those paths
# silently produce empty mounts. cygpath -m resolves any of those to the
# canonical mixed (forward-slash) Windows form.
#
# Args: <path>
# Echoes the converted path on stdout.
harness_docker_path() {
    local p="$1"
    if [[ "$(harness_detect_os)" != "windows" ]]; then
        echo "$p"
        return 0
    fi
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$p"
        return $?
    fi
    # Fallback when cygpath is unavailable: normalize /c/Users/... → C:/Users/...
    local abs
    abs=$(harness_realpath "$p" 2>/dev/null || echo "$p")
    if [[ "$abs" =~ ^/([a-zA-Z])/(.*)$ ]]; then
        local drive="${BASH_REMATCH[1]^^}"
        local rest="${BASH_REMATCH[2]}"
        echo "${drive}:/${rest}"
    else
        echo "$abs"
    fi
}

# Resolve a (possibly relative or unresolved) host path to an absolute,
# canonical path in the form used INSIDE the agent container as a
# bind-mount target. The whole point is path parity: `pwd` inside the
# agent equals the host path the user typed.
#
#   Linux/macOS: /home/me/proj      → /home/me/proj
#   Windows:     C:\Users\you\proj  → /c/Users/you/proj   (MSYS unix form)
#                /c/Users/you/proj  → /c/Users/you/proj
#
# Mirrors harness_docker_path's job for the *source* side of `-v`, but
# normalizes for the *target* side (always Linux-style — the container
# is Linux). Args: <path>. Returns 1 if the path doesn't exist.
harness_abs_path() {
    local target="$1"
    if [[ -z "$target" ]]; then
        echo "harness_abs_path: missing arg" >&2
        return 1
    fi
    local abs
    abs=$(harness_realpath "$target") || return 1
    if [[ "$(harness_detect_os)" == "windows" ]] && command -v cygpath >/dev/null 2>&1; then
        # cygpath -u produces /c/Users/... regardless of input shape
        # (C:\Users\..., C:/Users/..., /c/Users/...). MSYS unix form is
        # the variant the user sees in Git Bash, so it's also what they
        # expect to see inside the container.
        abs=$(cygpath -u "$abs")
    fi
    echo "$abs"
}

# Validate a user-supplied --mount path. The path must resolve (relative
# paths are accepted), must exist as a directory, and must not shadow
# in-container infrastructure the harness depends on (/etc, /usr, the
# harness home, etc.). On success, echoes the resolved absolute path
# (same form harness_abs_path returns) on stdout. On failure, prints a
# specific error to stderr and returns 1.
#
# Args: <path>
harness_validate_mount() {
    local raw="$1"
    if [[ -z "$raw" ]]; then
        echo "harness_validate_mount: --mount: missing path" >&2
        return 1
    fi
    if [[ ! -e "$raw" ]]; then
        echo "harness_validate_mount: --mount: path does not exist on host: $raw" >&2
        return 1
    fi
    if [[ ! -d "$raw" ]]; then
        echo "harness_validate_mount: --mount: path is not a directory: $raw" >&2
        return 1
    fi
    local abs
    abs=$(harness_abs_path "$raw") || {
        echo "harness_validate_mount: --mount: cannot resolve absolute path: $raw" >&2
        return 1
    }
    # Refuse paths whose container-side target would shadow infrastructure
    # the agent image relies on (the harness user's home is bind-mounted
    # separately; the firewall allowlist is bind-mounted at /etc/harness;
    # / and the system dirs are obviously off-limits).
    # shellcheck disable=SC2221,SC2222
    # (Patterns are disjoint: `/etc` is the literal /etc, `/etc/*` is
    #  anything under it. shellcheck flags them as overlapping but they're
    #  not — verified by the integration test in tests/integration_test.sh
    #  Phase 5.3.)
    case "$abs" in
        /|/etc|/etc/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/var|/var/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/root|/root/*|/home/harness|/home/harness/*|/etc/harness|/etc/harness/*|/workspace|/workspace/*)
            echo "harness_validate_mount: --mount: refusing '$abs' — would shadow container infrastructure" >&2
            return 1
            ;;
    esac
    echo "$abs"
}

# === Container runtime checks ===

# Is the resolved container runtime daemon/socket reachable?
#
# For docker: `docker info` confirms the daemon socket is up.
# For rootless podman: `podman info` works without a daemon (it talks to its
# own user-mode socket / runs forks); the same probe still returns 0/non-zero
# correctly. For rootful podman it also works.
harness_docker_running() {
    harness_docker info >/dev/null 2>&1
}

# Friendly alias. Same semantics as harness_docker_running.
harness_runtime_running() { harness_docker_running; }

# Attempt to start the local container runtime if it isn't already.
# Logs progress to stderr. Returns 0 if the runtime becomes available within
# the timeout, 1 otherwise.
#
# Behavior by runtime + OS:
#   - docker on windows/macos: starts Docker Desktop, polls until reachable.
#   - docker on linux:         prints actionable systemctl/service hints; does
#                              not attempt to start the system daemon (would
#                              need sudo). Returns 1.
#   - podman on macos/windows: tries `podman machine start`. Returns 0/1.
#   - podman on linux:         rootless podman doesn't need a started daemon
#                              — `podman info` is enough on its own. Prints a
#                              hint about `systemctl --user start podman.socket`
#                              for users who actually want the API socket
#                              (compose mostly uses it). Returns 1 since we
#                              didn't actually do anything.
#
# Args: [timeout_seconds] (default 90)
harness_start_docker_desktop() {
    local timeout="${1:-90}"
    local os rt
    os=$(harness_detect_os)
    rt=$(harness_container_runtime)

    case "$rt:$os" in
        docker:windows)
            local exe="/c/Program Files/Docker/Docker/Docker Desktop.exe"
            if [[ ! -f "$exe" ]]; then
                echo "[harness] Docker Desktop not found at expected path: $exe" >&2
                echo "[harness] Please start Docker Desktop manually." >&2
                return 1
            fi
            echo "[harness] Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            "$exe" >/dev/null 2>&1 &
            ;;
        docker:macos)
            echo "[harness] Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            if ! open -a Docker >/dev/null 2>&1; then
                echo "[harness] Failed to launch Docker Desktop. Please start it manually." >&2
                return 1
            fi
            ;;
        docker:linux)
            echo "[harness] Docker daemon not running on Linux. Start it with one of:" >&2
            echo "[harness]   sudo systemctl start docker" >&2
            echo "[harness]   sudo service docker start" >&2
            return 1
            ;;
        podman:windows|podman:macos)
            if ! command -v podman >/dev/null 2>&1; then
                echo "[harness] podman not found on PATH; cannot auto-start." >&2
                return 1
            fi
            echo "[harness] Podman machine is not running. Starting it now..." >&2
            if ! podman machine start >/dev/null 2>&1; then
                echo "[harness] 'podman machine start' failed. Run it manually:" >&2
                echo "[harness]   podman machine init   # if no machine yet" >&2
                echo "[harness]   podman machine start" >&2
                return 1
            fi
            ;;
        podman:linux)
            # Rootless podman doesn't have a long-running daemon — `podman info`
            # spawns a short-lived helper as needed. If we got here, the
            # initial probe failed for some other reason (PATH, permissions,
            # broken install). Don't try to start anything; print the hint and
            # let the caller surface the failure.
            echo "[harness] podman info failed. Common causes on Linux:" >&2
            echo "[harness]   - rootless not configured: see https://github.com/containers/podman/blob/main/docs/tutorials/rootless_tutorial.md" >&2
            echo "[harness]   - subuid/subgid not set for your user:    cat /etc/subuid /etc/subgid" >&2
            echo "[harness]   - if you want podman's REST API socket:   systemctl --user start podman.socket" >&2
            return 1
            ;;
        *)
            echo "[harness] Unknown runtime/OS combination ($rt/$os); cannot auto-start. Please start it manually." >&2
            return 1
            ;;
    esac

    # Poll for runtime availability. Track elapsed against wall clock — each
    # `harness_docker_running` (i.e. `docker info`) can block for many seconds
    # while the daemon is still booting, so counting `sleep 2` ticks
    # under-reports elapsed time and also lets the loop overrun `timeout`.
    local start_ts elapsed=0 last_log_bucket=0 bucket
    start_ts=$(date +%s)
    while (( elapsed < timeout )); do
        if harness_docker_running; then
            echo "[harness] $rt is now running." >&2
            return 0
        fi
        sleep 2
        elapsed=$(( $(date +%s) - start_ts ))
        bucket=$(( elapsed / 10 ))
        if (( bucket > last_log_bucket )); then
            echo "[harness]   ...still waiting (${elapsed}s elapsed, ${timeout}s timeout)" >&2
            last_log_bucket=$bucket
        fi
    done

    echo "[harness] $rt did not become available within ${timeout}s." >&2
    return 1
}

# Friendly alias for the runtime-agnostic name.
harness_start_runtime() { harness_start_docker_desktop "$@"; }

# Ensure the container runtime is running; auto-start if possible. Hard exit
# if not. Kept named harness_require_docker for backwards compatibility.
harness_require_docker() {
    if harness_docker_running; then
        return 0
    fi
    if harness_start_docker_desktop; then
        return 0
    fi
    local rt
    rt=$(harness_container_runtime)
    echo "[harness] $rt is required but not available. Aborting." >&2
    exit 1
}

# Friendly alias.
harness_require_runtime() { harness_require_docker; }

# === Preflight check primitives (used by both install.sh and harness) ===

# Check that a command exists in PATH.
# Args: <command_name> <human_friendly_description>
# Echoes pass/fail line to stderr. Returns 0/1.
harness_check_command() {
    local cmd="$1"
    local desc="${2:-$cmd}"
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✓ $desc" >&2
        return 0
    else
        echo "  ✗ $desc — '$cmd' not found in PATH" >&2
        return 1
    fi
}

# Check that an env var is set and non-empty.
# Args: <var_name> <required:true|false> [description]
# Returns 0 if OK or optional-and-empty, 1 if required-and-empty.
harness_check_env_var() {
    local var="$1"
    local required="${2:-true}"
    local desc="${3:-}"
    local value="${!var:-}"

    if [[ -n "$value" ]]; then
        echo "  ✓ $var is set" >&2
        return 0
    fi

    if [[ "$required" == "true" ]]; then
        echo "  ✗ $var is required but not set${desc:+ — $desc}" >&2
        return 1
    fi

    echo "  ⚠ $var is optional, not set${desc:+ ($desc)}" >&2
    return 0
}

# Check that a file exists. Optionally check it's readable.
# Args: <path> <required:true|false> [description]
harness_check_file_exists() {
    local path="$1"
    local required="${2:-true}"
    local desc="${3:-$path}"

    if [[ -f "$path" ]]; then
        if [[ -r "$path" ]]; then
            echo "  ✓ $desc" >&2
            return 0
        fi
        echo "  ✗ $desc exists but is not readable" >&2
        return 1
    fi

    if [[ "$required" == "true" ]]; then
        echo "  ✗ $desc not found at $path" >&2
        return 1
    fi

    echo "  ⚠ $desc not present at $path (optional)" >&2
    return 0
}

# Check that a directory exists and is writable.
# Args: <path> <required:true|false> [description]
harness_check_dir_writable() {
    local path="$1"
    local required="${2:-true}"
    local desc="${3:-$path}"

    if [[ -d "$path" ]]; then
        if [[ -w "$path" ]]; then
            echo "  ✓ $desc" >&2
            return 0
        fi
        echo "  ✗ $desc exists but is not writable" >&2
        return 1
    fi

    if [[ "$required" == "true" ]]; then
        echo "  ✗ $desc not found at $path" >&2
        return 1
    fi

    echo "  ⚠ $desc not present at $path (optional)" >&2
    return 0
}

# Check available disk space in MB at a given path.
# Args: <path> <required_mb> [description]
harness_check_disk_space() {
    local path="$1"
    local required_mb="$2"
    local desc="${3:-disk space at $path}"

    local available_kb
    available_kb=$(df -k "$path" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ -z "$available_kb" ]]; then
        echo "  ⚠ $desc — could not determine available space" >&2
        return 0   # Don't fail on inability to check
    fi

    local available_mb=$(( available_kb / 1024 ))
    if (( available_mb >= required_mb )); then
        echo "  ✓ $desc (${available_mb}M available, ${required_mb}M required)" >&2
        return 0
    fi
    echo "  ✗ $desc — only ${available_mb}M available, ${required_mb}M required" >&2
    return 1
}
