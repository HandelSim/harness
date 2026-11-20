#!/usr/bin/env bash
#
# tests/unit_lite_test.sh — lite/harness-lite.ps1 is a BUILT, committed file:
# lite/build.py assembles it from lite/src.ps1, lite/ui.html and lite/texts.ps1,
# and texts.ps1 is rendered from the real proxy (proxy.py, reminder.md,
# tool-guidance.json). This test rebuilds both into a temp dir and compares
# bytes, so a proxy prompt change (or a src/ui edit) without a rebuild is a red
# CI rather than a harness-lite that silently drifts from `harness host`.
#
# Docker-free, no pip installs (gen_texts.py stubs flask/requests when absent).
# With `pwsh` on PATH it also parse-checks the built file.
#
# Run:  bash tests/unit_lite_test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LITE="${REPO_ROOT}/lite"

pass=0
fail() { echo "[lite] FAIL: $*" >&2; exit 1; }
ok()   { echo "[lite] OK: $*"; pass=$((pass + 1)); }

echo "============================================================"
echo " harness-lite build is current (docker-free)"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[lite] SKIP: python3 not found"
    exit 0
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

PYTHONDONTWRITEBYTECODE=1 python3 "$LITE/build.py" --out "$TMP_ROOT" >"$TMP_ROOT/build.log" 2>&1 \
    || { cat "$TMP_ROOT/build.log" >&2; fail "lite/build.py failed"; }
ok "lite/build.py ran"

for f in texts.ps1 harness-lite.ps1; do
    cmp -s "$TMP_ROOT/$f" "$LITE/$f" \
        || fail "lite/$f is stale (its inputs changed: proxy/ prompts, lite/src.ps1, lite/ui.html or lite/opencode-texts.json): run python3 lite/build.py and commit"
    ok "lite/$f matches a fresh build"
done

if LC_ALL=C grep -q '[^[:print:][:space:]]' "$LITE/harness-lite.ps1"; then
    fail "lite/harness-lite.ps1 has non-ASCII bytes"
fi
if LC_ALL=C grep -qv $'\r$' "$LITE/harness-lite.ps1"; then
    fail "lite/harness-lite.ps1 has a line without CRLF"
fi
ok "harness-lite.ps1 is ASCII with CRLF line ends"

if command -v pwsh >/dev/null 2>&1; then
    # shellcheck disable=SC2016  # PowerShell, not bash, expands these
    out="$(LITE_FILE="$LITE/harness-lite.ps1" pwsh -NoProfile -NonInteractive -Command '
        $t = $null; $e = $null
        [System.Management.Automation.Language.Parser]::ParseFile($env:LITE_FILE, [ref]$t, [ref]$e) | Out-Null
        if ($e) { $e | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" }; exit 1 }' 2>&1)" \
        || fail "harness-lite.ps1 does not parse: ${out}"
    ok "harness-lite.ps1 parses (pwsh)"
else
    echo "[lite] SKIP: pwsh not on PATH, parse check not run"
fi

echo
echo "[lite] all ${pass} checks passed"
