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
# reached the fetch environment, the args (with --branch) reached the
# installer, and the fetched script's $script_dir resolves to the bundle dir
# (so the real installer would find .env / .harness-allowlist beside it).
# Every run has stdin on /dev/null, so the main/dev question is never asked.
#
# Deterministic, network-free paths covered:
#   - T1: happy path. Local-path fetch copies the stub installer, hands off
#         (executed). The stub sees HTTPS_PROXY from the bundled .env, a
#         $script_dir equal to the bundle dir, and the caller's flags plus
#         `--branch main` (the no-tty default); the fetched temp is cleaned up.
#   - T1b: an explicit `-b dev` is forwarded as given, with no second --branch.
#   - T1c: a branch other than main/dev aborts before running any installer.
#   - T2: sanity-check + fallback. A fetched file with no shebang is rejected,
#         and the bootstrap falls back to a bundled harness-install.sh.
#   - T3: sourced handoff. `source`-ing the bootstrap runs the installer in the
#         same shell with the caller's args and returns its rc, does NOT leak
#         `set -e`/`set -u`, and leaves no _hb_* names or `cleanup` behind
#         (also on the invalid-branch abort).
#   - T4: static source checks for the load-bearing invariants.
#   - T5: a quoted, CRLF-terminated proxy in .env is exported clean.
#   - T6: a read-only bundle dir says so (not "network"), and falls back to a
#         bundled installer when there is one. Skipped as root.
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
echo "SAW_https_proxy=${https_proxy:-<unset>}"
echo "SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
echo "ARGS=$*"
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
    HARNESS_REPO_URL="$repo1" bash "$bundle1/harness-bootstrap.sh" --foo bar </dev/null
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T1: bootstrap should exit 0, got rc=$rc — $out"
grep -qx "ARGS=--foo bar --branch main" <<<"$out" \
    || fail "T1: installer should get the caller's flags plus --branch main — $out"
grep -q "FAKE_INSTALLER_RAN" <<<"$out" \
    || fail "T1: installer was not handed control — $out"
grep -q "SAW_HTTPS_PROXY=http://proxy.test:8080" <<<"$out" \
    || fail "T1: proxy from .env did not reach the fetch/handoff env — $out"
grep -q "SCRIPT_DIR=$bundle1\$" <<<"$out" \
    || fail "T1: fetched installer's \$script_dir is not the bundle dir — $out"
[[ ! -e "$bundle1/.harness-install.fetched.sh" ]] \
    || fail "T1: fetched installer temp was not cleaned up"
ok "T1: local-path fetch hands off; flags + --branch main, proxy, \$script_dir correct; temp cleaned"

# --- T1b: an explicit -b dev is forwarded as given, not doubled ---------------
out="$(
    set +e
    HARNESS_REPO_URL="$repo1" bash "$bundle1/harness-bootstrap.sh" -b dev --foo </dev/null
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T1b: bootstrap should exit 0, got rc=$rc — $out"
grep -qx "ARGS=-b dev --foo" <<<"$out" \
    || fail "T1b: -b dev should reach the installer unchanged, with no added --branch — $out"
ok "T1b: an explicit -b dev is forwarded as given"

# --- T1c: an unknown branch aborts before any installer runs -------------------
out="$(
    set +e
    HARNESS_REPO_URL="$repo1" bash "$bundle1/harness-bootstrap.sh" --branch production </dev/null 2>&1
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -ne 0 ]] || fail "T1c: -b production should fail — $out"
grep -q "must be 'main' or 'dev'" <<<"$out" \
    || fail "T1c: missing the main/dev error — $out"
! grep -q "FAKE_INSTALLER_RAN" <<<"$out" \
    || fail "T1c: the installer ran despite the bad branch — $out"
ok "T1c: a branch other than main/dev aborts before the installer runs"

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
    HARNESS_REPO_URL="$junkrepo" bash "$bundle2/harness-bootstrap.sh" </dev/null 2>&1
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
    # It also lists any _hb_* variable or function, or a `cleanup` function,
    # left in the shell afterward (there must be none).
    HARNESS_REPO_URL="$repo3" bash -c '
        source "'"$bundle3"'/harness-bootstrap.sh" -b dev
        src_rc=$?
        : "${THIS_VAR_IS_UNSET}"        # would abort if set -u leaked
        echo "SOURCED_RC=$src_rc"
        echo "LEAKED=$(compgen -v _hb_; compgen -A function _hb_; compgen -A function cleanup)"
        source "'"$bundle3"'/harness-bootstrap.sh" -b production 2>/dev/null
        echo "BAD_RC=$?"
        echo "LEAKED_BAD=$(compgen -v _hb_; compgen -A function _hb_)"
    ' </dev/null
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T3: sourced bootstrap should not crash the shell — $out"
grep -q "FAKE_INSTALLER_RAN" <<<"$out" \
    || fail "T3: sourced handoff did not run the installer — $out"
grep -q "SOURCED_RC=0" <<<"$out" \
    || fail "T3: sourced bootstrap did not return the installer's rc — $out"
grep -qx "ARGS=-b dev" <<<"$out" \
    || fail "T3: sourced handoff did not forward the caller's args — $out"
grep -qx "LEAKED=" <<<"$out" \
    || fail "T3: sourcing left _hb_* names or a cleanup function behind — $out"
grep -qx "BAD_RC=1" <<<"$out" \
    || fail "T3: a sourced bad -b should return 1 without killing the shell — $out"
grep -qx "LEAKED_BAD=" <<<"$out" \
    || fail "T3: the bad-branch abort left _hb_* names behind — $out"
ok "T3: sourced handoff runs in-shell with the caller's args, returns rc, leaks nothing"

# --- T4: static source checks for the load-bearing invariants ----------------
grep -q 'HARNESS_REPO_URL' "$BOOTSTRAP" \
    || fail "T4: bootstrap must honor HARNESS_REPO_URL"
grep -q 'raw.githubusercontent.com' "$BOOTSTRAP" \
    || fail "T4: bootstrap must fetch the raw installer from the repo"
grep -qE 'BASH_SOURCE\[0\].* != .*\$\{0\}' "$BOOTSTRAP" \
    || fail "T4: bootstrap must detect sourced-vs-executed (no set -e leak when sourced)"
ok "T4: repo override, raw fetch, and sourced-detection are present"

# --- T5: a quoted, CRLF-terminated proxy in .env is exported clean ------------
bundle5="$TMP/bundle5"; mkdir -p "$bundle5"
cp "$BOOTSTRAP" "$bundle5/harness-bootstrap.sh"
printf 'HTTPS_PROXY="http://proxy.test:8080"\r\n' >"$bundle5/.env"
out="$(
    set +e
    HARNESS_REPO_URL="$repo1" bash "$bundle5/harness-bootstrap.sh" </dev/null
    echo "rc=$?"
)"
rc="${out##*rc=}"
[[ "$rc" -eq 0 ]] || fail "T5: bootstrap should exit 0, got rc=$rc — $out"
grep -qx "SAW_HTTPS_PROXY=http://proxy.test:8080" <<<"$out" \
    || fail "T5: quotes/CR were not stripped from HTTPS_PROXY — $(cat -A <<<"$out")"
grep -qx "SAW_https_proxy=http://proxy.test:8080" <<<"$out" \
    || fail "T5: lower-case https_proxy was not exported clean — $(cat -A <<<"$out")"
ok "T5: a quoted, CRLF-terminated proxy in .env is exported clean"

# --- T6: read-only bundle dir: a clear message, then the bundled fallback ------
if [[ "$(id -u)" -eq 0 ]]; then
    echo "  - T6: skipped (root can write to a read-only dir)"
else
    bundle6="$TMP/bundle6"; mkdir -p "$bundle6"
    cp "$BOOTSTRAP" "$bundle6/harness-bootstrap.sh"
    chmod 555 "$bundle6"
    out="$(
        set +e
        HARNESS_REPO_URL="$repo1" bash "$bundle6/harness-bootstrap.sh" </dev/null 2>&1
        echo "rc=$?"
    )"
    chmod 755 "$bundle6"
    rc="${out##*rc=}"
    [[ "$rc" -ne 0 ]] || fail "T6: a read-only bundle with no installer should fail — $out"
    grep -q "cannot write to $bundle6" <<<"$out" \
        || fail "T6: missing the not-writable message — $out"
    ! grep -qi "network" <<<"$out" \
        || fail "T6: a read-only dir was reported as a network problem — $out"

    cat >"$bundle6/harness-install.sh" <<'BUNDLED'
#!/usr/bin/env bash
echo "BUNDLED_FALLBACK_RAN"
BUNDLED
    chmod 555 "$bundle6"
    out="$(
        set +e
        HARNESS_REPO_URL="$repo1" bash "$bundle6/harness-bootstrap.sh" </dev/null 2>&1
        echo "rc=$?"
    )"
    chmod 755 "$bundle6"
    rc="${out##*rc=}"
    [[ "$rc" -eq 0 ]] || fail "T6: read-only bundle with an installer should succeed — $out"
    grep -q "BUNDLED_FALLBACK_RAN" <<<"$out" \
        || fail "T6: the bundled installer did not run — $out"
    ok "T6: a read-only bundle dir says so and falls back to the bundled installer"
fi

echo
echo "BOOTSTRAP TEST PASSED"
