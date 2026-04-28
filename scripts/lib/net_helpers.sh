# scripts/lib/net_helpers.sh — sourceable bash toolkit for harness network
# (allowlist + per-service firewall override) management. Used by both
# `harness net <subcmd>` and the test scripts.
#
# Source from a script:
#
#   source "$REPO_ROOT/scripts/lib/net_helpers.sh"
#
# Required globals at call time:
#   * for allowlist functions: NETLIB_ALLOWLIST  — path to .harness-allowlist
#   * for overrides functions: NETLIB_OVERRIDES  — path to .harness-net-overrides.json
#
# Tests can set both to mktemp paths to avoid touching the install root.
#
# All functions emit only data on stdout. Errors go to stderr and are non-fatal
# unless explicitly noted; `set -euo pipefail` in the caller will not be
# tripped by a missing optional file.

# Define harness_jq as a fallback for standalone use (e.g. tests sourcing this
# library directly without the full harness script). When sourced from the
# harness script, harness_jq is already defined and we don't override.
if ! declare -F harness_jq >/dev/null 2>&1; then
    harness_jq() {
        if command -v jq >/dev/null 2>&1; then
            jq "$@"
        else
            echo "net_helpers: jq required for standalone use" >&2
            return 1
        fi
    }
fi

# --- host validation --------------------------------------------------------

# Validate a hostname against the strict allowlist regex. Echoes the
# normalized (lower-cased) host on success; returns 1 with no output on a
# malformed host. Allowed: lowercase letters, digits, dots, hyphens. Any
# whitespace, comments, scheme, or path is rejected.
#
# Args: <host>
netlib_validate_host() {
    local h="${1:-}"
    h="${h,,}"                              # to lower
    if [[ -z "$h" ]]; then
        return 1
    fi
    if [[ ! "$h" =~ ^[a-z0-9.-]+$ ]]; then
        return 1
    fi
    # Reject leading/trailing dot or hyphen; reject consecutive dots.
    if [[ "$h" == .* || "$h" == *. || "$h" == -* || "$h" == *- ]]; then
        return 1
    fi
    if [[ "$h" == *..* ]]; then
        return 1
    fi
    printf '%s\n' "$h"
}

# --- allowlist parsing ------------------------------------------------------

# Echo every host in the allowlist that has the `# git-push` annotation.
# One host per line, no annotation. Reads NETLIB_ALLOWLIST.
netlib_list_pushable() {
    local file="${NETLIB_ALLOWLIST:-}"
    [[ -f "$file" ]] || return 0
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        {
            if ($0 ~ /#[[:space:]]*git-push([[:space:]]|$)/) {
                line = $0
                sub(/[[:space:]]*#.*$/, "", line)
                sub(/^[[:space:]]+/, "", line)
                sub(/[[:space:]]+$/, "", line)
                if (length(line) > 0) print line
            }
        }
    ' "$file"
}

# Echo every host in the allowlist with annotation status. Format:
#   <host>\t<pull|push>
# pull = read-only (no annotation); push = git-push annotation present.
# Reads NETLIB_ALLOWLIST.
netlib_list_hosts() {
    local file="${NETLIB_ALLOWLIST:-}"
    [[ -f "$file" ]] || return 0
    awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        {
            line = $0
            push = "pull"
            if (line ~ /#[[:space:]]*git-push([[:space:]]|$)/) {
                push = "push"
            }
            sub(/[[:space:]]*#.*$/, "", line)
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (length(line) > 0) printf "%s\t%s\n", line, push
        }
    ' "$file"
}

# Return 0 iff the allowlist already contains <host> as a non-comment line.
# Reads NETLIB_ALLOWLIST.
#
# Args: <host>
netlib_has_host() {
    local host="${1:-}"
    local file="${NETLIB_ALLOWLIST:-}"
    [[ -f "$file" && -n "$host" ]] || return 1
    netlib_list_hosts | awk -v h="$host" -F '\t' '$1 == h { found=1; exit } END { exit (found ? 0 : 1) }'
}

# --- allowlist mutation -----------------------------------------------------

# Append a host to the allowlist. Atomic via temp + mv. If <git_push> is
# truthy ("1" or "true"), the line is annotated `# git-push`. No-op (returns
# 0) if the host is already present with the same push state.
#
# Args: <host> [<git_push>]
netlib_add_host() {
    local host="${1:-}"
    local push="${2:-0}"
    local file="${NETLIB_ALLOWLIST:-}"
    if [[ -z "$host" || -z "$file" ]]; then
        echo "[net-helpers] add_host: host or NETLIB_ALLOWLIST missing" >&2
        return 1
    fi
    host=$(netlib_validate_host "$host") || {
        echo "[net-helpers] add_host: invalid host" >&2
        return 1
    }
    [[ -f "$file" ]] || : >"$file"

    # If already present without push and we're adding push, rewrite that line.
    # If already present and push state matches, nothing to do.
    local existing
    existing=$(netlib_list_hosts | awk -v h="$host" -F '\t' '$1 == h { print $2; exit }')
    if [[ -n "$existing" ]]; then
        if [[ "$existing" == "push" && "$push" != "1" && "$push" != "true" ]]; then
            return 0   # downgrades silently — caller used `allow` w/o flag
        fi
        if [[ "$existing" == "pull" && ( "$push" == "1" || "$push" == "true" ) ]]; then
            netlib_remove_host "$host" || true
        else
            return 0
        fi
    fi

    local tmp
    tmp=$(mktemp "${file}.XXXXXX")
    cp -p "$file" "$tmp" 2>/dev/null || true
    if [[ "$push" == "1" || "$push" == "true" ]]; then
        printf '%s   # git-push\n' "$host" >>"$tmp"
    else
        printf '%s\n' "$host" >>"$tmp"
    fi
    mv "$tmp" "$file"
}

# Remove every line whose stripped host matches <host>. Atomic via temp + mv.
# No-op if the host is not present. Returns 0.
#
# Args: <host>
netlib_remove_host() {
    local host="${1:-}"
    local file="${NETLIB_ALLOWLIST:-}"
    [[ -n "$host" && -f "$file" ]] || return 0
    host=$(netlib_validate_host "$host") || return 1

    local tmp
    tmp=$(mktemp "${file}.XXXXXX")
    awk -v h="$host" '
        /^[[:space:]]*#/ { print; next }
        /^[[:space:]]*$/ { print; next }
        {
            line = $0
            sub(/[[:space:]]*#.*$/, "", line)
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line == h) next
            print
        }
    ' "$file" >"$tmp"
    mv "$tmp" "$file"
}

# --- per-service firewall overrides ----------------------------------------
#
# .harness-net-overrides.json shape:
#
#   {
#     "services": {
#       "agent": {"firewall_disabled": true, "reason": "..."},
#       "ollama": {"firewall_disabled": true}
#     }
#   }
#
# All operations are idempotent and atomic.

# Ensure NETLIB_OVERRIDES exists with a valid empty body. Creates parent dirs.
netlib_overrides_ensure() {
    local file="${NETLIB_OVERRIDES:-}"
    if [[ -z "$file" ]]; then
        echo "[net-helpers] overrides_ensure: NETLIB_OVERRIDES not set" >&2
        return 1
    fi
    if [[ ! -f "$file" ]]; then
        mkdir -p "$(dirname "$file")"
        printf '{"services": {}}\n' >"$file"
    fi
}

# Echo names of services that currently have firewall_disabled=true. One per
# line. Empty output if the file is missing or empty.
netlib_overrides_open_services() {
    local file="${NETLIB_OVERRIDES:-}"
    [[ -f "$file" ]] || return 0
    if ! command -v harness_jq >/dev/null 2>&1; then
        return 0
    fi
    harness_jq -r '
        .services // {}
        | to_entries[]
        | select(.value.firewall_disabled == true)
        | .key
    ' "$file" 2>/dev/null || true
}

# True iff service <name> currently has firewall_disabled=true.
# Args: <service>
netlib_overrides_is_open() {
    local svc="${1:-}"
    local file="${NETLIB_OVERRIDES:-}"
    [[ -n "$svc" && -f "$file" ]] || return 1
    if ! command -v harness_jq >/dev/null 2>&1; then
        return 1
    fi
    local v
    v=$(harness_jq -r --arg s "$svc" '.services[$s].firewall_disabled // false' "$file" 2>/dev/null || echo "false")
    [[ "$v" == "true" ]]
}

# Mark service <name> as firewall_disabled=true. Optional reason recorded
# alongside (free-form string from the user, kept for audit).
# Args: <service> [<reason>]
netlib_overrides_open() {
    local svc="${1:-}"
    local reason="${2:-}"
    local file="${NETLIB_OVERRIDES:-}"
    if [[ -z "$svc" || -z "$file" ]]; then
        echo "[net-helpers] overrides_open: service or NETLIB_OVERRIDES missing" >&2
        return 1
    fi
    if ! command -v harness_jq >/dev/null 2>&1; then
        echo "[net-helpers] overrides_open: jq is required" >&2
        return 1
    fi
    netlib_overrides_ensure
    local tmp
    tmp=$(mktemp "${file}.XXXXXX")
    harness_jq --arg s "$svc" --arg r "$reason" '
        .services = (.services // {})
        | .services[$s] = (
            (.services[$s] // {})
            | .firewall_disabled = true
            | (if $r == "" then . else .reason = $r end)
        )
    ' "$file" >"$tmp" 2>/dev/null && mv "$tmp" "$file" || {
        rm -f "$tmp"
        echo "[net-helpers] overrides_open: jq failed" >&2
        return 1
    }
}

# Remove the firewall_disabled flag for service <name>. Drops the service key
# entirely if it has no other state.
# Args: <service>
netlib_overrides_close() {
    local svc="${1:-}"
    local file="${NETLIB_OVERRIDES:-}"
    if [[ -z "$svc" || -z "$file" ]]; then
        echo "[net-helpers] overrides_close: service or NETLIB_OVERRIDES missing" >&2
        return 1
    fi
    [[ -f "$file" ]] || return 0
    if ! command -v harness_jq >/dev/null 2>&1; then
        echo "[net-helpers] overrides_close: jq is required" >&2
        return 1
    fi
    local tmp
    tmp=$(mktemp "${file}.XXXXXX")
    harness_jq --arg s "$svc" '
        .services = (.services // {})
        | (
            (.services[$s] // {})
            | del(.firewall_disabled)
            | del(.reason)
        ) as $cleaned
        | if ($cleaned | length) == 0
            then .services = (.services | del(.[$s]))
            else .services[$s] = $cleaned
          end
    ' "$file" >"$tmp" 2>/dev/null && mv "$tmp" "$file" || {
        rm -f "$tmp"
        echo "[net-helpers] overrides_close: jq failed" >&2
        return 1
    }
}
