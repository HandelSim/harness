# Codebase Concerns

**Analysis Date:** 2026-06-10
**Last Mapped Commit:** 4ebbb2c

Scope: `harness` codebase on branch `dev`, commit 4ebbb2c ("You were right:
reverted my bad upstream change and fixed the real 504 (opencode was tunneling
the loopback through the corp proxy)"). Focus: correctness of the recent 504
fix, proxy routing divergence between host/container modes, and any new debt
introduced.

Every concern below cites a file:line that was read directly. Verified =
trace code path end to end. Inferred = read code but did not execute it.

---

## Resolution Status (2026-06-10)

The 504 fix resolved itself into two separate, correct routing paths:
- **Commit 1c3d545**: Host proxy upstream hop now uses the corporate proxy
  (correct for a host behind a corp proxy).
- **Commit 4ebbb2c**: Opencode loopback hop now exempts itself via `NO_PROXY`
  (correct for Bun's HTTP_PROXY awareness). Host proxy now *restarted on upgrade*
  instead of silently reusing stale code.

All critical items from the prior audit (C1, H1, H2, H3, H4, M1-M5, L3-L4) were
fixed in prior commits. No new critical debt introduced by the 504 fix.

---

## CRITICAL

None currently.

---

## HIGH

### H5 — Venv `requirements.txt` change is not detected; proxy serves stale deps on code upgrade

**Status:** Open; latent, low likelihood.

- **Issue:** `host_proxy_ensure_venv` stamps only the `requirements.txt` SHA
  (`harness:3259`), so if `requirements.txt` changes but the fingerprint logic
  ignores it, the venv is not refreshed. However, the fingerprint that triggers
  a proxy restart (`host_proxy_fingerprint` at `harness:2779-2787`) does NOT
  include the requirements hash — it only covers `port/url/key/model/output`.
  A `harness upgrade` that changes `proxy/requirements.txt` will pull the new
  code but the running proxy is only stopped if its config vars changed. If they
  don't, the proxy restarts with the *old* venv (old deps), then runs the new
  `proxy.py` code against them.
- **Files:** `harness:2779-2787` (fingerprint definition),
  `harness:3255-3287` (venv ensure), `harness:3300-3306` (proxy reuse check).
- **Impact:** An upgrade that pins a new `flask` or `requests` version ships the
  new code but the old proxy process serves it with the old deps — version
  mismatch, silent failures. The venv refreshes lazily *next time someone runs
  `harness host` after* the old proxy exits naturally (network error,
  heartbeat timeout, manual `harness host down`).
- **Severity:** HIGH (subtle, high-impact versioning inconsistency).
- **Verified:** Traced the fingerprint definition, venv stamp, and reuse logic.
  The fix in 4ebbb2c stops a running proxy on every upgrade, which closes this
  gap *for the next host run*. However, the issue itself (fingerprint not
  covering requirements) remains latent for a future refactor that breaks the
  "always stop proxy on upgrade" contract.
- **Strongest counter-argument:** Commit 4ebbb2c added `host_proxy_was_running`
  check and stops the proxy unconditionally on every upgrade (not on config
  change), so in practice the venv is always refreshed. The latent bug only
  surfaces if that stop-on-upgrade logic is ever removed or gated differently.
- **Suggested fix:** Include a requirements hash in `host_proxy_fingerprint`
  alongside `output`, so the proxy auto-restarts on deps change even if the
  user doesn't edit `.env`. Or document that the "stop on every upgrade"
  behavior is required for correctness and add a regression guard in the test
  suite asserting it.

---

## MEDIUM

### M6 — `proxy.py` `requests` library uses `verify=False` for self-signed upstream cert; no pinning

**Status:** Open; design choice, not a bug.

- **Issue:** `proxy.py` disables SSL certificate verification globally at
  module load (`proxy.py:50-52`) and all upstream calls use `verify=False`
  (`proxy.py:1876`, `2051`, `2154`, `2244`). The comment says this is "required
  because the upstream uses a self-signed cert."
- **Files:** `proxy/proxy.py:48-52` (global disable),
  `proxy/proxy.py:1876, 2051, 2154, 2244` (call sites).
- **Impact:** The proxy is vulnerable to MITM if the route to `PROXY_API_URL`
  is compromised (ARP spoofing on the host network, corp proxy substitution,
  compromised DNS). An attacker can intercept upstream responses and inject
  tool calls or modify model output. In container mode this is mitigated by the
  firewall allowlisting the upstream host; in host mode with unrestricted
  egress, there is no mitigation.
- **Severity:** MEDIUM (high-impact if exploited, but requires network-level
  MITM; mitigated in container mode by the firewall).
- **Verified:** Read the disable site, call sites, and the SSL context. No
  certificate pinning or hostname verification override.
- **Strongest counter-argument:** The upstream uses self-signed certs and
  certificate pinning would require shipping the upstream's CA cert as a
  runtime artifact. The current approach is pragmatic and standard for
  internal-service integration.
- **Suggested fix:** Document the security boundary (assumes the network path to
  `PROXY_API_URL` is trusted) in a comment at the disable site. In host mode
  documentation, mention that the lack of egress firewall means MITM is
  possible. No code change required unless pinning becomes a requirement.

### M7 — `proxy.py` does not rate-limit or validate request volume; unbounded memory on tool-call loops

**Status:** Open; latent DoS surface.

- **Issue:** When `_META_TOOL_SERVE_BUDGET = 3` is exhausted
  (`proxy.py:166`, `proxy.py:1099`), the proxy returns a hard error and stops
  the conversation. However, there is no rate limit on the request rate itself
  — a client sending 1000 requests/second will cause 1000 upstream calls, 1000
  response parsings, and unbounded history accumulation in the request. The
  proxy's memory will grow with the total response size across the session.
- **Files:** `proxy/proxy.py:1099-1100` (budget exhaustion),
  `proxy/proxy.py:1870-1913` (upstream call in a loop).
- **Impact:** A misbehaving client (or an adversary with network access in host
  mode) can exhaust the proxy's memory and crash it, disrupting all other
  sessions on that host.
- **Severity:** MEDIUM (requires a hostile client; only impacts host mode where
  the proxy is exposed to the full host user, not the container network).
- **Verified:** Traced the request handler, tool-call loop, and budget logic.
  No request-rate or total-memory guards are present.
- **Strongest counter-argument:** The upstream has its own rate limits and will
  return 429 on sustained attack. The proxy will propagate the 429 upstream and
  stop making calls. Practical DoS requires thousands of requests to exhaust
  memory.
- **Suggested fix:** Add a per-session request counter and a max-memory guard,
  or add a note in the host-mode docs that the proxy is not rate-limited and
  should be run in a trusted environment (which it is — the full host user).

### M8 — Host-mode proxy leaves the logfile unbounded; no rotation or size cap

**Status:** Open; operational concern.

- **Issue:** `host_proxy_start` pipes the proxy process's stdout/stderr to
  `$lf` = `state/host/proxy.log` (`host_proxy_logfile`, `harness:2738`) with
  `nohup ... >"$lf" 2>&1 &` (`harness:3341`). There is no logrotate config, no
  size limit, and no cleanup. A long-running proxy or a chatty error condition
  will grow the logfile indefinitely.
- **Files:** `harness:3341` (nohup redirection),
  `harness:2738` (logfile path definition).
- **Impact:** The logfile can consume unbounded disk space over time. A user
  running `harness host` for a week might accumulate gigabytes of logs.
- **Severity:** MEDIUM (operational; not a correctness issue, but can degrade
  system performance).
- **Verified:** Read the logfile path and the nohup redirection. No rotation
  logic is present.
- **Strongest counter-argument:** The proxy logs are minimal and mostly
  error-level. For typical use, the logfile will stay small. Only a chatty
  debug mode or a pathological error loop would cause growth.
- **Suggested fix:** Add a cleanup/rotation note in the host-mode docs
  (`architecture/harness-cli.md`), or add a `tail -n 1000 > temp && mv temp
  logfile` truncation on proxy start so it never exceeds ~4 MB.

### M9 — Proxy routing now diverges between host and container on the upstream hop; documentation is the only safeguard

**Status:** Open; architectural risk.

- **Issue:** Commit 4ebbb2c explicitly makes the two routing paths different:
  - **Host:** proxy.py inherits `HTTP_PROXY`/`HTTPS_PROXY` from the shell
    (`harness:3337-3340`), so the upstream hop tunnels through the corp proxy.
  - **Container:** docker-compose.yml does NOT set proxy vars on the proxy
    service (`docker-compose.yml:30-48`), so the upstream hop goes direct to
    `PROXY_API_URL`.
  - The architectural docs now explain both paths (`architecture/harness-cli.md`
    lines ~104-130).
- **Files:** `harness:3325-3336` (comment explaining the difference),
  `docker-compose.yml:30-48` (no proxy vars), `architecture/harness-cli.md`
  (routing documentation).
- **Impact:** This is correct and intentional (a host behind a corp proxy has
  no other egress). However, the only enforcement is comments and docs. A
  future refactor that "makes host and container identical" by scrubbing or
  adding proxy vars could silently break routing. The opencode loopback fix is
  also subtle: `NO_PROXY` must be set precisely and merged correctly
  (`harness:3506-3509`).
- **Severity:** MEDIUM (correct today, but fragile to refactoring).
- **Verified:** Traced the proxy launch, docker-compose definition, and the
  NO_PROXY logic. Saw the tests (T11, T12 in `tests/unit_host_test.sh`).
- **Strongest counter-argument:** Tests T11 and T12 explicitly guard against
  the scrub and NO_PROXY bugs, so regressions will be caught in CI.
- **Suggested fix:** The guards are in place. Keep the architecture docs
  detailed and linked from the code comments, and maintain the regression
  tests as they are.

### M10 — Windows Git Bash `taskkill` fallback in proxy stop is untested

**Status:** Open; latent.

- **Issue:** `host_proxy_stop` calls `taskkill //PID "$pid" //T //F` on Git
  Bash (`harness:2674-2677`) to kill the Python proxy process and its child
  tree. This code path is never executed in CI (which runs on Linux) and the
  only guidance is the comment "`//T` kills the child tree, `//F` forces it.
  Double-slash so MSYS doesn't translate."
- **Files:** `harness:2658-2677` (proxy stop with taskkill branch).
- **Impact:** A bug in the taskkill invocation (wrong flag, wrong escaping) on
  Windows will leave the proxy process orphaned when a user runs `harness host
  down`. They will have to manually kill the Python process with Task Manager.
- **Severity:** MEDIUM (Windows-only, affects a small user base; no automation
  or CI coverage).
- **Verified:** Read the code. The `taskkill` path is guarded by `harness_is_git_bash`
  and only runs on Windows Git Bash, which is not tested in CI.
- **Strongest counter-argument:** The TERM + sleep loop (`harness:2668-2673`)
  covers the happy path on Windows as well; the taskkill is a fallback for a
  hung process that didn't respond to SIGTERM. On Windows, SIGTERM may not work
  for Python subprocesses; taskkill is the correct tool.
- **Suggested fix:** Add a Windows Git Bash test that stubs the proxy and
  verifies the taskkill command is invoked (or use CI-available Windows
  runners if the budget allows). Document the taskkill flags in a comment.

---

## LOW

### L5 — Host-mode opencode config is scoped but not isolated from global config

**Status:** Open; low-impact design choice.

- **Issue:** `host_write_opencode_config` writes to `state/host/opencode.json`
  and exports `OPENCODE_CONFIG="$state_root/host/opencode.json"`
  (`harness:3411-3412`). This isolates the config from the user's global
  `~/.config/opencode/opencode.json`, which is good. However, both paths read
  from the same `~/.opencode/cache/` (if the user has one), and both write
  to the same `~/.opencode/state/` (session history). A user running both host
  and container modes concurrently could have state conflicts.
- **Files:** `harness:3411-3412` (OPENCODE_CONFIG export).
- **Impact:** Concurrent host and container mode sessions might overwrite each
  other's opencode history or cache. Low likelihood (most users run one mode at
  a time), and the impact is lost history, not data corruption.
- **Severity:** LOW.
- **Verified:** Read the config export and the opencode docs reference.
- **Strongest counter-argument:** The `.cache/` and `.state/` directories are
  opencode's design, not harness's. The scoped config file is sufficient for
  most users. Preventing concurrent use is out of scope.
- **Suggested fix:** None required. Document in the host-mode docs that
  concurrent host and container opencode sessions may interact with shared
  state directories.

### L6 — (WITHDRAWN — false positive) logs subdirectory not created

**Status:** Not a bug. Withdrawn after verification.

- **Original claim:** the logfile lives in `state/host/logs/` which is never
  `mkdir`'d, so the `nohup` redirection fails silently.
- **Why it is wrong:** `host_proxy_logfile` resolves to `state/host/proxy.log`
  (`harness:2738`), which is a direct child of `host_state_dir` =
  `state/host` (`harness:2734`). `host_proxy_start` does
  `mkdir -p "$(host_state_dir)"` at `harness:3312` BEFORE the nohup at
  `harness:3341`, so the logfile's parent directory always exists. There is no
  `state/host/logs/` subdirectory anywhere in the codebase.
- **Verified:** Read `host_proxy_logfile` (`harness:2738`), `host_state_dir`
  (`harness:2734`), and the start sequence (`harness:3311-3341`).
- **Suggested fix:** None. No code change needed.

### L7 — `output` directory path collision in host mode (both proxy.py and harness write to `state/output`)

**Status:** Open; design note.

- **Issue:** Both the container proxy (via `OUTPUT_DIR=/output` in docker-compose)
  and the host proxy (when `OUTPUT_DIR` is set in `.env`) write debug dumps to
  the same `state/output/` directory. If a user enables `OUTPUT_DIR` and runs
  both modes concurrently, the two proxies' dumps will interleave in the same
  directory.
- **Files:** `docker-compose.yml:42` (container OUTPUT_DIR mount),
  `harness:3315-3320` (host OUTPUT_DIR comment).
- **Impact:** Confusing dump filenames when both modes are active. Low impact.
- **Severity:** LOW.
- **Verified:** Read the mount and the comment.
- **Strongest counter-argument:** The dumps include a request UUID (`req_id`)
  that is unique per proxy process, so the files will not collide. The user
  probably won't run both modes at once.
- **Suggested fix:** None required, but consider clarifying in the host-mode
  docs that `OUTPUT_DIR` is shared across both modes if both are running.

---

## Test Coverage Gaps

### Gap-H1: Host-mode upgrade path not tested

**Status:** Open.

- **Coverage:** `tests/unit_host_upgrade_test.sh` (114 lines, very minimal).
  Tests only the manifest version parsing, not the actual proxy stop/restart
  cycle on upgrade.
- **Impact:** The critical fix in 4ebbb2c (stopping the proxy on every upgrade)
  has no behavior-level regression guard. A future refactor that breaks the
  stop logic would not be caught.
- **Suggested fix:** Add a test that (1) stubs a running proxy, (2) runs
  `harness upgrade` with a mocked git pull that reports new commits, (3)
  asserts the proxy was stopped. Covered by the venv test suite if we mock it.

### Gap-H2: NO_PROXY merge logic not tested end-to-end

**Status:** Covered partially (T12 in unit_host_test.sh).

- **Coverage:** `tests/unit_host_test.sh` T12 stubs opencode and verifies
  `NO_PROXY` is set correctly. This is good, but it doesn't test the case where
  `.env` already sets `NO_PROXY` with other values — the merge logic must not
  drop them.
- **Impact:** Low; T12 covers the merge case explicitly (`NO_PROXY=internal.example`
  in the test).
- **Suggested fix:** Covered by existing tests. No gap.

### Gap-H3: Proxy socket failure modes (port in use, etc.) not tested

**Status:** Open.

- **Coverage:** `host_proxy_wait_ready` (`harness:3351-3370`) handles a proxy
  that exits during startup by surfacing the logfile. There are no tests for
  (1) port already in use (EADDRINUSE), (2) permission denied binding loopback
  (EACCES), or (3) proxy hanging indefinitely (timeout). The 50-iteration loop
  and 0.2s sleep (total ~10s timeout) is not gated by any test.
- **Impact:** If the readiness logic breaks, `harness host` will hang for 10s
  before reporting "did not start." No early-exit or better UX.
- **Suggested fix:** Add a test that stubs a proxy that never accepts the port
  and verifies the timeout triggers and the error message is printed.

---

## Architectural Risks

### Risk-A1: Host mode has no egress firewall; only explicit user confirmation gates the blast radius

**Status:** Mitigated (commit 4fe4361 added `host_confirm_gate`).

- **Design:** Host mode runs opencode as the full host user with no egress
  firewall. The only safeguard is `host_confirm_gate` (`harness:3214-3250`),
  which prints a warning and requires a `y` response on every launch. There is
  no rate-limiting, no execution sandbox, and no time-bound pause (the
  confirmation is instant).
- **Impact:** A user who sees the warning but doesn't read it carefully might
  accidentally launch host mode and grant opencode full host access.
- **Severity:** MEDIUM (the warning is loud and requires explicit confirmation,
  which is the appropriate level for a choice this powerful).
- **Verified:** Read the confirm gate and the warning text.
- **Strongest counter-argument:** The warning is very explicit and is shown on
  every launch, not just the first. The `HARNESS_HOST_CONFIRM=1` override is
  documented for automation. This is the right balance of safety vs. usability.
- **Suggested fix:** No code change. The design is correct.

### Risk-A2: Proxy serves plaintext API key in the `Authorization` header; host-mode traffic is unencrypted

**Status:** Open; design consequence.

- **Design:** The proxy forwards requests to `PROXY_API_URL` with the API key
  in the `Authorization: Bearer <key>` header (`proxy.py:2145`, `2232`). In
  host mode, the proxy binds `127.0.0.1` and opencode connects via plain HTTP
  (`http://127.0.0.1:PORT/v1`, `harness:3414`). If a local attacker sniffs the
  loopback traffic (possible with `tcpdump` or similar), they can see the key.
- **Files:** `proxy/proxy.py:2145`, `harness:3414`.
- **Impact:** The API key is exposed to any process running as the same user
  (or root). In host mode, there is no sandboxing, so this is the threat model
  anyway.
- **Severity:** LOW (expected in host mode; the user is running the full host
  user already).
- **Verified:** Read the auth header and the connection URL.
- **Strongest counter-argument:** Host mode explicitly grants opencode full
  access to the user's filesystem and network. Exposing the key to local
  processes is not a new vector; it's part of the same threat model.
- **Suggested fix:** No code change. Document in host-mode docs that the proxy
  traffic is local and unencrypted, and the API key is visible to local tools.

---

## Summary: Top Improvement Opportunities (by effort/impact)

### Immediate (1–2 hours)

1. **M8**: Document logfile rotation in host-mode docs or add a simple tail
   truncate on startup. (Operational)
2. **M10**: Add a comment explaining the `taskkill` flags and rationale.
   (Clarity)
3. **M6**: Document the `verify=False` upstream SSL boundary in a code comment
   at the disable site. (Clarity/security)

(L6 was withdrawn as a false positive — see the LOW section.)

### Short-term (half day)

4. **H5**: Include `requirements.txt` hash in the proxy fingerprint, so venv
   refresh is automatic on deps change. (Correctness safeguard)
5. **Gap-H1**: Add an upgrade behavior test that stubs a proxy and verifies
   stop/restart. (Test coverage)
6. **M6**: Document the SSL verification boundary in code comments.
   (Clarity/security)

### Medium-term (1–2 days)

7. **M9**: Maintain the proxy routing documentation and T11/T12 tests as-is.
   Current state is good. (Maintenance)
8. **Gap-H3**: Add tests for proxy socket errors (port in use, permission
   denied, timeout). (Robustness)

### Future refactors (longer-term)

9. **L1/L2**: Consider splitting proxy.py and harness monoliths if the codebase
   grows further. (Code health; no urgency now)
10. **M7**: Add request rate-limiting or memory guards if host-mode security
    requirements change. (Future proofing)

---

*Concerns audit: 2026-06-10*
