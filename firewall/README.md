# harness firewall

Two scripts that ship inside every harness container to enforce a default-deny
egress policy and block accidental git pushes:

- `init-firewall.sh` — sets up iptables + ipset rules; reads the allowlist
  from `/etc/harness/allowlist` (mounted from `<install-root>/.harness-allowlist`).
- `configure-git-credentials.sh` — sets `credential.helper=/bin/false`
  globally, then enables `store` for any host annotated `# git-push` in the
  allowlist.

Both are adapted from the canonical Anthropic devcontainer init script:
<https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh>

## Where they run

Each container's entrypoint invokes `init-firewall.sh` early — for proxy
and ollama, as the only privileged process; for agents, before the gosu
drop, so the script runs with `NET_ADMIN`/`NET_RAW`. `init-firewall.sh`
deliberately does NOT call `configure-git-credentials.sh` itself. Agent
entrypoints invoke it after the gosu drop so `git config --global` writes
to `/home/harness/.gitconfig` rather than `/root/.gitconfig`. Proxy and
ollama skip it entirely (no git inside those images).

`docker-compose.yml` and the harness CLI both pass `--cap-add NET_ADMIN
--cap-add NET_RAW` and mount the allowlist read-only.

## Allowlist format

One host per line. `#` starts a comment. Inline `# git-push` annotation on a
host enables `git push` to that host (`store` credential helper). Without it,
git pull works but push fails — the default for new installs.

```
# pull-only
github.com
api.github.com

# push allowed (still requires the user's own credentials in
# $HOME/.git-credentials)
my-self-hosted-git.example.com   # git-push
```

## Debugging

Container exits 1 immediately on start, before anything useful runs?

1. Check container logs: `docker compose logs <service>` — the firewall
   prints clearly-prefixed `[harness-firewall]` lines and a final
   `[harness-firewall] FATAL: ...` line on the failure case.
2. Common failures and fixes:
   - **"allowlist file not found at /etc/harness/allowlist"** — the
     compose mount isn't wired up. Confirm `volumes:` in the service
     definition includes `${HARNESS_ALLOWLIST_PATH:-...}:/etc/harness/allowlist:ro`.
   - **"PROXY_API_URL hostname '<x>' is not in /etc/harness/allowlist"** —
     add the upstream hostname to the allowlist (B2's `harness net allow
     <host>` will do this for you, or edit the file directly).
   - **"example.com is reachable but should be blocked"** — iptables/ipset
     packages missing from the image, or the container is running without
     `NET_ADMIN`. Confirm `cap_add: [NET_ADMIN, NET_RAW]` on the service.
   - **"could not resolve <host>"** — typo in allowlist, or the host is
     genuinely down. Each unresolvable host is logged WARN; the firewall
     continues with whatever did resolve.

## Emergency bypass

If you need to debug a container without the firewall (say, to confirm a
problem is firewall-induced vs application-induced):

1. Edit `<install-root>/.harness-allowlist` to add a wildcard-ish set of
   hosts the failing operation needs (you cannot blanket-disable; the
   firewall is wired into the container's startup path).
2. `harness restart` (B2) or `harness start` to reload.
3. After debugging, restore the original allowlist.

A future `harness net open <service>` (B2) will provide a one-command path
to lift the firewall on a single service for an interactive session.
