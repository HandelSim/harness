---
last_mapped_commit: 4ebbb2c
last_mapped_date: 2026-06-10
---

# Testing Patterns

**Analysis Date:** 2026-06-10

Tests live under `tests/`. Two canonical maps live beside the suites and must be kept current when behavior changes:
- `tests/INVENTORY.md` — every atomic testable behavior, one stable ID per row (F### CLI, P### proxy, A### agent entrypoint, M### MCP, N### firewall, U### upgrade, Pe### persistence, I### installer). 372 IDs as of 2026-06-08 audit.
- `tests/COVERAGE.md` — maps each inventory ID to the test(s) that exercise it with green/yellow/red status, file:line evidence, and a per-prefix breakdown. Includes a spot-check log (introduce a regression, confirm RED, revert, confirm GREEN) proving selected assertions are load-bearing.

## Test Framework

**Bash suites:** no framework. Each `tests/*_test.sh` is a self-contained `#!/usr/bin/env bash` + `set -euo pipefail` script with local `fail()`/`ok()` helpers (`fail() { echo "[<suite>] FAIL: $*" >&2; exit 1; }`, `tests/unit_host_mcp_test.sh:33-34`) and a terminal success banner (`echo "HOST MCP TEST PASSED"`). A non-zero exit = failure; first `fail` aborts the script.

**Python:** `proxy/test_proxy.py` (~1017 lines) uses the stdlib `unittest` module (`TestXxx` classes, `test_<behavior>` methods), driven via `python -m unittest`. `tests/proxy_test.sh` Scenario delegates to it.

**Shared fixtures:** `tests/lib/test_helpers.sh` (sourceable; no assertions) supplies `require_docker`, `test_section`, `test_generate_env`, `test_generate_mockupstream_override`, `test_generate_allowlist`, `test_start_mockupstream`, `test_wait_for_healthy`, and sources `scripts/lib/platform.sh` so suites inherit `harness_docker` etc. Extracted to kill setup duplication across `proxy_test.sh` / `firewall_test.sh` / `full_pipeline_test.sh` / `harness_test.sh`.

**Mock upstream:** `tests/mock_upstream.py` is a fake chat-completions server used as a compose sidecar by docker-based suites. It returns only 200 — error-path behaviors (P037-P042) are red because it can't yet emit 401/403/429/5xx.

## Run Commands

```bash
harness test                 # all tests/*_test.sh (docker-heavy; CI-style)
harness test unit            # docker-FREE slice only (see classification below)
harness test mcp             # prefix mode: tests/mcp_test.sh + tests/mcp*_test.sh
harness test --pattern 'tests/firewall_*'   # explicit glob, relative to repo root
harness test --slow          # exports HARNESS_RUN_SLOW=1 for the run
bash tests/<name>_test.sh    # run a single suite directly
python -m unittest proxy.test_proxy   # proxy pure-Python units
```

The runner is `cmd_test` (`harness:6137-6308`): it globs `$clone_dir/tests/*_test.sh`, runs each in a subshell from `clone_dir`, prints a `passed/failed` summary, and returns 1 if any failed. `--pattern` and `--slow` are the only flags; sections are positional (`all`, `unit`, `integration`, or a `<prefix>`).

## Docker-free vs Docker-based (critical split)

This split is the single most important testing fact: issue-handling agents run only the docker-free slice locally and let CI run the rest.

**Docker-FREE (`harness test unit` → globs `tests/unit_*_test.sh` plus `tests/upgrade_test.sh`, `harness:6214-6227`):**
- `tests/upgrade_test.sh` (no docker) — `envfile_merge`, `linefile_merge`, `directory_overwrite`, `_upgrade_confirm`, `_git_branches_diverged`, synthetic N→N+1 upgrade, rsync fallback, standalone `harness_jq`.
- `tests/unit_platform_timer_test.sh` — `harness_start_docker_desktop` poll-timeout logic with stubbed daemon probes.
- `tests/unit_workdir_test.sh` — Git Bash path translation (`harness_abs_path`, `harness_container_workdir`, MSYS env guards, issue #112).
- `tests/unit_host_test.sh` — host-mode CLI surface, proxy startup, loopback-only binding, model-list discovery.
- `tests/unit_host_toolchain_test.sh` — host-mode dependency checks (`host_preflight`), node-version parsing, jq/Node/opencode vendoring + venv provisioning.
- `tests/unit_host_upgrade_test.sh` — host-mode upgrade safety (`.harness-state.json` migration, venv preservation).
- `tests/unit_host_mcp_test.sh` — host-MCP CLI surface (`mcp host-init`/`host-setup`, register, list/status/up/down/logs host branches) against a tmp `HARNESS_INSTALL_ROOT`, no docker / no real server spawn.
- `tests/unit_host_mcp_net_test.sh` — docker-free host-MCP net helpers.
- `tests/unit_clipboard_test.sh` — clipboard bridge enable/disable, copy-to-host container escape path.
- `tests/unit_net_open_test.sh` — firewall opt-out (`net open`/`net close`), JSON override file manipulation.
- Also docker-free but NOT in the `unit` glob (run via `bash` directly or as their own CI section): `tests/harness_test.sh` sources the wrapper under `HARNESS_SOURCE_ONLY=1` and stubs `ensure_services_up`, so most of its CLI/doctor/net/upgrade-flag tests run without docker.

**Docker-BASED (CI only; never run from an issue agent per CLAUDE.md):** `tests/proxy_test.sh`, `tests/harness_test.sh` (its container assertions), `tests/persistence_test.sh`, `tests/mcp_test.sh`, `tests/firewall_test.sh`, `tests/scheme_contract_test.sh`. These bring up compose + mock upstream.

**Slow / gated (`HARNESS_RUN_SLOW=1`):** `tests/integration_test.sh` (Serena MCP, Graphify pipx, `--mount` rejection), `tests/full_pipeline_test.sh` (install → start → agent → mcp → down → update, builds the full image set), `tests/host_mcp_e2e_test.sh` (real host-MCP spawn — skips with "skipped (set HARNESS_RUN_SLOW=1 to run)" otherwise). `harness benchmark` targets refuse to run when `$CI` is set (`cmd_benchmark`, `harness:6359`).

## How CI Runs (`.github/workflows/ci.yml`)

Parallel jobs, each its own GitHub Actions job:
1. **lint** — `bash -n` on all shell scripts (blocking), `scripts/check_runtime_calls.sh` (no raw `docker` calls), `shellcheck` (advisory, `additional_files` list).
2. **unit** — `bash ./harness test unit` (the docker-free slice).
3. **docker / ${{ matrix.test }}** — matrix of docker-based suites, each `bash tests/${{ matrix.test }}.sh`. Relocates the Docker data-root to `/mnt` (66 GB vs ~8 GB) and caches Buildx layers because images are multi-GB. Matrix includes: `harness_test`, `proxy_test`, `persistence_test`, `mcp_test`, `firewall_test`.
4. **full_pipeline_test** — own job, full image build.
5. **integration (slow)** — own job with `HARNESS_RUN_SLOW: "1"` + pipx.
6. **scheme_contract** — own job.
7. **Auto-fix on failure (triage)** — opens a `ci-failure` issue.

CI runs the full matrix on every push/PR to `dev`/`main`. The reason the docker-free/docker-based split matters: the runner can exhaust disk on local docker builds, so local verification is intentionally limited to the unit slice + linters.

## Test File Organization & Structure

- **Naming:** `tests/<area>_test.sh`. The `unit_` prefix marks docker-free (`unit_workdir_test.sh`, `unit_host_mcp_test.sh`). Python units co-locate with the code under test (`proxy/test_proxy.py`).
- **Structure:** numbered cases `T1`, `T2`, … (`TR1`-`TR10` for MCP register; `T2b`, `T23.4b` for sub-cases; `T26pm` etc.). Each prints `--- Tn: description ---`, runs the path, asserts, and `ok`s. The inventory ID(s) a case covers are named in the assertion message and in `COVERAGE.md`.
- **Isolation:** every suite points `HARNESS_INSTALL_ROOT` (and friends — `HARNESS_ALLOWLIST_PATH`, `HARNESS_NET_OVERRIDES_PATH`, `HARNESS_REGISTRY_DIR`, `HARNESS_PROJECT_NAME`) at a `mktemp -d` tmpdir, with `trap 'rm -rf "$TMP_ROOT"' EXIT`. clone_dir still resolves to the real repo so template sources load while all writes land in the tmp.
- **Invocation seam:** unit tests run the real `harness` as a subprocess capturing combined output (`HARNESS_OUT=$( ... 2>&1 ) || rc=$?`, `tests/unit_host_mcp_test.sh:46-54`); deeper function tests `source` the wrapper under `HARNESS_SOURCE_ONLY=1` and call functions directly (`tests/harness_test.sh`).

## Mocking / Stubbing Patterns

- **Function shadowing:** redefine a bash function after sourcing the wrapper to intercept it — stub `harness_docker` to record args or fail loudly (`tests/harness_test.sh` T19b/T19c, F015/F131/F133), stub `ensure_services_up` as a sentinel (F094), stub `curl` with locked-key/200/401/500 fixtures (T0.4), shadow `command -v rsync` to force the shell-loop fallback (`tests/upgrade_test.sh` T7).
- **Fake pidfile / live-PID trick:** plant `server.pid` containing `$$` (the test's own guaranteed-alive PID) so `mcp up`'s already-running short-circuit fires without spawning a server, then remove it immediately (`tests/unit_host_mcp_test.sh:129-133`).
- **Git fixtures:** real local origin+clone repos with crafted tag/divergence history for `_downgrade_target_tag` and `_git_branches_diverged` (`tests/harness_test.sh` T32, `tests/upgrade_test.sh` T11).
- **OS pinning:** override `harness_detect_os` + stub `cygpath` to exercise Windows paths on Linux CI (`tests/unit_workdir_test.sh`).
- **Source-grep assertions:** where behavior can't be driven end-to-end, suites grep the installed artifact for a load-bearing literal (e.g. `git pull --ff-only`, the hard-coded install-root in the wrapper, `_self_path` + realpath fallback). Used heavily in `full_pipeline_test.sh`.

## What's Tested vs Gaps (from COVERAGE.md)

Strong (green): proxy translation core (P019-P036, ~all of `test_proxy.py`), CLI surface/doctor/preflight/net-allow/upgrade-actions (F, U prefixes), MCP lifecycle incl. dynamic `register` (M/TR1-TR10), installer layout + platform primitives (I), new host-mode (host_preflight, host_confirm_gate, host_proxy_ensure_venv, cmd_host covered by unit tests added 2026-06-08+).

Persistent red clusters (don't assume coverage exists):
- **Proxy error forwarding P037-P042** (401/403/429/5xx/502) and **debug-dump files P043-P050** — entirely red; the mock upstream only returns 200 and `OUTPUT_DIR` is set empty to bypass dumps.
- **Firewall rule introspection N002-N011, N015-N016, N019-N028** — mostly red; no test reads back iptables/ipset state from a clean init. Only bypass (N001/N017) and one negative (N018) are covered.
- **Interactive/401-gated CLI surfaces** F045 (`shell`), F057-F061 (`stop`/`pick_agent`), F081-F092 (`net open`/`close`/`unlock` interactive) — red, hard to drive without a TTY or 401 mock.

---

*Testing analysis: 2026-06-10*
