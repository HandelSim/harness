#!/usr/bin/env bash
#
# tests/upgrade_test.sh — exercise the upgrade action types and a
# synthetic version-N → N+1 upgrade end-to-end.
#
# This test does NOT require docker. It runs entirely against the host
# filesystem and the upgrade_actions library; the manifest runner inside
# `harness` is exercised by harness_test.sh under T16/T17. Here we focus on
# correctness of each individual action plus an integrated scenario where
# adding new env vars / hosts / MCP files to a "version N+1" repo, then
# running the actions against a "version N" install root, results in the
# user-customized state being preserved exactly while new items are
# introduced.
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

# === Test 2b: linefile_merge — target missing trailing newline ===========
#
# U012 in the inventory: `upgrade_linefile_merge` must ensure the target
# file ends with a newline before appending. Without this safety net, a
# target file that was hand-edited (or produced by a tool that omits the
# trailing newline) would have the first appended line glued onto the
# last existing entry, e.g. `host-b.example# Added by harness upgrade...`,
# silently corrupting the user's allowlist.
#
# The default T2 target above is constructed via a heredoc which always
# ends in \n, so the newline-injection path is NOT exercised by T2. This
# block constructs a target with NO trailing newline and asserts that the
# appended content lives on its own line.

echo
echo "--- T2b: linefile_merge into target without trailing newline ---"
T2B_DIR="${WORK}/t2b"
mkdir -p "${T2B_DIR}"
cat >"${T2B_DIR}/source.list" <<'EOF'
host-a.example
host-new.example
EOF
# Target ends with the literal bytes "host-a.example" — no trailing \n.
# Use printf (not heredoc / not echo) to guarantee absence of newline.
printf 'host-a.example' >"${T2B_DIR}/target.list"

# Inventory U012: target must NOT end with a newline before the merge
# (precondition for this test — guards against future refactors that
# accidentally normalize the fixture). `tail -c 1 | od` survives bash's
# command-substitution stripping of trailing newlines (a naked `$(tail
# -c 1 ...)` would return an empty string for both "ends with \n" and
# "is empty", which is exactly the bug we are trying to NOT have here).
T2B_PRE_LAST=$(tail -c 1 "${T2B_DIR}/target.list" | od -An -tx1 | tr -d ' \n')
[[ "${T2B_PRE_LAST}" != "0a" ]] \
    || fail "T2b: precondition violated — target fixture already ends in newline"

T2B_OUT=$(upgrade_linefile_merge "${T2B_DIR}/source.list" "${T2B_DIR}/target.list" 0)
T2B_ADDED=$(json_field 'added_lines | join(",")' "${T2B_OUT}")
[[ "${T2B_ADDED}" == "host-new.example" ]] \
    || fail "T2b: expected added=[host-new.example], got [${T2B_ADDED}]"

# Inventory U012: appended line must live on its own line — no
# `host-a.examplehost-new.example` or `host-a.example# Added by...`
# glued-together result.
grep -Eq '^host-new\.example$' "${T2B_DIR}/target.list" \
    || fail "U012: host-new.example not on its own line in target after merge"

# Inventory U012: the original last entry must remain intact on its own
# line (no junk suffix from a missing-newline-induced glue).
grep -Eq '^host-a\.example$' "${T2B_DIR}/target.list" \
    || fail "U012: host-a.example was corrupted by missing-newline append"

# Inventory U012: the post-merge target must end with a newline (the
# injected newline + the appended block, which itself ends in \n).
# See precondition note above re: od for byte-accurate comparison.
T2B_POST_LAST=$(tail -c 1 "${T2B_DIR}/target.list" | od -An -tx1 | tr -d ' \n')
[[ "${T2B_POST_LAST}" == "0a" ]] \
    || fail "U012: post-merge target does not end with newline (last byte hex: ${T2B_POST_LAST})"

# Inventory U012: the marker comment from the merge must be on its own
# line, not glued to the previous entry.
grep -Eq '^# Added by harness upgrade on 2026-04-25$' "${T2B_DIR}/target.list" \
    || fail "U012: marker comment is not on its own line in the post-merge target"

ok "T2b: linefile_merge injects newline before appending when target lacks one"

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

# 5.4: linefile annotation discrepancy preserved (not modified).
cat >"${T5_DIR}/src.list" <<'EOF'
github.com   # git-push
EOF
cat >"${T5_DIR}/tgt.list" <<'EOF'
github.com
EOF
T5F=$(upgrade_linefile_merge "${T5_DIR}/src.list" "${T5_DIR}/tgt.list" 0)
[[ "$(json_field 'warnings | length' "${T5F}")" == "1" ]] || fail "T5.4: expected 1 warning"
grep -Eq '^github\.com$' "${T5_DIR}/tgt.list" || fail "T5.4: target line should not have been modified"
ok "T5.4: linefile annotation discrepancy yields warning, no modification"

# 5.5: comment-only source + missing target — the created-path summary
# builds added_keys from an empty bash array. The single-jq-call emit
# feeds keys to jq on stdin via `printf '%s\n' "${added[@]}"`, which
# prints one empty line when the array is empty; the `select(.!="")`
# filter must drop it so added_keys is `[]`, not `[""]`.
rm -f "${T5_DIR}/created-empty.env"
T5G=$(upgrade_envfile_merge "${T5_DIR}/comments.env" "${T5_DIR}/created-empty.env" 0)
[[ -f "${T5_DIR}/created-empty.env" ]] || fail "T5.5: target was not created"
[[ "$(json_field 'created' "${T5G}")" == "true" ]] || fail "T5.5: created flag missing"
[[ "$(json_field 'added_keys | length' "${T5G}")" == "0" ]] \
    || fail "T5.5: empty added_keys leaked a phantom entry (got $(json_field 'added_keys' "${T5G}"))"
ok "T5.5: created-path emit yields added_keys=[] for a comment-only source"

# 5.6: same empty-array edge for linefile_merge's created path.
printf '# only a comment\n' >"${T5_DIR}/comments.list"
rm -f "${T5_DIR}/created-empty.list"
T5H=$(upgrade_linefile_merge "${T5_DIR}/comments.list" "${T5_DIR}/created-empty.list" 0)
[[ -f "${T5_DIR}/created-empty.list" ]] || fail "T5.6: target was not created"
[[ "$(json_field 'created' "${T5H}")" == "true" ]] || fail "T5.6: created flag missing"
[[ "$(json_field 'added_lines | length' "${T5H}")" == "0" ]] \
    || fail "T5.6: empty added_lines leaked a phantom entry (got $(json_field 'added_lines' "${T5H}"))"
ok "T5.6: created-path emit yields added_lines=[] for a comment-only source"

# === Test 6: synthetic version-N → N+1 end-to-end ========================
#
# Build a synthetic install root mirroring "version N" and a synthetic repo
# tree mirroring "version N+1". Run every manifest action by hand. Assert
# the user state is preserved while new entities show up.

echo
echo "--- T6: synthetic version-N → N+1 upgrade ---"
T6_REPO="${WORK}/t6/repo"
T6_INST="${WORK}/t6/install"
mkdir -p "${T6_REPO}/mcp-registry/_test_mcp/data"
mkdir -p "${T6_INST}/mcp/_test_mcp/data"

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
    {"id":"allow","type":"linefile_merge","source":".harness-allowlist.example","target_relative":".harness-allowlist","description":"merge allowlist"}
  ],
  "registry_actions": [
    {"id":"_test_mcp","type":"directory_overwrite","source":"mcp-registry/_test_mcp","target_relative":"mcp/_test_mcp","preserve":["harness-meta.json","data/","data"],"condition":"installed","description":"refresh test MCP"}
  ]
}
EOF

# --- 6a: dry-run reports correct delta and modifies nothing ---
ENV_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/.env")
ALLOW_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/.harness-allowlist")
COMPOSE_MTIME_BEFORE=$(stat -c '%Y' "${T6_INST}/mcp/_test_mcp/compose.yml")

DRY_OUT=$(upgrade_envfile_merge "${T6_REPO}/.env.example" "${T6_INST}/.env" 1)
[[ "$(json_field 'added_keys | sort | join(",")' "${DRY_OUT}")" == "HARNESS_NEW_VAR_A,HARNESS_NEW_VAR_B" ]] \
    || fail "T6.dry: env diff incorrect: got $(json_field 'added_keys | sort | join(",")' "${DRY_OUT}")"

DRY_OUT=$(upgrade_linefile_merge "${T6_REPO}/.harness-allowlist.example" "${T6_INST}/.harness-allowlist" 1)
[[ "$(json_field 'added_lines | join(",")' "${DRY_OUT}")" == "new-host.example" ]] \
    || fail "T6.dry: allowlist diff incorrect: got $(json_field 'added_lines | join(",")' "${DRY_OUT}")"
# github.com annotation discrepancy → warning, not modification.
[[ "$(json_field 'warnings | length' "${DRY_OUT}")" == "1" ]] || fail "T6.dry: expected github.com annotation warning"

# Mtimes unchanged → confirms dry run.
[[ "$(stat -c '%Y' "${T6_INST}/.env")" == "${ENV_MTIME_BEFORE}" ]] || fail "T6.dry: .env mtime changed"
[[ "$(stat -c '%Y' "${T6_INST}/.harness-allowlist")" == "${ALLOW_MTIME_BEFORE}" ]] || fail "T6.dry: allowlist mtime changed"
[[ "$(stat -c '%Y' "${T6_INST}/mcp/_test_mcp/compose.yml")" == "${COMPOSE_MTIME_BEFORE}" ]] || fail "T6.dry: compose.yml mtime changed"
ok "T6.dry: dry-run reports deltas without modifying files"

# --- 6b: apply mode actually mutates ---
upgrade_envfile_merge "${T6_REPO}/.env.example" "${T6_INST}/.env" 0 >/dev/null
upgrade_linefile_merge "${T6_REPO}/.harness-allowlist.example" "${T6_INST}/.harness-allowlist" 0 >/dev/null
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

# === Test 8: cmd_upgrade y/n confirmation helper =========================
#
# Issue #18: on Windows Git Bash, the upgrade prompt would hang on "n"
# because the harness_jq Docker fallback (docker.exe -i) leaves the
# parent shell's stdin in a broken state. The fix routes the prompt
# through /dev/tty and strips a trailing \r. This test exercises the
# helper's input-classification logic via the HARNESS_CONFIRM_FROM_STDIN
# hook — /dev/tty isn't reliably available in CI, so we use the stdin
# path here. The hook itself is the only production-visible difference
# between the two paths; the answer-classification + \r-strip logic is
# shared.

echo
echo "--- T8: _upgrade_confirm input classification ---"

# Source harness with HARNESS_SOURCE_ONLY=1 to access the helper without
# invoking main. Point HARNESS_INSTALL_ROOT at a fresh tmpdir so the script's
# top-level .env / state-dir lookups see an empty install (no side effects).
mkdir -p "${WORK}/t8-install"
# shellcheck disable=SC1091
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="${WORK}/t8-install" source "${REPO_ROOT}/harness"
export HARNESS_CONFIRM_FROM_STDIN=1

# Each case: (input, expected_rc, label)
# rc=0 → proceed; rc=1 → abort.
confirm_case() {
    local input="$1" expected="$2" label="$3"
    local rc=0
    _upgrade_confirm "test? " <<<"$input" >/dev/null || rc=$?
    [[ "$rc" == "$expected" ]] \
        || fail "T8 [$label]: input=$(printf '%q' "$input") expected rc=$expected, got rc=$rc"
}

# Proceed cases.
confirm_case ""    0 "empty (default Y)"
confirm_case "y"   0 "y"
confirm_case "Y"   0 "Y"
confirm_case "yes" 0 "yes"
confirm_case "YES" 0 "YES"

# Abort cases.
confirm_case "n"   1 "n"
confirm_case "N"   1 "N"
confirm_case "no"  1 "no"
confirm_case "NO"  1 "NO"
confirm_case "x"   1 "stray keystroke"

# CR-stripping: 'n\r' (Git Bash CRLF input) must abort, not proceed.
confirm_case $'n\r'   1 "n with trailing CR"
confirm_case $'no\r'  1 "no with trailing CR"
# 'y\r' must still proceed.
confirm_case $'y\r'   0 "y with trailing CR"
confirm_case $'yes\r' 0 "yes with trailing CR"
# Bare CR (empty after strip) defaults to proceed.
confirm_case $'\r'    0 "bare CR (empty after strip)"

unset HARNESS_CONFIRM_FROM_STDIN
ok "T8: _upgrade_confirm correctly classifies y/n inputs and strips CR"

# === Test 9: harness_jq fallback wired inside upgrade_actions.sh =========
#
# U025 in the inventory: `harness_jq` is defined as a standalone fallback
# inside `upgrade_actions.sh` (gated by `declare -F harness_jq` so it
# yields to the real harness-script definition when sourced from the CLI).
# upgrade_actions.sh's own helpers (`_upg_json_array`, `_upg_json_str`)
# pipe through `harness_jq`, so if the standalone fallback were missing
# or broken, every JSON-emitting action would silently produce malformed
# output even though `jq` exists on the host.
#
# Earlier tests (T1, T2, T4...) DO source upgrade_actions.sh standalone
# (line 23), but they only assert that the eventual JSON is parseable by
# `jq` — they don't isolate the fallback as a named entry point. This
# block exercises `harness_jq` and the helpers that depend on it directly
# so a regression like "harness_jq deleted from upgrade_actions.sh" is
# caught by the test name, not by an unrelated failure deep in T6.
#
# Note: this asserts the host-jq path of the fallback (jq present →
# delegate to jq). The docker-shim path of harness's full `harness_jq`
# definition is tested elsewhere (F016 territory) — this test only owns
# what upgrade_actions.sh ships.

echo
echo "--- T9: harness_jq fallback inside upgrade_actions.sh ---"

# Inventory U025: the standalone fallback `harness_jq` must be defined
# as a shell function after sourcing upgrade_actions.sh by itself (no
# outer harness script in play).
declare -F harness_jq >/dev/null 2>&1 \
    || fail "U025: harness_jq is not defined after sourcing upgrade_actions.sh standalone"

# Inventory U025: the fallback must behave as a jq wrapper for a basic
# expression — proves it's actually wired up, not just declared.
T9_JQ_OUT=$(printf '{"k":1}' | harness_jq -r '.k')
[[ "${T9_JQ_OUT}" == "1" ]] \
    || fail "U025: harness_jq fallback did not delegate to jq correctly (got '${T9_JQ_OUT}', want '1')"

# Inventory U025: the fallback must handle the -Rs and -R/.|@. patterns
# used by _upg_json_str / _upg_json_array, since those helpers route
# through harness_jq.
T9_STR_OUT=$(printf 'hello world' | harness_jq -Rs .)
[[ "${T9_STR_OUT}" == '"hello world"' ]] \
    || fail "U025: harness_jq -Rs did not produce expected JSON string (got '${T9_STR_OUT}')"

# Inventory U025: `_upg_json_array` is the direct consumer of harness_jq
# inside upgrade_actions.sh; verify it produces a valid JSON array. The
# helper pipes through `harness_jq -R . | harness_jq -s .`, which by
# default emits pretty-printed (multi-line) JSON, so we canonicalize via
# jq -c before comparing.
T9_ARR_OUT=$(_upg_json_array foo bar | jq -c .)
[[ "${T9_ARR_OUT}" == '["foo","bar"]' ]] \
    || fail "U025: _upg_json_array via harness_jq produced wrong output (got '${T9_ARR_OUT}')"

# Inventory U025: `_upg_json_array` builds the array in a single
# `harness_jq -Rn '[inputs]'` call; verify it still JSON-escapes values
# with embedded quotes and backslashes exactly as the old two-call
# `-R . | -s .` pipeline did — a botched collapse would corrupt env keys
# / allowlist entries that contain such characters.
T9_ESC_OUT=$(_upg_json_array 'a"b' 'c\d' | jq -c .)
[[ "${T9_ESC_OUT}" == '["a\"b","c\\d"]' ]] \
    || fail "U025: _upg_json_array did not JSON-escape special chars (got '${T9_ESC_OUT}')"

# Inventory U025: empty-input edge case for _upg_json_array — must yield
# '[]' without calling harness_jq (the fast path), proving the helper
# stays consistent regardless of which path harness_jq takes.
T9_EMPTY_OUT=$(_upg_json_array)
[[ "${T9_EMPTY_OUT}" == '[]' ]] \
    || fail "U025: _upg_json_array empty case produced '${T9_EMPTY_OUT}', want '[]'"

# Inventory U025: `_upg_json_str` is the other direct consumer; verify it
# routes through harness_jq and produces a valid JSON string literal.
# Canonicalize via jq -c (the helper's `harness_jq -Rs .` output is a
# single-line JSON string already, but we canonicalize for safety).
T9_JSTR_OUT=$(_upg_json_str 'a/b "c"' | jq -c .)
# After canonicalization expect a JSON string with backslash-escaped inner quotes.
[[ "${T9_JSTR_OUT}" == '"a/b \"c\""' ]] \
    || fail "U025: _upg_json_str via harness_jq produced wrong output (got '${T9_JSTR_OUT}')"

ok "T9: harness_jq standalone fallback works and is consumed by upgrade_actions.sh helpers"

echo
echo "============================================================"
echo " UPGRADE TEST PASSED"
echo "============================================================"
