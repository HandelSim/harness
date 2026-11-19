#!/usr/bin/env bash
#
# tests/unit_desktop_cmd_test.sh — `harness desktop` (build, install, launch the
# Electron app in desktop/) without building anything.
#
# Docker-free and network-free. The real `harness` and scripts/lib/platform.sh
# are copied into a throwaway git clone with a stub desktop/ (package.json,
# package-lock.json, build/icon.png), so the build stamp is a real
# `git rev-parse HEAD:./desktop` and nothing touches the repo's desktop/.
# Every external tool the command runs (node, npm, npx, cygpath, cmd.exe, open,
# ditto, codesign, xattr, setsid, uname -m) is a stub on PATH that appends its
# argv to a log; PATH has no real node/npm/npx at all. The stub npx "packages"
# by creating the output electron-builder would; the stub Windows installer
# creates Harness.exe under $LOCALAPPDATA/Programs/Harness.
#
#   - T1: not installed -> npm ci, build, electron-builder --linux dir, copy,
#         .desktop + icon, stamp, then a detached launch (setsid -f)
#   - T2: current stamp -> launch only, nothing built
#   - T3: `install` again with the same package-lock.json -> no npm ci
#   - T4: desktop/ changed (new commit) -> rebuild, then launch, new stamp
#   - T5: node missing / node 20 -> clear error with an install hint, non-zero,
#         no stamp, nothing built
#   - T6: build / empty build / electron-builder failure keeps the old install and stamp
#   - T7: windows: --win nsis --x64 (ARM64 -> --arm64), the installer run with
#         /S unconverted, launch via `cmd.exe /c start "" <C:\ path>`
#   - T8: macos: --mac dir, ditto to a temp name, ad-hoc codesign, swap into
#         ~/Applications, `open -a`
#   - T9: linux .desktop Exec/Icon/StartupWMClass and the --no-sandbox rule
#         (AppArmor userns restriction or unprivileged_userns_clone=0)
#   - T10: `where` output for current / stale / not installed; no git ->
#          package.json version stamp, no rebuild loop; no display -> error
#
# Prints "DESKTOP CMD TEST PASSED" on success.

# SC2016: stub bodies are single-quoted on purpose (they expand when the stub
# runs). SC2015: `A && B || fail` is the intended "both must hold" assertion.
# shellcheck disable=SC2016,SC2015
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "============================================================"
echo " harness desktop command test (docker-free, no real build)"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
# In-place sed that works with GNU and BSD sed alike; cat keeps the file's mode.
subst() { sed "$1" "$2" >"$2.tmp" && cat "$2.tmp" >"$2" && rm -f "$2.tmp"; }
ok()   { echo "  ✓ $*"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/calls.log"
export LOG

# --- PATH: system tools minus node/npm/npx, plus the stubs ------------------
SYS="$TMP/sys"; mkdir -p "$SYS"
for d in /usr/local/bin /usr/bin /bin; do
    [[ -d "$d" ]] || continue
    for f in "$d"/*; do
        n=${f##*/}
        case "$n" in node|nodejs|npm|npx|corepack) continue ;; esac
        [[ -e "$SYS/$n" || ! -x "$f" ]] || ln -s "$f" "$SYS/$n"
    done
done
STUBS="$TMP/stubs"; mkdir -p "$STUBS"
stub() {  # $1 name, $2 body (bash) run after the argv is logged
    printf '#!/usr/bin/env bash\nprintf "%%s" "%s" >>"$LOG"; printf " [%%s]" "$@" >>"$LOG"; echo >>"$LOG"\n%s\n' \
        "$1" "$2" >"$STUBS/$1"
    chmod +x "$STUBS/$1"
}
stub node '[[ "${1:-}" == -v ]] && { echo "${STUB_NODE_VERSION:-v22.11.0}"; exit 0; }
[[ "${STUB_BUILD_RC:-0}" == 0 ]] || exit "$STUB_BUILD_RC"
[[ "${1:-}" != scripts/build.mjs || -n "${STUB_BUILD_EMPTY:-}" ]] || { mkdir -p out/main && echo built >out/main/index.cjs; }
exit 0'
stub npm '[[ "${1:-}" == ci ]] && mkdir -p node_modules
exit "${STUB_NPM_RC:-0}"'
stub npx 'echo "npx-env CSC_IDENTITY_AUTO_DISCOVERY=${CSC_IDENTITY_AUTO_DISCOVERY:-}" >>"$LOG"
[[ "${STUB_EB_RC:-0}" == 0 ]] || exit "$STUB_EB_RC"
arch=x64; for a in "$@"; do case "$a" in --arm64) arch=arm64 ;; esac; done
suf=""; [[ $arch == x64 ]] || suf="-$arch"
case " $* " in
  *" --linux "*) d="dist/linux$suf-unpacked"; mkdir -p "$d"
                 printf "#!/bin/sh\n" >"$d/harness-desktop"; chmod +x "$d/harness-desktop"
                 : >"$d/chrome-sandbox"; echo "${STUB_BUILD_ID:-b1}" >"$d/build-id" ;;
  *" --mac "*)   mkdir -p "dist/mac$suf/Harness.app/Contents/MacOS"
                 echo "${STUB_BUILD_ID:-b1}" >"dist/mac$suf/Harness.app/build-id" ;;
  *" --win "*)   mkdir -p dist; f="dist/Harness-0.1.0-win-$arch.exe"
                 printf "#!/usr/bin/env bash\nprintf \"installer\" >>\"\$LOG\"; printf \" [%%s]\" \"\$@\" >>\"\$LOG\"; echo \" MSYS_NO_PATHCONV=\${MSYS_NO_PATHCONV:-}\" >>\"\$LOG\"\nmkdir -p \"\$LOCALAPPDATA/Programs/Harness\" && : >\"\$LOCALAPPDATA/Programs/Harness/Harness.exe\"\n" >"$f"
                 chmod +x "$f" ;;
esac'
stub cygpath 'case "$1" in -w|-m) p="$2"; printf "C:%s\n" "${p//\//\\}" ;; *) echo "$2" ;; esac'
stub cmd.exe 'echo "cmd-env HARNESS_DESKTOP_ROOT=${HARNESS_DESKTOP_ROOT:-} MSYS_NO_PATHCONV=${MSYS_NO_PATHCONV:-}" >>"$LOG"'
stub open 'echo "open-env HARNESS_DESKTOP_ROOT=${HARNESS_DESKTOP_ROOT:-}" >>"$LOG"'
stub ditto 'cp -R "$1" "$2"'
stub codesign ':'
stub xattr ':'
stub setsid 'echo "setsid-env HARNESS_DESKTOP_ROOT=${HARNESS_DESKTOP_ROOT:-}" >>"$LOG"'
stub uname '[[ "${1:-}" == -m ]] && { echo "${STUB_ARCH:-x86_64}"; exit 0; }; exec "'"$SYS"'/uname" "$@"'
# uname is called a lot (harness_detect_os); keep it out of the log.
subst 's/^printf "%s" "uname".*$//' "$STUBS/uname"
export PATH="$STUBS:$SYS"
command -v node | grep -q "^$STUBS/" || fail "setup: the node stub is not first on PATH"

# --- a throwaway clone --------------------------------------------------------
GIT=(git -c user.name=t -c user.email=t@t -c commit.gpgsign=false -c init.defaultBranch=main)
make_clone() {  # $1 dir, $2 "git" to make it a repo
    local c="$1"
    mkdir -p "$c/scripts/lib" "$c/desktop/build" "$c/desktop/scripts"
    cp "$REPO_ROOT/harness" "$c/harness"
    cp "$REPO_ROOT/scripts/lib/platform.sh" "$c/scripts/lib/platform.sh"
    printf '{\n  "name": "harness-desktop",\n  "productName": "Harness",\n  "version": "0.1.0"\n}\n' >"$c/desktop/package.json"
    printf '{ "lockfileVersion": 3 }\n' >"$c/desktop/package-lock.json"
    printf 'PNG' >"$c/desktop/build/icon.png"
    : >"$c/desktop/scripts/build.mjs"
    if [[ "${2:-}" == git ]]; then
        (cd "$c" && "${GIT[@]}" init -q && "${GIT[@]}" add -A && "${GIT[@]}" commit -qm init)
    fi
}
C="$TMP/clone"; make_clone "$C" git
export HOME="$TMP/home"; mkdir -p "$HOME"
unset XDG_DATA_HOME HARNESS_INSTALL_ROOT PROCESSOR_ARCHITEW6432 MSYS_NO_PATHCONV 2>/dev/null || true
export DISPLAY=:0 LOCALAPPDATA="$HOME/AppData/Local"
PROC="$TMP/proc"; mkdir -p "$PROC"; echo 0 >"$PROC/apparmor_restrict_unprivileged_userns"
export HARNESS_DESKTOP_PROC_SYS="$PROC"

OUT=""; RC=0
run() {  # run harness in clone $C with extra env; sets OUT and RC
    : >"$LOG"
    RC=0
    OUT="$(env "$@" bash "$C/harness" desktop "${ARGS[@]}" 2>&1)" || RC=$?
}
ARGS=()
DATA="$HOME/.local/share"
APP="$DATA/harness-desktop/app/harness-desktop"
STAMP() { cat "$C/state/desktop/installed" 2>/dev/null || true; }
tree() { git -C "$C" rev-parse HEAD:desktop; }

# --- T1: not installed -> install, then launch --------------------------------
ARGS=(); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] || fail "T1: exit $RC — $OUT"
grep -q "not installed yet" <<<"$OUT" || fail "T1: no 'not installed' line — $OUT"
grep -qx 'npm \[ci\]' "$LOG" || fail "T1: npm ci not run — $(cat "$LOG")"
grep -qx 'node \[scripts/build.mjs\] \[--prod\]' "$LOG" || fail "T1: build not run — $(cat "$LOG")"
grep -qx 'npx \[--no-install\] \[electron-builder\] \[--linux\] \[dir\] \[--x64\] \[--publish\] \[never\]' "$LOG" \
    || fail "T1: electron-builder args — $(cat "$LOG")"
[[ -x "$APP" ]] || fail "T1: app not copied to $APP"
[[ "$(STAMP)" == "tree:$(tree)" ]] || fail "T1: stamp '$(STAMP)' != tree:$(tree)"
[[ -s "$C/state/desktop/lock-hash" ]] || fail "T1: no lock hash stamp"
grep -qx "setsid \[-f\] \[$APP\]" "$LOG" || fail "T1: not launched via setsid -f — $(cat "$LOG")"
grep -qx "setsid-env HARNESS_DESKTOP_ROOT=$C" "$LOG" || fail "T1: HARNESS_DESKTOP_ROOT not passed to the launch"
order="$(grep -o '^npm\|^node \[scripts\|^npx \[\|^setsid \[' "$LOG" | tr '\n' ,)"
[[ "$order" == "npm,node [scripts,npx [,setsid [," ]] || fail "T1: wrong order: $order"
ok "T1: not installed -> npm ci, build, electron-builder, copy, stamp, then setsid -f launch"

# --- T2: current -> launch only -------------------------------------------------
ARGS=(start); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] || fail "T2: exit $RC — $OUT"
! grep -q '^npm \|^node \[scripts\|^npx ' "$LOG" || fail "T2: built although current — $(cat "$LOG")"
grep -q "^setsid \[-f\] \[$APP\]" "$LOG" || fail "T2: not launched"
ok "T2: current stamp -> launch only, nothing built"

# --- T3: install again, same lock -> no npm ci -----------------------------------
ARGS=(install); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] || fail "T3: exit $RC — $OUT"
! grep -q '^npm ' "$LOG" || fail "T3: npm ci ran with an unchanged lock — $(cat "$LOG")"
grep -q '^npx \[--no-install\] \[electron-builder\]' "$LOG" || fail "T3: no package step"
! grep -q '^setsid' "$LOG" || fail "T3: install launched the app"
echo '{ "lockfileVersion": 3, "x": 1 }' >"$C/desktop/package-lock.json"
run HARNESS_DESKTOP_OS=linux
grep -qx 'npm \[ci\]' "$LOG" || fail "T3: npm ci skipped after the lock changed"
ok "T3: npm ci skipped while package-lock.json is unchanged, run when it changes; install does not launch"

# --- T4: desktop/ changed -> rebuild --------------------------------------------
subst 's/0.1.0/0.1.1/' "$C/desktop/package.json"
(cd "$C" && "${GIT[@]}" commit -qam bump)
[[ "$(STAMP)" != "tree:$(tree)" ]] || fail "T4: setup: stamp already matches"
ARGS=(); run HARNESS_DESKTOP_OS=linux STUB_BUILD_ID=b2
[[ $RC == 0 ]] || fail "T4: exit $RC — $OUT"
grep -q "changed since the installed app was built" <<<"$OUT" || fail "T4: no stale line — $OUT"
grep -q '^npx \[--no-install\] \[electron-builder\]' "$LOG" || fail "T4: not rebuilt"
grep -q '^setsid \[-f\]' "$LOG" || fail "T4: not launched after the rebuild"
[[ "$(cat "$DATA/harness-desktop/app/build-id")" == b2 ]] || fail "T4: new build not installed"
[[ "$(STAMP)" == "tree:$(tree)" ]] || fail "T4: stamp not updated"
[[ ! -e "$DATA/harness-desktop/app.old" && ! -e "$DATA/harness-desktop/app.new" ]] || fail "T4: temp dirs left"
ok "T4: stale stamp (desktop/ committed) -> rebuild, swap, new stamp, launch"

# --- T5: node missing / too old -------------------------------------------------
C2="$C"; C="$TMP/clone5"; make_clone "$C" git
mv "$STUBS/node" "$TMP/node.off"
ARGS=(); run HARNESS_DESKTOP_OS=windows HARNESS_NO_AUTO_DEPS=1
[[ $RC != 0 ]] || fail "T5: node missing exited 0"
grep -q "needs Node.js 22 or newer; node is not on PATH and harness could not fetch its own" <<<"$OUT" || fail "T5: no clear error — $OUT"
grep -q "winget install OpenJS.NodeJS.LTS" <<<"$OUT" || fail "T5: no Windows install hint — $OUT"
mv "$TMP/node.off" "$STUBS/node"
ARGS=(install); run HARNESS_DESKTOP_OS=macos STUB_NODE_VERSION=v20.18.0 HARNESS_NO_AUTO_DEPS=1
[[ $RC != 0 ]] || fail "T5: node 20 exited 0"
grep -q "this node is v20.18.0" <<<"$OUT" || fail "T5: no too-old error — $OUT"
grep -q "brew install node" <<<"$OUT" || fail "T5: no macOS hint — $OUT"
! grep -q '^npm \|^npx ' "$LOG" || fail "T5: built without a usable node"
[[ ! -e "$C/state/desktop/installed" ]] || fail "T5: stamp written"
ok "T5: node missing / v20 and auto-fetch off -> clear error with a per-OS install hint, non-zero, no build, no stamp"

# --- T5b: no node 22 -> harness fetches its own Node and builds with it ---------
# A fake nodejs.org: curl serves SHASUMS256.txt and a tarball whose node/npm/npx
# log as vnode/vnpm/vnpx, so the build provably ran on the fetched toolchain.
NV=$(sed -n 's/^HARNESS_DESKTOP_NODE_VERSION="\${HARNESS_DESKTOP_NODE_VERSION:-\(.*\)}"$/\1/p' "$REPO_ROOT/harness")
[[ -n "$NV" ]] || fail "T5b: HARNESS_DESKTOP_NODE_VERSION default not found"
NODEFIX="$TMP/nodefix/node-v$NV-linux-x64/bin"; mkdir -p "$NODEFIX"
for t in node npm npx; do
    sed "s/printf \"%s\" \"$t\"/printf \"%s\" \"v$t\"/; s/v22.11.0/v$NV/" "$STUBS/$t" >"$NODEFIX/$t"; chmod +x "$NODEFIX/$t"
done
tar -czf "$TMP/node.tgz" -C "$TMP/nodefix" "node-v$NV-linux-x64"
NSHA=$(sha256sum "$TMP/node.tgz"); NSHA=${NSHA%% *}
stub curl 'o=""; while (( $# )); do [[ "$1" == -o ]] && { o="$2"; shift; }; shift; done
if [[ -n "$o" ]]; then cp "'"$TMP"'/node.tgz" "$o"; else
  echo "0000000000000000000000000000000000000000000000000000000000000000  node-v'"$NV"'-linux-x64.tar.xz"
  echo "'"$NSHA"'  node-v'"$NV"'-linux-x64.tar.gz"; fi'
mv "$STUBS/node" "$TMP/node.off"; mkdir -p "$TMP/home5b"  # own HOME: T6 needs T4's install intact
ARGS=(install); run HARNESS_DESKTOP_OS=linux HOME="$TMP/home5b"
[[ $RC == 0 ]] || fail "T5b: build with the fetched node failed (rc $RC) — $OUT"
grep -q "fetching harness's own copy (Node $NV" <<<"$OUT" || fail "T5b: no fetch notice — $OUT"
grep -q "nodejs.org/dist/v$NV/node-v$NV-linux-x64.tar.gz" "$LOG" || fail "T5b: tarball not fetched — $(cat "$LOG")"
grep -q '^vnpm \[ci\]' "$LOG" && grep -q '^vnpx \[--no-install\] \[electron-builder\]' "$LOG" \
    || fail "T5b: not built with the fetched node — $(cat "$LOG")"
[[ -x "$C/state/tools/node-v$NV-linux-x64/bin/node" ]] || fail "T5b: node not kept under state/tools"
compgen -G "$C/state/tools/*.part.*" >/dev/null && fail "T5b: temp dir left behind"
[[ -e "$C/state/desktop/installed" ]] || fail "T5b: stamp not written"
rm -f "$C/state/desktop/installed"
ARGS=(install); run HARNESS_DESKTOP_OS=linux HOME="$TMP/home5b"
[[ $RC == 0 ]] || fail "T5b: second build failed — $OUT"
! grep -q '^curl' "$LOG" || fail "T5b: node fetched again — $(cat "$LOG")"
grep -q '^vnpx ' "$LOG" || fail "T5b: second build not on the kept node"
# Windows Defender holds freshly unpacked files open, so renaming their folder
# into state/tools fails for a few seconds: retried, and copied if it never clears.
REALMV=$(PATH="$SYS" command -v mv)
stub mv 'n=$(cat "'"$TMP"'/mvfail" 2>/dev/null || echo 0)
case "${@: -1}" in */state/tools/node-v*-linux-x64)
  if (( n > 0 )); then echo $((n-1)) >"'"$TMP"'/mvfail"; echo "mv: cannot move: Permission denied" >&2; exit 1; fi ;;
esac
exec "'"$REALMV"'" "$@"'
rm -rf "$C/state/tools"; rm -f "$C/state/desktop/installed"; echo 2 >"$TMP/mvfail"
ARGS=(install); run HARNESS_DESKTOP_OS=linux HOME="$TMP/home5b"
[[ $RC == 0 ]] || fail "T5b: locked rename not retried (rc $RC) — $OUT"
grep -q "still locked (a virus scanner" <<<"$OUT" || fail "T5b: no locked-files notice — $OUT"
[[ "$(cat "$TMP/mvfail")" == 0 ]] && grep -q '^vnpx ' "$LOG" || fail "T5b: not built after the retry — $(cat "$LOG")"
rm -rf "$C/state/tools"; rm -f "$C/state/desktop/installed"; echo 99 >"$TMP/mvfail"
ARGS=(install); run HARNESS_DESKTOP_OS=linux HOME="$TMP/home5b" HARNESS_PLACE_TRIES=2
[[ $RC == 0 ]] && grep -q "copying instead" <<<"$OUT" && grep -q '^vnpx ' "$LOG" \
    || fail "T5b: rename never clearing did not fall back to a copy (rc $RC) — $OUT"
[[ -x "$C/state/tools/node-v$NV-linux-x64/bin/node" ]] || fail "T5b: copied node missing"
compgen -G "$C/state/tools/*.part.*" >/dev/null && fail "T5b: temp dir left after the copy"
rm -f "$STUBS/mv" "$TMP/mvfail"
# A node on PATH that is new enough wins; nothing is fetched or used from state.
mv "$TMP/node.off" "$STUBS/node"; rm -f "$C/state/desktop/installed"
ARGS=(install); run HARNESS_DESKTOP_OS=linux HOME="$TMP/home5b"
[[ $RC == 0 ]] && ! grep -q '^curl\|^vnp' "$LOG" && grep -q '^npx ' "$LOG" || fail "T5b: PATH node 22 not preferred — $(cat "$LOG")"
rm -f "$STUBS/curl"
C="$C2"
ok "T5b: no node 22 -> harness fetches Node $NV (checksum-verified) into state/tools, builds with it, reuses it; a locked rename is retried, then copied; a PATH node 22 wins"

# --- T6: failures keep the old install ------------------------------------------
subst 's/0.1.1/0.1.2/' "$C/desktop/package.json"; (cd "$C" && "${GIT[@]}" commit -qam bump2)
old_stamp="$(STAMP)"
ARGS=(); run HARNESS_DESKTOP_OS=linux STUB_EB_RC=1 STUB_BUILD_ID=b3
[[ $RC != 0 ]] || fail "T6: electron-builder failure exited 0"
grep -q "electron-builder failed" <<<"$OUT" || fail "T6: no clear error — $OUT"
! grep -q '^setsid' "$LOG" || fail "T6: launched after a failed rebuild"
[[ "$(cat "$DATA/harness-desktop/app/build-id")" == b2 ]] || fail "T6: old install changed"
[[ "$(STAMP)" == "$old_stamp" ]] || fail "T6: stamp changed"
ARGS=(install); run HARNESS_DESKTOP_OS=linux STUB_BUILD_RC=2
[[ $RC != 0 ]] && grep -q "the build failed" <<<"$OUT" || fail "T6: build failure not reported — $OUT"
! grep -q '^npx ' "$LOG" || fail "T6: packaged after a failed build"
[[ "$(STAMP)" == "$old_stamp" && -x "$APP" ]] || fail "T6: install/stamp changed after a build failure"
# A build that exits 0 having built nothing (build.mjs's main-module check never matching, as on
# Windows) stops before packaging, even with a stale entry from an earlier build left in out/.
mkdir -p "$C/desktop/out/main"; echo stale >"$C/desktop/out/main/index.cjs"
ARGS=(install); run HARNESS_DESKTOP_OS=linux STUB_BUILD_EMPTY=1
[[ $RC != 0 ]] && grep -q "produced no out/main/index.cjs" <<<"$OUT" || fail "T6: empty build not caught — $OUT"
! grep -q '^npx ' "$LOG" || fail "T6: packaged after an empty build"
[[ "$(STAMP)" == "$old_stamp" && -x "$APP" ]] || fail "T6: install/stamp changed after an empty build"
ok "T6: build, empty build or electron-builder failure -> non-zero, old install and stamp kept, no launch"

# --- T7: windows -----------------------------------------------------------------
WAPP="$LOCALAPPDATA/Programs/Harness/Harness.exe"
ARGS=(); run HARNESS_DESKTOP_OS=windows PROCESSOR_ARCHITECTURE=AMD64
[[ $RC == 0 ]] || fail "T7: exit $RC — $OUT"
grep -qx 'npx \[--no-install\] \[electron-builder\] \[--win\] \[nsis\] \[--x64\] \[-c.npmRebuild=false\] \[--publish\] \[never\]' "$LOG" \
    || fail "T7: electron-builder args — $(cat "$LOG")"
grep -qx 'installer \[/S\] MSYS_NO_PATHCONV=1' "$LOG" || fail "T7: installer not run silently with /S — $(cat "$LOG")"
[[ -f "$WAPP" ]] || fail "T7: Harness.exe not installed"
wpath="C:${WAPP//\//\\}"
grep -qxF "cmd.exe [/c] [start] [] [$wpath]" "$LOG" || fail "T7: launch is not cmd.exe /c start \"\" <winpath> — $(cat "$LOG")"
grep -qxF "cmd-env HARNESS_DESKTOP_ROOT=C:${C//\//\\} MSYS_NO_PATHCONV=1" "$LOG" || fail "T7: launch env — $(cat "$LOG")"
grep -q '^installer' "$LOG" && [[ "$(grep -n '^installer' "$LOG" | cut -d: -f1)" -lt "$(grep -n '^cmd.exe' "$LOG" | cut -d: -f1)" ]] \
    || fail "T7: launched before the installer"
[[ "$(cat "$C/state/desktop/installed")" == "tree:$(tree)" ]] || fail "T7: stamp"
ARGS=(where); run HARNESS_DESKTOP_OS=windows
[[ $RC == 0 && "$(head -n1 <<<"$OUT")" == "$WAPP" ]] || fail "T7: where — $OUT"
rm -f "$C/state/desktop/installed"
ARGS=(install); run HARNESS_DESKTOP_OS=windows PROCESSOR_ARCHITECTURE=ARM64
grep -q '^npx .*\[--win\] \[nsis\] \[--arm64\]' "$LOG" || fail "T7: ARM64 did not build --arm64 — $(cat "$LOG")"
grep -q '^installer \[/S\]' "$LOG" || fail "T7: arm64 installer not run"
ok "T7: windows -> --win nsis --x64/--arm64, installer /S (no path conversion), cmd.exe /c start \"\" <C:\\ path>"

# --- T8: macos ---------------------------------------------------------------------
rm -f "$C/state/desktop/installed"
ARGS=(); run HARNESS_DESKTOP_OS=macos STUB_ARCH=arm64
[[ $RC == 0 ]] || fail "T8: exit $RC — $OUT"
MAPP="$HOME/Applications/Harness.app"
grep -qx 'npx \[--no-install\] \[electron-builder\] \[--mac\] \[dir\] \[--arm64\] \[-c.npmRebuild=false\] \[--publish\] \[never\]' "$LOG" \
    || fail "T8: electron-builder args — $(cat "$LOG")"
grep -qx 'npx-env CSC_IDENTITY_AUTO_DISCOVERY=false' "$LOG" || fail "T8: identity auto-discovery not off"
grep -qx "ditto \[$C/desktop/dist/mac-arm64/Harness.app\] \[$HOME/Applications/.Harness.app.new\]" "$LOG" \
    || fail "T8: not copied with ditto to a temp name — $(cat "$LOG")"
grep -qx "codesign \[--force\] \[--deep\] \[-s\] \[-\] \[$HOME/Applications/.Harness.app.new\]" "$LOG" \
    || fail "T8: not ad-hoc signed — $(cat "$LOG")"
grep -qx "xattr \[-dr\] \[com.apple.quarantine\] \[$HOME/Applications/.Harness.app.new\]" "$LOG" || fail "T8: quarantine not cleared"
[[ -d "$MAPP" && ! -e "$HOME/Applications/.Harness.app.new" ]] || fail "T8: not swapped into $MAPP"
grep -qx "open \[-a\] \[$MAPP\]" "$LOG" || fail "T8: not launched with open -a — $(cat "$LOG")"
mv "$STUBS/codesign" "$TMP/cs.off"; stub codesign 'exit 1'
echo keep >"$MAPP/marker"; rm -f "$C/state/desktop/installed"
ARGS=(install); run HARNESS_DESKTOP_OS=macos STUB_ARCH=arm64
[[ $RC != 0 && -f "$MAPP/marker" && ! -e "$HOME/Applications/.Harness.app.new" ]] || fail "T8: failed codesign replaced the app — $OUT"
mv "$TMP/cs.off" "$STUBS/codesign"
ok "T8: macos -> --mac dir, ditto to a temp name, codesign -s -, swap into ~/Applications, open -a; failed sign keeps the old app"

# --- T9: linux .desktop + sandbox rule -------------------------------------------
ENTRY="$DATA/applications/harness-desktop.desktop"
ARGS=(install); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] || fail "T9: exit $RC — $OUT"
grep -qx "Exec=$APP %U" "$ENTRY" || fail "T9: Exec — $(cat "$ENTRY")"
grep -qx 'Icon=harness-desktop' "$ENTRY" && grep -qx 'Name=Harness' "$ENTRY" \
    && grep -qx 'StartupWMClass=Harness' "$ENTRY" && grep -qx 'Categories=Development;' "$ENTRY" \
    && grep -qx 'Type=Application' "$ENTRY" || fail "T9: entry fields — $(cat "$ENTRY")"
[[ -f "$DATA/icons/hicolor/512x512/apps/harness-desktop.png" ]] || fail "T9: icon not installed"
echo 1 >"$PROC/apparmor_restrict_unprivileged_userns"
run HARNESS_DESKTOP_OS=linux
grep -qx "Exec=$APP --no-sandbox %U" "$ENTRY" || fail "T9: no --no-sandbox under the AppArmor restriction — $(cat "$ENTRY")"
ARGS=(); run HARNESS_DESKTOP_OS=linux
grep -qx "setsid \[-f\] \[$APP\] \[--no-sandbox\]" "$LOG" || fail "T9: launch without --no-sandbox — $(cat "$LOG")"
echo 0 >"$PROC/apparmor_restrict_unprivileged_userns"; echo 0 >"$PROC/unprivileged_userns_clone"
ARGS=(); run HARNESS_DESKTOP_OS=linux
grep -qx "setsid \[-f\] \[$APP\] \[--no-sandbox\]" "$LOG" || fail "T9: unprivileged_userns_clone=0 did not add --no-sandbox"
rm -f "$PROC/unprivileged_userns_clone"
ARGS=(); run HARNESS_DESKTOP_OS=linux
grep -qx "setsid \[-f\] \[$APP\]" "$LOG" || fail "T9: --no-sandbox with userns allowed"
ARGS=(); run HARNESS_DESKTOP_OS=linux DISPLAY= WAYLAND_DISPLAY=
[[ $RC != 0 ]] && grep -q "no graphical session" <<<"$OUT" || fail "T9: no-display launch not refused — $OUT"
ok "T9: linux .desktop (Exec/Icon/Name/StartupWMClass) + icon; --no-sandbox only when userns is blocked; no display refused"

# --- T10: where + no-git fallback --------------------------------------------------
ARGS=(where); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] || fail "T10: where exit $RC — $OUT"
[[ "$(sed -n 1p <<<"$OUT")" == "$APP" ]] || fail "T10: where line 1 — $OUT"
sed -n 2p <<<"$OUT" | grep -q '^current: ' || fail "T10: where line 2 — $OUT"
[[ "$(wc -l <<<"$OUT")" == 2 ]] || fail "T10: where is not two lines — $OUT"
echo "tree:old" >"$C/state/desktop/installed"
run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 ]] && sed -n 2p <<<"$OUT" | grep -q '^stale: ' || fail "T10: stale where — $OUT"
rm -rf "$DATA/harness-desktop"
run HARNESS_DESKTOP_OS=linux
[[ $RC == 1 ]] && grep -q '^not installed' <<<"$OUT" || fail "T10: not-installed where — rc $RC $OUT"
ARGS=(where); run HARNESS_DESKTOP_OS=plan9
[[ $RC != 0 ]] && grep -q "not supported on 'plan9'" <<<"$OUT" || fail "T10: unsupported OS — $OUT"
C="$TMP/clone-nogit"; make_clone "$C"
ARGS=(); run HARNESS_DESKTOP_OS=linux
[[ $RC == 0 && "$(STAMP)" == "version:0.1.0" ]] || fail "T10: no-git stamp '$(STAMP)' — $OUT"
run HARNESS_DESKTOP_OS=linux
! grep -q '^npx ' "$LOG" || fail "T10: no-git clone rebuilt again"
ok "T10: where (current/stale/not installed, exit 1), unsupported OS; no git -> version stamp, no rebuild loop"

echo
echo "DESKTOP CMD TEST PASSED"
