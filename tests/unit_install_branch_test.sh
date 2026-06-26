#!/usr/bin/env bash
#
# tests/unit_install_branch_test.sh — exercise harness-install.sh's branch
# selection: the -b/--branch flag is restricted to main|dev, and the
# interactive "which branch to track" prompt offers only those two.
#
# The -b rejection paths abort during argument parsing — before preflight, any
# clone, or any disk write — so they are safe to drive by running the real
# installer with bad flags (no docker, no network). The interactive prompt and
# the accepted-value cases are verified by static source inspection, since
# driving the full install (preflight + clone) is out of scope for a unit test
# and the prompt is tty-gated (no pty in CI).
#
# Prints "INSTALL BRANCH TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALLER="${REPO_ROOT}/harness-install.sh"

echo "============================================================"
echo " install branch-selection unit test"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

[[ -f "$INSTALLER" ]] || fail "installer not found at $INSTALLER"

# --- T1: -b with an unsupported branch is rejected, exits non-zero ----------
out="$(bash "$INSTALLER" -b production 2>&1)" && rc=0 || rc=$?
[[ "$rc" -ne 0 ]] || fail "T1: '-b production' should exit non-zero"
grep -qi "must be 'main' or 'dev'" <<<"$out" \
    || fail "T1: missing the 'main or dev' rejection message — got: $out"
# It must abort at parse time: nothing about preflight/cloning should appear.
grep -qiE 'clon(e|ing)|preflight' <<<"$out" \
    && fail "T1: rejection should happen before any clone/preflight — got: $out"
ok "T1: '-b production' is rejected before any clone, exits non-zero"

# --- T2: -b with no value is rejected ---------------------------------------
out="$(bash "$INSTALLER" -b 2>&1)" && rc=0 || rc=$?
[[ "$rc" -ne 0 ]] || fail "T2: bare '-b' should exit non-zero"
grep -qi 'requires a branch name' <<<"$out" \
    || fail "T2: missing 'requires a branch name' message — got: $out"
ok "T2: bare '-b' is rejected with a clear message"

# --- T3: both 'main' and 'dev' are the accepted -b values (source check) ----
# The case arm that whitelists the two branches.
grep -Eq 'main\|dev\)[[:space:]]*BRANCH="\$2"' "$INSTALLER" \
    || fail "T3: the 'main|dev) BRANCH=\$2' accept arm is missing"
ok "T3: -b accepts exactly main and dev"

# --- T4: the interactive prompt is tty-gated and offers only main/dev -------
# Pull the prompt block and assert its shape: gated on an empty BRANCH and a
# tty, offering 1) main and 2) dev, defaulting to main.
prompt="$(awk '/Which branch should this install track/,/tracking branch:/' "$INSTALLER")"
[[ -n "$prompt" ]] || fail "T4: branch-prompt block not found"
grep -q '1) main' <<<"$prompt" || fail "T4: prompt missing the main option"
grep -q '2) dev'  <<<"$prompt" || fail "T4: prompt missing the dev option"
grep -Eq 'BRANCH="dev"' <<<"$prompt" || fail "T4: prompt never selects dev"
grep -Eq 'BRANCH="main"' <<<"$prompt" || fail "T4: prompt never defaults to main"
# Gated so non-interactive installs keep the previous default-branch behavior.
grep -Eq '\[\[ -z "\$BRANCH" && -t 0 \]\]' "$INSTALLER" \
    || fail "T4: branch prompt is not gated on empty BRANCH + a tty"
ok "T4: the branch prompt is tty-gated and offers only main (default) and dev"

echo
echo "INSTALL BRANCH TEST PASSED"
