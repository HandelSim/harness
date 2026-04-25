#!/usr/bin/env bash
#
# harness installer.
#
# Run from the directory you want to install into. The current working
# directory becomes the "install root" — the script clones the harness
# repo as ./harness/, creates the persistent dirs alongside it, and
# (optionally) symlinks the management script into ~/.local/bin.
#
# Layout produced:
#   <cwd>/
#     install.sh               (this file)
#     .env                     (kept if present, else copied from .env.example)
#     README.md                (kept if shipped in zip)
#     harness/                 (the clone)
#     output/, agent/{claude,opencode}/, ollama-data/

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

# --- intent -----------------------------------------------------------------

cat <<EOF
${C_BOLD}harness installer${C_RESET}

This will install the harness runtime into:
  $cwd

Steps:
  1. Verify git, docker, and 'docker compose' are available.
  2. Clone $REPO_URL into ./harness
  3. Create persistent directories: output/, agent/claude/, agent/opencode/, ollama-data/
  4. Set up your .env (copied from .env.example if not already present)
  5. Optionally symlink the harness command into $LOCAL_BIN and update PATH.

EOF

# --- CWD cleanliness check --------------------------------------------------
#
# Allow install.sh, .env, README files, hidden dotfiles, and .git to coexist
# (zip extracts the first three; .git would mean someone unzipped into a repo).
# Anything else is suspicious — warn the user but don't block.

allow_re='^(install\.sh|\.env|README\.md|README\.txt|quickstart\.md|\..*)$'
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

if [[ -e "$cwd/$CLONE_DIR" ]]; then
    fail "$cwd/$CLONE_DIR already exists; remove it or run install.sh in a clean directory"
fi
git clone "$REPO_URL" "$cwd/$CLONE_DIR"
ok "cloned into $cwd/$CLONE_DIR"

# --- persistent dirs --------------------------------------------------------

title "creating persistent directories"
mkdir -p "$cwd/output" "$cwd/agent/claude" "$cwd/agent/opencode" "$cwd/ollama-data"
ok "created output/, agent/claude/, agent/opencode/, ollama-data/"

# --- .env handling ----------------------------------------------------------

title "configuring .env"
if [[ -f "$cwd/.env" ]]; then
    ok ".env already present; left untouched"
else
    cp "$cwd/$CLONE_DIR/.env.example" "$cwd/.env"
    ok "copied .env.example to .env"
    warn "edit .env and fill in PROXY_API_KEY (and any other blank required values)"
fi

# --- PATH setup -------------------------------------------------------------

if (( want_path )); then
    title "setting up PATH"
    mkdir -p "$LOCAL_BIN"
    ln -sf "$cwd/$CLONE_DIR/$PROGRAM_NAME" "$LOCAL_BIN/$PROGRAM_NAME"
    ok "symlinked $LOCAL_BIN/$PROGRAM_NAME -> $cwd/$CLONE_DIR/$PROGRAM_NAME"

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

next steps:
  1. edit ${cwd}/.env and fill in any blank required values (especially PROXY_API_KEY)
  2. run: harness start
  3. cd into a project directory and run: harness claude

If PATH was just modified, open a new terminal first.
EOF

exit 0
