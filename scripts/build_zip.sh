#!/usr/bin/env bash
#
# build_zip.sh — produce dist/harness-distribution.zip from the current
# repo state. The zip ships three files: install.sh, .env (a copy of
# .env.example), and README.md (the zip-specific readme).

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out_dir="$repo_root/dist"
out_zip="$out_dir/harness-distribution.zip"

mkdir -p "$out_dir"
rm -f "$out_zip"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

cp "$repo_root/install.sh"     "$staging/install.sh"
cp "$repo_root/.env.example"   "$staging/.env"
cp "$repo_root/zip-readme.md"  "$staging/README.md"

(cd "$staging" && zip -q "$out_zip" install.sh .env README.md)

echo "built: $out_zip"
echo "contents:"
unzip -l "$out_zip"
