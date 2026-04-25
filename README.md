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
├── docker-compose.yml      services: ollama, proxy, agents
├── .env.example            documented env variables (copy to ../.env)
├── ollama/                 custom ollama image + entrypoint that registers
│                           the stub model with RemoteHost set to the proxy
├── proxy/                  the translating proxy (Phase 1: mock; Phase 2: real)
├── agents/                 agent images (populated in Phase 3)
└── scripts/
    └── derisk_test.sh      end-to-end test of the curl→ollama→proxy round trip
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

End users do not clone this repo. They install via the zip distribution
produced in Phase 4 (separate README ships in that zip).

## Project phases

- **Phase 1** — repo skeleton, ollama service, mock proxy, de-risk test
- **Phase 2** — real translating proxy
- **Phase 3** — agent containers (claude-code, opencode)
- **Phase 4** — `harness` management script + `install.sh` + zip
