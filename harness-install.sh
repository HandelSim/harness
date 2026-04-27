#!/usr/bin/env bash
#
# harness installer.
#
# Run from the directory in which you want the install to live. The script
# clones the harness repo as ./harness/ — and that directory IS the install
# root. Code, user config (.env, .harness-allowlist), and runtime state
# (state/) all live inside the clone; runtime state is gitignored.
#
# Layout produced:
#   <cwd>/harness/                        the install root (also the git clone)
#     .git/                                managed by 'harness update'
#     harness-install.sh, harness, docker-compose.yml, ...   (code; tracked)
#     .env                                 your config (gitignored)
#     .harness-allowlist                   egress allowlist (gitignored)
#     state/                               runtime state (gitignored)
#       output/                            proxy debug dumps
#       agent/home/                        shared agent /home/harness
#                                          (claude, opencode, shell)
#       ollama-data/                       ollama model blobs
#       mcp/<name>/                        active MCP services
#
# To uninstall later:
#   rm -rf <install-root>
#   rm ~/.local/bin/harness

# Detect whether we were sourced (so the PATH update inside this script
# takes effect in the caller's shell) vs executed as a subprocess. Behavior
# differs:
#   - sourced:  do NOT enable `set -e`; it would terminate the user's
#               interactive shell on any non-zero command in the rest of
#               the script. Use `return` to leave the script.
#   - executed: enable strict mode for installer safety; use `exit` normally.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    HARNESS_INSTALL_SOURCED=1
else
    HARNESS_INSTALL_SOURCED=0
    set -euo pipefail
fi

# Helper: exit if executed, return if sourced. Without this, `source
# harness-install.sh` (which the README recommends so PATH updates land in
# the parent shell) would kill the calling shell on the first `exit`.
exit_or_return() {
    local code="${1:-0}"
    if (( HARNESS_INSTALL_SOURCED )); then
        return "$code"
    else
        exit "$code"
    fi
}

# The default points at the public GitHub remote. scripts/full_pipeline_test.sh
# overrides this via HARNESS_REPO_URL=<local-path> so the pipeline test can
# clone the working tree under test without needing a network round-trip.
# `git clone` accepts a local directory as a URL, so any path on disk works.
REPO_URL="${HARNESS_REPO_URL:-https://github.com/HandelSim/harness}"
CLONE_DIR="harness"
PROGRAM_NAME="harness"
LOCAL_BIN="$HOME/.local/bin"

# --- ANSI colors ------------------------------------------------------------
# Disabled if stdout is not a tty.

if [[ -t 1 ]]; then
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

ok()    { printf '%s✓%s %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s!%s %s\n'  "$C_YELLOW" "$C_RESET" "$*"; }
fail()  { printf '%sx%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; exit_or_return 1; }
title() { printf '%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

cwd=$(pwd)
install_root="$cwd/$CLONE_DIR"

# --- inline platform fallbacks (pre-clone) ----------------------------------
#
# install.sh runs BEFORE the clone, so scripts/lib/platform.sh from the
# repo isn't yet available. We inline the minimum subset of helpers needed
# in the early phases (OS detection, docker check, Docker Desktop start).
# After the clone we source the full library for the rest of the script.

_inline_detect_os() {
    case "$(uname -s)" in
        Linux*) echo "linux";;
        Darwin*) echo "macos";;
        MINGW*|MSYS*|CYGWIN*) echo "windows";;
        *) echo "unknown";;
    esac
}

_inline_docker_running() { docker info >/dev/null 2>&1; }

_inline_start_docker() {
    local timeout=90
    local os
    os=$(_inline_detect_os)

    case "$os" in
        windows)
            local exe="/c/Program Files/Docker/Docker/Docker Desktop.exe"
            if [[ ! -f "$exe" ]]; then
                echo "  Docker Desktop not found at expected path: $exe" >&2
                echo "  Please start Docker Desktop manually." >&2
                return 1
            fi
            echo "  Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            "$exe" >/dev/null 2>&1 &
            ;;
        macos)
            echo "  Docker Desktop is not running. Starting it now (typically 30-60 seconds)..." >&2
            if ! open -a Docker >/dev/null 2>&1; then
                echo "  Failed to launch Docker Desktop. Please start it manually." >&2
                return 1
            fi
            ;;
        linux)
            echo "  Docker daemon not running on Linux. Start it with one of:" >&2
            echo "    sudo systemctl start docker" >&2
            echo "    sudo service docker start" >&2
            return 1
            ;;
        *)
            echo "  Unknown OS; cannot auto-start Docker. Please start it manually." >&2
            return 1
            ;;
    esac

    local elapsed=0
    while (( elapsed < timeout )); do
        if _inline_docker_running; then
            echo "  Docker is now running." >&2
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if (( elapsed % 10 == 0 )); then
            echo "    ...still waiting (${elapsed}s elapsed, ${timeout}s timeout)" >&2
        fi
    done

    echo "  Docker did not become available within ${timeout}s." >&2
    return 1
}

_inline_check_command() {
    local cmd="$1" desc="${2:-$1}"
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✓ $desc"
        return 0
    fi
    echo "  ✗ $desc — '$cmd' not found in PATH"
    return 1
}

# --- preflight --------------------------------------------------------------
#
# Validates that the host can run the installer at all. Failures are listed
# up front, before any prompting, so the user can fix them in one pass
# instead of fixing-rerun-fixing.

preflight() {
    local errors=0
    echo
    title "preflight checks"

    _inline_check_command git "git" || errors=$((errors+1))
    _inline_check_command docker "docker" || errors=$((errors+1))

    if ! docker compose version >/dev/null 2>&1; then
        echo "  ✗ docker compose v2 — 'docker compose' subcommand not available"
        echo "    (you may have docker, but need compose v2 specifically)"
        errors=$((errors+1))
    else
        echo "  ✓ docker compose v2"
    fi

    # Docker daemon (with auto-start attempt on Win/Mac)
    if _inline_docker_running; then
        echo "  ✓ docker daemon"
    else
        echo "  - docker daemon not running; attempting auto-start..."
        if _inline_start_docker; then
            echo "  ✓ docker daemon (started)"
        else
            echo "  ✗ docker daemon not running"
            errors=$((errors+1))
        fi
    fi

    # Disk space (5GB recommended for fresh install + image pulls)
    local available_mb
    available_mb=$(df -m "$cwd" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ -n "$available_mb" ]]; then
        if (( available_mb >= 5120 )); then
            echo "  ✓ disk space (${available_mb}M available, 5120M recommended)"
        else
            echo "  ⚠ disk space — only ${available_mb}M available; ollama/serena images need ~5GB total"
            # warning, not error
        fi
    fi

    # Write access to CWD
    if [[ ! -w "$cwd" ]]; then
        echo "  ✗ CWD ($cwd) is not writable"
        errors=$((errors+1))
    else
        echo "  ✓ write access to $cwd"
    fi

    # Existing harness/ in CWD
    if [[ -d "$cwd/$CLONE_DIR" ]]; then
        echo "  ✗ ./$CLONE_DIR/ already exists; remove it or run install in a different parent directory"
        errors=$((errors+1))
    fi

    if (( errors > 0 )); then
        echo
        echo "[install] $errors check(s) failed. Resolve the issues above and re-run."
        exit_or_return 1
    fi

    echo "  all checks passed"
    echo
}

# --- intent -----------------------------------------------------------------

cat <<EOF
${C_BOLD}harness installer${C_RESET}

This will install the harness runtime into a single self-contained folder:
  $install_root

That folder is both the git clone and the install root — code, user config,
and runtime state all live inside it. To uninstall later:
  rm -rf $install_root
  rm $LOCAL_BIN/$PROGRAM_NAME

Steps:
  1. Run preflight checks (git, docker, disk space, write access).
  2. Refuse if $install_root already exists.
  3. Clone $REPO_URL into $install_root
  4. Create runtime state directories under $install_root/state/
  5. Seed .env (from a pre-edited .env in $cwd if present, else from .env.example).
  6. Seed .harness-allowlist from .harness-allowlist.example (if not present).
  7. Optionally install a 'harness' wrapper into $LOCAL_BIN and update PATH.

EOF

# --- preflight (fail fast, before any prompts) ------------------------------

preflight

# --- prompts ----------------------------------------------------------------

read -rp "continue? [y/N]: " ans
case "${ans:-}" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit_or_return 0 ;;
esac

read -rp "add 'harness' to PATH (recommended)? [Y/n]: " path_ans
case "${path_ans:-}" in
    n|N|no|NO) want_path=0 ;;
    *) want_path=1 ;;
esac

# --- clone ------------------------------------------------------------------

title "cloning repo"

if [[ -e "$install_root" ]]; then
    fail "$install_root already exists; remove it or run harness-install.sh in a clean directory"
fi
git clone "$REPO_URL" "$install_root"
ok "cloned into $install_root"

# --- post-clone: source full platform.sh ------------------------------------
#
# After the clone, the full helper library is on disk. Source it so anything
# below this point can use the canonical helpers instead of the inline
# fallbacks. Failure to find it indicates a broken clone — abort.

if [[ ! -f "$install_root/scripts/lib/platform.sh" ]]; then
    fail "internal: $install_root/scripts/lib/platform.sh missing after clone"
fi
# shellcheck disable=SC1091
source "$install_root/scripts/lib/platform.sh"

# --- defense-in-depth: dos2unix on Windows ----------------------------------
#
# .gitattributes already forces LF on shell scripts, so this is belt-and-
# braces in case a user's git was configured to ignore .gitattributes or
# the clone path went through a tool that re-wrote line endings.

if [[ "$(harness_detect_os)" == "windows" ]]; then
    if command -v dos2unix >/dev/null 2>&1; then
        title "normalizing line endings on Windows"
        find "$install_root" -type f \( -name "*.sh" -o -name "harness" -o -name "harness-install.sh" \) \
            -exec dos2unix -q {} + 2>/dev/null || true
        ok "ran dos2unix on shell scripts"
    else
        warn "dos2unix not available; relying on .gitattributes"
    fi
fi

# --- runtime state dirs -----------------------------------------------------
#
# Everything under state/ is gitignored. .gitignore already excludes state/
# so these dirs never show up in `git status` inside the clone.

title "creating runtime state directories"
mkdir -p "$install_root/state/output" \
         "$install_root/state/agent/home" \
         "$install_root/state/ollama-data" \
         "$install_root/state/mcp"
ok "created state/output, state/agent/home, state/ollama-data, state/mcp"

# --- .env handling ----------------------------------------------------------
#
# Three cases, in priority order:
#   1. $install_root/.env already exists (unusual; clone shouldn't ship .env)
#      → leave it alone.
#   2. $cwd/.env exists (user pre-placed an edited .env in cwd)
#      → move it into the clone and remove the source so the layout is clean.
#   3. Neither → seed from .env.example inside the clone.
#
# B3-MANAGED: env-vars — <install-root>/.env. `harness upgrade` runs the
# `env_vars` manifest action (envfile_merge) to surface new variables added
# to .env.example without touching existing user values. See
# scripts/upgrade-manifest.json and scripts/lib/upgrade_actions.sh.

title "configuring .env"
if [[ -f "$install_root/.env" ]]; then
    ok "$install_root/.env already present; left untouched"
elif [[ -f "$cwd/.env" ]]; then
    cp "$cwd/.env" "$install_root/.env"
    ok "moved your pre-filled .env into $install_root/.env"
    rm -f "$cwd/.env"
else
    cp "$install_root/.env.example" "$install_root/.env"
    ok "seeded $install_root/.env from .env.example"
    warn "edit $install_root/.env and fill in PROXY_API_KEY (and any other blank required values)"
fi

# --- firewall allowlist -----------------------------------------------------
#
# Every harness container reads its egress allowlist from
# <install-root>/.harness-allowlist. Seed from the bundled example on a
# fresh install. Idempotent: existing user customizations are never touched.
#
# B3-MANAGED: allowlist-hosts — <install-root>/.harness-allowlist. `harness
# upgrade` runs the `allowlist_hosts` manifest action (linefile_merge) to
# append new hostnames added upstream without modifying user entries.

title "configuring firewall allowlist"
if [[ -f "$install_root/.harness-allowlist" ]]; then
    ok ".harness-allowlist already present; left untouched"
elif [[ -f "$install_root/.harness-allowlist.example" ]]; then
    cp "$install_root/.harness-allowlist.example" "$install_root/.harness-allowlist"
    ok "seeded $install_root/.harness-allowlist from .harness-allowlist.example"
    warn "edit $install_root/.harness-allowlist and add your upstream LLM API hostname (must match PROXY_API_URL)"
else
    warn "no .harness-allowlist.example bundled; create $install_root/.harness-allowlist before 'harness start'"
fi

# --- PATH setup -------------------------------------------------------------
#
# We install a wrapper script (not a symlink) at ~/.local/bin/harness. On
# Windows, creating symlinks requires Developer Mode or admin privileges;
# wrappers work everywhere with no special permission. The wrapper exec's
# the real harness script so $0 still resolves to the install root.

if (( want_path )); then
    title "setting up PATH"
    mkdir -p "$LOCAL_BIN"
    wrapper="$LOCAL_BIN/$PROGRAM_NAME"
    target_harness="$install_root/$PROGRAM_NAME"

    cat > "$wrapper" <<EOF
#!/usr/bin/env bash
# harness wrapper — calls the real harness script in the install root.
exec "$target_harness" "\$@"
EOF
    chmod +x "$wrapper"
    ok "wrapper installed at $wrapper -> $target_harness"

    # Detect whether ~/.local/bin is already on PATH. case-style match against
    # the literal expanded directory.
    case ":$PATH:" in
        *":$LOCAL_BIN:"*)
            ok "$LOCAL_BIN already in PATH"
            ;;
        *)
            # Pick the right rcfile based on $SHELL.
            shell_name=$(basename "${SHELL:-}")
            case "$shell_name" in
                zsh)  rcfile="$HOME/.zshrc" ;;
                bash) rcfile="$HOME/.bashrc" ;;
                fish)
                    warn "fish shell detected; PATH not auto-updated"
                    warn "add this to ~/.config/fish/config.fish manually:"
                    echo "    set -gx PATH $LOCAL_BIN \$PATH"
                    rcfile=""
                    ;;
                *)    rcfile="$HOME/.profile" ;;
            esac

            if [[ -n "$rcfile" ]]; then
                # Idempotent: only append if no existing line references
                # ~/.local/bin. This is a heuristic — exact matching against
                # the literal export line we'd write would miss shell-managed
                # equivalents.
                if [[ -f "$rcfile" ]] && grep -q '\.local/bin' "$rcfile"; then
                    ok "$rcfile already references .local/bin; left untouched"
                else
                    {
                        printf '\n# Added by harness installer\n'
                        printf 'export PATH="$HOME/.local/bin:$PATH"\n'
                    } >>"$rcfile"
                    ok "appended PATH update to $rcfile"
                    warn "open a new terminal or run:  source $rcfile"
                fi
            fi
            ;;
    esac
fi

# --- final message ----------------------------------------------------------
#
# Direct, no-jargon. Lists the agents available out-of-the-box and any MCPs
# that came pre-installed (currently always zero, but the loop is here in
# case future installer flags pre-stage one).

if (( want_path )) && [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    # If install.sh was sourced (rather than run as a subprocess), update
    # the parent shell's PATH directly so `harness` works in this session
    # without opening a new terminal. When run as a subprocess the `export`
    # is harmless; the user still needs the rcfile to take effect for
    # future shells.
    export PATH="$LOCAL_BIN:$PATH"
fi

cat <<EOF

${C_BOLD}install complete${C_RESET} at $install_root.

EOF

if (( want_path )); then
    cat <<EOF
'harness' added to PATH. If it doesn't work immediately:
  - Open a new terminal, OR
  - Run: export PATH="\$HOME/.local/bin:\$PATH"

EOF
fi

cat <<EOF
Next:
  1. Edit $install_root/.env and set PROXY_API_KEY (and any other required values)
  2. cd into a project directory and run: harness <agent>

Available agents:
  harness claude
  harness opencode
EOF

# Show what MCPs are present in the bundled registry, since none are
# auto-installed by this script. Earlier the message said "Auto-installed
# MCPs:" with a list pulled from state/mcp, which was always "(none)" on
# a fresh install — misleading to users who took it as a status report
# rather than an empty-by-design state.
echo
echo "MCPs available to install:"
if [[ -d "$install_root/mcp-registry" ]]; then
    mcp_count=0
    for mcp_dir in "$install_root"/mcp-registry/*/; do
        [[ -d "$mcp_dir" ]] || continue
        name=$(basename "$mcp_dir")
        echo "  - $name"
        mcp_count=$((mcp_count + 1))
    done
    if (( mcp_count == 0 )); then
        echo "  (none in registry)"
    fi
fi
echo
echo "To install one: harness mcp install <name>"
echo "(see 'harness mcp list --available' for descriptions)"

cat <<EOF

Manage MCPs:
  harness mcp list                  show installed MCPs
  harness mcp install <name>        copy a registry entry into the active tree
  harness mcp uninstall <name>      remove entirely
  harness mcp enable <name>         start auto-loading on 'harness start'
  harness mcp disable <name>        stop auto-loading

Need a shell inside an agent container (for installing skills, debugging)?
  harness shell

If 'harness start' fails after configuration:
  harness preflight                   # validates .env and allowlist
  docker logs harness-proxy-1         # see what the proxy says

Found a bug or have an improvement to suggest, however small?
  https://github.com/HandelSim/harness/issues

Uninstall harness:
  rm -rf "$install_root"
  rm "\$HOME/.local/bin/harness"
EOF

exit_or_return 0
