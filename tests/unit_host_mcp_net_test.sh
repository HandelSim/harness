#!/usr/bin/env bash
#
# unit_host_mcp_net_test.sh — docker-free unit coverage for the host MCP
# reachability auto-wiring added alongside the PowerShell removal.
#
# Covers the two host-side helpers in `harness`:
#   - any_host_mcp_enabled   (is there an installed+enabled host MCP?)
#   - emit_host_mcp_docker_args (the --add-host + -e args to inject)
# and the /etc/hosts resolution logic the firewall uses (init-firewall.sh
# section 9b) to turn host.docker.internal into an IP it can allow.
#
# These run without docker: `harness` is sourced with HARNESS_SOURCE_ONLY=1 so
# main() does not run, and the helpers are exercised against a synthetic
# state/mcp tree. The firewall resolution is exercised as a standalone snippet
# (init-firewall.sh itself flushes iptables at the top and must never run on a
# developer host; the live path is covered by the slow host_mcp_e2e_test.sh).
#
# Run:  bash tests/unit_host_mcp_net_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

pass=0
fail() { echo "[host-mcp-net] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-mcp-net] OK: $*"; pass=$((pass + 1)); }

echo "============================================================"
echo " host MCP reachability unit test (docker-free)"
echo "============================================================"

# --- source harness for its helpers, without running main -------------------
export HARNESS_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "${REPO_ROOT}/harness"

# Point the MCP registry at a throwaway tree and rebuild the fixtures there.
TMP_STATE="$(mktemp -d -t harness-hostmcp-net.XXXXXX)"
trap 'rm -rf "${TMP_STATE}"' EXIT
mcp_active_dir="${TMP_STATE}/mcp"
mkdir -p "${mcp_active_dir}"

mk_host_mcp() {  # name enabled(true|false)
    local name="$1" enabled="$2" d="${mcp_active_dir}/$1"
    mkdir -p "$d"
    printf '{"mcpServers":{"%s":{"url":"http://host.docker.internal:9999/mcp"}}}\n' "$name" >"$d/client-config.json"
    printf '{"enabled":%s,"transport":"host","host_port":9999,"allowed_domains":[]}\n' "$enabled" >"$d/harness-meta.json"
}
mk_container_mcp() {  # name — has compose.yml ⇒ NOT a host MCP
    local name="$1" d="${mcp_active_dir}/$1"
    mkdir -p "$d"
    printf '{"mcpServers":{"%s":{"url":"http://%s:8000/mcp"}}}\n' "$name" "$name" >"$d/client-config.json"
    printf 'services: {}\n' >"$d/compose.yml"
    printf '{"enabled":true}\n' >"$d/harness-meta.json"
}

# --- T1: enabled host MCP present ⇒ any_host_mcp_enabled true ----------------
rm -rf "${mcp_active_dir:?}"/*
mk_host_mcp enabledhost true
if any_host_mcp_enabled; then ok "T1: enabled host MCP detected"
else fail "T1: enabled host MCP not detected"; fi

# --- T2: only a DISABLED host MCP ⇒ false ------------------------------------
rm -rf "${mcp_active_dir:?}"/*
mk_host_mcp disabledhost false
if any_host_mcp_enabled; then fail "T2: disabled host MCP must not count"
else ok "T2: disabled host MCP correctly ignored"; fi

# --- T3: only a CONTAINER MCP (has compose.yml) ⇒ false ----------------------
rm -rf "${mcp_active_dir:?}"/*
mk_container_mcp containermcp
if any_host_mcp_enabled; then fail "T3: container MCP must not count as host"
else ok "T3: container MCP correctly ignored"; fi

# --- T4: emit_host_mcp_docker_args emits exactly the 3 expected NUL args -----
rm -rf "${mcp_active_dir:?}"/*
mk_host_mcp enabledhost true
mk_container_mcp containermcp          # noise: must not change the output
mapfile -d '' -t args < <(emit_host_mcp_docker_args)
(( ${#args[@]} == 3 )) || fail "T4: expected 3 args, got ${#args[@]}: ${args[*]}"
[[ "${args[0]}" == "--add-host=host.docker.internal:host-gateway" ]] \
    || fail "T4: arg0 wrong: ${args[0]}"
[[ "${args[1]}" == "-e" ]] || fail "T4: arg1 wrong: ${args[1]}"
[[ "${args[2]}" == "HARNESS_HOST_MCP_HOSTS=host.docker.internal" ]] \
    || fail "T4: arg2 wrong: ${args[2]}"
ok "T4: emit_host_mcp_docker_args emits --add-host + -e HARNESS_HOST_MCP_HOSTS"

# --- T5: no host MCP ⇒ emit nothing ------------------------------------------
rm -rf "${mcp_active_dir:?}"/*
mk_container_mcp onlycontainer
out="$(emit_host_mcp_docker_args | tr -d '\0')"
[[ -z "$out" ]] || fail "T5: expected no args, got: $out"
ok "T5: no host MCP ⇒ no docker args injected"

# --- T6: firewall /etc/hosts resolution logic (section 9b) -------------------
# Mirrors init-firewall.sh: resolve a host MCP name from /etc/hosts (NOT dig,
# which ignores /etc/hosts) and pull its IPv4. We exercise the awk fallback
# (getent on the host reads the real /etc/hosts, not our synthetic file).
SYN_HOSTS="${TMP_STATE}/hosts"
cat >"${SYN_HOSTS}" <<'EOF'
127.0.0.1	localhost
# a comment line that must be ignored
192.168.65.2	host.docker.internal gateway.internal
10.0.0.5	someotherhost
EOF
resolve_from_hosts() {  # hostsfile hostname  → prints IPv4s (none ⇒ empty, rc 0)
    awk -v h="$2" \
        '($0 !~ /^[[:space:]]*#/) { for (i=2;i<=NF;i++) if ($i==h) { print $1; break } }' \
        "$1" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u || true
}
got="$(resolve_from_hosts "${SYN_HOSTS}" host.docker.internal)"
[[ "$got" == "192.168.65.2" ]] \
    || fail "T6: expected 192.168.65.2 for host.docker.internal, got '$got'"
# A name only present in a comment must NOT resolve.
none="$(resolve_from_hosts "${SYN_HOSTS}" gateway.internal)"
[[ "$none" == "192.168.65.2" ]] \
    || fail "T6: alias on same line should resolve to its IP, got '$none'"
absent="$(resolve_from_hosts "${SYN_HOSTS}" not.present.example)"
[[ -z "$absent" ]] || fail "T6: absent host must resolve to nothing, got '$absent'"
ok "T6: /etc/hosts IPv4 resolution (Docker-Desktop-style off-bridge IP) works"

echo
echo "host MCP reachability unit test PASSED (${pass} checks)"
