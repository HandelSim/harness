#!/usr/bin/env bash
#
# scripts/upgrade_test.sh — exercise the four upgrade action types and a
# synthetic version-N → N+1 upgrade end-to-end.
#
# This test does NOT require docker. It runs entirely against the host
# filesystem and the upgrade_actions library; the manifest runner inside
# `harness` is exercised by harness_test.sh under T16/T17. Here we focus on
# correctness of each individual action plus an integrated scenario where
# adding new env vars / hosts / json keys / MCP files to a "version N+1"
# repo, then running the actions against a "version N" install root,
# results in the user-customized state being preserved exactly while new
# items are introduced.
#
# Prints "UPGRADE TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/lib/upgrade_actions.sh"

# Pin the marker comment date so post-upgrade greps are deterministic.
export HARNESS_UPGRADE_DATE="2026-04-25"

echo "============================================================"
echo " upgrade test"
echo "============================================================"

# Per-test workdir under one parent so the trap can wipe everything.
WORK="$(mktemp -d -t harness-upg-test.XXXXXX)"
cleanup() {
    if [[ -n "${WORK:-}" && -d "${WORK}" ]]; then
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT INT TERM

fail() {
    echo "[upgrade-test] FAIL: $*" >&2
    exit 1
}

ok() {
    echo "[upgrade-test] OK: $*"
}

# Capture fields out of an action's JSON output.
json_field() {
    local key="$1" json="$2"
    jq -r ".$key" <<<"$json" 2>/dev/null
}

# === Test 1: envfile_merge core ===========================================

echo
echo "--- T1: envfile_merge ---"
T1_DIR="${WORK}/t1"
mkdir -p "${T1_DIR}"
cat >"${T1_DIR}/source.env" <<'EOF'
# header comment

# A is the first variable
A=default-A

# B is the second variable
B=default-B

# C is a NEW variable added in this version
C=default-C
EOF
cat >"${T1_DIR}/target.env" <<'EOF'
# user file

# A is the first variable
A=user-set-A

# B is the second variable
B=user-set-B
EOF

T1_OUT=$(upgrade_envfile_merge "${T1_DIR}/source.env" "${T1_DIR}/target.env" 0)
T1_ADDED=$(json_field 'added_keys | join(",")' "${T1_OUT}")
[[ "${T1_ADDED}" == "C" ]] || fail "T1: expected added_keys=[C], got [${T1_ADDED}]"
grep -q '^A=user-set-A$' "${T1_DIR}/target.env" || fail "T1: A value not preserved"
grep -q '^B=user-set-B$' "${T1_DIR}/target.env" || fail "T1: B value not preserved"
grep -q '^C=default-C$' "${T1_DIR}/target.env" || fail "T1: C not appended with default value"
grep -q "Added by harness upgrade on 2026-04-25" "${T1_DIR}/target.env" || fail "T1: marker comment missing"
grep -q "C is a NEW variable added in this version" "${T1_DIR}/target.env" || fail "T1: source comment context not carried"

# Idempotency: second run reports no changes.
T1_OUT2=$(upgrade_envfile_merge "${T1_DIR}/source.env" "${T1_DIR}/target.env" 0)
T1_ADDED2=$(json_field 'added_keys | length' "${T1_OUT2}")
[[ "${T1_ADDED2}" == "0" ]] || fail "T1: idempotency broken; second run added ${T1_ADDED2} key(s)"
ok "envfile_merge: append + comment context + value preservation + idempotency"

# Dry-run: no file modification.
cat >"${T1_DIR}/source.env" <<'EOF'
A=1
B=2
D=4
EOF
T1_DRY=$(upgrade_envfile_merge "${T1_DIR}/source.env" "${T1_DIR}/target.env" 1)
[[ "$(json_field 'added_keys | join(",")' "${T1_DRY}")" == "D" ]] || fail "T1 dry-run: expected D, got $(json_field 'added_keys | join(",")' "${T1_DRY}")"
grep -q '^D=' "${T1_DIR}/target.env" && fail "T1 dry-run: D was actually appended despite dry_run=1"
ok "envfile_merge dry-run: reports without modifying"

# === Test 2: linefile_merge core =========================================

echo
echo "--- T2: linefile_merge ---"
T2_DIR="${WORK}/t2"
mkdir -p "${T2_DIR}"
cat >"${T2_DIR}/source.list" <<'EOF'
# Section A
host-a.example
host-b.example   # git-push

# Section B
host-c.example
EOF
cat >"${T2_DIR}/target.list" <<'EOF'
# user-managed
host-a.example
host-b.example
EOF

T2_OUT=$(upgrade_linefile_merge "${T2_DIR}/source.list" "${T2_DIR}/target.list" 0)
T2_ADDED=$(json_field 'added_lines | join(",")' "${T2_OUT}")
[[ "${T2_ADDED}" == "host-c.example" ]] || fail "T2: expected added=[host-c.example], got [${T2_ADDED}]"
T2_WARN_COUNT=$(json_field 'warnings | length' "${T2_OUT}")
[[ "${T2_WARN_COUNT}" == "1" ]] || fail "T2: expected 1 warning for host-b.example annotation diff, got ${T2_WARN_COUNT}"
grep -Eq '^host-b\.example$' "${T2_DIR}/target.list" || fail "T2: host-b.example was not preserved (annotation diff should leave it alone)"
grep -q "host-c.example" "${T2_DIR}/target.list" || fail "T2: host-c.example not appended"

# Idempotency.
T2_OUT2=$(upgrade_linefile_merge "${T2_DIR}/source.list" "${T2_DIR}/target.list" 0)
[[ "$(json_field 'added_lines | length' "${T2_OUT2}")" == "0" ]] || fail "T2: idempotency broken"
ok "linefile_merge: append + annotation-diff warning + idempotency"

# === Test 3: json_merge core =============================================

echo
echo "--- T3: json_merge ---"
T3_DIR="${WORK}/t3"
mkdir -p "${T3_DIR}"
cat >"${T3_DIR}/source.json" <<'EOF'
{
  "version": 3,
  "lines": [["a", "b"]],
  "newKey": "new-value",
  "nested": {"existing": "from-source", "added": "yep"}
}
EOF
cat >"${T3_DIR}/target.json" <<'EOF'
{
  "version": 3,
  "lines": [["x", "y", "z"]],
  "userOnly": true,
  "nested": {"existing": "USER_VALUE"}
}
EOF
T3_OUT=$(upgrade_json_merge "${T3_DIR}/source.json" "${T3_DIR}/target.json" add_missing_keys 0)
T3_PATHS=$(json_field 'added_paths | sort | join(",")' "${T3_OUT}")
echo "${T3_PATHS}" | grep -q '\.newKey' || fail "T3: expected .newKey in added_paths; got ${T3_PATHS}"
echo "${T3_PATHS}" | grep -q '\.nested\.added' || fail "T3: expected .nested.added in added_paths; got ${T3_PATHS}"
[[ "$(jq -r '.userOnly' "${T3_DIR}/target.json")" == "true" ]] || fail "T3: userOnly preserved"
[[ "$(jq -r '.nested.existing' "${T3_DIR}/target.json")" == "USER_VALUE" ]] || fail "T3: nested.existing was overwritten — DEALBREAKER"
[[ "$(jq -r '.lines | length' "${T3_DIR}/target.json")" == "1" ]] || fail "T3: user array got merged/extended; should be user-wins"
[[ "$(jq -r '.lines[0] | length' "${T3_DIR}/target.json")" == "3" ]] || fail "T3: user array contents changed"
[[ "$(jq -r '.newKey' "${T3_DIR}/target.json")" == "new-value" ]] || fail "T3: newKey not added"
[[ "$(jq -r '.nested.added' "${T3_DIR}/target.json")" == "yep" ]] || fail "T3: nested.added not added"

# Idempotency.
T3_OUT2=$(upgrade_json_merge "${T3_DIR}/source.json" "${T3_DIR}/target.json" add_missing_keys 0)
[[ "$(json_field 'added_paths | length' "${T3_OUT2}")" == "0" ]] || fail "T3: idempotency broken"
ok "json_merge: deep merge + user-wins + array conservatism + idempotency"

# === Test 4: directory_overwrite core ====================================

echo
echo "--- T4: directory_overwrite ---"
T4_DIR="${WORK}/t4"
mkdir -p "${T4_DIR}"/{src,tgt/data,tgt/sub}
echo "v2-compose"  >"${T4_DIR}/src/compose.yml"
echo "v2-readme"   >"${T4_DIR}/src/README.md"
mkdir -p "${T4_DIR}/src/sub"
echo "v2-deep"     >"${T4_DIR}/src/sub/deep.txt"

echo "v1-compose"  >"${T4_DIR}/tgt/compose.yml"
echo "user-state"  >"${T4_DIR}/tgt/data/user.txt"
echo '{"enabled":false}' >"${T4_DIR}/tgt/harness-meta.json"
echo "user-extra"  >"${T4_DIR}/tgt/sub/user-extra.txt"

T4_OUT=$(upgrade_directory_overwrite "${T4_DIR}/src" "${T4_DIR}/tgt" 0 harness-meta.json data/)
[[ "$(jq -r '.action' <<<"${T4_OUT}")" == "directory_overwrite" ]] || fail "T4: action mismatch in JSON output"

# Updated:
[[ "$(cat "${T4_DIR}/tgt/compose.yml")" == "v2-compose" ]] || fail "T4: compose.yml not updated"
[[ "$(cat "${T4_DIR}/tgt/README.md")" == "v2-readme" ]] || fail "T4: README.md not added"
[[ "$(cat "${T4_DIR}/tgt/sub/deep.txt")" == "v2-deep" ]] || fail "T4: nested file not updated"

# Preserved:
[[ "$(cat "${T4_DIR}/tgt/data/user.txt")" == "user-state" ]] || fail "T4: data/ was clobbered — DEALBREAKER"
[[ "$(jq -r '.enabled' "${T4_DIR}/tgt/harness-meta.json")" == "false" ]] || fail "T4: harness-meta.json was overwritten — DEALBREAKER"

# Files in target not in source: left in place.
[[ "$(cat "${T4_DIR}/tgt/sub/user-extra.txt")" == "user-extra" ]] || fail "T4: user-extra file was deleted (should be left alone)"

ok "directory_overwrite: update + preserve + non-destructive"

# === Test 5: edge cases ==================================================

echo
echo "--- T5: edge cases ---"

# 5.1: empty source envfile.
T5_DIR="${WORK}/t5"
mkdir -p "${T5_DIR}"
: >"${T5_DIR}/empty.env"
echo "X=1" >"${T5_DIR}/target.env"
T5A=$(upgrade_envfile_merge "${T5_DIR}/empty.env" "${T5_DIR}/target.env" 0)
[[ "$(json_field 'added_keys | length' "${T5A}")" == "0" ]] || fail "T5.1: empty source produced additions"
ok "T5.1: empty source envfile is a no-op"

# 5.2: source with only comments.
cat >"${T5_DIR}/comments.env" <<'EOF'
# only comments here
# nothing else
EOF
T5B=$(upgrade_envfile_merge "${T5_DIR}/comments.env" "${T5_DIR}/target.env" 0)
[[ "$(json_field 'added_keys | length' "${T5B}")" == "0" ]] || fail "T5.2: comment-only source produced additions"
ok "T5.2: comment-only source is a no-op"

# 5.3: target missing — envfile_merge creates it.
rm -f "${T5_DIR}/missing.env"
echo "Y=2" >"${T5_DIR}/source.env"
T5C=$(upgrade_envfile_merge "${T5_DIR}/source.env" "${T5_DIR}/missing.env" 0)
[[ -f "${T5_DIR}/missing.env" ]] || fail "T5.3: target was not created"
grep -q '^Y=2$' "${T5_DIR}/missing.env" || fail "T5.3: created target lacks source content"
[[ "$(json_field 'created' "${T5C}")" == "true" ]] || fail "T5.3: created flag missing in JSON output"
ok "T5.3: missing target is created from source"

# 5.4: malformed JSON target — refuses to overwrite.
cat >"${T5_DIR}/source.json" <<'EOF'
{"a": 1}
EOF
echo 'this is not json {' >"${T5_DIR}/bad.json"
T5D_RC=0
T5D=$(upgrade_json_merge "${T5_DIR}/source.json" "${T5_DIR}/bad.json" add_missing_keys 0) || T5D_RC=$?
(( T5D_RC != 0 )) || fail "T5.4: malformed target should have triggered nonzero rc"
[[ "$(json_field 'skipped' "${T5D}")" == "true" ]] || fail "T5.4: skipped flag missing"
[[ "$(cat "${T5_DIR}/bad.json")" == 'this is not json {' ]] || fail "T5.4: malformed target was modified — DEALBREAKER"
ok "T5.4: malformed JSON target is left untouched"

# 5.5: malformed JSON source — refuses to apply.
cat >"${T5_DIR}/bad-src.json" <<'EOF'
{not json
EOF
cat >"${T5_DIR}/good-tgt.json" <<'EOF'
{"a": 1}
EOF
T5E_RC=0
T5E=$(upgrade_json_merge "${T5_DIR}/bad-src.json" "${T5_DIR}/good-tgt.json" add_missing_keys 0) || T5E_RC=$?
(( T5E_RC != 0 )) || fail "T5.5: malformed source should have triggered nonzero rc"
[[ "$(jq -c . "${T5_DIR}/good-tgt.json")" == '{"a":1}' ]] || fail "T5.5: target was modified despite source error"
ok "T5.5: malformed JSON source leaves target intact"

# 5.6: linefile annotation discrepancy preserved (not modified).
cat >"${T5_DIR}/src.list" <<'EOF'
github.com   # git-push
EOF
cat >"${T5_DIR}/tgt.list" <<'EOF'
github.com
EOF
T5F=$(upgrade_linefile_merge "${T5_DIR}/src.list" "${T5_DIR}/tgt.list" 0)
[[ "$(json_field 'warnings | length' "${T5F}")" == "1" ]] || fail "T5.6: expected 1 warning"
grep -Eq '^github\.com$' "${T5_DIR}/tgt.list" || fail "T5.6: target line should not have been modified"
ok "T5.6: linefile annotation discrepancy yields warning, no modification"

# === Test 6: synthetic version-N → N+1 end-to-end ========================
#
# Build a synthetic install root mirroring "version N" and a synthetic repo
# tree mirroring "version N+1". Run every manifest action by hand. Assert
# the user state is preserved while new entities show up.

echo
echo "--- T6: synthetic version-N → N+1 upgrade ---"
T6_REPO="${WORK}/t6/repo"
T6_INST="${WORK}/t6/install"
mkdir -p "${T6_REPO}"/{agents/claude/defaults,mcp-registry/_test_mcp,mcp-registry/_test_mcp/data}
mkdir -p "${T6_INST}"/{agent/claude/.config/ccstatusline,mcp/_test_mcp/data}

# --- N+1 repo state ---
cat >"${T6_REPO}/.env.example" <<'EOF'
# REQUIRED. Upstream API URL.
PROXY_API_URL=
# Upstream timeout in seconds.
PROXY_TIMEOUT=180
# NEW in this version: agent model name.
HARNESS_NEW_VAR_A=default-a
# NEW in this version: yet another knob.
HARNESS_NEW_VAR_B=default-b
EOF
cat >"${T6_REPO}/.harness-allowlist.example" <<'EOF'
github.com
api.github.com
new-host.example
EOF
cat >"${T6_REPO}/agents/claude/defaults/ccstatusline-settings.json" <<'EOF'
{
  "version": 4,
  "lines": [
    [{"id": "1", "type": "model"}],
    [],
    []
  ],
  "newColorScheme": "auto"
}
EOF
cat >"${T6_REPO}/mcp-registry/_test_mcp/compose.yml" <<'EOF'
# v2 compose for the test MCP
services:
  _test_mcp:
    image: example/test:v2
EOF
cat >"${T6_REPO}/mcp-registry/_test_mcp/client-config.json" <<'EOF'
{ "mcpServers": { "_test_mcp": { "type": "sse", "url": "http://test:1/" } } }
EOF
cat >"${T6_REPO}/mcp-registry/_test_mcp/harness-meta.json.template" <<'EOF'
{ "enabled": true }
EOF

# --- N install state ---
cat >"${T6_INST}/.env" <<'EOF'
# user comments
PROXY_API_URL=https://my-llm.example/v1
# user kept the default for timeout
PROXY_TIMEOUT=180
EOF
cat >"${T6_INST}/.harness-allowlist" <<'EOF'
# user-customized header
github.com   # git-push
api.github.com
my-corp.example
EOF
cat >"${T6_INST}/agent/claude/.config/ccstatusline/settings.json" <<'EOF'
{
  "version": 3,
  "lines": [
    [{"id": "1", "type": "model", "color": "magenta"}],
    [],
    []
  ]
}
EOF
cat >"${T6_INST}/mcp/_test_mcp/compose.yml" <<'EOF'
# v1 compose
services:
  _test_mcp:
    image: example/test:v1
EOF
cat >"${T6_INST}/mcp/_test_mcp/harness-meta.json" <<'EOF'
{ "enabled": false }
EOF
echo "important user data" >"${T6_INST}/mcp/_test_mcp/data/important_user_data.txt"

# Build a synthetic manifest pointing at these tmp paths.
cat >"${T6_REPO}/manifest.json" <<EOF
{
  "version": 1,
  "actions": [
    {"id":"env_vars","type":"envfile_merge","source":".env.example","target_relative":".env","description":"merge env"},
    {"id":"allow","type":"linefile_merge","source":".harness-allowlist.example","target_relative":".harness-allowlist","description":"merge allowlist"},
    {"id":"ccstatus","type":"json_merge","source":"agents/claude/defaults/ccstatusline-settings.json","target_relative":"agent/claude/.config/ccstatusline/settings.json","strategy":"add_missing_keys","description":"merge ccstatus"}
  ],
  "registry_actions": [
    {"id":"_test_mcp","type":"directory_overwrite","source":"mcp-registry/_test_mcp","target_relative":"mcp/_test_mcp","preserve":["harness-meta.json","data/","data"],"condition":"installed","description":"refresh test MCP"}
  ]
}
EOF

# --- 6a: dry-run reports correct delta and modifies nothing ---
ENV_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/.env")
ALLOW_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/.harness-allowlist")
JSON_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/agent/claude/.config/ccstatusline/settings.json")
COMPOSE_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/mcp/_test_mcp/compose.yml")

DRY_OUT=$(upgrade_envfile_merge "${T6_REPO}/.env.example" "${T6_INST}/.env" 1)
[[ "$(json_field 'added_keys | sort | join(",")' "${DRY_OUT}")" == "HARNESS_NEW_VAR_A,HARNESS_NEW_VAR_B" ]] \
    || fail "T6.dry: env diff incorrect: got $(json_field 'added_keys | sort | join(",")' "${DRY_OUT}")"

DRY_OUT=$(upgrade_linefile_merge "${T6_REPO}/.harness-allowlist.example" "${T6_INST}/.harness-allowlist" 1)
[[ "$(json_field 'added_lines | join(",")' "${DRY_OUT}")" == "new-host.example" ]] \
    || fail "T6.dry: allowlist diff incorrect: got $(json_field 'added_lines | join(",")' "${DRY_OUT}")"
# github.com annotation discrepancy → warning, not modification.
[[ "$(json_field 'warnings | length' "${DRY_OUT}")" == "1" ]] || fail "T6.dry: expected github.com annotation warning"

DRY_OUT=$(upgrade_json_merge "${T6_REPO}/agents/claude/defaults/ccstatusline-settings.json" \
    "${T6_INST}/agent/claude/.config/ccstatusline/settings.json" add_missing_keys 1)
PATHS_DIFF=$(json_field 'added_paths | sort | join(",")' "${DRY_OUT}")
echo "${PATHS_DIFF}" | grep -q '\.newColorScheme' || fail "T6.dry: ccstatus added_paths missing newColorScheme: ${PATHS_DIFF}"

# Mtimes unchanged → confirms dry run.
[[ "$(stat -c '%Y' "${T6_INST}/.env")" == "${ENV_MTIME_BEFORE}" ]] || fail "T6.dry: .env mtime changed"
[[ "$(stat -c '%Y' "${T6_INST}/.harness-allowlist")" == "${ALLOW_MTIME_BEFORE}" ]] || fail "T6.dry: allowlist mtime changed"
[[ "$(stat -c '%Y' "${T6_INST}/agent/claude/.config/ccstatusline/settings.json")" == "${JSON_MTIME_BEFORE}" ]] || fail "T6.dry: ccstatus mtime changed"
[[ "$(stat -c '%Y' "${T6_INST}/mcp/_test_mcp/compose.yml")" == "${COMPOSE_MTIME_BEFORE}" ]] || fail "T6.dry: compose.yml mtime changed"
ok "T6.dry: dry-run reports deltas without modifying files"

# --- 6b: apply mode actually mutates ---
upgrade_envfile_merge "${T6_REPO}/.env.example" "${T6_INST}/.env" 0 >/dev/null
upgrade_linefile_merge "${T6_REPO}/.harness-allowlist.example" "${T6_INST}/.harness-allowlist" 0 >/dev/null
upgrade_json_merge "${T6_REPO}/agents/claude/defaults/ccstatusline-settings.json" \
    "${T6_INST}/agent/claude/.config/ccstatusline/settings.json" add_missing_keys 0 >/dev/null
upgrade_directory_overwrite "${T6_REPO}/mcp-registry/_test_mcp" "${T6_INST}/mcp/_test_mcp" 0 \
    harness-meta.json data/ data >/dev/null

# Verify .env: original 2 vars unchanged + 2 new vars with defaults.
grep -q '^PROXY_API_URL=https://my-llm.example/v1$' "${T6_INST}/.env" || fail "T6.apply: PROXY_API_URL value not preserved"
grep -q '^PROXY_TIMEOUT=180$' "${T6_INST}/.env" || fail "T6.apply: PROXY_TIMEOUT value not preserved"
grep -q '^HARNESS_NEW_VAR_A=default-a$' "${T6_INST}/.env" || fail "T6.apply: HARNESS_NEW_VAR_A not added with default"
grep -q '^HARNESS_NEW_VAR_B=default-b$' "${T6_INST}/.env" || fail "T6.apply: HARNESS_NEW_VAR_B not added with default"
grep -q "Added by harness upgrade on 2026-04-25" "${T6_INST}/.env" || fail "T6.apply: marker comment missing in .env"
grep -q "user comments" "${T6_INST}/.env" || fail "T6.apply: existing user comments not preserved"

# Verify allowlist: new-host.example appended; github.com line unchanged (annotation discrepancy preserved user form).
grep -q '^new-host.example$' "${T6_INST}/.harness-allowlist" || fail "T6.apply: new-host.example missing"
grep -Eq '^github\.com[[:space:]]+# git-push$' "${T6_INST}/.harness-allowlist" || fail "T6.apply: user's github.com # git-push annotation lost"
grep -q '^my-corp.example$' "${T6_INST}/.harness-allowlist" || fail "T6.apply: user-only host my-corp.example removed"

# Verify ccstatusline: user color preserved, newColorScheme added.
[[ "$(jq -r '.lines[0][0].color' "${T6_INST}/agent/claude/.config/ccstatusline/settings.json")" == "magenta" ]] \
    || fail "T6.apply: user color preserved (should still be magenta)"
[[ "$(jq -r '.newColorScheme' "${T6_INST}/agent/claude/.config/ccstatusline/settings.json")" == "auto" ]] \
    || fail "T6.apply: newColorScheme not added"
[[ "$(jq -r '.version' "${T6_INST}/agent/claude/.config/ccstatusline/settings.json")" == "3" ]] \
    || fail "T6.apply: version was overwritten (3 → 4) — DEALBREAKER (user value clobbered)"

# Verify mcp/_test_mcp: compose.yml updated, harness-meta.json preserved (enabled=false), data preserved.
grep -q "v2 compose" "${T6_INST}/mcp/_test_mcp/compose.yml" || fail "T6.apply: compose.yml not updated"
[[ "$(jq -r '.enabled' "${T6_INST}/mcp/_test_mcp/harness-meta.json")" == "false" ]] \
    || fail "T6.apply: harness-meta.json was overwritten — DEALBREAKER"
[[ "$(cat "${T6_INST}/mcp/_test_mcp/data/important_user_data.txt")" == "important user data" ]] \
    || fail "T6.apply: data/ was clobbered — DEALBREAKER"
[[ -f "${T6_INST}/mcp/_test_mcp/client-config.json" ]] || fail "T6.apply: client-config.json not added"
ok "T6.apply: full version-N → N+1 upgrade preserves user state and adds new content"

# --- 6c: idempotency ---
RERUN_OUT=$(upgrade_envfile_merge "${T6_REPO}/.env.example" "${T6_INST}/.env" 0)
[[ "$(json_field 'added_keys | length' "${RERUN_OUT}")" == "0" ]] || fail "T6.rerun: env_vars not idempotent"
RERUN_OUT=$(upgrade_linefile_merge "${T6_REPO}/.harness-allowlist.example" "${T6_INST}/.harness-allowlist" 0)
[[ "$(json_field 'added_lines | length' "${RERUN_OUT}")" == "0" ]] || fail "T6.rerun: allowlist not idempotent"
RERUN_OUT=$(upgrade_json_merge "${T6_REPO}/agents/claude/defaults/ccstatusline-settings.json" \
    "${T6_INST}/agent/claude/.config/ccstatusline/settings.json" add_missing_keys 0)
[[ "$(json_field 'added_paths | length' "${RERUN_OUT}")" == "0" ]] || fail "T6.rerun: json_merge not idempotent"
ok "T6.rerun: idempotent — repeat runs produce zero changes"

# === Test 7: rsync-fallback path =========================================
#
# Force the directory_overwrite shell-loop fallback by shadowing `command`
# so the rsync probe fails. Verify the result matches the rsync path.

echo
echo "--- T7: directory_overwrite rsync fallback ---"
T7_DIR="${WORK}/t7"
mkdir -p "${T7_DIR}"/{src,tgt/data}
echo v2 >"${T7_DIR}/src/file.txt"
echo "{\"a\":1}" >"${T7_DIR}/src/conf.json"
echo v1 >"${T7_DIR}/tgt/file.txt"
echo user >"${T7_DIR}/tgt/data/user.txt"

(
    # Subshell: shadow `command -v rsync` to force the fallback.
    command() {
        if [[ "${1:-}" == "-v" && "${2:-}" == "rsync" ]]; then return 1; fi
        builtin command "$@"
    }
    # Re-source so the function definition takes effect inside this scope.
    HARNESS_UPGRADE_ACTIONS_LOADED=""
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/scripts/lib/upgrade_actions.sh"
    upgrade_directory_overwrite "${T7_DIR}/src" "${T7_DIR}/tgt" 0 data/ data >/dev/null
)
[[ "$(cat "${T7_DIR}/tgt/file.txt")" == "v2" ]] || fail "T7: fallback did not update file.txt"
[[ "$(cat "${T7_DIR}/tgt/conf.json")" == '{"a":1}' ]] || fail "T7: fallback did not add conf.json"
[[ "$(cat "${T7_DIR}/tgt/data/user.txt")" == "user" ]] || fail "T7: fallback did not preserve data/"
ok "T7: rsync-fallback shell loop produces identical result"

echo
echo "============================================================"
echo " UPGRADE TEST PASSED"
echo "============================================================"
