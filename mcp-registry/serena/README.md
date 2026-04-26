# Serena MCP

Semantic code analysis MCP server from
[oraios/serena](https://github.com/oraios/serena). Lets agents understand
your codebase symbolically (find references, jump to definition, list
symbols, etc.) instead of grepping raw text.

## Enable

```
harness mcp enable serena
harness start
```

The first `start` after enabling builds Serena from upstream source
(roughly 5–10 minutes; ~2GB image). Subsequent starts reuse the cached
image, and the data dir at `<install-root>/state/mcp/serena/data/`
persists across rebuilds.

Once it's up, agents launched with `harness claude` or `harness opencode`
will find Serena listed in their MCP servers and can call its tools.

## What it can see

Serena mounts `${HARNESS_PROJECTS_ROOT:-/home}` from the host **read-only**
at `/workspaces/projects/` inside the container. It cannot modify files —
edits still flow through the agent, which has write access to its own
working directory via the normal `/workspace` mount. This is a deliberate
security tradeoff: Serena gets broad read access in exchange for being
unable to corrupt your code.

The mount lives under `/workspaces/projects/` (not `/workspaces` itself)
because the upstream serena image bakes its own install at
`/workspaces/serena/`; a flat mount at `/workspaces` would shadow it.

If you want Serena to only see specific projects, set
`HARNESS_PROJECTS_ROOT` in `<install-root>/.env` to that subtree.

## Optional env vars

| Variable                | Default      | Effect                                               |
| ----------------------- | ------------ | ---------------------------------------------------- |
| `HARNESS_PROJECTS_ROOT` | host `/home` | Host path mounted read-only at `/workspaces/projects/` |
| `SERENA_DASHBOARD_PORT` | unset        | Documents the host port at which to browse Serena's dashboard. Publishing requires a manual `docker run -p` or your own compose override mapping to `harness-serena:24282`; the static compose snippet keeps it internal-only because compose can't conditionally include a `ports:` entry. |

## Disable

```
harness mcp disable serena
```

This stops the service and removes the registry-installed config files
under `<install-root>/state/mcp/serena/`, but **leaves
`<install-root>/state/mcp/serena/data/` intact** so a future re-enable
picks up the existing index.

## Upstream

- Repo: <https://github.com/oraios/serena>
- License: see upstream
