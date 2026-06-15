#!/usr/bin/env bash
#
# harness-bootstrap.sh - thin, version-stable entrypoint for installing harness.
#
# Ship this ONE file alongside your pre-edited .env and .harness-allowlist.
# It fetches the CURRENT harness-install.sh from the repo and hands off to it,
# so the install LOGIC is always up to date even though your bundle never
# changes. You maintain three small files; the install procedure lives upstream
# and is whatever is on the repo at install time.
#
# Why this exists: bundling a pinned harness-install.sh goes stale as the repo
# evolves (new prompts, new state dirs, new seeding logic). This bootstrap keeps
# only the tiny pre-clone step (resolve proxy, fetch the installer) and delegates
# everything else - the clone, .env/.harness-allowlist seeding, PATH wrapper - to
# the freshly fetched installer.
#
# Run it from the directory where you want ./harness/ to be created:
#   source ./harness-bootstrap.sh      # sourced: a PATH update reaches your shell
#   bash   ./harness-bootstrap.sh      # executed: PATH update takes effect next shell
#
# What it does, and nothing more:
#   1. find its own directory (where your .env + .harness-allowlist live)
#   2. read HTTP_PROXY/HTTPS_PROXY from that .env and export them for the fetch
#   3. download the current harness-install.sh next to your .env
#   4. hand control to it (it clones the repo, seeds config, sets up PATH)
#
# Pin a specific release for reproducibility:
#   HARNESS_INSTALL_REF=v1.0 source ./harness-bootstrap.sh
# Point at a fork/mirror (GitHub-style remote):
#   HARNESS_REPO_URL=https://github.com/you/harness source ./harness-bootstrap.sh

# Detect sourced vs executed (same discipline as harness-install.sh): when
# sourced we must NOT enable `set -e`/`set -u`, because those options would
# leak into and govern the user's interactive shell. Only the executed path
# turns on strict mode. The rest of the script is written to be correct without
# relying on set -e/-u (explicit checks, ${VAR:-} defaults).
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    HARNESS_BOOTSTRAP_SOURCED=1
else
    HARNESS_BOOTSTRAP_SOURCED=0
    set -euo pipefail
fi

REPO_URL="${HARNESS_REPO_URL:-https://github.com/HandelSim/harness}"
REF="${HARNESS_INSTALL_REF:-main}"

# Resolve our own directory: the bundle dir holding .env + .harness-allowlist.
# Falls back to $PWD when BASH_SOURCE can't be resolved.
bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || bundle_dir="$(pwd)"
[[ -n "${bundle_dir:-}" ]] || bundle_dir="$(pwd)"

# Read a proxy from the bundled .env and export it for the fetch below. The
# installer re-reads the same file for its own clone and persists it into the
# install root, so .env stays the single source of truth for the proxy. Both
# cases are exported (upper- and lower-case): libcurl gives the lower-case name
# precedence, so exporting only the upper form would lose to a host-set lower
# one. Wrapped in a function with an explicit `return 0` so the loop's last
# status can't trip the caller's set -e (mirrors apply_preclone_proxy).
apply_env_proxy() {
    local env_file="$bundle_dir/.env"
    [[ -f "$env_file" ]] || return 0
    local pk val lk line
    for pk in HTTP_PROXY HTTPS_PROXY; do
        val=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[[:space:]]*${pk}=(.*)$ ]] && val="${BASH_REMATCH[1]}"
        done <"$env_file"
        [[ -z "$val" ]] && continue            # blank/absent: keep host env
        lk=$(printf '%s' "$pk" | tr '[:upper:]' '[:lower:]')
        export "$pk"="$val" "$lk"="$val"
        echo "bootstrap: using $pk from $env_file for the fetch"
    done
    return 0
}
apply_env_proxy

installer="$bundle_dir/.harness-install.fetched.sh"

fetch_installer() {
    # Local-path REPO_URL (used by the test suite and local installs): copy the
    # installer straight out of the tree, no network.
    if [[ -d "$REPO_URL" && -f "$REPO_URL/harness-install.sh" ]]; then
        cp "$REPO_URL/harness-install.sh" "$installer.tmp"
        return $?
    fi
    # Remote: fetch the raw script for the requested ref. Assumes a GitHub-style
    # remote (the default and the fork override both are); a non-GitHub remote
    # makes the raw URL wrong, curl -f then fails, and we fall back to a bundled
    # installer below.
    local slug raw
    slug="${REPO_URL%/}"; slug="${slug%.git}"; slug="${slug#https://github.com/}"
    raw="https://raw.githubusercontent.com/${slug}/${REF}/harness-install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$raw" -o "$installer.tmp"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$installer.tmp" "$raw"
    else
        echo "bootstrap: need curl or wget to fetch the installer" >&2
        return 1
    fi
}

# Fetch, then sanity-check it is actually a script (a captive-portal HTML page
# that returns 200 would fail the shebang check), then atomically swap it in.
if fetch_installer && head -1 "$installer.tmp" 2>/dev/null | grep -q '^#!'; then
    mv -f "$installer.tmp" "$installer"
    echo "bootstrap: fetched current harness-install.sh (ref: $REF)"
else
    rm -f "$installer.tmp"
    if [[ -f "$bundle_dir/harness-install.sh" ]]; then
        echo "bootstrap: fetch failed; falling back to the bundled harness-install.sh" >&2
        installer="$bundle_dir/harness-install.sh"
    else
        echo "bootstrap: could not fetch harness-install.sh and no bundled copy to fall back to" >&2
        echo "bootstrap: check network/proxy, or set HARNESS_INSTALL_REF / HARNESS_REPO_URL" >&2
        # return ends a sourced run; exit ends an executed one (return fails at
        # top level of an executed script, so the exit fires).
        # shellcheck disable=SC2317
        { return 1 2>/dev/null || exit 1; }
    fi
fi

# Remove the fetched installer when we're done with it (never the user's own
# bundled copy, whose name does not match). Returns 0 so it can't trip set -e
# between here and the final return/exit.
cleanup() {
    [[ "$installer" == *.harness-install.fetched.sh ]] && rm -f "$installer"
    return 0
}

# Hand off. Source it if we were sourced (so the installer's PATH export reaches
# your shell), else execute it as a child. Either way the installer's
# $script_dir resolves to bundle_dir, so it finds your .env and
# .harness-allowlist sitting beside it, exactly as if you had run it directly.
if (( HARNESS_BOOTSTRAP_SOURCED )); then
    # shellcheck disable=SC1090
    source "$installer"; rc=$?
    cleanup
    return "$rc"
else
    bash "$installer"; rc=$?
    cleanup
    exit "$rc"
fi
