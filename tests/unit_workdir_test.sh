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

echo
echo "WORKDIR TEST PASSED"
