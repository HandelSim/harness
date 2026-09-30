#!/usr/bin/env bash
#
# tests/unit_win_location_test.sh — on Windows, harness wants to live inside the
# user profile: managed PCs often run programs only from there, and host mode
# runs everything it downloads from <install-root>/state/host.
#
# Docker-free and network-free. The installer's check is a top-level block, so
# it is cut out of harness-install.sh and eval'd with stubs (the real installer
# would go on to clone); `harness` is sourced with HARNESS_SOURCE_ONLY=1.
#
#   - T1: the installer's USERPROFILE → Git Bash path conversion (no cygpath)
#         and its case-insensitive "inside" test, which must not match a
#         sibling that merely shares a prefix (/c/Users/me2 vs /c/Users/me).
#   - T2: install outside the profile, no tty: warns, keeps the location.
#         Inside the profile: says nothing. Not Windows: says nothing.
#   - T3: outside, on a tty, Enter: switches the install to <profile>/harness
#         (needs util-linux `script` for a pty; skipped without it).
#   - T4: harness's host_win_location_hint: outside → the note plus the move
#         commands; inside or on Linux → silent. cmd_host calls it when the
#         toolchain fails to provision or run.
#
# Prints "WIN LOCATION TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALLER="$REPO_ROOT/harness-install.sh"
HARNESS="$REPO_ROOT/harness"

echo "============================================================"
echo " windows install-location test"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The installer's location block, helpers included, as a standalone script.
BLOCK="$TMP/block.sh"
awk '/^# --- Windows: keep the install inside the user folder/,/^unset _win_profile _loc_ans/' \
    "$INSTALLER" >"$BLOCK"
grep -q '_inline_win_profile_dir()' "$BLOCK" || fail "location block not found in the installer"

# Run the block with a stubbed OS, profile and cwd; prints output then the
# resulting install_root. PATH hides cygpath so the manual conversion runs.
run_block() {  # $1 os, $2 USERPROFILE, $3 cwd
    CLONE_DIR=harness bash -c '
        set -euo pipefail
        _os="$1"; _inline_detect_os() { echo "$_os"; }
        command() { [[ "${1:-} ${2:-}" == "-v cygpath" ]] && return 1; builtin command "$@"; }
        cwd="$3"; install_root="$cwd/$CLONE_DIR"
        USERPROFILE="$2"
        source "$0"
        echo "ROOT=$install_root"
    ' "$BLOCK" "$1" "$2" "$3"
}

# --- T1: helpers ---------------------------------------------------------------
helpers="$(awk '/^_inline_win_profile_dir\(\)/,/^}/; /^_inline_path_within\(\)/,/^}/' "$INSTALLER")"
got="$(bash -c "$helpers"'
    command() { [[ "${1:-} ${2:-}" == "-v cygpath" ]] && return 1; builtin command "$@"; }
    USERPROFILE="C:\\Users\\Me\\" _inline_win_profile_dir')"
[[ "$got" == "/c/Users/Me" ]] || fail "T1: profile conversion gave '$got', want /c/Users/Me"
bash -c "$helpers"'; _inline_path_within /c/users/me/harness /c/Users/Me' \
    || fail "T1: a path inside the profile (other case) was not matched"
bash -c "$helpers"'; _inline_path_within /c/Users/Me /c/Users/Me' \
    || fail "T1: the profile itself was not matched"
! bash -c "$helpers"'; _inline_path_within /c/Users/Me2/harness /c/Users/Me' \
    || fail "T1: a sibling sharing the prefix was matched"
ok "T1: profile conversion and case-insensitive containment (no prefix false-positive)"

# --- T2: non-interactive -----------------------------------------------------------
out="$(run_block windows 'C:\Users\Me' /c/HandelAI </dev/null 2>&1)"
grep -q "outside your Windows user folder" <<<"$out" || fail "T2: no warning outside the profile — $out"
grep -q "no terminal to ask" <<<"$out" || fail "T2: should say it can't ask — $out"
grep -q "^ROOT=/c/HandelAI/harness$" <<<"$out" || fail "T2: install root changed without asking — $out"
out="$(run_block windows 'C:\Users\Me' /c/Users/me/tools </dev/null 2>&1)"
! grep -q "outside your Windows user folder" <<<"$out" || fail "T2: warned inside the profile — $out"
out="$(run_block linux 'C:\Users\Me' /opt </dev/null 2>&1)"
! grep -q "outside your Windows user folder" <<<"$out" || fail "T2: warned on Linux — $out"
ok "T2: outside the profile without a tty warns and keeps the location; inside/Linux is silent"

# --- T3: interactive, Enter accepts the profile ------------------------------------------
if command -v script >/dev/null 2>&1 && script -qc true /dev/null >/dev/null 2>&1; then
    declare -f run_block >"$TMP/rb.sh"
    printf 'BLOCK=%q\nsource %q\nrun_block windows %q /c/HandelAI\n' \
        "$BLOCK" "$TMP/rb.sh" 'C:\Users\Me' >"$TMP/t3.sh"
    out="$(printf '\n' | script -qc "bash $TMP/t3.sh" /dev/null 2>&1 | tr -d '\r')"
    grep -q "instead (recommended)? \[Y/n\]" <<<"$out" || fail "T3: no prompt on a tty — $out"
    grep -q "^ROOT=/c/Users/Me/harness$" <<<"$out" || fail "T3: Enter did not switch to the profile — $out"
    ok "T3: on a tty, Enter installs into <profile>/harness"
else
    ok "T3: skipped (no util-linux script for a pty)"
fi

# --- T4: harness runtime hint ----------------------------------------------------------------
hint() {  # $1 os, $2 install_root; prints the hint's stderr
    local _hos="$1" _hroot="$2"
    # shellcheck disable=SC1090,SC2034  # USERPROFILE/install_root are read by the sourced code
    ( HARNESS_SOURCE_ONLY=1 source "$HARNESS" >/dev/null 2>&1
      harness_detect_os() { echo "$_hos"; }
      command() { [[ "${1:-} ${2:-}" == "-v cygpath" ]] && return 1; builtin command "$@"; }
      USERPROFILE='C:\Users\Me'; install_root="$_hroot"
      host_win_location_hint 2>&1 )
}
out="$(hint windows /c/HandelAI/harness)"
grep -q "installed outside your Windows user folder (/c/HandelAI/harness)" <<<"$out" \
    || fail "T4: no note outside the profile — $out"
grep -q 'mv "/c/HandelAI/harness" "/c/Users/Me/harness"' <<<"$out" || fail "T4: no mv command — $out"
grep -qF "printf '#!/usr/bin/env bash\\nexec \"%s/harness\" \"\$@\"\\n' \"/c/Users/Me/harness\" > ~/.local/bin/harness" <<<"$out" \
    || fail "T4: the wrapper rewrite command is wrong — $out"
out="$(hint windows /C/users/me/harness)"
[[ -z "$out" ]] || fail "T4: a note inside the profile — $out"
out="$(hint linux /opt/harness)"
[[ -z "$out" ]] || fail "T4: a note on Linux — $out"
grep -q 'host_ensure_toolchain || { host_win_location_hint; exit 1; }' "$HARNESS" \
    || fail "T4: cmd_host does not hint on a provisioning failure"
grep -q 'host_preflight        || { host_win_location_hint; exit 1; }' "$HARNESS" \
    || fail "T4: cmd_host does not hint on a preflight failure"
ok "T4: host_win_location_hint notes an out-of-profile install with the move commands; cmd_host calls it on failure"

echo
echo "WIN LOCATION TEST PASSED"
