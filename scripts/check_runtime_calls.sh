#!/usr/bin/env bash
#
# scripts/check_runtime_calls.sh — static guard for raw `docker` call sites.
#
# harness routes every container-runtime invocation through wrapper functions
# in scripts/lib/platform.sh (harness_docker, harness_docker_exec, etc.) so
# the codebase honors HARNESS_CONTAINER_RUNTIME (docker/podman) and rootless
# podman support. A regression where a contributor adds a raw `docker ...`
# call would silently break podman support — they pass review because they
# work locally where docker is installed.
#
# This script greps the codebase and fails if it finds any literal `docker `
# token outside the small set of allowed sites:
#   - the wrapper definitions themselves (scripts/lib/platform.sh)
#   - inline Bourne-shell fallbacks in harness-install.sh BEFORE the clone
#     happens (the platform library isn't on disk yet at that point)
#   - in-message strings (echo, printf) where literal "docker" is shown to
#     users as part of error or hint text
#   - comment lines (`#`)
#   - the harness `require_docker` function's defensive fallback that
#     directly tries docker/podman when platform.sh wasn't loaded
#
# Usage: bash scripts/check_runtime_calls.sh
# Returns 0 if clean, 1 if violations found (with a list).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Files to scan. We deliberately scan tests too — if a test reaches around
# the wrapper, podman users still need to be able to run it.
files=(
    harness
    harness-install.sh
    scripts/lib/test_helpers.sh
    scripts/lib/upgrade_actions.sh
    scripts/lib/net_helpers.sh
    scripts/firewall_test.sh
    scripts/full_pipeline_test.sh
    scripts/harness_test.sh
    scripts/integration_test.sh
    scripts/mcp_test.sh
    scripts/persistence_test.sh
    scripts/proxy_test.sh
    scripts/upgrade_test.sh
)

# Lines that legitimately contain `docker` as a literal:
#   1. Comments and shebangs: `^\s*#`
#   2. Wrapper definitions in platform.sh (we don't list it above anyway).
#   3. Inline echoes/printfs that show `docker` to the user as a hint.
#      These match the leading `echo `, `printf `, `cat <<`, or `>&2`
#      patterns — text content, not invocations.
#   4. The `harness_docker` function family — already wrapped.
#   5. `harness-install.sh` runs before the clone, so its `_inline_*` helpers
#      reference `docker` by literal name; those are the only allowed
#      pre-clone exception.
#
# Everything else with a literal `docker ` (followed by a subcommand) is a
# violation.

# Pattern: a line starts with optional whitespace, then a literal `docker`
# token, then a subcommand we recognize. We grep for these and then filter
# out the allowed-sites list.
SUBCMDS='info|ps|inspect|rm|stop|run|compose|image|network|tag|rmi|logs|exec|build|kill'

violations=0
for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    # Find lines that have a `docker <subcmd>` token anywhere AFTER stripping
    # comments, then exclude the allowed cases via a positive prefix list:
    #   - lines that already use harness_docker (the prefix appears as
    #     `harness_docker` so an `[^_]docker` boundary check rejects them)
    #   - lines that are clearly hint text (echo/printf/cat/EOF body),
    #     identified by being inside a heredoc context — too complex for
    #     grep; we instead allow any line that contains `'echo` or `'printf`
    #     or starts with `echo ` / `printf ` / `cat ` followed by a quoted
    #     literal, OR that is part of an `<<EOF` / `<<-EOF` heredoc body.
    # The simplification we use: a line is a violation iff
    #   - it has `docker <subcmd>` not preceded by an underscore or letter
    #     (so it's a fresh token, not `harness_docker` or `_inline_docker`)
    #   - AND it is not clearly text (echo/printf/" docker ").
    #
    # In practice this regex catches all live invocations and skips the
    # remaining text-only mentions.
    while IFS= read -r line; do
        # `line` here is grep's `<lineno>:<content>` shape. Strip the lineno
        # prefix to get just the source content for filtering.
        lineno="${line%%:*}"
        content="${line#*:}"
        # Skip pure comment lines.
        [[ "$content" =~ ^[[:space:]]*# ]] && continue
        # Skip echoed/printed strings (docker shown as text only).
        [[ "$content" =~ (echo|printf)[[:space:]] ]] && continue
        # Skip the explicit docker-OR-podman fallback probe in require_docker.
        [[ "$content" =~ podman[[:space:]]+info ]] && continue
        violations=$((violations + 1))
        printf '%s:%s: %s\n' "$f" "$lineno" "$content" >&2
    done < <(grep -nE "(^|[^a-zA-Z0-9_])docker[[:space:]]+($SUBCMDS)\\b" "$f" 2>/dev/null \
        | grep -vE 'harness_docker|_inline_docker' || true)
done

if (( violations > 0 )); then
    echo >&2
    echo "[check] $violations raw 'docker <subcmd>' invocation(s) found." >&2
    echo "[check] Route these through harness_docker (or harness_docker_exec)" >&2
    echo "[check] from scripts/lib/platform.sh so podman runtime is honored." >&2
    exit 1
fi

echo "[check] OK: no raw docker invocations outside the wrapper layer."
