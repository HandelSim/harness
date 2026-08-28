#!/usr/bin/env bash
#
# tests/unit_reminder_seed_test.sh — exercise seed_reminder_file, the helper
# that puts the user's editable recency-reminder file in place.
#
# The hybrid reminder's prose is DATA, not code: the tracked default lives at
# proxy/reminder.md and is copied once to <install_root>/.harness-reminder.md,
# which is gitignored and user-owned. The invariants that matter:
#
#   - it seeds when the file is missing (so an existing install picks the
#     feature up on its next `harness start`, no upgrade action needed);
#   - it NEVER overwrites an existing copy, or a `harness upgrade` would
#     silently discard the user's wording;
#   - it removes an empty DIRECTORY left at that path. A compose bind-mount
#     whose source is missing makes docker create one; left in place it looks
#     like "already seeded" forever and mounts a directory over the file, so
#     the proxy silently runs on its built-in fallback (see F152, P064).
#
# Runs without docker: `harness` is sourced with HARNESS_SOURCE_ONLY=1 so
# main() never runs, and install_root/clone_dir are pointed at a tmpdir.
#
# Run:  bash tests/unit_reminder_seed_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

pass=0
fail() { echo "[reminder-seed] FAIL: $*" >&2; exit 1; }
ok()   { echo "[reminder-seed] OK: $*"; pass=$((pass + 1)); }

echo "============================================================"
echo " reminder-file seeding unit test (docker-free)"
echo "============================================================"

export HARNESS_SOURCE_ONLY=1
# shellcheck source=/dev/null
source "${REPO_ROOT}/harness"

TMP_ROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

# Fresh install_root + clone_dir per case; seed_reminder_file reads both.
new_case() {
    local d="$TMP_ROOT/$1"
    mkdir -p "$d/root" "$d/clone/proxy"
    printf '%s\n' "<!-- header -->" "[Reminder shipped default]" \
        >"$d/clone/proxy/reminder.md"
    install_root="$d/root"
    clone_dir="$d/clone"
}

# --- T1: seeds a missing file from the tracked default -----------------------
new_case t1
seed_reminder_file 2>/dev/null
[[ -f "$install_root/.harness-reminder.md" ]] \
    || fail "T1: .harness-reminder.md was not created"
diff -q "$clone_dir/proxy/reminder.md" "$install_root/.harness-reminder.md" \
    >/dev/null || fail "T1: seeded copy differs from proxy/reminder.md"
ok "T1: missing file is seeded byte-for-byte from the tracked default"

# --- T2: an existing (edited) copy is never overwritten ----------------------
new_case t2
printf '%s\n' "[Reminder MY OWN WORDING]" >"$install_root/.harness-reminder.md"
seed_reminder_file 2>/dev/null
grep -q "MY OWN WORDING" "$install_root/.harness-reminder.md" \
    || fail "T2: seeding clobbered the user's edited reminder"
ok "T2: an existing copy is left untouched (upgrade-safe)"

# --- T3: an EMPTY directory (docker's missing-mount-source artifact) is
#         removed and replaced with the real file -----------------------------
new_case t3
mkdir "$install_root/.harness-reminder.md"
seed_reminder_file 2>/dev/null
[[ -f "$install_root/.harness-reminder.md" ]] \
    || fail "T3: empty directory was not replaced by the seeded file"
grep -q "shipped default" "$install_root/.harness-reminder.md" \
    || fail "T3: replacement file does not carry the shipped default"
ok "T3: docker's empty-directory artifact is rmdir'd, then seeded"

# --- T4: a NON-empty directory is reported, not silently destroyed -----------
new_case t4
mkdir "$install_root/.harness-reminder.md"
: >"$install_root/.harness-reminder.md/something"
out=$(seed_reminder_file 2>&1) || fail "T4: seed_reminder_file returned non-zero"
[[ -d "$install_root/.harness-reminder.md" ]] \
    || fail "T4: a non-empty directory must not be removed"
[[ -f "$install_root/.harness-reminder.md/something" ]] \
    || fail "T4: contents of the directory were destroyed"
grep -q "non-empty directory" <<<"$out" \
    || fail "T4: expected a warning naming the non-empty directory, got: $out"
ok "T4: a non-empty directory is left alone and reported"

# --- T5: a missing tracked default warns instead of failing the start --------
# The proxy still runs (it falls back to the copy baked into the image), so
# this must never abort `harness start`.
new_case t5
rm "$clone_dir/proxy/reminder.md"
out=$(seed_reminder_file 2>&1) || fail "T5: seed_reminder_file returned non-zero"
[[ ! -e "$install_root/.harness-reminder.md" ]] \
    || fail "T5: nothing should be created when the tracked default is gone"
grep -q "proxy/reminder.md missing" <<<"$out" \
    || fail "T5: expected a warning about the missing default, got: $out"
ok "T5: a missing proxy/reminder.md warns and returns cleanly"

echo "------------------------------------------------------------"
echo "REMINDER SEED TEST PASSED (${pass} checks)"
