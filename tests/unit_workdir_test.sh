#!/usr/bin/env bash
#
# tests/unit_workdir_test.sh — exercise the cross-platform helpers that
# resolve a host CWD into a container working directory:
#   - harness_abs_path        (POSIX form on every OS)
#   - harness_container_workdir (Windows: `//c/...` MSYS escape; else pass-through)
#
# Regression for issue #112: on Windows Git Bash, `docker run -w /c/foo`
# was being silently rewritten by MSYS argv conversion to `-w C:/foo`, and
# the Linux docker daemon rejected it with "the working directory is
# invalid, it needs to be an absolute path". The fix is `//c/foo` (MSYS
# leaves `//` paths alone; Linux normalises `//foo` to `/foo` on chdir).
#
# Also covers I035 from tests/COVERAGE.md (was: red).
#
# Pure unit test — no docker, no network. Sourced from a fresh shell.
# Prints "WORKDIR TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo " workdir / abs_path test"
echo "============================================================"

fail() { echo "[workdir-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[workdir-test] OK: $*"; }

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

# --- T1: harness_container_workdir is a pass-through on non-Windows ----------
harness_detect_os() { printf '%s' linux; }
export -f harness_detect_os

got=$(harness_container_workdir "/home/me/proj")
[[ "$got" == "/home/me/proj" ]] || fail "T1: linux pass-through expected /home/me/proj, got '$got'"
ok "T1: linux pass-through"

harness_detect_os() { printf '%s' macos; }
export -f harness_detect_os
got=$(harness_container_workdir "/Users/me/proj")
[[ "$got" == "/Users/me/proj" ]] || fail "T1: macos pass-through expected /Users/me/proj, got '$got'"
ok "T1: macos pass-through"

# --- T2: harness_container_workdir adds the // escape on Windows -------------
harness_detect_os() { printf '%s' windows; }
export -f harness_detect_os

got=$(harness_container_workdir "/c/Developer/HandelAI/harness")
[[ "$got" == "//c/Developer/HandelAI/harness" ]] || fail "T2: expected //c/Developer/HandelAI/harness, got '$got'"
ok "T2: windows /c/... → //c/..."

got=$(harness_container_workdir "/c/Folder")
[[ "$got" == "//c/Folder" ]] || fail "T2: expected //c/Folder, got '$got'"
ok "T2: windows /c/Folder → //c/Folder"

# Idempotence: if a caller somehow re-runs it, we don't pile up slashes.
got=$(harness_container_workdir "//c/Folder")
[[ "$got" == "//c/Folder" ]] || fail "T2: expected //c/Folder (idempotent), got '$got'"
ok "T2: windows idempotent on //c/..."

# Triple-slash input still normalises to exactly two.
got=$(harness_container_workdir "///c/Folder")
[[ "$got" == "//c/Folder" ]] || fail "T2: expected //c/Folder (collapsed), got '$got'"
ok "T2: windows collapses extra leading slashes"

# --- T3: harness_abs_path normalises /c/..., C:/..., c:\... to /c/... on Windows
#
# This covers I035: harness_abs_path translates `/c/Users/...` form back to
# POSIX on Git Bash. We pin OS=windows, stub harness_realpath so we don't
# need a real Windows filesystem, and exercise the cygpath -u fast path.
harness_detect_os() { printf '%s' windows; }
export -f harness_detect_os

# Fake cygpath that mimics `cygpath -u` for the inputs we feed it.
fake_cygpath_dir=$(mktemp -d)
cat >"$fake_cygpath_dir/cygpath" <<'CYG'
#!/usr/bin/env bash
# Minimal stand-in for cygpath -u: normalise C:/foo, c:\foo, /c/foo → /c/foo.
mode="$1"; shift
p="$1"
if [[ "$mode" == "-u" ]]; then
    # Backslash → forward slash
    p="${p//\\//}"
    # C:/foo or c:/foo → /c/foo
    if [[ "$p" =~ ^([A-Za-z]):/(.*)$ ]]; then
        drive="${BASH_REMATCH[1],,}"
        rest="${BASH_REMATCH[2]}"
        echo "/${drive}/${rest}"
    else
        echo "$p"
    fi
else
    echo "$p"
fi
CYG
chmod +x "$fake_cygpath_dir/cygpath"
trap 'rm -rf "$fake_cygpath_dir"' EXIT

# Make our stub win over any real cygpath on PATH for this test only.
PATH="$fake_cygpath_dir:$PATH"

# Stub realpath so we don't depend on the host having `/c/Users/...` on disk.
harness_realpath() { printf '%s' "$1"; }
export -f harness_realpath

for input in "C:/Users/allen/proj" "c:\\Users\\allen\\proj" "/c/Users/allen/proj"; do
    got=$(harness_abs_path "$input")
    [[ "$got" == "/c/Users/allen/proj" ]] \
        || fail "T3: input '$input' → expected /c/Users/allen/proj, got '$got'"
done
ok "T3: I035 — harness_abs_path normalises C:/..., c:\\..., /c/... → /c/... on Windows"

# --- T4: end-to-end — what docker -w would receive on Windows ----------------
#
# Compose harness_abs_path + harness_container_workdir the same way the
# launch sites do (`-w "$(harness_container_workdir "$mount_path")"` where
# `$mount_path` came from `harness_abs_path`). The output must start with
# `//` so MSYS's argv conversion leaves it alone, otherwise the Linux
# docker daemon rejects it with the issue #112 error.
for input in "C:/Folder" "c:\\Folder" "/c/Folder"; do
    mount_path=$(harness_abs_path "$input")
    w_arg=$(harness_container_workdir "$mount_path")
    [[ "$w_arg" == "//c/Folder" ]] \
        || fail "T4: input '$input' → expected docker -w //c/Folder, got '$w_arg'"
done
ok "T4: full pipeline (abs_path → container_workdir) → //c/Folder on Windows"

# --- T5: docker wrappers export MSYS conv toggles on Windows -----------------
#
# Issue #112 follow-up: the bind-mount `-v C:/foo:/c/foo` was being
# path-list-converted by MSYS to `C:\foo;C:\foo`, breaking docker's volume
# parser. Root-cause fix is to put MSYS_NO_PATHCONV=1 AND
# MSYS2_ARG_CONV_EXCL='*' into bash's *own* env (via `local -x`) for the
# duration of the wrapper call, so MSYS sees them at the moment of argv
# conversion. The old inline `VAR=val cmd` pattern set them in the child's
# env but conversion had already happened in the parent shell.
#
# This test asserts the wiring: on Windows the wrappers spawn the runtime
# with both vars set; on non-Windows neither is exported (no leakage).

recorder_dir=$(mktemp -d)
trap 'rm -rf "$fake_cygpath_dir" "$recorder_dir"' EXIT
cat >"$recorder_dir/fake-runtime" <<'REC'
#!/usr/bin/env bash
{
    echo "MSYS_NO_PATHCONV=${MSYS_NO_PATHCONV:-<unset>}"
    echo "MSYS2_ARG_CONV_EXCL=${MSYS2_ARG_CONV_EXCL:-<unset>}"
} >"$RECORD_FILE"
REC
chmod +x "$recorder_dir/fake-runtime"

harness_container_runtime() { printf '%s' "$recorder_dir/fake-runtime"; }
export -f harness_container_runtime
unset _harness_runtime_cache
export RECORD_FILE="$recorder_dir/out"

# Windows pin → both vars must reach the child.
harness_detect_os() { printf '%s' windows; }
export -f harness_detect_os
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
: >"$RECORD_FILE"
harness_docker run --rm alpine sh
got=$(<"$RECORD_FILE")
[[ "$got" == *"MSYS_NO_PATHCONV=1"* ]] \
    || fail "T5: harness_docker on Windows should set MSYS_NO_PATHCONV=1; recorder saw: $got"
[[ "$got" == *"MSYS2_ARG_CONV_EXCL=*"* ]] \
    || fail "T5: harness_docker on Windows should set MSYS2_ARG_CONV_EXCL='*'; recorder saw: $got"
ok "T5: harness_docker exports both MSYS conv toggles on Windows"

# `local -x` scope: vars must NOT leak into the caller's shell after return.
[[ -z "${MSYS_NO_PATHCONV:-}" ]] \
    || fail "T5: MSYS_NO_PATHCONV leaked out of harness_docker into the test shell"
[[ -z "${MSYS2_ARG_CONV_EXCL:-}" ]] \
    || fail "T5: MSYS2_ARG_CONV_EXCL leaked out of harness_docker into the test shell"
ok "T5: harness_docker does not leak MSYS conv vars into the caller (local -x scope)"

# Non-Windows pin → neither var is set by the wrapper.
harness_detect_os() { printf '%s' linux; }
export -f harness_detect_os
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
: >"$RECORD_FILE"
harness_docker run --rm alpine sh
got=$(<"$RECORD_FILE")
[[ "$got" == *"MSYS_NO_PATHCONV=<unset>"* ]] \
    || fail "T5: harness_docker on Linux should not set MSYS_NO_PATHCONV; recorder saw: $got"
[[ "$got" == *"MSYS2_ARG_CONV_EXCL=<unset>"* ]] \
    || fail "T5: harness_docker on Linux should not set MSYS2_ARG_CONV_EXCL; recorder saw: $got"
ok "T5: harness_docker is pass-through on non-Windows (no MSYS env leakage)"

# harness_docker_winpty: same expectation (Windows-only callers).
harness_detect_os() { printf '%s' windows; }
export -f harness_detect_os
# Stub winpty so we don't depend on it being installed in CI.
cat >"$recorder_dir/winpty" <<'WP'
#!/usr/bin/env bash
exec "$@"
WP
chmod +x "$recorder_dir/winpty"
PATH="$recorder_dir:$PATH"
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
: >"$RECORD_FILE"
harness_docker_winpty run --rm alpine sh
got=$(<"$RECORD_FILE")
[[ "$got" == *"MSYS_NO_PATHCONV=1"* && "$got" == *"MSYS2_ARG_CONV_EXCL=*"* ]] \
    || fail "T5: harness_docker_winpty should set both MSYS toggles; recorder saw: $got"
ok "T5: harness_docker_winpty exports both MSYS conv toggles"

# --- T6: harness_add_bind_mount emits --mount on Windows, -v elsewhere -------
#
# Issue #112 (third failure mode): the `-v C:/foo:/c/foo` composite is
# path-list-converted by MSYS *and* winpty into `C:\foo;C:\foo`, which
# docker's volume parser rejects with `invalid mode: \foo`. The env-var
# toggles (T5) fix the plain `harness_docker` path but NOT the winpty path,
# so the bind-mount arg itself is reshaped to the single-token
# `--mount=type=bind,source=,target=` form, which has no bare `:` for the
# converter to latch onto. This test asserts the arg shape on each OS.

# Linux/macOS → classic `-v src:tgt` (two tokens), `:ro` suffix for readonly.
harness_detect_os() { printf '%s' linux; }
export -f harness_detect_os

bm=()
harness_add_bind_mount bm "/host/proj" "/host/proj"
[[ "${bm[*]}" == "-v /host/proj:/host/proj" ]] \
    || fail "T6: linux rw mount expected '-v /host/proj:/host/proj', got '${bm[*]}'"
ok "T6: linux read-write mount → -v src:tgt"

bm=()
harness_add_bind_mount bm "/host/allow" "/etc/harness/allowlist" ro
[[ "${bm[*]}" == "-v /host/allow:/etc/harness/allowlist:ro" ]] \
    || fail "T6: linux ro mount expected '-v …:ro', got '${bm[*]}'"
ok "T6: linux read-only mount → -v src:tgt:ro"

# Windows → single `--mount=type=bind,…` token (no bare `:` to convert),
# source kept in Windows mixed form, `,readonly` for read-only.
harness_detect_os() { printf '%s' windows; }
export -f harness_detect_os

bm=()
harness_add_bind_mount bm "C:/Developer/folder/share" "/c/Developer/folder/share"
[[ ${#bm[@]} -eq 1 ]] \
    || fail "T6: windows mount should be a single token, got ${#bm[@]}: '${bm[*]}'"
[[ "${bm[0]}" == "--mount=type=bind,source=C:/Developer/folder/share,target=/c/Developer/folder/share" ]] \
    || fail "T6: windows rw mount unexpected, got '${bm[0]}'"
# No bare colon-joined `src:tgt` composite for MSYS/winpty to path-list-convert.
[[ "${bm[0]}" != *":/c/"* ]] \
    || fail "T6: windows mount must not contain a ':/c/' composite, got '${bm[0]}'"
ok "T6: windows read-write mount → single --mount=type=bind token"

bm=()
harness_add_bind_mount bm "C:/Developer/HandelAI/harness/.harness-allowlist" "/etc/harness/allowlist" ro
[[ "${bm[0]}" == "--mount=type=bind,source=C:/Developer/HandelAI/harness/.harness-allowlist,target=/etc/harness/allowlist,readonly" ]] \
    || fail "T6: windows ro mount expected ',readonly' suffix, got '${bm[0]}'"
ok "T6: windows read-only mount → --mount=…,readonly"

# Reset the OS stub so nothing downstream inherits the Windows pin.
harness_detect_os() { printf '%s' linux; }
export -f harness_detect_os

echo
echo "WORKDIR TEST PASSED"
