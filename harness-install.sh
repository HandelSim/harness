#!/usr/bin/env bash
#
# harness installer.
#
# Run from the directory in which you want the install to live. The script
# clones the harness repo as ./harness/ — and that directory IS the install
# root. Code, user config (.env, .harness-allowlist), and runtime state
# (state/) all live inside the clone; runtime state is gitignored.
#
# Layout produced:
#   <cwd>/harness/                        the install root (also the git clone)
#     .git/                                managed by 'harness update'
#     harness-install.sh, harness, docker-compose.yml, ...   (code; tracked)
#     .env                                 your config (gitignored)
#     .harness-allowlist                   egress allowlist (gitignored)
#     state/                               runtime state (gitignored)
#       output/                            proxy debug dumps
#       agent/home/                        shared agent /home/harness
#                                          (opencode, shell)
#       mcp/<name>/                        active MCP services
#
# To uninstall later:
#   rm -rf <install-root>
#   rm ~/.local/bin/harness

# Detect whether we were sourced (so the PATH update inside this script
# takes effect in the caller's shell) vs executed as a subprocess. Behavior
# differs:
#   - sourced:  do NOT enable `set -e`; it would terminate the user's
#               interactive shell on any non-zero command in the rest of
#               the script. Use `return` to leave the script.
#   - executed: enable strict mode for installer safety; use `exit` normally.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    HARNESS_INSTALL_SOURCED=1
else
    HARNESS_INSTALL_SOURCED=0
    set -euo pipefail
fi

# Helper: exit if executed, return if sourced. Without this, `source
# harness-install.sh` (which the README recommends so PATH updates land in
# the parent shell) would kill the calling shell on the first `exit`.
#
# IMPORTANT — only safe to abort the script from the TOP LEVEL. When sourced
# this does `return "$code"`, which terminates the script ONLY if exit_or_return
# is called at the script's top level (the final line below) — i.e. the
# `return` it runs is itself one stack frame deep, so it pops back into the
# sourced script. Calling it from a NESTED helper (e.g. an old `fail` that ran
# `exit_or_return`) just pops back into that helper and the script keeps going.
# That is the bug that bit #105 and #106. For fatal aborts MID-script, use the
# fatal-abort idiom below instead, which runs its `return` at the top level.
exit_or_return() {
    local code="${1:-0}"
    if (( HARNESS_INSTALL_SOURCED )); then
        return "$code"
    else
        exit "$code"
    fi
}

# Fatal-abort idiom. Because the terminating `return` MUST run at the script's
# top level (it can't be hidden inside a helper — see above), every fatal site
# inlines these two lines rather than calling a shared function:
#
#     if <bad condition>; then
#         fail "message"
#         (( HARNESS_INSTALL_SOURCED )) && return 1   # ends a sourced run here
#         exit 1                                       # ends an executed run
#     fi
#
# Keep this pattern verbatim at each fatal site; do not refactor it into a
# function (that reintroduces the #105/#106 "kept going after a fatal" bug).

# The default points at the public GitHub remote. tests/full_pipeline_test.sh
# overrides this via HARNESS_REPO_URL=<local-path> so the pipeline test can
# clone the working tree under test without needing a network round-trip.
# `git clone` accepts a local directory as a URL, so any path on disk works.
REPO_URL="${HARNESS_REPO_URL:-https://github.com/HandelSim/harness}"
CLONE_DIR="harness"
PROGRAM_NAME="harness"
LOCAL_BIN="$HOME/.local/bin"
BRANCH=""
MODEL_MENU=0
CONN_CHECK=0
UNINSTALL=0

# --- argument parsing -------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--branch)
            [[ -z "${2:-}" ]] && { fail "-b requires a branch name"; (( HARNESS_INSTALL_SOURCED )) && return 1; exit 1; }
            BRANCH="$2"
            shift 2
            ;;
        -m|--model-menu)
            MODEL_MENU=1
            shift
            ;;
        -c|--check)
            CONN_CHECK=1
            shift
            ;;
        -u|--uninstall)
            UNINSTALL=1
            shift
            ;;
        *)
            fail "unknown option: $1"
            (( HARNESS_INSTALL_SOURCED )) && return 1
            exit 1
            ;;
    esac
done

# --- ANSI colors ------------------------------------------------------------
# Disabled if stdout is not a tty.

if [[ -t 1 ]]; then
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

ok()    { printf '%s✓%s %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s!%s %s\n'  "$C_YELLOW" "$C_RESET" "$*"; }
# fail() ONLY prints an error to stderr — it deliberately does NOT abort and
# returns success. Two reasons: (1) a `return` inside a helper can't terminate a
# sourced script (it just leaves the helper), so the abort must happen at the
# script's top level; (2) if fail returned non-zero, `set -e` (on in executed
# mode) would exit on the fail line itself, swallowing any follow-up hint lines.
# So every fatal site prints with fail/echo, then aborts explicitly with the
# top-level idiom: `(( HARNESS_INSTALL_SOURCED )) && return 1; exit 1`.
fail()  { printf '%sx%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }
title() { printf '%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

cwd=$(pwd)
install_root="$cwd/$CLONE_DIR"

# Resolve the directory that harness-install.sh itself lives in. This is
# where a distributor can drop a pre-edited .env and .harness-allowlist
# beside the installer so the user gets them copied into the install root
# automatically (one folder to ship, fewer post-install steps). Prefer the
# script's own location over $cwd so "beside the script" works even when the
# installer is run from a different directory. Falls back to $cwd when
# BASH_SOURCE can't be resolved (e.g. piped from curl), which matches the
# pre-existing $cwd-based behavior.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || script_dir="$cwd"
[[ -n "$script_dir" ]] || script_dir="$cwd"

# --- inline platform fallbacks (pre-clone) ----------------------------------
#
# install.sh runs BEFORE the clone, so scripts/lib/platform.sh from the
# repo isn't yet available. We inline the minimum subset of helpers needed
# in the early phases (OS detection, container runtime check, runtime
# auto-start). After the clone we source the full library for the rest of
# the script.

_inline_detect_os() {
    case "$(uname -s)" in
        Linux*) echo "linux";;
        Darwin*) echo "macos";;
        MINGW*|MSYS*|CYGWIN*) echo "windows";;
        *) echo "unknown";;
    esac
}

# Resolve the container runtime once. Honors $HARNESS_CONTAINER_RUNTIME if
# set; otherwise auto-detects: docker first, then podman. The result is
# cached in _inline_runtime_cache after the first call.
_inline_container_runtime() {
    if [[ -n "${_inline_runtime_cache:-}" ]]; then
        printf '%s' "$_inline_runtime_cache"
        return 0
    fi
    local rt=""
    if [[ -n "${HARNESS_CONTAINER_RUNTIME:-}" ]]; then
        case "$HARNESS_CONTAINER_RUNTIME" in
            docker|podman) rt="$HARNESS_CONTAINER_RUNTIME" ;;
            *) rt="" ;;
        esac
    fi
    if [[ -z "$rt" ]]; then
        if command -v docker >/dev/null 2>&1; then
            rt=docker
        elif command -v podman >/dev/null 2>&1; then
            rt=podman
        else
            rt=docker
        fi
    fi
    _inline_runtime_cache="$rt"
    printf '%s' "$rt"
}

_inline_docker_running() { "$(_inline_container_runtime)" info >/dev/null 2>&1; }

_inline_start_docker() {
    local timeout=90
    local os rt
    os=$(_inline_detect_os)
    rt=$(_inline_container_runtime)

    case "$rt:$os" in
        docker:windows)
            local exe="/c/Program Files/Docker/Docker/Docker Desktop.exe"
            if [[ ! -f "$exe" ]]; then
                echo "  Docker Desktop not found at expected path: $exe" >&2
                echo "  Please start Docker Desktop manually." >&2
                return 1
            fi
            echo "  Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            "$exe" >/dev/null 2>&1 &
            ;;
        docker:macos)
            echo "  Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            if ! open -a Docker >/dev/null 2>&1; then
                echo "  Failed to launch Docker Desktop. Please start it manually." >&2
                return 1
            fi
            ;;
        docker:linux)
            echo "  Docker daemon not running on Linux. Start it with one of:" >&2
            echo "    sudo systemctl start docker" >&2
            echo "    sudo service docker start" >&2
            return 1
            ;;
        podman:windows|podman:macos)
            echo "  Podman machine not running. Starting it now..." >&2
            if ! podman machine start >/dev/null 2>&1; then
                echo "  'podman machine start' failed. Run it manually:" >&2
                echo "    podman machine init   # if no machine yet" >&2
                echo "    podman machine start" >&2
                return 1
            fi
            ;;
        podman:linux)
            echo "  podman info failed. Common causes on Linux:" >&2
            echo "    - rootless not configured: see https://github.com/containers/podman/blob/main/docs/tutorials/rootless_tutorial.md" >&2
            echo "    - subuid/subgid not set:    cat /etc/subuid /etc/subgid" >&2
            echo "    - if you want podman's REST API socket: systemctl --user start podman.socket" >&2
            return 1
            ;;
        *)
            echo "  Unknown runtime/OS combination ($rt/$os); cannot auto-start. Please start it manually." >&2
            return 1
            ;;
    esac

    # Track wall-clock elapsed; `docker info` blocks while the daemon boots,
    # so counting `sleep 2` ticks under-reports time and also overruns the
    # caller's timeout. Mirrors scripts/lib/platform.sh:harness_start_docker_desktop.
    local start_ts elapsed=0 last_log_bucket=0 bucket
    start_ts=$(date +%s)
    while (( elapsed < timeout )); do
        if _inline_docker_running; then
            echo "  $rt is now running." >&2
            return 0
        fi
        sleep 2
        elapsed=$(( $(date +%s) - start_ts ))
        bucket=$(( elapsed / 10 ))
        if (( bucket > last_log_bucket )); then
            echo "    ...still waiting (${elapsed}s elapsed, ${timeout}s timeout)" >&2
            last_log_bucket=$bucket
        fi
    done

    echo "  $rt did not become available within ${timeout}s." >&2
    return 1
}

_inline_check_command() {
    local cmd="$1" desc="${2:-$1}"
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✓ $desc"
        return 0
    fi
    echo "  ✗ $desc — '$cmd' not found in PATH"
    return 1
}

# Variant: pass if EITHER of two commands is found. Used for the
# docker-or-podman preflight gate.
_inline_check_either_command() {
    local cmd_a="$1" cmd_b="$2" desc="$3"
    if command -v "$cmd_a" >/dev/null 2>&1; then
        echo "  ✓ $desc (using $cmd_a)"
        return 0
    fi
    if command -v "$cmd_b" >/dev/null 2>&1; then
        echo "  ✓ $desc (using $cmd_b)"
        return 0
    fi
    echo "  ✗ $desc — neither '$cmd_a' nor '$cmd_b' found in PATH"
    return 1
}

# --- model menu helper ------------------------------------------------------
#
# Shared by the -m / --model-menu standalone mode and the "configuring default
# model" step during a normal install. Reads PROXY_API_URL and PROXY_API_KEY
# from $install_root/.env, queries GET /v1/models, and writes the chosen model
# to DEFAULT_MODEL_NAME. Pressing Enter without a value skips the write.

_run_model_menu() {
    local env_file="$install_root/.env"
    echo "  reading $env_file"
    local api_url api_key cur_model
    api_url=$(grep  -E '^PROXY_API_URL='       "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
    api_key=$(grep  -E '^PROXY_API_KEY='       "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
    cur_model=$(grep -E '^DEFAULT_MODEL_NAME=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')

    local -a model_list=()

    if [[ -n "$api_url" && -n "$api_key" ]] && command -v curl >/dev/null 2>&1; then
        local models_url="${api_url%/}/v1/models"
        echo "  querying ${models_url}..."
        local response
        response=$(curl -sf --max-time 10 \
            -H "Authorization: Bearer ${api_key}" \
            -H "Content-Type: application/json" \
            "$models_url" 2>/dev/null) || response=""

        if [[ -n "$response" ]]; then
            local m
            if command -v jq >/dev/null 2>&1; then
                while IFS= read -r m; do
                    if [[ -n "$m" ]]; then model_list+=("$m"); fi
                done < <(printf '%s' "$response" | jq -r '.data[].id' 2>/dev/null)
            else
                # Portable grep+sed fallback when jq is absent
                while IFS= read -r m; do
                    if [[ -n "$m" ]]; then model_list+=("$m"); fi
                done < <(printf '%s' "$response" \
                    | grep -o '"id":"[^"]*"' \
                    | sed 's/^"id":"//;s/"$//')
            fi
        fi
    fi

    local selected_model=""
    if (( ${#model_list[@]} > 0 )); then
        echo "  Available models:"
        local i=1 m
        for m in "${model_list[@]}"; do
            printf '    %2d) %s\n' "$i" "$m"
            i=$((i + 1))
        done
        if [[ -n "$cur_model" ]]; then echo "  (current: $cur_model)"; fi
        echo
        local choice
        while true; do
            read -rp "  select a model (1-${#model_list[@]}) or type a name [Enter to keep current]: " choice
            if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#model_list[@]} )); then
                selected_model="${model_list[$((choice - 1))]}"
                break
            elif [[ -n "$choice" ]]; then
                selected_model="$choice"
                break
            else
                break  # empty input → skip
            fi
        done
    else
        if [[ -z "$api_url" || -z "$api_key" ]]; then
            warn "cannot fetch model list — missing value(s) in $env_file:"
            [[ -z "$api_url" ]] && echo "  PROXY_API_URL is blank — edit $env_file and set it" >&2
            [[ -z "$api_key" ]] && echo "  PROXY_API_KEY is blank — edit $env_file and set it" >&2
        elif ! command -v curl >/dev/null 2>&1; then
            warn "curl not found; cannot fetch model list"
        else
            warn "could not fetch models from ${api_url%/}/v1/models"
        fi
        if [[ -n "$cur_model" ]]; then echo "  (current: $cur_model)"; fi
        read -rp "  enter DEFAULT_MODEL_NAME (e.g. gpt-4o) or press Enter to keep current: " selected_model
    fi

    if [[ -n "$selected_model" ]]; then
        local model_tmp="$env_file.tmp.$$"
        local key_written=0 line
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^[[:space:]]*DEFAULT_MODEL_NAME= ]]; then
                printf 'DEFAULT_MODEL_NAME=%s\n' "$selected_model"
                key_written=1
            else
                printf '%s\n' "$line"
            fi
        done <"$env_file" >"$model_tmp"
        if (( ! key_written )); then
            printf 'DEFAULT_MODEL_NAME=%s\n' "$selected_model" >>"$model_tmp"
        fi
        mv -f "$model_tmp" "$env_file"
        ok "set DEFAULT_MODEL_NAME=$selected_model in $env_file"
    else
        if [[ -z "$cur_model" ]]; then
            warn "DEFAULT_MODEL_NAME not set; edit $env_file before running harness"
        else
            ok "DEFAULT_MODEL_NAME unchanged ($cur_model)"
        fi
    fi
}

# --- connectivity check helper ----------------------------------------------
#
# Used by -c / --check mode. Runs from the HOST (not inside the container) to
# validate credentials and URL format before debugging the container stack.
# Tests:
#   1. Allowlist — is the API hostname present?
#   2. HTTPS with SSL verification — reports HTTP status or curl error.
#   3. HTTPS without SSL (-k) — if step 2 timed out/errored, distinguishes a
#      network failure from an untrusted certificate (very common on .mil/.gov
#      domains with internal CAs).
#   4. Model list — shows available models and validates DEFAULT_MODEL_NAME.

_run_conn_check() {
    local env_file="$install_root/.env"
    echo "  .env: $env_file"

    local api_url api_key model
    api_url=$(grep -E '^PROXY_API_URL='       "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
    api_key=$(grep -E '^PROXY_API_KEY='       "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')
    model=$(grep   -E '^DEFAULT_MODEL_NAME='  "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r')

    echo
    printf '  %-22s %s\n' "PROXY_API_URL:"      "${api_url:-(not set)}"
    printf '  %-22s %s\n' "PROXY_API_KEY:"      "${api_key:+(set, ${#api_key} chars)}${api_key:-(not set)}"
    printf '  %-22s %s\n' "DEFAULT_MODEL_NAME:" "${model:-(not set)}"
    echo

    local abort=0
    [[ -z "$api_url" ]] && { fail "PROXY_API_URL is blank — edit $env_file"; abort=1; }
    [[ -z "$api_key" ]] && { fail "PROXY_API_KEY is blank — edit $env_file"; abort=1; }
    if (( abort )); then return 1; fi

    # --- 1. allowlist -------------------------------------------------------
    local api_host
    api_host=$(printf '%s' "$api_url" \
        | sed 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||;s|[/?#].*||;s|:.*||')
    local allowlist="$install_root/.harness-allowlist"
    if [[ -f "$allowlist" ]]; then
        if grep -qxF "$api_host" "$allowlist" 2>/dev/null; then
            ok "$api_host is in .harness-allowlist"
        else
            warn "$api_host is NOT in .harness-allowlist — container egress will be blocked"
            echo "  Fix: printf '%s\\n' \"$api_host\" >> \"$allowlist\"" >&2
        fi
    else
        warn ".harness-allowlist not found at $allowlist"
    fi
    echo

    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not found; skipping HTTP tests"
        return 0
    fi

    # --- 2. HTTPS with SSL verification -------------------------------------
    local models_url="${api_url%/}/v1/models"
    echo "  [1/2] GET $models_url  (SSL on)"
    local http_code body curl_stderr_file
    curl_stderr_file=$(mktemp /tmp/harness_curl_XXXXXX 2>/dev/null || echo "/tmp/harness_curl_$$")
    body=$(curl -sS --max-time 15 \
        -H "Authorization: Bearer ${api_key}" \
        -H "Content-Type: application/json" \
        -w '\n__STATUS__%{http_code}' \
        "$models_url" 2>"$curl_stderr_file") || true
    http_code=$(printf '%s' "$body" | grep '__STATUS__' | sed 's/.*__STATUS__//')
    body=$(printf '%s' "$body" | grep -v '__STATUS__')
    local curl_err
    curl_err=$(cat "$curl_stderr_file" 2>/dev/null); rm -f "$curl_stderr_file"

    case "${http_code:-000}" in
        200)
            ok "HTTP 200 — connected (SSL verified, credentials accepted)"
            ;;
        000)
            fail "no response — connection failed or SSL certificate rejected"
            [[ -n "$curl_err" ]] && echo "  detail: $curl_err" >&2
            echo
            echo "  [1b/2] retrying without SSL certificate verification (-k)..."
            local http_nossl
            http_nossl=$(curl -sk --max-time 15 \
                -H "Authorization: Bearer ${api_key}" \
                -o /dev/null -w '%{http_code}' \
                "$models_url" 2>/dev/null) || http_nossl="000"
            if [[ "$http_nossl" == "000" ]]; then
                fail "still unreachable — check PROXY_API_URL and network/VPN access"
            else
                warn "HTTP $http_nossl reached with SSL disabled"
                warn "SSL certificate for $api_host is NOT trusted by this host"
                echo >&2
                echo "  The proxy container will have the same problem. To fix:" >&2
                echo "    1. Obtain the CA certificate bundle for $api_host" >&2
                echo "    2. Add  PROXY_API_CACERT=/path/to/ca.pem  to $env_file" >&2
                echo "    3. Mount it into the proxy container (docker-compose.override.yml)" >&2
            fi
            return 1
            ;;
        401|403)
            fail "HTTP $http_code — authentication rejected; check PROXY_API_KEY in $env_file"
            ;;
        404)
            fail "HTTP $http_code — endpoint not found: $models_url"
            echo "  PROXY_API_URL may need /v1 appended or removed. Currently: $api_url" >&2
            ;;
        *)
            warn "HTTP ${http_code} — unexpected status"
            [[ -n "$body" ]] && printf '  response: %s\n' "${body:0:300}" >&2
            ;;
    esac

    # --- 3. model list + DEFAULT_MODEL_NAME check ---------------------------
    if [[ "${http_code:-000}" == "200" ]]; then
        echo
        echo "  [2/2] available models:"
        local model_ids=""
        if command -v jq >/dev/null 2>&1; then
            model_ids=$(printf '%s' "$body" | jq -r '.data[].id' 2>/dev/null)
        else
            model_ids=$(printf '%s' "$body" \
                | grep -o '"id":"[^"]*"' \
                | sed 's/^"id":"//;s/"$//')
        fi
        if [[ -n "$model_ids" ]]; then
            printf '%s\n' "$model_ids" | sed 's/^/    /'
            echo
            if [[ -n "$model" ]]; then
                if printf '%s\n' "$model_ids" | grep -qxF "$model"; then
                    ok "DEFAULT_MODEL_NAME '$model' is valid"
                else
                    warn "DEFAULT_MODEL_NAME '$model' not found in the model list above"
                    echo "  Run: ./harness-install.sh -m  to pick a valid model" >&2
                fi
            else
                warn "DEFAULT_MODEL_NAME is not set — run: ./harness-install.sh -m"
            fi
        else
            warn "model list was empty or could not be parsed"
            [[ -n "$body" ]] && printf '  raw response: %s\n' "${body:0:300}" >&2
        fi
    fi
}

# --- preflight --------------------------------------------------------------
#
# Validates that the host can run the installer at all. Failures are listed
# up front, before any prompting, so the user can fix them in one pass
# instead of fixing-rerun-fixing.

preflight() {
    local errors=0
    echo
    title "preflight checks"

    _inline_check_command git "git" || errors=$((errors+1))

    # Container runtime is OPTIONAL. harness has a containerless 'host' mode
    # ('harness host') that needs no docker, so a missing/unreachable runtime is
    # no longer a hard failure: we install host-only and say so. Container
    # subcommands (start/opencode/shell) then tell the user to install docker or
    # use 'harness host'. HOST_ONLY (global, this function isn't called twice)
    # is read by the closing messages to tailor next-steps.
    HOST_ONLY=0
    if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
        local rt
        rt=$(_inline_container_runtime)

        if ! "$rt" compose version >/dev/null 2>&1; then
            echo "  ⚠ $rt compose — 'compose' subcommand not available"
            if [[ "$rt" == "docker" ]]; then
                echo "    (you may have docker, but container mode needs compose v2; 'harness host' still works)"
            else
                echo "    (podman 4.0+ is required for 'podman compose'; 'harness host' still works)"
            fi
        else
            echo "  ✓ $rt compose"
        fi

        # Container runtime (with auto-start attempt on Win/Mac)
        if _inline_docker_running; then
            echo "  ✓ $rt runtime"
        else
            echo "  - $rt runtime not reachable; attempting auto-start..."
            if _inline_start_docker; then
                echo "  ✓ $rt runtime (started)"
            else
                echo "  ⚠ $rt runtime not running — start it for container mode; 'harness host' works without it"
            fi
        fi
    else
        HOST_ONLY=1
        echo "  ⚠ no container runtime (docker/podman) found — installing host-only"
        echo "    'harness host' runs containerless; 'harness start/opencode/shell' will need docker"

        # Python 3 is the only host-mode dep the user must supply: it bootstraps
        # the proxy venv, so it has to exist before harness can fetch anything.
        # jq, Node, and opencode are auto-provisioned on the first 'harness host'
        # (into state/host/toolchain), so they're informational here, not blockers.
        # Resolve like harness's host_python_bin: python3, then a python that is
        # Python 3, then the 'py -3' launcher — Windows Git Bash usually has no
        # 'python3' (python.org ships 'python'/'py'; only MS Store ships python3).
        # Verify each by running --version, not just `command -v`: Windows puts an
        # App-execution-alias stub named python3.exe on PATH that reports "Python
        # was not found" instead of running, so a bare command -v picks the stub.
        if command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 | grep -q '^Python 3'; then
            echo "  ✓ python3 (host proxy runtime)"
        elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -q '^Python 3'; then
            echo "  ✓ python (Python 3; host proxy runtime)"
        elif command -v py >/dev/null 2>&1 && py -3 --version 2>&1 | grep -q '^Python 3'; then
            echo "  ✓ py -3 (host proxy runtime)"
        else
            echo "  ⚠ Python 3 not found — 'harness host' needs it (the translating proxy); install it from your OS package manager (Windows: python.org provides 'python'/'py')"
        fi
        if command -v jq >/dev/null 2>&1; then
            echo "  ✓ jq (host binary; harness will reuse it)"
        else
            echo "  · jq not found — harness fetches it automatically on first 'harness host'"
        fi
        if command -v node >/dev/null 2>&1; then
            local node_major
            node_major=$(node --version 2>/dev/null | sed -E 's/^v?([0-9]+).*/\1/')
            if [[ -n "$node_major" ]] && (( node_major >= 20 )); then
                echo "  ✓ node $(node --version 2>/dev/null) (host binary; harness will reuse it)"
            else
                echo "  · node $(node --version 2>/dev/null) is < 20 — harness fetches Node >= 20 automatically on first 'harness host'"
            fi
        else
            echo "  · node not found — harness fetches Node automatically on first 'harness host'"
        fi
        if command -v opencode >/dev/null 2>&1; then
            echo "  ✓ opencode CLI (host binary; reused if it matches the pinned version)"
        else
            echo "  · opencode not found — harness installs it automatically on first 'harness host'"
        fi
    fi

    # Disk space (5GB recommended for fresh install + image pulls)
    local available_mb
    available_mb=$(df -m "$cwd" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ -n "$available_mb" ]]; then
        if (( available_mb >= 5120 )); then
            echo "  ✓ disk space (${available_mb}M available, 5120M recommended)"
        else
            echo "  ⚠ disk space — only ${available_mb}M available; proxy/agent/serena images need ~5GB total"
            # warning, not error
        fi
    fi

    # Write access to CWD
    if [[ ! -w "$cwd" ]]; then
        echo "  ✗ CWD ($cwd) is not writable"
        errors=$((errors+1))
    else
        echo "  ✓ write access to $cwd"
    fi

    # Existing harness/ in CWD
    if [[ -d "$cwd/$CLONE_DIR" ]]; then
        echo "  ✗ ./$CLONE_DIR/ already exists; remove it or run install in a different parent directory"
        errors=$((errors+1))
    fi

    if (( errors > 0 )); then
        echo
        echo "[install] $errors check(s) failed. Resolve the issues above and re-run."
        # Signal failure to the top-level caller, which performs the actual
        # abort. preflight is a function, so it cannot end a sourced script
        # itself (see exit_or_return) — it must return non-zero and let the
        # caller abort at the top level.
        return 1
    fi

    echo "  all checks passed"
    echo
}

# --- host-only risk acceptance ----------------------------------------------
#
# A HOST_ONLY install (no docker/podman found) can only launch via 'harness
# host', which runs the proxy + opencode as plain host processes: NO egress
# firewall, opencode running as the full host user. That is a real blast
# radius, so before the installer writes anything to disk we (a) recommend
# installing a container runtime instead and (b) REQUIRE the user to accept
# the risks explicitly. This is the install-time analog of harness's
# launch-time host_confirm_gate and shares its HARNESS_HOST_CONFIRM=1
# automation bypass and /dev/tty discipline. Container installs skip it.
# Returns 0 to proceed, 1 to abort; the top-level caller performs the actual
# abort, because a `return` inside a helper can't end a sourced script (see
# exit_or_return / fail).
host_only_risk_gate() {
    (( ${HOST_ONLY:-0} == 1 )) || return 0   # container runtime present: nothing to accept

    local host_user
    host_user=$(id -un 2>/dev/null || echo "${USER:-your account}")

    echo
    title "no container runtime found: host-only install"
    cat <<EOF

docker/podman were not found, so this install can only run harness in
containerless ${C_BOLD}host mode${C_RESET} ('harness host'). Host mode trades away the
sandbox:

  ${C_YELLOW}!${C_RESET} opencode runs as your full host user ($host_user) — it can read and
    write every file your account can, including ~/.ssh, ~/.aws, ~/.config.
  ${C_YELLOW}!${C_RESET} There is NO egress firewall. Outbound network is UNRESTRICTED (the
    harness allowlist only constrains container mode).

${C_BOLD}Recommended:${C_RESET} install docker or podman and re-run this installer. Container
mode sandboxes the agent inside an image behind the egress allowlist, a much
smaller blast radius. Run with docker whenever you can; host mode is the
fallback, not the default.

(Host mode also re-confirms this on every launch; accepting here does not
suppress that.)
EOF
    echo

    if [[ "${HARNESS_HOST_CONFIRM:-0}" == "1" ]]; then
        echo "  (host-only risks auto-accepted via HARNESS_HOST_CONFIRM=1)"
        return 0
    fi
    if [[ ! -e /dev/tty ]]; then
        fail "non-interactive install without /dev/tty; refusing a silent host-only install"
        fail "install docker/podman and re-run, or set HARNESS_HOST_CONFIRM=1 to accept the risks in automation"
        return 1
    fi
    local ans
    printf 'Proceed with a host-only install and accept these risks? [y/n]: ' >&2
    if ! IFS= read -r ans </dev/tty; then
        fail "no input received; aborting"
        return 1
    fi
    case "${ans:-}" in
        y|Y|yes|YES) return 0 ;;
        *)
            fail "aborted: host-only risks not accepted"
            echo "  Install docker or podman for sandboxed container mode, then re-run." >&2
            return 1
            ;;
    esac
}

# --- model-menu mode --------------------------------------------------------
#
# When -m / --model-menu is given the full install is skipped. The harness
# directory must already exist. We read the existing .env, fetch the model
# list from the upstream API, show the selection menu, and update
# DEFAULT_MODEL_NAME in place.

if (( MODEL_MENU )); then
    title "update default model"
    # Locate the install root. The user may run this script from the parent
    # directory (install_root = $cwd/harness) OR from inside the install root
    # itself — e.g. the script lives at <root>/harness-install.sh and the
    # .env is in the same directory. Try each candidate and use the first
    # that contains a readable .env.
    _mm_root=""
    for _candidate in "$install_root" "$cwd" "$script_dir"; do
        if [[ -f "$_candidate/.env" ]]; then
            _mm_root="$_candidate"
            break
        fi
    done
    if [[ -z "$_mm_root" ]]; then
        fail "no harness install found — run harness-install.sh without -m to install first"
        echo "  searched: $install_root" >&2
        echo "            $cwd" >&2
        echo "            $script_dir" >&2
        (( HARNESS_INSTALL_SOURCED )) && return 1
        exit 1
    fi
    install_root="$_mm_root"
    ok "found install at $install_root"
    _run_model_menu
    exit_or_return 0
fi

# --- connectivity-check mode ------------------------------------------------
#
# When -c / --check is given the full install is skipped. Reads the existing
# .env, then runs staged curl tests to diagnose why the AI is unreachable:
# allowlist, SSL certificate trust, HTTP status, and model-name validity.

if (( CONN_CHECK )); then
    title "connectivity check"
    _cc_root=""
    for _candidate in "$install_root" "$cwd" "$script_dir"; do
        if [[ -f "$_candidate/.env" ]]; then
            _cc_root="$_candidate"
            break
        fi
    done
    if [[ -z "$_cc_root" ]]; then
        fail "no harness install found — run harness-install.sh without flags to install first"
        echo "  searched: $install_root" >&2
        echo "            $cwd" >&2
        echo "            $script_dir" >&2
        (( HARNESS_INSTALL_SOURCED )) && return 1
        exit 1
    fi
    install_root="$_cc_root"
    ok "found install at $install_root"
    echo
    _run_conn_check
    exit_or_return 0
fi

# --- uninstall mode ---------------------------------------------------------
#
# When -u / --uninstall is given the full install is skipped. Locates the
# install root, shows exactly what will be removed, prompts for confirmation,
# then deletes the install directory and the PATH wrapper.

if (( UNINSTALL )); then
    title "uninstall harness"
    _un_root=""
    for _candidate in "$install_root" "$cwd" "$script_dir"; do
        if [[ -f "$_candidate/.env" ]]; then
            _un_root="$_candidate"
            break
        fi
    done
    if [[ -z "$_un_root" ]]; then
        fail "no harness install found"
        echo "  searched: $install_root" >&2
        echo "            $cwd" >&2
        echo "            $script_dir" >&2
        (( HARNESS_INSTALL_SOURCED )) && return 1
        exit 1
    fi
    install_root="$_un_root"
    _wrapper="$LOCAL_BIN/$PROGRAM_NAME"
    echo
    echo "  The following will be permanently deleted:"
    echo "    ${C_YELLOW}$install_root${C_RESET}   (install directory — code, config, state)"
    [[ -f "$_wrapper" ]] && \
        echo "    ${C_YELLOW}$_wrapper${C_RESET}   (PATH wrapper)"
    echo
    read -rp "  type 'yes' to confirm uninstall, anything else to cancel: " _un_ans
    if [[ "${_un_ans:-}" != "yes" ]]; then
        echo "  cancelled."
        exit_or_return 0
    fi
    echo
    # Stop running containers. Show output so the user can confirm they stopped.
    # --volumes removes anonymous Docker volumes tied to this project.
    _un_rt=$(_inline_container_runtime)
    if command -v "$_un_rt" >/dev/null 2>&1 && "$_un_rt" info >/dev/null 2>&1; then
        echo "  stopping harness containers..."
        (cd "$install_root" && "$_un_rt" compose --project-name harness down \
            --remove-orphans --volumes) || true
        ok "containers stopped"
    fi
    echo
    # Wipe the bind-mounted state directory first. Docker Desktop on Windows
    # can hold handles on bind-mounted paths and recreate the directory skeleton
    # after rm -rf, leaving stale opencode state that causes EEXIST on reinstall.
    if [[ -d "$install_root/state" ]]; then
        rm -rf "$install_root/state"
        ok "cleared state/"
    fi
    rm -rf "$install_root"
    if [[ -d "$install_root" ]]; then
        warn "could not fully remove $install_root — some files may still be locked by Docker"
        echo "  Close Docker Desktop and retry: rm -rf \"$install_root\"" >&2
    else
        ok "removed $install_root"
    fi
    if [[ -f "$_wrapper" ]]; then
        rm -f "$_wrapper"
        ok "removed $_wrapper"
    fi
    echo
    warn "If PATH still shows 'harness', open a new terminal or run:  hash -r"
    exit_or_return 0
fi

# --- intent -----------------------------------------------------------------

cat <<EOF
${C_BOLD}harness installer${C_RESET}

This will install the harness runtime into a single self-contained folder:
  $install_root

That folder is both the git clone and the install root — code, user config,
and runtime state all live inside it. To uninstall later:
  rm -rf $install_root
  rm $LOCAL_BIN/$PROGRAM_NAME

Steps:
  1. Run preflight checks (git, docker, disk space, write access).
  2. Refuse if $install_root already exists.
  3. Clone $REPO_URL into $install_root${BRANCH:+ (branch: $BRANCH)}
  4. Create runtime state directories under $install_root/state/
  5. Seed .env (from a pre-edited .env beside this installer if present, else
     from .env.example).
  6. Seed .harness-allowlist (from one beside this installer if present, else
     from .harness-allowlist.example).
  7. Optionally install a 'harness' wrapper into $LOCAL_BIN and update PATH.
  8. Optionally accept the upstream API URL and key (PROXY_API_URL, PROXY_API_KEY in .env).
  9. Query the upstream API for available models and set DEFAULT_MODEL_NAME in .env.

EOF

# --- preflight (fail fast, before any prompts) ------------------------------

if ! preflight; then
    (( HARNESS_INSTALL_SOURCED )) && return 1
    exit 1
fi

# --- host-only risk acceptance (before any writes) --------------------------
#
# On a HOST_ONLY install, require explicit acceptance of the no-sandbox risks
# (and recommend docker) before the clone or any disk write, so declining
# leaves the disk untouched. No-op for container installs.
if ! host_only_risk_gate; then
    (( HARNESS_INSTALL_SOURCED )) && return 1
    exit 1
fi

# --- prompts ----------------------------------------------------------------
#
# No generic "continue?" gate here: for a container install the next prompt
# (PATH) is the first thing the user answers, so the intent text above still
# gets a beat of consideration (a HOST_ONLY install stops earlier, at the
# host-only risk-acceptance gate above), and
# Ctrl-C aborts at any prompt. A prior confirm prompt was also broken when the
# script is sourced (the README-recommended path) — its abort ran
# `exit_or_return 0`, whose `return` only leaves that helper function, not the
# sourced script, so answering "n" continued the install anyway.

read -rp "add 'harness' to PATH (recommended)? [y/n]: " path_ans
case "${path_ans:-}" in
    n|N|no|NO) want_path=0 ;;
    *) want_path=1 ;;
esac

# Upstream API URL. The proxy needs PROXY_API_URL in .env to know where to
# forward requests. Prompt for it now so the user doesn't have to hand-edit
# .env afterward. A blank answer is fine — the user can set it manually.
api_url_value=""
echo
echo "The proxy needs an upstream API URL (PROXY_API_URL in .env)."
echo "This is the base URL of your LLM provider, e.g. https://api.openai.com"
read -rp "enter upstream API URL (or press Enter to skip): " api_url_value
if [[ -z "$api_url_value" ]]; then
    echo "  skipping; remember to set PROXY_API_URL in .env manually."
fi

# Read a secret with each character echoed as '*' (not hidden entirely). Sets
# the global REPLY_SECRET. The point is diagnostic: the user sees the LENGTH and
# shape of what was pasted, so a wrong clipboard (a URL, a blank paste) is
# obvious from the count of stars, while the value itself stays off-screen.
# Backspace works (DEL 0x7f and BS 0x08). Char-by-char masking needs a real tty;
# when stdin isn't one (piped/non-interactive install) we fall back to a plain
# hidden read, matching the previous behavior. This must be the LAST interactive
# stdin read in the installer, so a multi-line clipboard that stops at the first
# newline can't bleed into a later prompt.
_read_secret_masked() {
    local prompt="$1"
    REPLY_SECRET=""
    printf '%s' "$prompt" >&2
    if [[ ! -t 0 ]]; then
        read -rs REPLY_SECRET
        printf '\n' >&2
        return 0
    fi
    local ch
    while IFS= read -rsn1 ch; do
        [[ -z "$ch" ]] && break                          # Enter ends input
        if [[ "$ch" == $'\x7f' || "$ch" == $'\x08' ]]; then   # DEL / Backspace
            if [[ -n "$REPLY_SECRET" ]]; then
                REPLY_SECRET="${REPLY_SECRET%?}"
                printf '\b \b' >&2
            fi
            continue
        fi
        REPLY_SECRET+="$ch"
        printf '*' >&2
    done
    printf '\n' >&2
}

# Upstream API key. The proxy needs a per-user key (PROXY_API_KEY in .env)
# that we can't ship — every user brings their own. Offer to capture it now
# so the user doesn't have to hand-edit .env afterward. Declining is fine;
# we just remind them to set it manually. No validation: whatever is pasted
# is accepted verbatim and written later (after .env is seeded).
want_api_key=0
api_key_value=""
echo
echo "The proxy needs an upstream API key (PROXY_API_KEY in .env)."
echo "If you skip this, edit .env and set PROXY_API_KEY before running harness."
read -rp "enter an upstream API key now? [y/n]: " key_ans
case "${key_ans:-}" in
    y|Y|yes|YES)
        want_api_key=1
        # Echo each char as '*' so a wrong/blank paste is visible by length
        # without revealing the key. Falls back to a hidden read off a tty.
        _read_secret_masked "paste API key (each char shown as *): "
        api_key_value="$REPLY_SECRET"
        ;;
    *)
        echo "  skipping; remember to set PROXY_API_KEY in .env manually."
        ;;
esac

# --- clone ------------------------------------------------------------------
#
# Resolve a proxy for the clone BEFORE running it. The clone happens before the
# install root's .env exists, so it can only reach a corp proxy via the
# environment. If a .env was dropped beside the installer ($script_dir) with
# HTTP_PROXY/HTTPS_PROXY set, honor those for the clone too — the same file is
# copied into the install root afterward, so the clone and later 'harness' runs
# share one source of truth. A blank/absent value in that .env is treated as
# unset: we leave the host's existing proxy environment alone, so whatever the
# shell already exports wins by default ("default to the host config").
#
# Both cases are exported (HTTPS_PROXY and https_proxy): git's libcurl gives the
# LOWER-case name precedence, so exporting only the upper-case form would lose
# to a host-exported lower-case value and defeat "the .env value wins".
apply_preclone_proxy() {
    local env_file="$script_dir/.env"
    [[ -f "$env_file" ]] || return 0
    local pk val lk line
    for pk in HTTP_PROXY HTTPS_PROXY; do
        val=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[[:space:]]*${pk}=(.*)$ ]] && val="${BASH_REMATCH[1]}"
        done <"$env_file"
        [[ -z "$val" ]] && continue   # unset/blank in .env → keep host env
        lk=$(printf '%s' "$pk" | tr '[:upper:]' '[:lower:]')
        export "$pk"="$val" "$lk"="$val"
        ok "using $pk from $env_file for the clone"
    done
    return 0   # never let the loop's last status trip the caller's set -e
}

title "cloning repo"

if [[ -e "$install_root" ]]; then
    fail "$install_root already exists; remove it or run harness-install.sh in a clean directory"
    (( HARNESS_INSTALL_SOURCED )) && return 1
    exit 1
fi

apply_preclone_proxy

# Detect a failed clone explicitly. We can't rely on `set -e` here: when the
# script is sourced (the README-recommended path) strict mode is off, so an
# unchecked failure would fall through to the ok() below and leave a broken
# half-install (the original #106 report). Check the exit code directly.
clone_args=()
[[ -n "$BRANCH" ]] && clone_args+=(--branch "$BRANCH")
if ! git clone "${clone_args[@]}" "$REPO_URL" "$install_root"; then
    fail "git clone of $REPO_URL failed.${BRANCH:+ (branch: $BRANCH)}"
    echo "  - Check network/VPN connectivity to the git host." >&2
    echo "  - Behind a corporate proxy? Export it before running, e.g." >&2
    echo "      HTTPS_PROXY=http://proxy.example:8080 source harness-install.sh" >&2
    echo "    or set HTTP_PROXY/HTTPS_PROXY in a .env placed beside this installer." >&2
    (( HARNESS_INSTALL_SOURCED )) && return 1
    exit 1
fi
ok "cloned into $install_root"

# --- post-clone: source full platform.sh ------------------------------------
#
# After the clone, the full helper library is on disk. Source it so anything
# below this point can use the canonical helpers instead of the inline
# fallbacks. Failure to find it indicates a broken clone — abort.

if [[ ! -f "$install_root/scripts/lib/platform.sh" ]]; then
    fail "internal: $install_root/scripts/lib/platform.sh missing after clone"
    (( HARNESS_INSTALL_SOURCED )) && return 1
    exit 1
fi
# shellcheck disable=SC1091
source "$install_root/scripts/lib/platform.sh"

# --- defense-in-depth: dos2unix on Windows ----------------------------------
#
# .gitattributes already forces LF on shell scripts, so this is belt-and-
# braces in case a user's git was configured to ignore .gitattributes or
# the clone path went through a tool that re-wrote line endings.

if [[ "$(harness_detect_os)" == "windows" ]]; then
    if command -v dos2unix >/dev/null 2>&1; then
        title "normalizing line endings on Windows"
        find "$install_root" -type f \( -name "*.sh" -o -name "harness" -o -name "harness-install.sh" \) \
            -exec dos2unix -q {} + 2>/dev/null || true
        ok "ran dos2unix on shell scripts"
    else
        warn "dos2unix not available; relying on .gitattributes"
    fi
fi

# --- runtime state dirs -----------------------------------------------------
#
# Everything under state/ is gitignored. .gitignore already excludes state/
# so these dirs never show up in `git status` inside the clone.

title "creating runtime state directories"
mkdir -p "$install_root/state/output" \
         "$install_root/state/agent/home" \
         "$install_root/state/mcp"
ok "created state/output, state/agent/home, state/mcp"

# --- .env handling ----------------------------------------------------------
#
# Three cases, in priority order:
#   1. $install_root/.env already exists (unusual; clone shouldn't ship .env)
#      → leave it alone.
#   2. $script_dir/.env exists (distributor/user dropped an edited .env beside
#      the installer) → copy it into the clone, leaving the source in place so
#      the shipped folder stays intact.
#   3. Neither → seed from .env.example inside the clone.
#
# B3-MANAGED: env-vars — <install-root>/.env. `harness upgrade` runs the
# `env_vars` manifest action (envfile_merge) to surface new variables added
# to .env.example without touching existing user values. See
# scripts/upgrade-manifest.json and scripts/lib/upgrade_actions.sh.

title "configuring .env"
if [[ -f "$install_root/.env" ]]; then
    ok "$install_root/.env already present; left untouched"
elif [[ -f "$script_dir/.env" ]]; then
    cp "$script_dir/.env" "$install_root/.env"
    ok "copied your pre-filled .env from $script_dir into $install_root/.env"
else
    cp "$install_root/.env.example" "$install_root/.env"
    ok "seeded $install_root/.env from .env.example"
    warn "edit $install_root/.env and fill in PROXY_API_URL, PROXY_API_KEY (and any other blank required values)"
fi

# If the user supplied an API key at the prompt, write it into the freshly
# seeded .env now, overwriting whatever PROXY_API_KEY value was there (the
# prompt is the most recent explicit signal, so it wins over a pre-placed
# value). A bash read-loop rewrites just the PROXY_API_KEY= line — not sed —
# so keys containing /, &, etc. need no escaping. Atomic .tmp + rename
# matches the rest of the script's write convention. Only non-empty input is
# written, so an accidental empty paste can't blank out a pre-placed key.
if (( want_api_key )) && [[ -n "$api_key_value" ]]; then
    env_target="$install_root/.env"
    env_tmp="$env_target.tmp.$$"
    key_written=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*PROXY_API_KEY= ]]; then
            printf 'PROXY_API_KEY=%s\n' "$api_key_value"
            key_written=1
        else
            printf '%s\n' "$line"
        fi
    done <"$env_target" >"$env_tmp"
    if (( ! key_written )); then
        printf 'PROXY_API_KEY=%s\n' "$api_key_value" >>"$env_tmp"
    fi
    mv -f "$env_tmp" "$env_target"
    ok "wrote PROXY_API_KEY from prompt input into $install_root/.env"
fi

if [[ -n "$api_url_value" ]]; then
    env_target="$install_root/.env"
    env_tmp="$env_target.tmp.$$"
    key_written=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*PROXY_API_URL= ]]; then
            printf 'PROXY_API_URL=%s\n' "$api_url_value"
            key_written=1
        else
            printf '%s\n' "$line"
        fi
    done <"$env_target" >"$env_tmp"
    if (( ! key_written )); then
        printf 'PROXY_API_URL=%s\n' "$api_url_value" >>"$env_tmp"
    fi
    mv -f "$env_tmp" "$env_target"
    ok "wrote PROXY_API_URL from prompt input into $install_root/.env"
fi

# Persist any corp proxy exported in the installing shell into the .env so
# later 'harness' runs (update/upgrade pull, mcp clone) reuse it without the
# user re-exporting each time. We fill ONLY blank HTTP_PROXY/HTTPS_PROXY
# lines, so a value the user pre-placed in their own .env wins. These
# are host-only (honored for host git, stripped from containers by harness).
proxy_env_target="$install_root/.env"
for pk in HTTP_PROXY HTTPS_PROXY; do
    pv="${!pk:-}"
    [[ -z "$pv" ]] && continue
    # Read the current value (if any) without sed, so proxy URLs containing
    # / & : @ need no escaping.
    cur=""
    has_key=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*${pk}=(.*)$ ]]; then
            cur="${BASH_REMATCH[1]}"; has_key=1; break
        fi
    done <"$proxy_env_target"
    [[ -n "$cur" ]] && continue   # explicit value already set — don't clobber
    proxy_tmp="$proxy_env_target.tmp.$$"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*${pk}= ]]; then
            printf '%s=%s\n' "$pk" "$pv"
        else
            printf '%s\n' "$line"
        fi
    done <"$proxy_env_target" >"$proxy_tmp"
    (( has_key )) || printf '%s=%s\n' "$pk" "$pv" >>"$proxy_tmp"
    mv -f "$proxy_tmp" "$proxy_env_target"
    ok "persisted $pk from your shell into $install_root/.env"
done

# --- default model selection ------------------------------------------------

title "configuring default model"
_run_model_menu

# --- firewall allowlist -----------------------------------------------------
#
# Every harness container reads its egress allowlist from
# <install-root>/.harness-allowlist. Priority order mirrors the .env logic:
#   1. one already in the install root → leave it alone.
#   2. one beside the installer ($script_dir) → copy it in, source left in
#      place.
#   3. otherwise seed from the bundled .harness-allowlist.example.
# Idempotent: existing user customizations are never touched.
#
# B3-MANAGED: allowlist-hosts — <install-root>/.harness-allowlist. `harness
# upgrade` runs the `allowlist_hosts` manifest action (linefile_merge) to
# append new hostnames added upstream without modifying user entries.

title "configuring firewall allowlist"
if [[ -f "$install_root/.harness-allowlist" ]]; then
    ok ".harness-allowlist already present; left untouched"
elif [[ -f "$script_dir/.harness-allowlist" ]]; then
    cp "$script_dir/.harness-allowlist" "$install_root/.harness-allowlist"
    ok "copied your pre-filled .harness-allowlist from $script_dir into $install_root/.harness-allowlist"
elif [[ -f "$install_root/.harness-allowlist.example" ]]; then
    cp "$install_root/.harness-allowlist.example" "$install_root/.harness-allowlist"
    ok "seeded $install_root/.harness-allowlist from .harness-allowlist.example"
else
    warn "no .harness-allowlist.example bundled; create $install_root/.harness-allowlist before running harness"
fi

# Auto-add the upstream API hostname to the allowlist so the proxy container
# can reach the LLM provider. This is the most common reason AI requests
# silently fail on a fresh install — the container firewall blocks the egress.
# Extract the hostname from PROXY_API_URL by stripping scheme, path, and port.
_api_host=$(grep -E '^PROXY_API_URL=' "$install_root/.env" 2>/dev/null \
    | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '\r' \
    | sed 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||;s|[/?#].*||;s|:.*||')
if [[ -n "$_api_host" ]]; then
    if [[ -f "$install_root/.harness-allowlist" ]]; then
        if grep -qxF "$_api_host" "$install_root/.harness-allowlist" 2>/dev/null; then
            ok "$_api_host already in .harness-allowlist"
        else
            printf '%s\n' "$_api_host" >> "$install_root/.harness-allowlist"
            ok "added $_api_host to .harness-allowlist (from PROXY_API_URL)"
        fi
    fi
else
    warn "PROXY_API_URL not set — add your LLM provider hostname to $install_root/.harness-allowlist before running harness"
fi

# --- PATH setup -------------------------------------------------------------
#
# We install a wrapper script (not a symlink) at ~/.local/bin/harness. On
# Windows, creating symlinks requires Developer Mode or admin privileges;
# wrappers work everywhere with no special permission. The wrapper exec's
# the real harness script so $0 still resolves to the install root.

if (( want_path )); then
    title "setting up PATH"
    mkdir -p "$LOCAL_BIN"
    wrapper="$LOCAL_BIN/$PROGRAM_NAME"
    target_harness="$install_root/$PROGRAM_NAME"

    cat > "$wrapper" <<EOF
#!/usr/bin/env bash
# harness wrapper — calls the real harness script in the install root.
exec "$target_harness" "\$@"
EOF
    chmod +x "$wrapper"
    ok "wrapper installed at $wrapper -> $target_harness"

    # Detect whether ~/.local/bin is already on PATH. case-style match against
    # the literal expanded directory.
    case ":$PATH:" in
        *":$LOCAL_BIN:"*)
            ok "$LOCAL_BIN already in PATH"
            ;;
        *)
            # Pick the right rcfile based on $SHELL.
            shell_name=$(basename "${SHELL:-}")
            case "$shell_name" in
                zsh)  rcfile="$HOME/.zshrc" ;;
                bash) rcfile="$HOME/.bashrc" ;;
                fish)
                    warn "fish shell detected; PATH not auto-updated"
                    warn "add this to ~/.config/fish/config.fish manually:"
                    echo "    set -gx PATH $LOCAL_BIN \$PATH"
                    rcfile=""
                    ;;
                *)    rcfile="$HOME/.profile" ;;
            esac

            if [[ -n "$rcfile" ]]; then
                # Idempotent: only append if no existing line references
                # ~/.local/bin. This is a heuristic — exact matching against
                # the literal export line we'd write would miss shell-managed
                # equivalents.
                if [[ -f "$rcfile" ]] && grep -q '\.local/bin' "$rcfile"; then
                    ok "$rcfile already references .local/bin; left untouched"
                else
                    {
                        printf '\n# Added by harness installer\n'
                        printf 'export PATH="$HOME/.local/bin:$PATH"\n'
                    } >>"$rcfile"
                    ok "appended PATH update to $rcfile"
                    warn "open a new terminal or run:  source $rcfile"
                fi
            fi
            ;;
    esac

    # Windows Git Bash: it starts LOGIN shells, which source ~/.bash_profile
    # (or ~/.bash_login / ~/.profile) and skip ~/.bashrc by default. The PATH
    # line we wrote to ~/.bashrc above therefore never runs in a fresh Git
    # Bash session — the reported symptom: harness installed, but ~/.local/bin
    # still not on PATH in new shells. Bridge it: make ~/.bash_profile source
    # ~/.bashrc. Only relevant for bash (other shells write to a file login
    # shells already read). Done outside the PATH-check above so a re-install
    # repairs a missing bridge even when ~/.local/bin is already on PATH.
    if [[ "$(harness_detect_os)" == "windows" ]] \
       && [[ "$(basename "${SHELL:-}")" == "bash" ]]; then
        bp="$HOME/.bash_profile"
        if [[ -f "$bp" ]] && grep -q '\.bashrc' "$bp"; then
            ok "$bp already sources ~/.bashrc; left untouched"
        else
            # Capture whether ~/.bash_profile existed BEFORE the append: the
            # `>>` redirection below creates the file, so an inline `! -f`
            # test inside the block would always be false.
            bp_new=1
            [[ -f "$bp" ]] && bp_new=0
            {
                printf '\n# Added by harness installer: Git Bash starts login shells,\n'
                printf '# which read ~/.bash_profile and skip ~/.bashrc by default.\n'
                # When creating a fresh ~/.bash_profile we would shadow an
                # existing ~/.profile (login shells stop reading it), so source
                # it too. When ~/.bash_profile already exists it already
                # handles ~/.profile as the user intended — only add the
                # ~/.bashrc bridge in that case.
                if (( bp_new )) && [[ -f "$HOME/.profile" ]]; then
                    printf 'if [ -f ~/.profile ]; then . ~/.profile; fi\n'
                fi
                printf 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi\n'
            } >>"$bp"
            ok "bridged $bp -> ~/.bashrc for Git Bash login shells"
            warn "open a new terminal for 'harness' to be on PATH"
        fi
    fi
fi

# --- final message ----------------------------------------------------------
#
# Direct, no-jargon. Lists the agents available out-of-the-box and any MCPs
# that came pre-installed (currently always zero, but the loop is here in
# case future installer flags pre-stage one).

if (( want_path )) && [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    # If install.sh was sourced (rather than run as a subprocess), update
    # the parent shell's PATH directly so `harness` works in this session
    # without opening a new terminal. When run as a subprocess the `export`
    # is harmless; the user still needs the rcfile to take effect for
    # future shells.
    export PATH="$LOCAL_BIN:$PATH"
fi

cat <<EOF

${C_BOLD}install complete${C_RESET} at $install_root.

EOF

# Show what MCPs are present in the bundled registry, since none are
# auto-installed by this script. Earlier the message said "Auto-installed
# MCPs:" with a list pulled from state/mcp, which was always "(none)" on
# a fresh install — misleading to users who took it as a status report
# rather than an empty-by-design state.
echo
echo "MCPs available to install:"
if [[ -d "$install_root/mcp-registry" ]]; then
    mcp_count=0
    for mcp_dir in "$install_root"/mcp-registry/*/; do
        [[ -d "$mcp_dir" ]] || continue
        name=$(basename "$mcp_dir")
        echo "  - $name"
        mcp_count=$((mcp_count + 1))
    done
    if (( mcp_count == 0 )); then
        echo "  (none in registry)"
    fi
fi
echo
echo "To install one: harness mcp install <name>"
echo "(see 'harness mcp list --available' for descriptions)"

cat <<EOF

Manage MCPs:
  harness mcp list                  show installed MCPs
  harness mcp install <name>        copy a registry entry into the active tree
  harness mcp uninstall <name>      remove entirely
  harness mcp enable <name>         auto-load it the next time the stack starts
  harness mcp disable <name>        stop auto-loading

Need a shell inside an agent container (for installing skills, debugging)?
  harness shell

If harness can't start the stack after configuration:
  harness preflight                   # validates .env and allowlist
  <runtime> logs harness-proxy-1      # see what the proxy says (runtime: docker or podman)
EOF

cat <<EOF

Uninstall harness:
  cd "$install_root" && <runtime> compose down --remove-orphans   # stop containers first (runtime: docker or podman)
  rm -rf "$install_root"
  rm "\$HOME/.local/bin/harness"

  (or run: ./harness-install.sh -u)
EOF

if (( want_path )); then
    cat <<EOF

'harness' added to PATH. If it doesn't work immediately:
  - Open a new terminal, OR
  - Run: export PATH="\$HOME/.local/bin:\$PATH"
EOF
fi

cat <<EOF

Found a bug or have an improvement to suggest, however small?
  https://github.com/HandelSim/harness/issues
EOF

# Final 'Next' block. Parse the three REQUIRED vars straight from the freshly
# written .env (same grep-based parse 'harness preflight' uses; the installer
# never sources .env) so we only flag values that are actually still empty —
# e.g. if PROXY_API_KEY was supplied at the prompt above, we don't nag for it.
missing_required=()
for v in PROXY_API_URL PROXY_API_KEY DEFAULT_MODEL_NAME; do
    val=$(grep -E "^${v}=" "$install_root/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    [[ -z "$val" ]] && missing_required+=("$v")
done

echo
title "Next"
# A HOST_ONLY install has no container runtime, so bare 'harness' (which starts
# the container stack) would hard-fail with "container runtime is required". The
# working launch verb for these users is 'harness host', so the primary "Next"
# instruction must point there, not at bare 'harness'.
run_cmd="harness"
(( ${HOST_ONLY:-0} == 1 )) && run_cmd="harness host"
if (( ${#missing_required[@]} > 0 )); then
    cat <<EOF
  1. Edit $install_root/.env and set these still-empty REQUIRED value(s):
       ${C_YELLOW}${missing_required[*]}${C_RESET}
  2. cd into any project directory and run: $run_cmd [agent flags...]
EOF
else
    cat <<EOF
  ${C_GREEN}✓${C_RESET} all REQUIRED values in .env are set
  Just one step: cd into any project directory and run: $run_cmd [agent flags...]
EOF
fi

if (( ${HOST_ONLY:-0} == 1 )); then
cat <<EOF

No container runtime was found, so this is a HOST-ONLY install. Use the
containerless mode:
  cd into any project directory and run: harness host [agent flags...]

'harness host' runs the proxy + opencode as plain host processes. The first run
fetches its dependencies (jq, Node >= 20, opencode) automatically into
state/host/toolchain — you only need Python 3 (python3/python/py). It has NO
egress firewall and runs
as your full host user, so it prompts to confirm on every launch. Recommended:
install docker/podman and use the sandboxed container mode ('harness
start/opencode') whenever you can; host mode is the fallback, not the default.
EOF
else
cat <<EOF

Running 'harness' with no command launches an opencode agent in the current
directory ('harness opencode' does the same). The FIRST run builds the
container images (proxy + agent), so expect it to take a few minutes;
every run afterward starts in seconds.

Prefer a lighter footprint with no docker? 'harness host' runs the proxy +
opencode as plain host processes (no daemon, no images). It trades away the
egress firewall and container isolation, so it prompts to confirm on every
launch.

Common agent flags (examples, not the full set):
  --yolo         auto-approve / skip-permissions
  --net          full outbound network for THIS run only
  --mount PATH   mount an extra host directory (repeatable)
  -p "PROMPT"    headless single-shot run
Any other opencode flag is passed straight through, so anything 'opencode'
accepts works here too.

Tip: don't install the 'serena' MCP up front — it adds a slow image build to
your first run. Add it later with 'harness mcp install serena', and only if
you understand it and know you need it.
EOF
fi

exit_or_return 0