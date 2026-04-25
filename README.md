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
└── scripts/
    ├── derisk_test.sh       Phase 1 end-to-end smoke
    ├── proxy_test.sh        Phase 2 proxy translation tests
    ├── agent_test.sh        Phase 3 end-to-end via both agents
    ├── harness_test.sh      Phase 4 management script tests
    └── build_zip.sh         produces dist/harness-distribution.zip
```

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
$ bash scripts/derisk_test.sh    # ollama RemoteHost forwarding
$ bash scripts/proxy_test.sh     # proxy translation
$ bash scripts/agent_test.sh     # end-to-end via both agents
$ bash scripts/harness_test.sh   # management script subcommands
```

## Project phases

- **Phase 1** — repo skeleton, ollama service, mock proxy, de-risk test
- **Phase 2** — real translating proxy
- **Phase 3** — agent containers (claude-code, opencode)
- **Phase 4** — `harness` management script + `install.sh` + zip
