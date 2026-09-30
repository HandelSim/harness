#!/usr/bin/env bash
#
# tests/unit_update_check_test.sh — the update-available banner and
# `harness check-updates` follow the branch the clone is on, not just main.
#
# Docker-free and network-free: a local bare repo stands in for origin, and
# `harness` is sourced with HARNESS_SOURCE_ONLY=1 against a throwaway clone.
# (tests/harness_test.sh T23 covers the main-branch basics: first-run banner,
# skip env var, offline cache fallback, check-updates offline error.)
#
#   - T1: a clone on dev that is behind origin/dev shows the banner (naming
#         dev) and caches "dev <sha>"; check-updates says an update exists.
#   - T2: dev up to date: no banner; check-updates says "up to date".
#   - T3: dev AHEAD of origin/dev (unpushed local commits): no banner.
#   - T4: offline, the cache is only used for the branch that wrote it: a dev
#         cache gives no banner on main, and a legacy bare-SHA cache (written
#         only on main) still gives one on main.
#   - T5: a local branch origin doesn't have: no banner, launch unaffected.
#
# Prints "UPDATE CHECK TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$(cd "${SCRIPT_DIR}/.." && pwd)/harness"

echo "============================================================"
echo " update check test"
echo "============================================================"

fail() { echo "  ✗ $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@invalid

REMOTE="$TMP/remote.git"
ROOT="$TMP/root"
CACHE="$ROOT/state/.harness-update-check"

git init -q --bare "$REMOTE"
git init -q "$TMP/seed"
( cd "$TMP/seed" && echo a >a && git add a && git commit -qm a \
    && git branch -M main && git remote add origin "$REMOTE" \
    && git push -q origin main && git checkout -qb dev && git push -q origin dev )
git clone -q --branch dev "$REMOTE" "$ROOT"

# Push one more commit to origin/<branch> from the seed checkout.
advance() { ( cd "$TMP/seed" && git checkout -q "$1" && echo "$RANDOM" >>a \
    && git commit -qam adv && git push -q origin "$1" ); }

# Run the banner helper against $ROOT; prints its stderr.
banner() {
    # shellcheck disable=SC1090
    ( HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$ROOT" source "$HARNESS" >/dev/null 2>&1
      _update_check_and_banner 4 2>&1 )
}
check_updates() {
    HARNESS_INSTALL_ROOT="$ROOT" HARNESS_PROJECT_NAME=harness-upd-unit "$HARNESS" check-updates 2>&1
}

# --- T1: dev behind origin/dev ------------------------------------------------
advance dev
out="$(banner)"
grep -q "update available on dev" <<<"$out" || fail "T1: no banner for a dev clone behind origin/dev — $out"
remote_dev="$(git --git-dir="$REMOTE" rev-parse dev)"
[[ "$(cat "$CACHE")" == "dev $remote_dev" ]] || fail "T1: cache should be 'dev <sha>', got: $(cat "$CACHE")"
out="$(check_updates)" || fail "T1: check-updates failed — $out"
grep -q "update available on dev" <<<"$out" || fail "T1: check-updates missed the dev update — $out"
ok "T1: a dev clone behind origin/dev gets the banner and a dev-keyed cache"

# --- T2: dev up to date ---------------------------------------------------------
git -C "$ROOT" pull -q --ff-only
out="$(banner)"
! grep -q "update available" <<<"$out" || fail "T2: banner shown while up to date — $out"
out="$(check_updates)" || fail "T2: check-updates failed — $out"
grep -q "up to date with origin/dev" <<<"$out" || fail "T2: check-updates should say up to date — $out"
ok "T2: an up-to-date dev clone gets no banner"

# --- T3: dev ahead of origin/dev --------------------------------------------------
( cd "$ROOT" && echo local >>a && git commit -qam local )
out="$(banner)"
! grep -q "update available" <<<"$out" || fail "T3: unpushed local commits read as an update — $out"
ok "T3: a dev clone ahead of origin/dev gets no banner"

# --- T4: offline cache is per branch ----------------------------------------------
git -C "$ROOT" remote set-url origin "$TMP/nonexistent"
git -C "$ROOT" checkout -q -b main
printf 'dev deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n' >"$CACHE"
out="$(banner)"
! grep -q "update available" <<<"$out" || fail "T4: a dev cache drove a banner on main — $out"
printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n' >"$CACHE"
out="$(banner)"
grep -q "update available on main" <<<"$out" || fail "T4: legacy bare-SHA cache should still work on main — $out"
ok "T4: the offline cache only applies to the branch that wrote it"

# --- T5: a branch origin doesn't have ---------------------------------------------
git -C "$ROOT" remote set-url origin "$REMOTE"
git -C "$ROOT" checkout -q -b my-feature
rc=0; out="$(banner)" || rc=$?
(( rc == 0 )) || fail "T5: helper returned rc=$rc"
! grep -q "update available" <<<"$out" || fail "T5: banner on a branch origin doesn't have — $out"
ok "T5: a local-only branch gets no banner"

echo
echo "UPDATE CHECK TEST PASSED"
