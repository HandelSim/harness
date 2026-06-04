#!/usr/bin/env bash
#
# tests/unit_host_mcp_test.sh — exercise the host-MCP CLI surface
# (`harness mcp host-init` / `host-setup` scaffolding + registration, and the
# host-aware behavior of list/status/up/down/logs).
#
# A "host MCP" is a non-container MCP: it runs as a process on the host and is
# registered as a client-config-only entry under state/mcp/<name>/ (no
# compose.yml). See architecture/mcp.md "Host MCPs".
#
# Pure unit test — NO docker, NO network, NO real server spawn. It invokes the
# real `harness` script as a subprocess with HARNESS_INSTALL_ROOT pointed at a
# throwaway tmpdir, so clone_dir still resolves to the real repo (for the
# template source) while all writes land in the tmpdir. host-init touches no
# docker; the up/down/logs host branches route to the process supervisor BEFORE
# require_docker, so they are reachable here too. We exercise only the branches
# that don't spawn a server (down-when-stopped, logs-without-logfile, and up's
# already-running short-circuit via a fake live pidfile). The real start/stop
# path is covered by tests/host_mcp_e2e_test.sh.
#
# Prints "HOST MCP TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " host MCP CLI test"
echo "============================================================"

fail() { echo "[host-mcp-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-mcp-test] OK: $*"; }

[[ -x "$HARNESS" || -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
[[ -d "$REPO_ROOT/host-mcp/template" ]] || fail "template missing at host-mcp/template"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# Invoke the real harness with an isolated install root. Capture combined
# output; return the harness exit code. HARNESS_NET_CONFIRM keeps any stray
# prompt non-interactive.
HARNESS_OUT=""
run_harness() {
    local rc=0
    HARNESS_OUT=$(
        HARNESS_INSTALL_ROOT="$TMP_ROOT" \
        HARNESS_NET_CONFIRM=1 \
        bash "$HARNESS" "$@" 2>&1
    ) || rc=$?
    return $rc
}

NAME="demoproj"
PORT=9123
INST="$TMP_ROOT/host-mcp/$NAME"
REG="$TMP_ROOT/state/mcp/$NAME"

# --- T1: host-init scaffolds the instance with placeholders substituted ------
run_harness mcp host-init "$NAME" --port "$PORT" || fail "T1: host-init exited $? — $HARNESS_OUT"

[[ -f "$INST/server.py" ]]      || fail "T1: instance server.py missing"
[[ -f "$INST/project.json" ]]   || fail "T1: instance project.json missing"
[[ -f "$INST/AGENTS.md" ]]      || fail "T1: instance AGENTS.md missing"
[[ -f "$INST/requirements.txt" ]] || fail "T1: instance requirements.txt missing"
[[ -f "$INST/run.sh" ]] || fail "T1: run.sh missing"
# run.sh is the single git-bash-native launcher; PowerShell was removed, so the
# scaffold must NOT carry a run.ps1.
[[ -e "$INST/run.ps1" ]] && fail "T1: run.ps1 should not be scaffolded (PowerShell removed)"
ok "T1: instance scaffolded at $INST"

grep -q "__MCP_NAME__" "$INST/server.py" "$INST/project.json" \
    && fail "T1: __MCP_NAME__ placeholder not substituted"
grep -q "__MCP_PORT__" "$INST/server.py" "$INST/project.json" \
    && fail "T1: __MCP_PORT__ placeholder not substituted"
grep -q "\"$NAME\"" "$INST/server.py" || fail "T1: server.py missing substituted name"
grep -q "$PORT" "$INST/project.json"  || fail "T1: project.json missing substituted port"
ok "T1: name/port placeholders substituted"

# --- T2: registration is client-config-only (no compose.yml) -----------------
[[ -f "$REG/client-config.json" ]] || fail "T2: registration client-config.json missing"
[[ -f "$REG/harness-meta.json" ]]  || fail "T2: registration harness-meta.json missing"
[[ ! -f "$REG/compose.yml" ]]      || fail "T2: host MCP must NOT have a compose.yml"
ok "T2: registration is client-config-only (no compose.yml)"

grep -q "host.docker.internal:$PORT/mcp" "$REG/client-config.json" \
    || fail "T2: client-config.json missing the host.docker.internal:$PORT/mcp URL"
grep -q '"transport": "host"' "$REG/harness-meta.json" \
    || fail "T2: harness-meta.json missing transport:host"
grep -q "\"host_port\": $PORT" "$REG/harness-meta.json" \
    || fail "T2: harness-meta.json missing host_port:$PORT"
grep -q '"enabled": true' "$REG/harness-meta.json" \
    || fail "T2: harness-meta.json should be enabled"
ok "T2: registration content correct (URL, transport, port, enabled)"

# --- T3: status recognizes it as a host MCP ----------------------------------
run_harness mcp status "$NAME" || fail "T3: status exited $? — $HARNESS_OUT"
grep -q "transport: host" <<<"$HARNESS_OUT" || fail "T3: status missing 'transport: host' — $HARNESS_OUT"
grep -q "host.docker.internal:$PORT/mcp" <<<"$HARNESS_OUT" || fail "T3: status missing endpoint — $HARNESS_OUT"
grep -q "state:     installed-enabled" <<<"$HARNESS_OUT" || fail "T3: status not installed-enabled — $HARNESS_OUT"
# Not started yet (no supervised pidfile), so status reports the process stopped.
grep -q "process:   stopped" <<<"$HARNESS_OUT" || fail "T3: status should show process stopped — $HARNESS_OUT"
ok "T3: status reports host transport + endpoint + installed-enabled + process state"

# --- T4: list shows the supervised host runtime state ------------------------
run_harness mcp list || fail "T4: list exited $? — $HARNESS_OUT"
grep -q "$NAME" <<<"$HARNESS_OUT" || fail "T4: list missing $NAME — $HARNESS_OUT"
# Supervisor reports live state; stopped because nothing has started it.
grep -q "host (stopped)" <<<"$HARNESS_OUT" || fail "T4: list missing host stopped marker — $HARNESS_OUT"
ok "T4: list shows the host MCP with its supervised runtime state"

# --- T5: up/down/logs route to the host-process supervisor (no docker) --------
# down on a stopped host MCP is an idempotent no-op (exit 0, "not running"). No
# pidfile exists yet, so nothing is killed.
run_harness mcp down "$NAME" || fail "T5: 'mcp down' on a stopped host MCP should exit 0 — $HARNESS_OUT"
grep -qi "not running" <<<"$HARNESS_OUT" || fail "T5: 'mcp down' should report not running — $HARNESS_OUT"

# logs with no captured logfile errors clearly (and never touches docker).
if run_harness mcp logs "$NAME"; then
    fail "T5: 'mcp logs' with no logfile should exit non-zero — $HARNESS_OUT"
fi
grep -qi "no supervised log" <<<"$HARNESS_OUT" || fail "T5: 'mcp logs' should explain there's no log — $HARNESS_OUT"

# up's already-running short-circuit: plant a fake pidfile pointing at THIS
# test's PID (guaranteed alive), so `up` sees it running and returns without
# spawning a server. Remove it immediately after so no later step kills us.
mkdir -p "$REG"
printf '%s\n' "$$" >"$REG/server.pid"
run_harness mcp up "$NAME" || fail "T5: 'mcp up' on a running host MCP should exit 0 — $HARNESS_OUT"
grep -qi "already running" <<<"$HARNESS_OUT" || fail "T5: 'mcp up' should report already running — $HARNESS_OUT"
rm -f "$REG/server.pid"
ok "T5: up/down/logs route to the host supervisor without docker or a real spawn"

# --- T6: re-init without --force is refused; --force re-scaffolds ------------
if run_harness mcp host-init "$NAME" --port "$PORT"; then
    fail "T6: re-init without --force should be refused — $HARNESS_OUT"
fi
grep -qi "already registered" <<<"$HARNESS_OUT" || fail "T6: expected 'already registered' — $HARNESS_OUT"
ok "T6: re-init without --force refused"

run_harness mcp host-init "$NAME" --port 9200 --force || fail "T6: --force re-init exited $? — $HARNESS_OUT"
grep -q "9200" "$REG/client-config.json" || fail "T6: --force did not update the port to 9200"
ok "T6: --force re-init re-scaffolds with the new port"

# --- T7: input validation ----------------------------------------------------
if run_harness mcp host-init "Bad/Name"; then
    fail "T7: invalid name should be rejected — $HARNESS_OUT"
fi
grep -qi "invalid name" <<<"$HARNESS_OUT" || fail "T7: expected 'invalid name' — $HARNESS_OUT"

if run_harness mcp host-init template; then
    fail "T7: 'template' name should be reserved — $HARNESS_OUT"
fi
grep -qi "reserved" <<<"$HARNESS_OUT" || fail "T7: expected 'reserved' for template — $HARNESS_OUT"

if run_harness mcp host-init okname --port 99999; then
    fail "T7: out-of-range port should be rejected — $HARNESS_OUT"
fi
grep -qi "invalid --port" <<<"$HARNESS_OUT" || fail "T7: expected 'invalid --port' — $HARNESS_OUT"
ok "T7: name + port validation rejects bad input"

# --- T8: host-setup refuses a name that was never scaffolded -----------------
if run_harness mcp host-setup neverscaffolded; then
    fail "T8: host-setup on a missing scaffold should fail — $HARNESS_OUT"
fi
grep -qi "no host MCP scaffold" <<<"$HARNESS_OUT" || fail "T8: expected 'no host MCP scaffold' — $HARNESS_OUT"
ok "T8: host-setup refuses a missing scaffold"

echo
echo "HOST MCP TEST PASSED"
