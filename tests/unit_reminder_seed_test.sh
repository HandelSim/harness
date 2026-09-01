#!/usr/bin/env bash
#
# tests/unit_reminder_seed_test.sh — exercise seed_reminder_file and
# seed_tool_guidance_file, the two wrappers over seed_user_data_file that put
# the user's editable prompt-data files in place.
#
# The hybrid reminder's prose and its per-tool entries are DATA, not code: the
# tracked defaults live at proxy/reminder.md and proxy/tool-guidance.json and
# are copied once to <install_root>/reminder.md and
# <install_root>/tool-guidance.json — next to .env and .harness-allowlist,
# where they are gitignored and user-owned. Same basenames as the tracked
# defaults, on purpose: that is what lets docker-compose mount both files off
# ${INSTALL_ROOT} with no path variable per file, while the unset-INSTALL_ROOT
# default still resolves to ./proxy. The invariants that matter:
#
#   - it seeds when the file is missing (so an existing install picks the
#     feature up on its next `harness start`, no upgrade action needed);
#   - it NEVER overwrites an existing copy, or a `harness upgrade` would
#     silently discard the user's wording;
#   - it removes an empty DIRECTORY left at that path. A compose bind-mount
#     whose source is missing makes docker create one; left in place it looks
#     like "already seeded" forever and mounts a directory over the file, so
#     the proxy silently runs on its built-in fallback (see F152, P064);
#   - it MOVES a copy left by either earlier layout (<install_root>/
#     .harness-reminder.md, .harness-tool-guidance.json, and the short-lived
#     .harness-data/ subdirectory) to the current path instead of seeding a
#     fresh default over the top of the user's edits (see F155).
#
# T1-T5 cover the shared helper through the reminder wrapper; T6-T8 cover the
# tool-guidance wrapper, which must seed a SEPARATE file with the same rules
# (see F154, P092); T9-T11 cover the legacy-layout migrations.
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

# Fresh install_root + clone_dir per case; the seeders read both.
new_case() {
    local d="$TMP_ROOT/$1"
    mkdir -p "$d/root" "$d/clone/proxy"
    printf '%s\n' "<!-- header -->" "[Reminder shipped default]" \
        >"$d/clone/proxy/reminder.md"
    printf '%s\n' '{"tools": {"bash": "shipped default line"}}' \
        >"$d/clone/proxy/tool-guidance.json"
    install_root="$d/root"
    clone_dir="$d/clone"
}

# --- T1: seeds a missing file from the tracked default -----------------------
new_case t1
seed_reminder_file 2>/dev/null
[[ -f "$install_root/reminder.md" ]] \
    || fail "T1: reminder.md was not created at the install root"
diff -q "$clone_dir/proxy/reminder.md" "$install_root/reminder.md" \
    >/dev/null || fail "T1: seeded copy differs from proxy/reminder.md"
ok "T1: missing file is seeded byte-for-byte from the tracked default"

# --- T2: an existing (edited) copy is never overwritten ----------------------
new_case t2
printf '%s\n' "[Reminder MY OWN WORDING]" >"$install_root/reminder.md"
seed_reminder_file 2>/dev/null
grep -q "MY OWN WORDING" "$install_root/reminder.md" \
    || fail "T2: seeding clobbered the user's edited reminder"
ok "T2: an existing copy is left untouched (upgrade-safe)"

# --- T3: an EMPTY directory (docker's missing-mount-source artifact) is
#         removed and replaced with the real file -----------------------------
new_case t3
mkdir "$install_root/reminder.md"
seed_reminder_file 2>/dev/null
[[ -f "$install_root/reminder.md" ]] \
    || fail "T3: empty directory was not replaced by the seeded file"
grep -q "shipped default" "$install_root/reminder.md" \
    || fail "T3: replacement file does not carry the shipped default"
ok "T3: docker's empty-directory artifact is rmdir'd, then seeded"

# --- T4: a NON-empty directory is reported, not silently destroyed -----------
new_case t4
mkdir "$install_root/reminder.md"
: >"$install_root/reminder.md/something"
out=$(seed_reminder_file 2>&1) || fail "T4: seed_reminder_file returned non-zero"
[[ -d "$install_root/reminder.md" ]] \
    || fail "T4: a non-empty directory must not be removed"
[[ -f "$install_root/reminder.md/something" ]] \
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
[[ ! -e "$install_root/reminder.md" ]] \
    || fail "T5: nothing should be created when the tracked default is gone"
grep -q "proxy/reminder.md missing" <<<"$out" \
    || fail "T5: expected a warning about the missing default, got: $out"
ok "T5: a missing proxy/reminder.md warns and returns cleanly"

# --- T6: the tool-guidance file is seeded independently of the reminder ------
new_case t6
seed_tool_guidance_file 2>/dev/null
[[ -f "$install_root/tool-guidance.json" ]] \
    || fail "T6: tool-guidance.json was not created at the install root"
diff -q "$clone_dir/proxy/tool-guidance.json" \
    "$install_root/tool-guidance.json" >/dev/null \
    || fail "T6: seeded copy differs from proxy/tool-guidance.json"
[[ ! -e "$install_root/reminder.md" ]] \
    || fail "T6: the guidance seeder must not touch the reminder file"
ok "T6: missing tool-guidance file is seeded byte-for-byte, on its own"

# --- T7: an edited tool-guidance copy is never overwritten -------------------
# Same upgrade-safety contract as the reminder: a `harness upgrade` must not
# discard retuned per-tool descriptions.
new_case t7
printf '%s\n' '{"tools": {"bash": "MY OWN LINE"}}' \
    >"$install_root/tool-guidance.json"
seed_tool_guidance_file 2>/dev/null
grep -q "MY OWN LINE" "$install_root/tool-guidance.json" \
    || fail "T7: seeding clobbered the user's edited guidance"
ok "T7: an existing tool-guidance copy is left untouched (upgrade-safe)"

# --- T8: docker's empty-directory artifact is handled here too ---------------
new_case t8
mkdir "$install_root/tool-guidance.json"
seed_tool_guidance_file 2>/dev/null
[[ -f "$install_root/tool-guidance.json" ]] \
    || fail "T8: empty directory was not replaced by the seeded file"
grep -q "shipped default line" "$install_root/tool-guidance.json" \
    || fail "T8: replacement file does not carry the shipped default"
ok "T8: docker's empty-directory artifact is rmdir'd, then seeded"

# --- T9: a dotted legacy reminder copy is MOVED, not replaced ----------------
# The first layout kept the user's copy at <install_root>/.harness-reminder.md.
# Seeding runs on every `harness start`, so this is the whole migration path:
# an install that never runs `harness upgrade` still ends up at the current
# path, with its edits intact.
new_case t9
printf '%s\n' "[Reminder MY LEGACY WORDING]" >"$install_root/.harness-reminder.md"
seed_reminder_file 2>/dev/null
[[ ! -e "$install_root/.harness-reminder.md" ]] \
    || fail "T9: the legacy dotted copy was left behind"
grep -q "MY LEGACY WORDING" "$install_root/reminder.md" \
    || fail "T9: the user's legacy wording did not survive the move"
ok "T9: a legacy .harness-reminder.md is moved to reminder.md, edits intact"

# --- T10: same migration for the tool-guidance copy, and a legacy file is
#          never allowed to clobber the current copy -------------------------
new_case t10
printf '%s\n' '{"tools": {"bash": "MY LEGACY LINE"}}' \
    >"$install_root/.harness-tool-guidance.json"
seed_tool_guidance_file 2>/dev/null
grep -q "MY LEGACY LINE" "$install_root/tool-guidance.json" \
    || fail "T10: the legacy guidance file was not migrated"
# Now plant a second legacy file next to the migrated one: the new copy wins.
printf '%s\n' '{"tools": {"bash": "STALE LEGACY LINE"}}' \
    >"$install_root/.harness-tool-guidance.json"
seed_tool_guidance_file 2>/dev/null
grep -q "MY LEGACY LINE" "$install_root/tool-guidance.json" \
    || fail "T10: a stale legacy file overwrote the current copy"
ok "T10: legacy guidance migrates once; it never overwrites the current copy"

# --- T11: the short-lived .harness-data/ layout migrates too, and the empty
#          directory it leaves behind is removed ------------------------------
new_case t11
mkdir "$install_root/.harness-data"
printf '%s\n' "[Reminder DATA DIR WORDING]" \
    >"$install_root/.harness-data/reminder.md"
seed_reminder_file 2>/dev/null
grep -q "DATA DIR WORDING" "$install_root/reminder.md" \
    || fail "T11: the .harness-data copy did not move up to the install root"
[[ ! -e "$install_root/.harness-data" ]] \
    || fail "T11: the emptied .harness-data directory was left behind"
ok "T11: a .harness-data/ copy moves up and the empty directory is rmdir'd"

echo "------------------------------------------------------------"
echo "REMINDER SEED TEST PASSED (${pass} checks)"
