#!/usr/bin/env bash
#
# tests/unit_model_print_test.sh — exercise _print_upstream_models' formatting
# without a network or a real upstream.
#
# Sources `harness` with HARNESS_SOURCE_ONLY=1 so main() never runs, then stubs
# `curl` to emit a canned body plus the `__HTTP_STATUS__<code>` marker the real
# `-w` format appends. Asserts the startup model line is a clean, counted,
# one-id-per-line list (the fix for the malformed single space-joined dump):
#   - well-formed catalog        -> "(N):" header + one indented id per line, in order
#   - null-id / whitespace entry  -> dropped / trimmed, never a bogus "null" name
#   - non-2xx status              -> nothing printed to stdout
#   - empty / invalid body        -> nothing printed, no error
#   - HARNESS_SKIP_AUTH_PROBE=1   -> short-circuits, prints nothing
#
# Prints "MODEL PRINT TEST PASSED" on success.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS="${REPO_ROOT}/harness"

echo "============================================================"
echo " model-print (startup catalog) unit test"
echo "============================================================"

fail() { echo "[model-print-test] FAIL: $*" >&2; exit 1; }
ok()   { echo "[model-print-test] OK: $*"; }

[[ -f "$HARNESS" ]] || fail "harness script not found at $HARNESS"
command -v jq >/dev/null 2>&1 || fail "this test needs host jq"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# shellcheck disable=SC1090
HARNESS_SOURCE_ONLY=1 HARNESS_INSTALL_ROOT="$TMP_ROOT" source "$HARNESS" 2>/dev/null

[[ "$(type -t _print_upstream_models)" == "function" ]] || fail "_print_upstream_models not sourced"

# Stub curl to reproduce `curl -w $'\n__HTTP_STATUS__%{http_code}'`: the canned
# body, then a newline, then the status marker (no trailing newline). command -v
# finds the function so the real binary is never called.
FAKE_BODY=""
FAKE_STATUS="200"
curl() { printf '%s\n__HTTP_STATUS__%s' "$FAKE_BODY" "$FAKE_STATUS"; }

# The probe only runs with both vars set and the skip flag off.
export PROXY_API_URL="https://upstream.example"
export PROXY_API_KEY="eyJ-test-key"
unset HARNESS_SKIP_AUTH_PROBE 2>/dev/null || true

# --- T1: well-formed catalog -> counted, ordered, one-per-line --------------
FAKE_STATUS="200"
FAKE_BODY='{"object":"list","data":[{"id":"gemini-3.1-pro","object":"model"},{"id":"gemini-2.5-flash"},{"id":"gpt-4"}]}'
out="$(_print_upstream_models)" || fail "T1: _print_upstream_models returned non-zero on a 200"
grep -qx '\[harness\] upstream models available (3):' <<<"$out" \
    || fail "T1: missing counted header (3) — got: $out"
grep -qx '\[harness\]   gemini-3.1-pro'   <<<"$out" || fail "T1: gemini-3.1-pro not on its own line — $out"
grep -qx '\[harness\]   gemini-2.5-flash' <<<"$out" || fail "T1: gemini-2.5-flash not on its own line — $out"
grep -qx '\[harness\]   gpt-4'            <<<"$out" || fail "T1: gpt-4 not on its own line — $out"
# Exactly one id per line: 1 header + 3 ids = 4 lines, no space-joined dump.
[[ "$(grep -c . <<<"$out")" == "4" ]] || fail "T1: expected 4 lines (header + 3 ids), got: $out"
grep -q 'gemini-3.1-pro gemini-2.5-flash' <<<"$out" && fail "T1: ids are still space-joined on one line — $out"
ok "T1: well-formed catalog prints a counted, one-id-per-line list"

# --- T2: null-id entry dropped, whitespace trimmed --------------------------
FAKE_BODY='{"object":"list","data":[{"id":"gemini-3.1-pro"},{"object":"model"},{"id":"  claude-opus-4-8  "}]}'
out="$(_print_upstream_models)" || fail "T2: returned non-zero"
grep -qx '\[harness\] upstream models available (2):' <<<"$out" \
    || fail "T2: null-id entry not dropped from count — $out"
grep -qx '\[harness\]   claude-opus-4-8' <<<"$out" || fail "T2: id not whitespace-trimmed — $out"
grep -q 'null' <<<"$out" && fail "T2: a bogus 'null' model leaked into the list — $out"
ok "T2: null-id entry dropped and surrounding whitespace trimmed"

# --- T3: non-2xx status prints nothing to stdout ----------------------------
FAKE_STATUS="500"
FAKE_BODY='{"error":"upstream boom"}'
out="$(_print_upstream_models 2>/dev/null)" || true
[[ -z "$out" ]] || fail "T3: a 500 should print no model line to stdout — got: $out"
ok "T3: a non-2xx upstream status prints no model line"

# --- T4: 200 with empty / invalid body prints nothing, no error -------------
FAKE_STATUS="200"
for b in '' 'not json at all' '{"object":"list","data":[]}'; do
    FAKE_BODY="$b"
    out="$(_print_upstream_models)" || fail "T4: returned non-zero on body [$b]"
    [[ -z "$out" ]] || fail "T4: body [$b] should print nothing — got: $out"
done
ok "T4: empty / invalid / no-models body prints nothing and does not error"

# --- T5: HARNESS_SKIP_AUTH_PROBE=1 short-circuits ---------------------------
FAKE_BODY='{"object":"list","data":[{"id":"gpt-4"}]}'
out="$(HARNESS_SKIP_AUTH_PROBE=1 _print_upstream_models)" || fail "T5: returned non-zero"
[[ -z "$out" ]] || fail "T5: skip flag should print nothing — got: $out"
ok "T5: HARNESS_SKIP_AUTH_PROBE=1 short-circuits the catalog print"

echo
echo "MODEL PRINT TEST PASSED"
