# Running harness with Podman

harness supports Podman as a drop-in alternative to Docker on **Linux**. The
target configuration is **rootless Podman 4.0+** because:

- Rootless is the default Podman mode on most distros.
- 4.0+ ships the built-in `podman compose` subcommand. Earlier versions
  require `podman-compose` separately, and `podman-compose` has historical
  gaps with `depends_on: condition: service_healthy` (which the harness
  compose file uses to gate ollama on a healthy proxy).

Rootful Podman should also work but isn't the primary target.

Podman on macOS / Windows (Podman Desktop) is **out of scope** for now —
bind-mount semantics across the WSL2/QEMU machine boundary differ from
Docker Desktop's, and same-path host↔container parity (which the harness
relies on) needs a dedicated pass to get right. Use Docker Desktop on those
hosts for now.

## How harness picks a runtime

`scripts/lib/platform.sh:harness_container_runtime` resolves the runtime in
this order:

1. If `HARNESS_CONTAINER_RUNTIME` is set (`docker` or `podman`), use it.
2. Else: if `docker` is on `PATH`, use Docker.
3. Else: if `podman` is on `PATH`, use Podman.
4. Else: default to `docker` (the next call surfaces a clear "command not
   found" error).

Set the env var explicitly (in `.env` or your shell) if you have both
installed and want to force one. Otherwise the auto-detect is enough.

```bash
# In <install-root>/.env (or your shell):
HARNESS_CONTAINER_RUNTIME=podman
```

The setting flows into every `docker`-equivalent call in the harness CLI,
the install script, and the test scripts. There is no separate
`podman compose` plumbing — the harness wraps every runtime invocation
behind one of:

- `harness_docker` — runtime invocation with Windows path-translation guard.
- `harness_docker_exec` — same, but `exec`s into the runtime process.
- `harness_container_runtime` — echoes `docker` or `podman` for callers
  that need the literal name (error messages, `command -v` checks).

The names start with `harness_docker` for backwards compatibility with the
bulk of the codebase that pre-dates podman support; they also resolve under
podman.

## Linux setup (rootless)

Most distros ship Podman:

```bash
# Debian/Ubuntu
sudo apt install -y podman uidmap

# Fedora/RHEL
sudo dnf install -y podman

# Arch
sudo pacman -S podman
```

Confirm rootless is functional:

```bash
podman --version              # 4.0+ recommended
podman info | head            # should not error
podman compose version        # should print compose v2.x
```

If `podman info` fails, check:

- `cat /etc/subuid /etc/subgid` — your user must have a subuid/subgid range
  (e.g. `you:100000:65536`). If missing, run
  `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`.
- Run `podman system migrate` once after installing.

## Firewall + capabilities

The harness firewall (`firewall/init-firewall.sh`) requires `NET_ADMIN` and
`NET_RAW` to manage `iptables`/`ipset`. Under rootless Podman these caps
operate inside the container's own user namespace and apply to its own
network namespace, so iptables works as expected.

The harness compose declares both caps on every service that needs the
firewall, and the agent `docker run` adds them on the command line. No
configuration changes are needed for podman.

If you encounter firewall-init failures under rootless podman, you can
temporarily disable the firewall for one service:

```bash
harness net open agent      # persistent (until 'harness net close agent')
harness claude --net        # per-launch only
```

## `harness start` under podman

Once the env var is set (or auto-detect picks podman because docker isn't
installed), every `harness` subcommand routes through podman:

```bash
harness preflight   # 'podman runtime ✓' / 'podman compose ✓'
harness start       # builds proxy/ollama/agent images via 'podman compose'
harness doctor      # reports 'podman runtime reachable'
harness claude      # ephemeral agent container via 'podman run'
```

State paths, the bind-mounted CWD, and `--mount` extras work the same way
as under Docker.

## Smoke test

A standalone smoke test lives at `scripts/podman_smoke_test.sh`:

```bash
bash scripts/podman_smoke_test.sh
```

It pins `HARNESS_CONTAINER_RUNTIME=podman`, brings up a temp install root,
verifies `harness start`, the firewall posture inside the proxy container,
`harness doctor`, and `harness down`. It's NOT part of the default test
suite (CI doesn't have podman installed), so run it manually on your dev
host after install or after touching the runtime layer.

## Static guard

`scripts/check_runtime_calls.sh` greps the codebase for raw `docker <subcmd>`
invocations outside the wrapper layer and fails on hits. Run it after any
change to `harness`, `harness-install.sh`, or the test scripts:

```bash
bash scripts/check_runtime_calls.sh
```

## Known gaps

- macOS/Windows Podman Desktop is unsupported (see top of doc).
- The `harness_docker_path` helper (mostly for Docker Desktop on Windows)
  is a no-op on Linux+podman.
- The auto-start helper for podman on macOS/Windows is `podman machine
  start`; for podman on Linux it's a no-op (rootless podman doesn't have a
  long-running daemon to start).
- The `harness_jq` Docker-fallback (uses `<runtime> run --rm proxy:latest jq`)
  works under podman but every invocation pays the container start latency
  twice — install host `jq` if you do many `harness upgrade` cycles.
