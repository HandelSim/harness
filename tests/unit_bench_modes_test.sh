#!/usr/bin/env bash
#
# tests/unit_bench_modes_test.sh — `harness benchmark --test-modes`, docker-free.
#
#   T1: the python runner's own unit tests (stats, plan, proxy-log counters,
#       every task checker against its seed + mock solution, the mock's
#       script, env scrubbing, redaction, report)
#   T2: cmd_benchmark routes --test-modes (anywhere in argv) to the runner,
#       minus the flag itself, and leaves the harbor targets alone
#   T3: the runner's --list works through the real entry point
#   T4: HARNESS_HOST_PORT overrides the host proxy port (and .env's PROXY_PORT)
#   T5: HARNESS_HOST_NO_WEB=1 denies webfetch/websearch and disables sharing in
#       the host opencode config; without it the config is unchanged; the
#       launcher drops OPENCODE_ENABLE_EXA under it
#
# Run:  bash tests/unit_bench_modes_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " harness benchmark --test-modes unit test"
echo "============================================================"

pass=0
fail() { echo "[bench-modes] FAIL: $*" >&2; exit 1; }
ok()   { echo "[bench-modes] OK: $*"; pass=$((pass + 1)); }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
: >"$TMP_ROOT/.harness-allowlist"
cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://api.example.com
PROXY_API_KEY=sk-test-key
DEFAULT_MODEL_NAME=test-model
PROXY_PORT=8123
EOF

# --- T1: python unit tests ----------------------------------------------------
( cd "$REPO_ROOT" && python3 -m unittest tests/benchmarks/modes/test_modes.py ) \
    || fail "T1: tests/benchmarks/modes/test_modes.py"
ok "T1: runner stats, plan, log counters, checkers, mock, env, redaction, report"

src() {
    # shellcheck disable=SC1090
    HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" \
        HARNESS_ALLOWLIST_PATH="$TMP_ROOT/.harness-allowlist" source "$HARNESS" >/dev/null 2>&1
    clone_dir="$REPO_ROOT"
}

# --- T2: routing ---------------------------------------------------------------
t2=$(
    unset CI
    src
    _harness_bench_test_modes() { echo "ROUTED=[$*]"; }
    _harness_run_bench_target() { echo "HARBOR=[$1]"; }
    cmd_benchmark --test-modes --repeats 2 --mock
    cmd_benchmark --repeats 5 --test-modes
)
grep -q 'ROUTED=\[--test-modes --repeats 2 --mock\]' <<<"$t2" || fail "T2: leading flag — $t2"
grep -q 'ROUTED=\[--repeats 5 --test-modes\]' <<<"$t2" || fail "T2: trailing flag — $t2"
grep -q 'HARBOR' <<<"$t2" && fail "T2: --test-modes fell through to a harbor runner — $t2"
# the helper itself strips the flag before handing argv to run.py
grep -q '\[\[ "$a" == "--test-modes" \]\] || args+=("$a")' \
    < <(sed -n '/^_harness_bench_test_modes()/,/^}/p' "$HARNESS") \
    || fail "T2: _harness_bench_test_modes does not strip --test-modes"
ok "T2: cmd_benchmark routes --test-modes to the python runner"

# --- T3: --list through the real entry point ----------------------------------
t3=$(
    src
    _harness_bench_test_modes --test-modes --list --repeats 2
) || fail "T3: --list exited non-zero — $t3"
grep -q '24 trials (6 tasks x 2 modes x 2 repeats)' <<<"$t3" || fail "T3: --list plan — $t3"
grep -q '^fix-bug ' <<<"$t3" || fail "T3: --list tasks — $t3"
ok "T3: harness benchmark --test-modes --list prints the tasks and the plan"

# --- T4: HARNESS_HOST_PORT ------------------------------------------------------
t4=$( src; host_proxy_port; echo; HARNESS_HOST_PORT=45678 host_proxy_port )
[[ "$t4" == $'8123\n45678' ]] || fail "T4: host_proxy_port — got [$t4]"
ok "T4: HARNESS_HOST_PORT overrides .env's PROXY_PORT"

# --- T5: HARNESS_HOST_NO_WEB -------------------------------------------------------
if command -v jq >/dev/null 2>&1; then
    cfg_for() {
        (
            src
            export HARNESS_HOST_PORT=1   # nothing listens: the model list falls back
            [[ -n "$1" ]] && export HARNESS_HOST_NO_WEB="$1"
            host_write_opencode_config
            cat "$(host_opencode_config)"
        )
    }
    on=$(cfg_for 1)
    off=$(cfg_for "")
    [[ "$(jq -r '.agent.yolo.permission.webfetch' <<<"$on")" == "deny" ]] || fail "T5: webfetch not denied — $on"
    [[ "$(jq -r '.agent.yolo.permission.websearch' <<<"$on")" == "deny" ]] || fail "T5: websearch not denied"
    [[ "$(jq -r '.share' <<<"$on")" == "disabled" ]] || fail "T5: share not disabled — $on"
    [[ "$(jq -r '.agent.yolo.permission.webfetch' <<<"$off")" == "allow" ]] || fail "T5: default lost webfetch"
    [[ "$(jq -r 'has("share")' <<<"$off")" == "false" ]] || fail "T5: default config gained a share key"
else
    echo "[bench-modes] (jq not installed; config half of T5 skipped)"
fi
grep -q 'HARNESS_HOST_NO_WEB:-0}" == "1" ]] && unset OPENCODE_ENABLE_EXA' \
    < <(sed -n '/^host_run_opencode()/,/^}/p' "$HARNESS") \
    || fail "T5: host_run_opencode keeps Exa on under HARNESS_HOST_NO_WEB"
ok "T5: HARNESS_HOST_NO_WEB turns off web tools, Exa and sharing; default unchanged"

# --- T6: a Windows-form HARNESS_INSTALL_ROOT ----------------------------------------
# The runner (a Windows python) passes C:/... ; the drive colon would split the
# toolchain dirs host mode prepends to PATH, so jq "vanishes" after provisioning.
t6dir=$(mktemp -d); printf '#!/bin/sh\necho MINGW64_NT-10.0\n' >"$t6dir/uname"; chmod +x "$t6dir/uname"
t6=$(PATH="$t6dir:$PATH" HARNESS_INSTALL_ROOT='C:/Users/me/h\state\bench-modes\root' HARNESS_SOURCE_ONLY=1 \
    bash -c 'source "$1" >/dev/null 2>&1; printf "%s|%s" "$install_root" "$(host_tool_bin_dir)"' _ "$HARNESS")
t6l=$(HARNESS_INSTALL_ROOT='/srv/h' HARNESS_SOURCE_ONLY=1 \
    bash -c 'source "$1" >/dev/null 2>&1; printf "%s" "$install_root"' _ "$HARNESS")
rm -rf "$t6dir"
[[ "$t6" == "/c/Users/me/h/state/bench-modes/root|/c/Users/me/h/state/bench-modes/root/state/host/toolchain/bin" ]] \
    || fail "T6: Windows install root not normalized — got [$t6]"
[[ "$t6l" == "/srv/h" ]] || fail "T6: a Linux install root changed — got [$t6l]"
ok "T6: a C:/... HARNESS_INSTALL_ROOT becomes /c/... on Windows (no drive colon in PATH)"

echo "[bench-modes] all $pass checks passed"
