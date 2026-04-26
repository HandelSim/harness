#!/usr/bin/env bash
#
# build_zip.sh — produce dist/harness-distribution.zip from the current
# repo state. The zip ships three files: harness-install.sh, .env (a copy
# of .env.example), and README.md (the zip-specific readme).

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out_dir="$repo_root/dist"
out_zip="$out_dir/harness-distribution.zip"

mkdir -p "$out_dir"
rm -f "$out_zip"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

cp "$repo_root/harness-install.sh" "$staging/harness-install.sh"
cp "$repo_root/.env.example"       "$staging/.env"
cp "$repo_root/zip-readme.md"      "$staging/README.md"

# Prefer system `zip` (Linux + macOS); fall back to python's zipfile so this
# script also works on Git Bash for Windows where `zip` isn't bundled.
if command -v zip >/dev/null 2>&1; then
    (cd "$staging" && zip -q "$out_zip" harness-install.sh .env README.md)
elif command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    py=python3
    command -v python3 >/dev/null 2>&1 || py=python
    (cd "$staging" && "$py" -c "
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED) as z:
    for n in sys.argv[2:]:
        z.write(n)
" "$out_zip" harness-install.sh .env README.md)
else
    echo "[build_zip] ERROR: neither 'zip' nor 'python' is available" >&2
    exit 1
fi

echo "built: $out_zip"
echo "contents:"
if command -v unzip >/dev/null 2>&1; then
    unzip -l "$out_zip"
elif command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    py=python3
    command -v python3 >/dev/null 2>&1 || py=python
    "$py" -c "
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        print(f'{info.file_size:>10}  {info.date_time}  {info.filename}')
" "$out_zip"
fi
