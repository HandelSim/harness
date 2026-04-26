#!/usr/bin/env bash
#
# firewall/configure-git-credentials.sh — block accidental git push.
#
# Default behavior: every host has credential.helper = /bin/false, so any
# `git push` that needs a credential prompt fails immediately. Hosts marked
# with an inline `# git-push` annotation in the allowlist file get
# credential.<host>.helper = store, which lets the user use git push to that
# host (with credentials they've placed in $HOME/.git-credentials, or via an
# agent's own auth flow).
#
# Invoked from /usr/local/bin/init-firewall.sh AFTER the firewall is in place
# and AFTER the entrypoint has dropped privileges to the harness user. That
# matters because `git config --global` writes to $HOME/.gitconfig, and we
# want the harness user's gitconfig, not root's.

set -euo pipefail

allowlist="${1:-/etc/harness/allowlist}"

if [[ ! -f "$allowlist" ]]; then
    echo "[harness-git-creds] FATAL: allowlist file not found at $allowlist" >&2
    exit 1
fi

# Step 1: global default = block all credential helpers. Setting this to
# /bin/false means any prompt for credentials returns failure immediately,
# rather than blocking on an interactive prompt or pulling from the user's
# real keychain.
git config --global credential.helper /bin/false

# Step 2: scan the allowlist for `# git-push` annotations and enable a
# real credential helper for those hosts. We use the `store` helper, which
# reads $HOME/.git-credentials. If the file doesn't exist, push will still
# fail (with a "could not read username" error), but it won't prompt and
# leak credentials from the host's keychain.
push_hosts=()
while IFS= read -r line; do
    case "$line" in
        ''|\#*) continue ;;
    esac
    if [[ "$line" == *"#"*"git-push"* ]]; then
        host=$(awk '{print $1}' <<<"$line")
        [[ -z "$host" ]] && continue
        git config --global "credential.https://${host}.helper" "store"
        push_hosts+=("$host")
    fi
done < "$allowlist"

if (( ${#push_hosts[@]} > 0 )); then
    echo "[harness-git-creds] git push enabled for: ${push_hosts[*]}"
else
    echo "[harness-git-creds] git push disabled for all hosts (no '# git-push' annotations in allowlist)"
fi

echo "[harness-git-creds] configured."
