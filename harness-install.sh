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
#       agent/{claude,opencode}/           persistent agent /home/harness
#       ollama-data/                       ollama model blobs
#       mcp/<name>/                        active MCP services
#
# To uninstall later:
#   rm -rf <install-root>
#   rm ~/.local/bin/harness

set -euo pipefail

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
fail()  { printf '%sx%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; exit 1; }
title() { printf '%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

cwd=$(pwd)
install_root="$cwd/$CLONE_DIR"

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
  1. Verify git, docker, and 'docker compose' are available.
  2. Refuse if $install_root already exists.
  3. Clone $REPO_URL into $install_root
  4. Create runtime state directories under $install_root/state/
  5. Seed .env (from your zip-edited .env if present in $cwd, else from .env.example).
  6. Seed .harness-allowlist from .harness-allowlist.example (if not present).
  7. Optionally symlink the harness command into $LOCAL_BIN and update PATH.

EOF

# --- CWD cleanliness check --------------------------------------------------
#
# Allow harness-install.sh, .env, README files, hidden dotfiles, and .git to
# coexist (zip extracts the first three; .git would mean someone unzipped
# into a repo). Anything else is suspicious — warn the user but don't block.

allow_re='^(harness-install\.sh|\.env|README\.md|README\.txt|quickstart\.md|\..*)$'
unexpected=$(ls -A . | grep -Ev "$allow_re" || true)
if [[ -n "$unexpected" ]]; then
    warn "current directory has unexpected entries:"
    while IFS= read -r line; do
        printf '    %s\n' "$line"
    done <<<"$unexpected"
    echo
fi

# --- prompts ----------------------------------------------------------------

read -rp "continue? [y/N]: " ans
case "${ans:-}" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 0 ;;
esac

read -rp "add 'harness' to PATH (recommended)? [Y/n]: " path_ans
case "${path_ans:-}" in
    n|N|no|NO) want_path=0 ;;
    *) want_path=1 ;;
esac

# --- prereq checks ----------------------------------------------------------

title "checking prerequisites"

if command -v git >/dev/null 2>&1; then ok "git found ($(git --version | head -1))"; else fail "git is required but not found"; fi
if command -v docker >/dev/null 2>&1; then ok "docker found ($(docker --version | head -1))"; else fail "docker is required but not found"; fi
if docker compose version >/dev/null 2>&1; then ok "docker compose found ($(docker compose version | head -1))"; else fail "docker compose v2 is required but not found"; fi

# --- clone ------------------------------------------------------------------

title "cloning repo"

if [[ -e "$install_root" ]]; then
    fail "$install_root already exists; remove it or run harness-install.sh in a clean directory"
fi
git clone "$REPO_URL" "$install_root"
ok "cloned into $install_root"

# --- runtime state dirs -----------------------------------------------------
#
# Everything under state/ is gitignored. .gitignore already excludes state/
# so these dirs never show up in `git status` inside the clone.

title "creating runtime state directories"
mkdir -p "$install_root/state/output" \
         "$install_root/state/agent/claude" \
         "$install_root/state/agent/opencode" \
         "$install_root/state/ollama-data" \
         "$install_root/state/mcp"
ok "created state/output, state/agent/{claude,opencode}, state/ollama-data, state/mcp"

# --- .env handling ----------------------------------------------------------
#
# Three cases, in priority order:
#   1. $install_root/.env already exists (unusual; clone shouldn't ship .env)
#      → leave it alone.
#   2. $cwd/.env exists (user pre-filled the zip-shipped .env)
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

if (( want_path )); then
    title "setting up PATH"
    mkdir -p "$LOCAL_BIN"
    ln -sf "$install_root/$PROGRAM_NAME" "$LOCAL_BIN/$PROGRAM_NAME"
    ok "symlinked $LOCAL_BIN/$PROGRAM_NAME -> $install_root/$PROGRAM_NAME"

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

cat <<EOF

${C_BOLD}install complete${C_RESET}

install root: $install_root

next steps:
  1. edit ${install_root}/.env and fill in any blank required values (especially PROXY_API_KEY)
  2. run: harness start
  3. cd into a project directory and run: harness claude

To uninstall later:
  rm -rf $install_root
  rm $LOCAL_BIN/$PROGRAM_NAME

If PATH was just modified, open a new terminal first.
EOF

exit 0
