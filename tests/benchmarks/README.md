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

Harbor runs in its own container (`harness-harbor:<version>`) and
launches one per-task container per benchmark task. Inside the per-task
container the adapter:

1. Clones the harness repo (`HARNESS_GIT_REF`, default `dev`).
2. Writes `.env` from runner-provided env vars
   (`PROXY_API_KEY`, `PROXY_API_URL`, `PROXY_API_MODEL`,
   `PROXY_PROMPT_MODE`, `HARNESS_PROXY_SCHEME`).
3. Runs `harness-install.sh`, which boots the docker compose stack
   (proxy + ollama + firewall + agent).
4. For each task, invokes `harness <agent> -p "<instruction>"` headlessly
   and captures stdout/stderr.

### Network model

Every long-running container in the path is under the same universal
egress firewall (`firewall/init-firewall.sh`), reading the same
`.harness-allowlist`:

```
harbor container             -> allowed: .harness-allowlist hosts only
  per-task container [*]     -> open network (apt/git/docker pulls at install)
    harness compose stack    -> allowed: .harness-allowlist hosts only
      proxy -> firewall ---> upstream LLM API
```

[*] The per-task container itself is NOT under the harness firewall —
it does `apt-get`, `git clone`, and `docker compose build` (which pulls
base images from Docker Hub / pip / npm) before the harness compose stack
comes up. That layer is intentionally left open; locking it down would
require a fully-vendored harness install image.

**Harbor's outbound is restricted to `.harness-allowlist`.** The harbor
container's entrypoint runs `init-firewall.sh` and verifies the policy
is live (example.com must be blocked; one canonical allowlist host must
be reachable) before harbor is exec'd. If harbor's hosted package
registry (Supabase backend + CDN) isn't on the allowlist, dataset-
registry calls fail closed with `icmp-admin-prohibited` rather than
leaking outbound. By default no harbor backend hosts are allowlisted —
this is what stops harbor from phoning home with anything it observes
during a benchmark run.

The only outbound the user's upstream LLM responses can travel through
is proxy -> firewall -> upstream LLM API, and that path never crosses
harbor's network namespace.

### When you DO want harbor to fetch a dataset

The shipped `smoketest.sh` uses a vendored local task
(`tests/benchmarks/tasks/hello-harness/`) so it works fully offline
w.r.t. harbor's backend — no allowlist edits required.

The full-scale runners (`terminal-bench.sh`, `swe-bench-lite.sh`) pass
`--dataset <org>/<name>` to harbor, which has to resolve the name
against harbor's hosted registry. To make this work without weakening
the production-run posture, do a one-time prefetch with the registry
hosts temporarily allowlisted, then remove them before running the real
benchmark:

```bash
# 1) Add harbor's backend hosts to .harness-allowlist (one-time).
echo 'harborframework.com'      >> .harness-allowlist
echo 'cdn.harborframework.com'  >> .harness-allowlist
# (plus the Supabase project host the harbor version uses; check the
# error from a first attempt to see which exact host gets queried.)

# 2) Run the full benchmark — harbor downloads + caches the dataset.
./tests/benchmarks/runners/terminal-bench.sh

# 3) Remove the entries from .harness-allowlist before the next run if
#    you want harbor fully sealed off again. Cached datasets stay on
#    disk; subsequent runs reuse the cache and don't re-query the
#    registry.
```

This preserves the "no harbor phone-home during benchmarking" property:
at the moment the agent is running and the upstream LLM is replying, the
firewall actively rejects any harbor outbound that isn't on the list.

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

## Harbor (dockerized only)

Harbor always runs from a pinned container — there is **no host install
path**. The wrapper at `tests/benchmarks/harbor/harbor.sh` builds the
image on first invocation and bind-mounts the host docker socket so
per-task containers it spawns are siblings on the host daemon. The
container also boots the universal harness egress firewall before exec'ing
harbor — see "Network model" above.

Host requirements: docker (with compose v2), this repo, and a
`.harness-allowlist` file (copy from `.harness-allowlist.example`). No
host Python, uv, pipx, or `harbor` binary needed.

```bash
# Image is built lazily on first runner invocation. To pre-build:
# (build context must be the repo root so the Dockerfile can COPY
# firewall/init-firewall.sh into the image)
docker build -t harness-harbor:0.6.6 -f tests/benchmarks/harbor/Dockerfile .

# Override the image tag (e.g. when bumping versions):
export HARNESS_BENCH_HARBOR_IMAGE=harness-harbor:0.7.0
```

---

## Wiring test (no API key required)

Before consuming real API budget, verify the benchmark plumbing with a
mock upstream:

```bash
# Spin up proxy + ollama + mock-api on a private compose project,
# loop over every scheme, capture per-scheme upstream requests, and
# print a summary that proves prompt-mode switching works.
./tests/benchmarks/mock-smoketest.sh                  # tests schemes/*.json
./tests/benchmarks/mock-smoketest.sh --probe-modes    # also tests every proxy mode
./tests/benchmarks/mock-smoketest.sh --keep           # leave the stack up to inspect
```

The script writes artifacts to `tests/benchmarks/runs/mock-smoketest-*/`
including the exact JSON payload the proxy sent to the mock for each
scheme. The summary table flags any schemes that produced identical
requests (i.e. schemes that aren't actually testing different prompts).

This script is **fully independent** from `runners/smoketest.sh` — it
doesn't touch your real `.env`, doesn't need Harbor, doesn't consume
LLM budget. Use it after editing schemes or after touching proxy
prompt-mode logic.

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
    passthrough.json                    # control (PROXY_PROMPT_MODE=passthrough — no mediation)
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
   `proxy/proxy.py` does not yet accept, file a proxy change first.
   Without it, the proxy's startup validator falls back to `user_front`
   and the scheme silently becomes a duplicate of `current`. The
   currently-accepted modes are listed in `architecture/proxy.md`.

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

| Scheme       | `PROXY_PROMPT_MODE` | Purpose |
| ------------ | ------------------- | ------- |
| `current`    | `user_front`        | Production scheme captured at HEAD. The baseline we compare against. |
| `passthrough`| `passthrough`       | Control. Skips every harness-side mediation: no cooperative-prompt injection, no system→user rewrite, no history translation. `tools` are forwarded to upstream verbatim. Isolates the harness contribution from upstream model capability. |

The passthrough caveat to be aware of: ollama-format tool schemas typically
aren't honored by non-ollama upstreams. Most A/B runs using passthrough
will show the model failing to call tools at all — that mismatch is
exactly the data point ("what does harness's mediation add on top of the
raw upstream?"). See `architecture/proxy.md` for the proxy-side details.

---

## Free-tier / CI note

Benchmarks consume LLM API budget at every step (cost scales with
`#tasks * #attempts * #schemes * #agents`). The runners refuse to start
under `$CI`. GitHub Actions on the free tier can absolutely launch
Harbor — but you almost certainly don't want it to. Re-run locally on a
host where you can watch the bill.

---

## Open questions / future work

- Trajectory ingestion: parse `state/output/*` proxy logs and emit ATIF
  trajectories for richer Harbor `analyze` output.
- Caching: Harbor's task-container layers and harness's compose images
  are independently cached. Investigate whether a host-level base image
  with `harness-install.sh` pre-run would amortize the cold-start cost.
