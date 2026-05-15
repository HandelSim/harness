#!/bin/bash
# Oracle solution: produces the expected output unconditionally.
# Used by Harbor's `oracle` agent for sanity-checking the task definition.
set -euo pipefail
mkdir -p /app
printf 'hello-harness' > /app/hello.txt
