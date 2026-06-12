#!/usr/bin/env bash
#
# tests/unit_host_test.sh — exercise the containerless `harness host` surface
# without docker, network, or a real proxy/opencode spawn.
#
# Sources the real `harness` with HARNESS_SOURCE_ONLY=1 (the same hook
# tests/upgrade_test.sh uses) so the host_* helpers are callable directly, then
# pins the install-root globals (state_root / env_file) at a throwaway tmpdir.
#
# Covers the host functions flagged as untested in
# .planning/codebase/CONCERNS.md H4, plus regressions for the fixes in this
# change set:
#   - host_require_config           rejects empty required vars (exit 1)
#   - host_confirm_gate             honors HARNESS_HOST_CONFIRM=1
#   - host_preflight                fails clearly when deps are absent
#   - host_write_opencode_config    emits valid JSON, baseURL -> 127.0.0.1,
#                                   dummy apiKey; and refuses without jq (M4)
#   - host_proxy_fingerprint        stable, and sensitive to port/key/reqs changes (M2)
#
# Prints "HOST TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " host (containerless) unit test"
echo "============================================================"

fail() { echo "[host-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v jq >/dev/null 2>&1 || fail "this test needs host jq"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# Source the host helpers without running main. HARNESS_INSTALL_ROOT pins the
# state_root / env_file / clone_dir globals the helpers read.
# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null

[[ "$(type -t host_require_config)" == "function" ]] || fail "host_require_config not sourced"
[[ "$(type -t host_write_opencode_config)" == "function" ]] || fail "host helpers not sourced"

# The helpers read $env_file; create it so the file-exists check passes.
: >"$env_file"

# --- T1: host_require_config rejects an empty required var -------------------
# Runs in a subshell because the function calls `exit 1` on failure.
if ( PROXY_API_URL=""; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) >/dev/null 2>&1; then
    fail "T1: host_require_config accepted an empty PROXY_API_URL"
fi
out=$( ( PROXY_API_URL=""; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) 2>&1 || true )
grep -q "PROXY_API_URL" <<<"$out" || fail "T1: error should name PROXY_API_URL — $out"
ok "T1: host_require_config rejects empty required vars and names them"

# --- T2: host_require_config passes when all three are set ------------------
if ! ( PROXY_API_URL="http://x"; PROXY_API_KEY="k"; DEFAULT_MODEL_NAME="m"; host_require_config ) >/dev/null 2>&1; then
    fail "T2: host_require_config rejected a fully-populated config"
fi
ok "T2: host_require_config passes with all required vars set"

# --- T3: host_confirm_gate honors HARNESS_HOST_CONFIRM=1 --------------------
if ! ( HARNESS_HOST_CONFIRM=1; host_confirm_gate 0 ) >/dev/null 2>&1; then
    fail "T3: host_confirm_gate did not auto-confirm with HARNESS_HOST_CONFIRM=1"
fi
out=$( ( HARNESS_HOST_CONFIRM=1; host_confirm_gate 0 ) 2>&1 || true )
grep -qi "auto-confirmed" <<<"$out" || fail "T3: expected auto-confirm notice — $out"
ok "T3: host_confirm_gate auto-confirms via HARNESS_HOST_CONFIRM=1"

# --- T4: host_preflight fails clearly when deps are absent ------------------
# Empty PATH => command -v finds none of python3/jq/node/opencode.
out=$( ( PATH="/nonexistent-dir"; host_preflight ) 2>&1 || true )
if ( PATH="/nonexistent-dir"; host_preflight ) >/dev/null 2>&1; then
    fail "T4: host_preflight passed with no deps on PATH"
fi
for dep in python3 jq node opencode; do
    grep -qi "$dep" <<<"$out" || fail "T4: preflight output should mention $dep — $out"
done
ok "T4: host_preflight fails and names every missing dep"

# --- T5: host_write_opencode_config emits valid JSON pointed at loopback ----
( PROXY_PORT=8123; DEFAULT_MODEL_NAME="test-model"; host_write_opencode_config ) \
    || fail "T5: host_write_opencode_config returned non-zero"
cfg="$(host_opencode_config)"
[[ -f "$cfg" ]] || fail "T5: config file not written at $cfg"
jq -e . "$cfg" >/dev/null 2>&1 || fail "T5: config is not valid JSON"
base=$(jq -r '.provider.harness.options.baseURL' "$cfg")
[[ "$base" == "http://127.0.0.1:8123/v1" ]] || fail "T5: baseURL should be loopback:8123/v1, got '$base'"
key=$(jq -r '.provider.harness.options.apiKey' "$cfg")
[[ "$key" == "harness-dummy" ]] || fail "T5: apiKey should be the dummy placeholder, got '$key'"
jq -e '.provider.harness.models | has("test-model")' "$cfg" >/dev/null 2>&1 \
    || fail "T5: models map should include the default model"
ok "T5: opencode config is valid JSON, loopback baseURL, dummy key, default model present"

# --- T6: host_write_opencode_config refuses when jq is absent (M4 guard) ----
if ( PATH="/nonexistent-dir"; PROXY_PORT=8123; DEFAULT_MODEL_NAME="m"; host_write_opencode_config ) >/dev/null 2>&1; then
    fail "T6: host_write_opencode_config should fail without jq, not reach the docker sidecar"
fi
ok "T6: host_write_opencode_config refuses without host jq (no docker fallback)"

# --- T7: host_proxy_fingerprint is stable and config-sensitive (M2) ---------
fp1=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
fp2=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ -n "$fp1" && "$fp1" == "$fp2" ]] || fail "T7: fingerprint not stable across identical config ($fp1 / $fp2)"
fp_port=$( PROXY_PORT=9999 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ "$fp_port" != "$fp1" ]] || fail "T7: fingerprint should change when PROXY_PORT changes"
fp_key=$( PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=DIFFERENT DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ "$fp_key" != "$fp1" ]] || fail "T7: fingerprint should change when PROXY_API_KEY changes"
# The fingerprint folds in proxy/requirements.txt so a deps-only change (an
# upgrade that bumps flask/requests) restarts the proxy instead of reusing the
# stale venv. Override clone_dir per-call (it is a plain sourced global) to point
# at a fake requirements file; the assignment is discarded with the $(...)
# subshell, so it never leaks into later tests.
FAKE_CLONE="$TMP_ROOT/fakeclone"; mkdir -p "$FAKE_CLONE/proxy"
printf 'flask\nrequests\n' >"$FAKE_CLONE/proxy/requirements.txt"
fp_r1=$( clone_dir="$FAKE_CLONE" PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
printf 'flask\nrequests\nnewdep==1.0\n' >"$FAKE_CLONE/proxy/requirements.txt"
fp_r2=$( clone_dir="$FAKE_CLONE" PROXY_PORT=8000 PROXY_API_URL=u PROXY_API_KEY=k DEFAULT_MODEL_NAME=m host_proxy_fingerprint )
[[ -n "$fp_r1" && "$fp_r1" != "$fp_r2" ]] || fail "T7: fingerprint should change when requirements.txt changes ($fp_r1 / $fp_r2)"
ok "T7: host_proxy_fingerprint is stable and changes on port/key/requirements edits"

# --- T8: host_python_bin verifies the interpreter, not just command -v ------
# Regression for the Windows trap: an App-execution-alias stub named python3
# sits on PATH and prints "Python was not found..." instead of running. A bare
# `command -v python3` picks it, then `python3 -m venv` fails as if Python were
# missing — even though a real `python` is present. host_python_bin must probe
# --version and fall through to the working interpreter.
PYBIN_DIR="$TMP_ROOT/pybin"; mkdir -p "$PYBIN_DIR"
make_py() { # $1=name  $2=version-output (empty => behave like the Windows stub)
    local f="$PYBIN_DIR/$1"
    if [[ -n "$2" ]]; then
        printf '#!/bin/sh\necho "%s"\n' "$2" >"$f"
    else
        # Mimic the alias stub: ignores args, prints the not-found notice, exits 9009.
        printf '#!/bin/sh\necho "Python was not found; run without arguments to install from the Microsoft Store"\nexit 9009\n' >"$f"
    fi
    chmod +x "$f"
}

# Case A: python3 is the dead stub, python is a real Python 3 -> must pick python.
make_py python3 ""
make_py python "Python 3.12.4"
got=$( PATH="$PYBIN_DIR:$PATH" host_python_bin )
[[ "$got" == "python" ]] || fail "T8a: stub python3 not rejected; host_python_bin returned '$got' (want 'python')"

# Case B: a real python3 -> preferred over python.
make_py python3 "Python 3.11.9"
got=$( PATH="$PYBIN_DIR:$PATH" host_python_bin )
[[ "$got" == "python3" ]] || fail "T8b: real python3 not chosen; got '$got'"

# Case C: nothing resolves (both stubs) -> non-zero, no output.
make_py python3 ""
make_py python ""
if got=$( PATH="$PYBIN_DIR:$PATH" host_python_bin ); then
    fail "T8c: host_python_bin succeeded with only dead stubs (returned '$got')"
fi
ok "T8: host_python_bin probes --version (rejects the Windows alias stub, falls through to python)"

# --- T9: cmd_host gates on the upstream auth probe like container mode -------
# Regression: host mode skipped _gate_on_upstream_auth / _print_upstream_models,
# so a LOCKED key launched opencode anyway. cmd_host must now run the same gate
# container mode runs in cmd_start, aborting BEFORE host_proxy_start when the key
# is locked. Mirrors harness_test.sh T0.2/T0.3 (run_agent gate), but for cmd_host.
#
# Every pre-gate step is stubbed to a no-op so a non-zero exit can only come from
# the gate, and host_proxy_start is a sentinel that proves whether the launch
# advanced past the gate. cmd_host calls `exit`, so each case runs in a subshell.
host_stub_pregate() {
    host_require_python3() { :; }
    host_require_config()  { :; }
    host_confirm_gate()    { :; }
    ensure_dirs()          { :; }
    host_ensure_toolchain() { :; }
    host_preflight()       { :; }
}

# T9.1 — locked key (gate rc=1): cmd_host aborts non-zero and never starts the proxy.
locked_rc=0
locked_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    host_stub_pregate
    _gate_on_upstream_auth() { return 1; }
    _print_upstream_models() { echo "MODELS_PULLED"; }
    host_proxy_start() { echo "PROXY_STARTED"; }
    cmd_host 2>&1
) || locked_rc=$?
(( locked_rc != 0 )) || fail "T9.1: cmd_host should exit non-zero when the auth gate returns 1"
if grep -qE 'PROXY_STARTED|MODELS_PULLED' <<<"$locked_out"; then
    fail "T9.1: cmd_host advanced past a locked gate (started proxy / pulled models): $locked_out"
fi
ok "T9.1: cmd_host aborts before host_proxy_start when the key is locked"

# T9.2 — valid key (gate rc=0): cmd_host advances through the model pull into
# host_proxy_start (sentinel exits 43 to mark arrival).
pass_rc=0
pass_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    host_stub_pregate
    _gate_on_upstream_auth() { return 0; }
    _print_upstream_models() { echo "MODELS_PULLED"; return 0; }
    host_proxy_start() { echo "PROXY_STARTED"; exit 43; }
    cmd_host 2>&1
) || pass_rc=$?
(( pass_rc == 43 )) || fail "T9.2: expected cmd_host to reach host_proxy_start (rc=43), got $pass_rc — $pass_out"
grep -q 'MODELS_PULLED' <<<"$pass_out" || fail "T9.2: cmd_host did not pull the model catalog after a passing gate — $pass_out"
grep -q 'PROXY_STARTED' <<<"$pass_out" || fail "T9.2: cmd_host did not reach host_proxy_start after a passing gate — $pass_out"
ok "T9.2: cmd_host pulls models and starts the proxy when the key is valid"

# T9.3 — HARNESS_SKIP_AUTH_PROBE=1 bypasses the gate (CI / offline), still launching.
skip_rc=0
skip_out=$(
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" >/dev/null 2>&1
    host_stub_pregate
    export HARNESS_SKIP_AUTH_PROBE=1
    # Real gate/print run here; both must short-circuit to success on the skip env
    # without any curl call. host_proxy_start sentinel marks arrival.
    host_proxy_start() { echo "PROXY_STARTED"; exit 43; }
    cmd_host 2>&1
) || skip_rc=$?
(( skip_rc == 43 )) || fail "T9.3: HARNESS_SKIP_AUTH_PROBE=1 should bypass the gate and reach the proxy (rc=43), got $skip_rc — $skip_out"
ok "T9.3: HARNESS_SKIP_AUTH_PROBE=1 bypasses the host auth gate"

# --- T10: host_proxy_flags pins npm/pip pulls to the .env proxy --------------
# host mode fetches jq/Node (curl), opencode (npm), and the proxy venv (pip) on
# the host; npm/pip get the .env proxy as an explicit CLI flag so an ambient
# .npmrc/pip.conf cannot override it. curl honors *_PROXY from the env on its
# own and needs no flag. host_proxy_flags is already in scope from the
# top-level HARNESS_SOURCE_ONLY source above.

# T10.1 — no proxy set: both dialects emit nothing.
unset HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy 2>/dev/null || true
[[ -z "$(host_proxy_flags npm)" ]] || fail "T10.1: host_proxy_flags npm should be empty with no proxy set"
[[ -z "$(host_proxy_flags pip)" ]] || fail "T10.1: host_proxy_flags pip should be empty with no proxy set"
ok "T10.1: host_proxy_flags emits nothing when no proxy is configured"

# T10.2 — full proxy set: npm gets --proxy/--https-proxy/--noproxy, pip gets one --proxy.
export HTTP_PROXY="http://corp:3128" HTTPS_PROXY="http://corp:3129" NO_PROXY="internal.example"
npm_flags="$(host_proxy_flags npm)"
grep -q -- '--proxy http://corp:3128'        <<<"$npm_flags" || fail "T10.2: npm flags missing --proxy: $npm_flags"
grep -q -- '--https-proxy http://corp:3129'  <<<"$npm_flags" || fail "T10.2: npm flags missing --https-proxy: $npm_flags"
grep -q -- '--noproxy internal.example'       <<<"$npm_flags" || fail "T10.2: npm flags missing --noproxy: $npm_flags"
pip_flags="$(host_proxy_flags pip)"
grep -q -- '--proxy http://corp:3129' <<<"$pip_flags" || fail "T10.2: pip flags should use the https proxy: $pip_flags"
[[ "$(grep -o -- '--proxy' <<<"$pip_flags" | wc -l | tr -d ' ')" == 1 ]] || fail "T10.2: pip should emit exactly one --proxy: $pip_flags"
ok "T10.2: host_proxy_flags pins npm and pip to the .env proxy"

# T10.3 — only HTTP_PROXY set: pip still gets a --proxy (falls back to the http one).
unset HTTPS_PROXY NO_PROXY
export HTTP_PROXY="http://corp:3128"
grep -q -- '--proxy http://corp:3128' <<<"$(host_proxy_flags pip)" || fail "T10.3: pip should fall back to HTTP_PROXY when HTTPS_PROXY is unset"
ok "T10.3: host_proxy_flags pip falls back to HTTP_PROXY"
unset HTTP_PROXY

# --- T11: host_proxy_start does NOT scrub the proxy vars (upstream uses proxy) -
# The host proxy's upstream call (python requests, trust_env=True) must go
# THROUGH the corp proxy on a host that reaches the internet only via it — the
# opposite of container mode, whose network reaches the upstream directly (so
# its compose service carries no proxy vars). The launch must INHERIT this
# shell's proxy env, not scrub it. (An earlier version `env -u`'d the vars to
# mirror the container and 504'd: the host can't reach the upstream directly.)
# Regression guard: assert no scrub wrapper survives on the proxy.py launch.
launch_def="$(declare -f host_proxy_start)"
grep -q 'proxy\.py' <<<"$launch_def" || fail "T11: host_proxy_start no longer launches proxy.py?"
if grep -qE 'env[[:space:]]+-u|nohup env' <<<"$launch_def"; then
    fail "T11: host_proxy_start scrubs the proxy env (env -u); proxy.py must INHERIT it for the upstream hop"
fi
ok "T11: host_proxy_start lets proxy.py inherit the corp proxy for the upstream hop (no scrub)"

# --- T12: host_run_opencode exempts the loopback proxy via NO_PROXY -----------
# opencode runs on Bun, whose native fetch honors HTTP_PROXY/HTTPS_PROXY, and its
# provider baseURL is the LOCAL proxy at 127.0.0.1. With a corp proxy in .env the
# loopback call would tunnel through it (which can't reach this box) and every
# chat would 504. host_run_opencode must add 127.0.0.1/localhost to NO_PROXY so
# the provider hop goes DIRECT, while KEEPING HTTP_PROXY for other egress (Exa).
# Behavioral check: stub opencode to record the env it is launched with.
OCSTUB_DIR="$TMP_ROOT/ocstub"; mkdir -p "$OCSTUB_DIR"
NOPROXY_REC="$TMP_ROOT/oc_noproxy.txt"
cat >"$OCSTUB_DIR/opencode" <<EOF
#!/bin/sh
printf 'NO_PROXY=%s\n' "\$NO_PROXY" >> "$NOPROXY_REC"
printf 'HTTP_PROXY=%s\n' "\$HTTP_PROXY" >> "$NOPROXY_REC"
exit 0
EOF
chmod +x "$OCSTUB_DIR/opencode"
: >"$NOPROXY_REC"
(
    PATH="$OCSTUB_DIR:$PATH"
    export HTTP_PROXY="http://corp:3128" NO_PROXY="internal.example"
    PROXY_PORT=8123 DEFAULT_MODEL_NAME="m" host_run_opencode 0 1 >/dev/null 2>&1
) || true
grep -q 'NO_PROXY=.*127\.0\.0\.1' "$NOPROXY_REC" \
    || fail "T12: opencode child NO_PROXY missing loopback 127.0.0.1 — $(cat "$NOPROXY_REC")"
grep -q 'NO_PROXY=.*localhost' "$NOPROXY_REC" \
    || fail "T12: opencode child NO_PROXY missing localhost — $(cat "$NOPROXY_REC")"
grep -q 'NO_PROXY=.*internal\.example' "$NOPROXY_REC" \
    || fail "T12: opencode NO_PROXY dropped the pre-existing .env entry — $(cat "$NOPROXY_REC")"
grep -q 'HTTP_PROXY=http://corp:3128' "$NOPROXY_REC" \
    || fail "T12: HTTP_PROXY must stay set so non-loopback egress (Exa) keeps the proxy — $(cat "$NOPROXY_REC")"
ok "T12: host_run_opencode exempts loopback via NO_PROXY and keeps HTTP_PROXY for other egress"

# --- T13: host_output_dir_resolve remaps the container target for host mode ---
# Debug dumps in host mode were silently disabled: the documented OUTPUT_DIR
# value is the CONTAINER target `/output` (.env.example), which container mode
# bind-mounts to ./state/output but host mode has no bind mount for — so the
# proxy tried to mkdir `/output` at the host root, failed its writability probe,
# and wrote nothing. host_output_dir_resolve maps `/output` (and the Windows
# `//output` form) to $state_root/output so dumps land in the same place in both
# modes, leaves an empty value empty (opt-in stays off, CONCERNS H3), and passes
# a deliberate absolute host path through unchanged.
[[ "$(type -t host_output_dir_resolve)" == "function" ]] || fail "T13: host_output_dir_resolve not sourced"

got=$(host_output_dir_resolve "/output")
[[ "$got" == "$state_root/output" ]] || fail "T13: /output should remap to \$state_root/output, got '$got'"

got=$(host_output_dir_resolve "//output")
[[ "$got" == "$state_root/output" ]] || fail "T13: //output (Windows form) should remap to \$state_root/output, got '$got'"

got=$(host_output_dir_resolve "")
[[ -z "$got" ]] || fail "T13: empty OUTPUT_DIR must stay empty (dumps off), got '$got'"

got=$(host_output_dir_resolve "/home/me/dumps")
[[ "$got" == "/home/me/dumps" ]] || fail "T13: a deliberate host path must pass through unchanged, got '$got'"

# A path that merely contains 'output' but is not the container target is untouched.
got=$(host_output_dir_resolve "/var/output-logs")
[[ "$got" == "/var/output-logs" ]] || fail "T13: only the exact /output target remaps, got '$got'"
ok "T13: host_output_dir_resolve remaps /output to state/output, keeps empty off, passes host paths through"

# --- T14: host_reset_terminal undoes opencode's leaked mouse-tracking --------
# opencode's TUI enables xterm mouse-tracking but a crash/abrupt exit can leave
# it on, so afterwards every mouse move dumps raw report bytes (e.g. 35;77;12M)
# into the host shell. host_reset_terminal re-sends the DECRST disables on the
# interactive host exit path. Two invariants:
#   - the byte sequence disables the tracking + report-encoding modes (the leak)
#     and shows the cursor, and
#   - the TTY guard means a NON-tty stdout (pipe/redirect, as in this test and in
#     any captured/-p run) receives NOTHING — escape bytes must never leak into
#     non-terminal output.
[[ "$(type -t host_terminal_reset_seq)" == "function" ]] || fail "T14: host_terminal_reset_seq not sourced"
[[ "$(type -t host_reset_terminal)"    == "function" ]] || fail "T14: host_reset_terminal not sourced"

seq_out="$(host_terminal_reset_seq)"
# The modes that actually cause the leak: button/drag/any-motion tracking and
# the SGR/urxvt report encodings, all disabled (low 'l'), plus cursor-show.
for code in '1000l' '1002l' '1003l' '1006l' '1015l' '25h'; do
    case "$seq_out" in
        *$'\033'"[?$code"*) : ;;
        *) fail "T14: reset sequence missing ESC[?$code — got $(printf '%q' "$seq_out")" ;;
    esac
done
# It must NOT touch the alt-screen buffer (?1049) — opencode restores that on a
# normal exit and re-disabling it could clobber scrollback.
case "$seq_out" in
    *'1049'*) fail "T14: reset sequence must not touch alt-screen (?1049) — $(printf '%q' "$seq_out")" ;;
esac

# TTY guard: stdout here is a command substitution (a pipe, not a tty), so the
# guarded entry point must emit nothing.
guarded="$(host_reset_terminal)"
[[ -z "$guarded" ]] || fail "T14: host_reset_terminal leaked bytes to a non-tty stdout — $(printf '%q' "$guarded")"
ok "T14: host_reset_terminal disables leaked mouse-tracking and is a no-op on non-tty stdout"

echo
echo "HOST TEST PASSED"
