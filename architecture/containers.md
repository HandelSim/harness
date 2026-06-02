# Containers: services, agents, firewall

How `docker-compose.yml` and the agent launch path compose into a working
runtime. For the install-time bootstrap and upgrade machinery, see
[`install-and-upgrade.md`](install-and-upgrade.md).

## Service graph

`docker-compose.yml` defines two services on a single bridge network
(`harness-net`):

```
harness-net (bridge)
├── proxy       builds from proxy/Dockerfile; runs proxy/proxy.py
└── agent       builds from agents/Dockerfile; `agent` profile (not in `up`)
```

The `agent` service is behind a compose profile so `docker compose up`
doesn't try to start it. The `harness` CLI launches agent containers via
direct `docker run`, not compose — see "Agent launch path" in
[`harness-cli.md`](harness-cli.md). The compose entry exists so
`docker compose up agent` works for debugging AND so the runtime contract
(`cap_add`, allowlist mount, network) is documented in one place.

## proxy service

Builds from `proxy/Dockerfile`; entrypoint runs `init-firewall.sh` then
`python3 proxy.py`. The proxy needs the firewall because its outbound
`PROXY_API_URL` request has to traverse the allowlist gate. opencode talks
to it directly over its OpenAI-compatible interface (`http://proxy:${PROXY_PORT}/v1`);
there is no ollama hop. See [`proxy.md`](proxy.md) for behavior.

Healthcheck: `curl -fsS http://127.0.0.1:${PROXY_PORT:-8000}/health`.

## agent containers (`agents/Dockerfile` + `agents/entrypoint.sh`)

A single image backs both modes (`opencode`, `shell`) with
a shared `/home/harness`. The entrypoint runs once at the top of every
launch (root side), regardless of mode:

1. **Firewall.** Runs `init-firewall.sh` unless
   `HARNESS_FIREWALL_DISABLED=1`. iptables/ipset need NET_ADMIN/NET_RAW,
   which `gosu` does NOT preserve, so the firewall must lay down BEFORE
   the privilege drop.
2. **UID remap.** When the host caller supplied `HOST_UID`/`HOST_GID`,
   `groupmod`/`usermod -o` remaps the `harness` user to those IDs.
   Recursive `chown` of `/home/harness` is skipped when the top-level
   ownership is already correct — on Windows + Docker Desktop the
   recursive chown across a bind-mounted host path is dramatically slow
   (every syscall is a WSL2/virtiofs translation), so the gate matters.
3. **gosu drop.** `exec gosu harness "$0" "$@"`. Agents should never run
   as root, and we want tests (which may not pass `--user 0:0`) to behave
   the same as production launches.

After the drop, still in the entrypoint:

4. **Git credentials.** `configure-git-credentials.sh` writes
   `credential.helper=/bin/false` globally, then enables `store` for any
   host annotated `# git-push` in the allowlist. Runs as the `harness`
   user so `~/.gitconfig` lives in the bind-mounted home, not
   `/root/.gitconfig`.
5. **skel seed.** On first run (gated by
   `~/.harness-home-initialized`), copies `/etc/skel/harness/.` over the
   empty bind-mounted home with `cp -an` (archive + no-clobber) so any
   files the user dropped in survive. Marks the home as initialized so
   re-running the image during `harness upgrade` does NOT re-seed.
6. **cd to host CWD.** `cd "$HARNESS_HOST_CWD"` so `pwd` inside the
   container matches the host shell.
7. **Mode dispatch.** `mode=${1:-opencode}`; calls `run_opencode` or drops
   to `bash`.

### Mode helpers

- `ensure_opencode_config` writes `~/.config/opencode/opencode.json` with
  the harness provider block pointing at `http://proxy:${PROXY_PORT}/v1`
  (default port 8000; `PROXY_PORT` is passed into the agent container as an
  `-e` env var so a custom port resolves correctly). opencode talks directly
  to the translating proxy over its OpenAI-compatible interface — there is no
  ollama hop. The provider display name is the fixed string `GenAI Harness`.
  The model dropdown is built from the proxy's `GET /v1/models` catalog (a
  verbatim pass-through of the upstream's OpenAI-format list, so ids come off
  `.data[].id` with no tag to strip) so opencode lists exactly the models the
  upstream advertises; `DEFAULT_MODEL_NAME` is always included and is the
  default selection (`harness/<DEFAULT_MODEL_NAME>`). Built with `jq` and
  re-written every launch because the model set may have changed. Falls back
  to `DEFAULT_MODEL_NAME` alone if the proxy isn't reachable (or its key is
  locked). The yolo agent's
  `permission` block grants `edit`, `bash` (all commands), `webfetch`, and
  `websearch` — matching the set of tools the agent ships so yolo mode
  doesn't trip on the new ones.
- `merge_opencode_mcp_servers` translates the canonical `{"mcpServers": {...}}`
  shape into opencode's `{"mcp": {<name>: {type: "remote"|"local", ...}}}`
  shape. Keeps the host harness script agent-agnostic.

`run_opencode` exports `OPENCODE_DISABLE_AUTOUPDATE=1` before launch (we
manage the version via `OPENCODE_VERSION` in `agents/Dockerfile`, not
opencode's self-update) and unconditionally exports `OPENCODE_ENABLE_EXA=1`
to open up opencode's built-in `websearch` tool (otherwise hidden when the
openai-compatible provider is in use — the official OpenCode provider is
opencode's other gate, and we're not that). The websearch call hits Exa's
hosted MCP at `mcp.exa.ai`; the egress firewall blocks it by design (no
separate allowlist entry — websearch belongs to the same firewall-down /
`--net` use case as the rest of unrestricted egress). With the firewall up
the env var is harmless: websearch surfaces in the tool list, and calls
return a network error at use time — the right failure mode.

### Headless `-p` output recovery

`run_opencode`'s `HARNESS_PRINT_MODE` branch (`harness -p` / `harness
opencode -p`) does NOT just `exec opencode run`. Since opencode 1.14.42 the
`run` renderer only writes the assistant body to a **non-TTY** stdout if the
text part's update event is processed before the session goes idle; for
fast/short replies (mock upstream, cached models) the idle event wins that
race and the body is silently dropped — the regression that forced the 1.15.7
rollback in issue #86. `--format json` does not avoid it either.

The body is always **persisted** to the session regardless of the race, so the
branch instead: runs `opencode run --format json` (stdout captured to a temp
file), learns the session id, then prints the final assistant text from
`opencode export <session>` (parsed with `jq` from
`{messages:[{info.role, parts:[{type:"text", text}]}]}`). A fallback extracts
text parts from the json stream if `export` yields nothing.

The session id is read from the `step_start` event when present, but that same
non-TTY race **also** drops the whole json stream on ~10% of fast runs (zero
bytes — not even `step_start`). When the stream yields no id, the branch falls
back to the newest persisted session for the launch directory via `opencode
session list --format json` (`max_by(.created)`, directory-scoped with a global
fallback), which reads the session store directly and is independent of the
render race. Without this, an empty stream means no id → `export` never runs →
empty `-p` output (the `full_pipeline_test` T10 failure on the first full CI run
of opencode 1.15.7).

The run's exit code is preserved so the tests' provider-auth skip path keeps
working. This trades live streaming (irrelevant for single-shot `-p`) for a
deterministic final answer. The opencode-version coupling and the bump checklist
live in the `OPENCODE_VERSION` note in `agents/Dockerfile`.

### Why a single image for both modes

UID remap, firewall, gosu drop, skel-seed, and git config are identical
across modes. Splitting per-tool images would duplicate that infra. The
mode dispatch happens after privilege drop; the only divergence is which
command line gets `exec`d.

## Mounts: same-path host ↔ container

The folder you run `harness` / `opencode` / `shell` from is
bind-mounted into the container at the **same absolute path** — no
`/workspace` indirection. `pwd` inside the agent matches your host
shell, which means absolute paths in code, log files, and tool output
round-trip cleanly.

- `--mount PATH` (repeatable) and `HARNESS_EXTRA_MOUNTS` (colon-separated
  in `.env`) add extra folders at their host paths. Missing paths produce
  a hard error before the container starts. Relative paths are resolved
  against the CWD.
- The CWD is ALWAYS mounted regardless of `--mount`; the agent always
  starts in the CWD.
- Nested mounts are fine (docker layers them). Duplicates are deduped.
- Paths under container infra (`/etc`, `/usr`, `/home/harness`, `/var`,
  etc.) are refused with a clear error so you can't accidentally shadow
  the image.

### Windows path handling

Git Bash uses MSYS-style absolute paths (`/c/Users/you/proj`). Inside the
container the same form is used, so `pwd` inside the agent matches what
you see in your host Git Bash session. `harness_docker_path` in
`scripts/lib/platform.sh` rewrites the source side of `-v` to the
mixed-form Windows path Docker Desktop expects; the target side is
untouched.

## Persistent agent home

A single bind-mounted home — `<install-root>/state/agent/home/` — backs
every agent invocation. Anything a user installs inside an agent (`pipx
install graphifyy`, `pip install --user requests`, custom dotfiles) is
visible across all modes and survives container rebuilds.

The image's build-time home contents are snapshotted into
`/etc/skel/harness/`, and the entrypoint copies them into an empty bind
mount on first run, marked with `~/.harness-home-initialized`.

## Universal egress firewall

`firewall/init-firewall.sh` runs at the top of every container's
entrypoint as root. Reads `/etc/harness/allowlist` (bind-mounted from
`<install-root>/.harness-allowlist`) and drops egress except to DNS, the
configured `PROXY_API_URL` host, and allowlist entries.

- `# git-push` annotation on a host in the allowlist enables `git push`
  to that host (the `store` credential helper). Without it, pull works
  but push fails.
- `HARNESS_FIREWALL_DISABLED=1` short-circuits the script and logs a loud
  bypass message. Set by `harness net open <service>` (stamped into the
  runtime override) and by `--net` per launch (set on the `docker run` env).
- The proxy container additionally validates `PROXY_API_URL`'s host is
  on the allowlist before applying rules; otherwise the proxy cannot
  reach upstream and there's no point continuing.
- The rules are IPv4-only (`iptables` + `dig +short A` + an `inet` ipset), so
  every harness container is created with the kernel IPv6 stack disabled via
  the `net.ipv6.conf.all.disable_ipv6=1` sysctl (`sysctls:` in
  `docker-compose.yml`, `--sysctl` on the CLI/`docker run` agent launches —
  it must be set at creation because `/proc/sys` is read-only inside the
  running container even with `NET_ADMIN`). Otherwise IPv6 egress would be an
  unfiltered hole, and dualstack allowlist hosts' AAAA records would draw an
  unroutable IPv6 connect attempt (instant `ENETUNREACH`) instead of using
  the allowed IPv4 path.
- See `firewall/README.md` for operator-side debugging.

`harness net` subcommands (`list`/`allow`/`deny`/`edit`/`status`/`open`/
`close`) manage the allowlist and per-service overrides. `net open`
requires the user to type `I understand the risks` on a TTY; scripts
cannot bypass.

### Host proxy reaches builds, never running containers

`HTTP_PROXY`/`HTTPS_PROXY` (see [`harness-cli.md`](harness-cli.md)) are
inherited by `docker compose build` from the harness process env: BuildKit
routes base-image pulls and the image `RUN` steps through the proxy. This is
required because the build happens on the host's BuildKit *before* any
container or firewall exists — nothing else can route it. They are **not**
injected into running containers, because `docker-compose.yml` declares no
proxy vars in any service `environment:` and compose does not copy the host
env into started containers; runtime egress stays on the runtime/firewall
path. No stripping is done in `harness_docker`/`harness_docker_exec`
(`scripts/lib/platform.sh`): build needs the proxy, and runtime simply never
receives it.

## `harness-net` is the integration surface

Every long-running service joins `harness_harness-net` (compose adds the
project-name prefix). MCP compose snippets reference this network as
`external` so they merge cleanly. Agent `docker run` invocations attach
to the same network with `--network harness_harness-net`. Everything
reachable on that network is by service name (`http://proxy:8000`,
`http://harness-serena:24282`).
