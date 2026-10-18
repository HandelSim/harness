#!/usr/bin/env bash
#
# tests/unit_vault_test.sh — exercise `harness vault` and the launch-time vault
# helpers without docker or network: the "remote" is a local bare git repo.
#
#   - cmd_vault add/list/use/off/remove   registry + active-vault bookkeeping
#   - vault_prepare                       clones on first use, fast-forwards
#                                         after, warns and keeps the local copy
#                                         when the remote is unreachable
#   - vault_opencode_config               instructions + external_directory
#   - harness_opencode_overlay            merges with the --slim overlay
#
# Prints "VAULT TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " vault unit test"
echo "============================================================"

fail() { echo "[vault-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[vault-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v jq >/dev/null 2>&1 || fail "this test needs host jq"
command -v git >/dev/null 2>&1 || fail "this test needs git"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT/install" source "$HARNESS" 2>/dev/null
[[ "$(type -t cmd_vault)" == "function" ]] || fail "cmd_vault not sourced"
# Sourced from a test, clone_dir resolves off $0 and platform.sh is skipped;
# point both at the repo.
clone_dir="$REPO_ROOT"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/lib/platform.sh"
mkdir -p "$state_root"

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# A "remote" with one commit carrying AGENTS.md.
REMOTE="$TMP_ROOT/brain.git"
WORK="$TMP_ROOT/work"
git init -q --bare "$REMOTE"
git clone -q "$REMOTE" "$WORK" 2>/dev/null
printf 'vault rules\n' > "$WORK/AGENTS.md"
git -C "$WORK" add AGENTS.md
git -C "$WORK" commit -qm one
git -C "$WORK" push -q origin HEAD 2>/dev/null

# --- V1: add registers and the first vault becomes active --------------------
cmd_vault add brain "$REMOTE" >/dev/null || fail "V1: add failed"
[[ "$(vault_active_name)" == "brain" ]] || fail "V1: first vault should be active"
out=$(cmd_vault)
grep -q '^\* brain  .*(not cloned yet)' <<<"$out" || fail "V1: list should mark brain active and uncloned — $out"
ok "V1: add registers and activates the first vault"

# --- V2: duplicate name and bad names are refused ----------------------------
if cmd_vault add brain "$REMOTE" >/dev/null 2>&1; then fail "V2: duplicate add accepted"; fi
if cmd_vault add ../x "$REMOTE" >/dev/null 2>&1; then fail "V2: path-like name accepted"; fi
if cmd_vault add .hidden "$REMOTE" >/dev/null 2>&1; then fail "V2: dot-leading name accepted"; fi
cmd_vault add other "$REMOTE" >/dev/null || fail "V2: second add failed"
[[ "$(vault_active_name)" == "brain" ]] || fail "V2: a second add must not change the active vault"
ok "V2: duplicate/bad names refused; later adds keep the active vault"

# --- V3: prepare clones on first use, then fast-forwards ---------------------
dir=$(vault_prepare 2>/dev/null)
[[ "$dir" == "$(harness_abs_path "$state_root/vaults/brain")" ]] || fail "V3: prepare should echo the clone path — '$dir'"
[[ -f "$dir/AGENTS.md" ]] || fail "V3: clone is missing AGENTS.md"
printf 'more\n' > "$WORK/notes.md"
git -C "$WORK" add notes.md
git -C "$WORK" commit -qm two
git -C "$WORK" push -q origin HEAD 2>/dev/null
dir=$(vault_prepare 2>/dev/null)
[[ -f "$dir/notes.md" ]] || fail "V3: second prepare should fast-forward the clone"
ok "V3: prepare clones, then fast-forwards"

# --- V4: unreachable remote warns and keeps the local copy -------------------
mv "$REMOTE" "$REMOTE.away"
err_out=$(vault_prepare 2>&1 >/dev/null)
dir=$(vault_prepare 2>/dev/null)
mv "$REMOTE.away" "$REMOTE"
[[ -n "$dir" ]] || fail "V4: a failed pull must still return the local clone"
grep -q 'using the local copy' <<<"$err_out" || fail "V4: failed pull should warn — $err_out"
ok "V4: unreachable remote keeps the local copy"

# --- V5: the overlay points opencode at the vault ----------------------------
harness_vault_dir="$dir"
cfg=$(vault_opencode_config)
[[ "$(jq -r '.instructions[0]' <<<"$cfg")" == "$dir/AGENTS.md" ]] || fail "V5: instructions should name AGENTS.md — $cfg"
[[ "$(jq -r --arg k "$dir/*" '.permission.external_directory[$k]' <<<"$cfg")" == "allow" ]] \
    || fail "V5: external_directory should allow the vault — $cfg"
harness_slim=1
merged=$(harness_opencode_overlay)
jq -e '.agent.build.prompt and .provider.harness.options.headers["x-harness-slim"] == "1" and (.instructions | length == 1) and .permission.external_directory' \
    <<<"$merged" >/dev/null || fail "V5: slim + vault overlays should merge — $merged"
harness_slim=0
[[ "$(harness_opencode_overlay)" == "$cfg" ]] || fail "V5: without slim the overlay is the vault part alone"
mv "$dir/AGENTS.md" "$dir/AGENTS.away"
cfg=$(vault_opencode_config 2>/dev/null)
mv "$dir/AGENTS.away" "$dir/AGENTS.md"
jq -e 'has("instructions") | not' <<<"$cfg" >/dev/null || fail "V5: no AGENTS.md/CLAUDE.md means no instructions — $cfg"
harness_vault_dir=""
[[ -z "$(harness_opencode_overlay)" ]] || fail "V5: no vault and no slim means no overlay"
ok "V5: overlay carries instructions + external_directory, merges with slim"

# --- V6: use / off / remove --------------------------------------------------
cmd_vault use other >/dev/null || fail "V6: use failed"
[[ "$(vault_active_name)" == "other" ]] || fail "V6: use should switch the active vault"
if cmd_vault use nope >/dev/null 2>&1; then fail "V6: use of an unknown vault accepted"; fi
cmd_vault off >/dev/null
[[ -z "$(vault_active_name)" ]] || fail "V6: off should clear the active vault"
[[ -z "$(vault_prepare 2>/dev/null)" ]] || fail "V6: prepare with no active vault should print nothing"
cmd_vault use brain >/dev/null
cmd_vault remove brain >/dev/null || fail "V6: remove failed"
[[ -z "$(vault_active_name)" ]] || fail "V6: removing the active vault should clear it"
[[ -d "$state_root/vaults/brain/.git" ]] || fail "V6: remove must keep the clone"
_vault_url brain >/dev/null && fail "V6: brain still registered"
_vault_url other >/dev/null || fail "V6: remove dropped the wrong vault"
ok "V6: use/off/remove keep the registry consistent and never delete a clone"

echo "VAULT TEST PASSED"
