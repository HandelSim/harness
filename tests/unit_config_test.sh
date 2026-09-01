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
#   - cmd_config (no args): prints the subcommand usage so the user learns how to
#     drive it, then the current values (no-tty branch of the picker)
#   - cmd_uninstall: cancels on a non-"yes" answer (leaves files), tears down on
#     --yes, refuses a "/" install root, and (with a stubbed runtime) removes the
#     agent + compose containers by label, the compose images (--rmi all), and
#     the named built images
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

# --- T11: bare 'harness config' lists the subcommands -----------------------
# No args, no tty (the test runner's stdin is not a terminal) -> _config_overview
# prints the usage/subcommand block then the current values, instead of erroring.
mkdir -p "$TMP_ROOT"
cat >"$TMP_ROOT/.env" <<'EOF'
PROXY_API_URL=https://h.example.com
DEFAULT_MODEL_NAME=gpt-4
EOF
out="$(cmd_config </dev/null)" || fail "T11: bare cmd_config returned non-zero"
grep -q 'harness config get'  <<<"$out" || fail "T11: usage missing 'config get' — $out"
grep -q 'harness config set'  <<<"$out" || fail "T11: usage missing 'config set' — $out"
grep -q 'harness config list' <<<"$out" || fail "T11: usage missing 'config list' — $out"
grep -q 'DEFAULT_MODEL_NAME'  <<<"$out" || fail "T11: current values not shown — $out"
ok "T11: bare 'config' lists the subcommands then the current values"

# --- T12: uninstall removes agent containers + compose images + named images -
# Stub the runtime to a fake binary that LOGS every call, so the docker-free run
# can assert the teardown issues the container/image removals (not just state).
# RT_LOG must live outside the install root, since uninstall deletes the root
# (and everything under it) before these assertions run.
RT_LOG="$FAKE_HOME/rt.log"
FAKE_RT="$FAKE_HOME/fakebin/rt"
mkdir -p "$FAKE_HOME/fakebin"
cat >"$FAKE_RT" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"$RT_LOG"
case "\$1" in
    info)  exit 0 ;;          # daemon reachable -> enter the teardown block
    ps)    echo "agentcid123" ;;   # one agent container to remove
    image) exit 0 ;;          # 'image inspect' -> image exists -> rmi runs
    *)     exit 0 ;;
esac
EOF
chmod +x "$FAKE_RT"
harness_container_runtime() { echo "$FAKE_RT"; }

mk_install
: >"$RT_LOG"
out="$(cmd_uninstall --yes)" || fail "T12: uninstall --yes returned non-zero"
grep -q 'harness.agent=true'  "$RT_LOG" || fail "T12: agent containers not enumerated — $(cat "$RT_LOG")"
grep -q 'com.docker.compose.project=harness' "$RT_LOG" || fail "T12: compose containers not enumerated — $(cat "$RT_LOG")"
grep -q 'rm -f -v agentcid123' "$RT_LOG" || fail "T12: container not removed — $(cat "$RT_LOG")"
grep -q 'down --rmi all'      "$RT_LOG" || fail "T12: compose down missing --rmi all — $(cat "$RT_LOG")"
grep -q 'rmi -f harness-proxy:latest' "$RT_LOG" || fail "T12: harness-proxy image not removed — $(cat "$RT_LOG")"
grep -q 'rmi -f harness-agent:latest' "$RT_LOG" || fail "T12: harness-agent image not removed — $(cat "$RT_LOG")"
[[ ! -d "$TMP_ROOT" ]] || fail "T12: install root not removed"
ok "T12: --yes removes agent + compose containers, compose images, and named images"

# --- T13: _warn_unquoted_env_values names values `source` cannot read back ---
# An unquoted value carrying a space or a shell metacharacter is truncated by
# `source` (and its tail is run as a command); compose reads the whole line, so
# the two consumers of .env disagree. The scan must name exactly the offenders
# and stay silent on every legitimate line shape.
WARN_ENV="$FAKE_HOME/warn.env"
cat >"$WARN_ENV" <<'EOF'
# a comment with spaces ; and & in it
PROXY_API_URL=https://ok.example.com
PROXY_API_KEY=sk-fine_123
  export EXPORTED=bar
QUOTED_S='has spaces; and semis'
QUOTED_D="has spaces; too"
INLINE=bar # trailing comment
EMPTY=
DOLLAR=$HOME
BAD_SPACE=a b
BAD_SEMI=a=1; oai-did=2
BAD_AMP=a&b
EOF
warn_out="$(_warn_unquoted_env_values "$WARN_ENV" 2>&1 || true)"
for k in BAD_SPACE BAD_SEMI BAD_AMP; do
    grep -q "$k" <<<"$warn_out" || fail "T13: $k not reported — $warn_out"
done
for k in PROXY_API_URL PROXY_API_KEY EXPORTED QUOTED_S QUOTED_D INLINE EMPTY DOLLAR; do
    grep -q "$k" <<<"$warn_out" && fail "T13: false positive on $k — $warn_out"
done
grep -q 'single quotes' <<<"$warn_out" || fail "T13: warning gives no fix — $warn_out"
printf 'A=1\nB=plain\n' >"$WARN_ENV"
[[ -z "$(_warn_unquoted_env_values "$WARN_ENV" 2>&1 || true)" ]] \
    || fail "T13: clean .env produced output"
ok "T13: unquoted values with shell metacharacters are named before sourcing"

# --- T14: _config_value_truncated is the prefix test, not an inequality test -
_config_value_truncated 'a=1' 'a=1; b=2' || fail "T14: prefix not flagged"
_config_value_truncated 'a=1' 'a=1'      && fail "T14: identical values flagged"
_config_value_truncated 'a=1' 'z=9; a=1' && fail "T14: non-prefix difference flagged"
_config_value_truncated ''    'a=1; b=2' && fail "T14: empty sourced value flagged"
_config_value_truncated 'a=1' ''         && fail "T14: empty on-disk value flagged"
ok "T14: _config_value_truncated fires only on a strict prefix of the on-disk value"

echo
echo "CONFIG TEST PASSED"
