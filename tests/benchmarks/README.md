# `tests/benchmarks/` — Harbor adapters for `harness`

This directory contains the Harbor benchmark adapters for `harness claude`
and `harness opencode`, the prompt-injection schemes evaluated, the shell
runners, and the output directory for run artifacts.

Benchmarks are **never** run in CI. Each runner refuses to start when
`$CI` is set.

---

## Execution model

```
host                                                upstream LLM
+----------+        +------------------+        +-------------+
|  harbor  | -----> | per-task         | -----> |  Anthropic, |
|  (CLI)   |        | container        |        |  OpenAI,    |
+----------+        |                  |        |  ...        |
                    | harness-install  |        +-------------+
                    | docker compose   |              ^
                    |   up             |              |
                    |  +--------+      |              |
                    |  | proxy  |--+   |              |
                    |  +--------+  |---+--------------+
                    |  | ollama |
                    |  +--------+
                    |  | agent  |  <-- harness <agent> -p "<task>"
                    |  +--------+
                    +------------------+
```

Harbor (running on the host) launches one container per benchmark task.
Inside that container the adapter:

1. Clones the harness repo (`HARNESS_GIT_REF`, default `dev`).
2. Writes `.env` from runner-provided env vars
   (`PROXY_API_KEY`, `PROXY_API_URL`, `PROXY_API_MODEL`,
   `PROXY_PROMPT_MODE`, `HARNESS_PROXY_SCHEME`).
3. Runs `harness-install.sh`, which boots the docker compose stack
   (proxy + ollama + firewall + agent).
4. For each task, invokes `harness <agent> -p "<instruction>"` headlessly
   and captures stdout/stderr.

**Only outbound traffic during a benchmark run is the LLM API call.** Task
container -> docker (compose stack) -> firewall container -> upstream
host (allow-listed in `.harness-allowlist`).

---

## Docker socket mount — security tradeoff

Step 3 above needs `docker compose up` to run **inside** Harbor's per-task
container. For that to work the host's `/var/run/docker.sock` must be bind-
mounted into the task container with read-write permissions. Harbor's
container-config knob for this is typically:

```yaml
# in the dataset / task's environment config
extra_volumes:
  - "/var/run/docker.sock:/var/run/docker.sock:rw"
```

(Exact field name depends on the Harbor version and the dataset's
`environment` type — `docker-compose` vs. `apple_container` vs. `gke`. For
Terminal-Bench's default `docker-compose` environment it is the
`volumes:` list on the task service.)

**This is a privileged mount.** Anything with rw access to the docker
socket can:

- Create and start sibling containers with any image and any flag,
  including `--privileged` and host bind mounts.
- Inspect, exec into, and stop other containers on the host.
- Read images and image layers including any baked-in secrets.

In effect, write access to the docker socket is equivalent to root on the
host. Mitigations:

- **Do not run untrusted benchmark code under this configuration.** The
  Terminal-Bench upstream task corpus is read-only and curated; treat any
  fork or third-party task as untrusted.
- **Dedicated host.** Run benchmarks on a VM whose only purpose is the
  benchmark. Tear down or snapshot-revert between unrelated runs.
- **Do not enable on shared CI runners.** (We enforce this with the CI
  guard, but it's worth restating.)

Read-only mount is **not** sufficient: docker's API requires write access
to start containers. The user explicitly accepted the rw tradeoff in
project conversation; see `orch/questions.md`.

---

## ARM64 + QEMU caveat (this codebase's primary host)

Terminal-Bench task images are predominantly **x86_64**. The Oracle Linux
**aarch64** VM runs them via QEMU user-mode emulation registered through
`binfmt_misc`:

- 5-10x slowdown vs. native x86 for most CPU-bound tasks.
- Some tasks fail under emulation (e.g. tools that probe `/proc/cpuinfo`
  for x86-specific CPU flags, or that fork-and-exec under-emulated
  binaries with tight timing).
- Concurrency must start low. The shared `_lib.sh` sets
  `BENCH_DEFAULT_CONCURRENT=1` on `uname -m == aarch64`.
- Disk pressure rises faster than on native (qemu's TCG image cache and
  multiple emulated layers). Keep **≥20 GB** free on `runs/`.

### One-time host setup on aarch64

Register `qemu-x86_64` (and friends) with `binfmt_misc`:

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
# verify:
cat /proc/sys/fs/binfmt_misc/qemu-x86_64
# should contain "enabled"
```

`tonistiigi/binfmt` is the official Docker-blessed installer image and
works across distros without distro-specific package juggling.

---

## Wall-clock expectations

| Benchmark         | x86_64 native, n=4 | aarch64 + QEMU, n=1 |
| ----------------- | ------------------ | ------------------- |
| `smoketest.sh`    | 5-15 min           | 30-60 min           |
| `terminal-bench`  | 2-4 hours          | 12-24 hours         |
| `swe-bench-lite`  | 4-8 hours          | 1-2 days            |

Numbers are rough order-of-magnitude. Tune `--n-concurrent` upward only
after observing a clean smoketest.

---

## Install Harbor

```bash
uv tool install harbor
# alternatives:
pipx install harbor
pip install --user harbor

harbor --help
```

Harbor depends on Python 3.10+ (uv pulls a 3.13 toolchain by default).

---

## Smoketest first

```bash
# Run from anywhere; the runner resolves paths relative to its own
# location.
./tests/benchmarks/runners/smoketest.sh --agent claude --scheme current
```

A successful smoketest means:

- The adapter is importable on `PYTHONPATH` Harbor picked up.
- The task container builds and starts.
- The harness repo clones and `harness-install.sh` succeeds inside it.
- `harness claude -p "..."` returns a transcript.
- The trial verifier marks the result (pass or fail — either proves
  wiring; fail just means the model couldn't do the task).

Always smoketest after any change to the adapter, the schemes, or the
host's binfmt registration.

---

## Layout

```
tests/benchmarks/
  adapters/
    _common.py                          # shared HarnessAgentBase
    harness_claude/
      pyproject.toml
      harness_claude_agent.py           # imports _common
    harness_opencode/
      pyproject.toml
      harness_opencode_agent.py
  schemes/
    current.json                        # prod scheme (PROXY_PROMPT_MODE=user_front)
    passthrough.json                    # control (no injection — see caveat below)
  runners/
    _lib.sh                             # bench_guard_ci, bench_check_arch, ...
    smoketest.sh
    terminal-bench.sh
    swe-bench-lite.sh
    compare-schemes.sh
  runs/                                 # gitignored
    .gitkeep
    .gitignore
  analyze/                              # placeholder for post-run analysis
  README.md                             # this file
```

---

## Adding a new scheme

1. Create `tests/benchmarks/schemes/<name>.json`:

   ```json
   {
     "name": "<name>",
     "description": "What is being varied and why",
     "env": {
       "PROXY_PROMPT_MODE": "...",
       "HARNESS_PROXY_SCHEME": "<name>"
     }
   }
   ```

2. The runner's `bench_apply_scheme` reads the `env` map and exports
   each key before invoking `harbor run`. The harness `.env` rendering
   in `_common.py` then writes those into the per-task `.env`.

3. If the scheme requires a new `PROXY_PROMPT_MODE` value that
   `proxy/proxy.py` does not yet accept (e.g. `passthrough`), file a
   proxy change first. Without it, the proxy's startup validator
   falls back to `user_front` and the scheme silently becomes a duplicate
   of `current`.

---

## Adding a new benchmark

1. Drop a new runner under `tests/benchmarks/runners/` (e.g.
   `gpqa-diamond.sh`).
2. Source `_lib.sh`. Call `bench_guard_ci`, `bench_check_arch`,
   `bench_check_binfmt`, `bench_check_docker_socket`, `bench_check_disk`,
   `bench_require_harbor` in that order at the top.
3. Use `bench_apply_scheme` and `bench_concurrency` to honor the
   common knobs.
4. Update this README's wall-clock table.

Harbor exposes datasets via `--dataset <org>/<name>`. Most public
benchmarks Harbor knows about are listed at
<https://harborframework.com/registry>.

---

## Schemes shipped today

| Scheme       | `PROXY_PROMPT_MODE`     | Purpose |
| ------------ | ----------------------- | ------- |
| `current`    | `user_front`            | Production scheme captured at HEAD. The baseline we compare against. |
| `passthrough`| `passthrough` (planned) | Control. Proxy returns request unmodified — isolates harness contribution from upstream model capability. **Not yet implemented in `proxy.py`.** See `orch/questions.md`. |

When `passthrough` is implemented, it should:

- Skip all cooperative-prompt injection.
- Skip system-to-user rewriting.
- Pass tool definitions through to the upstream as-is (the proxy
  currently strips them).

Until then, `passthrough` runs as if `current` (proxy validator coerces
unknown modes back to `user_front`). Compare-schemes results during this
window are not meaningful for `passthrough`.

---

## Free-tier / CI note

Benchmarks consume LLM API budget at every step (cost scales with
`#tasks * #attempts * #schemes * #agents`). The runners refuse to start
under `$CI`. GitHub Actions on the free tier can absolutely launch
Harbor — but you almost certainly don't want it to. Re-run locally on a
host where you can watch the bill.

---

## Open questions / future work

- `passthrough` proxy support (see schemes table).
- Trajectory ingestion: parse `state/output/*` proxy logs and emit ATIF
  trajectories for richer Harbor `analyze` output.
- Caching: Harbor's task-container layers and harness's compose images
  are independently cached. Investigate whether a host-level base image
  with `harness-install.sh` pre-run would amortize the cold-start cost.
