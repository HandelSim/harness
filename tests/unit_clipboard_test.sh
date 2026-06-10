#!/usr/bin/env bash
#
# tests/unit_clipboard_test.sh — docker-free coverage for the clipboard bridge.
#
# Two surfaces, both testable without docker or a real clipboard:
#   1. agents/clipboard-bridge.sh — the container-side shim symlinked as
#      xclip/xsel/wl-copy. Copy mode base64-encodes stdin to the bridge file;
#      read/paste mode (-o / wl-paste) is a no-op success; with no bridge file
#      set it swallows stdin and exits 0 (inert, default behavior unchanged).
#   2. harness_host_clipboard_cmd in scripts/lib/platform.sh — picks the host
#      clipboard command per OS, gated on the tool being present. We drive each
#      OS branch with a stubbed harness_detect_os and a PATH of fake tools.
#
# Pure unit test — no docker, no network, no real clipboard. Sourced/run from a
# fresh shell.
#
# Prints "CLIPBOARD TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHIM="${REPO_ROOT}/agents/clipboard-bridge.sh"

echo "============================================================"
echo " clipboard bridge test"
echo "============================================================"

fail() { echo "[clipboard-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[clipboard-test] OK: $*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/harness-cliptest.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

[[ -f "$SHIM" ]] || fail "shim not found at $SHIM"
# Not asserting the exec bit: like agents/entrypoint.sh, the shim ships mode
# 0644 in git and is chmod +x'd at image build. We invoke it via `bash "$SHIM"`.

# --- T1: copy mode writes base64(stdin), single line, to the bridge file -----
bridge="${WORK}/bridge"
: >"$bridge"
payload=$'hello world\nsecond line\ttab'
HARNESS_CLIPBOARD_FILE="$bridge" printf '%s' "$payload" | HARNESS_CLIPBOARD_FILE="$bridge" bash "$SHIM"
got="$(cat "$bridge")"
want="$(printf '%s' "$payload" | base64 | tr -d '\n')"
[[ "$got" == "$want" ]] || fail "T1: bridge file = '$got', expected base64 '$want'"
# Must be exactly one line (no trailing newline inside the framed value).
lines="$(wc -l <"$bridge")"
[[ "$lines" -eq 0 ]] || fail "T1: bridge file has $lines newline(s); expected 0 (single unterminated line)"
# And it must round-trip back to the original bytes.
back="$(printf '%s' "$got" | base64 -d)"
[[ "$back" == "$payload" ]] || fail "T1: base64 decode did not round-trip"
ok "T1: copy mode writes single-line base64 that round-trips"

# --- T2: copy mode overwrites in place (truncate-write keeps the inode) -------
bridge2="${WORK}/bridge2"
printf 'STALE-LONGER-PREVIOUS-VALUE' >"$bridge2"
inode_before="$(ls -i "$bridge2" | awk '{print $1}')"
HARNESS_CLIPBOARD_FILE="$bridge2" printf '%s' "x" | HARNESS_CLIPBOARD_FILE="$bridge2" bash "$SHIM"
inode_after="$(ls -i "$bridge2" | awk '{print $1}')"
[[ "$inode_before" == "$inode_after" ]] || fail "T2: inode changed ($inode_before -> $inode_after); bind mount would break"
got2="$(cat "$bridge2")"
want2="$(printf '%s' "x" | base64 | tr -d '\n')"
[[ "$got2" == "$want2" ]] || fail "T2: stale value not fully overwritten (got '$got2')"
ok "T2: copy mode overwrites in place, inode preserved"

# --- T3: bridge disabled (no env) -> swallow stdin, exit 0, write nothing -----
out_sink="${WORK}/should-not-exist"
set +e
# No HARNESS_CLIPBOARD_FILE in env. Shim must consume stdin and exit 0.
printf 'discard me' | bash "$SHIM"
rc=$?
set -e
[[ "$rc" -eq 0 ]] || fail "T3: disabled shim exited $rc, expected 0"
[[ ! -e "$out_sink" ]] || fail "T3: disabled shim created a file"
ok "T3: bridge disabled is an inert no-op (exit 0, no write)"

# --- T4: read/paste mode is a no-op success, never touches the file ----------
# xclip/xsel read with -o; wl-paste reads by name. None should write the file.
bridge4="${WORK}/bridge4"
: >"$bridge4"
for arg in -o --output; do
    set +e
    HARNESS_CLIPBOARD_FILE="$bridge4" printf 'paste-req' | HARNESS_CLIPBOARD_FILE="$bridge4" bash "$SHIM" "$arg"
    rc=$?
    set -e
    [[ "$rc" -eq 0 ]] || fail "T4: read mode ($arg) exited $rc, expected 0"
    [[ ! -s "$bridge4" ]] || fail "T4: read mode ($arg) wrote to the bridge file"
done
# wl-paste is detected by basename, so invoke the shim under that name.
ln -sf "$SHIM" "${WORK}/wl-paste"
set +e
HARNESS_CLIPBOARD_FILE="$bridge4" printf 'paste-req' | HARNESS_CLIPBOARD_FILE="$bridge4" bash "${WORK}/wl-paste"
rc=$?
set -e
[[ "$rc" -eq 0 ]] || fail "T4: wl-paste exited $rc, expected 0"
[[ ! -s "$bridge4" ]] || fail "T4: wl-paste wrote to the bridge file"
ok "T4: read/paste mode (-o, --output, wl-paste) is a no-op success"

# --- T5: harness_host_clipboard_cmd picks the right tool per OS --------------
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# Build a sandbox PATH of fake clipboard tools so command -v finds exactly the
# ones we choose to "install". keep coreutils reachable via the real PATH tail.
fakebin="${WORK}/fakebin"
mkdir -p "$fakebin"
mk_tool() { printf '#!/bin/sh\nexit 0\n' >"${fakebin}/$1"; chmod +x "${fakebin}/$1"; }
rm_tool() { rm -f "${fakebin}/$1"; }

real_path="$PATH"
with_fakebin() { PATH="${fakebin}:${real_path}"; }
restore_path() { PATH="$real_path"; }

assert_cmd() {
    local label="$1" expect="$2" got
    got="$(harness_host_clipboard_cmd)" || got="<none>"
    [[ "$got" == "$expect" ]] || fail "T5/$label: got '$got', expected '$expect'"
    ok "T5/$label: $got"
}
assert_none() {
    local label="$1"
    if harness_host_clipboard_cmd >/dev/null 2>&1; then
        fail "T5/$label: expected no tool found (rc!=0) but one was returned"
    fi
    ok "T5/$label: no tool -> non-zero"
}

# Windows -> clip.exe
harness_detect_os() { printf '%s' windows; }
with_fakebin; mk_tool clip.exe
assert_cmd "windows-clip.exe" "clip.exe"
rm_tool clip.exe

# macOS -> pbcopy
harness_detect_os() { printf '%s' macos; }
mk_tool pbcopy
assert_cmd "macos-pbcopy" "pbcopy"
rm_tool pbcopy

# Linux X11 (no WAYLAND_DISPLAY) -> xclip preferred over xsel
harness_detect_os() { printf '%s' linux; }
WAYLAND_DISPLAY="" ; export WAYLAND_DISPLAY=""
mk_tool xclip; mk_tool xsel
assert_cmd "linux-xclip" "xclip -selection clipboard"
rm_tool xclip
# With only xsel present -> xsel
assert_cmd "linux-xsel" "xsel --clipboard --input"
rm_tool xsel

# Linux Wayland -> wl-copy when WAYLAND_DISPLAY set and wl-copy present
mk_tool wl-copy; mk_tool xclip
WAYLAND_DISPLAY="wayland-0" ; export WAYLAND_DISPLAY="wayland-0"
assert_cmd "linux-wayland" "wl-copy"
# Wayland var set but wl-copy absent -> falls back to xclip
rm_tool wl-copy
assert_cmd "linux-wayland-fallback" "xclip -selection clipboard"
rm_tool xclip
unset WAYLAND_DISPLAY

# No tools installed -> non-zero
assert_none "linux-none"

restore_path
unset -f harness_detect_os

echo
echo "CLIPBOARD TEST PASSED"
