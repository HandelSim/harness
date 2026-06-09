#!/usr/bin/env bash
#
# unit_host_toolchain_test.sh — docker-free, DOWNLOAD-free coverage for the
# host-mode dependency auto-provisioner (host_ensure_toolchain and friends),
# added so `harness install` + `harness host` need no manual jq/Node/opencode
# install.
#
# What it covers (no network, no docker):
#   - arch/platform mapping (host_jq_platform / host_node_platform) shape
#   - host_sha_from_manifest parsing (curl stubbed with a fixture manifest)
#   - host_sha256_check against a real local file
#   - host_toolchain_path_prefix PATH assembly from stubbed vendored binaries
#   - host_ensure_toolchain os guard + PATH wiring (ensure_* stubbed)
#   - drift guard: harness HARNESS_HOST_* pins stay in sync with agents/Dockerfile
#
# It deliberately does NOT exercise the real download/extract/npm path (that
# needs network); it tests the parsing/assembly/guard logic those steps depend
# on. `harness` is sourced with HARNESS_SOURCE_ONLY=1 so main() never runs.
#
# Run:  bash tests/unit_host_toolchain_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " host toolchain provisioner unit test (docker-free, download-free)"
echo "============================================================"

fail() { echo "[host-toolchain] FAIL: $*" >&2; exit 1; }
ok()   { echo "[host-toolchain] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
    || fail "this test needs sha256sum or shasum"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# Source the host helpers without running main. HARNESS_INSTALL_ROOT pins the
# state_root the toolchain dir helpers read.
# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null

# When `harness` is sourced from tests/, its self-resolution sets clone_dir to
# tests/, so it can't auto-source scripts/lib/platform.sh (which defines
# harness_detect_os). Source it explicitly, the same lib harness loads at runtime.
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/platform.sh"

for fn in host_jq_platform host_node_platform host_sha_from_manifest \
          host_sha256_check host_toolchain_path_prefix host_ensure_toolchain \
          host_tool_bin_dir host_node_dir host_opencode_dir; do
    [[ "$(type -t "$fn")" == "function" ]] || fail "$fn not sourced"
done

# --- T1: platform tokens have the exact upstream shape ----------------------
jqp="$(host_jq_platform)" || fail "T1: host_jq_platform failed on $(uname -s)/$(uname -m)"
ndp="$(host_node_platform)" || fail "T1: host_node_platform failed on $(uname -s)/$(uname -m)"
[[ "$jqp" =~ ^(linux|macos)-(amd64|arm64)$ ]] || fail "T1: jq platform '$jqp' not a known token"
[[ "$ndp" =~ ^(linux|darwin)-(x64|arm64)$ ]]  || fail "T1: node platform '$ndp' not a known token"
# Both must reflect the same machine arch.
case "$(uname -m)" in
    x86_64|amd64)   [[ "$jqp" == *-amd64 && "$ndp" == *-x64   ]] || fail "T1: arch mismatch jq=$jqp node=$ndp on x86_64" ;;
    aarch64|arm64)  [[ "$jqp" == *-arm64 && "$ndp" == *-arm64 ]] || fail "T1: arch mismatch jq=$jqp node=$ndp on arm64" ;;
esac
ok "T1: jq/node platform tokens are well-formed and arch-consistent ($jqp / $ndp)"

# --- T2: host_sha_from_manifest extracts the right hash, fails on a miss -----
FIX="$TMP_ROOT/manifest.txt"
cat >"$FIX" <<'EOF'
deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef  jq-linux-amd64
cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe  jq-linux-arm64
0000111122223333444455556666777788889999aaaabbbbccccddddeeeeffff  node-v20.20.2-linux-arm64.tar.gz
EOF
# Stub curl so host_sha_from_manifest reads the fixture instead of the network.
# command -v finds the function; the function ignores its args and prints the fixture.
curl() { cat "$FIX"; }
got="$(host_sha_from_manifest "https://example/ignored" "jq-linux-arm64" || true)"
[[ "$got" == "cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe" ]] \
    || fail "T2: wrong hash for jq-linux-arm64: '$got'"
got="$(host_sha_from_manifest "https://example/ignored" "node-v20.20.2-linux-arm64.tar.gz" || true)"
[[ "$got" == "0000111122223333444455556666777788889999aaaabbbbccccddddeeeeffff" ]] \
    || fail "T2: wrong hash for node tarball: '$got'"
# A filename that is a prefix of another must NOT loose-match (anchored at end).
if host_sha_from_manifest "https://example/ignored" "jq-linux-amd" >/dev/null 2>&1; then
    fail "T2: host_sha_from_manifest matched a non-anchored prefix"
fi
if host_sha_from_manifest "https://example/ignored" "jq-macos-arm64" >/dev/null 2>&1; then
    fail "T2: host_sha_from_manifest returned success for an absent entry"
fi
unset -f curl
ok "T2: host_sha_from_manifest parses hashes and is end-anchored"

# --- T3: host_sha256_check accepts the real hash, rejects a wrong one -------
DATA="$TMP_ROOT/blob.bin"
printf 'harness-host-toolchain-test' >"$DATA"
if command -v sha256sum >/dev/null 2>&1; then
    real="$(sha256sum "$DATA" | cut -d' ' -f1)"
else
    real="$(shasum -a 256 "$DATA" | cut -d' ' -f1)"
fi
host_sha256_check "$DATA" "$real" || fail "T3: rejected the correct sha256"
if host_sha256_check "$DATA" "0000000000000000000000000000000000000000000000000000000000000000"; then
    fail "T3: accepted a wrong sha256"
fi
ok "T3: host_sha256_check verifies correctly"

# --- T4: host_toolchain_path_prefix assembles only existing vendored dirs ---
# Nothing vendored yet -> empty prefix.
[[ -z "$(host_toolchain_path_prefix)" ]] || fail "T4: prefix non-empty with nothing vendored"
# Lay down fake-but-executable vendored binaries.
mk_exe() { mkdir -p "$(dirname "$1")"; printf '#!/bin/sh\necho stub\n' >"$1"; chmod +x "$1"; }
mk_exe "$(host_tool_bin_dir)/jq"
node_bin="$(host_node_dir)/node-v${HARNESS_HOST_NODE_VERSION}-${ndp}/bin"
mk_exe "$node_bin/node"
mk_exe "$(host_opencode_dir)/bin/opencode"
prefix="$(host_toolchain_path_prefix)"
[[ "$prefix" == "$(host_tool_bin_dir):$node_bin:$(host_opencode_dir)/bin" ]] \
    || fail "T4: unexpected PATH prefix order: '$prefix'"
ok "T4: host_toolchain_path_prefix lists vendored dirs (jq, node, opencode) in order"

# --- T5: host_ensure_toolchain rejects unsupported OS, wires PATH otherwise --
( harness_detect_os() { echo windows; }
  if host_ensure_toolchain >/dev/null 2>&1; then exit 1; fi ) \
    || fail "T5: host_ensure_toolchain did not reject a non-Linux/macOS host"
# Happy path with the provisioners stubbed: PATH must gain the vendored dirs.
out="$(
  harness_detect_os() { echo linux; }
  host_ensure_jq() { return 0; }
  host_ensure_node() { return 0; }
  host_ensure_opencode() { return 0; }
  host_ensure_toolchain >/dev/null 2>&1 || { echo "ENSURE_FAILED"; exit 0; }
  case ":$PATH:" in
    *":$(host_tool_bin_dir):"*) echo "PATH_OK" ;;
    *) echo "PATH_MISSING" ;;
  esac
)"
[[ "$out" == "PATH_OK" ]] || fail "T5: host_ensure_toolchain did not prepend the vendored bin dir (got '$out')"
ok "T5: host_ensure_toolchain guards OS and prepends vendored dirs to PATH"

# --- T6: pins stay in sync with the container (agents/Dockerfile) ------------
DF="$REPO_ROOT/agents/Dockerfile"
[[ -f "$DF" ]] || fail "T6: agents/Dockerfile not found"
df_opencode="$(grep -E '^ARG OPENCODE_VERSION=' "$DF" | head -1 | cut -d= -f2)"
df_compat="$(grep -E '^ARG OPENAI_COMPAT_VERSION=' "$DF" | head -1 | cut -d= -f2)"
df_node_major="$(grep -E '^FROM node:' "$DF" | head -1 | sed -E 's/^FROM node:([0-9]+).*/\1/')"
[[ -n "$df_opencode" ]] || fail "T6: could not read OPENCODE_VERSION from Dockerfile"
[[ "$HARNESS_HOST_OPENCODE_VERSION" == "$df_opencode" ]] \
    || fail "T6: opencode pin drift — harness=$HARNESS_HOST_OPENCODE_VERSION dockerfile=$df_opencode"
[[ "$HARNESS_HOST_OPENAI_COMPAT_VERSION" == "$df_compat" ]] \
    || fail "T6: provider pin drift — harness=$HARNESS_HOST_OPENAI_COMPAT_VERSION dockerfile=$df_compat"
[[ "${HARNESS_HOST_NODE_VERSION%%.*}" == "$df_node_major" ]] \
    || fail "T6: Node major drift — harness=$HARNESS_HOST_NODE_VERSION dockerfile major=$df_node_major"
ok "T6: host toolchain pins match agents/Dockerfile (opencode $df_opencode, provider $df_compat, node major $df_node_major)"

echo
echo "HOST TOOLCHAIN TEST PASSED"
