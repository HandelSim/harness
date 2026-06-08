# Codebase Concerns

**Analysis Date:** 2026-06-08

Scope: current `harness` codebase on branch `dev`, with focus on the newly
added containerless `harness host` mode (commit 5f38a83) and how it integrates
with the docker-coupled rest of the system. Every concern below cites a
file:line that was read directly. Concerns are ranked by severity within each
section; the highest-severity items are at the top.

Verified vs inferred is called out per item. "Verified" = read the code path
end to end. "Inferred" = read the relevant code but did not execute it.

---

## Resolution status (updated 2026-06-08)

This audit drove two fix commits. Per-item status is repeated inline below.

- **Fixed in commit 4fe4361** ("Make host-only installs upgrade and transition
  correctly; refresh gsd map"): C1, H1, H2.
- **Fixed in this commit**: H3, H4, M1, M2, M3, M4, M5, L3, L4.
- **Deliberately deferred** (not bugs; large refactors flagged "do
  opportunistically" in the audit itself): L1 (proxy.py monolith split), L2
  (harness CLI monolith split). Left open on purpose.

---

## CRITICAL

### C1 — `harness upgrade` is unusable on a host-only install (no docker)

> **RESOLVED (commit 4fe4361).** `cmd_upgrade` now splits code/config (git pull
> + manifest apply + config merge, no docker) from rebuild/restart; the host-only
> path runs the merge and no-ops the rebuild with an informational message.

- **Concern:** `cmd_upgrade` calls `require_docker` unconditionally before doing
  any work (`harness:1820`). The installer now explicitly supports a host-only
  install with NO container runtime (`harness-install.sh:317-319`, sets
  `HOST_ONLY=1`), and tells the user "install host-only … `harness host` runs
  containerless." But the only documented way to update that install — `harness
  upgrade` — hard-exits at `require_docker` (`harness:1820` → `harness:525-527`
  / `530-535`) because no runtime is reachable. The host-only user is told to
  use `harness host` for running, but has no working upgrade path.
- **Impact:** Host-only installs cannot upgrade. They are stranded on whatever
  code they installed unless the user manually runs `harness update` (a bare
  `git pull --ff-only`, `cmd_update`) and skips the manifest-apply + config-merge
  that `upgrade` performs. New `.env`/allowlist keys never get merged; the
  install silently drifts.
- **Severity:** CRITICAL — it breaks the maintenance story for a mode the
  installer actively offers.
- **Verified.** (`harness:1820`, `harness:514-536`, `harness-install.sh:317-319`.)
- **Remediation:** Split upgrade into "code/config" (git pull + manifest apply +
  config merge — no docker) and "rebuild/restart" (docker). Gate only the
  rebuild/restart half behind `require_docker`, mirroring how `cmd_downgrade`
  already guards rebuild with `if (( ! no_restart )); then require_docker; fi`
  (`harness:2069-2071`). A host-only upgrade should run the manifest + merge and
  then no-op the image rebuild with an informational message, not abort. (Note:
  the orchestrator was already told this is being fixed; left here as the
  primary item for completeness and to anchor the related gaps below.)

---

## HIGH

### H1 — `harness doctor` has zero host-mode awareness and mis-reports a healthy host install as broken

> **RESOLVED (commit 4fe4361).** Added a `[host]` doctor section (proxy running,
> venv stamp, port, logfile tail) and demoted the container/compose/allowlist
> checks to skip/warn when no runtime is reachable.

- **Concern:** `cmd_doctor` opens its `[deps]` section by probing the container
  runtime and printing `fail` when it is not reachable (`harness:5679-5683`),
  then checks `$rt compose` (`harness:5685-5691`). The `[network]` section hard-
  requires the firewall allowlist and prints `fail` when it is missing
  (`harness:5776`, `5800`) — but host mode has no allowlist by design
  (`harness:2684-2686`, the comment on `host_require_config`). The `[runtime]`
  section inspects the docker network and proxy container health
  (`harness:5847-5870`). None of it knows about `state/host/` — the host proxy
  pidfile/venv/logfile (`harness:2618-2622`) — so a perfectly working host
  install reports multiple `fail`s and no host diagnostics at all.
- **Impact:** A host-only user running `harness doctor` to debug a problem gets a
  wall of red that describes container mode they never use, and gets zero signal
  about the thing that's actually running (host proxy up? venv built? port
  listening? logfile tail?). Doctor is advertised as the diagnostic entry point
  (`harness:1386`, `cmd_help`), so this actively misleads.
- **Severity:** HIGH — diagnostics that lie are worse than no diagnostics.
- **Verified.** (`harness:5671-5870`, `harness:2618-2622`, `harness:2684-2686`.)
- **Remediation:** Add a `[host]` doctor section that reports `host_proxy_running`,
  the venv stamp state, the configured port, and the last lines of
  `host_proxy_logfile`. Demote the container-runtime/compose/network/allowlist
  checks from `fail` to `skip`/`warn` ("container mode; not configured") when no
  runtime is reachable, instead of `fail`.

### H2 — `harness preflight` is container-only and reports a healthy host install as failing

> **RESOLVED (commit 4fe4361).** Host-mode preflight is reachable and the
> runtime/allowlist checks are advisory on a host-only install.

- **Concern:** `cmd_preflight` increments `errors` when no container runtime is
  reachable (`harness:5995-5996`, `6002-6003`) and treats a missing firewall
  allowlist as an error (`harness:6010`, `6013`), plus requires the
  `PROXY_API_URL` host to be in that allowlist (`harness:6046-6058`). Host mode
  requires none of this. `cmd_help` even describes preflight as "validate config
  (.env, allowlist, docker daemon)" (`harness:1387-1388`) — there is no host-mode
  preflight surfaced to users (`host_preflight` exists at `harness:2647` but is
  only reachable via `cmd_host`, never as a standalone `harness preflight host`).
- **Impact:** Same failure shape as H1 for the pre-launch validator: a host user
  cannot run `harness preflight` to sanity-check their setup; it will always
  report errors for docker + allowlist they intentionally don't have.
- **Severity:** HIGH.
- **Verified.** (`harness:5976-6058`, `harness:2647-2682`, `harness:1387-1388`.)
- **Remediation:** Either accept a `harness preflight host` subcommand that calls
  `host_preflight` + `host_require_config`, or auto-detect host-only and switch
  the runtime/allowlist checks to advisory. Update the `cmd_help` one-liner.

### H3 — Host mode unconditionally enables debug dumps; container mode defaults them OFF

> **RESOLVED (this commit).** `host_proxy_start` no longer force-sets
> `OUTPUT_DIR`; host mode now honors the same opt-in as container mode (default
> empty = no dumps), passing `OUTPUT_DIR` through from `.env`. The opt-in value is
> part of the config fingerprint (M2), so toggling it restarts the proxy.

- **Concern:** `host_proxy_start` always sets `OUTPUT_DIR="$state_root/output"`
  (`harness:2797`). The proxy writes a per-request JSON debug file for every
  pipeline stage when `OUTPUT_DIR` is set (`proxy.py:453-461`, `init_output_dir`
  at `proxy.py:437-450`; 20 `save_debug_file` call sites). These payloads are the
  full request/response bodies — i.e. complete prompts, file contents the agent
  read, and model output. Container mode deliberately defaults this OFF and makes
  it opt-in (`docker-compose.yml:39-42`, `OUTPUT_DIR: ${OUTPUT_DIR:-}` with the
  comment "empty by default (no debug dumps). Users opt in").
- **Impact:** (1) Behavior divergence: the same proxy silently logs everything in
  host mode but nothing in container mode, which will confuse anyone comparing the
  two. (2) Privacy/disk: host mode is the *less* sandboxed mode (full host user,
  no firewall), and it's the one that unconditionally writes every prompt — which
  may contain secrets the agent read from `~/.ssh`/`~/.aws` — to
  `state/host/../output/` as plaintext JSON, unbounded, with no rotation. The
  gate warning (`host_confirm_gate`, `harness:2710-2746`) never mentions this.
- **Severity:** HIGH — unexpected plaintext capture of sensitive prompt data in
  the highest-blast-radius mode.
- **Verified.** (`harness:2797`, `proxy.py:437-461`, `docker-compose.yml:39-42`.)
- **Remediation:** Make host mode honor the same opt-in: pass `OUTPUT_DIR`
  through from `.env` (default empty) instead of force-setting it. If a default
  dump location is wanted for host debugging, mention it in `host_confirm_gate`
  and add cleanup/rotation.

### H4 — No tests at all for containerless host mode

> **RESOLVED (this commit).** Added docker-free `tests/unit_host_test.sh` (7
> tests, auto-discovered by `harness test unit`): `host_require_config` rejection,
> `host_confirm_gate` auto-confirm, `host_preflight` missing-dep reporting,
> `host_write_opencode_config` JSON shape + jq guard, `host_proxy_fingerprint`
> stability. Catalogued as Ho001–Ho007 in INVENTORY.md / COVERAGE.md.

- **Concern:** A repo-wide grep for `harness host` / `cmd_host` / `host_proxy_*`
  / `host_confirm_gate` / `host_preflight` / `host_run_opencode` /
  `host_write_opencode_config` across `tests/` returns nothing. (The `host_mcp_*`
  test files are for the unrelated host-MCP feature, not containerless mode.)
- **Impact:** The entire new command path — preflight, config validation, the
  confirm gate, venv build, proxy supervision (start/wait/stop), opencode config
  generation, and the `-p` print-mode export dance (`harness:2940-2977`) — ships
  with zero automated coverage. This is the most error-prone area (process
  supervision, JSON generation via `harness_jq`, the `-p` session-export
  fallback chain) and a regression in any of it would be invisible to CI.
- **Severity:** HIGH.
- **Verified.** (grep over `/home/opc/repos/harness/tests/` returned no
  containerless-host matches; `tests/upgrade_test.sh` "host" hits are host-MCP.)
- **Remediation:** Add a docker-free `unit_host_test.sh`: assert `host_preflight`
  fails with clear messages when node/jq/python3/opencode are absent; assert
  `host_require_config` rejects empty required vars; assert `host_confirm_gate`
  refuses without `/dev/tty` and honors `HARNESS_HOST_CONFIRM=1`; assert
  `host_write_opencode_config` emits valid JSON with `baseURL` →
  `127.0.0.1`. The proxy-process lifecycle can be smoke-tested with a stub
  `proxy.py` that just binds the port.

---

## MEDIUM

### M1 — `harness host -p` (print mode) leaves the proxy running indefinitely with no reaper

> **RESOLVED (this commit).** Print mode now prints a stderr reminder after the
> run naming the live proxy pid/port and the `harness host down` stop command;
> the lifecycle (interactive stops, `-p` leaves up) is documented in `cmd_host
> --help` and the `cmd_help` host entry.

- **Concern:** In print mode, `host_run_opencode` deliberately does NOT call
  `host_proxy_stop` (only the interactive branch does, `harness:2985`); the
  docstring says this is to avoid thrashing a scripted `-p` loop
  (`harness:2916-2919`). The only way to stop it is `harness host down`
  (`cmd_host_down`, `harness:3025-3032`). There is no EXIT trap, no idle timeout,
  and no PID-ownership check — a user who runs one `harness host -p "..."` and
  walks away leaves a Flask server bound to 127.0.0.1:8000 forever.
- **Impact:** Orphaned proxy holding the port. A subsequent container-mode
  `harness start` that also wants 8000, or a second host launch after the user
  forgot, collides. `host_proxy_running` keys only on the pidfile
  (`harness:2627-2640`), so a manually-killed proxy with a stale pidfile is
  handled, but a still-running orphan from a prior `-p` is silently reused — fine
  if config is unchanged, wrong if `.env`/port changed since (see M2).
- **Severity:** MEDIUM.
- **Verified.** (`harness:2916-2987`, `harness:3025-3032`, `harness:2627-2640`.)
- **Remediation:** Document the lifecycle in `cmd_help` (the host help block at
  `harness:1368-1375` doesn't mention that `-p` leaves the proxy up), and
  consider an idle-timeout or a note printed after `-p` runs telling the user to
  `harness host down`.

### M2 — Reused host proxy ignores changed config / port

> **RESOLVED (this commit).** `host_proxy_start` now writes a config fingerprint
> (`host_proxy_fingerprint` over port + url + key + model + output) to
> `state/host/proxy.fp` beside the pidfile. On relaunch it compares the live
> fingerprint to the current `.env`; on mismatch it prints ".env changed since the
> proxy started; restarting it" and restarts instead of reusing the stale proxy.
> `host_proxy_stop` removes the fp file too.

- **Concern:** `host_proxy_start` short-circuits with `host_proxy_running &&
  return 0` (`harness:2786`) keyed purely on a live PID in the pidfile. The
  running proxy captured its `.env` (PROXY_API_URL/KEY, DEFAULT_MODEL_NAME) and
  `PROXY_PORT` at the moment it started (`harness:2795-2799`). If the user edits
  `.env` (new key, new model, new `PROXY_PORT`) and re-runs `harness host`, the
  stale proxy is reused with the OLD config, while `host_write_opencode_config`
  regenerates the opencode config from the NEW `.env` (`harness:2854-2911`) and
  `host_proxy_wait_ready` probes `host_proxy_port` from the NEW `PROXY_PORT`
  (`harness:2808-2810`). On a port change the probe waits on a port nothing is
  listening on and fails after ~10s with a confusing "did not start" error.
- **Impact:** Silent stale-config use (best case) or a confusing timeout on a
  port change (worse case). No "config changed, restarting proxy" detection.
- **Severity:** MEDIUM — inferred for the port-mismatch failure (read the paths,
  did not run); the stale-config reuse is directly verifiable from the code.
- **Verified/Inferred.** (`harness:2785-2801`, `2808-2810`, `2854-2911`; the
  timeout outcome on a port change is inferred.)
- **Remediation:** Record the config fingerprint (port + a hash of the required
  vars) alongside the pidfile; if it differs from the current `.env`, stop and
  restart the proxy instead of reusing it.

### M3 — `proxy.py` defaults to binding `0.0.0.0`; host-mode safety relies entirely on one env var

> **RESOLVED (this commit).** Added defense-in-depth: `proxy.py:_validate_config`
> now honors `HARNESS_FORCE_LOOPBACK` and exits fatally if it is set while
> `PROXY_HOST` is non-loopback. `host_proxy_start` sets `HARNESS_FORCE_LOOPBACK=1`
> alongside `PROXY_HOST=127.0.0.1`, so a dropped/overridden bind cannot expose the
> firewall-less host proxy off-box.

- **Concern:** `PROXY_HOST` defaults to `0.0.0.0` (`proxy.py:56`), i.e. all
  interfaces. Host-mode loopback-only binding is enforced solely by
  `host_proxy_start` exporting `PROXY_HOST=127.0.0.1` (`harness:2795`). There is
  no defense-in-depth in the proxy itself: if that single export is ever dropped,
  reordered, or overridden (e.g. a user sets `PROXY_HOST` in `.env`, which is
  sourced with `set -a` into the same shell — `host_proxy_start`'s inline
  assignment does win, but `.env` precedence here is subtle and untested per H4),
  the host proxy — which fronts the upstream API key in its `Authorization`
  header (`proxy.py:2145`, `2232`) and has no auth of its own — would be exposed
  on the LAN.
- **Impact:** A one-line regression in the launch path turns the host proxy into
  an open, unauthenticated relay to the paid upstream, reachable by anyone on the
  network. The egress firewall that would normally contain this does not exist in
  host mode (by design, `harness:2610-2612`).
- **Severity:** MEDIUM (low likelihood, high impact; mitigated today by the
  hardcoded export, but with no second layer).
- **Verified.** (`proxy.py:56`, `proxy.py:2509-2512`, `harness:2795`,
  `proxy.py:2145`/`2232`.)
- **Remediation:** Add a `HARNESS_FORCE_LOOPBACK=1` (or similar) that
  `proxy.py:_validate_config` honors to refuse any non-loopback `PROXY_HOST`,
  and have `host_proxy_start` set it. Belt-and-suspenders for the no-firewall
  mode. Alternatively change the proxy default to `127.0.0.1` and have
  container mode opt into `0.0.0.0` explicitly.

### M4 — `harness_jq` is reachable from a host-mode path that ran preflight, but the dependency contract is fragile

> **RESOLVED (this commit).** `host_write_opencode_config` now asserts `command
> -v jq` up front and fails with a clear message if jq is absent, so the
> docker-sidecar fallback can never be reached from a host path even if the
> preflight ordering invariant is ever broken. Covered by test Ho006.

- **Concern:** `host_write_opencode_config` and the `-p` export logic call
  `harness_jq` (`harness:2868`, `2884`, `2888`, `2961`, `2964`, `2969`, `2972`).
  `harness_jq` falls back to a docker sidecar when host `jq` is absent
  (`harness:206-229`, `_ensure_jq_sidecar:247-320`), which in host-only mode has
  no docker and would fail with the "jq required … runtime isn't installed
  either" error (`harness:252-258`). This is currently *prevented* because
  `host_preflight` hard-requires host `jq` before any of these run
  (`harness:2655-2659`, called first in `cmd_host` at `harness:3007`). So the
  path is safe today — but only by that single ordering invariant.
- **Impact:** Low today (preflight guards it). The risk is latent: any future
  host-mode code that calls `harness_jq` *before* `host_preflight`, or any
  refactor that makes preflight non-fatal, silently reintroduces a docker
  dependency into the "no docker" mode. The two are 350+ lines apart and the
  coupling is implicit.
- **Severity:** MEDIUM (latent, not live).
- **Verified.** (`harness:2647-2682`, `harness:3007-3009`, `harness:206-258`.)
- **Remediation:** In host mode, prefer a direct `jq` invocation (preflight
  guarantees it's present) or add an assertion in `host_write_opencode_config`
  that `command -v jq` succeeds, so the docker-sidecar fallback can never be
  reached from host paths. Document the ordering invariant next to
  `host_preflight`.

### M5 — Installer's host-only path validates none of the host-mode prerequisites

> **RESOLVED (this commit).** The installer's `HOST_ONLY` branch now probes
> `python3`, `jq`, `node` (>= 20), and `opencode` and warns (not fails) per
> missing one with the same install hints `host_preflight` prints, so the user
> learns before first `harness host` instead of at first launch.

- **Concern:** The installer's `HOST_ONLY` branch (`harness-install.sh:317-319`,
  final summary `887-896`) tells the user `harness host` "needs Node >= 20,
  opencode, python3, jq" but never *checks* for them at install time — it only
  verifies git/disk/write-access in `preflight()`. The actual check happens later
  in `host_preflight` (`harness:2647`) on first `harness host` run.
- **Impact:** A user does a host-only install, it reports success, and then the
  *first* `harness host` fails on missing node/opencode. The failure is deferred
  from install to first use, with no early signal. Not catastrophic (the error
  messages in `host_preflight` are good and actionable), but it undercuts the
  point of an installer preflight.
- **Severity:** MEDIUM.
- **Verified.** (`harness-install.sh:270-360`, `887-896`; `harness:2647-2682`.)
- **Remediation:** When `HOST_ONLY=1`, have the installer additionally probe
  node>=20 / python3 / jq / opencode and warn (not fail) so the user knows
  before first launch.

---

## LOW

### L1 — `proxy/proxy.py` is a 2516-line single-file monolith

> **DEFERRED (intentional).** A large refactor with no behavior change; the audit
> itself marks it "not urgent; do it opportunistically." Left open.

- **Concern:** All proxy behavior — config read (`proxy.py:56-67`), prompt-mode
  setup, the cooperative-prompt builders, tool-call extraction, SSE/JSON
  emission, debug dumps (`453-461`), Flask routes (`2127`, `2156`, `2438`),
  validation (`2459`), and `main()` (`2472`) — lives in one module
  (`wc -l` = 2516).
- **Impact:** Every proxy change touches one large file; unrelated edits collide;
  there's no module boundary between wire-translation and Flask plumbing. The
  test file mirrors the same shape. Now that host mode runs the *same* proxy
  unchanged, both modes share this single blast radius.
- **Severity:** LOW (works correctly; maintainability cost only).
- **Verified.** (`proxy/proxy.py`, 2516 lines.)
- **Remediation:** Extract config, prompt-builders, wire-translation, and Flask
  app into separate modules with a thin `main`. Not urgent; do it opportunistically.

### L2 — `harness` CLI is a 6589-line bash monolith

> **DEFERRED (intentional).** Same class as L1: a multi-file extraction with no
> behavior change. Left open for an opportunistic pass.

- **Concern:** The single `harness` script is 6589 lines and holds the entire
  CLI: dispatch (`harness:6519`), upgrade/downgrade machinery, net/firewall,
  MCP (container + host), doctor/preflight, and now the host-mode block
  (`harness:2604-3032`).
- **Impact:** High cognitive load; the docker-coupling gaps in H1/H2 partly stem
  from new modes being bolted onto functions (`cmd_doctor`, `cmd_preflight`,
  `cmd_upgrade`) that assume the container world. Cross-cutting changes are
  error-prone at this size.
- **Severity:** LOW (functional; maintainability cost).
- **Verified.** (`harness`, 6589 lines.)
- **Remediation:** Continue extracting cohesive blocks into `scripts/lib/*.sh`
  (the pattern already started with `platform.sh` / `upgrade_actions.sh`). A
  `host.sh` library would isolate the host-mode block and make its
  docker-independence enforceable.

### L3 — `cmd_help` host block omits the `-p`-leaves-proxy-running and config-merge caveats

> **RESOLVED (this commit).** `cmd_host --help` gained a "notes:" block (v1
> single CWD, Linux/macOS only; debug dumps opt-in via `OUTPUT_DIR`; interactive
> stops the proxy, `-p` leaves it; host-only upgrade via `harness upgrade`), and
> the `cmd_help` host entry now points at `harness host --help`.

- **Concern:** The `host` help entry (`harness:1368-1375`) covers the firewall
  warning and dependency list but does not mention: (a) that `-p` leaves the
  proxy running (M1), (b) that there's no `harness upgrade` path for host-only
  installs (C1), or (c) that host mode is "v1: single CWD, no host-MCP wiring,
  Linux/macOS only" — a limitation documented in the code comment
  (`harness:2614-2615`) and architecture doc (`harness-cli.md:340`) but not
  surfaced to users in `--help`.
- **Impact:** Users discover these limits by hitting them. Minor.
- **Severity:** LOW.
- **Verified.** (`harness:1368-1375`, `harness:2614-2615`,
  `architecture/harness-cli.md:340`.)
- **Remediation:** Add one line each to the host help block.

### L4 — Host opencode config bakes `apiKey: "harness-dummy"` and `OPENCODE_ENABLE_EXA=1` with no comment on why

> **RESOLVED (this commit).** Added a comment at the dummy-key call site (the real
> key lives in the proxy) and above the `OPENCODE_ENABLE_EXA=1` export noting it is
> a conscious parity choice in the firewall-less host mode.

- **Concern:** `host_write_opencode_config` writes `"apiKey": "harness-dummy"`
  (`harness:2899`) and `host_run_opencode` exports `OPENCODE_ENABLE_EXA=1`
  (`harness:2926`). The dummy key is correct (the real key lives in the proxy,
  not opencode) but undocumented at the call site, and the Exa flag silently
  enables a web-search provider in a mode that has *no egress firewall* — exactly
  the mode where unrestricted outbound is most concerning.
- **Impact:** Minor; the Exa enablement in firewall-less mode is worth a conscious
  decision rather than an unremarked copy from the container entrypoint.
- **Severity:** LOW.
- **Verified.** (`harness:2899`, `harness:2920-2926`.)
- **Remediation:** Comment the dummy-key rationale; confirm `OPENCODE_ENABLE_EXA`
  in firewall-less host mode is intended (it likely is, for parity, but the
  no-firewall context makes it worth an explicit note).

---

## Areas checked and found sound (no action)

- **Host proxy supervision lifecycle** (`host_proxy_pid`/`running`/`stop`,
  `harness:2627-2846`): stale-pidfile cleanup, TERM-then-KILL, and idempotent
  stop are correct and mirror the host-MCP model.
- **`host_confirm_gate`** (`harness:2710-2746`): correctly refuses non-interactive
  without `/dev/tty`, honors `HARNESS_HOST_CONFIRM=1`, and the warning text is
  appropriately blunt about the no-isolation blast radius.
- **`require_docker` host hint** (`harness:525-527`, `533-534`): both failure
  paths now point users at `harness host`, which is the right nudge.
- **`host_proxy_ensure_venv` stamp** (`harness:2751-2778`): the
  requirements-hash stamp correctly avoids re-running pip on every launch.
- **Secret redaction in proxy startup banner** (`proxy.py:2498`, `_redact_key`):
  the upstream key is redacted in the proxy's own stdout banner, and `cmd_doctor`
  redacts `PROXY_API_KEY` (`harness:5762`). The host proxy logfile
  (`host_proxy_logfile`) captures this redacted banner, not the raw key.

---

*Concerns audit: 2026-06-08*
