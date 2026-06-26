#!/usr/bin/env bash
#
# tests/unit_config_test.sh — exercise the post-install setup commands added to
# the CLI: `harness config` (read/write/list/allowlist-sync) and
# `harness uninstall` (confirm gate + teardown), docker-free.
#
# Sources `harness` with HARNESS_SOURCE_ONLY=1 so main() never runs, pointed at
# a throwaway install root (HARNESS_INSTALL_ROOT) and allowlist
# (HARNESS_ALLOWLIST_PATH) so nothing touches a real install. Covers:
#   - _config_write_key: rewrites an existing key in place, appends a missing
#     one, preserves unrelated lines, and survives values with / & : @
#   - _config_read_key: returns the on-disk value, strips quotes
#   - _config_get: masks secret keys, prints plain values otherwise
#   - _config_set PROXY_API_URL: writes the key AND adds the host to the
#     allowlist (the firewall sync side-effect)
#   - cmd_uninstall: cancels on a non-"yes" answer (leaves files), tears down on
#     --yes, and refuses a "/" install root
#
# Prints "CONFIG TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " config / uninstall (post-install setup) unit test"
echo "============================================================"

fail() { echo "[config-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[config-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"

TMP_ROOT="$(mktemp -d)"
FAKE_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT" "$FAKE_HOME"' EXIT

ALLOWLIST="$TMP_ROOT/.harness-allowlist"
: >"$ALLOWLIST"

# Seed a representative .env before sourcing (harness sources it at startup).
cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://old.example.com
PROXY_API_KEY=sk-secret-value-123
DEFAULT_MODEL_NAME=gpt-4
PROXY_TIMEOUT=120
EOF

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" HARNESS_ALLOWLIST_PATH="$ALLOWLIST" \
    source "$HARNESS" 2>/dev/null

# When `harness` is sourced (not executed), its self-path resolves to THIS test
# script, so clone_dir points at tests/ and the real scripts/lib/ is unreachable.
# In a real install clone_dir IS the repo; pin it there so the allowlist-sync
# path (which loads scripts/lib/net_helpers.sh) exercises the real add, not the
# missing-helpers hint branch.
clone_dir="$REPO_ROOT"

for fn in _config_read_key _config_write_key _config_get _config_set \
          _config_sync_allowlist_from_url cmd_config cmd_uninstall; do
    [[ "$(type -t "$fn")" == "function" ]] || fail "$fn not sourced"
done
ok "config + uninstall functions sourced"

# --- T1: _config_read_key reads on-disk values, strips quotes ----------------
[[ "$(_config_read_key PROXY_TIMEOUT)" == "120" ]] || fail "T1: read PROXY_TIMEOUT"
[[ "$(_config_read_key DEFAULT_MODEL_NAME)" == "gpt-4" ]] || fail "T1: read DEFAULT_MODEL_NAME"
[[ -z "$(_config_read_key NOPE_MISSING)" ]] || fail "T1: absent key should read empty"
ok "T1: _config_read_key returns on-disk values, empty for absent"

# --- T2: _config_write_key rewrites in place, keeps other lines -------------
_config_write_key PROXY_TIMEOUT 300 || fail "T2: write returned non-zero"
[[ "$(_config_read_key PROXY_TIMEOUT)" == "300" ]] || fail "T2: value not updated"
# Unrelated keys untouched, and no duplicate line introduced.
[[ "$(grep -c '^PROXY_TIMEOUT=' "$TMP_ROOT/.env")" == "1" ]] || fail "T2: duplicate PROXY_TIMEOUT line"
[[ "$(_config_read_key DEFAULT_MODEL_NAME)" == "gpt-4" ]] || fail "T2: clobbered another key"
ok "T2: _config_write_key rewrites in place without touching siblings"

# --- T3: _config_write_key appends a missing key ----------------------------
_config_write_key MODEL_CONTEXT_LENGTH 200000 || fail "T3: write returned non-zero"
[[ "$(_config_read_key MODEL_CONTEXT_LENGTH)" == "200000" ]] || fail "T3: appended key not readable"
ok "T3: _config_write_key appends an absent key"

# --- T4: values with / & : @ survive (read-loop, not sed) -------------------
tricky='https://u:p@h.example.com/v1?a=1&b=2'
_config_write_key PROXY_API_URL "$tricky" || fail "T4: write returned non-zero"
[[ "$(_config_read_key PROXY_API_URL)" == "$tricky" ]] || fail "T4: special chars mangled: got $(_config_read_key PROXY_API_URL)"
ok "T4: values with / & : @ ? = round-trip intact"

# --- T5: _config_get masks secrets, shows plain values ----------------------
out="$(_config_get PROXY_API_KEY)"
grep -q 'set,' <<<"$out" || fail "T5: secret not masked — $out"
grep -q 'sk-secret-value-123' <<<"$out" && fail "T5: secret value leaked — $out"
out="$(_config_get PROXY_TIMEOUT)"
grep -q '300' <<<"$out" || fail "T5: plain value not shown — $out"
ok "T5: _config_get masks secret keys, prints non-secret values"

# --- T6: _config_set PROXY_API_URL also adds host to allowlist --------------
: >"$ALLOWLIST"
_config_set PROXY_API_URL "https://api.newhost.example:8443/v1" >/dev/null \
    || fail "T6: config set returned non-zero"
[[ "$(_config_read_key PROXY_API_URL)" == "https://api.newhost.example:8443/v1" ]] \
    || fail "T6: URL not written"
grep -qx 'api.newhost.example' "$ALLOWLIST" \
    || fail "T6: host not added to allowlist — $(cat "$ALLOWLIST")"
ok "T6: setting PROXY_API_URL writes the key and allowlists its host"

# --- T7: cmd_config get with no key lists the curated set -------------------
out="$(cmd_config get)"
grep -q 'PROXY_API_URL' <<<"$out" || fail "T7: picker set missing PROXY_API_URL — $out"
grep -q 'DEFAULT_MODEL_NAME' <<<"$out" || fail "T7: picker set missing DEFAULT_MODEL_NAME — $out"
ok "T7: 'config get' with no key lists the curated common settings"

# --- uninstall ---------------------------------------------------------------
# Override HOME so the wrapper path points into the throwaway home, and stub the
# runtime resolver to a nonexistent binary so the compose-down block is skipped
# (no docker contact). harness_container_runtime may be unset if platform.sh was
# absent; define/override it either way.
export HOME="$FAKE_HOME"
harness_container_runtime() { echo "definitely-not-a-real-runtime-binary"; }

mk_install() {
    mkdir -p "$TMP_ROOT/state" "$FAKE_HOME/.local/bin"
    : >"$TMP_ROOT/.env"
    : >"$FAKE_HOME/.local/bin/harness"
}

# --- T8: non-"yes" answer cancels, nothing removed --------------------------
mk_install
out="$(HARNESS_CONFIRM_FROM_STDIN=1 cmd_uninstall <<<'no')" || fail "T8: uninstall errored on cancel"
grep -qi 'cancel' <<<"$out" || fail "T8: cancel not reported — $out"
[[ -d "$TMP_ROOT" ]] || fail "T8: install root removed despite cancel"
[[ -f "$FAKE_HOME/.local/bin/harness" ]] || fail "T8: wrapper removed despite cancel"
ok "T8: a non-'yes' answer cancels and removes nothing"

# --- T9: --yes tears down state, install root, wrapper ----------------------
mk_install
out="$(cmd_uninstall --yes)" || fail "T9: uninstall --yes returned non-zero"
[[ ! -d "$TMP_ROOT" ]] || fail "T9: install root not removed — $TMP_ROOT still present"
[[ ! -f "$FAKE_HOME/.local/bin/harness" ]] || fail "T9: PATH wrapper not removed"
ok "T9: --yes stops, clears state, removes the install root and wrapper"

# --- T10: refuses a root install_root ---------------------------------------
( install_root="/"; cmd_uninstall --yes ) && fail "T10: should refuse install_root=/"
ok "T10: refuses to uninstall when install_root is '/'"

echo
echo "CONFIG TEST PASSED"
