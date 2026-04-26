# shellcheck shell=bash
#
# platform.sh — sourceable cross-platform helper library.
#
# Provides:
#   - OS detection (linux/macos/windows/unknown)
#   - Path resolution that works on Linux, macOS, and Git Bash on Windows
#   - Docker daemon checks + Docker Desktop auto-start
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

# === Docker checks ===

# Is the docker daemon running and accepting connections?
harness_docker_running() {
    docker info >/dev/null 2>&1
}

# Attempt to start Docker Desktop on Windows or macOS.
# Logs progress to stderr. Returns 0 if Docker becomes available within timeout, 1 otherwise.
# Args: [timeout_seconds] (default 90)
harness_start_docker_desktop() {
    local timeout="${1:-90}"
    local os
    os=$(harness_detect_os)

    case "$os" in
        windows)
            local exe="/c/Program Files/Docker/Docker/Docker Desktop.exe"
            if [[ ! -f "$exe" ]]; then
                echo "[harness] Docker Desktop not found at expected path: $exe" >&2
                echo "[harness] Please start Docker Desktop manually." >&2
                return 1
            fi
            echo "[harness] Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            "$exe" >/dev/null 2>&1 &
            ;;
        macos)
            echo "[harness] Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            if ! open -a Docker >/dev/null 2>&1; then
                echo "[harness] Failed to launch Docker Desktop. Please start it manually." >&2
                return 1
            fi
            ;;
        linux)
            echo "[harness] Docker daemon not running on Linux. Start it with one of:" >&2
            echo "[harness]   sudo systemctl start docker" >&2
            echo "[harness]   sudo service docker start" >&2
            return 1
            ;;
        *)
            echo "[harness] Unknown OS; cannot auto-start Docker. Please start it manually." >&2
            return 1
            ;;
    esac

    # Poll for daemon availability
    local elapsed=0
    while (( elapsed < timeout )); do
        if harness_docker_running; then
            echo "[harness] Docker is now running." >&2
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if (( elapsed % 10 == 0 )); then
            echo "[harness]   ...still waiting (${elapsed}s elapsed, ${timeout}s timeout)" >&2
        fi
    done

    echo "[harness] Docker did not become available within ${timeout}s." >&2
    return 1
}

# Ensure docker is running; auto-start if possible. Hard exit if not.
harness_require_docker() {
    if harness_docker_running; then
        return 0
    fi
    if harness_start_docker_desktop; then
        return 0
    fi
    echo "[harness] Docker is required but not available. Aborting." >&2
    exit 1
}

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
