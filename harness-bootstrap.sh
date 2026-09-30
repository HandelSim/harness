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
# only the tiny pre-clone step (pick the branch, resolve proxy, fetch the
# installer) and delegates everything else - the clone, .env/.harness-allowlist
# seeding, PATH wrapper - to the freshly fetched installer.
#
# Run it from the directory where you want ./harness/ to be created:
#   source ./harness-bootstrap.sh      # sourced: a PATH update reaches your shell
#   bash   ./harness-bootstrap.sh      # executed: PATH update takes effect next shell
#
# What it does, and nothing more:
#   1. find its own directory (where your .env + .harness-allowlist live)
#   2. ask which branch to install, main or dev (or take -b main|dev)
#   3. read HTTP_PROXY/HTTPS_PROXY from that .env and export them for the fetch
#   4. download that branch's harness-install.sh next to your .env
#   5. hand control to it with --branch <that branch> plus any flags you passed
#      (it clones the repo on that branch, seeds config, sets up PATH)
#
# The branch picks BOTH the installer that runs and the branch the clone starts
# on, so the install logic always matches the code it installs. It is not saved
# anywhere: 'harness update'/'upgrade' simply follow the branch the clone is on.
#
# Non-interactive:  bash ./harness-bootstrap.sh -b dev
# Point at a fork/mirror (GitHub-style remote):
#   HARNESS_REPO_URL=https://github.com/you/harness source ./harness-bootstrap.sh
#
# Use bash: the installer is bash-only, so from zsh (the macOS default shell)
# run `bash ./harness-bootstrap.sh` rather than sourcing it.

# Detect sourced vs executed (same discipline as harness-install.sh): when
# sourced we must NOT enable `set -e`/`set -u`, because those options would
# leak into and govern the user's interactive shell. Only the executed path
# turns on strict mode. The rest of the script is written to be correct without
# relying on set -e/-u (explicit checks, ${VAR:-} defaults).
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    _hb_sourced=1
else
    _hb_sourced=0
    set -euo pipefail
fi

_hb_repo_url="${HARNESS_REPO_URL:-https://github.com/HandelSim/harness}"

# Resolve our own directory: the bundle dir holding .env + .harness-allowlist.
# Falls back to $PWD when BASH_SOURCE can't be resolved.
_hb_bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || _hb_bundle_dir="$(pwd)"
[[ -n "${_hb_bundle_dir:-}" ]] || _hb_bundle_dir="$(pwd)"

# Every name this script defines starts with _hb_ and is unset before it hands
# back to a sourcing shell, so nothing (a `cleanup` function, say) is left
# behind to shadow the user's own.
_hb_unset_all() {
    unset -f _hb_apply_env_proxy _hb_fetch_installer _hb_cleanup _hb_unset_all
    unset _hb_sourced _hb_repo_url _hb_bundle_dir _hb_ref _hb_args _hb_i \
          _hb_ans _hb_installer _hb_fetched_msg
}

# The flags to forward to the installer, and the branch to install. A -b/--branch
# the user passed is honored as-is (and forwarded); otherwise ask on a terminal,
# and default to main without one. Written without ${arr[@]} on an empty array
# and without ((i++)), both of which trip `set -eu` on bash 3.2 (macOS).
_hb_args=("$@")
_hb_ref=""
_hb_i=0
while (( _hb_i < ${#_hb_args[@]} )); do
    case "${_hb_args[_hb_i]}" in
        -b|--branch) _hb_ref="${_hb_args[_hb_i + 1]:-}"; break ;;
    esac
    _hb_i=$((_hb_i + 1))
done
if [[ -z "$_hb_ref" ]]; then
    _hb_ref="main"
    if [[ -t 0 ]]; then
        echo "Which branch should this install track?"
        echo "  1) main  - stable releases (recommended)"
        echo "  2) dev   - latest changes, less battle-tested"
        _hb_ans=""
        read -rp "select [1-2, Enter for main]: " _hb_ans || _hb_ans=""
        case "${_hb_ans:-}" in
            2|dev) _hb_ref="dev" ;;
            ""|1|main) ;;
            *) echo "  unrecognized choice '${_hb_ans}'; using main" ;;
        esac
    fi
    _hb_args+=(--branch "$_hb_ref")
fi
case "$_hb_ref" in
    main|dev) ;;
    *)
        echo "bootstrap: -b must be 'main' or 'dev' (got: ${_hb_ref:-<empty>})" >&2
        _hb_unset_all
        # return ends a sourced run; exit ends an executed one (return fails at
        # top level of an executed script, so the exit fires).
        # shellcheck disable=SC2317
        { return 1 2>/dev/null || exit 1; }
        ;;
esac

# Read a proxy from the bundled .env and export it for the fetch below. The
# installer re-reads the same file for its own clone and persists it into the
# install root, so .env stays the single source of truth for the proxy. Both
# cases are exported (upper- and lower-case): libcurl gives the lower-case name
# precedence, so exporting only the upper form would lose to a host-set lower
# one. A CR (a .env saved on Windows) and one pair of surrounding quotes are
# stripped, as compose does, so neither ends up inside the proxy URL. Wrapped in
# a function with an explicit `return 0` so the loop's last status can't trip
# the caller's set -e (mirrors apply_preclone_proxy).
_hb_apply_env_proxy() {
    local env_file="$_hb_bundle_dir/.env"
    [[ -f "$env_file" ]] || return 0
    local pk val lk line
    for pk in HTTP_PROXY HTTPS_PROXY; do
        val=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[[:space:]]*${pk}=(.*)$ ]] && val="${BASH_REMATCH[1]}"
        done <"$env_file"
        val="${val%$'\r'}"
        if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then
            val="${val:1:${#val}-2}"
        fi
        [[ -z "$val" ]] && continue            # blank/absent: keep host env
        lk=$(printf '%s' "$pk" | tr '[:upper:]' '[:lower:]')
        export "$pk"="$val" "$lk"="$val"
        echo "bootstrap: using $pk from $env_file for the fetch"
    done
    return 0
}
_hb_apply_env_proxy

_hb_installer="$_hb_bundle_dir/.harness-install.fetched.sh"
_hb_fetched_msg=""

_hb_fetch_installer() {
    # Local-path repo URL (used by the test suite and local installs): copy the
    # installer straight out of the tree, no network.
    if [[ -d "$_hb_repo_url" && -f "$_hb_repo_url/harness-install.sh" ]]; then
        cp "$_hb_repo_url/harness-install.sh" "$_hb_installer.tmp" || return 1
        _hb_fetched_msg="copied harness-install.sh from $_hb_repo_url"
        return 0
    fi
    # Remote: fetch the raw script for the chosen branch. Assumes a GitHub-style
    # remote (the default and the fork override both are); a non-GitHub remote
    # makes the raw URL wrong, curl -f then fails, and we fall back to a bundled
    # installer below.
    local slug raw
    slug="${_hb_repo_url%/}"; slug="${slug%.git}"; slug="${slug#https://github.com/}"
    raw="https://raw.githubusercontent.com/${slug}/${_hb_ref}/harness-install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$raw" -o "$_hb_installer.tmp" || return 1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$_hb_installer.tmp" "$raw" || return 1
    else
        echo "bootstrap: need curl or wget to fetch the installer" >&2
        return 1
    fi
    _hb_fetched_msg="fetched harness-install.sh (branch: $_hb_ref)"
}

# The fetched installer is written beside your .env so its $script_dir finds
# the bundle. A read-only bundle dir (a mounted image, a share) can't take it:
# say that, rather than letting the failed write read as a network error.
if [[ ! -w "$_hb_bundle_dir" ]]; then
    echo "bootstrap: cannot write to $_hb_bundle_dir, where the installer is staged beside your .env" >&2
    if [[ -f "$_hb_bundle_dir/harness-install.sh" ]]; then
        echo "bootstrap: falling back to the bundled harness-install.sh" >&2
        _hb_installer="$_hb_bundle_dir/harness-install.sh"
    else
        echo "bootstrap: copy the bundle folder somewhere writable and run it from there" >&2
        _hb_unset_all
        # shellcheck disable=SC2317
        { return 1 2>/dev/null || exit 1; }
    fi
# Fetch, then sanity-check it is actually a script (a captive-portal HTML page
# that returns 200 would fail the shebang check), then atomically swap it in.
elif _hb_fetch_installer && head -1 "$_hb_installer.tmp" 2>/dev/null | grep -q '^#!'; then
    mv -f "$_hb_installer.tmp" "$_hb_installer"
    echo "bootstrap: $_hb_fetched_msg"
else
    rm -f "$_hb_installer.tmp"
    if [[ -f "$_hb_bundle_dir/harness-install.sh" ]]; then
        echo "bootstrap: fetch failed; falling back to the bundled harness-install.sh" >&2
        _hb_installer="$_hb_bundle_dir/harness-install.sh"
    else
        echo "bootstrap: could not fetch harness-install.sh and no bundled copy to fall back to" >&2
        echo "bootstrap: check network/proxy (HTTP_PROXY/HTTPS_PROXY in your .env), or set HARNESS_REPO_URL" >&2
        _hb_unset_all
        # shellcheck disable=SC2317
        { return 1 2>/dev/null || exit 1; }
    fi
fi

# Remove the fetched installer when we're done with it (never the user's own
# bundled copy, whose name does not match). Returns 0 so it can't trip set -e
# between here and the final return/exit.
_hb_cleanup() {
    [[ "$_hb_installer" == *.harness-install.fetched.sh ]] && rm -f "$_hb_installer"
    return 0
}

# Hand off, forwarding the flags (always including --branch). Source it if we
# were sourced (so the installer's PATH export reaches your shell), else execute
# it as a child. Either way the installer's $script_dir resolves to the bundle
# dir, so it finds your .env and .harness-allowlist sitting beside it, exactly
# as if you had run it directly. The installer's status is captured with `||`
# so a failed install still reaches the cleanup under set -e.
if (( _hb_sourced )); then
    _hb_i=0
    # shellcheck disable=SC1090
    source "$_hb_installer" "${_hb_args[@]}" || _hb_i=$?
    _hb_cleanup
    # eval expands the status before the unset runs, so it survives the unset.
    eval "_hb_unset_all; return $_hb_i"
else
    _hb_i=0
    bash "$_hb_installer" "${_hb_args[@]}" || _hb_i=$?
    _hb_cleanup
    exit "$_hb_i"
fi
