# harness

A docker-based runtime that lets you launch a coding agent (claude-code,
opencode) against a third-party API endpoint, transparently. The agent runs
in a container, talks to a local ollama instance, and ollama forwards chat
requests to a translating proxy that calls the upstream API.

```
agent container ──► ollama ──(RemoteHost forward)──► proxy ──► upstream API
```

This repo is the source for the harness runtime. End users install via a
zip distribution (built in Phase 4); this repo is for development.

## Repo structure

```
harness/
├── harness                  management CLI (Phase 4)
├── install.sh               bootstrap installer (Phase 4; also bundled in zip)
├── zip-readme.md            README that ships in the distribution zip
├── docker-compose.yml       services: ollama, proxy, agents
├── .env.example             documented env variables (copy to ../.env)
├── ollama/                  custom ollama image + entrypoint that registers
│                            the stub model with RemoteHost set to the proxy
├── proxy/                   the translating proxy
├── agents/                  agent images (claude, opencode)
├── mcp-registry/            vetted MCP service definitions (Phase 6)
└── scripts/
    ├── derisk_test.sh       Phase 1 end-to-end smoke
    ├── proxy_test.sh        Phase 2 proxy translation tests
    ├── agent_test.sh        Phase 3 end-to-end via both agents
    ├── harness_test.sh      Phase 4 management script tests
    ├── persistence_test.sh  Phase 6 persistent home + skel-seed test
    ├── mcp_test.sh          Phase 6 MCP enable/disable lifecycle test
    └── build_zip.sh         produces dist/harness-distribution.zip
```

## Persistent agent homes

The agent containers' entire `/home/harness` is bind-mounted from
`<install-root>/agent/<tool>/`. Anything a user installs inside an agent
(`pipx install graphifyy`, `pip install --user requests`, custom dotfiles)
survives container rebuilds. The image's build-time home contents are
snapshotted into `/etc/skel/harness/`, and the entrypoint copies them into
an empty bind mount on first run, marking with
`~/.harness-home-initialized` so subsequent runs skip the seed.

## MCP registry

Long-running MCP servers — Serena, etc. — are described by a small set of
files under `mcp-registry/<name>/`:

- `compose.yml` — partial compose snippet defining the service. References
  the `harness_harness-net` network as external so it merges cleanly with
  the main `docker-compose.yml`. Lives behind the `mcp` profile so
  `docker compose up` without the profile leaves it alone.
- `client-config.json` — the entry that gets merged into the agent's MCP
  config. Uses the `{"mcpServers": {"<name>": {...}}}` shape.
- `README.md` — what the service does, what it mounts, security notes.

Users enable a registry entry with `harness mcp enable <name>`. The
`harness` script copies the entry to `<install-root>/mcp/<name>/` (the
"active tree"), discovers it on subsequent `harness start` invocations,
and writes the merged client config into each agent's home dir before
launch. `harness mcp disable <name>` reverses the activation but keeps
`<install-root>/mcp/<name>/data/` intact.

To contribute a new registry entry:

1. Create `mcp-registry/<name>/` with the three required files.
2. Make sure the compose snippet uses `profiles: [mcp]` and joins the
   `harness-net` network.
3. Document any optional env vars in `README.md` and add them to
   `.env.example` if they have a default that users should know about.
4. Test locally with `HARNESS_REGISTRY_DIR=$(pwd)/mcp-registry harness mcp enable <name>`.

## Local development

The `.env` file lives at the install root, one directory above the clone:

```
$ cp .env.example ../.env
$ $EDITOR ../.env       # fill in PROXY_API_URL / PROXY_API_KEY / PROXY_API_MODEL
$ docker compose --env-file ../.env up --build
```

To expose ollama on the host (useful for poking at it from outside the docker
network), set `PUBLISH_OLLAMA_PORT=11434` in `../.env`.

## De-risk test

Phase 1 ships an automated end-to-end test that verifies ollama's RemoteHost
forwarding is wired up correctly:

```
$ bash scripts/derisk_test.sh
```

The script builds the images, brings up ollama + a mock proxy, and asserts
that a chat request to ollama is forwarded to the proxy and the response is
returned to the caller. It tears everything down on exit.

## End-user installation

End users do not clone this repo. They install via the distribution zip,
which ships `install.sh`, a pre-filled `.env`, and a quick-start README.
See `zip-readme.md` for the user-facing instructions.

To build the distribution zip from the current repo state:

```
$ bash scripts/build_zip.sh
# -> dist/harness-distribution.zip
```

## Tests

```
$ bash scripts/derisk_test.sh        # ollama RemoteHost forwarding
$ bash scripts/proxy_test.sh         # proxy translation
$ bash scripts/agent_test.sh         # end-to-end via both agents
$ bash scripts/harness_test.sh       # management script subcommands
$ bash scripts/persistence_test.sh   # persistent home + skel seed
$ bash scripts/mcp_test.sh           # MCP enable/disable lifecycle
$ bash scripts/full_pipeline_test.sh # full install + run pipeline
```

## Project phases

- **Phase 1** — repo skeleton, ollama service, mock proxy, de-risk test
- **Phase 2** — real translating proxy
- **Phase 3** — agent containers (claude-code, opencode)
- **Phase 4** — `harness` management script + `install.sh` + zip
- **Phase 6** — persistent agent homes + MCP server registry
