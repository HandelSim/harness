#!/usr/bin/env bash
#
# tests/unit_bootstrap_test.sh — exercise harness-bootstrap.sh (the thin,
# version-stable install entrypoint) without docker, a network round-trip, or a
# real clone.
#
# harness-bootstrap.sh fetches the CURRENT harness-install.sh and hands off to
# it. We drive that locally by pointing HARNESS_REPO_URL at a fake "repo" dir
# holding a stub installer (so the bootstrap's local-path branch copies it, no
# network), then assert the handoff happened, the proxy from the bundled .env
# reached the fetch environment, and the fetched script's $script_dir resolves
# to the bundle dir (so the real installer would find .env / .harness-allowlist
# beside it).
#
# Deterministic, network-free paths covered:
#   - T1: happy path. Local-path fetch copies the stub installer, hands off
#         (executed). The stub sees HTTPS_PROXY from the bundled .env and a
#         $script_dir equal to the bundle dir; the fetched temp is cleaned up.
#   - T2: sanity-check + fallback. A fetched file with no shebang is rejected,
#         and the bootstrap falls back to a bundled harness-install.sh.
#   - T3: sourced handoff. `source`-ing the bootstrap runs the installer in the
#         same shell and returns its rc, and does NOT leak `set -e`/`set -u`.
#   - T4: static source checks for the load-bearing invariants.
#
# Prints "BOOTSTRAP TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP="${REPO_ROOT}/harness-bootstrap.sh"

echo "============================================================"
echo " harness-bootstrap unit test"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

[[ -f "$BOOTSTRAP" ]] || fail "bootstrap not found at $BOOTSTRAP"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A fake "repo" with a stub installer the bootstrap will copy via its local-path
# branch. The stub records that it ran, the proxy it inherited, and its own
# directory (which must be the bundle dir, proving $script_dir resolution).
make_stub_repo() {
    local repo="$1"
    mkdir -p "$repo"
    cat >"$repo/harness-install.sh" <<'STUB'
#!/usr/bin/env bash
echo "FAKE_INSTALLER_RAN"
echo "SAW_HTTPS_PROXY=${HTTPS_PROXY:-<unset>}"
echo "SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
STUB
    chmod +x "$repo/harness-install.sh"
}

# --- T1: happy path (local-path fetch, handoff, proxy, $script_dir) ---------
bundle1="$TMP/bundle1"; mkdir -p "$bundle1"
repo1="$TMP/repo1"; make_stub_repo "$repo1"
cp "$BOOTSTRAP" "$bundle1/harness-bootstrap.sh"
printf 'HTTPS_PROXY=http://proxy.test:8080\n' >"$bundle1/.env"

out="$(
    set +e
    HARNESS_REPO_URL="$repo1" bash "$bundle1/harness-bootstrap.sh"
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T1: bootstrap should exit 0, got rc=$rc — $out"
grep -q "FAKE_INSTALLER_RAN" <<<"$out" \
    || fail "T1: installer was not handed control — $out"
grep -q "SAW_HTTPS_PROXY=http://proxy.test:8080" <<<"$out" \
    || fail "T1: proxy from .env did not reach the fetch/handoff env — $out"
grep -q "SCRIPT_DIR=$bundle1\$" <<<"$out" \
    || fail "T1: fetched installer's \$script_dir is not the bundle dir — $out"
[[ ! -e "$bundle1/.harness-install.fetched.sh" ]] \
    || fail "T1: fetched installer temp was not cleaned up"
ok "T1: local-path fetch hands off; proxy + \$script_dir correct; temp cleaned"

# --- T2: shebang sanity check rejects junk, falls back to bundled installer --
bundle2="$TMP/bundle2"; mkdir -p "$bundle2"
junkrepo="$TMP/junkrepo"; mkdir -p "$junkrepo"
printf '<!DOCTYPE html><html>captive portal</html>\n' >"$junkrepo/harness-install.sh"
cp "$BOOTSTRAP" "$bundle2/harness-bootstrap.sh"
# A valid bundled installer to fall back to.
cat >"$bundle2/harness-install.sh" <<'BUNDLED'
#!/usr/bin/env bash
echo "BUNDLED_FALLBACK_RAN"
BUNDLED
chmod +x "$bundle2/harness-install.sh"

out="$(
    set +e
    HARNESS_REPO_URL="$junkrepo" bash "$bundle2/harness-bootstrap.sh" 2>&1
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T2: fallback path should exit 0, got rc=$rc — $out"
grep -qi "falling back to the bundled" <<<"$out" \
    || fail "T2: missing fallback notice — $out"
grep -q "BUNDLED_FALLBACK_RAN" <<<"$out" \
    || fail "T2: bundled fallback installer did not run — $out"
ok "T2: non-script fetch is rejected and the bundled installer runs instead"

# --- T3: sourced handoff runs in-shell, returns rc, no set -e/-u leak --------
bundle3="$TMP/bundle3"; mkdir -p "$bundle3"
repo3="$TMP/repo3"; make_stub_repo "$repo3"
cp "$BOOTSTRAP" "$bundle3/harness-bootstrap.sh"
printf 'HTTPS_PROXY=\n' >"$bundle3/.env"   # blank: must keep host env, not crash

out="$(
    set +e
    # A child bash that sources the bootstrap, then proves strict mode did not
    # leak by referencing an unset var (would error under a leaked `set -u`).
    HARNESS_REPO_URL="$repo3" bash -c '
        source "'"$bundle3"'/harness-bootstrap.sh"
        src_rc=$?
        : "${THIS_VAR_IS_UNSET}"        # would abort if set -u leaked
        echo "SOURCED_RC=$src_rc"
    '
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T3: sourced bootstrap should not crash the shell — $out"
grep -q "FAKE_INSTALLER_RAN" <<<"$out" \
    || fail "T3: sourced handoff did not run the installer — $out"
grep -q "SOURCED_RC=0" <<<"$out" \
    || fail "T3: sourced bootstrap did not return the installer's rc — $out"
ok "T3: sourced handoff runs in-shell, returns rc, and does not leak strict mode"

# --- T4: static source checks for the load-bearing invariants ----------------
grep -q 'HARNESS_INSTALL_REF' "$BOOTSTRAP" \
    || fail "T4: bootstrap must honor HARNESS_INSTALL_REF for ref pinning"
grep -q 'HARNESS_REPO_URL' "$BOOTSTRAP" \
    || fail "T4: bootstrap must honor HARNESS_REPO_URL"
grep -q 'raw.githubusercontent.com' "$BOOTSTRAP" \
    || fail "T4: bootstrap must fetch the raw installer from the repo"
grep -qE 'BASH_SOURCE\[0\].* != .*\$\{0\}' "$BOOTSTRAP" \
    || fail "T4: bootstrap must detect sourced-vs-executed (no set -e leak when sourced)"
ok "T4: ref pinning, repo override, raw fetch, and sourced-detection are present"

echo
echo "BOOTSTRAP TEST PASSED"
